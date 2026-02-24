"""
tradetron_screenshots.py — Full-page screenshots of every Tradetron strategy
─────────────────────────────────────────────────────────────────────────────
Reads:   TRADETRON_SESSION env var  (session_json from tradetron_auth.py)
         pnl_latest.csv             (written by tradetron_scraper.py)

Writes:  screenshots/strategy_<id>_<YYYY-MM-DD_HH-MM>.png  — one per strategy
         screenshots/manifest.json  — consumed by tradetron_telegram_notifier.py

Dependencies: playwright (installed inline in the workflow)
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

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)


def _load_session() -> dict:
    if not SESSION_ENV:
        raise RuntimeError("TRADETRON_SESSION env var is not set")
    return json.loads(SESSION_ENV)


def _read_strategies() -> list[dict]:
    """
    Return list of strategies from pnl_latest.csv.
    Columns come from tradetron_scraper.py -> parse_strategy():
      Strategy ID, Strategy Name, Status, Deployment Type, Exchange, Broker,
      Capital Required, PNL (Last Run), PNL (Overall), PNL (Live/Open),
      Run Counter, Completed Runs, Currency, Deployment Date, Creator, Snapshot Time
    """
    if not Path(PNL_CSV).exists():
        print(f"[Screenshots] {PNL_CSV} not found — no strategies to screenshot.")
        return []
    with open(PNL_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


async def _screenshot_strategy(
    page,
    strategy: dict,
    timestamp_str: str,
    out_dir: Path,
) -> str | None:
    """Navigate to the strategy page and take a full-page screenshot."""
    # Column names match tradetron_scraper.py -> parse_strategy()
    sid  = strategy.get("Strategy ID", "unknown")
    name = strategy.get("Strategy Name", f"strategy_{sid}")

    url = f"{BASE_URL}/deployed-strategy/{sid}"
    filename = f"strategy_{sid}_{timestamp_str}.png"
    filepath = out_dir / filename

    try:
        print(f"[Screenshots] Capturing '{name}' ({sid}) …")
        await page.goto(url, wait_until="networkidle", timeout=45_000)
        # Wait for the main content container to be visible
        await page.wait_for_selector("body", state="visible", timeout=15_000)
        await page.wait_for_timeout(2_000)   # let charts/tables render fully
        await page.screenshot(path=str(filepath), full_page=True)
        print(f"[Screenshots]  -> saved: {filepath}")
        return str(filepath)
    except Exception as exc:
        print(f"[Screenshots]  -> FAILED for '{name}': {exc}")
        return None


async def take_screenshots(strategies: list[dict], session_data: dict) -> list[dict]:
    """Launch Chromium, inject cookies, screenshot every strategy."""
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    now_ist      = datetime.now(IST)
    timestamp_str = now_ist.strftime("%Y-%m-%d_%H-%M")

    manifest = []   # [{strategy_id, strategy_name, file, timestamp_ist}]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
        )

        # Inject session cookies
        cookies_to_set = [
            {"name": name, "value": value, "domain": "tradetron.tech", "path": "/"}
            for name, value in session_data["cookies"].items()
        ]
        await context.add_cookies(cookies_to_set)

        page = await context.new_page()

        for strategy in strategies:
            filepath = await _screenshot_strategy(page, strategy, timestamp_str, SCREENSHOTS_DIR)
            # Column names match tradetron_scraper.py -> parse_strategy()
            sid  = strategy.get("Strategy ID", "unknown")
            name = strategy.get("Strategy Name", f"strategy_{sid}")
            manifest.append({
                "strategy_id":   sid,
                "Strategy Name": name,
                "pnl":           strategy.get("PNL (Overall)", ""),
                "file":          filepath,          # None if failed
                "timestamp_ist": now_ist.strftime("%d %b %Y  %I:%M %p IST"),
            })

        await browser.close()

    return manifest


def main() -> None:
    session_data = _load_session()
    strategies   = _read_strategies()

    if not strategies:
        print("[Screenshots] No strategies found — writing empty manifest.")
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        manifest = []
    else:
        print(f"[Screenshots] Found {len(strategies)} strategy/ies to capture.")
        manifest = asyncio.run(take_screenshots(strategies, session_data))

    manifest_path = SCREENSHOTS_DIR / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"[Screenshots] Manifest written → {manifest_path}")
    print(f"[Screenshots] Done. {sum(1 for m in manifest if m['file'])} / {len(manifest)} screenshots captured.")


if __name__ == "__main__":
    main()
