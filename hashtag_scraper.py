"""
hashtag_scraper.py — Hashtag Basketball player rankings scraper.

Scrapes z-scores and per-category values from Hashtag Basketball's public
player rankings page. The full-season view provides z-scores with per-category
breakdowns; 30-day and 14-day views provide ranking positions only.

Caches results to ht_cache.json (stale after 20h).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HT_URL = "https://hashtagbasketball.com/fantasy-basketball-rankings"
CACHE_FILE = os.path.join(os.path.dirname(__file__), "ht_cache.json")
CACHE_MAX_AGE_HOURS = 20

# ASP.NET form field name for the time-period dropdown
DD_DURATION = "ctl00$ContentPlaceHolder1$DDDURATION"
DD_SHOW = "ctl00$ContentPlaceHolder1$DDSHOW"

# Per-category hidden field suffixes → our internal keys
HF_CAT_MAP = {
    "HFFGP": "fg_pct",
    "HFFTP": "ft_pct",
    "HFTGM": "3pm",
    "HFPTS": "pts",
    "HFREB": "reb",
    "HFAST": "ast",
    "HFSTL": "stl",
    "HFBLK": "blk",
    "HFTUR": "to",
}

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
# Scraper internals
# ---------------------------------------------------------------------------


def _extract_form_state(soup: BeautifulSoup) -> dict[str, str]:
    """
    Extract all form state needed for a POST: ASP.NET hidden fields
    plus current values of every non-disabled <select> dropdown.
    """
    fields: dict[str, str] = {}

    for name in ("__VIEWSTATE", "__EVENTVALIDATION", "__VIEWSTATEGENERATOR"):
        tag = soup.find("input", {"name": name})
        if tag:
            fields[name] = tag.get("value", "")

    form = soup.find("form", {"id": "form1"})
    if form:
        for sel in form.find_all("select"):
            sname = sel.get("name", "")
            if not sname or sel.get("disabled"):
                continue
            selected = sel.find("option", selected=True)
            if selected:
                fields[sname] = selected.get("value", "")

    return fields


def _parse_season_table(soup: BeautifulSoup) -> list[dict]:
    """
    Parse the full-season rankings table from the initial GET.

    Returns list of player dicts with z-scores from visible TOTAL column
    and per-category z-scores from hidden input fields.
    """
    table = soup.find("table", {"id": lambda x: x and "GridView1" in x})
    if table is None:
        print("[hashtag_scraper] WARNING: No GridView1 table found.")
        return []

    rows = table.find_all("tr")
    if len(rows) < 2:
        print("[hashtag_scraper] WARNING: Table has no data rows.")
        return []

    # Parse header for column indices
    headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
    col_idx: dict[str, int] = {}
    for i, h in enumerate(headers):
        if h == "PLAYER":
            col_idx["name"] = i
        elif h == "TEAM":
            col_idx["team"] = i
        elif h == "TOTAL":
            col_idx["value"] = i

    if "name" not in col_idx:
        print(f"[hashtag_scraper] WARNING: No PLAYER column. Headers: {headers}")
        return []

    players: list[dict] = []

    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) <= col_idx.get("name", 0):
            continue

        # Name
        name_cell = cells[col_idx["name"]]
        name_link = name_cell.find("a")
        name = name_link.get_text(strip=True) if name_link else name_cell.get_text(strip=True)
        if not name or name.upper() in ("PLAYER", "NAME"):
            continue

        # Team
        team = ""
        if "team" in col_idx and col_idx["team"] < len(cells):
            team = cells[col_idx["team"]].get_text(strip=True)

        # TOTAL z-score
        value = 0.0
        if "value" in col_idx and col_idx["value"] < len(cells):
            try:
                value = float(cells[col_idx["value"]].get_text(strip=True))
            except (ValueError, TypeError):
                pass

        # Per-category z-scores from hidden fields in this row
        cat_values: dict[str, float] = {}
        for hf_suffix, our_key in HF_CAT_MAP.items():
            hf = row.find("input", {"name": lambda n: n and n.endswith(hf_suffix)})
            if hf:
                try:
                    cat_values[our_key] = round(float(hf.get("value", "0")), 4)
                except (ValueError, TypeError):
                    cat_values[our_key] = 0.0

        players.append({
            "name": name,
            "team": team,
            "value": value,
            "cat_values": cat_values,
        })

    return players


def _parse_ranked_table(soup: BeautifulSoup) -> dict[str, int]:
    """
    Parse a time-window (30d/14d) rankings table.

    These tables don't have z-scores but DO have ranking positions (R# column).
    Returns {player_name: rank_position}.
    """
    table = soup.find("table", {"id": lambda x: x and "GridView1" in x})
    if table is None:
        return {}

    rows = table.find_all("tr")
    if len(rows) < 2:
        return {}

    headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
    col_idx: dict[str, int] = {}
    for i, h in enumerate(headers):
        if h == "R#":
            col_idx["rank"] = i
        elif h == "PLAYER":
            col_idx["name"] = i

    if "name" not in col_idx or "rank" not in col_idx:
        return {}

    rankings: dict[str, int] = {}
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) <= max(col_idx.values()):
            continue

        name_cell = cells[col_idx["name"]]
        name_link = name_cell.find("a")
        name = name_link.get_text(strip=True) if name_link else name_cell.get_text(strip=True)
        if not name or name.upper() in ("PLAYER", "NAME"):
            continue

        try:
            rank = int(cells[col_idx["rank"]].get_text(strip=True))
        except (ValueError, TypeError):
            continue

        rankings[name] = rank

    return rankings


def _scrape_hashtag() -> dict:
    """
    Scrape Hashtag Basketball rankings.

    Returns {
        "players": [...],       # full-season z-scores
        "ranks_30d": {name: rank},  # 30-day ranking positions
        "ranks_14d": {name: rank},  # 14-day ranking positions
    }
    """
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    # Step 1: GET the full-season page (default view, DDDURATION=1)
    # This is the only view that provides z-scores and per-category values.
    print("[hashtag_scraper] Fetching full-season rankings …")
    resp = session.get(HT_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    players = _parse_season_table(soup)
    print(f"[hashtag_scraper] Full-season: {len(players)} players with z-scores.")

    form_state = _extract_form_state(soup)
    if not form_state.get("__VIEWSTATE"):
        print("[hashtag_scraper] WARNING: No __VIEWSTATE found — skipping time-window ranks.")
        return {"players": players, "ranks_30d": {}, "ranks_14d": {}}

    # Step 2: POST for 30-day and 14-day ranking positions.
    # These views don't provide z-scores but DO rank players by recent performance.
    result = {"players": players, "ranks_30d": {}, "ranks_14d": {}}

    for label, duration_val, key in [("30d", "30", "ranks_30d"), ("14d", "14", "ranks_14d")]:
        print(f"[hashtag_scraper] POSTing for {label} ranking positions …")
        data = dict(form_state)
        data["__EVENTTARGET"] = DD_DURATION
        data["__EVENTARGUMENT"] = ""
        data["__LASTFOCUS"] = ""
        data[DD_DURATION] = duration_val
        data[DD_SHOW] = "300"

        try:
            resp = session.post(HT_URL, data=data, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            rankings = _parse_ranked_table(soup)
            result[key] = rankings
            print(f"[hashtag_scraper] {label}: {len(rankings)} ranking positions.")
            # Update form state for the next POST
            form_state = _extract_form_state(soup)
        except Exception as exc:
            print(f"[hashtag_scraper] {label} POST failed: {exc}")

    return result


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


def _save_cache(rankings: dict) -> None:
    """Write rankings to cache file with current timestamp."""
    data = {
        "timestamp": datetime.now().isoformat(),
        **rankings,
    }
    with open(CACHE_FILE, "w") as fh:
        json.dump(data, fh, indent=2)
    print(f"[hashtag_scraper] Cached {len(rankings.get('players', []))} players to {CACHE_FILE}")


def _load_cache() -> dict:
    """Load rankings from cache file."""
    with open(CACHE_FILE) as fh:
        data = json.load(fh)
    result = {
        "players": data.get("players", []),
        "ranks_30d": data.get("ranks_30d", {}),
        "ranks_14d": data.get("ranks_14d", {}),
    }
    print(
        f"[hashtag_scraper] Loaded from cache (ts={data.get('timestamp', '?')}): "
        f"{len(result['players'])} players, "
        f"{len(result['ranks_30d'])} 30d ranks, "
        f"{len(result['ranks_14d'])} 14d ranks"
    )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_hashtag_rankings() -> dict:
    """
    Return Hashtag Basketball player rankings.

    Scrapes if cache is stale (>20 hours), otherwise loads cache.

    Returns {
        "players": [
            {name, team, value (TOTAL z-score),
             cat_values: {pts, 3pm, reb, ast, stl, blk, fg_pct, ft_pct, to}},
            ...
        ],
        "ranks_30d": {player_name: rank_position, ...},
        "ranks_14d": {player_name: rank_position, ...},
    }
    """
    if _cache_is_fresh():
        return _load_cache()

    try:
        rankings = _scrape_hashtag()
        if rankings.get("players"):
            _save_cache(rankings)
            return rankings
    except Exception as exc:
        print(f"[hashtag_scraper] Scrape failed: {exc}")

    # Fall back to stale cache if available
    if os.path.exists(CACHE_FILE):
        print("[hashtag_scraper] Using stale cache as fallback.")
        return _load_cache()

    print("[hashtag_scraper] No data available — returning empty.")
    return {"players": [], "ranks_30d": {}, "ranks_14d": {}}


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    rankings = fetch_hashtag_rankings()
    players = rankings["players"]
    ranks_30d = rankings["ranks_30d"]
    ranks_14d = rankings["ranks_14d"]

    print(f"\nTop 20 Hashtag Basketball players — full season ({len(players)} total):\n")
    print(
        f"  {'Name':<30} {'Team':<5} {'TOTAL':<8} "
        f"{'30dR':<5} {'14dR':<5} "
        f"{'FG%':<7} {'FT%':<7} {'3PM':<7} {'PTS':<7} {'REB':<7} {'AST':<7} {'STL':<7} {'BLK':<7}"
    )
    print("  " + "-" * 120)
    for p in players[:20]:
        cv = p.get("cat_values", {})
        r30 = ranks_30d.get(p["name"], "—")
        r14 = ranks_14d.get(p["name"], "—")
        print(
            f"  {p['name']:<30} {p['team']:<5} {p['value']:<8.2f} "
            f"{str(r30):<5} {str(r14):<5} "
            f"{cv.get('fg_pct', 0):<7.2f} {cv.get('ft_pct', 0):<7.2f} "
            f"{cv.get('3pm', 0):<7.2f} {cv.get('pts', 0):<7.2f} "
            f"{cv.get('reb', 0):<7.2f} {cv.get('ast', 0):<7.2f} "
            f"{cv.get('stl', 0):<7.2f} {cv.get('blk', 0):<7.2f}"
        )

    print(f"\n30-day top 10 (by HT rank):")
    sorted_30d = sorted(ranks_30d.items(), key=lambda x: x[1])[:10]
    for name, rank in sorted_30d:
        print(f"  #{rank}: {name}")

    print(f"\n14-day top 10 (by HT rank):")
    sorted_14d = sorted(ranks_14d.items(), key=lambda x: x[1])[:10]
    for name, rank in sorted_14d:
        print(f"  #{rank}: {name}")
