"""
tradetron_scraper.py
────────────────────
Scrapes all Tradetron strategies + PNL + Capital using the saved session.

SESSION SOURCE (in order of priority):
  1. TRADETRON_SESSION env var  ← injected by GitHub Actions from auth step output
  2. tradetron_session.json     ← fallback for local testing

EOD_MODE env var:
  "true"  → saves pnl_YYYY-MM-DD.csv + snapshot_path.txt (for Google Drive)
  "false" → saves pnl_latest.csv only (for Telegram intraday snapshot)

CAPITAL:
  Fetched from individual strategy page HTML:
  <p>Capital:&nbsp;<span class="currency-symbol">₹ </span><span>28.00 L</span></p>
  Converted to raw rupees: 28.00 L → 2800000
"""

import os
import json
import sys
import re
import requests
import pandas as pd
from datetime import datetime
from bs4 import BeautifulSoup
import pytz
import time

# ── FILE NAMES ─────────────────────────────────────────────────────────────────
SESSION_FILE      = "tradetron_session.json"
SNAPSHOT_PTR_FILE = "snapshot_path.txt"
LATEST_CSV        = "pnl_latest.csv"

BASE_URL = "https://tradetron.tech"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

# ── Load session — env var first, then file fallback ──────────────────────────
session_json_str = os.environ.get("TRADETRON_SESSION", "")

if session_json_str:
    print("[Scraper] Loading session from TRADETRON_SESSION env var")
    session_data = json.loads(session_json_str)
else:
    print(f"[Scraper] Loading session from {SESSION_FILE}")
    with open(SESSION_FILE) as f:
        session_data = json.load(f)

cookies = session_data.get("cookies", {})
token   = session_data.get("token")
xsrf    = session_data.get("xsrf", "")

print(f"[Scraper] Loaded session — cookies: {list(cookies.keys())}, token: {'yes' if token else 'no'}")

# ── Build session & headers ────────────────────────────────────────────────────
session = requests.Session()
session.cookies.update(cookies)

BASE_HEADERS = {
    "User-Agent":        UA,
    "Accept":            "application/json, text/plain, */*",
    "Origin":            BASE_URL,
    "Referer":           f"{BASE_URL}/user/dashboard",
    "X-Requested-With":  "XMLHttpRequest",
}
if token:
    BASE_HEADERS["Authorization"] = f"Bearer {token}"
if xsrf:
    BASE_HEADERS["X-XSRF-TOKEN"] = xsrf

HTML_HEADERS = {
    "User-Agent": UA,
    "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer":    f"{BASE_URL}/user/dashboard",
}

API_BASE_URL = f"{BASE_URL}/api/deployed-strategies"


# ── Capital parser ─────────────────────────────────────────────────────────────
def parse_capital_str(capital_str: str) -> float:
    """
    Convert capital string like '28.00 L' or '2.50 Cr' to raw rupees.
    L  = Lakh  = 100,000
    Cr = Crore = 10,000,000
    """
    capital_str = capital_str.strip()
    try:
        if "Cr" in capital_str or "cr" in capital_str:
            num = float(re.sub(r"[^\d.]", "", capital_str))
            return num * 10_000_000
        elif "L" in capital_str or "l" in capital_str:
            num = float(re.sub(r"[^\d.]", "", capital_str))
            return num * 100_000
        else:
            return float(re.sub(r"[^\d.]", "", capital_str))
    except Exception:
        return 0.0


def parse_counter_option(opt_text: str):
    """
    Parse a run_counter <option> label like '87 (₹  52,919)' or '65 (₹  -174,281)'.
    Returns (counter: int, pnl: float) or (None, None) on failure.
    """
    try:
        parts = opt_text.split("(", 1)
        counter = int(parts[0].strip())
        if len(parts) > 1:
            pnl_str = parts[1].rstrip(")")
            # Strip currency symbol, spaces, commas — keep digits, dot, minus
            pnl_str = re.sub(r"[₹\s,]", "", pnl_str)
            pnl = float(pnl_str)
        else:
            pnl = None
        return counter, pnl
    except Exception:
        return None, None


