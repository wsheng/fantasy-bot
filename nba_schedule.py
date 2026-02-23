"""
nba_schedule.py — NBA schedule and game-day checker.

Primary data source: nba_api (official stats endpoint).
Fallback: balldontlie public API.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import date, timedelta
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Team abbreviation reference
# ---------------------------------------------------------------------------

# Full team name -> official NBA abbreviation
NBA_TEAM_ABBR: dict[str, str] = {
    "Atlanta Hawks": "ATL",
    "Boston Celtics": "BOS",
    "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA",
    "Chicago Bulls": "CHI",
    "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL",
    "Denver Nuggets": "DEN",
    "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW",
    "Houston Rockets": "HOU",
    "Indiana Pacers": "IND",
    "Los Angeles Clippers": "LAC",
    "Los Angeles Lakers": "LAL",
    "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA",
    "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK",
    "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC",
    "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR",
    "Utah Jazz": "UTA",
    "Washington Wizards": "WAS",
}

# Reverse map (abbreviation -> full name)
ABBR_TO_TEAM: dict[str, str] = {v: k for k, v in NBA_TEAM_ABBR.items()}

# Abbreviation normalisation map — covers ESPN, balldontlie, and legacy codes
ABBR_FIXES: dict[str, str] = {
    # ESPN-specific
    "UTAH": "UTA",
    "WSH":  "WAS",
    "NO":   "NOP",
    "GS":   "GSW",
    "NY":   "NYK",
    "SA":   "SAS",
    "PHO":  "PHX",
    # Legacy / balldontlie
    "NJN":  "BKN",
    "NOH":  "NOP",
    "SEA":  "OKC",
    "VAN":  "MEM",
}

# Keep old name for any code that still references it
BDL_ABBR_FIXES = ABBR_FIXES


# ---------------------------------------------------------------------------
# Primary: nba_api
# ---------------------------------------------------------------------------


def _get_games_from_espn(game_date: str) -> Optional[set[str]]:
    """
    Primary source: ESPN public scoreboard API (no auth required).

    game_date: YYYY-MM-DD
    Returns set of team abbreviations, or None on failure.
    """
    date_nodash = game_date.replace("-", "")
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    print(f"[nba_schedule] Querying ESPN scoreboard for {game_date} …")
    try:
        resp = requests.get(url, params={"dates": date_nodash}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"[nba_schedule] ESPN request failed: {exc}")
        return None

    abbrs: set[str] = set()
    for event in data.get("events", []):
        for competition in event.get("competitions", []):
            for competitor in competition.get("competitors", []):
                team = competitor.get("team", {})
                raw = team.get("abbreviation", "").upper()
                if raw:
                    abbrs.add(ABBR_FIXES.get(raw, raw))

    print(f"[nba_schedule] ESPN: {len(abbrs)} teams playing today: {sorted(abbrs)}")
    return abbrs


def _get_games_from_nba_api(game_date: str) -> Optional[set[str]]:
    """
    Fallback: nba_api ScoreboardV2 with stats.nba.com-required headers.

    Returns a set of team abbreviations playing today, or None on failure.
    """
    try:
        from nba_api.stats.endpoints import scoreboardv2  # type: ignore
        from nba_api.stats.library.http import NBAStatsHTTP  # type: ignore

        # stats.nba.com requires these headers or it returns 403/timeout
        NBAStatsHTTP.HEADERS = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Host": "stats.nba.com",
            "Origin": "https://www.nba.com",
            "Referer": "https://www.nba.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            "x-nba-stats-origin": "stats",
            "x-nba-stats-token": "true",
        }

        print(f"[nba_schedule] Falling back to nba_api for {game_date} …")
        board = scoreboardv2.ScoreboardV2(
            game_date=game_date,
            league_id="00",
            day_offset=0,
            timeout=30,
        )
        line_score = board.line_score.get_data_frame()
        if line_score.empty:
            return set()

        abbrs: set[str] = set()
        if "TEAM_ABBREVIATION" in line_score.columns:
            for abbr in line_score["TEAM_ABBREVIATION"].dropna():
                abbrs.add(str(abbr).upper())

        print(f"[nba_schedule] nba_api: {len(abbrs)} teams playing today: {sorted(abbrs)}")
        return abbrs

    except Exception as exc:
        print(f"[nba_schedule] nba_api failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def get_todays_games() -> set[str]:
    """
    Return the set of NBA team abbreviations that have a game today.

    Tries nba_api first; falls back to balldontlie on any error.
    """
    today_str = date.today().strftime("%Y-%m-%d")

    result = _get_games_from_espn(today_str)
    if result is None:
        result = _get_games_from_nba_api(today_str)
    if result is None:
        print("[nba_schedule] All schedule sources failed — assuming no games today.")
        result = set()

    return result


def player_has_game_today(
    player_name: str,
    player_team_abbr: str,
    games_today: set[str],
) -> bool:
    """
    Return True if the player's team is playing today.

    player_team_abbr should already be a normalised NBA abbreviation
    (e.g. 'LAL', 'GSW').  Normalisation via BDL_ABBR_FIXES is applied
    as a safety net.
    """
    normalised = BDL_ABBR_FIXES.get(player_team_abbr.upper(), player_team_abbr.upper())
    return normalised in games_today


def get_player_team_abbr(player_name: str) -> Optional[str]:
    """
    Attempt to look up a player's current team abbreviation via nba_api.

    Returns the abbreviation string, or None if it cannot be determined.
    """
    try:
        from nba_api.stats.static import players as nba_players  # type: ignore
        from nba_api.stats.endpoints import commonplayerinfo  # type: ignore

        # Find player id
        matches = nba_players.find_players_by_full_name(player_name)
        if not matches:
            # Try partial first-name / last-name match
            parts = player_name.split()
            if len(parts) >= 2:
                matches = nba_players.find_players_by_last_name(parts[-1])

        if not matches:
            print(f"[nba_schedule] Could not find player '{player_name}' in nba_api.")
            return None

        player_id = matches[0]["id"]
        time.sleep(0.6)  # be polite to the stats.nba.com endpoint

        info = commonplayerinfo.CommonPlayerInfo(player_id=player_id, timeout=10)
        df = info.common_player_info.get_data_frame()

        if df.empty:
            return None

        team_abbr = df.iloc[0].get("TEAM_ABBREVIATION", "")
        if team_abbr:
            return str(team_abbr).upper()

    except Exception as exc:
        print(f"[nba_schedule] get_player_team_abbr('{player_name}') failed: {exc}")

    return None


def get_weekly_remaining_games() -> dict[str, int]:
    """
    Return the number of remaining games this fantasy week per team.

    Fantasy weeks run Mon-Sun. Queries ESPN for each remaining day
    (today through Sunday) and counts games per team abbreviation.

    Returns {"LAL": 3, "GSW": 2, ...}
    """
    today = date.today()
    # Days until Sunday (Monday=0 ... Sunday=6)
    days_until_sunday = (6 - today.weekday()) % 7
    # Include today
    remaining_dates = [today + timedelta(days=d) for d in range(days_until_sunday + 1)]

    print(f"[nba_schedule] Fetching weekly remaining games for {len(remaining_dates)} days "
          f"({remaining_dates[0]} to {remaining_dates[-1]}) …")

    team_counts: Counter[str] = Counter()
    for d in remaining_dates:
        date_str = d.strftime("%Y-%m-%d")
        teams = _get_games_from_espn(date_str)
        if teams is None:
            teams = _get_games_from_nba_api(date_str)
        if teams:
            team_counts.update(teams)

    result = dict(team_counts)
    print(f"[nba_schedule] Weekly games: {len(result)} teams with remaining games.")
    return result


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    games = get_todays_games()
    print(f"\nTeams with games today ({date.today()}): {sorted(games)}")

    print("\n--- Weekly Remaining Games ---")
    weekly = get_weekly_remaining_games()
    for team, count in sorted(weekly.items(), key=lambda x: -x[1]):
        print(f"  {team}: {count} games remaining this week")
