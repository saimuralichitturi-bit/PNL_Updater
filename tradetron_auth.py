"""
tradetron_auth.py — Pure requests + Altcha PoW solver
──────────────────────────────────────────────────────
Session Strategy (GitHub Actions compatible):
  1. Check TRADETRON_SESSION env var (injected from GitHub Secret)
  2. If present → validate it against a protected endpoint
     ✓ Valid   → reuse it, skip login
     ✗ Invalid → do a fresh login
  3. If env var absent → do a fresh login
  4. After fresh login → update the GitHub Secret automatically via GitHub API
     so the next run reuses the fresh session

NOTE: tradetron_session.json is NOT used for session persistence across runs.
      GitHub Secret TRADETRON_SESSION is the single source of truth.

Outputs:
  - GITHUB_OUTPUT step export: session_json  (passed to scraper + screenshots)
  - GitHub Secret TRADETRON_SESSION          (updated on fresh login)
"""

import os
import json
import hashlib
import base64
import requests
from urllib.parse import unquote
from bs4 import BeautifulSoup

# ── Env vars ───────────────────────────────────────────────────────────────────
TRADETRON_EMAIL    = os.environ.get("TRADETRON_EMAIL", "")
TRADETRON_PASSWORD = os.environ.get("TRADETRON_PASSWORD", "")
TRADETRON_SESSION  = os.environ.get("TRADETRON_SESSION", "")   # from GitHub Secret

# GitHub API vars — needed to update the secret after fresh login
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")         # auto-provided by Actions
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")    # e.g. "user/repo"

BASE_URL          = "https://tradetron.tech"
SESSION_CHECK_URL = f"{BASE_URL}/user/profile"
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


# ── Session validation ─────────────────────────────────────────────────────────
def _validate_session(session_data: dict) -> requests.Session | None:
    """
    Restore cookies from session_data dict and hit SESSION_CHECK_URL.
    Returns live requests.Session on success, None if expired/invalid.
    """
    session = requests.Session()
    for name, value in session_data.get("cookies", {}).items():
        session.cookies.set(name, value)

    try:
        print(f"[Session] Validating session against {SESSION_CHECK_URL} ...")
        resp = session.get(
            SESSION_CHECK_URL,
            headers={"User-Agent": UA, "Accept": "text/html"},
            allow_redirects=True,
            timeout=20,
        )
        if "/login" in resp.url:
            print("[Session] ✗ Session expired — redirected to /login.")
            return None
        if resp.status_code == 200:
            print(f"[Session] ✓ Session is still valid (status=200, url={resp.url})")
            return session
        print(f"[Session] ✗ Unexpected status {resp.status_code} — treating as invalid.")
        return None
    except Exception as exc:
        print(f"[Session] ✗ Validation failed ({exc}) — will re-login.")
        return None


# ── GitHub Secret updater ──────────────────────────────────────────────────────
def _update_github_secret(secret_value: str) -> None:
    """
    Update the TRADETRON_SESSION GitHub Secret via the GitHub API
    so the next workflow run picks up the fresh session automatically.

    Requires:
      - GITHUB_TOKEN  (automatically available in GitHub Actions)
      - GITHUB_REPOSITORY  (automatically available in GitHub Actions)
    """
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        print("[Secret] Skipping GitHub Secret update — GITHUB_TOKEN or GITHUB_REPOSITORY not set.")
        print("[Secret] (This is fine for local runs)")
        return

    try:
        from base64 import b64encode
        # We need the repo public key to encrypt the secret
        api_base = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
        headers  = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        # Step 1: Get repo public key
        pk_resp = requests.get(f"{api_base}/actions/secrets/public-key", headers=headers, timeout=15)
        pk_resp.raise_for_status()
        pk_data   = pk_resp.json()
        key_id    = pk_data["key_id"]
        pub_key_b64 = pk_data["key"]

        # Step 2: Encrypt secret using libsodium (PyNaCl)
        try:
            from nacl import encoding, public as nacl_public

            pub_key_bytes = b64encode(pub_key_b64.encode()) if isinstance(pub_key_b64, str) else pub_key_b64
            # pub_key is already base64 from GitHub API
            public_key = nacl_public.PublicKey(pub_key_b64.encode(), encoding.Base64Encoder)
            sealed_box = nacl_public.SealedBox(public_key)
            encrypted  = sealed_box.encrypt(secret_value.encode())
            encrypted_b64 = b64encode(encrypted).decode()

        except ImportError:
            # PyNaCl not installed — use a simple fallback that just logs a warning
            print("[Secret] ⚠️  PyNaCl not installed — cannot encrypt secret.")
            print("[Secret] Add 'PyNaCl' to requirements.txt to enable auto-update of TRADETRON_SESSION secret.")
            return

        # Step 3: PUT the encrypted secret
        put_resp = requests.put(
            f"{api_base}/actions/secrets/TRADETRON_SESSION",
            headers=headers,
            json={
                "encrypted_value": encrypted_b64,
                "key_id":          key_id,
            },
            timeout=15,
        )
        if put_resp.status_code in (201, 204):
            print("[Secret] ✓ GitHub Secret TRADETRON_SESSION updated successfully.")
        else:
            print(f"[Secret] ✗ Failed to update secret: {put_resp.status_code} — {put_resp.text}")

    except Exception as exc:
        # Non-fatal — just log the warning
        print(f"[Secret] ✗ Could not update GitHub Secret ({exc}). Session will still work for this run.")


