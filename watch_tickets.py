"""
Odyssea IMAX-70mm ticket watcher for Cinema City Flora (Prague).

How it works:
- For each of the next WINDOW_DAYS days, opens the Cinema City "buy tickets
  by cinema" page for Flora with that specific date in the URL.
- Checks whether the text "IMAX-70mm" appears on the page for that date.
  That label only shows up once IMAX-70mm sessions are actually bookable
  for that day (confirmed from a real screenshot on 29.7.2026, where
  29/30/31 July showed it and later dates did not yet).
- Once a date is confirmed available, it is never re-checked again (saves
  a lot of run time), so over time this only spends effort on the small
  number of "not yet open" dates near the edge of the window.
- Sends a Telegram message the moment a previously-unavailable date flips
  to available.

DEBUG_MODE:
Run once with DEBUG_MODE=true (see workflow "Run workflow" button) to sanity
check what the script sees, without sending any Telegram messages.
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

CINEMA_ID = "1052"  # Praha Flora, OC FLORA
CINEMA_SLUG = "flora"
WINDOW_DAYS = 21  # how many days ahead to keep an eye on
STATE_FILE = Path("state.json")
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def url_for_date(day: date) -> str:
    return (
        f"https://cinemacity.cz/cinemas/{CINEMA_SLUG}/{CINEMA_ID}/"
        f"#/buy-tickets-by-cinema?in-cinema={CINEMA_ID}&at={day.isoformat()}&view-mode=list"
    )


def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    resp.raise_for_status()


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"initialized": False, "dates": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def dismiss_cookie_banner(page) -> None:
    for label in ["Odmítnout všechny soubory cookie", "Povolit všechny soubory cookie"]:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=3000)
            page.wait_for_timeout(500)
            return
        except Exception:
            pass


def check_date_available(page, day: date) -> str:
    """Returns the page text for manual inspection, caller checks for IMAX-70mm."""
    page.goto(url_for_date(day), wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2500)
    return page.inner_text("body")


def main() -> None:
    state = load_state()
    dates_state = state.get("dates", {})
    today = date.today()

    # Drop dates in the past, no need to keep them around
    dates_state = {d: v for d, v in dates_state.items() if d >= today.isoformat()}

    window = [today + timedelta(days=i) for i in range(WINDOW_DAYS)]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        # First load: dismiss cookie banner once
        page.goto(url_for_date(window[0]), wait_until="networkidle", timeout=60000)
        dismiss_cookie_banner(page)
        page.wait_for_timeout(1500)

        newly_available = []

        for day in window:
            iso = day.isoformat()

            if DEBUG_MODE:
                text = check_date_available(page, day)
                available = "IMAX-70mm" in text
                print(f"{iso}: IMAX-70mm available = {available}")
                if day == window[0]:
                    Path("debug_page_text.txt").write_text(text, encoding="utf-8")
                    page.screenshot(path="debug_screenshot.png", full_page=True)
                continue

            # Skip dates we already confirmed as available, no need to recheck
            if dates_state.get(iso) is True:
                continue

            text = check_date_available(page, day)
            available = "IMAX-70mm" in text

            if available and not dates_state.get(iso, False):
                newly_available.append(iso)

            dates_state[iso] = available

        browser.close()

    if DEBUG_MODE:
        print("DEBUG MODE: no Telegram message sent, no state saved.")
        sys.exit(0)

    if newly_available and state.get("initialized"):
        msg = (
            "Odyssea IMAX-70mm Flora: nove otevrene terminy!\n"
            + "\n".join(newly_available)
            + f"\n\nhttps://cinemacity.cz/films/odyssea/7268s2r"
        )
        send_telegram(msg)
    elif not state.get("initialized"):
        send_telegram(
            "Odyssea watcher: prvni spusteni, znam aktualni stav terminu. "
            "Od ted te budu upozornovat jen na nove otevrene dny."
        )

    state["initialized"] = True
    state["dates"] = dates_state
    save_state(state)


if __name__ == "__main__":
    main()
