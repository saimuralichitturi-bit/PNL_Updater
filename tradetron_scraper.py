"""
tradetron_scraper.py
────────────────────
Scrapes all Tradetron strategies + PNL using the saved session.

SESSION SOURCE (in order of priority):
  1. TRADETRON_SESSION env var  ← injected by GitHub Actions from auth step output
  2. tradetron_session.json     ← fallback for local testing

EOD_MODE env var:
  "true"  → End-of-day run: writes pnl_YYYY-MM-DD.csv, pnl_latest.csv,
             snapshot_path.txt  (used by google_drive_uploader + Telegram)
  "false" → Intraday run: scrapes data into pnl_latest.csv ONLY

OUTPUT FILES (EOD only):
  pnl_YYYY-MM-DD.csv           ←  one EOD snapshot per trading day
  pnl_latest.csv               ←  always overwritten (used by Telegram notifier)
  snapshot_path.txt            ←  filename pointer for google_drive_uploader.py
"""

import os
import json
import sys
import requests
import pandas as pd
from datetime import datetime
import pytz

# ── FILE NAMES ─────────────────────────────────────────────────────────────────
SESSION_FILE      = "tradetron_session.json"
SNAPSHOT_PTR_FILE = "snapshot_path.txt"
LATEST_CSV        = "pnl_latest.csv"

# ── Load session ───────────────────────────────────────────────────────────────
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
print(f"[Scraper] Cookies loaded: {list(cookies.keys())}, token: {'yes' if token else 'no'}")

# ── Build requests session ─────────────────────────────────────────────────────
http = requests.Session()
http.cookies.update(cookies)

BASE_HEADERS = {
    "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept":           "application/json, text/plain, */*",
    "Origin":           "https://tradetron.tech",
    "Referer":          "https://tradetron.tech/user/dashboard",
    "X-Requested-With": "XMLHttpRequest",
}
if token:
    BASE_HEADERS["Authorization"] = f"Bearer {token}"
if xsrf:
    BASE_HEADERS["X-XSRF-TOKEN"] = xsrf

BASE = "https://tradetron.tech"

# ── API candidates to try (most likely first) ──────────────────────────────────
API_CANDIDATES = [
    f"{BASE}/api/deployed-strategies",
    f"{BASE}/api/deployed-strategies?page=1&per_page=100&status=all",
    f"{BASE}/api/deployed-strategies?status=active",
    f"{BASE}/api/deployed-strategies?type=live",
    f"{BASE}/api/strategies/deployed",
    f"{BASE}/api/user/strategies",
    f"{BASE}/api/strategies",
]


# ── Fetch strategies ───────────────────────────────────────────────────────────
def fetch_strategies() -> list:
    """
    Try each API candidate. Logs full debug info so we can diagnose 0-result issues.
    Returns first non-empty list found, or [] if all fail.
    """
    for url in API_CANDIDATES:
        print(f"\n[Scraper] Trying: {url}")
        try:
            resp = http.get(url, headers=BASE_HEADERS, timeout=30)
            print(f"[Scraper] Status: {resp.status_code}")

            if resp.status_code != 200:
                print(f"[Scraper] Skipping non-200")
                continue

            data = resp.json()

            # Full debug dump so we can see what the API actually returns
            raw_preview = str(data)[:800]
            print(f"[Scraper] Response preview: {raw_preview}")

            if isinstance(data, dict):
                print(f"[Scraper] Top-level keys: {list(data.keys())}")
                if "paginate" in data:
                    print(f"[Scraper] Paginate block: {data['paginate']}")
                if "success" in data:
                    print(f"[Scraper] success={data['success']}")

            # Extract strategies list
            if isinstance(data, list):
                strategies = data
            elif isinstance(data, dict):
                strategies = (
                    data.get("data")
                    or data.get("strategies")
                    or data.get("result")
                    or data.get("deployed_strategies")
                    or []
                )
            else:
                print(f"[Scraper] Unexpected response type: {type(data)}")
                continue

            if not isinstance(strategies, list):
                print(f"[Scraper] strategies field is not a list: {type(strategies)}")
                continue

            print(f"[Scraper] Strategies on page 1: {len(strategies)}")

            # Pagination
            if isinstance(data, dict) and data.get("paginate"):
                paginate    = data["paginate"]
                total_pages = paginate.get("last_page") or paginate.get("total_pages") or 1
                current     = paginate.get("current_page", 1)
                total_items = paginate.get("total") or paginate.get("total_count", "?")
                print(f"[Scraper] Pagination: page {current}/{total_pages}, total records: {total_items}")

                for page in range(current + 1, total_pages + 1):
                    base_url  = url.split("?")[0]
                    paged_url = f"{base_url}?page={page}&per_page=100"
                    print(f"[Scraper] Fetching page {page}: {paged_url}")
                    pr = http.get(paged_url, headers=BASE_HEADERS, timeout=30)
                    if pr.status_code != 200:
                        print(f"[Scraper] Page {page} returned {pr.status_code} — stopping")
                        break
                    pd_data     = pr.json()
                    page_strats = pd_data.get("data") or pd_data.get("strategies") or []
                    if not page_strats:
                        print(f"[Scraper] Page {page} empty — stopping pagination")
                        break
                    strategies.extend(page_strats)
                    print(f"[Scraper] Page {page}: +{len(page_strats)} (total: {len(strategies)})")

            if strategies:
                print(f"\n[Scraper] Found {len(strategies)} strategies via: {url}")
                print(f"[Scraper] First strategy keys: {list(strategies[0].keys())}")
                return strategies

            print(f"[Scraper] Empty result — trying next endpoint...")

        except Exception as exc:
            print(f"[Scraper] Exception for {url}: {exc}")
            continue

    print("\n[Scraper] ALL ENDPOINTS RETURNED 0 STRATEGIES")
    print("[Scraper] Diagnosis:")
    print("[Scraper]   If paginate showed total=0  -> No deployed strategies on this account")
    print("[Scraper]   If paginate showed total>0  -> Auth or filter issue, check session")
    print("[Scraper]   Verify strategies exist at tradetron.tech/user/dashboard manually")
    return []


