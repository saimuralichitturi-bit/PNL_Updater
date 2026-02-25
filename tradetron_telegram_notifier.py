"""
tradetron_telegram_notifier.py
──────────────────────────────
Sends a formatted PNL snapshot + strategy screenshots to a Telegram chat.

Reads:
  pnl_latest.csv              <- written by tradetron_scraper.py (always present)
  screenshots/manifest.json   <- written by tradetron_screenshots.py

Env vars:
  TELEGRAM_BOT_TOKEN   <- GitHub Secret
  TELEGRAM_CHAT_ID     <- GitHub Secret
  EOD_MODE             <- "true" for end-of-day run, "false" for intraday

Fixes:
  - Table header/data alignment corrected (was off by 2 chars)
  - sendPhoto falls back to sendDocument if file > 10MB
  - Robust error handling on photo send (no crash on one failure)
"""

import os
import csv
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_IDS = [
    cid.strip()
    for cid in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",")
    if cid.strip()
]
EOD_MODE  = os.environ.get("EOD_MODE", "false").strip().lower() == "true"
PNL_CSV   = "pnl_latest.csv"
MANIFEST  = Path("screenshots") / "manifest.json"
BASE_API  = f"https://api.telegram.org/bot{BOT_TOKEN}"
IST       = timezone(timedelta(hours=5, minutes=30))

PHOTO_SIZE_LIMIT = 10 * 1024 * 1024   # 10 MB — Telegram sendPhoto hard limit

# Emojis
E_GREEN = "🟢"
E_RED   = "🔴"
E_WHITE = "⚪"
E_UP    = "📈"
E_DOWN  = "📉"
E_CLOCK = "🕐"
E_CHART = "📊"
E_FIRE  = "🔥"
E_CAM   = "📸"
E_EOD   = "🏁"
E_LIVE  = "⚡"
E_DOC   = "📄"


