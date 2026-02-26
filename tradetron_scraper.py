"""
tradetron_scraper.py
────────────────────
Scrapes all Tradetron strategies + PNL using cookies from the auth step.

SESSION SOURCE:
  TRADETRON_SESSION env var — injected by GitHub Actions from tradetron_auth.py output.
  Contains: { "cookies": {...}, "xsrf": "..." }

EOD_MODE env var:
  "true"  → saves pnl_YYYY-MM-DD.csv + snapshot_path.txt (for Google Drive)
  "false" → saves pnl_latest.csv only (for Telegram intraday snapshot)
"""

import os
import json
import sys
import requests
import pandas as pd
from datetime import datetime
import pytz
import time

# ── FILE NAMES ─────────────────────────────────────────────────────────────────
SNAPSHOT_PTR_FILE = "snapshot_path.txt"
LATEST_CSV        = "pnl_latest.csv"
BASE_URL          = "https://tradetron.tech"

# ── Load session from env var (set by tradetron_auth.py via GITHUB_OUTPUT) ─────
session_json_str = os.environ.get("TRADETRON_SESSION", "")
if not session_json_str:
    raise RuntimeError(
        "[Scraper] TRADETRON_SESSION env var is not set. "
        "Make sure the auth step ran successfully and exported session_json."
    )

print("[Scraper] Loading session from TRADETRON_SESSION env var")
session_data = json.loads(session_json_str)

cookies = session_data.get("cookies", {})
xsrf    = session_data.get("xsrf", "")

if not cookies:
    raise RuntimeError("[Scraper] No cookies found in session data — auth step likely failed.")

print(f"[Scraper] Cookies loaded: {list(cookies.keys())}")
print(f"[Scraper] XSRF-TOKEN: {'✓ present' if xsrf else '✗ MISSING'}")

# ── Build requests session ─────────────────────────────────────────────────────
session = requests.Session()
session.cookies.update(cookies)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

API_HEADERS = {
    "User-Agent":       UA,
    "Accept":           "application/json, text/plain, */*",
    "Origin":           BASE_URL,
    "Referer":          f"{BASE_URL}/user/dashboard",
    "X-Requested-With": "XMLHttpRequest",
    "X-XSRF-TOKEN":     xsrf,
}

API_BASE_URL = "https://tradetron.tech/api/deployed-strategies"


# ── NEW: Select India (IND) exchange via the language/exchange dropdown ────────
def select_india_exchange() -> None:
    """
    Mimics clicking the exchange dropdown and selecting 'IND — Exchanges for India'.
    Hits the /set/cookie/IN endpoint which sets the exchange preference cookie,
    exactly as the frontend does when the user clicks:
      <a href="https://tradetron.tech/set/cookie/IN">IND</a>
    """
    url = f"{BASE_URL}/set/cookie/IN"
    print(f"[Scraper] Selecting India exchange: GET {url}")

    nav_headers = {
        "User-Agent": UA,
        "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer":    f"{BASE_URL}/user/dashboard",
    }

    try:
        resp = session.get(url, headers=nav_headers, allow_redirects=True, timeout=30)
        print(f"[Scraper] Exchange select status: {resp.status_code} | Final URL: {resp.url}")

        # Log any new/updated cookies
        updated = {k: v for k, v in session.cookies.items()}
        print(f"[Scraper] Cookies after exchange select: {list(updated.keys())}")

        if resp.status_code in (200, 302):
            print("[Scraper] ✓ India (IND) exchange selected successfully")
            
        else:
            print(f"[Scraper] ⚠ Unexpected status {resp.status_code} — continuing anyway")

    except Exception as e:
        # Non-fatal: log and continue; strategies may still be fetched
        print(f"[Scraper] ⚠ Exchange selection request failed: {e} — continuing anyway")


