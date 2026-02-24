"""
tradetron_auth.py — Pure requests + Altcha PoW solver
──────────────────────────────────────────────────────
Strategy:
  1. If tradetron_session.json exists → validate the session against a
     protected endpoint.
  2. Session still valid  → reuse it, skip login entirely.
  3. Session invalid/expired (any error) → do a fresh login and overwrite
     tradetron_session.json.

NOTE: The 1-hour re-run cadence is handled entirely by the GitHub Actions
      schedule trigger — no sleep/loop logic lives here.

Outputs:
  - tradetron_session.json  (runtime only, never committed)
  - GITHUB_OUTPUT step export: session_json
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

# A lightweight authenticated endpoint — any 200 response means the
# session cookies are still alive.
SESSION_CHECK_URL  = f"{BASE_URL}/user/profile"

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


# ── Session persistence helpers ────────────────────────────────────────────────
def _load_existing_session() -> dict | None:
    """Return parsed session dict from disk, or None if the file is absent/corrupt."""
    if not os.path.exists(SESSION_FILE):
        print("[Session] No existing session file found.")
        return None
    try:
        with open(SESSION_FILE) as f:
            data = json.load(f)
        if not data.get("cookies"):
            print("[Session] Session file exists but has no cookies — treating as invalid.")
            return None
        print(f"[Session] Loaded existing session from {SESSION_FILE}.")
        return data
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[Session] Could not read session file ({exc}) — will re-login.")
        return None


def _validate_session(session_data: dict) -> requests.Session | None:
    """
    Restore cookies into a requests.Session and hit SESSION_CHECK_URL.
    Returns the live Session on success, None on any error (expired, network, etc.).
    """
    session = requests.Session()
    for name, value in session_data["cookies"].items():
        session.cookies.set(name, value)

    try:
        print(f"[Session] Validating session against {SESSION_CHECK_URL} …")
        resp = session.get(
            SESSION_CHECK_URL,
            headers={"User-Agent": UA, "Accept": "text/html"},
            allow_redirects=True,
            timeout=20,
        )
        # Redirected back to /login → session has expired
        if "/login" in resp.url:
            print("[Session] Session expired — redirected to /login.")
            return None
        if resp.status_code == 200:
            print(f"[Session] ✓ Session is still valid (status={resp.status_code}).")
            return session
        print(f"[Session] Unexpected status {resp.status_code} — treating as invalid.")
        return None
    except Exception as exc:
        print(f"[Session] Validation request failed ({exc}) — will re-login.")
        return None


def _export_session(session_data: dict) -> None:
    """Write session to disk and optionally export to GITHUB_OUTPUT."""
    with open(SESSION_FILE, "w") as f:
        json.dump(session_data, f)
    print(f"[Auth] ✓ Session written to {SESSION_FILE} (runtime only, not committed)")

    session_json_str = json.dumps(session_data)
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            f.write("session_json<<SESSION_EOF\n")
            f.write(session_json_str + "\n")
            f.write("SESSION_EOF\n")
        print("[Auth] ✓ Session exported to GITHUB_OUTPUT")
    else:
        print("[Auth] (Not running in GitHub Actions — skipping GITHUB_OUTPUT export)")


# ── Fresh login ────────────────────────────────────────────────────────────────
def _do_login() -> dict:
    """
    Perform a full Altcha-PoW login and return the session_data dict.
    Raises RuntimeError if login fails for any reason.
    """
    if not TRADETRON_EMAIL or not TRADETRON_PASSWORD:
        raise RuntimeError("Set TRADETRON_EMAIL and TRADETRON_PASSWORD env vars")

    session = requests.Session()

    # Step 1: Seed cookies from homepage
    print("[Auth] Fetching homepage...")
    session.get(BASE_URL, headers={"User-Agent": UA}, timeout=30)

    # Step 2: Fetch login page
    print("[Auth] Fetching login page...")
    login_page = session.get(
        f"{BASE_URL}/login",
        headers={"User-Agent": UA, "Accept": "text/html"},
        timeout=30,
    )
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
    print("[Auth] Altcha solved ✓")

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
    resp = session.post(
        f"{BASE_URL}/login",
        data=form_data,
        headers=headers,
        allow_redirects=True,
        timeout=30,
    )
    print(f"[Auth] POST status: {resp.status_code} | Final URL: {resp.url}")

    if "/login" in resp.url:
        raise RuntimeError("[Auth] Login FAILED — still on /login. Check credentials.")

    print(f"[Auth] ✓ Login SUCCESS → {resp.url}")

    # Step 8: Build session data dict
    cookies_dict = dict(session.cookies)
    print(f"[Auth] Cookies captured: {list(cookies_dict.keys())}")

    return {
        "cookies": cookies_dict,
        "token":   None,
        "xsrf":    unquote(cookies_dict.get("XSRF-TOKEN", "")),
    }


# ── Entry point ────────────────────────────────────────────────────────────────
def login() -> None:
    """
    Main entry point.

    Flow:
      1. Try to load an existing session from disk.
      2. If found, validate it against a protected endpoint.
         ✓ Valid  → reuse it (export to GITHUB_OUTPUT, skip re-login).
         ✗ Invalid/error → fall through to fresh login.
      3. Fresh login via Altcha PoW → save & export new session.
    """
    # ── Try existing session first ──────────────────────────────────────────
    existing = _load_existing_session()
    if existing is not None:
        try:
            live_session = _validate_session(existing)
            if live_session is not None:
                print("[Auth] Reusing existing valid session — no login required.")
                _export_session(existing)   # re-export so GITHUB_OUTPUT is always set
                return
        except Exception as exc:
            # Defensive catch — any unexpected error falls through to re-login
            print(f"[Auth] Session validation raised unexpectedly ({exc}) — re-logging in.")

    # ── Existing session absent or invalid → fresh login ───────────────────
    print("[Auth] Performing fresh login...")
    try:
        session_data = _do_login()
    except Exception as exc:
        raise RuntimeError(f"[Auth] Fresh login failed: {exc}") from exc

    _export_session(session_data)


if __name__ == "__main__":
    login()
