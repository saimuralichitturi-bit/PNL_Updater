"""
tradetron_screenshots.py
─────────────────────────
Full-page screenshots of every deployed Tradetron strategy.

Reads:
  TRADETRON_SESSION env var   ← session_json from tradetron_auth.py
  pnl_latest.csv              ← written by tradetron_scraper.py

Writes:
  screenshots/strategy_<id>_<YYYY-MM-DD_HH-MM>.png   ← one per strategy
  screenshots/manifest.json                           ← consumed by telegram notifier

Dependencies: playwright (installed via requirements.txt + playwright install chromium)
"""

import os
import json
import csv
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

# ── Config ─────────────────────────────────────────────────────────────────────
SESSION_ENV     = os.environ.get("TRADETRON_SESSION", "")
PNL_CSV         = "pnl_latest.csv"
SCREENSHOTS_DIR = Path("screenshots")
BASE_URL        = "https://tradetron.tech"
IST             = timezone(timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


# ── Load session ───────────────────────────────────────────────────────────────
def _load_session() -> dict:
    if not SESSION_ENV:
        raise RuntimeError("[Screenshots] TRADETRON_SESSION env var is not set")
    return json.loads(SESSION_ENV)


# ── Read strategies from CSV ───────────────────────────────────────────────────
def _read_strategies() -> list[dict]:
    """
    Read strategy list from pnl_latest.csv.
    Columns (from tradetron_scraper.py -> parse_strategy):
      Strategy ID, Strategy Name, Status, Deployment Type, Exchange, Broker,
      Capital Required, PNL (Last Run), PNL (Overall), PNL (Live/Open),
      Run Counter, Completed Runs, Currency, Deployment Date, Creator, Snapshot Time
    """
    if not Path(PNL_CSV).exists():
        print(f"[Screenshots] {PNL_CSV} not found — nothing to screenshot.")
        return []

    with open(PNL_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Filter out rows with no Strategy ID (can happen with empty CSV)
    valid = [r for r in rows if r.get("Strategy ID", "").strip()]
    print(f"[Screenshots] {len(valid)} strategies to capture (from {PNL_CSV})")
    return valid


# ── Screenshot one strategy page ──────────────────────────────────────────────
async def _screenshot_strategy(page, strategy: dict, timestamp_str: str, out_dir: Path) -> str | None:
    sid  = strategy.get("Strategy ID", "unknown").strip()
    name = strategy.get("Strategy Name", f"strategy_{sid}")
    url  = f"{BASE_URL}/deployed-strategy/{sid}"
    path = out_dir / f"strategy_{sid}_{timestamp_str}.png"

    try:
        print(f"[Screenshots] Capturing '{name}' (id={sid}) ...")
        await page.goto(url, wait_until="networkidle", timeout=45_000)
        await page.wait_for_selector("body", state="visible", timeout=15_000)
        await page.wait_for_timeout(2_500)   # let charts/tables fully render
        await page.screenshot(path=str(path), full_page=True)
        print(f"[Screenshots]   ✓ Saved: {path}")
        return str(path)
    except Exception as exc:
        print(f"[Screenshots]   ✗ FAILED for '{name}': {exc}")
        return None


# ── Main async screenshot loop ─────────────────────────────────────────────────
async def take_screenshots(strategies: list[dict], session_data: dict) -> list[dict]:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    now_ist       = datetime.now(IST)
    timestamp_str = now_ist.strftime("%Y-%m-%d_%H-%M")
    time_label    = now_ist.strftime("%d %b %Y  %I:%M %p IST")

    manifest = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
        )

        # Inject session cookies
        await context.add_cookies([
            {"name": name, "value": value, "domain": "tradetron.tech", "path": "/"}
            for name, value in session_data["cookies"].items()
        ])

        page = await context.new_page()

        for strategy in strategies:
            sid      = strategy.get("Strategy ID", "unknown").strip()
            name     = strategy.get("Strategy Name", f"strategy_{sid}")
            filepath = await _screenshot_strategy(page, strategy, timestamp_str, SCREENSHOTS_DIR)

            manifest.append({
                "strategy_id":   sid,
                "Strategy Name": name,
                "pnl":           strategy.get("PNL (Overall)", "0"),
                "file":          filepath,    # None if capture failed
                "timestamp_ist": time_label,
            })

        await browser.close()

    return manifest


# ── Entry point ────────────────────────────────────────────────────────────────
def main() -> None:
    session_data = _load_session()
    strategies   = _read_strategies()

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    if not strategies:
        print("[Screenshots] No strategies — writing empty manifest.")
        manifest = []
    else:
        manifest = asyncio.run(take_screenshots(strategies, session_data))

    # Write manifest for telegram notifier
    manifest_path = SCREENSHOTS_DIR / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    captured = sum(1 for m in manifest if m.get("file"))
    print(f"\n[Screenshots] Done — {captured}/{len(manifest)} screenshots captured.")
    print(f"[Screenshots] Manifest → {manifest_path}")


if __name__ == "__main__":
    main()
