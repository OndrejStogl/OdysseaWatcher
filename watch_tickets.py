"""
Odyssea IMAX ticket watcher for Cinema City Flora (Prague).

What it does:
- Loads the film page in a headless browser (so JavaScript-rendered
  showtime/date pickers are visible, not just raw HTML).
- Pulls out anything that looks like a date near the "IMAX" part of the page.
- Compares it to the list saved from the previous run (state.json).
- If a NEW date shows up, sends you a Telegram message.

DEBUG_MODE:
Run this once with DEBUG_MODE = True first (see workflow file) to check
that the scraping is actually finding the right thing. It will save a
screenshot and the extracted text as an artifact you can download and look at.
Once you've confirmed it, switch to DEBUG_MODE = False for normal operation.
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = "https://www.cinemacity.cz/films/odyssea/7268s2r"
STATE_FILE = Path("state.json")
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Matches Czech dates like "16.7.", "16. 7. 2026", "16.7.2026"
DATE_PATTERN = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.(?:\s?\d{2,4})?\b")


def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    resp.raise_for_status()


def load_previous_dates() -> list[str]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text()).get("dates", [])
    return []


def save_dates(dates: list[str]) -> None:
    STATE_FILE.write_text(json.dumps({"dates": dates}, ensure_ascii=False, indent=2))


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)  # let any lazy JS finish rendering

        if DEBUG_MODE:
            page.screenshot(path="debug_screenshot.png", full_page=True)

        full_text = page.inner_text("body")
        browser.close()

    if DEBUG_MODE:
        Path("debug_page_text.txt").write_text(full_text, encoding="utf-8")
        print("DEBUG MODE: saved debug_screenshot.png and debug_page_text.txt")
        print("---- first 2000 chars of page text ----")
        print(full_text[:2000])
        sys.exit(0)

    found_dates = sorted(set(DATE_PATTERN.findall(full_text)))
    previous_dates = load_previous_dates()

    new_dates = [d for d in found_dates if d not in previous_dates]

    print(f"Found dates: {found_dates}")
    print(f"Previously known: {previous_dates}")
    print(f"New: {new_dates}")

    if new_dates and previous_dates:
        # Only alert if this isn't the very first run (empty previous state)
        msg = (
            "Odyssea IMAX Flora: nove terminy!\n"
            + "\n".join(new_dates)
            + f"\n\n{URL}"
        )
        send_telegram(msg)
    elif not previous_dates:
        send_telegram(
            "Odyssea watcher: prvni spusteni, ulozil jsem aktualni terminy. "
            "Od dalsiho behu te budu upozornovat na nove."
        )

    save_dates(found_dates)


if __name__ == "__main__":
    main()
