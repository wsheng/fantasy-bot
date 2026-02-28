"""
optimizer.py — Daily lineup optimizer.

Uses a two-phase greedy approach + game-day swap optimization:
  Phase 1 (Stable): Fill PG, SG, SF, PF, C using 30-day avg rank
          — consistent, reliable floor players.
  Phase 2 (Flex):   Fill G, F, C, UTIL, UTIL using 14-day avg rank
          — ride the hot hand for flexible slots.
  Phase 3 (Swaps):  Swap bench players with games into active slots of
          players without games, prioritizing slot expendability:
            UTIL → G/F/C2 → PG/SG/SF/PF/C1
          This keeps core starters stable and only displaces them as
          a last resort.

Hashtag Basketball (HT) z-score is the primary signal for both phases
when available; the 30-day vs 14-day split applies to the rank fallback
(HT rank, then Yahoo rank).
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

# Two-phase fill: stable slots use 30-day rank, flex slots use 14-day rank.
# Each phase fills most-restrictive first within its group.
STABLE_FILL_ORDER: list[tuple[str, int]] = [
    ("C", 1), ("PG", 1), ("SG", 1), ("SF", 1), ("PF", 1),
]
FLEX_FILL_ORDER: list[tuple[str, int]] = [
    ("C", 1), ("G", 1), ("F", 1), ("UTIL", 2),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _player_eligible_for_slot(player: dict, slot: str) -> bool:
    """Return True if the player can fill the given slot."""
    eligible_positions = player.get("positions", [])
    slot_requires = SLOT_ELIGIBILITY.get(slot, [])
    return any(pos in slot_requires for pos in eligible_positions)


def _rank_sort_key(player: dict, untouchables: dict[str, float], *, use_14day: bool = False) -> tuple:
    """
    Sort key for player selection (lower = better).

    When an HT z-score is available, use its negation (higher = better,
    so negate for ascending sort). Fall back to rank otherwise:
      - use_14day=False (stable slots): HT 30d rank → Yahoo 30-day rank
      - use_14day=True  (flex slots):   HT 14d rank → Yahoo 14-day rank

    Untouchables always sort first via a large bonus.
    """
    is_untouchable = player["name"] in untouchables
    ht_score = player.get("ht_score")

    if ht_score is not None:
        # HT z-score: higher is better, so negate. Untouchable bonus: +10_000.
        effective = -(ht_score + (10_000 if is_untouchable else 0))
        return (effective,)
    else:
        # Fallback: HT time-window rank if available, else Yahoo rank
        if use_14day:
            rank = player.get("ht_rank_14d", player.get("yahoo_14day_rank", 999))
        else:
            rank = player.get("ht_rank_30d", player.get("yahoo_30day_rank", 999))
        bonus = -10_000 if is_untouchable else 0
        return (rank + bonus,)


def _best_player_for_slot(
    slot: str,
    candidates: list[dict],
    untouchables: dict[str, float],
    games_today: set[str],
    use_14day: bool = False,
) -> Optional[dict]:
    """
    Pick the best unassigned candidate for `slot` by pure rank.

    Ignores game-today status — game-day swaps are handled in a
    post-processing step that respects slot swap priority.

    use_14day: when True, rank by 14-day avg (flex slots); otherwise 30-day (stable slots).
    """
    eligible = [
        p for p in candidates if _player_eligible_for_slot(p, slot)
    ]
    if not eligible:
        return None

    eligible.sort(key=lambda p: _rank_sort_key(p, untouchables, use_14day=use_14day))
    return eligible[0]


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

    def _fill_slots(fill_order: list[tuple[str, int]], use_14day: bool, is_stable: bool) -> None:
        for slot, count in fill_order:
            for _ in range(count):
                chosen = _best_player_for_slot(
                    slot, unassigned, untouchables, games_today,
                    use_14day=use_14day,
                )
                if chosen is None:
                    continue

                unassigned.remove(chosen)

                rank_30 = chosen.get("yahoo_30day_rank", 999)
                rank_14 = chosen.get("yahoo_14day_rank", 999)
                status = chosen.get("status", "healthy")

                # Stable slots flag on 30-day rank > 60; flex slots don't flag
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
                    "slot_type": "stable" if is_stable else "flex",
                }
                if chosen.get("ht_score") is not None:
                    entry["ht_score"] = chosen["ht_score"]
                assigned_active.append(entry)

    _fill_slots(STABLE_FILL_ORDER, use_14day=False, is_stable=True)
    _fill_slots(FLEX_FILL_ORDER,   use_14day=True,  is_stable=False)

    # ------------------------------------------------------------------
    # Game-day swap optimization
    # ------------------------------------------------------------------
    # Swap bench players who have games into active slots of players who
    # don't, following SWAP_PRIORITY (UTIL first, then G/F/C2, then
    # PG/SG/SF/PF/C1).  Preserves core starter stability.
    # ------------------------------------------------------------------

    HARD_OUT = {"INJ", "O", "NA"}

    # Bench players with a game today, playable status, sorted best-first
    bench_with_game = sorted(
        [p for p in unassigned
         if p.get("has_game_today") and p.get("status") not in HARD_OUT],
        key=lambda p: _rank_sort_key(p, untouchables),
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
                best_swap_idx = max(
                    candidates,
                    key=lambda i: _rank_sort_key(assigned_active[i], untouchables),
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

            assigned_active[best_swap_idx] = new_entry

            # Move displaced player back to unassigned pool
            # Reconstruct the original player dict from the displaced entry
            unassigned.remove(bench_player)
            unassigned.append({
                "name": displaced["name"],
                "positions": displaced.get("positions", []),
                "status": displaced["injury_status"],
                "has_game_today": displaced["has_game_today"],
                "is_untouchable": displaced["is_untouchable"],
                "yahoo_30day_rank": displaced["rank_30day"],
                "yahoo_14day_rank": displaced["rank_14day"],
                **({"ht_score": displaced["ht_score"]} if "ht_score" in displaced else {}),
            })

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
         "has_game_today": True, "ht_score": 8.5},
        {"name": "Shooting Guard B", "positions": ["SG", "G"], "status": "healthy",
         "current_slot": "SG", "yahoo_30day_rank": 12, "yahoo_14day_rank": 10,
         "has_game_today": True, "ht_score": 5.2},
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
        bm = f"ht={p.get('ht_score', '—')}" if 'ht_score' in p else ""
        phase = p.get("slot_type", "?")
        rank_display = f"rank30={p['rank_30day']:<4}" if phase == "stable" else f"rank14={p['rank_14day']:<4}"
        print(
            f"  {p['slot']:<6} [{phase:<6}] {p['name']:<25} {rank_display} "
            f"game={'Y' if p['has_game_today'] else 'N'} {bm} {' '.join(flags)}"
        )

    print("\n=== BENCH ===")
    for p in result["bench"]:
        print(f"  BN     {p['name']:<25} rank30={p['rank_30day']}")

    print("\n=== IL ===")
    for p in result["on_il"]:
        print(f"  {p['slot']:<6} {p['name']:<25} status={p['injury_status']}")

    actual_shape, met, desc = check_bench_shape(result["bench"])
    print(f"\nBench shape: {desc}  target_met={met}")
