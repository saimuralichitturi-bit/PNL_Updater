"""
tradetron_scraper.py
────────────────────
Scrapes all Tradetron strategies + PNL using cookies from the auth step.

DEFINITIVE FIX EXPLANATION
───────────────────────────
Screenshots confirm:
  - USA exchange active  → "No Strategies deployed yet" (API returns success=false, data=[])
  - IND exchange active  → Strategies load with real PNL data

So the ENTIRE problem was always just: the exchange was set to USA, not IND.
The API returns success=false legitimately when exchange=USA and user has no USA strategies.

Root cause of all previous failures:
  1. Snapshot/restore of cookies PREVENTED the session from updating after /set/cookie/IN
     → the IN preference never actually took hold server-side
  2. Deleting domain-less cookies REMOVED the authenticated session
     → replaced with guest session from the redirect

CORRECT APPROACH (mimics exactly what the browser does):
  1. Load auth cookies normally (domain='' is fine — requests sends them to any domain)
  2. GET /set/cookie/IN — let the session jar UPDATE freely (this is the key step)
  3. After this, the server has registered IN preference for this session
  4. Then call the API — now returns Indian strategies
  5. To avoid CookieConflictError from duplicate names, use _safe_get_cookie()
     which iterates the jar manually instead of calling .get()
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

raw_cookies = session_data.get("cookies", {})
xsrf        = session_data.get("xsrf", "")

if not raw_cookies:
    raise RuntimeError("[Scraper] No cookies found in session data")

print(f"[Scraper] Cookies from auth : {list(raw_cookies.keys())}")
print(f"[Scraper] XSRF from auth    : {'YES' if xsrf else 'NO'}")

# ── SESSION — load cookies normally, let requests manage the jar ───────────────
# DO NOT use RequestsCookieJar with explicit domain — that caused guest-session
# cookies from /set/cookie/IN redirect to overwrite auth cookies.
# Just load as plain dict; requests will send them to tradetron.tech correctly.
session = requests.Session()
for name, value in raw_cookies.items():
    session.cookies.set(name, value)

print("[Scraper] Initial cookie jar:")
for c in session.cookies:
    print(f"  {c.name:<25} domain={c.domain!r:<6} value={c.value[:55]}")


# ── SAFE COOKIE READER ─────────────────────────────────────────────────────────
def _safe_get(name: str) -> str:
    """
    Read a cookie by name without raising CookieConflictError.
    When duplicates exist, prefer domain='tradetron.tech', then any domain.
    """
    fallback = ""
    for c in session.cookies:
        if c.name == name:
            if c.domain == "tradetron.tech":
                return c.value   # best match
            fallback = c.value
    return fallback


def _fresh_xsrf() -> str:
    raw = _safe_get("XSRF-TOKEN")
    return unquote(raw) if raw else unquote(xsrf)


def _api_headers() -> dict:
    return {
        "User-Agent":       UA,
        "Accept":           "application/json, text/plain, */*",
        "Origin":           BASE_URL,
        "Referer":          f"{BASE_URL}/user/dashboard",
        "X-Requested-With": "XMLHttpRequest",
        "X-XSRF-TOKEN":     _fresh_xsrf(),
        # NO Content-Type on GET — causes Laravel anonymous-session behaviour
    }


def _dump_jar(label: str) -> None:
    print(f"[Scraper] [{label}]")
    for c in session.cookies:
        print(f"  {c.name:<25} domain={c.domain!r:<22} value={c.value[:55]}")


# ── SELECT INDIA EXCHANGE ──────────────────────────────────────────────────────
def select_india_exchange() -> None:
    """
    Mimics the browser clicking IND in the exchange dropdown.

    KEY INSIGHT from screenshots:
      - The API returns data ONLY when the session has exchange=IN preference.
      - /set/cookie/IN sets this preference and rotates the session cookie.
      - We MUST let the jar update here — the new tradetron_session value is
        the one that carries the IN preference. Restoring the old session after
        this call was the core bug in all previous attempts.
    """
    url = f"{BASE_URL}/set/cookie/IN"
    print(f"\n[Scraper] ── Selecting India Exchange ──────────────────────────")
    print(f"[Scraper] GET {url}")
    print(f"[Scraper] (session jar will update — this is intentional)")

    try:
        resp = session.get(
            url,
            headers={
                "User-Agent": UA,
                "Accept":     "text/html,application/xhtml+xml,*/*;q=0.9",
                "Referer":    f"{BASE_URL}/user/dashboard",
            },
            allow_redirects=True,
            timeout=30,
        )
        print(f"[Scraper] Status: {resp.status_code} | Final URL: {resp.url}")
        print(f"[Scraper] Set-Cookie received: {'YES — session updated with IN preference' if resp.headers.get('Set-Cookie') else 'NO'}")
        _dump_jar("after /set/cookie/IN — jar now has IN preference")
        print(f"[Scraper] ✓ Exchange set to India")

    except Exception as e:
        print(f"[Scraper] ✗ Exchange selection failed: {e}")
        raise

    print(f"[Scraper] ────────────────────────────────────────────────────────\n")


# ── SESSION HEALTH CHECK ───────────────────────────────────────────────────────
def verify_session() -> None:
    """
    Verify the session is authenticated by calling a lightweight API endpoint
    and checking for success=true (not by checking URL, which is unreliable
    because Tradetron may return login page HTML at /user/dashboard with 200).
    """
    print("[Scraper] Verifying session via API probe...")
    try:
        probe_url = f"{API_BASE_URL}?page=1"
        resp = session.get(probe_url, headers=_api_headers(), timeout=30)
        print(f"[Scraper] Probe: HTTP {resp.status_code}")

        if resp.status_code in (401, 403):
            raise RuntimeError(f"[Scraper] Session DEAD — HTTP {resp.status_code}")

        ct = resp.headers.get("Content-Type", "")
        if "text/html" in ct:
            raise RuntimeError("[Scraper] Session DEAD — got HTML (login redirect)")

        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                print(f"[Scraper] ✓ Session alive and API returning data")
                return
            else:
                # success=false is the exact symptom of wrong exchange
                print(f"[Scraper] ⚠ API returned success=false — exchange may not have updated yet")
                return  # we'll find out at fetch time

    except RuntimeError:
        raise
    except Exception as e:
        print(f"[Scraper] ⚠ Session probe error: {e} — continuing")


# ── SINGLE PAGE FETCH ──────────────────────────────────────────────────────────
def _fetch_page(url: str) -> dict | None:
    hdrs = _api_headers()
    print(f"[Scraper]   XSRF: {hdrs['X-XSRF-TOKEN'][:50]}...")

    try:
        resp = session.get(url, headers=hdrs, timeout=30)
        print(f"[Scraper]   HTTP {resp.status_code}")

        if resp.status_code == 401:
            print("[Scraper]   → 401 Unauthorized")
            return None

        ct = resp.headers.get("Content-Type", "")
        if "text/html" in ct:
            print(f"[Scraper]   → HTML (login redirect): {resp.text[:150]}")
            return None

        if resp.status_code != 200:
            print(f"[Scraper]   → HTTP {resp.status_code}")
            return None

        data = resp.json()

        if not data.get("success"):
            print(f"[Scraper]   → success=false: {str(data)[:250]}")
            _dump_jar("at failure")
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

    print("[Scraper] Fetching strategies...")

    while page <= max_pages:
        url = f"{API_BASE_URL}?page={page}"
        print(f"\n[Scraper] ── Page {page} {'─'*40}")
        print(f"[Scraper] GET {url}")

        data = _fetch_page(url)

        if data is None:
            if page == 1:
                # Full diagnostic dump
                print("[Scraper] ── DIAGNOSTIC DUMP ────────────────────────────────")
                try:
                    r = session.get(url, headers=_api_headers(), timeout=30)
                    print(f"  Status    : {r.status_code}")
                    print(f"  CT        : {r.headers.get('Content-Type','')}")
                    print(f"  Set-Cookie: {r.headers.get('Set-Cookie','')[:200]}")
                    print(f"  Body      : {r.text[:500]}")
                except Exception as ex:
                    print(f"  Dump error: {ex}")
                _dump_jar("diagnostic")
                print("[Scraper] ─────────────────────────────────────────────────────")
                raise RuntimeError(
                    "[Scraper] Failed page 1.\n"
                    "If body is {success:false,data:[]} — exchange cookie not applied.\n"
                    "If body is HTML — session is dead, check tradetron_auth.py logs."
                )
            print(f"[Scraper] Page {page} failed — stopping")
            break

        strategies = data.get("data", [])
        if not isinstance(strategies, list) or not strategies:
            print(f"[Scraper] No data on page {page} — done")
            break

        print(f"[Scraper] ✓ {len(strategies)} strategies on page {page}")

        existing_ids = {s.get("id") for s in all_strategies}
        new_ids      = {s.get("id") for s in strategies}
        if new_ids & existing_ids and page > 1:
            print("[Scraper] Duplicate IDs — end of pagination")
            break

        all_strategies.extend(strategies)

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

    print(f"\n[Scraper] ✓ Total: {len(all_strategies)} strategies")
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

# 1. Hit /set/cookie/IN — let the session update freely (carries IN preference)
select_india_exchange()

# 2. Quick session health probe
verify_session()

# 3. Wait for server-side exchange preference to propagate
print("[Scraper] Waiting 5 seconds for exchange preference to propagate...")
time.sleep(5)

# 4. Fetch all strategies
raw_strategies = fetch_strategies()

# 5. Timestamps
ist      = pytz.timezone("Asia/Kolkata")
now_ist  = datetime.now(ist)
ts_str   = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
date_str = now_ist.strftime("%Y-%m-%d")

# 6. Handle empty result
if not raw_strategies:
    print("[Scraper] ⚠ No strategies — writing empty CSV")
    pd.DataFrame(columns=[
        "Strategy ID", "Strategy Name", "Status", "Deployment Type", "Exchange",
        "Broker", "Capital Required", "PNL (Last Run)", "PNL (Overall)",
        "PNL (Live/Open)", "Run Counter", "Completed Runs", "Currency",
        "Deployment Date", "Creator", "Snapshot Time",
    ]).to_csv(LATEST_CSV, index=False)
    sys.exit(0)

# 7. Deduplicate + DataFrame
unique = {s["id"]: s for s in raw_strategies if s.get("id")}
print(f"[Scraper] Removed {len(raw_strategies) - len(unique)} duplicates")

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
    print(f"  {SNAPSHOT_CSV:<38} ← EOD snapshot")
    print(f"  {LATEST_CSV:<38} ← latest copy")
    print(f"  {SNAPSHOT_PTR_FILE:<38} ← pointer for uploader")
else:
    print(f"\n[Scraper] Intraday mode: {LATEST_CSV} written")

# 9. Summary
print(f"\n[Scraper] Preview (first 10 rows):")
print(df[["Strategy Name", "Status", "Broker",
          "PNL (Last Run)", "PNL (Overall)", "PNL (Live/Open)", "Run Counter"]].head(10).to_string())

print(f"\n[Scraper] ── TOTALS ({len(df)} strategies) ──────────────────────")
print(f"  Overall PNL  : ₹{df['PNL (Overall)'].sum():>12,.2f}")
print(f"  Last Run PNL : ₹{df['PNL (Last Run)'].sum():>12,.2f}")
print(f"  Live PNL     : ₹{df['PNL (Live/Open)'].sum():>12,.2f}")
