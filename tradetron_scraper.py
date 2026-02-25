"""
tradetron_scraper.py
────────────────────
Scrapes all Tradetron strategies + PNL using the saved session.
Now with improved pagination handling.

SESSION SOURCE (in order of priority):
  1. TRADETRON_SESSION env var  ← injected by GitHub Actions from auth step output
  2. tradetron_session.json     ← fallback for local testing

EOD_MODE env var:
  "true"  → saves pnl_YYYY-MM-DD.csv + snapshot_path.txt (for Google Drive)
  "false" → saves pnl_latest.csv only (for Telegram intraday snapshot)

PAGINATION:
  Tries multiple pagination approaches to ensure all strategies are scraped.
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

# ── API endpoint ───────────────────────────────────────────────────────────────
API_BASE_URL = "https://tradetron.tech/api/deployed-strategies"


# ── Fetch strategies with multiple pagination attempts ─────────────────────────
def fetch_strategies():
    """
    Fetch all strategies across multiple pages.
    Tries different pagination parameter formats to ensure compatibility.
    """
    all_strategies = []
    page = 1
    max_pages = 100  # Safety limit
    
    print(f"[Scraper] Starting pagination scan...")
    
    while page <= max_pages:
        # Try different pagination formats
        urls_to_try = [
            f"{API_BASE_URL}?page={page}",           # Standard REST pagination
            f"{API_BASE_URL}?p={page}",              # Alternative format
            f"{API_BASE_URL}?offset={(page-1)*10}",  # Offset-based
        ]
        
        success = False
        for url in urls_to_try:
            if page > 1 and success:  # Only try alternatives on first failure
                break
                
            try:
                print(f"[Scraper] Attempting: {url}")
                resp = session.get(url, headers=BASE_HEADERS, timeout=30)
                
                if resp.status_code != 200:
                    print(f"[Scraper] Status {resp.status_code} - trying next format")
                    continue

                data = resp.json()
                
                # Check for success flag
                if not data.get("success"):
                    print(f"[Scraper] API returned success=false - trying next format")
                    continue

                strategies = data.get("data")
                if not isinstance(strategies, list):
                    print(f"[Scraper] Unexpected data format - trying next format")
                    continue

                # Empty list means we've reached the end
                if not strategies:
                    if page == 1:
                        print(f"[Scraper] No strategies on first page - trying next format")
                        continue
                    else:
                        print(f"[Scraper] No more strategies - pagination complete")
                        return all_strategies
                
                print(f"[Scraper] ✓ Page {page}: Found {len(strategies)} strategies")
                
                # Check if we got duplicate strategies (indicates we hit the end)
                new_ids = {s.get("id") for s in strategies if s.get("id")}
                existing_ids = {s.get("id") for s in all_strategies if s.get("id")}
                duplicates = new_ids & existing_ids
                
                if duplicates and page > 1:
                    print(f"[Scraper] Found {len(duplicates)} duplicate strategies - reached end")
                    return all_strategies
                
                all_strategies.extend(strategies)
                success = True
                
                # Check pagination metadata
                meta = data.get("meta") or data.get("pagination") or data.get("links") or {}
                
                # Try to determine if there are more pages
                current_page = meta.get("current_page", page)
                last_page = meta.get("last_page") or meta.get("total_pages")
                per_page = meta.get("per_page", 10)
                total = meta.get("total")
                
                print(f"[Scraper] Metadata - current: {current_page}, last: {last_page}, per_page: {per_page}, total: {total}")
                
                # Multiple ways to check if we're done
                if last_page and current_page >= last_page:
                    print(f"[Scraper] Reached last page ({last_page})")
                    return all_strategies
                
                if total and len(all_strategies) >= total:
                    print(f"[Scraper] Collected all {total} strategies")
                    return all_strategies
                
                # If we got fewer strategies than per_page, likely the last page
                if len(strategies) < per_page and per_page > 0:
                    print(f"[Scraper] Got {len(strategies)} strategies (less than {per_page}) - likely last page")
                    # Continue to next page to confirm
                
                break  # Success - move to next page
                
            except Exception as e:
                print(f"[Scraper] Error with {url}: {e}")
                continue
        
        if not success:
            if page == 1:
                raise RuntimeError("[Scraper] Failed to fetch first page with all URL formats")
            else:
                print(f"[Scraper] Failed to fetch page {page} - assuming end of results")
                break
        
        page += 1
        time.sleep(0.5)  # Be nice to the API

    print(f"[Scraper] ✓ Total strategies collected: {len(all_strategies)}")
    return all_strategies


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
    print("[Scraper] ⚠️  No strategies returned — writing empty CSV.")
    print("[Scraper] This usually means the session login failed silently.")
    print("[Scraper] Check the auth step logs above for any warnings.")
    empty_df = pd.DataFrame(columns=[
        "Strategy ID", "Strategy Name", "Status", "Deployment Type", "Exchange",
        "Broker", "Capital Required", "PNL (Last Run)", "PNL (Overall)",
        "PNL (Live/Open)", "Run Counter", "Completed Runs", "Currency",
        "Deployment Date", "Creator", "Snapshot Time",
    ])
    empty_df.to_csv(LATEST_CSV, index=False)
    sys.exit(0)

# Remove duplicates based on Strategy ID
unique_strategies = {}
for s in raw_strategies:
    sid = s.get("id")
    if sid and sid not in unique_strategies:
        unique_strategies[sid] = s

print(f"[Scraper] Removed {len(raw_strategies) - len(unique_strategies)} duplicate strategies")
rows = [parse_strategy(s) for s in unique_strategies.values()]

df = pd.DataFrame(rows)
df["Snapshot Time"] = timestamp_str

if not df.empty and "Strategy Name" in df.columns:
    df.sort_values("Strategy Name", inplace=True, ignore_index=True)

# ── Write files based on EOD_MODE ─────────────────────────────────────────────
EOD_MODE = os.environ.get("EOD_MODE", "false").strip().lower() == "true"

# pnl_latest.csv always written — used by Telegram in both modes
df.to_csv(LATEST_CSV, index=False)

if EOD_MODE:
    # End-of-day: dated snapshot + pointer for Google Drive uploader
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

print(f"\n[Scraper] Preview (first 10 strategies):")
preview_df = df[[
    "Strategy Name", "Status", "Broker",
    "PNL (Last Run)", "PNL (Overall)", "PNL (Live/Open)", "Run Counter"
]].head(10)
print(preview_df.to_string())

print(f"\n[Scraper] ── TOTALS ({len(df)} strategies) ──────────────")
print(f"  Overall PNL  : ₹{total_overall:>12,.2f}")
print(f"  Last Run PNL : ₹{total_last:>12,.2f}")
print(f"  Live PNL     : ₹{total_live:>12,.2f}")