"""
tradetron_auth.py — Pure requests + Altcha PoW solver
──────────────────────────────────────────────────────
Logs into Tradetron and saves session to:
  - tradetron_session.json  (runtime only, never committed)

The session JSON is also exported as a GitHub Actions output
so it can be passed securely between steps via GITHUB_OUTPUT.
"""

import os
import json
import hashlib
import base64
import requests
from urllib.parse import unquote
from bs4 import BeautifulSoup

SESSION_FILE       = "tradetron_session.json"
TRADETRON_EMAIL    = os.environ.get("TRADETRON_EMAIL", "")
TRADETRON_PASSWORD = os.environ.get("TRADETRON_PASSWORD", "")
BASE_URL           = "https://tradetron.tech"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


# ── Altcha PoW solver ──────────────────────────────────────────────────────────
def solve_altcha(challenge_url: str, session: requests.Session) -> str:
    print(f"[Altcha] Fetching challenge from: {challenge_url}")
    resp = session.get(challenge_url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30)
    print(f"[Altcha] Challenge response status: {resp.status_code}")

    challenge_data = resp.json()
    algorithm  = challenge_data.get("algorithm", "SHA-256")
    challenge  = challenge_data.get("challenge")
    salt       = challenge_data.get("salt")
    signature  = challenge_data.get("signature")
    max_number = challenge_data.get("maxnumber", 1_000_000)

    print(f"[Altcha] algorithm={algorithm}, maxnumber={max_number}")

    if not challenge or not salt:
        raise RuntimeError("[Altcha] Invalid challenge response — missing challenge or salt")

    algo = algorithm.replace("-", "").lower()
    print(f"[Altcha] Solving PoW (max iterations: {max_number})...")

    for number in range(int(max_number) + 1):
        test_hash = hashlib.new(algo, f"{salt}{number}".encode()).hexdigest()
        if test_hash == challenge:
            print(f"[Altcha] ✓ Solution found! number={number}")
            payload = {
                "algorithm": algorithm,
                "challenge": challenge,
                "number":    number,
                "salt":      salt,
                "signature": signature,
            }
            return base64.b64encode(json.dumps(payload).encode()).decode()

    raise RuntimeError(f"[Altcha] PoW unsolvable within {max_number} iterations")


# ── Main login ─────────────────────────────────────────────────────────────────
def login():
    if not TRADETRON_EMAIL or not TRADETRON_PASSWORD:
        raise RuntimeError("Set TRADETRON_EMAIL and TRADETRON_PASSWORD env vars")

    session = requests.Session()

    # Step 1: Seed cookies from homepage
    print("[Auth] Fetching homepage...")
    session.get(BASE_URL, headers={"User-Agent": UA}, timeout=30)

    # Step 2: Fetch login page
    print("[Auth] Fetching login page...")
    login_page = session.get(f"{BASE_URL}/login", headers={"User-Agent": UA, "Accept": "text/html"}, timeout=30)
    soup = BeautifulSoup(login_page.text, "html.parser")

    # Step 3: Extract CSRF token
    csrf_input = soup.find("input", {"name": "_token"})
    csrf_token = csrf_input["value"] if csrf_input else ""
    print(f"[Auth] CSRF _token: {'found' if csrf_token else 'NOT FOUND'}")

    # Step 4: Find Altcha widget challengeurl
    altcha_widget = soup.find("altcha-widget")
    if not altcha_widget:
        altcha_widget = soup.find(lambda tag: tag.name and "altcha" in tag.name.lower())
    if not altcha_widget:
        raise RuntimeError("[Auth] Could not find altcha-widget on login page")

    challenge_url = altcha_widget.get("challengeurl") or altcha_widget.get("challenge-url", "")
    if not challenge_url.startswith("http"):
        challenge_url = BASE_URL + challenge_url
    print(f"[Auth] Altcha challenge URL: {challenge_url}")

    # Step 5: Solve Altcha PoW
    altcha_payload = solve_altcha(challenge_url, session)
    print(f"[Auth] Altcha solved ✓")

    # Step 6: URL-decode XSRF token
    raw_xsrf   = session.cookies.get("XSRF-TOKEN", "")
    xsrf_token = unquote(raw_xsrf)

    # Step 7: Submit login form
    form_data = {
        "_token":         csrf_token,
        "email":          TRADETRON_EMAIL,
        "password":       TRADETRON_PASSWORD,
        "altcha":         altcha_payload,
        "reference":      "",
        "force_redirect": "",
    }
    headers = {
        "User-Agent":   UA,
        "Referer":      f"{BASE_URL}/login",
        "Origin":       BASE_URL,
        "Accept":       "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-XSRF-TOKEN": xsrf_token,
    }

    print("[Auth] Submitting login form...")
    resp = session.post(f"{BASE_URL}/login", data=form_data, headers=headers, allow_redirects=True, timeout=30)
    print(f"[Auth] POST status: {resp.status_code} | Final URL: {resp.url}")

    if "/login" in resp.url:
        raise RuntimeError("[Auth] Login FAILED — still on /login. Check credentials.")

    print(f"[Auth] ✓ Login SUCCESS → {resp.url}")

    # Step 8: Build session data dict (cookies only — no token needed)
    cookies_dict = dict(session.cookies)
    print(f"[Auth] Cookies captured: {list(cookies_dict.keys())}")

    session_data = {
        "cookies": cookies_dict,
        "token":   None,
        "xsrf":    unquote(cookies_dict.get("XSRF-TOKEN", "")),
    }

    # Step 9: Write to local runtime file (used by tradetron_scraper.py in same job)
    with open(SESSION_FILE, "w") as f:
        json.dump(session_data, f)
    print(f"[Auth] ✓ Session written to {SESSION_FILE} (runtime only, not committed)")

    # Step 10: Export session JSON as a GitHub Actions step output
    # This allows other steps to read it via ${{ steps.auth.outputs.session_json }}
    session_json_str = json.dumps(session_data)
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            # Use heredoc syntax to safely handle special characters in JSON
            f.write("session_json<<SESSION_EOF\n")
            f.write(session_json_str + "\n")
            f.write("SESSION_EOF\n")
        print("[Auth] ✓ Session exported to GITHUB_OUTPUT")
    else:
        print("[Auth] (Not running in GitHub Actions — skipping GITHUB_OUTPUT export)")


if __name__ == "__main__":
    login()