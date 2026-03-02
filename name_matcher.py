"""
name_matcher.py — Fuzzy name matching between Basketball Monster and Yahoo player names.

Handles common discrepancies: accents, Jr./Sr./III/IV suffixes, initials
(C.J. vs CJ), and minor spelling variations.
"""

from __future__ import annotations

import re
import unicodedata

from thefuzz import fuzz

# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

# Suffixes to strip for comparison
_SUFFIX_PATTERN = re.compile(
    r"\b(jr\.?|sr\.?|ii|iii|iv|v)\s*$", re.IGNORECASE
)

# Remove dots and punctuation between initials (C.J. -> CJ)
_DOT_PATTERN = re.compile(r"\.(?=\s|[A-Z]|$)")


def _normalize_name(name: str) -> str:
    """
    Normalize a player name for comparison.

    Steps:
      1. Unicode normalize (NFD) and strip accents
      2. Lowercase
      3. Strip suffixes (Jr., Sr., III, IV)
      4. Remove dots between initials (C.J. -> CJ)
      5. Collapse whitespace and strip
    """
    # Strip accents
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))

    result = ascii_name.lower()
    result = _SUFFIX_PATTERN.sub("", result)
    result = _DOT_PATTERN.sub("", result)
    result = re.sub(r"['\-]", "", result)  # strip apostrophes/hyphens
    result = re.sub(r"\s+", " ", result).strip()
    return result


def _last_name_first_initial(name: str) -> str:
    """
    Extract 'last_name + first_initial' key for fallback matching.

    'LeBron James' -> 'james l'
    'C.J. McCollum' -> 'mccollum c'
    """
    parts = _normalize_name(name).split()
    if len(parts) < 2:
        return name.lower()
    return f"{parts[-1]} {parts[0][0]}"


# ---------------------------------------------------------------------------
# Team normalization
# ---------------------------------------------------------------------------

# Common abbreviation variants → canonical form
TEAM_ALIASES: dict[str, str] = {
    "SA": "SAS", "GS": "GSW", "NY": "NYK", "NO": "NOP",
    "PHO": "PHX", "BKN": "BKN", "BRK": "BKN",
}


def _normalize_team(abbr: str) -> str:
    """Normalize a team abbreviation to a canonical form."""
    upper = abbr.strip().upper()
    return TEAM_ALIASES.get(upper, upper)


def _teams_match(team_a: str, team_b: str) -> bool:
    """Return True if two team abbreviations refer to the same team."""
    if not team_a or not team_b:
        return False
    return _normalize_team(team_a) == _normalize_team(team_b)


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------

# Minimum fuzzy ratio to accept a match (unconditional)
FUZZY_THRESHOLD = 90
# Lower threshold when team abbreviation also matches
FUZZY_THRESHOLD_WITH_TEAM = 85


