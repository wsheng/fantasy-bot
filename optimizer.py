"""
optimizer.py — Daily lineup optimizer.

Uses a three-tier greedy approach + game-day swap optimization:
  Phase 1 (Stable): Fill PG, SG, SF, PF, C using weighted composite
          (40% season rank + 60% 30-day rank) — consistent, reliable floor.
  Phase 2 (Flex):   Fill G, F, C using weighted composite
          (40% season rank + 60% 14-day rank) — moderate recency bias.
  Phase 2b (Util):  Fill UTIL×2 using weighted composite
          (20% season rank + 80% 14-day rank) — ride the hot hand.
  Phase 3 (Swaps):  Swap bench players with games into active slots of
          players without games, prioritizing slot expendability:
            UTIL → G/F/C2 → PG/SG/SF/PF/C1
          This keeps core starters stable and only displaces them as
          a last resort.

Composite rank formula: α × season_rank + (1-α) × window_rank (lower = better).
Season rank is derived from HT z-score list position (ht_season_rank).
Window rank uses HT 30d/14d rank → Yahoo rank → 999 as fallback chain.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Tier definitions for composite ranking
# ---------------------------------------------------------------------------

TIER_STABLE = "stable"   # PG, SG, SF, PF, C1
TIER_FLEX = "flex"        # G, F, C2
TIER_UTIL = "util"        # UTIL × 2, bench

# (α_season, α_window, window_field)
# composite = α_season × season_rank + α_window × window_rank
TIER_WEIGHTS: dict[str, tuple[float, float, str]] = {
    TIER_STABLE: (0.4, 0.6, "30d"),
    TIER_FLEX:   (0.4, 0.6, "14d"),
    TIER_UTIL:   (0.2, 0.8, "14d"),
}

DEFAULT_RANK = 999  # Fallback when rank data is missing

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

# Swap priority: when a bench player has a game and an active player doesn't,
# prefer displacing active players in this order (most expendable first).
# Tuples of (slot_name, slot_type) to distinguish C1 (stable) from C2 (flex).
SWAP_PRIORITY: list[tuple[str, str]] = [
    # Tier 1: UTILs — swap these out first
    ("UTIL", "flex"),
    # Tier 2: flex position slots
    ("G", "flex"), ("F", "flex"), ("C", "flex"),
    # Tier 3: core starters — last resort
    ("PG", "stable"), ("SG", "stable"), ("SF", "stable"), ("PF", "stable"), ("C", "stable"),
]

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

# Rank threshold for stable slots (5 stable × 12 teams = top 60)
STABLE_LOW_RANK_THRESHOLD = 60

# Three-tier fill: stable slots, flex slots, util slots.
# Each phase fills most-restrictive first within its group.
STABLE_FILL_ORDER: list[tuple[str, int]] = [
    ("C", 1), ("PG", 1), ("SG", 1), ("SF", 1), ("PF", 1),
]
FLEX_FILL_ORDER: list[tuple[str, int]] = [
    ("C", 1), ("G", 1), ("F", 1),
]
UTIL_FILL_ORDER: list[tuple[str, int]] = [
    ("UTIL", 2),
]

# Display order: Yahoo website standard (PG → SG → G → SF → PF → F → C → C → UTIL × 2)
DISPLAY_ORDER: dict[tuple[str, str], int] = {
    ("PG", "stable"): 0,
    ("SG", "stable"): 1,
    ("G", "flex"):    2,
    ("SF", "stable"): 3,
    ("PF", "stable"): 4,
    ("F", "flex"):    5,
    ("C", "stable"):  6,
    ("C", "flex"):    7,
    ("UTIL", "flex"): 8,   # first UTIL
    ("UTIL", "util"): 8,   # both UTILs at same priority (stable order)
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _player_eligible_for_slot(player: dict, slot: str) -> bool:
    """Return True if the player can fill the given slot."""
    eligible_positions = player.get("positions", [])
    slot_requires = SLOT_ELIGIBILITY.get(slot, [])
    return any(pos in slot_requires for pos in eligible_positions)


def _rank_sort_key(player: dict, untouchables: dict[str, float], *, tier: str = TIER_UTIL) -> tuple:
    """
    Sort key for player selection (lower = better).

    Uses a weighted composite: α × season_rank + (1-α) × window_rank.
    Tier determines the weights and which window (30d vs 14d) to use.

    Untouchables always sort first via a large bonus.
    """
    alpha_season, alpha_window, window = TIER_WEIGHTS[tier]

    # Season rank: position in HT z-score list (attached by main.py)
    season_rank = player.get("ht_season_rank", DEFAULT_RANK)

    # Window rank: HT time-window rank → Yahoo rank → 999
    if window == "30d":
        window_rank = player.get("ht_rank_30d", player.get("yahoo_30day_rank", DEFAULT_RANK))
    else:
        window_rank = player.get("ht_rank_14d", player.get("yahoo_14day_rank", DEFAULT_RANK))

    composite = alpha_season * season_rank + alpha_window * window_rank
    bonus = -10_000 if player["name"] in untouchables else 0
    return (composite + bonus,)


def _best_player_for_slot(
    slot: str,
    candidates: list[dict],
    untouchables: dict[str, float],
    games_today: set[str],
    tier: str = TIER_UTIL,
) -> Optional[dict]:
    """
    Pick the best unassigned candidate for `slot` by composite rank.

    Ignores game-today status — game-day swaps are handled in a
    post-processing step that respects slot swap priority.
    """
    eligible = [
        p for p in candidates if _player_eligible_for_slot(p, slot)
    ]
    if not eligible:
        return None

    eligible.sort(key=lambda p: _rank_sort_key(p, untouchables, tier=tier))
    return eligible[0]


def _tier_for_entry(entry: dict) -> str:
    """Derive the ranking tier from an assigned active entry."""
    slot_type = entry.get("slot_type", "flex")
    if slot_type == "stable":
        return TIER_STABLE
    if entry["slot"] == "UTIL":
        return TIER_UTIL
    return TIER_FLEX


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
    # Phase 1 (Stable) — fill PG, SG, SF, PF, first C using 30-day rank
    # Phase 2 (Flex)   — fill G, F, second C, UTIL×2 using 14-day rank
    # ------------------------------------------------------------------

    def _fill_slots(fill_order: list[tuple[str, int]], tier: str) -> None:
        slot_type = "stable" if tier == TIER_STABLE else "flex"
        is_stable = tier == TIER_STABLE
        for slot, count in fill_order:
            for _ in range(count):
                chosen = _best_player_for_slot(
                    slot, unassigned, untouchables, games_today,
                    tier=tier,
                )
                if chosen is None:
                    continue

                unassigned.remove(chosen)

                rank_30 = chosen.get("yahoo_30day_rank", 999)
                rank_14 = chosen.get("yahoo_14day_rank", 999)
                status = chosen.get("status", "healthy")

                # Stable slots flag on 30-day rank > 60; others don't flag
                flag_low = rank_30 > STABLE_LOW_RANK_THRESHOLD if is_stable else False

                entry = {
                    "name": chosen["name"],
                    "slot": slot,
                    "rank_30day": rank_30,
                    "rank_14day": rank_14,
                    "has_game_today": chosen.get("has_game_today", False),
                    "injury_status": status,
                    "is_untouchable": chosen["name"] in untouchables,
                    "flag_low_rank": flag_low,
                    "flag_injured": status in ("INJ", "O", "Q", "DTD") and slot not in ("IL", "IL+"),
                    "positions": chosen.get("positions", []),
                    "slot_type": slot_type,
                }
                if chosen.get("ht_score") is not None:
                    entry["ht_score"] = chosen["ht_score"]
                if chosen.get("ht_season_rank") is not None:
                    entry["ht_season_rank"] = chosen["ht_season_rank"]
                assigned_active.append(entry)

    _fill_slots(STABLE_FILL_ORDER, tier=TIER_STABLE)
    _fill_slots(FLEX_FILL_ORDER,   tier=TIER_FLEX)
    _fill_slots(UTIL_FILL_ORDER,   tier=TIER_UTIL)

    # Snapshot the rank-based lineup before game-day swaps.
    # Starters (5 stable slots) → active-upgrade comparison.
    # Non-starters (flex/util active + unassigned bench) → bench-upgrade comparison.
    import copy
    rank_active = copy.deepcopy([e for e in assigned_active if e["slot_type"] == "stable"])
    rank_nonstarter_active = [e for e in assigned_active if e["slot_type"] != "stable"]
    rank_bench_players = list(unassigned)  # will be formatted after swaps

    # ------------------------------------------------------------------
    # Game-day swap optimization
    # ------------------------------------------------------------------
    # Swap bench players who have games into active slots of players who
    # don't, following SWAP_PRIORITY (UTIL first, then G/F/C2, then
    # PG/SG/SF/PF/C1).  Preserves core starter stability.
    # ------------------------------------------------------------------

    HARD_OUT = {"INJ", "O", "NA"}

    # Bench players with a game today, playable status, sorted best-first
    # Use TIER_UTIL weights for bench players (most recency-biased)
    bench_with_game = sorted(
        [p for p in unassigned
         if p.get("has_game_today") and p.get("status") not in HARD_OUT],
        key=lambda p: _rank_sort_key(p, untouchables, tier=TIER_UTIL),
    )

    for bench_player in bench_with_game:
        # Find the worst-ranked active player to displace, trying
        # UTIL first, then G/F/C2, then PG/SG/SF/PF/C1.
        best_swap_idx = None
        for prio_slot, prio_type in SWAP_PRIORITY:
            # Collect all candidates in this tier slot that qualify
            candidates = []
            for idx, active in enumerate(assigned_active):
                if active["slot"] != prio_slot or active["slot_type"] != prio_type:
                    continue
                if active["has_game_today"]:
                    continue
                if not _player_eligible_for_slot(bench_player, prio_slot):
                    continue
                candidates.append(idx)
            if candidates:
                # Pick the worst-ranked (highest rank_sort_key) to displace
                # Use each active entry's own tier for fair comparison
                best_swap_idx = max(
                    candidates,
                    key=lambda i: _rank_sort_key(
                        assigned_active[i], untouchables,
                        tier=_tier_for_entry(assigned_active[i]),
                    ),
                )
                break

        if best_swap_idx is not None:
            displaced = assigned_active[best_swap_idx]
            # Put bench player into the active slot
            rank_30 = bench_player.get("yahoo_30day_rank", 999)
            rank_14 = bench_player.get("yahoo_14day_rank", 999)
            status = bench_player.get("status", "healthy")
            is_stable = displaced["slot_type"] == "stable"

            new_entry = {
                "name": bench_player["name"],
                "slot": displaced["slot"],
                "rank_30day": rank_30,
                "rank_14day": rank_14,
                "has_game_today": True,
                "injury_status": status,
                "is_untouchable": bench_player["name"] in untouchables,
                "flag_low_rank": rank_30 > STABLE_LOW_RANK_THRESHOLD if is_stable else False,
                "flag_injured": status in ("INJ", "O", "Q", "DTD"),
                "positions": bench_player.get("positions", []),
                "slot_type": displaced["slot_type"],
            }
            if bench_player.get("ht_score") is not None:
                new_entry["ht_score"] = bench_player["ht_score"]
            if bench_player.get("ht_season_rank") is not None:
                new_entry["ht_season_rank"] = bench_player["ht_season_rank"]

            assigned_active[best_swap_idx] = new_entry

            # Move displaced player back to unassigned pool
            # Reconstruct the original player dict from the displaced entry
            unassigned.remove(bench_player)
            displaced_dict = {
                "name": displaced["name"],
                "positions": displaced.get("positions", []),
                "status": displaced["injury_status"],
                "has_game_today": displaced["has_game_today"],
                "is_untouchable": displaced["is_untouchable"],
                "yahoo_30day_rank": displaced["rank_30day"],
                "yahoo_14day_rank": displaced["rank_14day"],
            }
            if "ht_score" in displaced:
                displaced_dict["ht_score"] = displaced["ht_score"]
            if "ht_season_rank" in displaced:
                displaced_dict["ht_season_rank"] = displaced["ht_season_rank"]
            unassigned.append(displaced_dict)

    # ------------------------------------------------------------------
    # Phase 2 — fill bench slots with remaining players
    # ------------------------------------------------------------------

    remaining_players = sorted(
        unassigned,
        key=lambda p: _rank_sort_key(p, untouchables, tier=TIER_UTIL),
    )

    for i, player in enumerate(remaining_players):
        if i >= len(BENCH_SLOTS):
            break  # roster shouldn't have more than 13 players but guard anyway

        rank_30 = player.get("yahoo_30day_rank", 999)
        rank_14 = player.get("yahoo_14day_rank", 999)

        bench_entry = {
                "name": player["name"],
                "slot": "BN",
                "rank_30day": rank_30,
                "rank_14day": rank_14,
                "has_game_today": player.get("has_game_today", False),
                "injury_status": player.get("status", "healthy"),
                "is_untouchable": player["name"] in untouchables,
                "flag_low_rank": False,  # bench players aren't flagged
                "positions": player.get("positions", []),
            }
        if player.get("ht_score") is not None:
            bench_entry["ht_score"] = player["ht_score"]
        if player.get("ht_season_rank") is not None:
            bench_entry["ht_season_rank"] = player["ht_season_rank"]
        assigned_bench.append(bench_entry)

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

    # Format rank-based non-starters for waiver comparison.
    # Includes flex/util active slots + actual bench players.
    rank_bench: list[dict] = list(rank_nonstarter_active)  # already formatted entries

    # Add bench players (unassigned pool at snapshot time)
    rank_bench_sorted = sorted(
        rank_bench_players,
        key=lambda p: _rank_sort_key(p, untouchables, tier=TIER_UTIL),
    )
    for i, player in enumerate(rank_bench_sorted):
        if i >= len(BENCH_SLOTS):
            break
        rank_30 = player.get("yahoo_30day_rank", 999)
        rank_14 = player.get("yahoo_14day_rank", 999)
        bench_entry = {
            "name": player["name"],
            "slot": "BN",
            "rank_30day": rank_30,
            "rank_14day": rank_14,
            "has_game_today": player.get("has_game_today", False),
            "injury_status": player.get("status", "healthy"),
            "is_untouchable": player["name"] in untouchables,
            "flag_low_rank": False,
            "positions": player.get("positions", []),
        }
        if player.get("ht_score") is not None:
            bench_entry["ht_score"] = player["ht_score"]
        if player.get("ht_season_rank") is not None:
            bench_entry["ht_season_rank"] = player["ht_season_rank"]
        rank_bench.append(bench_entry)

    # ------------------------------------------------------------------
    # Sort active lineup by display order (PG → SG → G → SF → PF → F → C → C → UTIL)
    # ------------------------------------------------------------------
    assigned_active.sort(
        key=lambda e: DISPLAY_ORDER.get((e["slot"], e["slot_type"]), 99)
    )

    return {
        "active": assigned_active,
        "bench": assigned_bench,
        "on_il": il_formatted,
        "rank_active": rank_active,
        "rank_bench": rank_bench,
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
         "has_game_today": True, "ht_score": 8.5, "ht_season_rank": 3,
         "ht_rank_30d": 4, "ht_rank_14d": 6},
        {"name": "Shooting Guard B", "positions": ["SG", "G"], "status": "healthy",
         "current_slot": "SG", "yahoo_30day_rank": 12, "yahoo_14day_rank": 10,
         "has_game_today": True, "ht_score": 5.2, "ht_season_rank": 15,
         "ht_rank_30d": 10, "ht_rank_14d": 12},
        {"name": "Small Forward C", "positions": ["SF", "F"], "status": "healthy",
         "current_slot": "SF", "yahoo_30day_rank": 20, "yahoo_14day_rank": 18,
         "has_game_today": False, "ht_season_rank": 25,
         "ht_rank_30d": 22, "ht_rank_14d": 19},
        {"name": "Power Forward D", "positions": ["PF", "F"], "status": "Q",
         "current_slot": "PF", "yahoo_30day_rank": 30, "yahoo_14day_rank": 28,
         "has_game_today": True, "ht_season_rank": 35,
         "ht_rank_30d": 32, "ht_rank_14d": 30},
        {"name": "Center E", "positions": ["C"], "status": "healthy",
         "current_slot": "C", "yahoo_30day_rank": 8, "yahoo_14day_rank": 7,
         "has_game_today": True, "ht_score": 6.0, "ht_season_rank": 10,
         "ht_rank_30d": 9, "ht_rank_14d": 8},
        {"name": "Swing F", "positions": ["SG", "SF", "G", "F"], "status": "healthy",
         "current_slot": "UTIL", "yahoo_30day_rank": 15, "yahoo_14day_rank": 14,
         "has_game_today": True, "ht_season_rank": 20,
         "ht_rank_30d": 16, "ht_rank_14d": 13},
        {"name": "Big G", "positions": ["PF", "C"], "status": "healthy",
         "current_slot": "UTIL", "yahoo_30day_rank": 25, "yahoo_14day_rank": 22,
         "has_game_today": False, "ht_season_rank": 30,
         "ht_rank_30d": 28, "ht_rank_14d": 24},
        {"name": "Guard H", "positions": ["PG"], "status": "healthy",
         "current_slot": "BN", "yahoo_30day_rank": 45, "yahoo_14day_rank": 50,
         "has_game_today": True, "ht_season_rank": 50,
         "ht_rank_30d": 48, "ht_rank_14d": 55},
        {"name": "Forward I", "positions": ["SF"], "status": "INJ",
         "current_slot": "BN", "yahoo_30day_rank": 60, "yahoo_14day_rank": 55,
         "has_game_today": False, "ht_season_rank": 65},
        {"name": "Center IL", "positions": ["C"], "status": "INJ",
         "current_slot": "IL", "yahoo_30day_rank": 40, "yahoo_14day_rank": 38,
         "has_game_today": False},
        {"name": "Guard J", "positions": ["SG", "G"], "status": "healthy",
         "current_slot": "G", "yahoo_30day_rank": 70, "yahoo_14day_rank": 65,
         "has_game_today": True, "ht_season_rank": 75,
         "ht_rank_30d": 72, "ht_rank_14d": 68},
        {"name": "Forward K", "positions": ["PF", "F"], "status": "healthy",
         "current_slot": "F", "yahoo_30day_rank": 80, "yahoo_14day_rank": 75,
         "has_game_today": True, "ht_season_rank": 85,
         "ht_rank_30d": 82, "ht_rank_14d": 78},
        {"name": "Center L", "positions": ["C"], "status": "healthy",
         "current_slot": "BN", "yahoo_30day_rank": 90, "yahoo_14day_rank": 85,
         "has_game_today": False, "ht_season_rank": 95,
         "ht_rank_30d": 92, "ht_rank_14d": 88},
    ]

    games = {"LAL", "GSW", "BOS", "MIL", "DEN", "PHX"}
    untouchables = {"Point Guard A": 95.0, "Center E": 88.0}

    result = build_lineup(fake_roster, untouchables, games)

    print("\n=== ACTIVE LINEUP (display order) ===")
    for p in result["active"]:
        flags = []
        if p["flag_low_rank"]:
            flags.append("LOW_RANK")
        if p.get("flag_injured"):
            flags.append("INJURED")
        if p["is_untouchable"]:
            flags.append("UNTOUCHABLE")
        bm = f"ht={p.get('ht_score', '—')}" if 'ht_score' in p else ""
        tier = _tier_for_entry(p)
        rank_display = f"szn={p.get('ht_season_rank', '?'):<4}"
        print(
            f"  {p['slot']:<6} [{tier:<6}] {p['name']:<25} {rank_display} "
            f"game={'Y' if p['has_game_today'] else 'N'} {bm} {' '.join(flags)}"
        )

    print("\n=== BENCH ===")
    for p in result["bench"]:
        print(f"  BN     {p['name']:<25} rank30={p['rank_30day']}")

    print("\n=== IL ===")
    for p in result["on_il"]:
        print(f"  {p['slot']:<6} {p['name']:<25} status={p['injury_status']}")

    print("\n=== RANK-BASED STARTERS (5 stable, for active-upgrade comparison) ===")
    for p in result["rank_active"]:
        phase = p.get("slot_type", "?")
        print(f"  {p['slot']:<6} [{phase:<6}] {p['name']:<25} game={'Y' if p['has_game_today'] else 'N'}")

    print("\n=== RANK-BASED NON-STARTERS (flex/util/bench, for bench-upgrade comparison) ===")
    for p in result["rank_bench"]:
        slot = p.get("slot", "BN")
        print(f"  {slot:<6} {p['name']:<25} rank30={p['rank_30day']}")

    actual_shape, met, desc = check_bench_shape(result["bench"])
    print(f"\nBench shape: {desc}  target_met={met}")
