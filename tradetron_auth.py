"""
tradetron_auth.py
─────────────────
Logs into Tradetron using credentials from GitHub Secrets
and saves the session token/cookies for downstream scripts.

OUTPUT FILE:
  tradetron_session.json  →  read by tradetron_scraper.py & google_drive_uploader.py
"""

import os
import json
import requests

# ── FILE NAMES ─────────────────────────────────────────────────────────────────
SESSION_FILE = "tradetron_session.json"   # ← output: read by tradetron_scraper.py

# ── CREDENTIALS (from GitHub Secrets) ─────────────────────────────────────────
TRADETRON_EMAIL    = os.environ["TRADETRON_EMAIL"]
TRADETRON_PASSWORD = os.environ["TRADETRON_PASSWORD"]

# ── ENDPOINTS ──────────────────────────────────────────────────────────────────
LOGIN_URL     = "https://tradetron.tech/api/login"
ALT_LOGIN_URL = "https://tradetron.tech/login"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":       "application/json",
    "Origin":       "https://tradetron.tech",
    "Referer":      "https://tradetron.tech/login",
}

# ── LOGIN ──────────────────────────────────────────────────────────────────────
def login():
    session = requests.Session()

    # Attempt 1: JSON API login
    payload  = {"email": TRADETRON_EMAIL, "password": TRADETRON_PASSWORD}
    response = session.post(LOGIN_URL, json=payload, headers=HEADERS, timeout=30)
    print(f"[Auth] Primary login status: {response.status_code}")

    # Attempt 2: form-based fallback
    if response.status_code not in (200, 201):
        response = session.post(
            ALT_LOGIN_URL,
            data=payload,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=True,
            timeout=30,
        )
        print(f"[Auth] Fallback login status: {response.status_code}")

    session_data = {
        "cookies": dict(session.cookies),
        "token":   None,
    }

    # Extract Bearer token if present in JSON response
    try:
        resp_json = response.json()
        print(f"[Auth] Response keys: {list(resp_json.keys())}")
        token = (
            resp_json.get("token")
            or resp_json.get("access_token")
            or (resp_json.get("data") or {}).get("token")
        )
        if token:
            session_data["token"] = token
            print("[Auth] Bearer token extracted successfully.")
    except Exception:
        pass  # HTML response — cookies alone are enough

    if not session_data["cookies"] and not session_data["token"]:
        raise RuntimeError("[Auth] Login failed: no cookies or token received.")

    with open(SESSION_FILE, "w") as f:
        json.dump(session_data, f, indent=2)

    print(f"[Auth] Session saved → {SESSION_FILE}")
    print(f"[Auth] Cookies: {list(session_data['cookies'].keys())}")


if __name__ == "__main__":
    login()