def match_bm_to_yahoo(
    bm_players: list[dict],
    yahoo_players: list[dict],
) -> dict[str, dict]:
    """
    Match Basketball Monster players to Yahoo player dicts.

    Matching strategy (in order):
      1. Exact normalized name match
      2. thefuzz.fuzz.ratio >= 90
      3. Last-name + first-initial match

    Parameters
    ----------
    bm_players    : list of dicts from bm_scraper.fetch_bm_rankings()
    yahoo_players : list of player dicts (roster + FAs) with 'name' field

    Returns
    -------
    {yahoo_player_name: bm_data_dict} for all matched players.
    bm_data_dict has keys: name, team, value, cat_values
    """
    # Build Yahoo lookup structures
    yahoo_by_norm: dict[str, dict] = {}
    yahoo_by_lnfi: dict[str, list[dict]] = {}
    yahoo_all: list[dict] = []

    for yp in yahoo_players:
        yname = yp.get("name", "")
        norm = _normalize_name(yname)
        yahoo_by_norm[norm] = yp
        lnfi = _last_name_first_initial(yname)
        yahoo_by_lnfi.setdefault(lnfi, []).append(yp)
        yahoo_all.append(yp)

    matched: dict[str, dict] = {}
    unmatched: list[str] = []

    for bm in bm_players:
        bm_name = bm["name"]
        bm_norm = _normalize_name(bm_name)

        # Strategy 1: Exact normalized match
        if bm_norm in yahoo_by_norm:
            yp = yahoo_by_norm[bm_norm]
            matched[yp["name"]] = bm
            continue

        # Strategy 2: Fuzzy match (dual threshold — unconditional ≥90, team-confirmed ≥85)
        bm_team = bm.get("team", "")
        best_ratio = 0
        best_yp = None
        best_ratio_team = 0
        best_yp_team = None
        for yp in yahoo_all:
            if yp["name"] in matched:
                continue
            ratio = fuzz.ratio(bm_norm, _normalize_name(yp["name"]))
            if ratio > best_ratio:
                best_ratio = ratio
                best_yp = yp
            if ratio > best_ratio_team and _teams_match(bm_team, yp.get("team_abbr", "")):
                best_ratio_team = ratio
                best_yp_team = yp

        if best_ratio >= FUZZY_THRESHOLD and best_yp is not None:
            matched[best_yp["name"]] = bm
            continue
        if best_ratio_team >= FUZZY_THRESHOLD_WITH_TEAM and best_yp_team is not None:
            matched[best_yp_team["name"]] = bm
            continue

        # Strategy 3: Last-name + first-initial (prefer team-matched candidate)
        bm_lnfi = _last_name_first_initial(bm_name)
        candidates = yahoo_by_lnfi.get(bm_lnfi, [])
        team_candidate = None
        any_candidate = None
        for yp in candidates:
            if yp["name"] in matched:
                continue
            if any_candidate is None:
                any_candidate = yp
            if _teams_match(bm_team, yp.get("team_abbr", "")):
                team_candidate = yp
                break
        chosen_yp = team_candidate or any_candidate
        if chosen_yp is not None:
            matched[chosen_yp["name"]] = bm
        else:
            unmatched.append(bm_name)

    # Log unmatched (top 100 by value only)
    if unmatched:
        print(f"[name_matcher] {len(unmatched)} BM players unmatched. Top unmatched:")
        for name in unmatched[:15]:
            print(f"  - {name}")

    print(f"[name_matcher] Matched {len(matched)} / {len(bm_players)} BM players to Yahoo names.")
    return matched


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Test with known name variants
    test_bm = [
        {"name": "Nikola Jokic", "team": "DEN", "value": 12.5, "cat_values": {}},
        {"name": "Shai Gilgeous-Alexander", "team": "OKC", "value": 11.0, "cat_values": {}},
        {"name": "C.J. McCollum", "team": "NOP", "value": 3.2, "cat_values": {}},
        {"name": "Nicolas Claxton", "team": "BKN", "value": 2.1, "cat_values": {}},
        {"name": "Luka Doncic", "team": "DAL", "value": 9.8, "cat_values": {}},
        {"name": "Jaren Jackson Jr.", "team": "MEM", "value": 5.5, "cat_values": {}},
    ]
    test_yahoo = [
        {"name": "Nikola Jokic", "team_abbr": "DEN"},
        {"name": "Shai Gilgeous-Alexander", "team_abbr": "OKC"},
        {"name": "CJ McCollum", "team_abbr": "NOP"},
        {"name": "Nic Claxton", "team_abbr": "BKN"},
        {"name": "Luka Doncic", "team_abbr": "DAL"},
        {"name": "Jaren Jackson", "team_abbr": "MEM"},
    ]

    result = match_bm_to_yahoo(test_bm, test_yahoo)
    print(f"\nMatched {len(result)} players:")
    for yahoo_name, bm_data in result.items():
        print(f"  Yahoo: {yahoo_name:<30} -> BM: {bm_data['name']:<30} (value={bm_data['value']})")
