"""
tradetron_scraper.py
────────────────────
Scrapes all Tradetron strategies + PNL using cookies from the auth step.

KEY INSIGHT: The API /api/deployed-strategies requires filter parameters.
The frontend sends these based on the "Deployed" tab + "Expiry" dropdown
visible in the UI. Without them the API returns success=false, data=[].

We probe multiple known parameter combinations and use whichever succeeds.
"""

import os
import json
import sys
import requests
import pandas as pd
from datetime import datetime
from urllib.parse import unquote
import pytz
import time

# ── CONFIG ─────────────────────────────────────────────────────────────────────
BASE_URL          = "https://tradetron.tech"
API_BASE_URL      = f"{BASE_URL}/api/deployed-strategies"
SNAPSHOT_PTR_FILE = "snapshot_path.txt"
LATEST_CSV        = "pnl_latest.csv"
UA                = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ── LOAD SESSION ───────────────────────────────────────────────────────────────
session_json_str = os.environ.get("TRADETRON_SESSION", "")
if not session_json_str:
    raise RuntimeError("[Scraper] TRADETRON_SESSION env var is missing")

print("[Scraper] Loading session...")
session_data = json.loads(session_json_str)
cookies      = session_data.get("cookies", {})
xsrf         = session_data.get("xsrf", "")

if not cookies:
    raise RuntimeError("[Scraper] No cookies found in session data")

print(f"[Scraper] Cookies loaded: {list(cookies.keys())}")

# ── SESSION SETUP ──────────────────────────────────────────────────────────────
session = requests.Session()
session.cookies.update(cookies)


# ── COOKIE HELPERS ─────────────────────────────────────────────────────────────
def _get_cookie(name):
    """Prefer domain=tradetron.tech to avoid CookieConflictError."""
    best = ""
    for c in session.cookies:
        if c.name == name:
            if c.domain == "tradetron.tech":
                return c.value
            best = c.value
    return best


def _fresh_xsrf():
    raw = _get_cookie("XSRF-TOKEN")
    return unquote(raw) if raw else unquote(xsrf)


def _clear_domainless_cookies():
    """Remove domain='' duplicates left over from the auth step."""
    to_remove = [(c.name, c.domain, c.path) for c in session.cookies if not c.domain]
    for name, domain, path in to_remove:
        session.cookies.clear(domain, path, name)
    if to_remove:
        print(f"[Scraper] Cleared {len(to_remove)} domain-less cookie(s): {[n for n,_,_ in to_remove]}")


def _dump_cookies(label):
    print(f"[Scraper] [{label}]")
    for c in session.cookies:
        print(f"  {c.name:<25} domain={c.domain!r:<22} value={str(c.value)[:50]}")


def _api_headers():
    """No Content-Type on GET — causes Laravel to ignore the session cookie."""
    return {
        "User-Agent":       UA,
        "Accept":           "application/json, text/plain, */*",
        "Origin":           BASE_URL,
        "Referer":          f"{BASE_URL}/user/dashboard",
        "X-Requested-With": "XMLHttpRequest",
        "X-XSRF-TOKEN":     _fresh_xsrf(),
    }


# ── SELECT INDIA EXCHANGE ──────────────────────────────────────────────────────
def select_india_exchange():
    print(f"\n[Scraper] ── Exchange Selection ─────────────────────────────")
    try:
        resp = session.get(
            f"{BASE_URL}/set/cookie/IN",
            headers={"User-Agent": UA, "Accept": "text/html", "Referer": f"{BASE_URL}/user/dashboard"},
            allow_redirects=True,
            timeout=30,
        )
        print(f"[Scraper] /set/cookie/IN -> {resp.status_code} | {resp.url}")
        _clear_domainless_cookies()

        session.get(
            f"{BASE_URL}/user/dashboard",
            headers={"User-Agent": UA, "Accept": "text/html"},
            allow_redirects=True,
            timeout=30,
        )
        _clear_domainless_cookies()
        print(f"[Scraper] India exchange selected")
    except Exception as e:
        print(f"[Scraper] Exchange selection error: {e}")
    print(f"[Scraper] ─────────────────────────────────────────────────────\n")


