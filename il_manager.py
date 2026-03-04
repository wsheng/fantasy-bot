"""
il_manager.py — Injury List flag logic.

This module only surfaces flags; it does NOT make any roster moves.
Actual moves require a Yahoo API write call which is out of scope for
an automated read-only bot. The generated report will tell you what
to do manually (or you can extend this later).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

# Statuses that indicate a player should be placed on IL
SHOULD_BE_ON_IL: set[str] = {"INJ"}

# Statuses that indicate a player on IL could return
HEALTHY_STATUSES: set[str] = {"healthy", ""}

# Slots considered "active" (not IL and not bench)
ACTIVE_SLOTS: set[str] = {
    "PG", "SG", "G", "SF", "PF", "F", "C", "UTIL",
}

# Slots that are bench
BENCH_SLOTS: set[str] = {"BN"}

# Slots that are IL
IL_SLOTS: set[str] = {"IL", "IL+"}

# Maximum IL slots in this league (set to your league's roster config)
MAX_IL_SLOTS: int = 3


# ---------------------------------------------------------------------------
# Drop recommendation logic
# ---------------------------------------------------------------------------


def recommend_drop_for_activation(
    returning_player: dict,
    bench: list[dict],
    untouchables: dict[str, float],
) -> dict | None:
    """
    Pick the best bench player to drop when activating someone from IL.

    Scoring (worst player = best drop candidate):
    1. Never drop untouchables
    2. Primary: lowest ht_score (no HT score treated as -999)
    3. Tiebreaker: highest yahoo_14day_rank number (worse recent form)
    4. Positional bonus: prefer dropping a player whose positions overlap
       with the returning player (frees the right slot type)

    Returns a dict with drop candidate info, or None if bench is empty.
    """
    if not bench:
        return None

    returning_positions = set(returning_player.get("eligible_positions", []))

    candidates = []
    for p in bench:
        name = p.get("name", "Unknown")

        # Never drop untouchables
        if name in untouchables:
            continue

        ht = p.get("ht_score")
        # No HT score → treat as worst possible (most droppable)
        ht_sort = ht if ht is not None else -999.0

        rank_14 = p.get("ht_rank_14d") or p.get("rank_14day") or 0

        # Positional overlap: small bonus toward dropping (lower = more droppable)
        player_positions = set(p.get("eligible_positions", []))
        pos_overlap = bool(returning_positions & player_positions)

        # Sort key: lower ht_sort → more droppable, higher rank_14 → more droppable,
        # pos_overlap=True → slightly more droppable (subtract 0.5 from ht)
        sort_key = (ht_sort - (0.5 if pos_overlap else 0.0), -rank_14)

        candidates.append((sort_key, p, ht, rank_14, pos_overlap))

    if not candidates:
        return None

    # Sort ascending: worst player first
    candidates.sort(key=lambda x: x[0])
    _, best_drop, ht, rank_14, pos_overlap = candidates[0]

    reason_parts = []
    if ht is None:
        reason_parts.append("no HT score")
    else:
        reason_parts.append(f"HT: {ht:.1f}")
    if rank_14:
        reason_parts.append(f"rank14: {rank_14}")
    if pos_overlap:
        reason_parts.append("position overlap")

    return {
        "name": best_drop.get("name", "Unknown"),
        "positions": best_drop.get("eligible_positions", []),
        "ht_score": ht,
        "rank_14day": rank_14,
        "reason": ", ".join(reason_parts),
    }


# ---------------------------------------------------------------------------
# Pickup recommendation logic (player going ON IL → open roster spot)
# ---------------------------------------------------------------------------

# Statuses that disqualify a free agent from being recommended as a pickup
_FA_DISQUALIFYING_STATUSES: set[str] = {"INJ", "O", "NA", "SUSP", "IL", "IL+"}


def recommend_pickup_for_il_move(
    il_player: dict,
    free_agents: list[dict],
    untouchables: dict[str, float],
) -> list[dict]:
    """
    Recommend up to 3 free agents to pick up when a player is moved to IL.

    Moving a player to IL frees a roster spot, so we suggest the best
    available FA to claim that spot.

    Scoring (best player = best pickup):
    1. Filter out FAs with disqualifying statuses (INJ, O, NA, SUSP, IL)
    2. Primary: highest ht_score (None treated as -999)
    3. Tiebreaker: lowest yahoo_14day_rank (missing treated as 999)
    4. Positional bonus: +0.5 to ht_score for sorting if FA shares any
       eligible_positions with the IL player

    Returns a list of up to 3 dicts with pickup candidate info, or empty
    list if no eligible FAs available.
    """
    if not free_agents:
        return []

    il_positions = set(il_player.get("eligible_positions", []))

    candidates = []
    for fa in free_agents:
        # Skip FAs with disqualifying statuses
        status = fa.get("status", "healthy")
        if status in _FA_DISQUALIFYING_STATUSES:
            continue

        ht = fa.get("ht_score")
        ht_sort = ht if ht is not None else -999.0

        rank_14 = fa.get("yahoo_14day_rank") or fa.get("ht_rank_14d") or 999

        # Positional overlap: small bonus toward picking up
        fa_positions = set(fa.get("eligible_positions", []))
        pos_overlap = bool(il_positions & fa_positions)

        # Sort key: higher ht_sort → better pickup, lower rank_14 → better
        sort_key = (-(ht_sort + (0.5 if pos_overlap else 0.0)), rank_14)

        candidates.append((sort_key, fa, ht, rank_14, pos_overlap))

    if not candidates:
        return []

    # Sort ascending: best pickup first (most negative sort key)
    candidates.sort(key=lambda x: x[0])

    results = []
    for _, best_fa, ht, rank_14, pos_overlap in candidates[:3]:
        reason_parts = []
        if ht is not None:
            reason_parts.append(f"HT: {ht:.1f}")
        else:
            reason_parts.append("no HT score")
        if rank_14 and rank_14 != 999:
            reason_parts.append(f"rank14: {rank_14}")
        if pos_overlap:
            reason_parts.append("position match")

        results.append({
            "name": best_fa.get("name", "Unknown"),
            "positions": best_fa.get("eligible_positions", []),
            "ht_score": ht,
            "yahoo_14day_rank": rank_14,
            "reason": ", ".join(reason_parts),
        })

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_il_flags(
    roster: list[dict],
    bench: list[dict] | None = None,
    free_agents: list[dict] | None = None,
    untouchables: dict[str, float] | None = None,
) -> dict:
    """
    Analyse the roster and return two lists of actionable IL flags.

    Parameters
    ----------
    roster : list of player dicts from YahooFantasyClient.get_my_roster()
             Each dict must have: name, status, current_slot
    bench : optional list of bench player dicts (from optimizer output).
            When provided, activation entries will include a drop recommendation.
    free_agents : optional list of free agent dicts.
            When provided, move-to-IL entries will include pickup recommendations.
    untouchables : optional dict of {player_name: mvp_percent}.
            Untouchable players will never be recommended as drops.

    Returns
    -------
    {
        'should_move_to_il': [
            {'name': str, 'status': str, 'current_slot': str},
            ...
        ],
        'should_activate_from_il': [
            {'name': str, 'current_slot': str},
            ...
        ],
    }

    Notes
    -----
    - should_move_to_il: players with status INJ who are currently
      in an active or bench slot (i.e. wasting a roster spot).
    - should_activate_from_il: players in an IL/IL+ slot whose injury
      status is now healthy (no designation) — they are eligible to return
      to your active or bench roster.
    """
    should_move_to_il: list[dict] = []
    should_activate_from_il: list[dict] = []

    # Count currently occupied IL slots
    il_occupied = sum(1 for p in roster if p.get("current_slot", "") in IL_SLOTS)
    il_available = max(0, MAX_IL_SLOTS - il_occupied)

    for player in roster:
        name = player.get("name", "Unknown")
        status = player.get("status", "healthy")
        slot = player.get("current_slot", "BN")

        # ---------------------------------------------------------------
        # Case 1: Player is NOT on IL but has a hard injury designation
        # ---------------------------------------------------------------
        if status in SHOULD_BE_ON_IL and slot not in IL_SLOTS:
            entry = {
                "name": name,
                "status": status,
                "current_slot": slot,
                "action": f"Move {name} ({slot}) -> IL  [status: {status}]",
                "pickup_candidates": [],
            }

            # Attach pickup recommendations if free agent data is available
            if free_agents is not None:
                pickups = recommend_pickup_for_il_move(
                    player, free_agents, untouchables or {}
                )
                entry["pickup_candidates"] = pickups
                if pickups:
                    entry["action"] += (
                        f" — consider picking up {pickups[0]['name']}"
                        f" ({pickups[0]['reason']})"
                    )

            should_move_to_il.append(entry)

        # ---------------------------------------------------------------
        # Case 2: Player IS on IL but no longer has an injury designation
        # ---------------------------------------------------------------
        elif slot in IL_SLOTS and status in HEALTHY_STATUSES:
            entry = {
                "name": name,
                "current_slot": slot,
                "returning_positions": player.get("eligible_positions", []),
                "returning_ht_score": player.get("ht_score"),
                "action": (
                    f"Activate {name} from {slot} "
                    f"[status: {'healthy' if not status else status}]"
                ),
                "drop_candidate": None,
            }

            # Attach drop recommendation if bench data is available
            if bench is not None:
                drop = recommend_drop_for_activation(
                    player, bench, untouchables or {}
                )
                entry["drop_candidate"] = drop
                if drop:
                    entry["action"] += (
                        f" — consider dropping {drop['name']}"
                        f" ({drop['reason']})"
                    )

            should_activate_from_il.append(entry)

    # ---------------------------------------------------------------
    # Trim IL move suggestions to available slots
    # If IL is already full, we can't move anyone else in
    # ---------------------------------------------------------------
    if len(should_move_to_il) > il_available:
        trimmed = should_move_to_il[:il_available]
        skipped = should_move_to_il[il_available:]
        if skipped:
            print(
                f"[il_manager] IL is full ({il_occupied}/{MAX_IL_SLOTS}) — "
                f"skipping move suggestion for: "
                + ", ".join(p["name"] for p in skipped)
            )
        should_move_to_il = trimmed

    if should_move_to_il:
        print(
            f"[il_manager] {len(should_move_to_il)} player(s) should be moved to IL: "
            + ", ".join(p["name"] for p in should_move_to_il)
        )

    if should_activate_from_il:
        print(
            f"[il_manager] {len(should_activate_from_il)} player(s) can be activated from IL: "
            + ", ".join(p["name"] for p in should_activate_from_il)
        )

    if not should_move_to_il and not should_activate_from_il:
        print("[il_manager] No IL flags — roster looks clean.")

    return {
        "should_move_to_il": should_move_to_il,
        "should_activate_from_il": should_activate_from_il,
    }


def has_il_alerts(il_flags: dict) -> bool:
    """Return True if there are any pending IL actions."""
    return bool(il_flags.get("should_move_to_il") or il_flags.get("should_activate_from_il"))


def summarise_il_flags(il_flags: dict) -> list[str]:
    """
    Return a list of plain-text action strings for use in email alerts.
    """
    actions: list[str] = []

    for entry in il_flags.get("should_move_to_il", []):
        actions.append(entry["action"])

    for entry in il_flags.get("should_activate_from_il", []):
        actions.append(entry["action"])

    return actions


# ---------------------------------------------------------------------------
# Quick smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    fake_roster = [
        # Should move to IL (INJ, sitting on bench)
        {"name": "Injured Bench Star", "status": "INJ", "current_slot": "BN",
         "eligible_positions": ["PG", "SG"]},
        # Should move to IL (INJ, sitting in active spot)
        {"name": "Injured Active Player", "status": "INJ", "current_slot": "PF",
         "eligible_positions": ["SF", "PF"]},
        # Fine — Out status is not IL-eligible
        {"name": "Out For Tonight", "status": "O", "current_slot": "SG"},
        # Should activate (IL slot, but now healthy)
        {"name": "Recovered IL Player", "status": "healthy", "current_slot": "IL",
         "eligible_positions": ["SF", "PF"], "ht_score": 5.0},
        # Fine — healthy and active
        {"name": "Healthy Active", "status": "healthy", "current_slot": "PG"},
        # Fine — questionable but still active (Q is not a hard move-to-IL trigger)
        {"name": "Questionable Guard", "status": "Q", "current_slot": "SG"},
        # Fine — on IL and still injured
        {"name": "Still Injured IL", "status": "INJ", "current_slot": "IL"},
    ]

    fake_bench = [
        {"name": "GG Jackson", "eligible_positions": ["SF", "PF"],
         "ht_score": 1.2, "ht_rank_14d": 70},
        {"name": "Solid Bench Guy", "eligible_positions": ["PG", "SG"],
         "ht_score": 4.5, "ht_rank_14d": 30},
        {"name": "No HT Player", "eligible_positions": ["C"]},
        {"name": "Star Untouchable", "eligible_positions": ["SF"],
         "ht_score": 0.5, "ht_rank_14d": 90},
    ]

    fake_untouchables = {"Star Untouchable": 95.0}

    fake_free_agents = [
        {"name": "FA Guard", "eligible_positions": ["PG", "SG"], "positions": ["PG", "SG"],
         "status": "healthy", "ht_score": 3.5, "yahoo_14day_rank": 45},
        {"name": "FA Forward", "eligible_positions": ["SF", "PF"], "positions": ["SF", "PF"],
         "status": "healthy", "ht_score": 2.8, "yahoo_14day_rank": 55},
        {"name": "FA Center", "eligible_positions": ["C"], "positions": ["C"],
         "status": "healthy", "ht_score": 1.5, "yahoo_14day_rank": 80},
        {"name": "Injured FA", "eligible_positions": ["PG"], "positions": ["PG"],
         "status": "INJ", "ht_score": 5.0, "yahoo_14day_rank": 20},
    ]

    # Test without bench (backward compat)
    print("=== Without bench (backward compat) ===")
    flags = check_il_flags(fake_roster)
    for p in flags["should_activate_from_il"]:
        print(f"  {p['action']}")
        print(f"  drop_candidate: {p.get('drop_candidate')}")

    # Test with bench + untouchables + free agents
    print("\n=== With bench + untouchables + free agents ===")
    flags = check_il_flags(fake_roster, bench=fake_bench, free_agents=fake_free_agents,
                           untouchables=fake_untouchables)

    print("\n--- Should move to IL ---")
    for p in flags["should_move_to_il"]:
        print(f"  {p['action']}")
        pickups = p.get("pickup_candidates", [])
        if pickups:
            for pc in pickups:
                print(f"    -> Pickup: {pc['name']} | positions: {pc['positions']} | "
                      f"HT: {pc['ht_score']} | rank14: {pc['yahoo_14day_rank']} | reason: {pc['reason']}")
        else:
            print(f"    -> No pickup candidates")

    print("\n--- Should activate from IL ---")
    for p in flags["should_activate_from_il"]:
        print(f"  {p['action']}")
        dc = p.get("drop_candidate")
        if dc:
            print(f"    -> Drop: {dc['name']} | positions: {dc['positions']} | "
                  f"HT: {dc['ht_score']} | rank14: {dc['rank_14day']} | reason: {dc['reason']}")
        else:
            print(f"    -> No drop candidate")

    print("\n--- Alert strings ---")
    for line in summarise_il_flags(flags):
        print(f"  {line}")
