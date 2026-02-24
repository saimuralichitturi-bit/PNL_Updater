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
  "false" → Intraday run: scrapes data into pnl_latest.csv ONLY (no timestamped
             snapshot, no pointer file).  Drive upload is skipped in the workflow.

OUTPUT FILES (EOD only):
  pnl_YYYY-MM-DD.csv           ←  one EOD snapshot per trading day
  pnl_latest.csv               ←  always overwritten (used by Telegram notifier)
  snapshot_path.txt            ←  filename pointer for google_drive_uploader.py
"""

import os
import json
import requests
import pandas as pd
from datetime import datetime
import pytz

# ── FILE NAMES ─────────────────────────────────────────────────────────────────
SESSION_FILE      = "tradetron_session.json"
SNAPSHOT_PTR_FILE = "snapshot_path.txt"
LATEST_CSV        = "pnl_latest.csv"

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
    "User-Agent":        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept":            "application/json, text/plain, */*",
    "Origin":            "https://tradetron.tech",
    "Referer":           "https://tradetron.tech/user/dashboard",
    "X-Requested-With":  "XMLHttpRequest",
}
if token:
    BASE_HEADERS["Authorization"] = f"Bearer {token}"
if xsrf:
    BASE_HEADERS["X-XSRF-TOKEN"] = xsrf

# ── API endpoints to try (in order) ───────────────────────────────────────────
API_CANDIDATES = [
    "https://tradetron.tech/api/deployed-strategies",
    "https://tradetron.tech/api/deployed-strategies?page=1&per_page=100",
    "https://tradetron.tech/api/strategies/deployed",
    "https://tradetron.tech/api/user/strategies",
]


# ── Fetch strategies (with pagination + fallback endpoints) ───────────────────
def fetch_strategies() -> list:
    """
    Try each API candidate. For each one, handle pagination if present.
    Returns the first non-empty list found, or [] if all fail.
    """
    for url in API_CANDIDATES:
        print(f"[Scraper] Trying → {url}")
        try:
            resp = session.get(url, headers=BASE_HEADERS, timeout=30)
            print(f"[Scraper] Status: {resp.status_code}")

            if resp.status_code != 200:
                print(f"[Scraper] Skipping — non-200 response")
                continue

            data = resp.json()
            print(f"[Scraper] Response keys: {list(data.keys()) if isinstance(data, dict) else 'list'}")

            # Handle both list and dict responses
            if isinstance(data, list):
                strategies = data
            elif isinstance(data, dict):
                # Try common keys
                strategies = (
                    data.get("data")
                    or data.get("strategies")
                    or data.get("result")
                    or data.get("deployed_strategies")
                    or []
                )
            else:
                strategies = []

            if not isinstance(strategies, list):
                print(f"[Scraper] Unexpected shape — skipping")
                continue

            print(f"[Scraper] Found {len(strategies)} strategies on page 1")

            # ── Pagination: keep fetching if paginate info present ────────────
            if isinstance(data, dict) and data.get("paginate"):
                paginate    = data["paginate"]
                total_pages = paginate.get("last_page") or paginate.get("total_pages") or 1
                current     = paginate.get("current_page", 1)
                print(f"[Scraper] Pagination detected — total pages: {total_pages}")

                for page in range(current + 1, total_pages + 1):
                    paged_url = f"{url.split('?')[0]}?page={page}&per_page=100"
                    print(f"[Scraper] Fetching page {page} → {paged_url}")
                    pr = session.get(paged_url, headers=BASE_HEADERS, timeout=30)
                    if pr.status_code != 200:
                        print(f"[Scraper] Page {page} returned {pr.status_code} — stopping pagination")
                        break
                    pd_data      = pr.json()
                    page_strats  = pd_data.get("data") or pd_data.get("strategies") or []
                    if not page_strats:
                        break
                    strategies.extend(page_strats)
                    print(f"[Scraper] Page {page}: +{len(page_strats)} strategies (total so far: {len(strategies)})")

            if strategies:
                print(f"[Scraper] ✓ Got {len(strategies)} strategies total from {url}")
                return strategies

            # Empty list — try next candidate
            print(f"[Scraper] Zero strategies returned — trying next endpoint...")

        except Exception as exc:
            print(f"[Scraper] Error with {url}: {exc} — trying next endpoint...")
            continue

    print("[Scraper] ⚠️  All API endpoints returned 0 strategies.")
    return []


# ── Parse a single strategy dict into a flat CSV row ──────────────────────────
def parse_strategy(s: dict) -> dict:
    template        = s.get("template") or {}
    strategy_broker = s.get("strategy_broker") or {}
    broker          = strategy_broker.get("broker") or {}

    all_pnl  = s.get("all_pnl")  or 0
    last_pnl = s.get("last_pnl") or 0
    live_pnl = s.get("globalPt") or 0

    current_run = s.get("run_counter")     or 0
    max_run     = s.get("max_run_counter") or 0

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

# ── Guard: handle empty strategies gracefully ──────────────────────────────────
if not raw_strategies:
    print("[Scraper] ⚠️  No strategies found — writing empty CSV and exiting cleanly.")
    # Write an empty CSV with correct headers so downstream scripts don't crash
    empty_df = pd.DataFrame(columns=[
        "Strategy ID", "Strategy Name", "Status", "Deployment Type", "Exchange",
        "Broker", "Capital Required", "PNL (Last Run)", "PNL (Overall)",
        "PNL (Live/Open)", "Run Counter", "Completed Runs", "Currency",
        "Deployment Date", "Creator", "Snapshot Time",
    ])
    empty_df.to_csv(LATEST_CSV, index=False)
    print(f"[Scraper] Empty CSV written to {LATEST_CSV}")
    print("[Scraper] Possible reasons:")
    print("  1. No strategies are currently deployed on Tradetron")
    print("  2. Session cookies may have expired — check auth step logs")
    print("  3. API endpoint may have changed — check Tradetron dashboard manually")
    raise SystemExit(0)   # exit 0 so workflow doesn't fail on empty days

rows = [parse_strategy(s) for s in raw_strategies]

df = pd.DataFrame(rows)
df["Snapshot Time"] = timestamp_str

# ── Safe sort — only if column exists and DataFrame is non-empty ───────────────
if not df.empty and "Strategy Name" in df.columns:
    df.sort_values("Strategy Name", inplace=True, ignore_index=True)

# ── Decide what to write based on EOD_MODE ─────────────────────────────────────
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
    print(f"\n[Scraper] Intraday mode — CSV snapshot skipped.")
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

print(f"\n[Scraper] ── TOTALS ──────────────────────────────────────")
print(f"  Overall PNL  (all strategies) : Rs.{total_overall:>12,.2f}")
print(f"  Last Run PNL (all strategies) : Rs.{total_last:>12,.2f}")
print(f"  Live PNL     (all strategies) : Rs.{total_live:>12,.2f}")
