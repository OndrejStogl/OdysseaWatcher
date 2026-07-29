"""
Odyssea IMAX-70mm ticket watcher for Cinema City Flora (Prague).

How it works:
- For each of the next WINDOW_DAYS days, opens a FRESH browser page (no
  reuse between dates, this matters because the site is a single-page app
  and reusing a page can show stale content from the previous date if you
  read too early) pointed at the Odyssea booking page for that date.
- Waits until the page actually shows the requested date in its heading
  (e.g. "02.08.2026") before reading anything, retrying a few times if not.
- Extracts just the "PRAHA FLORA, OC FLORA" section of the page and checks
  for "IMAX-70mm" there specifically.
- Once a date is confirmed available, it is never re-checked again.
- Sends a Telegram message the moment a previously-unavailable date flips
  to available.

DEBUG_MODE:
Run once with DEBUG_MODE=true (see workflow "Run workflow" button) to sanity
check what the script sees for a handful of dates, without sending Telegram
messages or saving state.
"""

import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

FILM_SLUG = "odyssea/7268s2r"
CINEMA_HEADING = "PRAHA FLORA, OC FLORA"
WINDOW_DAYS = 21  # how many days ahead to keep an eye on
DEBUG_DAYS = 6  # how many days to check in debug mode (keep it quick)
MAX_ATTEMPTS = 3  # retries per date if the page hasn't updated yet
STATE_FILE = Path("state.json")
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def url_for_date(day: date) -> str:
    return (
        f"https://cinemacity.cz/films/{FILM_SLUG}/"
        f"#/buy-tickets-by-film?in-cinema=prague&at={day.isoformat()}"
        f"&for-movie=7268s2r&view-mode=list"
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


def flora_section_text(full_text: str) -> str | None:
    """Return just the chunk of text belonging to the Flora cinema, up to the
    next cinema heading (all-caps line starting with 'PRAHA' or similar), or
    None if the Flora heading isn't present at all."""
    idx = full_text.find(CINEMA_HEADING)
    if idx == -1:
        return None
    rest = full_text[idx + len(CINEMA_HEADING):]
    # Cut off at the next cinema heading, which looks like "PRAHA X, ..." or
    # another all-caps city name followed by a comma, on its own line.
    match = re.search(r"\n[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ ]{4,},", rest)
    if match:
        rest = rest[: match.start()]
    return rest


def get_page_for_date(browser, day: date, dismiss_cookies: bool = False):
    """Loads a fresh page for the given date, retrying until the page's own
    heading confirms it actually rendered that date. Returns page text, or
    None if it never confirmed after MAX_ATTEMPTS."""
    target_heading = day.strftime("%d.%m.%Y")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        page = browser.new_page()
        page.goto(url_for_date(day), wait_until="networkidle", timeout=60000)

        if dismiss_cookies and attempt == 1:
            dismiss_cookie_banner(page)

        page.wait_for_timeout(2500)
        text = page.inner_text("body")

        if target_heading in text:
            page.close()
            return text

        page.close()
        # not ready yet, small backoff then retry with a brand new page
        import time
        time.sleep(2)

    return None  # gave up after MAX_ATTEMPTS


def main() -> None:
    state = load_state()
    dates_state = state.get("dates", {})
    today = date.today()

    dates_state = {d: v for d, v in dates_state.items() if d >= today.isoformat()}

    num_days = DEBUG_DAYS if DEBUG_MODE else WINDOW_DAYS
    window = [today + timedelta(days=i) for i in range(num_days)]

    with sync_playwright() as p:
        browser = p.chromium.launch()

        newly_available = []
        debug_lines = []

        for i, day in enumerate(window):
            iso = day.isoformat()

            if not DEBUG_MODE and dates_state.get(iso) is True:
                continue  # already confirmed, skip re-checking

            text = get_page_for_date(browser, day, dismiss_cookies=(i == 0))

            if text is None:
                debug_lines.append(f"{iso}: COULD NOT CONFIRM PAGE LOADED (skipped)")
                continue

            section = flora_section_text(text)
            available = bool(section and "IMAX-70mm" in section)

            if DEBUG_MODE:
                debug_lines.append(f"{iso}: IMAX-70mm available = {available}")
                if i == 0:
                    Path("debug_page_text.txt").write_text(text, encoding="utf-8")
                continue

            if available and not dates_state.get(iso, False):
                newly_available.append(iso)

            dates_state[iso] = available

        browser.close()

    if DEBUG_MODE:
        print("\n".join(debug_lines))
        print("DEBUG MODE: no Telegram message sent, no state saved.")
        sys.exit(0)

    if newly_available and state.get("initialized"):
        msg = (
            "Odyssea IMAX-70mm Flora: nove otevrene terminy!\n"
            + "\n".join(newly_available)
            + f"\n\nhttps://cinemacity.cz/films/{FILM_SLUG}"
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
