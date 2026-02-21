"""
tradetron_auth.py
─────────────────
Logs into Tradetron and saves session cookies + CSRF token + auth headers.

OUTPUT FILE:
  tradetron_session.json  →  read by tradetron_scraper.py
"""

import os
import json
import requests
from bs4 import BeautifulSoup

# ── FILE NAMES ─────────────────────────────────────────────────────────────────
SESSION_FILE = "tradetron_session.json"

# ── CREDENTIALS ────────────────────────────────────────────────────────────────
TRADETRON_EMAIL    = os.environ["TRADETRON_EMAIL"]
TRADETRON_PASSWORD = os.environ["TRADETRON_PASSWORD"]

BASE_URL  = "https://tradetron.tech"
LOGIN_URL = "https://tradetron.tech/api/auth/login"

HEADERS = {
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept":       "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin":       "https://tradetron.tech",
    "Referer":      "https://tradetron.tech/login",
    "X-Requested-With": "XMLHttpRequest",
}

def login():
    session = requests.Session()

    # Step A: Hit the homepage first to get any initial cookies/CSRF
    print("[Auth] Fetching homepage for initial cookies...")
    home = session.get(BASE_URL, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
    print(f"[Auth] Homepage status: {home.status_code}")
    print(f"[Auth] Initial cookies: {dict(session.cookies)}")

    # Try to extract XSRF / CSRF token from cookies or page
    xsrf_token = session.cookies.get("XSRF-TOKEN") or session.cookies.get("xsrf_token") or ""
    if xsrf_token:
        HEADERS["X-XSRF-TOKEN"] = xsrf_token
        print(f"[Auth] XSRF token found: {xsrf_token[:20]}...")

    # Step B: Try the /api/auth/login endpoint
    payload = {"email": TRADETRON_EMAIL, "password": TRADETRON_PASSWORD}
    print(f"\n[Auth] Attempting API login → {LOGIN_URL}")
    resp = session.post(LOGIN_URL, json=payload, headers=HEADERS, timeout=30)
    print(f"[Auth] API login status: {resp.status_code}")
    print(f"[Auth] Response: {resp.text[:300]}")

    token = None
    if resp.status_code in (200, 201):
        try:
            data  = resp.json()
            token = (
                data.get("token")
                or data.get("access_token")
                or data.get("authToken")
                or (data.get("data") or {}).get("token")
                or (data.get("data") or {}).get("access_token")
            )
            print(f"[Auth] Token extracted: {'yes' if token else 'no'}")
        except Exception as e:
            print(f"[Auth] JSON parse error: {e}")

    # Step C: Fallback — try alternate endpoint paths
    if not token and resp.status_code not in (200, 201):
        for alt_url in [
            "https://tradetron.tech/api/login",
            "https://tradetron.tech/sanctum/token",
            "https://tradetron.tech/api/user/login",
        ]:
            print(f"[Auth] Trying alternate endpoint: {alt_url}")
            r = session.post(alt_url, json=payload, headers=HEADERS, timeout=30)
            print(f"[Auth] Status: {r.status_code} | Response: {r.text[:200]}")
            if r.status_code in (200, 201):
                try:
                    d = r.json()
                    token = (
                        d.get("token") or d.get("access_token")
                        or d.get("authToken")
                        or (d.get("data") or {}).get("token")
                    )
                    if token:
                        print(f"[Auth] Token found at {alt_url}")
                        break
                except Exception:
                    pass

    # Step D: Form-based login fallback (get CSRF from login page first)
    if not token:
        print("\n[Auth] Trying form-based login...")
        login_page = session.get(f"{BASE_URL}/login", headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
        soup = BeautifulSoup(login_page.text, "html.parser")

        csrf_input = soup.find("input", {"name": "_token"})
        csrf_token = csrf_input["value"] if csrf_input else ""
        print(f"[Auth] CSRF from form: {csrf_token[:20] if csrf_token else 'not found'}")

        form_data = {
            "email":    TRADETRON_EMAIL,
            "password": TRADETRON_PASSWORD,
            "_token":   csrf_token,
        }
        form_resp = session.post(
            f"{BASE_URL}/login",
            data=form_data,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=True,
            timeout=30,
        )
        print(f"[Auth] Form login status: {form_resp.status_code}")
        print(f"[Auth] Final URL: {form_resp.url}")

    # ── Save everything ────────────────────────────────────────────────────────
    cookies_dict = dict(session.cookies)
    print(f"\n[Auth] All cookies saved: {list(cookies_dict.keys())}")

    if not cookies_dict and not token:
        raise RuntimeError("[Auth] Login completely failed — no cookies or token.")

    session_data = {
        "cookies": cookies_dict,
        "token":   token,
        "xsrf":    xsrf_token,
    }

    with open(SESSION_FILE, "w") as f:
        json.dump(session_data, f, indent=2)

    print(f"[Auth] Session saved → {SESSION_FILE}")

if __name__ == "__main__":
    login()