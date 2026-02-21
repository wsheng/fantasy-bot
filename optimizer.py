"""
optimizer.py — Daily lineup optimizer.

Uses a greedy bipartite-matching approach: fill most-restrictive slots
first (C, then positional slots, then flex slots), always choosing the
highest-ranked eligible unassigned player.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Slot / position definitions
# ---------------------------------------------------------------------------

# The 10 active slots in order from most-restrictive to most-flexible
ACTIVE_SLOTS: list[str] = [
    "PG", "SG", "G",
    "SF", "PF", "F",
    "C", "C",
    "UTIL", "UTIL",
]

BENCH_SLOTS: list[str] = ["BN", "BN", "BN"]

# Which player positions are eligible for each roster slot
SLOT_ELIGIBILITY: dict[str, list[str]] = {
    "PG":   ["PG"],
    "SG":   ["SG"],
    "G":    ["PG", "SG"],
    "SF":   ["SF"],
    "PF":   ["PF"],
    "F":    ["SF", "PF"],
    "C":    ["C"],
    "UTIL": ["PG", "SG", "SF", "PF", "C", "G", "F"],
    "BN":   ["PG", "SG", "SF", "PF", "C", "G", "F"],
}

# Ideal bench shape: one guard, one forward, one centre
TARGET_BENCH_SHAPE: dict[str, int] = {"G": 1, "F": 1, "C": 1}

# Rank threshold — players outside top-N are flagged
LOW_RANK_THRESHOLD = 96

# Slot fill order — most restrictive first so flex slots can absorb
# any player that didn't fit elsewhere.
FILL_ORDER: list[str] = ["C", "PG", "SG", "SF", "PF", "G", "F", "UTIL"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _player_eligible_for_slot(player: dict, slot: str) -> bool:
    """Return True if the player can fill the given slot."""
    eligible_positions = player.get("positions", [])
    slot_requires = SLOT_ELIGIBILITY.get(slot, [])
    return any(pos in slot_requires for pos in eligible_positions)


def _rank_sort_key(player: dict, untouchables: dict[str, float]) -> tuple:
    """
    Sort key for player selection.

    Untouchables are given a large negative bonus so they are always
    preferred over equivalent-ranked non-untouchables.
    """
    rank = player.get("yahoo_30day_rank", 999)
    is_untouchable = player["name"] in untouchables
    bonus = -10_000 if is_untouchable else 0
    return (rank + bonus,)


def _best_player_for_slot(
    slot: str,
    candidates: list[dict],
    untouchables: dict[str, float],
    games_today: set[str],
    require_game_today: bool = True,
) -> Optional[dict]:
    """
    Pick the best unassigned candidate for `slot`.

    Preference order (when require_game_today=True):
      1. Healthy/Q/DTD/GTD  WITH a game today  — start them
      2. INJ/O              WITH a game today  — status might clear; keep on roster
      3. Healthy/Q/DTD/GTD  without a game     — rest day
      4. INJ/O              without a game     — last resort

    Rationale: An O-designated player who has a game today may have their
    status updated during the day, so they are more valuable than a healthy
    player who simply has no game scheduled.
    """
    eligible = [
        p for p in candidates if _player_eligible_for_slot(p, slot)
    ]
    if not eligible:
        return None

    if not require_game_today:
        eligible.sort(key=lambda p: _rank_sort_key(p, untouchables))
        return eligible[0]

    HARD_OUT = {"INJ", "O", "NA"}

    tiers: list[list] = [
        # tier 1: active status + game today
        [p for p in eligible if p.get("has_game_today") and p.get("status") not in HARD_OUT],
        # tier 2: out/injured + game today (may clear during the day)
        [p for p in eligible if p.get("has_game_today") and p.get("status") in HARD_OUT],
        # tier 3: active status + no game
        [p for p in eligible if not p.get("has_game_today") and p.get("status") not in HARD_OUT],
        # tier 4: out/injured + no game
        [p for p in eligible if not p.get("has_game_today") and p.get("status") in HARD_OUT],
    ]

    for tier in tiers:
        if tier:
            tier.sort(key=lambda p: _rank_sort_key(p, untouchables))
            return tier[0]

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_lineup(
    roster: list[dict],
    untouchables: dict[str, float],
    games_today: set[str],
) -> dict:
    """
    Compute the recommended active lineup and bench.

    Parameters
    ----------
    roster : list of player dicts from YahooFantasyClient.get_my_roster()
    untouchables : {player_name: mvp_percent} from weekly.py / untouchables.json
    games_today : set of team abbreviations playing today from nba_schedule.py

    Returns
    -------
    {
        'active': [
            {name, slot, rank_30day, rank_14day, has_game_today,
             injury_status, is_untouchable, flag_low_rank, flag_injured},
            ...  # 10 entries
        ],
        'bench': [
            {name, slot, rank_30day, rank_14day, has_game_today,
             injury_status, is_untouchable, flag_low_rank},
            ...  # up to 3 entries
        ],
        'on_il': [
            {name, slot, rank_30day, rank_14day, injury_status},
            ...
        ],
    }
    """
    # Split roster into IL players and available players
    on_il: list[dict] = []
    available: list[dict] = []

    for player in roster:
        slot = player.get("current_slot", "BN")
        if slot in ("IL", "IL+"):
            on_il.append(player)
        else:
            # Attach convenience fields
            player = dict(player)  # shallow copy to avoid mutating caller's data
            player["has_game_today"] = player.get("has_game_today", False)
            player["is_untouchable"] = player["name"] in untouchables
            available.append(player)

    # We work with a mutable pool of unassigned players
    unassigned = list(available)

    assigned_active: list[dict] = []   # final active assignments
    assigned_bench: list[dict] = []

    # ------------------------------------------------------------------
    # Phase 1 — fill active slots in fill order
    # ------------------------------------------------------------------

    # Track which slots we've filled and how many times (for duplicate slots)
    filled_slots: list[str] = []

    for slot in FILL_ORDER:
        # Count how many times this slot appears in ACTIVE_SLOTS
        total_of_slot = ACTIVE_SLOTS.count(slot)
        already_filled = filled_slots.count(slot)
        remaining = total_of_slot - already_filled

        for _ in range(remaining):
            chosen = _best_player_for_slot(
                slot, unassigned, untouchables, games_today, require_game_today=True
            )
            if chosen is None:
                # Relax game-today requirement
                chosen = _best_player_for_slot(
                    slot, unassigned, untouchables, games_today, require_game_today=False
                )
            if chosen is None:
                continue

            unassigned.remove(chosen)
            filled_slots.append(slot)

            rank_30 = chosen.get("yahoo_30day_rank", 999)
            rank_14 = chosen.get("yahoo_14day_rank", 999)
            status = chosen.get("status", "healthy")

            assigned_active.append(
                {
                    "name": chosen["name"],
                    "slot": slot,
                    "rank_30day": rank_30,
                    "rank_14day": rank_14,
                    "has_game_today": chosen.get("has_game_today", False),
                    "injury_status": status,
                    "is_untouchable": chosen["name"] in untouchables,
                    "flag_low_rank": rank_30 > LOW_RANK_THRESHOLD,
                    "flag_injured": status in ("INJ", "O", "Q", "DTD") and slot not in ("IL", "IL+"),
                    "positions": chosen.get("positions", []),
                }
            )

    # ------------------------------------------------------------------
    # Phase 2 — fill bench slots with remaining players
    # ------------------------------------------------------------------

    remaining_players = sorted(
        unassigned,
        key=lambda p: _rank_sort_key(p, untouchables),
    )

    for i, player in enumerate(remaining_players):
        if i >= len(BENCH_SLOTS):
            break  # roster shouldn't have more than 13 players but guard anyway

        rank_30 = player.get("yahoo_30day_rank", 999)
        rank_14 = player.get("yahoo_14day_rank", 999)

        assigned_bench.append(
            {
                "name": player["name"],
                "slot": "BN",
                "rank_30day": rank_30,
                "rank_14day": rank_14,
                "has_game_today": player.get("has_game_today", False),
                "injury_status": player.get("status", "healthy"),
                "is_untouchable": player["name"] in untouchables,
                "flag_low_rank": rank_30 > LOW_RANK_THRESHOLD,
                "positions": player.get("positions", []),
            }
        )

    # ------------------------------------------------------------------
    # Phase 3 — format IL players
    # ------------------------------------------------------------------

    il_formatted = [
        {
            "name": p["name"],
            "slot": p.get("current_slot", "IL"),
            "rank_30day": p.get("yahoo_30day_rank", 999),
            "rank_14day": p.get("yahoo_14day_rank", 999),
            "injury_status": p.get("status", "INJ"),
        }
        for p in on_il
    ]

    return {
        "active": assigned_active,
        "bench": assigned_bench,
        "on_il": il_formatted,
    }


# ---------------------------------------------------------------------------
# Bench shape analysis
# ---------------------------------------------------------------------------


def _classify_bench_player(player: dict) -> Optional[str]:
    """
    Map a bench player to a bench-shape category (G, F, or C).

    Uses the player's eligible positions (not their assigned slot).
    """
    positions = player.get("positions", [])
    if "C" in positions:
        return "C"
    if any(p in positions for p in ("SF", "PF", "F")):
        return "F"
    if any(p in positions for p in ("PG", "SG", "G")):
        return "G"
    return None


def check_bench_shape(bench: list[dict]) -> tuple[dict, bool, str]:
    """
    Analyse the composition of the bench against TARGET_BENCH_SHAPE.

    Returns
    -------
    actual_shape : dict  e.g. {'G': 1, 'F': 2, 'C': 0}
    is_target_met : bool
    description : human-readable summary string
    """
    actual: dict[str, int] = {"G": 0, "F": 0, "C": 0}

    for player in bench:
        category = _classify_bench_player(player)
        if category and category in actual:
            actual[category] += 1

    met = all(actual.get(k, 0) >= v for k, v in TARGET_BENCH_SHAPE.items())

    parts = []
    for cat in ("G", "F", "C"):
        have = actual.get(cat, 0)
        want = TARGET_BENCH_SHAPE.get(cat, 0)
        indicator = "OK" if have >= want else "NEED"
        parts.append(f"{cat}: {have}/{want} ({indicator})")

    desc = " | ".join(parts)
    return actual, met, desc


# ---------------------------------------------------------------------------
# Quick smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Minimal fake roster for quick testing
    fake_roster = [
        {"name": "Point Guard A", "positions": ["PG", "G"], "status": "healthy",
         "current_slot": "PG", "yahoo_30day_rank": 5, "yahoo_14day_rank": 4,
         "has_game_today": True},
        {"name": "Shooting Guard B", "positions": ["SG", "G"], "status": "healthy",
         "current_slot": "SG", "yahoo_30day_rank": 12, "yahoo_14day_rank": 10,
         "has_game_today": True},
        {"name": "Small Forward C", "positions": ["SF", "F"], "status": "healthy",
         "current_slot": "SF", "yahoo_30day_rank": 20, "yahoo_14day_rank": 18,
         "has_game_today": False},
        {"name": "Power Forward D", "positions": ["PF", "F"], "status": "Q",
         "current_slot": "PF", "yahoo_30day_rank": 30, "yahoo_14day_rank": 28,
         "has_game_today": True},
        {"name": "Center E", "positions": ["C"], "status": "healthy",
         "current_slot": "C", "yahoo_30day_rank": 8, "yahoo_14day_rank": 7,
         "has_game_today": True},
        {"name": "Swing F", "positions": ["SG", "SF", "G", "F"], "status": "healthy",
         "current_slot": "UTIL", "yahoo_30day_rank": 15, "yahoo_14day_rank": 14,
         "has_game_today": True},
        {"name": "Big G", "positions": ["PF", "C"], "status": "healthy",
         "current_slot": "UTIL", "yahoo_30day_rank": 25, "yahoo_14day_rank": 22,
         "has_game_today": False},
        {"name": "Guard H", "positions": ["PG"], "status": "healthy",
         "current_slot": "BN", "yahoo_30day_rank": 45, "yahoo_14day_rank": 50,
         "has_game_today": True},
        {"name": "Forward I", "positions": ["SF"], "status": "INJ",
         "current_slot": "BN", "yahoo_30day_rank": 60, "yahoo_14day_rank": 55,
         "has_game_today": False},
        {"name": "Center IL", "positions": ["C"], "status": "INJ",
         "current_slot": "IL", "yahoo_30day_rank": 40, "yahoo_14day_rank": 38,
         "has_game_today": False},
        {"name": "Guard J", "positions": ["SG", "G"], "status": "healthy",
         "current_slot": "G", "yahoo_30day_rank": 70, "yahoo_14day_rank": 65,
         "has_game_today": True},
        {"name": "Forward K", "positions": ["PF", "F"], "status": "healthy",
         "current_slot": "F", "yahoo_30day_rank": 80, "yahoo_14day_rank": 75,
         "has_game_today": True},
        {"name": "Center L", "positions": ["C"], "status": "healthy",
         "current_slot": "BN", "yahoo_30day_rank": 90, "yahoo_14day_rank": 85,
         "has_game_today": False},
    ]

    games = {"LAL", "GSW", "BOS", "MIL", "DEN", "PHX"}
    untouchables = {"Point Guard A": 95.0, "Center E": 88.0}

    result = build_lineup(fake_roster, untouchables, games)

    print("\n=== ACTIVE LINEUP ===")
    for p in result["active"]:
        flags = []
        if p["flag_low_rank"]:
            flags.append("LOW_RANK")
        if p.get("flag_injured"):
            flags.append("INJURED")
        if p["is_untouchable"]:
            flags.append("UNTOUCHABLE")
        print(
            f"  {p['slot']:<6} {p['name']:<25} rank30={p['rank_30day']:<4} "
            f"game={'Y' if p['has_game_today'] else 'N'} {' '.join(flags)}"
        )

    print("\n=== BENCH ===")
    for p in result["bench"]:
        print(f"  BN     {p['name']:<25} rank30={p['rank_30day']}")

    print("\n=== IL ===")
    for p in result["on_il"]:
        print(f"  {p['slot']:<6} {p['name']:<25} status={p['injury_status']}")

    actual_shape, met, desc = check_bench_shape(result["bench"])
    print(f"\nBench shape: {desc}  target_met={met}")
