"""
tradetron_scraper.py
────────────────────
Uses the saved session to scrape all Tradetron strategies + PNL
and writes the results to CSV files.

INPUT FILE:
  tradetron_session.json       ←  written by tradetron_auth.py

OUTPUT FILES:
  pnl_YYYY-MM-DD_HH-MM.csv    ←  timestamped snapshot (permanent history)
  pnl_latest.csv               ←  always overwritten with latest data
  snapshot_path.txt            ←  passes snapshot filename to google_drive_uploader.py
"""

import json
import requests
import pandas as pd
from datetime import datetime
import pytz

# ── FILE NAMES ─────────────────────────────────────────────────────────────────
SESSION_FILE      = "tradetron_session.json"   # ← input  (from tradetron_auth.py)
SNAPSHOT_PTR_FILE = "snapshot_path.txt"        # ← output (read by google_drive_uploader.py)
LATEST_CSV        = "pnl_latest.csv"           # ← output (always overwritten on Drive)
# Snapshot CSV is generated at runtime → pnl_YYYY-MM-DD_HH-MM.csv

# ── TRADETRON API ENDPOINTS ────────────────────────────────────────────────────
DEPLOYED_URL   = "https://tradetron.tech/api/deployed-strategies"
STRATEGIES_URL = "https://tradetron.tech/api/strategy/my-strategies"

# ── Load session ───────────────────────────────────────────────────────────────
with open(SESSION_FILE) as f:
    session_data = json.load(f)

cookies = session_data.get("cookies", {})
token   = session_data.get("token")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Origin":     "https://tradetron.tech",
    "Referer":    "https://tradetron.tech/deployed",
}
if token:
    HEADERS["Authorization"] = f"Bearer {token}"

session = requests.Session()
session.cookies.update(cookies)

# ── Helpers ────────────────────────────────────────────────────────────────────
def fetch_strategies():
    rows = []

    # Attempt 1: deployed strategies endpoint (has live PNL)
    resp = session.get(DEPLOYED_URL, headers=HEADERS, timeout=30)
    print(f"[Scraper] Deployed strategies status: {resp.status_code}")

    if resp.status_code == 200:
        try:
            data       = resp.json()
            strategies = data if isinstance(data, list) else data.get("data", data.get("strategies", []))
            for s in strategies:
                rows.append(parse_strategy(s))
            print(f"[Scraper] Fetched {len(rows)} deployed strategies.")
            return rows
        except Exception as e:
            print(f"[Scraper] Parse error on deployed endpoint: {e}")

    # Attempt 2: my-strategies fallback
    resp2 = session.get(STRATEGIES_URL, headers=HEADERS, timeout=30)
    print(f"[Scraper] My-strategies status: {resp2.status_code}")
    if resp2.status_code == 200:
        try:
            data       = resp2.json()
            strategies = data if isinstance(data, list) else data.get("data", data.get("strategies", []))
            for s in strategies:
                rows.append(parse_strategy(s))
            print(f"[Scraper] Fetched {len(rows)} strategies from my-strategies.")
            return rows
        except Exception as e:
            print(f"[Scraper] Parse error on my-strategies endpoint: {e}")

    raise RuntimeError("[Scraper] Could not fetch strategies. Verify session & credentials.")


def parse_strategy(s: dict) -> dict:
    """Flatten a raw strategy dict into a clean CSV row."""
    return {
        "Strategy ID":   s.get("id")         or s.get("strategy_id") or s.get("_id", ""),
        "Strategy Name": s.get("name")        or s.get("strategy_name") or s.get("title", ""),
        "Status":        s.get("status")      or s.get("state", ""),
        "PNL (Today)":   s.get("todayPnl")    or s.get("today_pnl")   or s.get("pnl", 0),
        "PNL (Overall)": s.get("overallPnl")  or s.get("overall_pnl") or s.get("totalPnl", 0),
        "Capital":       s.get("capital")     or s.get("investment", 0),
        "Broker":        s.get("broker")      or s.get("broker_name", ""),
        "Instrument":    s.get("instrument")  or s.get("underlying", ""),
        "Last Updated":  s.get("updatedAt")   or s.get("updated_at", ""),
    }


# ── Timestamp & filenames ──────────────────────────────────────────────────────
ist           = pytz.timezone("Asia/Kolkata")
now_ist       = datetime.now(ist)
timestamp_str = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")           # inside CSV
file_ts       = now_ist.strftime("%Y-%m-%d_%H-%M")                  # in filename
SNAPSHOT_CSV  = f"pnl_{file_ts}.csv"                                # e.g. pnl_2024-05-01_15-16.csv

# ── Fetch, build DataFrame, save ───────────────────────────────────────────────
rows = fetch_strategies()
df   = pd.DataFrame(rows)
df["Snapshot Time"] = timestamp_str
df.sort_values("Strategy Name", inplace=True, ignore_index=True)

df.to_csv(SNAPSHOT_CSV, index=False)
df.to_csv(LATEST_CSV,   index=False)

with open(SNAPSHOT_PTR_FILE, "w") as f:
    f.write(SNAPSHOT_CSV)

print(f"\n[Scraper] Files written:")
print(f"  {SNAPSHOT_CSV:<35} ← timestamped snapshot")
print(f"  {LATEST_CSV:<35} ← latest copy (overwritten each run)")
print(f"  {SNAPSHOT_PTR_FILE:<35} ← filename pointer for uploader")
print(f"\n[Scraper] Preview:")
print(df[["Strategy Name", "PNL (Today)", "PNL (Overall)", "Status"]].to_string())