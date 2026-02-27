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


def fetch_strategy_capital(strategy_id: int) -> float:
    """
    Fetch the capital for a single strategy from its detail page HTML.
    Looks for: <p>Capital:&nbsp;<span class="currency-symbol">₹ </span><span>28.00 L</span></p>
    """
    url = f"{BASE_URL}/strategy/deployed/{strategy_id}"
    try:
        resp = session.get(url, headers=HTML_HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"[Scraper] Capital fetch failed for {strategy_id}: HTTP {resp.status_code}")
            return 0.0

        soup = BeautifulSoup(resp.text, "html.parser")

        # Find <p> containing "Capital"
        for p in soup.find_all("p"):
            text = p.get_text(separator=" ", strip=True)
            if "Capital" in text:
                # Get all spans, last one has the value like "28.00 L"
                spans = p.find_all("span")
                for span in spans:
                    span_text = span.get_text(strip=True)
                    # Skip currency symbol span
                    if "₹" in span_text or span_text == "":
                        continue
                    capital = parse_capital_str(span_text)
                    if capital > 0:
                        return capital

        # Fallback: regex search in raw HTML
        match = re.search(r"Capital[^<]*<[^>]+>[^<]*<\/[^>]+><span[^>]*>([\d.,]+\s*(?:L|Cr|K)?)<\/span>", resp.text)
        if match:
            return parse_capital_str(match.group(1))

        print(f"[Scraper] Capital not found in HTML for strategy {strategy_id}")
        return 0.0

    except Exception as e:
        print(f"[Scraper] Capital fetch error for {strategy_id}: {e}")
        return 0.0


# ── Fetch strategies with pagination ──────────────────────────────────────────
def fetch_strategies():
    all_strategies = []
    page = 1
    max_pages = 100

    print(f"[Scraper] Starting pagination scan...")

    while page <= max_pages:
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

        page += 1
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
        "Currency", "Deployment Date", "Creator", "Snapshot Time",
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

# ── Scrape capital from HTML for each strategy ─────────────────────────────────
print(f"\n[Scraper] Fetching capital from HTML for {len(df)} strategies...")
html_capitals = []
for _, row in df.iterrows():
    sid  = row["Strategy ID"]
    name = row["Strategy Name"]
    cap  = fetch_strategy_capital(int(sid))

    # Fallback to API capital if HTML scrape returns 0
    if cap == 0 and row["Capital Required"] > 0:
        cap = row["Capital Required"]
        print(f"[Scraper]   {name}: using API capital ₹{cap:,.0f}")
    else:
        print(f"[Scraper]   {name}: ₹{cap:,.0f}")

    html_capitals.append(cap)
    time.sleep(0.5)  # be polite

df["Capital (HTML)"] = html_capitals

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
    "PNL (Last Run)", "PNL (Overall)", "PNL (Live/Open)"
]].head(10)
print(preview_df.to_string())

print(f"\n[Scraper] ── TOTALS ({len(df)} strategies) ──")
print(f"  Total Capital : ₹{total_capital:>12,.0f}")
print(f"  Overall PNL   : ₹{total_overall:>12,.2f}")
print(f"  Last Run PNL  : ₹{total_last:>12,.2f}")
print(f"  Live PNL      : ₹{total_live:>12,.2f}")
print(f"  Overall ROI   : {(total_overall/total_capital*100) if total_capital else 0:.2f}%")
