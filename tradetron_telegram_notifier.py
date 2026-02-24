"""
tradetron_telegram_notifier.py
──────────────────────────────
Sends a formatted PNL snapshot + strategy screenshots to a Telegram chat.

Reads:
  pnl_latest.csv              <- written by tradetron_scraper.py (always present)
  screenshots/manifest.json   <- written by tradetron_screenshots.py

CSV columns (from tradetron_scraper.py -> parse_strategy):
  Strategy ID, Strategy Name, Status, Deployment Type, Exchange, Broker,
  Capital Required, PNL (Last Run), PNL (Overall), PNL (Live/Open),
  Run Counter, Completed Runs, Currency, Deployment Date, Creator,
  Snapshot Time

Env vars:
  TELEGRAM_BOT_TOKEN   <- GitHub Secret
  TELEGRAM_CHAT_ID     <- GitHub Secret
  EOD_MODE             <- "true" for end-of-day run, "false" for intraday
"""

import os
import csv
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
EOD_MODE    = os.environ.get("EOD_MODE", "false").strip().lower() == "true"
PNL_CSV     = "pnl_latest.csv"
MANIFEST    = Path("screenshots") / "manifest.json"
BASE_API    = f"https://api.telegram.org/bot{BOT_TOKEN}"
IST         = timezone(timedelta(hours=5, minutes=30))

# Emojis
E_GREEN  = "🟢"
E_RED    = "🔴"
E_WHITE  = "⚪"
E_UP     = "📈"
E_DOWN   = "📉"
E_CLOCK  = "🕐"
E_CHART  = "📊"
E_FIRE   = "🔥"
E_CAM    = "📸"
E_EOD    = "🏁"
E_LIVE   = "⚡"


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
        f"{run_type}",
        f"{E_CLOCK} <code>{ts}</code>",
        "─" * 38,
    ]

    if not rows:
        lines.append("<i>No strategy data available.</i>")
        return "\n".join(lines)

    # Column widths
    name_w = min(max(len(r.get("Strategy Name", "")) for r in rows), 26)

    lines.append(
        f"<code>{'Strategy':<{name_w}}  {'Live PNL':>11}  {'Overall':>11}</code>"
    )
    lines.append(f"<code>{'─' * (name_w + 26)}</code>")

    pnl_overall_vals: list[tuple[str, float]] = []
    winners = losers = flat = 0

    for row in rows:
        name    = row.get("Strategy Name", "—")[:name_w]
        live    = _to_float(row.get("PNL (Live/Open)", 0))
        overall = _to_float(row.get("PNL (Overall)", 0))
        emoji   = _sign_emoji(overall)

        lines.append(
            f"<code>{emoji} {name:<{name_w - 2}}  {_inr(live):>11}  {_inr(overall):>11}</code>"
        )
        pnl_overall_vals.append((row.get("Strategy Name", "—"), overall))

        if overall > 0:   winners += 1
        elif overall < 0: losers  += 1
        else:             flat    += 1

    lines.append(f"<code>{'─' * (name_w + 26)}</code>")

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

    lines += ["", f"{E_CAM} <i>Strategy screenshots below</i>"]
    return "\n".join(lines)


# ── Telegram senders ───────────────────────────────────────────────────────────
def _send_text(text: str) -> None:
    resp = requests.post(
        f"{BASE_API}/sendMessage",
        json={
            "chat_id":    CHAT_ID,
            "text":       text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if resp.ok:
        print("[Telegram] Message sent ✓")
    else:
        print(f"[Telegram] sendMessage failed: {resp.status_code} — {resp.text}")
        resp.raise_for_status()


def _send_photo(filepath: str, caption: str) -> None:
    with open(filepath, "rb") as photo:
        resp = requests.post(
            f"{BASE_API}/sendPhoto",
            data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": photo},
            timeout=60,
        )
    if resp.ok:
        print(f"[Telegram] Photo sent: {filepath} ✓")
    else:
        print(f"[Telegram] sendPhoto failed ({filepath}): {resp.status_code} — {resp.text}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")

    rows     = _load_csv()
    manifest = _load_manifest()

    # 1. Send PNL summary text
    _send_text(_build_message(rows))

    # 2. Send screenshots — one photo per strategy, caption = name + PNL
    if not manifest:
        print("[Telegram] No screenshots in manifest — skipping photos.")
        return

    # Build a quick lookup: strategy_id -> overall PNL from CSV
    pnl_lookup = {
        r.get("Strategy ID", ""): _to_float(r.get("PNL (Overall)", 0))
        for r in rows
    }

    for entry in manifest:
        filepath = entry.get("file")
        if not filepath or not Path(filepath).exists():
            print(f"[Telegram] Missing screenshot for '{entry.get('Strategy Name')}' — skip.")
            continue

        sid     = entry.get("strategy_id", "")
        name    = entry.get("Strategy Name") or entry.get("strategy_name", "Strategy")
        overall = pnl_lookup.get(str(sid), _to_float(entry.get("pnl", 0)))
        emoji   = _sign_emoji(overall)

        caption = (
            f"{emoji} <b>{name}</b>\n"
            f"Overall PNL: <code>{_inr(overall)}</code>\n"
            f"{E_CLOCK} {entry.get('timestamp_ist', '')}"
        )
        _send_photo(filepath, caption)

    print("[Telegram] All done.")


if __name__ == "__main__":
    main()