# ── Export to GITHUB_OUTPUT ────────────────────────────────────────────────────
def _export_to_output(session_data: dict) -> None:
    """Pass session JSON to subsequent steps via GITHUB_OUTPUT."""
    session_json_str = json.dumps(session_data)
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            f.write("session_json<<SESSION_EOF\n")
            f.write(session_json_str + "\n")
            f.write("SESSION_EOF\n")
        print("[Auth] ✓ Session exported to GITHUB_OUTPUT")
    else:
        print("[Auth] (Not in GitHub Actions — skipping GITHUB_OUTPUT export)")


# ── Fresh login ────────────────────────────────────────────────────────────────
def _do_login() -> dict:
    """
    Perform a full Altcha-PoW login and return the session_data dict.
    Raises RuntimeError if login fails.
    """
    if not TRADETRON_EMAIL or not TRADETRON_PASSWORD:
        raise RuntimeError("TRADETRON_EMAIL and TRADETRON_PASSWORD env vars must be set.")

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

    csrf_input = soup.find("input", {"name": "_token"})
    csrf_token = csrf_input["value"] if csrf_input else ""
    print(f"[Auth] CSRF _token: {'found' if csrf_token else 'NOT FOUND'}")

    altcha_widget = soup.find("altcha-widget")
    if not altcha_widget:
        altcha_widget = soup.find(lambda tag: tag.name and "altcha" in tag.name.lower())
    if not altcha_widget:
        raise RuntimeError("[Auth] Could not find altcha-widget on login page")

    challenge_url = altcha_widget.get("challengeurl") or altcha_widget.get("challenge-url", "")
    if not challenge_url.startswith("http"):
        challenge_url = BASE_URL + challenge_url
    print(f"[Auth] Altcha challenge URL: {challenge_url}")

    altcha_payload = solve_altcha(challenge_url, session)
    print("[Auth] Altcha solved ✓")

    raw_xsrf   = session.cookies.get("XSRF-TOKEN", "")
    xsrf_token = unquote(raw_xsrf)

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
    Main flow:
      1. If TRADETRON_SESSION env var is set → validate it
         ✓ Valid  → reuse, export to GITHUB_OUTPUT, done
         ✗ Invalid → fall through to fresh login
      2. Fresh login → export to GITHUB_OUTPUT + update GitHub Secret
    """

    # ── Try session from env var (GitHub Secret) ───────────────────────────
    if TRADETRON_SESSION:
        print("[Auth] Found TRADETRON_SESSION env var — validating...")
        try:
            session_data = json.loads(TRADETRON_SESSION)
            live = _validate_session(session_data)
            if live is not None:
                print("[Auth] ✓ Existing session is valid — reusing, no login needed.")
                _export_to_output(session_data)
                return
            else:
                print("[Auth] Session invalid/expired — performing fresh login...")
        except (json.JSONDecodeError, Exception) as exc:
            print(f"[Auth] Could not parse TRADETRON_SESSION ({exc}) — performing fresh login...")
    else:
        print("[Auth] No TRADETRON_SESSION env var found — performing fresh login...")

    # ── Fresh login ────────────────────────────────────────────────────────
    try:
        session_data = _do_login()
    except Exception as exc:
        raise RuntimeError(f"[Auth] Fresh login failed: {exc}") from exc

    # Export to GITHUB_OUTPUT for this run's subsequent steps
    _export_to_output(session_data)

    # Update GitHub Secret so next run can reuse this session
    _update_github_secret(json.dumps(session_data))


if __name__ == "__main__":
    login()
