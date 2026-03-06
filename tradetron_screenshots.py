"""
tradetron_screenshots.py  — PNL table image generator
──────────────────────────────────────────────────────
Generates ONE consolidated PNG table (dark theme, no emojis).
Positive PNL values → green,  negative → red.

Reads:  pnl_latest.csv
Writes: screenshots/pnl_table.png
        pnl_table.json   ← consumed by tradetron_telegram_notifier.py
"""

import csv
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

PNL_CSV         = "pnl_latest.csv"
TABLE_JSON      = "pnl_table.json"
SCREENSHOTS_DIR = Path("screenshots")
IST             = timezone(timedelta(hours=5, minutes=30))

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("[Table] Pillow not installed — skipping image generation")


# ── Helpers ────────────────────────────────────────────────────────────────────
def _to_float(raw) -> float:
    try:
        return float(str(raw).replace("₹", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _fmt(v: float) -> str:
    """Format PNL as +52,919 or -174,281 (no currency symbol — font safe)."""
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:,.0f}"


def _pnl_color(v: float) -> tuple:
    if v > 0:  return (52,  211, 153)   # green
    if v < 0:  return (248, 113, 113)   # red
    return          (100, 116, 139)     # neutral gray


def _load_fonts():
    try:
        b = "/usr/share/fonts/truetype/dejavu/"
        return {
            "title":  ImageFont.truetype(b + "DejaVuSans-Bold.ttf",    22),
            "hdr":    ImageFont.truetype(b + "DejaVuSans-Bold.ttf",    14),
            "body":   ImageFont.truetype(b + "DejaVuSans.ttf",         14),
            "mono":   ImageFont.truetype(b + "DejaVuSansMono.ttf",     13),
            "mono_b": ImageFont.truetype(b + "DejaVuSansMono-Bold.ttf",14),
        }
    except Exception:
        d = ImageFont.load_default()
        return {"title": d, "hdr": d, "body": d, "mono": d, "mono_b": d}


def _truncate(draw, text: str, font, max_px: int) -> str:
    orig = text
    try:
        while draw.textlength(text, font=font) > max_px and len(text) > 3:
            text = text[:-1]
        if len(text) < len(orig):
            text = text.rstrip()[:-1] + "\u2026"   # ellipsis
    except Exception:
        if len(orig) > 30:
            text = orig[:28] + "\u2026"
    return text


def _read_strategies() -> list[dict]:
    if not Path(PNL_CSV).exists():
        print(f"[Table] {PNL_CSV} not found.")
        return []
    with open(PNL_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    valid = [r for r in rows if r.get("Strategy ID", "").strip()]
    print(f"[Table] {len(valid)} strategies loaded from {PNL_CSV}")
    return valid


# ── Image generator ────────────────────────────────────────────────────────────
def generate_table_image(rows: list[dict], out_path: Path, ts_label: str) -> None:
    # Layout
    PAD     = 24
    ROW_H   = 36
    HDR_H   = 40
    TITLE_H = 52
    FOOT_H  = 44
    GAP     = 18

    # Column widths (px)
    C_NUM  = 30
    C_NAME = max(200, min(320, max(len(r.get("Strategy Name", "")) for r in rows) * 9 + 24))
    C_CTR  = 44
    C_PNL  = 150

    INNER_W = C_NUM + GAP + C_NAME + GAP + C_CTR + GAP + C_PNL + GAP + C_PNL
    W = INNER_W + 2 * PAD
    H = TITLE_H + HDR_H + len(rows) * ROW_H + FOOT_H

    # Colors
    BG_MAIN  = (10,  14,  20)
    BG_TITLE = (21,  28,  38)
    BG_HDR   = (26,  35,  50)
    BG_ODD   = (16,  22,  32)
    BG_EVEN  = (20,  27,  40)
    BG_FOOT  = (26,  35,  50)
    C_WHITE  = (248, 250, 252)
    C_GRAY   = (100, 116, 139)
    C_DIV    = (40,  52,  68)

    img  = Image.new("RGB", (W, H), BG_MAIN)
    draw = ImageDraw.Draw(img)
    F    = _load_fonts()

    # Column x anchors (left edge, except PNL right-anchor = x + C_PNL)
    x_num  = PAD
    x_name = x_num  + C_NUM  + GAP
    x_ctr  = x_name + C_NAME + GAP
    x_pnl1 = x_ctr  + C_CTR  + GAP
    x_pnl2 = x_pnl1 + C_PNL  + GAP

    # ── Title bar ──────────────────────────────────────────────────────────
    draw.rectangle([0, 0, W, TITLE_H], fill=BG_TITLE)
    draw.text(
        (PAD, TITLE_H // 2),
        f"PNL Data  —  {ts_label}",
        font=F["title"], fill=C_WHITE, anchor="lm",
    )
    draw.line([(0, TITLE_H), (W, TITLE_H)], fill=C_DIV, width=1)

    # ── Column headers ─────────────────────────────────────────────────────
    y  = TITLE_H
    cy = y + HDR_H // 2
    draw.rectangle([0, y, W, y + HDR_H], fill=BG_HDR)
    draw.text((x_num + C_NUM // 2,    cy), "#",         font=F["hdr"], fill=C_GRAY, anchor="mm")
    draw.text((x_name,                cy), "Strategy",  font=F["hdr"], fill=C_GRAY, anchor="lm")
    draw.text((x_ctr + C_CTR // 2,    cy), "Ctr",       font=F["hdr"], fill=C_GRAY, anchor="mm")
    draw.text((x_pnl1 + C_PNL,        cy), "Curr Run",  font=F["hdr"], fill=C_GRAY, anchor="rm")
    draw.text((x_pnl2 + C_PNL,        cy), "Overall",   font=F["hdr"], fill=C_GRAY, anchor="rm")
    draw.line([(0, y + HDR_H), (W, y + HDR_H)], fill=C_DIV, width=1)
    y += HDR_H

    # ── Data rows ──────────────────────────────────────────────────────────
    tot_curr = tot_overall = 0.0

    for i, r in enumerate(rows):
        draw.rectangle([0, y, W, y + ROW_H], fill=BG_ODD if i % 2 == 0 else BG_EVEN)
        cy = y + ROW_H // 2

        name    = r.get("Strategy Name", "—")
        ctr_raw = r.get("Latest Counter") or r.get("Run Counter", "")
        curr    = _to_float(r.get("Counter PNL") or r.get("PNL (Last Run)", 0))
        overall = _to_float(r.get("PNL (Overall)", 0))

        try:
            ctr_str = str(int(float(str(ctr_raw)))) if str(ctr_raw).strip() else "—"
        except Exception:
            ctr_str = "—"

        name_disp = _truncate(draw, name, F["body"], C_NAME - 8)

        draw.text((x_num + C_NUM // 2, cy), str(i + 1),   font=F["body"],   fill=C_GRAY,           anchor="mm")
        draw.text((x_name,             cy), name_disp,     font=F["body"],   fill=C_WHITE,           anchor="lm")
        draw.text((x_ctr + C_CTR // 2, cy), ctr_str,      font=F["body"],   fill=C_GRAY,            anchor="mm")
        draw.text((x_pnl1 + C_PNL,    cy), _fmt(curr),    font=F["mono"],   fill=_pnl_color(curr),  anchor="rm")
        draw.text((x_pnl2 + C_PNL,    cy), _fmt(overall), font=F["mono_b"], fill=_pnl_color(overall), anchor="rm")

        tot_curr    += curr
        tot_overall += overall
        y += ROW_H

    # ── Totals row ─────────────────────────────────────────────────────────
    draw.line([(PAD, y), (W - PAD, y)], fill=C_DIV, width=1)
    draw.rectangle([0, y, W, y + FOOT_H], fill=BG_FOOT)
    cy = y + FOOT_H // 2
    draw.text((x_name,          cy), "TOTAL",              font=F["hdr"],    fill=C_GRAY,               anchor="lm")
    draw.text((x_pnl1 + C_PNL, cy), _fmt(tot_curr),       font=F["mono_b"], fill=_pnl_color(tot_curr),   anchor="rm")
    draw.text((x_pnl2 + C_PNL, cy), _fmt(tot_overall),    font=F["mono_b"], fill=_pnl_color(tot_overall), anchor="rm")

    img.save(str(out_path), "PNG")
    print(f"[Table] Image saved: {out_path}")


# ── Entry point ────────────────────────────────────────────────────────────────
def main() -> None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    rows     = _read_strategies()
    now_ist  = datetime.now(IST)
    ts       = now_ist.strftime("%d %b %Y  %I:%M %p IST")
    out_path = SCREENSHOTS_DIR / "pnl_table.png"
    img_path = None

    if not rows:
        print("[Table] No data — skipping image generation.")
    elif HAS_PILLOW:
        try:
            generate_table_image(rows, out_path, ts)
            img_path = str(out_path)
        except Exception as e:
            print(f"[Table] Image generation failed: {e}")
    else:
        print("[Table] Pillow not available — skipping image.")

    with open(TABLE_JSON, "w", encoding="utf-8") as f:
        json.dump({"image_path": img_path, "timestamp": ts, "rows": len(rows)},
                  f, ensure_ascii=False, indent=2)

    print(f"[Table] Metadata written to {TABLE_JSON}")


if __name__ == "__main__":
    main()