def fetch_strategy_html_data(strategy_id: int) -> dict:
    """
    Fetch the strategy deployed page HTML and extract:
      - capital        : float (raw rupees)
      - latest_counter : int   (first/highest run counter from dropdown)
      - counter_pnl    : float (PNL shown for that counter in the dropdown label)

    After identifying the latest counter, re-fetches the page with
    ?run_counter=<value> so the response reflects the latest run's data.
    """
    url      = f"{BASE_URL}/strategy/deployed/{strategy_id}"
    result   = {"capital": 0.0, "latest_counter": None, "counter_pnl": None}

    try:
        resp = session.get(url, headers=HTML_HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"[Scraper] HTML fetch failed for {strategy_id}: HTTP {resp.status_code}")
            return result

        soup = BeautifulSoup(resp.text, "html.parser")

        # ── Capital ──────────────────────────────────────────────────────────
        for p in soup.find_all("p"):
            text = p.get_text(separator=" ", strip=True)
            if "Capital" in text:
                spans = p.find_all("span")
                for span in spans:
                    span_text = span.get_text(strip=True)
                    if "₹" in span_text or span_text == "":
                        continue
                    cap = parse_capital_str(span_text)
                    if cap > 0:
                        result["capital"] = cap
                        break
                if result["capital"] > 0:
                    break

        # Fallback: regex in raw HTML
        if result["capital"] == 0:
            m = re.search(
                r"Capital[^<]*<[^>]+>[^<]*<\/[^>]+><span[^>]*>([\d.,]+\s*(?:L|Cr|K)?)<\/span>",
                resp.text,
            )
            if m:
                result["capital"] = parse_capital_str(m.group(1))

        # ── Run Counter dropdown ─────────────────────────────────────────────
        # id pattern: run_counter_<strategy_id>  (e.g. run_counter_25764945)
        select = soup.find("select", id=re.compile(r"^run_counter_"))
        if not select:
            select = soup.find("select", {"name": "run_counter"})

        if select:
            options = select.find_all("option")
            for opt in options:
                val = opt.get("value", "")
                if not val or val == "All":
                    continue
                counter, pnl = parse_counter_option(opt.get_text(strip=True))
                if counter is not None:
                    result["latest_counter"] = counter
                    result["counter_pnl"]    = pnl
                    print(f"[Scraper]   id={strategy_id} → latest counter={counter}, counter PNL={pnl}")
                    break  # first numeric option = latest
        else:
            print(f"[Scraper]   id={strategy_id}: run_counter select not found in HTML")

        # ── Re-fetch with the latest counter to load run-specific data ───────
        if result["latest_counter"] is not None:
            counter_url = f"{url}?run_counter={result['latest_counter']}"
            try:
                cr = session.get(counter_url, headers=HTML_HEADERS, timeout=20)
                if cr.status_code == 200:
                    # Capital re-parse from run-specific view (may be same, just being thorough)
                    cr_soup = BeautifulSoup(cr.text, "html.parser")
                    for p in cr_soup.find_all("p"):
                        text = p.get_text(separator=" ", strip=True)
                        if "Capital" in text:
                            for span in p.find_all("span"):
                                span_text = span.get_text(strip=True)
                                if "₹" in span_text or span_text == "":
                                    continue
                                cap = parse_capital_str(span_text)
                                if cap > 0 and result["capital"] == 0:
                                    result["capital"] = cap
                                    break
                            break
            except Exception as ce:
                print(f"[Scraper]   Counter re-fetch error for {strategy_id}: {ce}")

    except Exception as e:
        print(f"[Scraper] HTML data fetch error for {strategy_id}: {e}")

    return result