# ── Helpers ────────────────────────────────────────────────────────────────────
def _to_float(raw) -> float:
    try:
        return float(str(raw).replace("₹", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _sign_emoji(v: float) -> str:
    return E_GREEN if v > 0 else (E_RED if v < 0 else E_WHITE)


def _inr(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}₹{v:,.2f}"


def _load_csv() -> list[dict]:
    if not Path(PNL_CSV).exists():
        print(f"[Telegram] {PNL_CSV} not found.")
        return []
    with open(PNL_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    with open(MANIFEST) as f:
        return json.load(f)


# ── Message builder ────────────────────────────────────────────────────────────
def _build_message(rows: list[dict]) -> str:
    now_ist  = datetime.now(IST)
    ts       = now_ist.strftime("%d %b %Y  %I:%M %p IST")
    run_type = f"{E_EOD} <b>End-of-Day Report</b>" if EOD_MODE else f"{E_LIVE} <b>Intraday Snapshot</b>"

    lines = [
        f"{E_CHART} <b>Tradetron PNL Update</b>",
        run_type,
        f"{E_CLOCK} <code>{ts}</code>",
        "─" * 38,
    ]

    if not rows:
        lines.append("<i>No strategy data available.</i>")
        return "\n".join(lines)

    # ── Column width ───────────────────────────────────────────────────────────
    # Each data row is:  "{emoji} {name:<name_w}  {live:>11}  {overall:>11}"
    # emoji takes 1 char + 1 space = prefix of 2 chars
    # Header row must match: "  {'Strategy':<name_w}  {'Live PNL':>11}  {'Overall':>11}"
    # We use name_w for the name field in BOTH header and data rows consistently.
    name_w = min(max(len(r.get("Strategy Name", "")) for r in rows), 24)

    # Header — 2-space indent to align with "emoji " prefix in data rows
    lines.append(
        f"<code>{'  ' + 'Strategy':<{name_w + 2}}  {'Live PNL':>11}  {'Overall':>11}</code>"
    )
    lines.append(f"<code>{'─' * (name_w + 28)}</code>")

    pnl_overall_vals: list[tuple[str, float]] = []
    winners = losers = flat = 0

    for row in rows:
        name    = row.get("Strategy Name", "—")[:name_w]
        live    = _to_float(row.get("PNL (Live/Open)", 0))
        overall = _to_float(row.get("PNL (Overall)", 0))
        emoji   = _sign_emoji(overall)

        # emoji + space = 2 chars, then name padded to name_w
        lines.append(
            f"<code>{emoji} {name:<{name_w}}  {_inr(live):>11}  {_inr(overall):>11}</code>"
        )
        pnl_overall_vals.append((row.get("Strategy Name", "—"), overall))

        if overall > 0:   winners += 1
        elif overall < 0: losers  += 1
        else:             flat    += 1

    lines.append(f"<code>{'─' * (name_w + 28)}</code>")

    total        = sum(v for _, v in pnl_overall_vals)
    best_n,  bv  = max(pnl_overall_vals, key=lambda x: x[1])
    worst_n, wv  = min(pnl_overall_vals, key=lambda x: x[1])
    total_emoji  = E_UP if total >= 0 else E_DOWN

    lines += [
        "",
        f"{total_emoji} <b>Total Overall PNL</b> : <b>{_inr(total)}</b>",
        f"{E_FIRE} <b>Best</b>   : {best_n}  <code>{_inr(bv)}</code>",
        f"{E_DOWN} <b>Worst</b>  : {worst_n}  <code>{_inr(wv)}</code>",
        f"✅ <b>W / L / Flat</b> : {winners} {E_GREEN}  {losers} {E_RED}  {flat} {E_WHITE}",
    ]

    if EOD_MODE:
        lines += ["", f"{E_EOD} <i>EOD CSV saved to Google Drive.</i>"]

    lines += ["", f"{E_CAM} <i>Strategy screenshots below ↓</i>"]
    return "\n".join(lines)


# ── Telegram senders ───────────────────────────────────────────────────────────
def _send_text(text: str) -> None:
    for chat_id in CHAT_IDS:
        resp = requests.post(
            f"{BASE_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if resp.ok:
            print(f"[Telegram] ✓ Message sent to {chat_id}")
        else:
            print(f"[Telegram] ✗ sendMessage failed ({chat_id}): {resp.status_code} — {resp.text}")



def _send_photo_or_doc(filepath: str, caption: str) -> None:
    file_size = Path(filepath).stat().st_size

    for chat_id in CHAT_IDS:

        if file_size <= PHOTO_SIZE_LIMIT:
            with open(filepath, "rb") as f:
                resp = requests.post(
                    f"{BASE_API}/sendPhoto",
                    data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                    files={"photo": f},
                    timeout=60,
                )
            if resp.ok:
                print(f"[Telegram] ✓ Photo sent to {chat_id}")
                continue
            print(f"[Telegram] sendPhoto failed ({chat_id}) — fallback to document")

        with open(filepath, "rb") as f:
            resp = requests.post(
                f"{BASE_API}/sendDocument",
                data={
                    "chat_id": chat_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                files={"document": f},
                timeout=120,
            )

        if resp.ok:
            print(f"[Telegram] ✓ Document sent to {chat_id}")
        else:
            print(f"[Telegram] ✗ Failed sending to {chat_id}: {resp.status_code}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN or not CHAT_IDS:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS must be set.")

    rows     = _load_csv()
    manifest = _load_manifest()

    # 1. Send PNL summary text
    _send_text(_build_message(rows))

    # 2. Send screenshots
    if not manifest:
        print("[Telegram] No screenshots in manifest — skipping photos.")
        return

    # Build lookup: Strategy ID (str) -> overall PNL
    pnl_lookup = {
        str(r.get("Strategy ID", "")): _to_float(r.get("PNL (Overall)", 0))
        for r in rows
    }

    sent = skipped = 0
    for entry in manifest:
        filepath = entry.get("file")
        if not filepath or not Path(filepath).exists():
            print(f"[Telegram] ⚠ Missing screenshot for '{entry.get('Strategy Name', '?')}' — skipping.")
            skipped += 1
            continue

        sid     = str(entry.get("strategy_id", ""))
        name    = entry.get("Strategy Name") or entry.get("strategy_name", "Strategy")
        overall = pnl_lookup.get(sid, _to_float(entry.get("pnl", 0)))
        emoji   = _sign_emoji(overall)

        caption = (
            f"{emoji} <b>{name}</b>\n"
            f"Overall PNL: <code>{_inr(overall)}</code>\n"
            f"{E_CLOCK} {entry.get('timestamp_ist', '')}"
        )

        try:
            _send_photo_or_doc(filepath, caption)
            sent += 1
        except Exception as exc:
            print(f"[Telegram] ✗ Failed to send '{name}': {exc}")
            skipped += 1

    print(f"\n[Telegram] Done — {sent} sent, {skipped} skipped.")


if __name__ == "__main__":
    main()
