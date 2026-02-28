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
import re
import sys
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
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "yahoo_cookies.json")

# Seconds to wait for elements
WAIT_TIMEOUT = 20

# Minimum MVP percent to include in untouchables
MVP_PERCENT_THRESHOLD = 0.0


# ---------------------------------------------------------------------------
# Driver setup
# ---------------------------------------------------------------------------


def _build_driver(headless: bool = True) -> webdriver.Chrome:
    """
    Create a Chrome WebDriver with anti-detection options.
    webdriver-manager automatically downloads the correct chromedriver.
    """
    options = Options()
    if headless:
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


def _save_cookies(driver: webdriver.Chrome) -> None:
    """Save browser cookies to file for reuse across sessions."""
    cookies = driver.get_cookies()
    with open(COOKIES_FILE, "w") as fh:
        json.dump(cookies, fh)
    os.chmod(COOKIES_FILE, 0o600)
    print(f"[weekly] Saved {len(cookies)} cookies to {COOKIES_FILE}.")


def _load_cookies(driver: webdriver.Chrome) -> bool:
    """Load cookies from file into the driver. Returns True if cookies were loaded."""
    if not os.path.exists(COOKIES_FILE):
        return False

    try:
        with open(COOKIES_FILE) as fh:
            cookies = json.load(fh)
    except (json.JSONDecodeError, IOError):
        return False

    if not cookies:
        return False

    # Must be on a Yahoo domain first for cookies to apply.
    # Use a minimal page to avoid heavy yahoo.com loading.
    driver.get("https://basketball.fantasysports.yahoo.com/robots.txt")
    time.sleep(2)

    loaded = 0
    for cookie in cookies:
        # Selenium doesn't accept expiry as float
        if "expiry" in cookie:
            cookie["expiry"] = int(cookie["expiry"])
        try:
            driver.add_cookie(cookie)
            loaded += 1
        except Exception:
            pass

    print(f"[weekly] Loaded {loaded}/{len(cookies)} cookies from {COOKIES_FILE}.")
    return True


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
            # Yahoo may show error page instead of redirecting to login
            try:
                driver.find_element(By.XPATH, "//*[contains(text(), 'You are not in this league')]")
                return False
            except NoSuchElementException:
                pass
            # Check for "Sign in" link in nav — indicates not logged in
            try:
                driver.find_element(By.XPATH, "//a[contains(text(), 'Sign in')]")
                return False
            except NoSuchElementException:
                return True

    return False


def _click_button_with_text(driver: webdriver.Chrome, text: str, timeout: int = 10) -> bool:
    """Click the first visible button whose text contains *text* (case-insensitive)."""
    try:
        WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable(
                (By.XPATH, f"//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{text.lower()}')]")
            )
        ).click()
        return True
    except (TimeoutException, NoSuchElementException):
        return False