# ── Parse a single strategy dict into a flat CSV row ──────────────────────────
def parse_strategy(s: dict) -> dict:
    template        = s.get("template") or {}
    strategy_broker = s.get("strategy_broker") or {}
    broker          = strategy_broker.get("broker") or {}

    all_pnl  = s.get("all_pnl")  or 0
    last_pnl = s.get("last_pnl") or 0
    live_pnl = s.get("globalPt") or 0

    return {
        "Strategy ID":      s.get("id", ""),
        "Strategy Name":    template.get("name", ""),
        "Status":           s.get("status", ""),
        "Deployment Type":  s.get("deployment_type", ""),
        "Exchange":         s.get("exchange", "") or strategy_broker.get("exchange", ""),
        "Broker":           broker.get("name", ""),
        "Capital Required": template.get("capital_required", 0),
        "PNL (Last Run)":   round(float(last_pnl or 0), 2),
        "PNL (Overall)":    round(float(all_pnl  or 0), 2),
        "PNL (Live/Open)":  round(float(live_pnl or 0), 2),
        "Run Counter":      s.get("run_counter")     or 0,
        "Completed Runs":   s.get("max_run_counter") or 0,
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

# ── Empty guard: write skeleton CSV and exit 0 so workflow doesn't fail ────────
if not raw_strategies:
    print("[Scraper] Writing empty CSV with correct headers.")
    empty_df = pd.DataFrame(columns=[
        "Strategy ID", "Strategy Name", "Status", "Deployment Type", "Exchange",
        "Broker", "Capital Required", "PNL (Last Run)", "PNL (Overall)",
        "PNL (Live/Open)", "Run Counter", "Completed Runs", "Currency",
        "Deployment Date", "Creator", "Snapshot Time",
    ])
    empty_df.to_csv(LATEST_CSV, index=False)
    print(f"[Scraper] Empty CSV written to {LATEST_CSV}")
    sys.exit(0)

rows = [parse_strategy(s) for s in raw_strategies]
df   = pd.DataFrame(rows)
df["Snapshot Time"] = timestamp_str

if not df.empty and "Strategy Name" in df.columns:
    df.sort_values("Strategy Name", inplace=True, ignore_index=True)

# ── Write files based on mode ──────────────────────────────────────────────────
EOD_MODE = os.environ.get("EOD_MODE", "false").strip().lower() == "true"

df.to_csv(LATEST_CSV, index=False)

if EOD_MODE:
    df.to_csv(SNAPSHOT_CSV, index=False)
    with open(SNAPSHOT_PTR_FILE, "w") as f:
        f.write(SNAPSHOT_CSV)
    print(f"\n[Scraper] EOD mode — files written:")
    print(f"  {SNAPSHOT_CSV:<38} <- EOD snapshot")
    print(f"  {LATEST_CSV:<38} <- latest copy")
    print(f"  {SNAPSHOT_PTR_FILE:<38} <- pointer for uploader")
else:
    print(f"\n[Scraper] Intraday mode:")
    print(f"  {LATEST_CSV:<38} <- latest copy (for Telegram)")

# ── Summary ────────────────────────────────────────────────────────────────────
total_overall = df["PNL (Overall)"].sum()
total_last    = df["PNL (Last Run)"].sum()
total_live    = df["PNL (Live/Open)"].sum()

print(f"\n[Scraper] Preview:")
print(df[[
    "Strategy Name", "Status", "Broker",
    "PNL (Last Run)", "PNL (Overall)", "PNL (Live/Open)", "Run Counter"
]].to_string())

print(f"\n[Scraper] TOTALS:")
print(f"  Overall PNL  : Rs.{total_overall:>12,.2f}")
print(f"  Last Run PNL : Rs.{total_last:>12,.2f}")
print(f"  Live PNL     : Rs.{total_live:>12,.2f}")