# ── Fetch strategies with pagination ──────────────────────────────────────────
def fetch_strategies():
    all_strategies = []
    MAX_PAGES = 2  # Tradetron has exactly 2 pages of strategies

    print(f"[Scraper] Starting pagination scan (max {MAX_PAGES} pages)...")

    for page in range(1, MAX_PAGES + 1):
        url = f"{API_BASE_URL}?page={page}"
        try:
            print(f"[Scraper] Page {page}: GET {url}")
            resp = session.get(url, headers=BASE_HEADERS, timeout=30)

            if resp.status_code != 200:
                print(f"[Scraper] HTTP {resp.status_code} on page {page}")
                break

            data = resp.json()

            if not data.get("success"):
                print(f"[Scraper] success=false on page {page} — end of results")
                break

            strategies = data.get("data", [])
            if not isinstance(strategies, list) or not strategies:
                print(f"[Scraper] No strategies on page {page} — done")
                break

            # Deduplicate
            existing_ids = {s.get("id") for s in all_strategies}
            new = [s for s in strategies if s.get("id") not in existing_ids]
            if not new:
                print(f"[Scraper] All duplicates on page {page} — done")
                break

            all_strategies.extend(new)
            print(f"[Scraper] ✓ Page {page}: {len(new)} new strategies (total: {len(all_strategies)})")

        except Exception as e:
            print(f"[Scraper] Error on page {page}: {e}")
            break

        time.sleep(0.3)

    print(f"[Scraper] ✓ Total strategies fetched: {len(all_strategies)}")
    return all_strategies


# ── Parse a single strategy dict ──────────────────────────────────────────────
def parse_strategy(s: dict) -> dict:
    template        = s.get("template") or {}
    strategy_broker = s.get("strategy_broker") or {}
    broker          = strategy_broker.get("broker") or {}

    all_pnl  = s.get("all_pnl")  or 0
    last_pnl = s.get("last_pnl") or 0
    live_pnl = s.get("globalPt") or 0

    current_run = s.get("run_counter")     or 0
    max_run     = s.get("max_run_counter") or 0

    # Capital from API (may be 0 — will be overridden by HTML scrape)
    api_capital = float(template.get("capital_required") or 0)

    return {
        "Strategy ID":      s.get("id", ""),
        "Strategy Name":    template.get("name", ""),
        "Status":           s.get("status", ""),
        "Deployment Type":  s.get("deployment_type", ""),
        "Exchange":         s.get("exchange", "") or strategy_broker.get("exchange", ""),
        "Broker":           broker.get("name", ""),
        "Capital Required": api_capital,
        "PNL (Last Run)":   round(float(last_pnl or 0), 2),
        "PNL (Overall)":    round(float(all_pnl  or 0), 2),
        "PNL (Live/Open)":  round(float(live_pnl or 0), 2),
        "Run Counter":      current_run,
        "Completed Runs":   max_run,
        "Currency":         s.get("currency_code", "INR"),
        "Deployment Date":  s.get("deployment_date", "")[:10] if s.get("deployment_date") else "",
        "Creator":          (template.get("user") or {}).get("name", ""),
    }


# ── Fetch & build DataFrame ────────────────────────────────────────────────────
raw_strategies = fetch_strategies()

ist           = pytz.timezone("Asia/Kolkata")
now_ist       = datetime.now(ist)
timestamp_str = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
file_date     = now_ist.strftime("%Y-%m-%d")
SNAPSHOT_CSV  = f"pnl_{file_date}.csv"

if not raw_strategies:
    print("[Scraper] ⚠️  No strategies returned — writing empty CSV.")
    empty_df = pd.DataFrame(columns=[
        "Strategy ID", "Strategy Name", "Status", "Deployment Type", "Exchange",
        "Broker", "Capital Required", "Capital (HTML)", "PNL (Last Run)",
        "PNL (Overall)", "PNL (Live/Open)", "Run Counter", "Completed Runs",
        "Currency", "Deployment Date", "Creator",
        "Latest Counter", "Counter PNL", "Snapshot Time",
    ])
    empty_df.to_csv(LATEST_CSV, index=False)
    sys.exit(0)