# ── Single page fetch with diagnostics ────────────────────────────────────────
def _fetch_page(url: str) -> dict | None:
    try:
        resp = session.get(url, headers=API_HEADERS, timeout=30)
        print(f"[Scraper]   HTTP {resp.status_code}")

        if resp.status_code == 401:
            print("[Scraper]   → 401 Unauthorized — login likely failed")
            return None

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in content_type:
            preview = resp.text[:300].replace("\n", " ")
            print(f"[Scraper]   → Got HTML instead of JSON (login redirect?): {preview}")
            return None

        if resp.status_code != 200:
            print(f"[Scraper]   → Unexpected status {resp.status_code}")
            return None

        data = resp.json()

        if not data.get("success"):
            preview = str(data)[:400]
            print(f"[Scraper]   → API returned success=false: {preview}")
            return None

        return data

    except Exception as e:
        print(f"[Scraper]   → Exception: {e}")
        return None


# ── Paginated strategy fetch ───────────────────────────────────────────────────
def fetch_strategies() -> list:
    all_strategies = []
    page = 1
    max_pages = 100

    print("[Scraper] Starting strategy fetch...")

    while page <= max_pages:
        url = f"{API_BASE_URL}?page={page}"
        print(f"[Scraper] Fetching page {page}: {url}")

        data = _fetch_page(url)

        if data is None:
            if page == 1:
                # Dump full raw response for diagnosis before giving up
                print("[Scraper] ── Raw response dump (page 1) ────────────────────────")
                try:
                    resp = session.get(url, headers=API_HEADERS, timeout=30)
                    print(f"  Status : {resp.status_code}")
                    print(f"  Headers: {dict(resp.headers)}")
                    print(f"  Body   : {resp.text[:600]}")
                except Exception as e:
                    print(f"  Could not dump response: {e}")
                print("[Scraper] ─────────────────────────────────────────────────────")
                raise RuntimeError(
                    "[Scraper] Failed to fetch page 1. "
                    "Session cookies are likely invalid — check the auth step logs."
                )
            else:
                print(f"[Scraper] Failed on page {page} — treating as end of results")
                break

        strategies = data.get("data", [])
        if not isinstance(strategies, list):
            print(f"[Scraper] Unexpected 'data' type: {type(strategies)} — stopping")
            break

        if not strategies:
            print(f"[Scraper] Empty page {page} — done")
            break

        print(f"[Scraper] ✓ Page {page}: {len(strategies)} strategies")

        # Duplicate check
        new_ids      = {s.get("id") for s in strategies if s.get("id")}
        existing_ids = {s.get("id") for s in all_strategies if s.get("id")}
        if new_ids & existing_ids and page > 1:
            print(f"[Scraper] Duplicate IDs detected — reached end")
            break

        all_strategies.extend(strategies)

        # Pagination metadata
        meta         = data.get("meta") or data.get("pagination") or {}
        current_page = meta.get("current_page", page)
        last_page    = meta.get("last_page") or meta.get("total_pages")
        per_page     = meta.get("per_page", 10)
        total        = meta.get("total")

        print(f"[Scraper]   Meta → current={current_page} last={last_page} per_page={per_page} total={total}")

        if last_page and current_page >= last_page:
            print(f"[Scraper] Reached last page ({last_page})")
            break
        if total and len(all_strategies) >= total:
            print(f"[Scraper] Collected all {total} strategies")
            break

        page += 1
        time.sleep(0.5)

    print(f"[Scraper] ✓ Total collected: {len(all_strategies)} strategies")
    return all_strategies


