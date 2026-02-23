"""
bm_scraper.py — Basketball Monster player rankings scraper.

Scrapes per-category value scores from Basketball Monster's public
player rankings page. Caches results to bm_cache.json (stale after 20h).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BM_URL = "https://basketballmonster.com/playerrankings.aspx"
CACHE_FILE = os.path.join(os.path.dirname(__file__), "bm_cache.json")
CACHE_MAX_AGE_HOURS = 20

# Column names we look for in the BM table header.
# BM uses short labels like "pV" (points value), "3V" (3PTM value), etc.
# Mapping from BM column label -> our internal key
BM_CAT_COLS: dict[str, str] = {
    "pV":   "pts",
    "3V":   "3pm",
    "rV":   "reb",
    "aV":   "ast",
    "sV":   "stl",
    "bV":   "blk",
    "fg%V": "fg_pct",
    "ft%V": "ft_pct",
    "toV":  "to",
}

# Headers to mimic a browser
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


def _scrape_bm() -> list[dict]:
    """
    Scrape Basketball Monster player rankings page.

    Returns a list of dicts:
        {name, team, value, cat_values: {pts, 3pm, reb, ast, stl, blk, fg_pct, ft_pct, to}}
    """
    print("[bm_scraper] Scraping Basketball Monster rankings …")
    resp = requests.get(BM_URL, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Find the main rankings table — it's the one with id containing "GridView"
    # or the largest table on the page
    table = soup.find("table", {"id": lambda x: x and "GridView" in x})
    if table is None:
        # Fallback: find largest table
        tables = soup.find_all("table")
        if not tables:
            print("[bm_scraper] WARNING: No tables found on page.")
            return []
        table = max(tables, key=lambda t: len(t.find_all("tr")))

    rows = table.find_all("tr")
    if len(rows) < 2:
        print("[bm_scraper] WARNING: Table has no data rows.")
        return []

    # Parse header row to find column indices
    header_row = rows[0]
    headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]

    # Find key column indices
    col_idx: dict[str, int] = {}
    for i, h in enumerate(headers):
        h_lower = h.lower()
        if h_lower in ("player", "name"):
            col_idx["name"] = i
        elif h_lower == "team":
            col_idx["team"] = i
        elif h_lower == "value":
            col_idx["value"] = i
        # Category value columns
        for bm_label, our_key in BM_CAT_COLS.items():
            if h_lower == bm_label.lower():
                col_idx[our_key] = i

    if "name" not in col_idx:
        # Try looking for a link in data rows to identify the name column
        for data_row in rows[1:3]:
            cells = data_row.find_all(["td", "th"])
            for i, cell in enumerate(cells):
                if cell.find("a"):
                    col_idx["name"] = i
                    break
            if "name" in col_idx:
                break

    if "name" not in col_idx:
        print(f"[bm_scraper] WARNING: Could not identify name column. Headers: {headers}")
        return []

    print(f"[bm_scraper] Found columns: {list(col_idx.keys())}")

    # Parse data rows
    players: list[dict] = []
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) <= col_idx.get("name", 0):
            continue

        # Name: often wrapped in a link
        name_cell = cells[col_idx["name"]]
        name_link = name_cell.find("a")
        name = name_link.get_text(strip=True) if name_link else name_cell.get_text(strip=True)
        if not name or name.lower() in ("name", "player"):
            continue

        # Team
        team = ""
        if "team" in col_idx and col_idx["team"] < len(cells):
            team = cells[col_idx["team"]].get_text(strip=True)

        # Total value
        value = 0.0
        if "value" in col_idx and col_idx["value"] < len(cells):
            try:
                value = float(cells[col_idx["value"]].get_text(strip=True))
            except (ValueError, TypeError):
                pass

        # Per-category values
        cat_values: dict[str, float] = {}
        for our_key in BM_CAT_COLS.values():
            if our_key in col_idx and col_idx[our_key] < len(cells):
                try:
                    cat_values[our_key] = float(cells[col_idx[our_key]].get_text(strip=True))
                except (ValueError, TypeError):
                    cat_values[our_key] = 0.0

        players.append({
            "name": name,
            "team": team,
            "value": value,
            "cat_values": cat_values,
        })

    print(f"[bm_scraper] Scraped {len(players)} players from Basketball Monster.")
    return players


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def _cache_is_fresh() -> bool:
    """Return True if the cache file exists and is less than CACHE_MAX_AGE_HOURS old."""
    if not os.path.exists(CACHE_FILE):
        return False
    try:
        with open(CACHE_FILE) as fh:
            data = json.load(fh)
        ts = data.get("timestamp", "")
        cached_time = datetime.fromisoformat(ts)
        return datetime.now() - cached_time < timedelta(hours=CACHE_MAX_AGE_HOURS)
    except (json.JSONDecodeError, ValueError, KeyError):
        return False


def _save_cache(players: list[dict]) -> None:
    """Write players to cache file with current timestamp."""
    data = {
        "timestamp": datetime.now().isoformat(),
        "players": players,
    }
    with open(CACHE_FILE, "w") as fh:
        json.dump(data, fh, indent=2)
    print(f"[bm_scraper] Cached {len(players)} players to {CACHE_FILE}")


def _load_cache() -> list[dict]:
    """Load players from cache file."""
    with open(CACHE_FILE) as fh:
        data = json.load(fh)
    players = data.get("players", [])
    print(f"[bm_scraper] Loaded {len(players)} players from cache (ts={data.get('timestamp', '?')})")
    return players


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_bm_rankings() -> list[dict]:
    """
    Return Basketball Monster player rankings.

    Scrapes if cache is stale (>20 hours), otherwise loads cache.

    Returns list of dicts:
        {name: str, team: str, value: float, cat_values: {pts, 3pm, reb, ast, stl, blk, fg_pct, ft_pct, to}}
    """
    if _cache_is_fresh():
        return _load_cache()

    try:
        players = _scrape_bm()
        if players:
            _save_cache(players)
            return players
    except Exception as exc:
        print(f"[bm_scraper] Scrape failed: {exc}")

    # Fall back to stale cache if available
    if os.path.exists(CACHE_FILE):
        print("[bm_scraper] Using stale cache as fallback.")
        return _load_cache()

    print("[bm_scraper] No data available — returning empty list.")
    return []


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    players = fetch_bm_rankings()
    if players:
        print(f"\nTop 20 Basketball Monster players ({len(players)} total):\n")
        print(f"  {'Name':<30} {'Team':<5} {'Value':<8} {'pV':<6} {'3V':<6} {'rV':<6} {'aV':<6} {'sV':<6} {'bV':<6}")
        print("  " + "-" * 90)
        for p in players[:20]:
            cv = p.get("cat_values", {})
            print(
                f"  {p['name']:<30} {p['team']:<5} {p['value']:<8.2f} "
                f"{cv.get('pts', 0):<6.2f} {cv.get('3pm', 0):<6.2f} "
                f"{cv.get('reb', 0):<6.2f} {cv.get('ast', 0):<6.2f} "
                f"{cv.get('stl', 0):<6.2f} {cv.get('blk', 0):<6.2f}"
            )
    else:
        print("No players scraped.")
