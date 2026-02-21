"""
tradetron_scraper.py
────────────────────
Scrapes all Tradetron strategies + PNL using the saved session.

INPUT FILE:
  tradetron_session.json       ←  written by tradetron_auth.py

OUTPUT FILES:
  pnl_YYYY-MM-DD_HH-MM.csv    ←  timestamped snapshot
  pnl_latest.csv               ←  always overwritten
  snapshot_path.txt            ←  filename pointer for google_drive_uploader.py
"""

import json
import requests
import pandas as pd
from datetime import datetime
import pytz

# ── FILE NAMES ─────────────────────────────────────────────────────────────────
SESSION_FILE      = "tradetron_session.json"
SNAPSHOT_PTR_FILE = "snapshot_path.txt"
LATEST_CSV        = "pnl_latest.csv"

# ── Load session ───────────────────────────────────────────────────────────────
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept":     "application/json, text/plain, */*",
    "Origin":     "https://tradetron.tech",
    "Referer":    "https://tradetron.tech/dashboard",
    "X-Requested-With": "XMLHttpRequest",
}
if token:
    BASE_HEADERS["Authorization"] = f"Bearer {token}"
if xsrf:
    BASE_HEADERS["X-XSRF-TOKEN"] = xsrf

# ── Strategy endpoints to try (in order) ──────────────────────────────────────
ENDPOINTS = [
    "https://tradetron.tech/api/strategy/deployed",
    "https://tradetron.tech/api/deployed-strategies",
    "https://tradetron.tech/api/strategy/my-strategies",
    "https://tradetron.tech/api/strategies",
    "https://tradetron.tech/api/user/strategies",
    "https://tradetron.tech/api/strategy/list",
]

def try_fetch(url: str):
    """GET a URL and return parsed list of strategies, or None on failure."""
    try:
        resp = session.get(url, headers=BASE_HEADERS, timeout=30)
        print(f"[Scraper] {url} → {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            # Handle various response shapes
            if isinstance(data, list):
                return data
            for key in ("data", "strategies", "result", "results", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
            # If it's a dict with strategy-like keys, wrap it
            if isinstance(data, dict) and ("id" in data or "name" in data):
                return [data]
        else:
            print(f"[Scraper]   Response snippet: {resp.text[:150]}")
    except Exception as e:
        print(f"[Scraper]   Error: {e}")
    return None

def fetch_strategies():
    for url in ENDPOINTS:
        result = try_fetch(url)
        if result is not None and len(result) > 0:
            print(f"[Scraper] ✓ Got {len(result)} strategies from {url}")
            return result
        elif result is not None:
            print(f"[Scraper]   Empty list returned from {url}, trying next...")

    raise RuntimeError(
        "[Scraper] All endpoints returned 401/404/empty.\n"
        "Check tradetron_auth.py logs — the session token/cookies may not be valid.\n"
        "Tradetron may require a different login flow."
    )

def parse_strategy(s: dict) -> dict:
    """Flatten a raw strategy dict into a clean CSV row."""
    # Handle nested pnl objects
    pnl_obj   = s.get("pnl") or {}
    today_pnl = (
        s.get("todayPnl") or s.get("today_pnl")
        or (pnl_obj.get("today") if isinstance(pnl_obj, dict) else None)
        or s.get("pnl") if not isinstance(s.get("pnl"), dict) else 0
    ) or 0
    overall_pnl = (
        s.get("overallPnl") or s.get("overall_pnl") or s.get("totalPnl")
        or (pnl_obj.get("overall") or pnl_obj.get("total") if isinstance(pnl_obj, dict) else None)
    ) or 0

    return {
        "Strategy ID":   s.get("id")        or s.get("strategy_id") or s.get("_id", ""),
        "Strategy Name": s.get("name")       or s.get("strategy_name") or s.get("title", ""),
        "Status":        s.get("status")     or s.get("state") or s.get("deployStatus", ""),
        "PNL (Today)":   today_pnl,
        "PNL (Overall)": overall_pnl,
        "Capital":       s.get("capital")    or s.get("investment") or s.get("allocatedCapital", 0),
        "Broker":        s.get("broker")     or s.get("broker_name") or s.get("brokerName", ""),
        "Instrument":    s.get("instrument") or s.get("underlying")  or s.get("symbol", ""),
        "Last Updated":  s.get("updatedAt")  or s.get("updated_at")  or s.get("lastUpdated", ""),
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

df.to_csv(SNAPSHOT_CSV, index=False)
df.to_csv(LATEST_CSV,   index=False)

with open(SNAPSHOT_PTR_FILE, "w") as f:
    f.write(SNAPSHOT_CSV)

print(f"\n[Scraper] Files written:")
print(f"  {SNAPSHOT_CSV:<38} ← timestamped snapshot")
print(f"  {LATEST_CSV:<38} ← latest copy")
print(f"  {SNAPSHOT_PTR_FILE:<38} ← pointer for uploader")
print(f"\n[Scraper] Preview:")
print(df[["Strategy Name", "PNL (Today)", "PNL (Overall)", "Status"]].to_string())