# ── Parse strategy dict into CSV row ──────────────────────────────────────────
def parse_strategy(s: dict) -> dict:
    template        = s.get("template") or {}
    strategy_broker = s.get("strategy_broker") or {}
    broker          = strategy_broker.get("broker") or {}

    return {
        "Strategy ID":      s.get("id", ""),
        "Strategy Name":    template.get("name", ""),
        "Status":           s.get("status", ""),
        "Deployment Type":  s.get("deployment_type", ""),
        "Exchange":         s.get("exchange", "") or strategy_broker.get("exchange", ""),
        "Broker":           broker.get("name", ""),
        "Capital Required": template.get("capital_required", 0),
        "PNL (Last Run)":   round(float(s.get("last_pnl") or 0), 2),
        "PNL (Overall)":    round(float(s.get("all_pnl")  or 0), 2),
        "PNL (Live/Open)":  round(float(s.get("globalPt") or 0), 2),
        "Run Counter":      s.get("run_counter")     or 0,
        "Completed Runs":   s.get("max_run_counter") or 0,
        "Currency":         s.get("currency_code", "INR"),
        "Deployment Date":  s.get("deployment_date", "")[:10] if s.get("deployment_date") else "",
        "Creator":          (template.get("user") or {}).get("name", ""),
    }


# ── Main ───────────────────────────────────────────────────────────────────────

# Step 0: Select India (IND) exchange — mimics dropdown click in the UI
select_india_exchange()

# Small pause to let the exchange cookie propagate before API calls
time.sleep(8)
raw_strategies = fetch_strategies()

ist           = pytz.timezone("Asia/Kolkata")
now_ist       = datetime.now(ist)
timestamp_str = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
file_date     = now_ist.strftime("%Y-%m-%d")
SNAPSHOT_CSV  = f"pnl_{file_date}.csv"

if not raw_strategies:
    print("[Scraper] ⚠️  No strategies returned — writing empty CSV.")
    pd.DataFrame(columns=[
        "Strategy ID", "Strategy Name", "Status", "Deployment Type", "Exchange",
        "Broker", "Capital Required", "PNL (Last Run)", "PNL (Overall)",
        "PNL (Live/Open)", "Run Counter", "Completed Runs", "Currency",
        "Deployment Date", "Creator", "Snapshot Time",
    ]).to_csv(LATEST_CSV, index=False)
    sys.exit(0)

# Deduplicate by Strategy ID
unique = {}
for s in raw_strategies:
    sid = s.get("id")
    if sid and sid not in unique:
        unique[sid] = s

print(f"[Scraper] Removed {len(raw_strategies) - len(unique)} duplicates")

df = pd.DataFrame([parse_strategy(s) for s in unique.values()])
df["Snapshot Time"] = timestamp_str

if "Strategy Name" in df.columns:
    df.sort_values("Strategy Name", inplace=True, ignore_index=True)

# ── Write CSV files ────────────────────────────────────────────────────────────
EOD_MODE = os.environ.get("EOD_MODE", "false").strip().lower() == "true"

df.to_csv(LATEST_CSV, index=False)

if EOD_MODE:
    df.to_csv(SNAPSHOT_CSV, index=False)
    with open(SNAPSHOT_PTR_FILE, "w") as f:
        f.write(SNAPSHOT_CSV)
    print(f"\n[Scraper] EOD mode:")
    print(f"  {SNAPSHOT_CSV:<38} <- EOD snapshot")
    print(f"  {LATEST_CSV:<38} <- latest copy")
    print(f"  {SNAPSHOT_PTR_FILE:<38} <- pointer for uploader")
else:
    print(f"\n[Scraper] Intraday mode:")
    print(f"  {LATEST_CSV:<38} <- latest copy (for Telegram)")

# ── Summary ────────────────────────────────────────────────────────────────────
print(f"\n[Scraper] Preview (first 10):")
print(df[["Strategy Name", "Status", "Broker",
          "PNL (Last Run)", "PNL (Overall)", "PNL (Live/Open)", "Run Counter"]].head(10).to_string())

print(f"\n[Scraper] ── TOTALS ({len(df)} strategies) ──────────────")
print(f"  Overall PNL  : ₹{df['PNL (Overall)'].sum():>12,.2f}")
print(f"  Last Run PNL : ₹{df['PNL (Last Run)'].sum():>12,.2f}")
print(f"  Live PNL     : ₹{df['PNL (Live/Open)'].sum():>12,.2f}")
