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

ROOT CAUSE FIX (success=false):
  The API was receiving a brand-new anonymous session because:
  1. Content-Type: application/json on a GET request caused Laravel to
     treat the request as a fresh/anonymous context and ignore the session cookie.
  2. The XSRF token must be URL-decoded before use as a header value.
  Both are fixed here.
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

cookies = session_data.get("cookies", {})
xsrf    = session_data.get("xsrf", "")

if not cookies:
    raise RuntimeError("[Scraper] No cookies found in session data")

print(f"[Scraper] Cookies loaded  : {list(cookies.keys())}")
print(f"[Scraper] XSRF present    : {'YES — ' + xsrf[:40] + '...' if xsrf else 'NO'}")

# ── SESSION SETUP ──────────────────────────────────────────────────────────────
session = requests.Session()
session.cookies.update(cookies)

# ── HELPERS ────────────────────────────────────────────────────────────────────
def _fresh_xsrf() -> str:
    """URL-decode the XSRF-TOKEN cookie from the live jar (always current)."""
    raw = session.cookies.get("XSRF-TOKEN", "")
    return unquote(raw) if raw else unquote(xsrf)


def _api_headers() -> dict:
    """
    Headers for JSON API calls.
    KEY: do NOT include Content-Type for GET requests — it causes Laravel to
    treat the request as a fresh/anonymous context and ignore the session cookie.
    """
    return {
        "User-Agent":       UA,
        "Accept":           "application/json, text/plain, */*",
        "Origin":           BASE_URL,
        "Referer":          f"{BASE_URL}/user/dashboard",
        "X-Requested-With": "XMLHttpRequest",
        "X-XSRF-TOKEN":     _fresh_xsrf(),
        # NO Content-Type — critical for GET requests on Laravel
    }


def _dump_cookies(label: str) -> None:
    print(f"[Scraper] [{label}] Cookie jar:")
    for c in session.cookies:
        print(f"  {c.name:<25} domain={c.domain}  path={c.path}  value={str(c.value)[:60]}")


# ── SELECT INDIA EXCHANGE ──────────────────────────────────────────────────────
def select_india_exchange() -> None:
    """
    Hits /set/cookie/IN — exactly what the browser does when the user clicks
    the IND flag in the dropdown. Sets the exchange preference server-side.
    After the redirect we re-fetch the dashboard so the session cookie is
    re-issued with the exchange preference baked in.
    """
    url = f"{BASE_URL}/set/cookie/IN"
    print(f"\n[Scraper] ── Exchange Selection ───────────────────────────────")
    print(f"[Scraper] GET {url}")

    try:
        resp = session.get(
            url,
            headers={
                "User-Agent": UA,
                "Accept":     "text/html,application/xhtml+xml,*/*;q=0.8",
                "Referer":    f"{BASE_URL}/user/dashboard",
            },
            allow_redirects=True,
            timeout=30,
        )
        print(f"[Scraper] Status: {resp.status_code} | Final URL: {resp.url}")
        print(f"[Scraper] Set-Cookie in response: {'YES' if resp.headers.get('Set-Cookie') else 'NO'}")

        _dump_cookies("after /set/cookie/IN")

        # Re-fetch dashboard so Laravel re-issues a stable session with IN preference
        print(f"[Scraper] Re-fetching dashboard to stabilise session...")
        session.get(
            f"{BASE_URL}/user/dashboard",
            headers={
                "User-Agent": UA,
                "Accept":     "text/html,application/xhtml+xml,*/*;q=0.8",
                "Referer":    f"{BASE_URL}/set/cookie/IN",
            },
            allow_redirects=True,
            timeout=30,
        )
        _dump_cookies("after dashboard re-fetch")

        print(f"[Scraper] country_code cookie = '{session.cookies.get('country_code', 'NOT SET')}'")
        print(f"[Scraper] ✓ India (IND) exchange selected")

    except Exception as e:
        print(f"[Scraper] ⚠ Exchange selection failed: {e} — continuing anyway")

    print(f"[Scraper] ──────────────────────────────────────────────────────\n")


