"""
weekly.py — Monday MVP scraper.

Scrapes the Yahoo Fantasy Basketball "Keys to Success" page to identify
the week's MVP players and cross-references them with your roster to
build the untouchables list.

Run this every Monday (or manually) before main.py.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LEAGUE_ID = os.environ.get("YAHOO_LEAGUE_ID", "11642")
KEYS_URL = f"https://basketball.fantasysports.yahoo.com/nba/{LEAGUE_ID}/keystosuccess"
YAHOO_LOGIN_URL = "https://login.yahoo.com/"
YAHOO_USERNAME = os.environ.get("YAHOO_USERNAME", "")
YAHOO_PASSWORD = os.environ.get("YAHOO_PASSWORD", "")

UNTOUCHABLES_FILE = os.path.join(os.path.dirname(__file__), "untouchables.json")

# Seconds to wait for elements
WAIT_TIMEOUT = 20

# Minimum MVP percent to include in untouchables
MVP_PERCENT_THRESHOLD = 0.0


# ---------------------------------------------------------------------------
# Driver setup
# ---------------------------------------------------------------------------


def _build_driver() -> webdriver.Chrome:
    """
    Create a headless Chrome WebDriver with anti-detection options.
    webdriver-manager automatically downloads the correct chromedriver.
    """
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    # Patch navigator.webdriver to evade detection
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    return driver


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


def _is_logged_in(driver: webdriver.Chrome) -> bool:
    """Check if the user is already authenticated by looking for avatar/username."""
    try:
        driver.find_element(By.CSS_SELECTOR, "#yucs-profile, .YucsUserMenu, [data-ylk*='acct']")
        return True
    except NoSuchElementException:
        pass

    # Also check URL — if we're on a fantasy page and not redirected to login, we're good
    if "login.yahoo.com" not in driver.current_url:
        if "basketball.fantasysports.yahoo.com" in driver.current_url:
            return True

    return False


def _yahoo_login(driver: webdriver.Chrome) -> None:
    """
    Walk through Yahoo's two-step login form.

    Step 1: Enter username, click Next.
    Step 2: Enter password, click Sign In.
    """
    if not YAHOO_USERNAME or not YAHOO_PASSWORD:
        raise ValueError(
            "YAHOO_USERNAME and YAHOO_PASSWORD must be set in .env for weekly.py to work."
        )

    print("[weekly] Navigating to Yahoo login …")
    driver.get(YAHOO_LOGIN_URL)
    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    # --- Step 1: Username ---
    try:
        username_field = wait.until(
            EC.presence_of_element_located((By.ID, "login-username"))
        )
    except TimeoutException:
        # Some regions use a different selector
        username_field = wait.until(
            EC.presence_of_element_located((By.NAME, "username"))
        )

    username_field.clear()
    username_field.send_keys(YAHOO_USERNAME)

    # Click "Next"
    next_btn = driver.find_element(By.ID, "login-signin")
    next_btn.click()
    time.sleep(2)

    # --- Step 2: Password ---
    try:
        password_field = wait.until(
            EC.presence_of_element_located((By.ID, "login-passwd"))
        )
    except TimeoutException:
        password_field = wait.until(
            EC.presence_of_element_located((By.NAME, "password"))
        )

    password_field.clear()
    password_field.send_keys(YAHOO_PASSWORD)

    # Click "Sign In"
    try:
        sign_in_btn = driver.find_element(By.ID, "login-signin")
    except NoSuchElementException:
        sign_in_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")

    sign_in_btn.click()
    print("[weekly] Login submitted — waiting for redirect …")
    time.sleep(4)

    # Check if we hit a CAPTCHA or 2FA prompt
    if "challenge" in driver.current_url or "verify" in driver.current_url:
        print(
            "[weekly] WARNING: Yahoo is asking for additional verification (CAPTCHA/2FA). "
            "Consider running weekly.py interactively (without --headless) to complete it."
        )

    if "login.yahoo.com" in driver.current_url:
        raise RuntimeError(
            "[weekly] Login appears to have failed — still on login page. "
            "Check YAHOO_USERNAME and YAHOO_PASSWORD."
        )

    print("[weekly] Login successful.")


# ---------------------------------------------------------------------------
# Keys to Success scraper
# ---------------------------------------------------------------------------


def _parse_mvp_table(driver: webdriver.Chrome) -> list[dict]:
    """
    Parse the Keys to Success / MVP table.

    Yahoo renders this as a table with columns that include the player name,
    roster status (Mine / Available / On Waivers), and a percent value.

    Returns a list of {name: str, roster_status: str, mvp_percent: float}.
    """
    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    # Wait for some table to appear on the page
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table, .Table")))
    except TimeoutException:
        print("[weekly] WARNING: Timed out waiting for table — page may have changed.")

    # Try multiple selectors for the MVP/Keys table
    table = None
    selectors = [
        "table.Table",
        "#keystosuccess-table",
        ".keystosuccess table",
        "table",
    ]
    for sel in selectors:
        tables = driver.find_elements(By.CSS_SELECTOR, sel)
        if tables:
            # Pick the table most likely to be the MVP one (has "%" in headers)
            for t in tables:
                header_text = t.text[:200].lower()
                if "%" in header_text or "player" in header_text:
                    table = t
                    break
            if table is None:
                table = tables[0]
            break

    if table is None:
        print("[weekly] Could not find MVP table on the page.")
        return []

    rows = table.find_elements(By.TAG_NAME, "tr")
    players: list[dict] = []

    for row in rows[1:]:  # skip header row
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) < 2:
            continue

        # Cell 0 or 1 is typically the player name
        # Cell with % is the MVP percent
        # Cell with Mine/Available is roster status

        row_text = [c.text.strip() for c in cells]

        # Find player name: first non-empty, non-numeric cell
        player_name = ""
        roster_status = "Unknown"
        mvp_percent = 0.0

        for i, cell_text in enumerate(row_text):
            if not cell_text:
                continue

            # Detect percent cell
            if "%" in cell_text:
                try:
                    mvp_percent = float(cell_text.replace("%", "").strip())
                except ValueError:
                    pass
                continue

            # Detect roster status
            if cell_text in ("Mine", "Available", "On Waivers", "Taken"):
                roster_status = cell_text
                continue

            # First substantial text is likely the player name
            if not player_name and len(cell_text) > 2 and not cell_text.replace(".", "").isdigit():
                player_name = cell_text

        if player_name:
            players.append(
                {
                    "name": player_name,
                    "roster_status": roster_status,
                    "mvp_percent": mvp_percent,
                }
            )

    return players


def _load_my_roster_names() -> set[str]:
    """
    Load roster player names from untouchables.json (previous run) or
    return empty set. Weekly.py doesn't import yahoo_client to keep
    the Selenium session independent.

    The caller (scrape_mvp) can pass in current roster names directly.
    """
    return set()


# ---------------------------------------------------------------------------
# Main scrape function
# ---------------------------------------------------------------------------


def scrape_mvp(my_roster_names: Optional[set[str]] = None) -> list[dict]:
    """
    Scrape the Yahoo Keys to Success page and return the list of
    untouchables (players on my roster who appear in the MVP table).

    Parameters
    ----------
    my_roster_names : set of player name strings from get_my_roster().
                      If None, ALL players from the MVP table are returned
                      (useful for inspection).

    Returns
    -------
    List of {name: str, mvp_percent: float} dicts, sorted by mvp_percent desc.
    """
    driver = _build_driver()
    untouchables: list[dict] = []

    try:
        # Navigate to the target page first to check login state
        print(f"[weekly] Navigating to {KEYS_URL} …")
        driver.get(KEYS_URL)
        time.sleep(3)

        # If redirected to login, authenticate
        if "login.yahoo.com" in driver.current_url or not _is_logged_in(driver):
            _yahoo_login(driver)
            print(f"[weekly] Navigating to {KEYS_URL} after login …")
            driver.get(KEYS_URL)
            time.sleep(4)

        # Wait for page content
        WebDriverWait(driver, WAIT_TIMEOUT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(2)

        # Attempt to click the H2H tab if present
        try:
            h2h_tabs = driver.find_elements(
                By.XPATH,
                "//*[contains(text(), 'H2H') or contains(text(), 'Head to Head')]"
            )
            if h2h_tabs:
                h2h_tabs[0].click()
                time.sleep(2)
                print("[weekly] Clicked H2H tab.")
        except Exception:
            pass  # Not all leagues have tabs

        # Parse the table
        all_players = _parse_mvp_table(driver)
        print(f"[weekly] Parsed {len(all_players)} players from MVP table.")

        if my_roster_names is None:
            # Return everyone above threshold
            untouchables = [
                {"name": p["name"], "mvp_percent": p["mvp_percent"]}
                for p in all_players
                if p["mvp_percent"] >= MVP_PERCENT_THRESHOLD
            ]
        else:
            # Only players on my roster
            for p in all_players:
                # Fuzzy name match: check if any roster name is contained
                # in the scraped name or vice versa
                for roster_name in my_roster_names:
                    name_scraped = p["name"].lower().strip()
                    name_roster = roster_name.lower().strip()

                    # Match on last name + first initial or full name
                    last_scraped = name_scraped.split()[-1] if name_scraped else ""
                    last_roster = name_roster.split()[-1] if name_roster else ""

                    if (
                        name_scraped == name_roster
                        or last_scraped == last_roster
                        or name_scraped in name_roster
                        or name_roster in name_scraped
                    ):
                        untouchables.append(
                            {"name": roster_name, "mvp_percent": p["mvp_percent"]}
                        )
                        break

        # Deduplicate
        seen: set[str] = set()
        deduped: list[dict] = []
        for item in untouchables:
            if item["name"] not in seen:
                seen.add(item["name"])
                deduped.append(item)

        untouchables = sorted(deduped, key=lambda x: -x["mvp_percent"])
        print(f"[weekly] Untouchables identified: {[u['name'] for u in untouchables]}")

    except WebDriverException as exc:
        print(f"[weekly] WebDriver error: {exc}")
    finally:
        driver.quit()

    return untouchables


def save_untouchables(untouchables: list[dict]) -> None:
    """Persist untouchables list to untouchables.json."""
    payload = {
        "updated": datetime.utcnow().isoformat() + "Z",
        "untouchables": untouchables,
    }
    with open(UNTOUCHABLES_FILE, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"[weekly] Saved {len(untouchables)} untouchables to {UNTOUCHABLES_FILE}.")


def load_untouchables() -> dict[str, float]:
    """
    Load untouchables.json and return {name: mvp_percent} dict.
    Returns empty dict if file does not exist.
    """
    if not os.path.exists(UNTOUCHABLES_FILE):
        print("[weekly] untouchables.json not found — returning empty dict.")
        return {}

    with open(UNTOUCHABLES_FILE) as fh:
        data = json.load(fh)

    result: dict[str, float] = {}
    for entry in data.get("untouchables", []):
        result[entry["name"]] = entry.get("mvp_percent", 0.0)

    print(f"[weekly] Loaded {len(result)} untouchables from file.")
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scrape Yahoo Fantasy MVP table.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Show all players from table, not just those on my roster.",
    )
    args = parser.parse_args()

    roster_names = None if args.all else set()  # empty set = no matches unless populated
    # To cross-reference with your actual roster, import and call yahoo_client here:
    # from yahoo_client import YahooFantasyClient
    # client = YahooFantasyClient()
    # roster_names = {p['name'] for p in client.get_my_roster()}

    results = scrape_mvp(my_roster_names=roster_names)
    save_untouchables(results)

    print("\n=== UNTOUCHABLES ===")
    for u in results:
        print(f"  {u['name']:<30} MVP%={u['mvp_percent']:.1f}%")
