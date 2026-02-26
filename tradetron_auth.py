"""
tradetron_auth.py — Pure requests + Altcha PoW solver
──────────────────────────────────────────────────────
Performs a fresh email/password login on every run.
No session token reuse — always logs in from scratch.

Outputs:
  - GITHUB_OUTPUT step export: session_json  (cookies + fresh XSRF token)
"""

import os
import json
import hashlib
import base64
import requests
from urllib.parse import unquote
from bs4 import BeautifulSoup

TRADETRON_EMAIL    = os.environ.get("TRADETRON_EMAIL", "")
TRADETRON_PASSWORD = os.environ.get("TRADETRON_PASSWORD", "")
BASE_URL           = "https://tradetron.tech"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


# ── Altcha PoW solver ──────────────────────────────────────────────────────────
def solve_altcha(challenge_url: str, session: requests.Session) -> str:
    print(f"[Altcha] Fetching challenge: {challenge_url}")
    resp = session.get(challenge_url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30)
    print(f"[Altcha] Status: {resp.status_code}")

    d          = resp.json()
    algorithm  = d.get("algorithm", "SHA-256")
    challenge  = d.get("challenge")
    salt       = d.get("salt")
    signature  = d.get("signature")
    max_number = d.get("maxnumber", 1_000_000)

    if not challenge or not salt:
        raise RuntimeError("[Altcha] Missing challenge or salt in response")

    algo = algorithm.replace("-", "").lower()
    print(f"[Altcha] Solving PoW (max={max_number})...")

    for number in range(int(max_number) + 1):
        if hashlib.new(algo, f"{salt}{number}".encode()).hexdigest() == challenge:
            print(f"[Altcha] ✓ Solved at number={number}")
            payload = {"algorithm": algorithm, "challenge": challenge,
                       "number": number, "salt": salt, "signature": signature}
            return base64.b64encode(json.dumps(payload).encode()).decode()

    raise RuntimeError(f"[Altcha] Unsolvable within {max_number} iterations")


# ── Email/password login ───────────────────────────────────────────────────────
def do_login() -> dict:
    if not TRADETRON_EMAIL or not TRADETRON_PASSWORD:
        raise RuntimeError("TRADETRON_EMAIL and TRADETRON_PASSWORD must be set")

    session = requests.Session()

    print("[Auth] Fetching homepage...")
    session.get(BASE_URL, headers={"User-Agent": UA}, timeout=30)

    print("[Auth] Fetching login page...")
    login_page = session.get(
        f"{BASE_URL}/login",
        headers={"User-Agent": UA, "Accept": "text/html"},
        timeout=30,
    )
    soup = BeautifulSoup(login_page.text, "html.parser")

    # Extract CSRF token from the login form
    csrf_input = soup.find("input", {"name": "_token"})
    csrf_token = csrf_input["value"] if csrf_input else ""
    print(f"[Auth] CSRF token: {'found' if csrf_token else 'NOT FOUND'}")
    if not csrf_token:
        raise RuntimeError("[Auth] Could not find CSRF _token on login page")

    # Solve Altcha proof-of-work challenge
    altcha_widget = soup.find("altcha-widget")
    if not altcha_widget:
        altcha_widget = soup.find(lambda tag: tag.name and "altcha" in tag.name.lower())
    if not altcha_widget:
        raise RuntimeError("[Auth] altcha-widget not found on login page")

    challenge_url = altcha_widget.get("challengeurl") or altcha_widget.get("challenge-url", "")
    if not challenge_url.startswith("http"):
        challenge_url = BASE_URL + challenge_url

    altcha_payload = solve_altcha(challenge_url, session)

    # Use the pre-login XSRF only for the form POST itself
    pre_login_xsrf = unquote(session.cookies.get("XSRF-TOKEN", ""))

    form_data = {
        "_token":         csrf_token,
        "email":          TRADETRON_EMAIL,
        "password":       TRADETRON_PASSWORD,
        "altcha":         altcha_payload,
        "reference":      "",
        "force_redirect": "",
    }
    post_headers = {
        "User-Agent":   UA,
        "Referer":      f"{BASE_URL}/login",
        "Origin":       BASE_URL,
        "Accept":       "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-XSRF-TOKEN": pre_login_xsrf,
    }

    print(f"[Auth] Logging in as {TRADETRON_EMAIL}...")
    resp = session.post(
        f"{BASE_URL}/login",
        data=form_data,
        headers=post_headers,
        allow_redirects=True,
        timeout=30,
    )
    print(f"[Auth] POST status: {resp.status_code} | Final URL: {resp.url}")

    if "/login" in resp.url:
        raise RuntimeError("[Auth] Login FAILED — still on /login. Check credentials.")

    print(f"[Auth] ✓ Login SUCCESS → {resp.url}")

    # Fetch the dashboard so Tradetron issues a fresh post-login XSRF-TOKEN.
    # The pre-login token is stale and will cause /api/* calls to return success=false.
    print("[Auth] Fetching dashboard to refresh XSRF-TOKEN...")
    session.get(
        f"{BASE_URL}/user/dashboard",
        headers={"User-Agent": UA, "Accept": "text/html"},
        timeout=30,
    )

    cookies_dict = dict(session.cookies)
    fresh_xsrf   = unquote(cookies_dict.get("XSRF-TOKEN", ""))

    print(f"[Auth] Cookies: {list(cookies_dict.keys())}")
    print(f"[Auth] XSRF-TOKEN (post-login): {'✓ present' if fresh_xsrf else '✗ MISSING'}")

    return {
        "cookies": cookies_dict,
        "xsrf":    fresh_xsrf,
    }


# ── Export to GITHUB_OUTPUT ────────────────────────────────────────────────────
def export_session(session_data: dict) -> None:
    session_json_str = json.dumps(session_data)
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            f.write("session_json<<SESSION_EOF\n")
            f.write(session_json_str + "\n")
            f.write("SESSION_EOF\n")
        print("[Auth] ✓ Session exported to GITHUB_OUTPUT")
    else:
        print("[Auth] (Not in GitHub Actions — skipping GITHUB_OUTPUT)")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    session_data = do_login()
    export_session(session_data)