"""
tradetron_telegram_notifier.py
──────────────────────────────
Sends the PNL table image to Telegram.
Caption: "PNL Data — date time".  No emojis.  No screenshots.

Reads:  pnl_table.json   ← written by tradetron_screenshots.py

Env vars:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_IDS   (comma-separated)
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_IDS   = [
    cid.strip()
    for cid in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",")
    if cid.strip()
]
TABLE_JSON = "pnl_table.json"
BASE_API   = f"https://api.telegram.org/bot{BOT_TOKEN}"
IST        = timezone(timedelta(hours=5, minutes=30))


# ── Loaders ────────────────────────────────────────────────────────────────────
def _load_table_data() -> dict:
    if not Path(TABLE_JSON).exists():
        print(f"[Telegram] {TABLE_JSON} not found.")
        return {}
    with open(TABLE_JSON) as f:
        return json.load(f)


# ── Senders ────────────────────────────────────────────────────────────────────
def _send_text(text: str) -> None:
    for chat_id in CHAT_IDS:
        resp = requests.post(
            f"{BASE_API}/sendMessage",
            json={
                "chat_id":                  chat_id,
                "text":                     text,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        status = "sent" if resp.ok else f"FAILED {resp.status_code}"
        print(f"[Telegram] Text to {chat_id}: {status}")
        if not resp.ok:
            print(f"[Telegram]   {resp.text[:200]}")


def _send_photo(filepath: str, caption: str) -> None:
    for chat_id in CHAT_IDS:
        with open(filepath, "rb") as f:
            resp = requests.post(
                f"{BASE_API}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=60,
            )
        status = "sent" if resp.ok else f"FAILED {resp.status_code}"
        print(f"[Telegram] Photo to {chat_id}: {status}")
        if not resp.ok:
            print(f"[Telegram]   {resp.text[:200]}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN or not CHAT_IDS:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS must be set.")

    data       = _load_table_data()
    image_path = data.get("image_path")
    ts         = data.get("timestamp") or datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")
    n_rows     = data.get("rows", 0)
    caption    = f"PNL Data  —  {ts}"

    if image_path and Path(image_path).exists():
        _send_photo(image_path, caption)
        print(f"[Telegram] Done — {n_rows} strategies in table image.")
    else:
        _send_text(f"<b>{caption}</b>\n\n<i>Table image not available ({n_rows} strategies).</i>")
        print("[Telegram] Done — image not found, sent text fallback.")


if __name__ == "__main__":
    main()