# ── SESSION HEALTH CHECK ───────────────────────────────────────────────────────
def verify_session() -> None:
    """Quick sanity check — make sure we're NOT on the login page."""
    print("[Scraper] Verifying session health...")
    try:
        resp = session.get(
            f"{BASE_URL}/user/dashboard",
            headers={"User-Agent": UA, "Accept": "text/html"},
            allow_redirects=True,
            timeout=30,
        )
        if "/login" in resp.url:
            raise RuntimeError(f"[Scraper] Session DEAD — redirected to {resp.url}")
        print(f"[Scraper] ✓ Session alive (final URL: {resp.url})")
    except RuntimeError:
        raise
    except Exception as e:
        print(f"[Scraper] ⚠ Session check failed: {e} — continuing")


# ── SINGLE PAGE FETCH ──────────────────────────────────────────────────────────
def _fetch_page(url: str) -> dict | None:
    hdrs = _api_headers()
    print(f"[Scraper]   X-XSRF-TOKEN (sent): {hdrs['X-XSRF-TOKEN'][:40]}...")

    try:
        resp = session.get(url, headers=hdrs, timeout=30)
        print(f"[Scraper]   HTTP {resp.status_code}")
        print(f"[Scraper]   Set-Cookie in API response: {'YES — token rotated' if resp.headers.get('Set-Cookie') else 'no'}")

        if resp.status_code == 401:
            print("[Scraper]   → 401 Unauthorized")
            return None

        ct = resp.headers.get("Content-Type", "")
        if "text/html" in ct:
            print(f"[Scraper]   → HTML response (login redirect?): {resp.text[:200]}")
            return None

        if resp.status_code != 200:
            print(f"[Scraper]   → HTTP {resp.status_code}")
            return None

        data = resp.json()

        if not data.get("success"):
            print(f"[Scraper]   → success=false — raw: {str(data)[:300]}")
            _dump_cookies("at failure")
            return None

        return data

    except Exception as e:
        print(f"[Scraper]   → Exception: {e}")
        return None


# ── PAGINATED STRATEGY FETCH ───────────────────────────────────────────────────
def fetch_strategies() -> list:
    all_strategies = []
    page      = 1
    max_pages = 100

    print("[Scraper] Starting strategy fetch...")

    while page <= max_pages:
        url = f"{API_BASE_URL}?page={page}"
        print(f"\n[Scraper] ── Page {page} ──────────────────────────────────────")
        print(f"[Scraper] GET {url}")

        data = _fetch_page(url)

        if data is None:
            if page == 1:
                print("[Scraper] ── Full raw dump (page 1) ───────────────────────────")
                try:
                    resp = session.get(url, headers=_api_headers(), timeout=30)
                    print(f"  Status   : {resp.status_code}")
                    print(f"  CT       : {resp.headers.get('Content-Type', '')}")
                    print(f"  Set-Cookie: {resp.headers.get('Set-Cookie', '')[:200]}")
                    print(f"  Body     : {resp.text[:600]}")
                except Exception as ex:
                    print(f"  Dump failed: {ex}")
                print("[Scraper] ───────────────────────────────────────────────────────")
                raise RuntimeError(
                    "[Scraper] Failed to fetch page 1 — see dump above for diagnosis"
                )
            print(f"[Scraper] Failed on page {page} — stopping pagination")
            break

        strategies = data.get("data", [])
        if not isinstance(strategies, list) or not strategies:
            print(f"[Scraper] No more strategies on page {page}")
            break

        print(f"[Scraper] ✓ Page {page}: {len(strategies)} strategies")

        # Duplicate guard
        existing_ids = {s.get("id") for s in all_strategies}
        new_ids      = {s.get("id") for s in strategies}
        if new_ids & existing_ids and page > 1:
            print("[Scraper] Duplicate IDs — reached end of pagination")
            break

        all_strategies.extend(strategies)

        # Pagination metadata
        meta         = data.get("meta") or data.get("pagination") or {}
        current_page = meta.get("current_page", page)
        last_page    = meta.get("last_page") or meta.get("total_pages")
        total        = meta.get("total")

        print(f"[Scraper] Meta → current={current_page} last={last_page} total={total}")

        if last_page and current_page >= last_page:
            break
        if total and len(all_strategies) >= total:
            break

        page += 1
        time.sleep(0.5)

    print(f"\n[Scraper] ✓ Total collected: {len(all_strategies)} strategies")
    return all_strategies


