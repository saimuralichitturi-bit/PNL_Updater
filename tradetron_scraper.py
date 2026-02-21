"""
tradetron_scraper.py
────────────────────
Scrapes all Tradetron strategies + PNL using the saved session.

SESSION SOURCE (in order of priority):
  1. TRADETRON_SESSION env var  ← injected by GitHub Actions from auth step output
  2. tradetron_session.json     ← fallback for local testing

OUTPUT FILES:
  pnl_YYYY-MM-DD_HH-MM.csv    ←  timestamped snapshot
  pnl_latest.csv               ←  always overwritten
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

# ── API endpoint ───────────────────────────────────────────────────────────────
API_URL = "https://tradetron.tech/api/deployed-strategies"


# ── Fetch strategies ───────────────────────────────────────────────────────────
def fetch_strategies():
    print(f"[Scraper] Fetching → {API_URL}")
    resp = session.get(API_URL, headers=BASE_HEADERS, timeout=30)
    print(f"[Scraper] Status: {resp.status_code}")

    if resp.status_code != 200:
        raise RuntimeError(f"[Scraper] API returned {resp.status_code}: {resp.text[:200]}")

    data = resp.json()

    # Response shape: {"success": true, "data": [...], "paginate": {...}}
    strategies = data.get("data")
    if not isinstance(strategies, list):
        raise RuntimeError(f"[Scraper] Unexpected response shape. Keys: {list(data.keys())}")

    print(f"[Scraper] ✓ Got {len(strategies)} strategies")
    return strategies


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
        "PNL (Last Run)":   round(last_pnl, 2),
        "PNL (Overall)":    round(all_pnl, 2),
        "PNL (Live/Open)":  round(live_pnl, 2),
        "Run Counter":      current_run,
        "Completed Runs":   max_run,
        "Currency":         s.get("currency_code", "INR"),
        "Deployment Date":  s.get("deployment_date", "")[:10] if s.get("deployment_date") else "",
        "Creator":          (template.get("user") or {}).get("name", ""),
    }


# ── Fetch & build DataFrame ────────────────────────────────────────────────────
raw_strategies = fetch_strategies()
rows = [parse_strategy(s) for s in raw_strategies]

ist           = pytz.timezone("Asia/Kolkata")
now_ist       = datetime.now(ist)
timestamp_str = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
file_ts       = now_ist.strftime("%Y-%m-%d_%H-%M")
SNAPSHOT_CSV  = f"pnl_{file_ts}.csv"

df = pd.DataFrame(rows)
df["Snapshot Time"] = timestamp_str
df.sort_values("Strategy Name", inplace=True, ignore_index=True)

# ── Write output files ─────────────────────────────────────────────────────────
df.to_csv(SNAPSHOT_CSV, index=False)
df.to_csv(LATEST_CSV,   index=False)

with open(SNAPSHOT_PTR_FILE, "w") as f:
    f.write(SNAPSHOT_CSV)

# ── Summary ────────────────────────────────────────────────────────────────────
total_overall = df["PNL (Overall)"].sum()
total_last    = df["PNL (Last Run)"].sum()
total_live    = df["PNL (Live/Open)"].sum()

print(f"\n[Scraper] Files written:")
print(f"  {SNAPSHOT_CSV:<38} ← timestamped snapshot")
print(f"  {LATEST_CSV:<38} ← latest copy")
print(f"  {SNAPSHOT_PTR_FILE:<38} ← pointer for uploader")

print(f"\n[Scraper] Preview:")
print(df[[
    "Strategy Name", "Status", "Broker",
    "PNL (Last Run)", "PNL (Overall)", "PNL (Live/Open)", "Run Counter"
]].to_string())

print(f"\n[Scraper] ── TOTALS ──────────────────────────────────────")
print(f"  Overall PNL  (all strategies) : ₹{total_overall:>12,.2f}")
print(f"  Last Run PNL (all strategies) : ₹{total_last:>12,.2f}")
print(f"  Live PNL     (all strategies) : ₹{total_live:>12,.2f}")