# ── PROBE: find working API params ─────────────────────────────────────────────
# The Tradetron frontend sends filter params with the deployed-strategies request.
# The screenshot shows "Expiry" in the filter dropdown on the Deployed tab.
# We try all known combinations until one returns success=true with data.
PARAM_CANDIDATES = [
    {"type": "expiry"},
    {"type": "deployed"},
    {"type": "all"},
    {"deployment_type": "expiry"},
    {"deployment_type": "deployed"},
    {"deployment_type": "all"},
    {"status": "all"},
    {"filter": "all"},
    {"tab": "deployed"},
    {},
]

ENDPOINT_CANDIDATES = [
    f"{BASE_URL}/api/deployed-strategies",
    f"{BASE_URL}/api/strategies/deployed",
    f"{BASE_URL}/api/user/deployed-strategies",
    f"{BASE_URL}/api/my-strategies",
]


def _probe_api_params():
    """
    Try each candidate endpoint + param set.
    Return the first combo that gives success=true with non-empty data.
    """
    print("[Scraper] ── Probing API parameter combinations ─────────────────")

    for endpoint in ENDPOINT_CANDIDATES:
        for params in PARAM_CANDIDATES:
            probe = {**params, "page": 1}
            try:
                resp = session.get(endpoint, params=probe, headers=_api_headers(), timeout=30)
                if resp.headers.get("Set-Cookie"):
                    _clear_domainless_cookies()
                if resp.status_code != 200:
                    print(f"[Scraper]   {resp.status_code} {endpoint} {probe}")
                    continue
                if "text/html" in resp.headers.get("Content-Type", ""):
                    print(f"[Scraper]   HTML {endpoint} {probe}")
                    continue
                data = resp.json()
                success   = data.get("success")
                data_list = data.get("data", [])
                print(f"[Scraper]   success={success} data_len={len(data_list)} | {endpoint} params={probe}")
                if success and data_list:
                    print(f"[Scraper] FOUND working combination!")
                    print(f"  Endpoint : {endpoint}")
                    print(f"  Params   : {params}")
                    return {"endpoint": endpoint, "params": params}
            except Exception as e:
                print(f"[Scraper]   error={e} | {endpoint} {probe}")

    print("[Scraper] ── No combination succeeded ──────────────────────────")
    return None


# ── SINGLE PAGE FETCH ──────────────────────────────────────────────────────────
def _fetch_page(url, params):
    try:
        resp = session.get(url, params=params, headers=_api_headers(), timeout=30)
        if resp.headers.get("Set-Cookie"):
            _clear_domainless_cookies()
        if resp.status_code == 401:
            print("[Scraper]   -> 401 Unauthorized")
            return None
        if "text/html" in resp.headers.get("Content-Type", ""):
            print("[Scraper]   -> HTML redirect")
            return None
        if resp.status_code != 200:
            print(f"[Scraper]   -> HTTP {resp.status_code}")
            return None
        data = resp.json()
        if not data.get("success"):
            print(f"[Scraper]   -> success=false: {str(data)[:200]}")
            return None
        return data
    except Exception as e:
        print(f"[Scraper]   -> Exception: {e}")
        return None


# ── PAGINATED FETCH ────────────────────────────────────────────────────────────
def fetch_strategies(endpoint, base_params):
    all_strategies = []
    page      = 1
    max_pages = 100

    print(f"[Scraper] Fetching: {endpoint} params={base_params}")

    while page <= max_pages:
        params = {**base_params, "page": page}
        print(f"[Scraper] Page {page}...")

        data = _fetch_page(endpoint, params)
        if data is None:
            if page == 1:
                raise RuntimeError("[Scraper] Failed page 1 after probe succeeded")
            break

        strategies = data.get("data", [])
        if not isinstance(strategies, list) or not strategies:
            print(f"[Scraper] No more data on page {page}")
            break

        print(f"[Scraper] Page {page}: {len(strategies)} strategies")

        existing_ids = {s.get("id") for s in all_strategies}
        if {s.get("id") for s in strategies} & existing_ids and page > 1:
            print("[Scraper] Duplicate IDs - end of pagination")
            break

        all_strategies.extend(strategies)

        meta         = data.get("meta") or data.get("pagination") or {}
        current_page = meta.get("current_page", page)
        last_page    = meta.get("last_page") or meta.get("total_pages")
        total        = meta.get("total")

        print(f"[Scraper]   Meta: current={current_page} last={last_page} total={total}")

        if last_page and current_page >= last_page:
            break
        if total and len(all_strategies) >= total:
            break

        page += 1
        time.sleep(0.5)

    print(f"[Scraper] Total collected: {len(all_strategies)}")
    return all_strategies