# ── PARSE ──────────────────────────────────────────────────────────────────────
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
        "Deployment Date":  (s.get("deployment_date") or "")[:10],
        "Creator":          (template.get("user") or {}).get("name", ""),
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

# 1. Select India exchange
select_india_exchange()

# 2. Verify the session is alive post-exchange-selection
verify_session()

# 3. Wait for server-side preference to propagate
print("[Scraper] Waiting 5 seconds for exchange preference to propagate...")
time.sleep(5)

# 4. Fetch strategies
raw_strategies = fetch_strategies()

# 5. Handle empty result
ist      = pytz.timezone("Asia/Kolkata")
now_ist  = datetime.now(ist)
ts_str   = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
date_str = now_ist.strftime("%Y-%m-%d")

if not raw_strategies:
    print("[Scraper] ⚠ No strategies returned — writing empty CSV")
    pd.DataFrame(columns=[
        "Strategy ID", "Strategy Name", "Status", "Deployment Type", "Exchange",
        "Broker", "Capital Required", "PNL (Last Run)", "PNL (Overall)",
        "PNL (Live/Open)", "Run Counter", "Completed Runs", "Currency",
        "Deployment Date", "Creator", "Snapshot Time",
    ]).to_csv(LATEST_CSV, index=False)
    sys.exit(0)

# 6. Deduplicate
unique = {s["id"]: s for s in raw_strategies if s.get("id")}
print(f"[Scraper] Removed {len(raw_strategies) - len(unique)} duplicates")

# 7. Build DataFrame
df = pd.DataFrame([parse_strategy(s) for s in unique.values()])
df["Snapshot Time"] = ts_str
df.sort_values("Strategy Name", inplace=True, ignore_index=True)

# 8. Write CSVs
EOD_MODE = os.environ.get("EOD_MODE", "false").strip().lower() == "true"

df.to_csv(LATEST_CSV, index=False)

if EOD_MODE:
    SNAPSHOT_CSV = f"pnl_{date_str}.csv"
    df.to_csv(SNAPSHOT_CSV, index=False)
    with open(SNAPSHOT_PTR_FILE, "w") as f:
        f.write(SNAPSHOT_CSV)
    print(f"\n[Scraper] EOD mode:")
    print(f"  {SNAPSHOT_CSV:<38} <- EOD snapshot")
    print(f"  {LATEST_CSV:<38} <- latest copy")
    print(f"  {SNAPSHOT_PTR_FILE:<38} <- pointer for uploader")
else:
    print(f"\n[Scraper] Intraday mode: {LATEST_CSV} saved")

# 9. Summary
print(f"\n[Scraper] Preview (first 10):")
print(df[["Strategy Name", "Status", "Broker",
          "PNL (Last Run)", "PNL (Overall)", "PNL (Live/Open)", "Run Counter"]].head(10).to_string())

print(f"\n[Scraper] ── TOTALS ({len(df)} strategies) ─────────────────────")
print(f"  Overall PNL  : ₹{df['PNL (Overall)'].sum():>12,.2f}")
print(f"  Last Run PNL : ₹{df['PNL (Last Run)'].sum():>12,.2f}")
print(f"  Live PNL     : ₹{df['PNL (Live/Open)'].sum():>12,.2f}")
