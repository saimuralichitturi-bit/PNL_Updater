"""
tradetron_screenshots.py
─────────────────────────
Generates per-strategy summary images using only the stdlib + Pillow.
No browser / Playwright needed — works purely from pnl_latest.csv data.

Each "screenshot" is a clean PNG card showing:
  - Strategy name + status
  - PNL (Overall), PNL (Last Run), PNL (Live/Open)
  - Run counter
  - Timestamp

Reads:  pnl_latest.csv
Writes: screenshots/strategy_<id>_<ts>.png
        screenshots/manifest.json
"""

import csv
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

PNL_CSV         = "pnl_latest.csv"
SCREENSHOTS_DIR = Path("screenshots")
IST             = timezone(timedelta(hours=5, minutes=30))

# ── Try to import Pillow (in requirements.txt) ─────────────────────────────────
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("[Screenshots] Pillow not installed — will create minimal manifest only")


def _read_strategies() -> list[dict]:
    if not Path(PNL_CSV).exists():
        print(f"[Screenshots] {PNL_CSV} not found.")
        return []
    with open(PNL_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    valid = [r for r in rows if r.get("Strategy ID", "").strip()]
    print(f"[Screenshots] {len(valid)} strategies loaded from {PNL_CSV}")
    return valid


def _color(value: float):
    """Return RGB color based on positive/negative value."""
    if value > 0:
        return (34, 197, 94)    # green
    elif value < 0:
        return (239, 68, 68)    # red
    return (156, 163, 175)      # gray


def _inr(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}₹{v:,.2f}"


def _draw_card(strategy: dict, out_path: Path, timestamp: str) -> None:
    """Draw a clean PNL card as a PNG using Pillow."""
    W, H    = 800, 320
    BG      = (15, 23, 42)       # dark navy
    CARD    = (30, 41, 59)       # slightly lighter
    WHITE   = (248, 250, 252)
    GRAY    = (148, 163, 184)
    ACCENT  = (99, 102, 241)     # indigo

    name    = strategy.get("Strategy Name", "Unknown")
    status  = strategy.get("Status", "")
    broker  = strategy.get("Broker", "")
    overall = float(strategy.get("PNL (Overall)", 0) or 0)
    last    = float(strategy.get("PNL (Last Run)", 0) or 0)
    live    = float(strategy.get("PNL (Live/Open)", 0) or 0)
    runs    = strategy.get("Run Counter", "0")

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Try to load a font, fall back to default
    try:
        font_lg  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_md  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
        font_sm  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        font_xl  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
    except Exception:
        font_lg = font_md = font_sm = font_xl = ImageFont.load_default()

    # Card background
    draw.rounded_rectangle([20, 20, W - 20, H - 20], radius=16, fill=CARD)

    # Top accent bar
    draw.rounded_rectangle([20, 20, W - 20, 60], radius=16, fill=ACCENT)
    draw.rectangle([20, 44, W - 20, 60], fill=ACCENT)  # fill bottom corners of top bar

    # Strategy name in header
    draw.text((40, 30), name[:35], font=font_lg, fill=WHITE)

    # Status + broker badge (top right)
    badge_text = f"{status}  |  {broker}"
    draw.text((W - 40, 30), badge_text, font=font_sm, fill=WHITE, anchor="ra")

    # Overall PNL — large center
    draw.text((40, 80), "Overall PNL", font=font_sm, fill=GRAY)
    draw.text((40, 104), _inr(overall), font=font_xl, fill=_color(overall))

    # Divider
    draw.line([(40, 170), (W - 40, 170)], fill=(51, 65, 85), width=1)

    # Three columns: Last Run | Live/Open | Runs
    col_w = (W - 80) // 3
    cols = [
        ("Last Run PNL", _inr(last), _color(last)),
        ("Live / Open",  _inr(live), _color(live)),
        ("Total Runs",   str(runs),  WHITE),
    ]
    for i, (label, value, color) in enumerate(cols):
        x = 40 + i * col_w
        draw.text((x, 185), label, font=font_sm, fill=GRAY)
        draw.text((x, 208), value, font=font_md, fill=color)

    # Timestamp footer
    draw.text((40, H - 36), f"Snapshot: {timestamp}", font=font_sm, fill=GRAY)

    img.save(str(out_path), "PNG")


def main() -> None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    strategies = _read_strategies()
    now_ist    = datetime.now(IST)
    ts_file    = now_ist.strftime("%Y-%m-%d_%H-%M")
    ts_label   = now_ist.strftime("%d %b %Y  %I:%M %p IST")

    manifest = []

    for strategy in strategies:
        sid      = strategy.get("Strategy ID", "unknown").strip()
        name     = strategy.get("Strategy Name", f"strategy_{sid}")
        out_path = SCREENSHOTS_DIR / f"strategy_{sid}_{ts_file}.png"
        filepath = None

        if HAS_PILLOW:
            try:
                _draw_card(strategy, out_path, ts_label)
                filepath = str(out_path)
                print(f"[Screenshots] ✓ Card created: {out_path.name}")
            except Exception as exc:
                print(f"[Screenshots] ✗ Failed for '{name}': {exc}")
        else:
            print(f"[Screenshots] Skipping card for '{name}' — Pillow not available")

        manifest.append({
            "strategy_id":   sid,
            "Strategy Name": name,
            "pnl":           strategy.get("PNL (Overall)", "0"),
            "file":          filepath,
            "timestamp_ist": ts_label,
        })

    manifest_path = SCREENSHOTS_DIR / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    captured = sum(1 for m in manifest if m.get("file"))
    print(f"\n[Screenshots] Done — {captured}/{len(manifest)} cards created.")
    print(f"[Screenshots] Manifest → {manifest_path}")


if __name__ == "__main__":
    main()
