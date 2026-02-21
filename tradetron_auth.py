"""
tradetron_auth.py — Pure requests + manual Altcha PoW solver (no browser needed)
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

# ── STEP 1: Solve Altcha PoW in pure Python ────────────────────────────────────
def solve_altcha(challenge_url: str, session: requests.Session) -> str:
    """
    Fetches the Altcha challenge from the server and solves the SHA-256 PoW.
    Returns a base64-encoded payload string to inject into the form.
    """
    print(f"[Altcha] Fetching challenge from: {challenge_url}")
    resp = session.get(challenge_url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30)
    print(f"[Altcha] Challenge response status: {resp.status_code}")
    print(f"[Altcha] Challenge raw: {resp.text[:300]}")

    challenge_data = resp.json()
    algorithm  = challenge_data.get("algorithm", "SHA-256")
    challenge  = challenge_data.get("challenge")
    salt       = challenge_data.get("salt")
    signature  = challenge_data.get("signature")
    max_number = challenge_data.get("maxnumber", 1_000_000)

    print(f"[Altcha] algorithm={algorithm}, salt={salt}, maxnumber={max_number}")
    print(f"[Altcha] challenge={challenge}")

    if not challenge or not salt:
        raise RuntimeError("[Altcha] Invalid challenge response — missing challenge or salt")

    # ── Brute-force the PoW ────────────────────────────────────────────────────
    print(f"[Altcha] Solving PoW (max iterations: {max_number})...")
    algo = algorithm.replace("-", "").lower()   # "SHA-256" → "sha256"

    for number in range(int(max_number) + 1):
        test_str  = f"{salt}{number}".encode("utf-8")
        test_hash = hashlib.new(algo, test_str).hexdigest()
        if test_hash == challenge:
            print(f"[Altcha] ✓ Solution found! number={number}")
            # Build the payload dict
            payload = {
                "algorithm": algorithm,
                "challenge": challenge,
                "number":    number,
                "salt":      salt,
                "signature": signature,
            }
            # Altcha expects this as a base64-encoded JSON string
            payload_b64 = base64.b64encode(
                json.dumps(payload).encode("utf-8")
            ).decode("utf-8")
            return payload_b64

    raise RuntimeError(f"[Altcha] PoW unsolvable within {max_number} iterations")


# ── MAIN LOGIN FUNCTION ────────────────────────────────────────────────────────
def login():
    if not TRADETRON_EMAIL or not TRADETRON_PASSWORD:
        raise RuntimeError("Set TRADETRON_EMAIL and TRADETRON_PASSWORD env vars")

    session = requests.Session()

    # Step 1: Fetch homepage to seed cookies
    print("[Auth] Fetching homepage...")
    session.get(BASE_URL, headers={"User-Agent": UA}, timeout=30)

    # Step 2: Fetch login page
    print("[Auth] Fetching login page...")
    login_page = session.get(
        f"{BASE_URL}/login",
        headers={"User-Agent": UA, "Accept": "text/html"},
        timeout=30
    )
    soup = BeautifulSoup(login_page.text, "html.parser")

    # Step 3: Extract _token (CSRF)
    csrf_input = soup.find("input", {"name": "_token"})
    csrf_token = csrf_input["value"] if csrf_input else ""
    print(f"[Auth] CSRF _token: {'found' if csrf_token else 'NOT FOUND'}")

    # Step 4: Find the Altcha widget and its challengeurl attribute
    altcha_widget = soup.find("altcha-widget")
    if not altcha_widget:
        # Sometimes it's a custom element with different casing or inside shadow DOM
        altcha_widget = soup.find(lambda tag: tag.name and "altcha" in tag.name.lower())

    if altcha_widget:
        challenge_url = altcha_widget.get("challengeurl") or altcha_widget.get("challenge-url")
        print(f"[Auth] Altcha widget found. challengeurl: {challenge_url}")
    else:
        print("[Auth] WARNING: altcha-widget tag not found in HTML. Dumping all custom elements:")
        for tag in soup.find_all(True):
            if "-" in tag.name:  # custom elements have hyphens
                print(f"  <{tag.name}> attrs: {tag.attrs}")
        raise RuntimeError("[Auth] Could not find altcha-widget on login page")

    # Step 5: Make the challenge URL absolute if it's relative
    if challenge_url and not challenge_url.startswith("http"):
        challenge_url = BASE_URL + challenge_url
    print(f"[Auth] Full challenge URL: {challenge_url}")

    # Step 6: Solve the Altcha PoW
    altcha_payload = solve_altcha(challenge_url, session)
    print(f"[Auth] Altcha payload (b64): {altcha_payload[:60]}...")

    # Step 7: Get XSRF token (URL-decoded)
    raw_xsrf   = session.cookies.get("XSRF-TOKEN", "")
    xsrf_token = unquote(raw_xsrf)

    # Step 8: Build and submit the login form
    form_data = {
        "_token":    csrf_token,
        "email":     TRADETRON_EMAIL,
        "password":  TRADETRON_PASSWORD,
        "altcha":    altcha_payload,      # ← the solved PoW injected here
        "reference": "",
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

    print("[Auth] Submitting login form with solved Altcha...")
    resp = session.post(
        f"{BASE_URL}/login",
        data=form_data,
        headers=headers,
        allow_redirects=True,
        timeout=30
    )
    print(f"[Auth] POST status: {resp.status_code}")
    print(f"[Auth] Final URL:   {resp.url}")

    # Step 9: Verify login success
    if "/login" in resp.url:
        err_soup = BeautifulSoup(resp.text, "html.parser")
        # Print any validation errors
        for el in err_soup.find_all(class_=lambda c: c and any(
            x in c for x in ["error", "alert", "invalid", "danger"]
        )):
            txt = el.get_text(strip=True)
            if txt:
                print(f"[Auth] Page error: {txt[:300]}")
        raise RuntimeError("[Auth] Login FAILED — still on /login. Check credentials or form field names.")

    print(f"[Auth] ✓ Login SUCCESS → redirected to: {resp.url}")

    # Step 10: Try to extract bearer token from dashboard JS
    token = None
    try:
        dashboard = session.get(
            f"{BASE_URL}/dashboard",
            headers={"User-Agent": UA, "Accept": "text/html"},
            timeout=30
        )
        d_soup = BeautifulSoup(dashboard.text, "html.parser")
        # Check meta tags for API token
        for meta in d_soup.find_all("meta"):
            if any(x in (meta.get("name", "") + meta.get("id", "")).lower() for x in ["token", "api"]):
                token = meta.get("content")
                print(f"[Auth] Token found in meta: {meta}")
                break
        # Check inline scripts for token patterns
        if not token:
            import re
            scripts = d_soup.find_all("script")
            for s in scripts:
                if s.string:
                    m = re.search(r'["\']?(?:api_token|authToken|token)["\']?\s*[:=]\s*["\']([^"\']{20,})["\']', s.string)
                    if m:
                        token = m.group(1)
                        print(f"[Auth] Token found in inline script")
                        break
    except Exception as e:
        print(f"[Auth] Token extraction error (non-fatal): {e}")

    # Step 11: Save session
    cookies_dict = dict(session.cookies)
    print(f"[Auth] Saving cookies: {list(cookies_dict.keys())}")

    session_data = {
        "cookies": cookies_dict,
        "token":   token,
        "xsrf":    unquote(cookies_dict.get("XSRF-TOKEN", "")),
    }
    with open(SESSION_FILE, "w") as f:
        json.dump(session_data, f, indent=2)

    print(f"[Auth] ✓ Session saved → {SESSION_FILE}")


if __name__ == "__main__":
    login()