# ── PARSE ──────────────────────────────────────────────────────────────────────
def parse_strategy(s):
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
        "Deployment Date":  (s.get("deployment_date") or "")[:10],
        "Creator":          (template.get("user") or {}).get("name", ""),
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

# 1. Select India exchange
select_india_exchange()

# 2. Wait for session propagation
print("[Scraper] Waiting 5 seconds...")
time.sleep(5)

# 3. Probe for correct API params
working = _probe_api_params()

if working is None:
    print("\n[Scraper] ── DIAGNOSTIC DUMP ─────────────────────────────────────")
    try:
        resp = session.get(f"{API_BASE_URL}?page=1", headers=_api_headers(), timeout=30)
        print(f"  Status    : {resp.status_code}")
        print(f"  CT        : {resp.headers.get('Content-Type')}")
        print(f"  Set-Cookie: {resp.headers.get('Set-Cookie','')[:300]}")
        print(f"  Body      : {resp.text[:800]}")
        _dump_cookies("diagnostic")
    except Exception as e:
        print(f"  Error: {e}")
    print("[Scraper] ─────────────────────────────────────────────────────────")
    raise RuntimeError(
        "[Scraper] Could not find working API params.\n"
        "ACTION NEEDED: Open Chrome DevTools -> Network tab on the Tradetron "
        "Deployed page, find the /api/deployed-strategies request and share "
        "the exact URL + query params it uses."
    )

# 4. Fetch all pages
raw_strategies = fetch_strategies(working["endpoint"], working["params"])

# 5. Handle empty
ist      = pytz.timezone("Asia/Kolkata")
now_ist  = datetime.now(ist)
ts_str   = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
date_str = now_ist.strftime("%Y-%m-%d")

if not raw_strategies:
    print("[Scraper] No strategies found - writing empty CSV")
    pd.DataFrame(columns=[
        "Strategy ID", "Strategy Name", "Status", "Deployment Type", "Exchange",
        "Broker", "Capital Required", "PNL (Last Run)", "PNL (Overall)",
        "PNL (Live/Open)", "Run Counter", "Completed Runs", "Currency",
        "Deployment Date", "Creator", "Snapshot Time",
    ]).to_csv(LATEST_CSV, index=False)
    sys.exit(0)

# 6. Deduplicate + build DataFrame
unique = {s["id"]: s for s in raw_strategies if s.get("id")}
print(f"[Scraper] Removed {len(raw_strategies) - len(unique)} duplicates")

df = pd.DataFrame([parse_strategy(s) for s in unique.values()])
df["Snapshot Time"] = ts_str
df.sort_values("Strategy Name", inplace=True, ignore_index=True)

# 7. Write CSVs
EOD_MODE = os.environ.get("EOD_MODE", "false").strip().lower() == "true"
df.to_csv(LATEST_CSV, index=False)

if EOD_MODE:
    SNAPSHOT_CSV = f"pnl_{date_str}.csv"
    df.to_csv(SNAPSHOT_CSV, index=False)
    with open(SNAPSHOT_PTR_FILE, "w") as f:
        f.write(SNAPSHOT_CSV)
    print(f"\n[Scraper] EOD: {SNAPSHOT_CSV} + {LATEST_CSV} saved")
else:
    print(f"\n[Scraper] Intraday: {LATEST_CSV} saved")

# 8. Summary
print(f"\n[Scraper] Preview (first 10):")
print(df[["Strategy Name", "Status", "Broker",
          "PNL (Last Run)", "PNL (Overall)", "PNL (Live/Open)", "Run Counter"]].head(10).to_string())

print(f"\n[Scraper] TOTALS ({len(df)} strategies)")
print(f"  Overall PNL  : Rs.{df['PNL (Overall)'].sum():>12,.2f}")
print(f"  Last Run PNL : Rs.{df['PNL (Last Run)'].sum():>12,.2f}")
print(f"  Live PNL     : Rs.{df['PNL (Live/Open)'].sum():>12,.2f}")
