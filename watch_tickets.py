"""
Odyssea IMAX-70mm ticket watcher for Cinema City Flora (Prague).

IMPORTANT DESIGN NOTE:
GitHub Actions cron schedules are "best effort" and can be delayed by
hours, especially outside of the exact minute requested. To get closer
to real-time checking, this script does NOT rely on being re-triggered
every few minutes by cron. Instead, once started, it loops internally
for up to MAX_RUNTIME_SECONDS (just under GitHub's 6-hour hard job
limit), checking every POLL_INTERVAL_SECONDS. The workflow's cron only
needs to fire a handful of times a day to keep restarting this loop.

How the availability check works:
- For each of the next WINDOW_DAYS days, opens a FRESH browser page
  pointed at the Odyssea booking page for that date.
- Waits until the page actually shows the requested date in its heading
  (e.g. "02.08.2026") before reading anything, retrying a few times if not.
- Extracts just the "PRAHA FLORA, OC FLORA" section of the page and checks
  for "IMAX-70mm" there specifically.
- Once a date is confirmed available, it is never re-checked again.
- Sends a Telegram message the moment a previously-unavailable date flips
  to available, and immediately commits+pushes the updated state so a
  restart (or the 6-hour cutoff) never re-sends the same alert twice.

DEBUG_MODE:
Run once with DEBUG_MODE=true (see workflow "Run workflow" button) to do a
single quick pass over a few days, without sending Telegram messages,
looping, or committing anything.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

FILM_SLUG = "odyssea/7268s2r"
CINEMA_HEADING = "PRAHA FLORA, OC FLORA"
WINDOW_DAYS = 21  # how many days ahead to keep an eye on
DEBUG_DAYS = 6  # how many days to check in debug mode (keep it quick)
MAX_ATTEMPTS = 3  # retries per date if the page hasn't updated yet
POLL_INTERVAL_SECONDS = 3 * 60  # how often to re-check while looping
MAX_RUNTIME_SECONDS = int(5.7 * 3600)  # stay comfortably under the 6h hard job limit
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
    if not resp.ok:
        print(f"Telegram error response: {resp.text}")
    resp.raise_for_status()


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"initialized": False, "dates": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def git_commit_and_push(message: str) -> None:
    try:
        subprocess.run(["git", "add", "state.json"], check=True)
        result = subprocess.run(["git", "commit", "-m", message], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"git commit: nothing to commit or failed: {result.stdout} {result.stderr}")
            return
        subprocess.run(["git", "push"], check=True)
        print("Pushed updated state.json")
    except Exception as e:
        print(f"git commit/push failed (continuing anyway): {e}")


def dismiss_cookie_banner(page) -> None:
    for label in ["Odmítnout všechny soubory cookie", "Povolit všechny soubory cookie"]:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=3000)
            page.wait_for_timeout(500)
            return
        except Exception:
            pass


def flora_section_text(full_text: str) -> str | None:
    idx = full_text.find(CINEMA_HEADING)
    if idx == -1:
        return None
    rest = full_text[idx + len(CINEMA_HEADING):]
    match = re.search(r"\n[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ ]{4,},", rest)
    if match:
        rest = rest[: match.start()]
    return rest


def get_page_for_date(browser, day: date, dismiss_cookies: bool = False):
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
        time.sleep(2)

    return None


def check_window(browser, dates_state: dict, window: list[date], dismiss_cookies_once: bool) -> list[str]:
    """Checks all not-yet-available dates in window, updates dates_state in
    place, returns list of dates that newly became available this pass."""
    newly_available = []
    first = True
    for day in window:
        iso = day.isoformat()
        if dates_state.get(iso) is True:
            continue

        text = get_page_for_date(browser, day, dismiss_cookies=(dismiss_cookies_once and first))
        first = False

        if text is None:
            print(f"{iso}: could not confirm page loaded, skipping this pass")
            continue

        section = flora_section_text(text)
        available = bool(section and "IMAX-70mm" in section)

        if available and not dates_state.get(iso, False):
            newly_available.append(iso)

        dates_state[iso] = available

    return newly_available


def run_debug(browser) -> None:
    today = date.today()
    window = [today + timedelta(days=i) for i in range(DEBUG_DAYS)]
    lines = []
    for i, day in enumerate(window):
        text = get_page_for_date(browser, day, dismiss_cookies=(i == 0))
        if text is None:
            lines.append(f"{day.isoformat()}: COULD NOT CONFIRM PAGE LOADED")
            continue
        section = flora_section_text(text)
        available = bool(section and "IMAX-70mm" in section)
        lines.append(f"{day.isoformat()}: IMAX-70mm available = {available}")
        if i == 0:
            Path("debug_page_text.txt").write_text(text, encoding="utf-8")
    print("\n".join(lines))
    print("DEBUG MODE: no Telegram message sent, no state saved.")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()

        if DEBUG_MODE:
            run_debug(browser)
            browser.close()
            sys.exit(0)

        subprocess.run(["git", "config", "user.name", "github-actions[bot]"])
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"])

        state = load_state()
        start_time = time.time()
        first_cycle = True

        while time.time() - start_time < MAX_RUNTIME_SECONDS:
            today = date.today()
            dates_state = {d: v for d, v in state.get("dates", {}).items() if d >= today.isoformat()}
            window = [today + timedelta(days=i) for i in range(WINDOW_DAYS)]

            newly_available = check_window(browser, dates_state, window, dismiss_cookies_once=first_cycle)
            state["dates"] = dates_state

            if newly_available and state.get("initialized"):
                msg = (
                    "Odyssea IMAX-70mm Flora: nove otevrene terminy!\n"
                    + "\n".join(newly_available)
                    + f"\n\nhttps://cinemacity.cz/films/{FILM_SLUG}"
                )
                send_telegram(msg)
                save_state(state)
                git_commit_and_push("Update ticket state (new dates found)")
            elif not state.get("initialized"):
                send_telegram(
                    "Odyssea watcher: prvni spusteni, znam aktualni stav terminu. "
                    "Od ted te budu upozornovat jen na nove otevrene dny."
                )
                state["initialized"] = True
                save_state(state)
                git_commit_and_push("Initialize ticket state")

            first_cycle = False
            print(f"Cycle done at {time.strftime('%H:%M:%S')}, sleeping {POLL_INTERVAL_SECONDS}s")
            time.sleep(POLL_INTERVAL_SECONDS)

        browser.close()
        print("Reached max runtime for this job, exiting so the next scheduled run can take over.")


if __name__ == "__main__":
    main()