# Deduplicate
unique_strategies = {}
for s in raw_strategies:
    sid = s.get("id")
    if sid and sid not in unique_strategies:
        unique_strategies[sid] = s

print(f"[Scraper] Removed {len(raw_strategies) - len(unique_strategies)} duplicates")
rows = [parse_strategy(s) for s in unique_strategies.values()]
df = pd.DataFrame(rows)

# ── Scrape capital + latest run counter from HTML for each strategy ────────────
print(f"\n[Scraper] Fetching capital & latest run counter from HTML for {len(df)} strategies...")
html_capitals    = []
latest_counters  = []
counter_pnls     = []

for _, row in df.iterrows():
    sid  = row["Strategy ID"]
    name = row["Strategy Name"]
    data = fetch_strategy_html_data(int(sid))

    cap     = data["capital"]
    counter = data["latest_counter"]
    cpnl    = data["counter_pnl"]

    # Fallback to API capital if HTML scrape returns 0
    if cap == 0 and row["Capital Required"] > 0:
        cap = row["Capital Required"]
        print(f"[Scraper]   {name}: using API capital ₹{cap:,.0f}")
    else:
        print(f"[Scraper]   {name}: ₹{cap:,.0f}")

    html_capitals.append(cap)
    latest_counters.append(counter)
    counter_pnls.append(cpnl)
    time.sleep(0.5)  # be polite

df["Capital (HTML)"]  = html_capitals
df["Latest Counter"]  = latest_counters
df["Counter PNL"]     = counter_pnls

# Use HTML capital as primary, fallback to API capital
df["Capital"] = df.apply(
    lambda r: r["Capital (HTML)"] if r["Capital (HTML)"] > 0 else r["Capital Required"],
    axis=1
)

df["Snapshot Time"] = timestamp_str

if "Strategy Name" in df.columns:
    df.sort_values("Strategy Name", inplace=True, ignore_index=True)

# ── Write files ────────────────────────────────────────────────────────────────
EOD_MODE = os.environ.get("EOD_MODE", "false").strip().lower() == "true"

df.to_csv(LATEST_CSV, index=False)

if EOD_MODE:
    df.to_csv(SNAPSHOT_CSV, index=False)
    with open(SNAPSHOT_PTR_FILE, "w") as f:
        f.write(SNAPSHOT_CSV)
    print(f"\n[Scraper] EOD mode — files written:")
    print(f"  {SNAPSHOT_CSV}")
    print(f"  {LATEST_CSV}")
    print(f"  {SNAPSHOT_PTR_FILE}")
else:
    print(f"\n[Scraper] Intraday mode: {LATEST_CSV}")

# ── Summary ────────────────────────────────────────────────────────────────────
total_overall = df["PNL (Overall)"].sum()
total_last    = df["PNL (Last Run)"].sum()
total_live    = df["PNL (Live/Open)"].sum()
total_capital = df["Capital"].sum()

print(f"\n[Scraper] Preview:")
preview_df = df[[
    "Strategy Name", "Status", "Capital",
    "Latest Counter", "Counter PNL",
    "PNL (Last Run)", "PNL (Overall)", "PNL (Live/Open)"
]].head(10)
print(preview_df.to_string())

print(f"\n[Scraper] ── TOTALS ({len(df)} strategies) ──")
print(f"  Total Capital : ₹{total_capital:>12,.0f}")
print(f"  Overall PNL   : ₹{total_overall:>12,.2f}")
print(f"  Last Run PNL  : ₹{total_last:>12,.2f}")
print(f"  Live PNL      : ₹{total_live:>12,.2f}")
print(f"  Overall ROI   : {(total_overall/total_capital*100) if total_capital else 0:.2f}%")