def _navigate_to_password(driver: webdriver.Chrome, wait: WebDriverWait):
    """
    Navigate Yahoo's multi-step challenge screens until the password field appears.

    Yahoo's current login flow after entering username:
      1. WebAuthn / QR code page  →  "Try signing in another way"
      2. Push notification page   →  "No, I can't access that device"
      3. Challenge picker page    →  click the password option
      4. Password entry page      →  return the field

    Each step may or may not appear depending on account settings.
    """
    max_attempts = 5
    for attempt in range(max_attempts):
        # Check if password field is already visible
        try:
            pwd = driver.find_element(By.ID, "login-passwd")
            print("[weekly] Password field found.")
            return pwd
        except NoSuchElementException:
            pass
        try:
            pwd = driver.find_element(By.NAME, "password")
            print("[weekly] Password field found.")
            return pwd
        except NoSuchElementException:
            pass

        url = driver.current_url.lower()
        print(f"[weekly] Challenge step {attempt + 1}: {url.split('?')[0]}")

        # Try various bypass buttons in order of priority
        clicked = False

        # "No, I can't access that device" (push notification page)
        if not clicked:
            clicked = _click_button_with_text(driver, "access that device", timeout=3)
            if clicked:
                print("[weekly]   → Clicked 'No, I can't access that device'")

        # "Try signing in another way" (WebAuthn / QR page)
        if not clicked:
            clicked = _click_button_with_text(driver, "another way", timeout=3)
            if clicked:
                print("[weekly]   → Clicked 'Try signing in another way'")

        # "password" option on challenge picker
        if not clicked:
            clicked = _click_button_with_text(driver, "password", timeout=3)
            if clicked:
                print("[weekly]   → Clicked password option")

        if not clicked:
            # Try clicking any link/anchor with relevant text via JS
            clicked_js = driver.execute_script("""
                var els = document.querySelectorAll('a, button, [role=button]');
                var keywords = ['another way', 'access that device', 'password',
                                'other way', 'more ways', 'sign in with'];
                for (var el of els) {
                    var txt = el.textContent.toLowerCase();
                    for (var kw of keywords) {
                        if (txt.includes(kw)) { el.click(); return kw; }
                    }
                }
                return null;
            """)
            if clicked_js:
                print(f"[weekly]   → JS clicked element with '{clicked_js}'")
                clicked = True

        if not clicked:
            print(f"[weekly]   → No actionable button found on this page.")

        time.sleep(3)

    raise RuntimeError(
        "[weekly] Could not reach password field after navigating challenge screens. "
        "Yahoo may require interactive login. Try running weekly.py with --no-headless."
    )


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
    time.sleep(3)

    # --- Step 2: Navigate challenge screens to reach password entry ---
    # Yahoo may present: WebAuthn → push notification → password
    # We need to click through "Try signing in another way" / "No, I can't
    # access that device" until we reach the password form.
    password_field = _navigate_to_password(driver, wait)

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

    # Current table structure (Feb 2026):
    #   Header: ['', 'Player', '', 'Roster Status', 'Percent']
    #   Data:   [icon, 'Name\nTEAM - POS\nScore', icon, 'Team Name', '27.4']
    # Player name is in cell 1, first line only. Percent is in the last cell.

    for row in rows[1:]:  # skip header row
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) < 3:
            continue

        row_text = [c.text.strip() for c in cells]

        # Extract player name from the player cell (cell index 1)
        # The cell contains "Name\nTEAM - POS\nGame Score" — take first line
        player_cell = row_text[1] if len(row_text) > 1 else ""
        player_name = ""
        if player_cell:
            first_line = player_cell.split("\n")[0].strip()
            # Clean up suffixes like "Video Forecast", injury tags (INJ, O, GTD)
            first_line = re.sub(r"(Video Forecast|Video|Forecast)$", "", first_line).strip()
            first_line = re.sub(r"(INJ|GTD|O|DTD|SUSP)$", "", first_line).strip()
            player_name = first_line

        # Extract roster status (team name) from cell 3
        roster_status = row_text[3] if len(row_text) > 3 else "Unknown"

        # Extract percent from last cell (no % symbol, just a number like "27.4")
        mvp_percent = 0.0
        last_cell = row_text[-1] if row_text else ""
        try:
            mvp_percent = float(last_cell.replace("%", "").strip())
        except ValueError:
            pass

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
        # Try loading saved cookies first
        cookies_loaded = _load_cookies(driver)
        if cookies_loaded:
            print(f"[weekly] Trying saved cookies …")
            driver.get(KEYS_URL)
            time.sleep(3)

        if not cookies_loaded or "login.yahoo.com" in driver.current_url or not _is_logged_in(driver):
            if cookies_loaded:
                print("[weekly] Saved cookies expired — need fresh login.")

            # Navigate to the target page to check login state
            if not cookies_loaded:
                print(f"[weekly] Navigating to {KEYS_URL} …")
                driver.get(KEYS_URL)
                time.sleep(3)

            if "login.yahoo.com" in driver.current_url or not _is_logged_in(driver):
                _yahoo_login(driver)
                _save_cookies(driver)
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


def interactive_login() -> None:
    """
    Open a visible Chrome window for manual Yahoo login.

    Yahoo now requires QR code / push notification / passkey auth which
    can't be automated headlessly. Run this once to log in manually;
    cookies are saved to yahoo_cookies.json for future headless runs.
    """
    driver = _build_driver(headless=False)
    try:
        driver.get(KEYS_URL)
        print("[weekly] A Chrome window has opened.")
        print("[weekly] Please log in to Yahoo and navigate to the Keys to Success page.")
        print("[weekly] Once you see the MVP table, press ENTER here to save cookies …")
        input()
        _save_cookies(driver)
        print("[weekly] Cookies saved! Future runs will use these automatically.")
    finally:
        driver.quit()


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
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open Chrome for interactive Yahoo login (saves cookies for headless runs).",
    )
    args = parser.parse_args()

    if args.login:
        interactive_login()
        sys.exit(0)

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
