"""
tradetron_scraper.py
────────────────────
Robust Tradetron scraper with:
- Exchange selection (IND)
- XSRF auto-refresh
- Retry logic
- Session validation
"""

import os
import json
import sys
import requests
import pandas as pd
from datetime import datetime
import pytz
import time
from urllib.parse import unquote

# ── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL = "https://tradetron.tech"
API_BASE_URL = f"{BASE_URL}/api/deployed-strategies"

SNAPSHOT_PTR_FILE = "snapshot_path.txt"
LATEST_CSV = "pnl_latest.csv"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

# ── LOAD SESSION ──────────────────────────────────────────────────────────────
session_json_str = os.environ.get("TRADETRON_SESSION", "")

if not session_json_str:
    raise RuntimeError("[Scraper] TRADETRON_SESSION env var is missing")

print("[Scraper] Loading session...")

session_data = json.loads(session_json_str)

cookies = session_data.get("cookies", {})
xsrf = session_data.get("xsrf", "")

if not cookies:
    raise RuntimeError("[Scraper] No cookies found")

print(f"[Scraper] Cookies loaded: {list(cookies.keys())}")
print(f"[Scraper] XSRF present: {'YES' if xsrf else 'NO'}")

# ── SESSION SETUP ─────────────────────────────────────────────────────────────
session = requests.Session()
session.cookies.update(cookies)

API_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/user/dashboard",
    "X-Requested-With": "XMLHttpRequest",
    "X-XSRF-TOKEN": xsrf,
}

# ── REFRESH XSRF TOKEN ────────────────────────────────────────────────────────
def refresh_xsrf_from_cookies():
    global xsrf, API_HEADERS

    xsrf_value = None

    # Iterate over all cookies and pick the correct XSRF-TOKEN
    for cookie in session.cookies:
        if cookie.name == "XSRF-TOKEN":
            xsrf_value = cookie.value
            # Prefer root path cookie
            if cookie.path == "/":
                break

    if xsrf_value:
        xsrf = unquote(xsrf_value)
        API_HEADERS["X-XSRF-TOKEN"] = xsrf
        print("[Scraper] ✓ XSRF token refreshed (resolved conflict)")
    else:
        print("[Scraper] ⚠ XSRF token not found")

# ── SELECT INDIA EXCHANGE ─────────────────────────────────────────────────────
def select_india_exchange():
    url = f"{BASE_URL}/set/cookie/IN"

    print(f"[Scraper] Selecting India exchange...")

    try:
        resp = session.get(
            url,
            headers={
                "User-Agent": UA,
                "Referer": f"{BASE_URL}/user/dashboard",
            },
            allow_redirects=True,
            timeout=30,
        )

        print(f"[Scraper] Exchange status: {resp.status_code}")

        # Refresh cookies → VERY IMPORTANT
        refresh_xsrf_from_cookies()

        if resp.status_code in (200, 302):
            print("[Scraper] ✓ India exchange set")
        else:
            print("[Scraper] ⚠ Exchange selection issue")

    except Exception as e:
        print(f"[Scraper] ⚠ Exchange error: {e}")

# ── FETCH PAGE ────────────────────────────────────────────────────────────────
def _fetch_page(url):
    try:
        resp = session.get(url, headers=API_HEADERS, timeout=30)

        print(f"[Scraper]   HTTP {resp.status_code}")

        if resp.status_code == 401:
            print("[Scraper]   → Unauthorized (session expired)")
            return None

        if "text/html" in resp.headers.get("Content-Type", ""):
            print("[Scraper]   → Got HTML (login redirect)")
            return None

        data = resp.json()

        if not data.get("success"):
            print(f"[Scraper]   → success=false")
            return None

        return data

    except Exception as e:
        print(f"[Scraper]   → Exception: {e}")
        return None

# ── RETRY WRAPPER ─────────────────────────────────────────────────────────────
def fetch_with_retry(url, retries=3, delay=3):
    for i in range(retries):
        data = _fetch_page(url)
        if data:
            return data

        print(f"[Scraper] Retry {i+1}/{retries}...")
        time.sleep(delay)

        # refresh xsrf every retry
        refresh_xsrf_from_cookies()

    return None

# ── FETCH STRATEGIES ──────────────────────────────────────────────────────────
def fetch_strategies():
    all_strategies = []
    page = 1

    print("[Scraper] Fetching strategies...")

    while True:
        url = f"{API_BASE_URL}?page={page}"
        print(f"[Scraper] Page {page}")

        data = fetch_with_retry(url)

        if not data:
            if page == 1:
                raise RuntimeError("[Scraper] Failed page 1 → invalid session")
            break

        strategies = data.get("data", [])

        if not strategies:
            break

        print(f"[Scraper] ✓ {len(strategies)} strategies")

        all_strategies.extend(strategies)

        page += 1
        time.sleep(1)

    print(f"[Scraper] Total: {len(all_strategies)}")
    return all_strategies

# ── PARSE ─────────────────────────────────────────────────────────────────────
def parse_strategy(s):
    template = s.get("template") or {}
    strategy_broker = s.get("strategy_broker") or {}
    broker = strategy_broker.get("broker") or {}

    return {
        "Strategy ID": s.get("id", ""),
        "Strategy Name": template.get("name", ""),
        "Status": s.get("status", ""),
        "Deployment Type": s.get("deployment_type", ""),
        "Exchange": s.get("exchange", ""),
        "Broker": broker.get("name", ""),
        "Capital Required": template.get("capital_required", 0),
        "PNL (Last Run)": round(float(s.get("last_pnl") or 0), 2),
        "PNL (Overall)": round(float(s.get("all_pnl") or 0), 2),
        "PNL (Live/Open)": round(float(s.get("globalPt") or 0), 2),
        "Run Counter": s.get("run_counter") or 0,
        "Completed Runs": s.get("max_run_counter") or 0,
        "Currency": s.get("currency_code", "INR"),
        "Deployment Date": (s.get("deployment_date") or "")[:10],
        "Creator": (template.get("user") or {}).get("name", ""),
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────
select_india_exchange()

print("[Scraper] Waiting 5 seconds...")
time.sleep(5)

raw = fetch_strategies()

if not raw:
    print("[Scraper] No data")
    sys.exit(1)

# Deduplicate
unique = {s["id"]: s for s in raw if s.get("id")}

df = pd.DataFrame([parse_strategy(s) for s in unique.values()])

# Timestamp
ist = pytz.timezone("Asia/Kolkata")
now = datetime.now(ist)

df["Snapshot Time"] = now.strftime("%Y-%m-%d %H:%M:%S IST")

df.sort_values("Strategy Name", inplace=True)

# Save
EOD_MODE = os.environ.get("EOD_MODE", "false").lower() == "true"

LATEST_CSV = "pnl_latest.csv"
df.to_csv(LATEST_CSV, index=False)

if EOD_MODE:
    SNAPSHOT = f"pnl_{now.strftime('%Y-%m-%d')}.csv"
    df.to_csv(SNAPSHOT, index=False)

    with open("snapshot_path.txt", "w") as f:
        f.write(SNAPSHOT)

    print(f"[Scraper] EOD saved: {SNAPSHOT}")
else:
    print("[Scraper] Intraday saved")

# Summary
print("\n[Scraper] TOTALS")
print(f"Overall PNL : ₹{df['PNL (Overall)'].sum():,.2f}")
print(f"Last Run PNL: ₹{df['PNL (Last Run)'].sum():,.2f}")
print(f"Live PNL    : ₹{df['PNL (Live/Open)'].sum():,.2f}")
