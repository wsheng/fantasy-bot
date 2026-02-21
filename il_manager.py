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
SHOULD_BE_ON_IL: set[str] = {"INJ", "O"}

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
# Public API
# ---------------------------------------------------------------------------


def check_il_flags(roster: list[dict]) -> dict:
    """
    Analyse the roster and return two lists of actionable IL flags.

    Parameters
    ----------
    roster : list of player dicts from YahooFantasyClient.get_my_roster()
             Each dict must have: name, status, current_slot

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
    - should_move_to_il: players with status INJ or O who are currently
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
            should_move_to_il.append(
                {
                    "name": name,
                    "status": status,
                    "current_slot": slot,
                    "action": f"Move {name} ({slot}) -> IL  [status: {status}]",
                }
            )

        # ---------------------------------------------------------------
        # Case 2: Player IS on IL but no longer has an injury designation
        # ---------------------------------------------------------------
        elif slot in IL_SLOTS and status in HEALTHY_STATUSES:
            should_activate_from_il.append(
                {
                    "name": name,
                    "current_slot": slot,
                    "action": (
                        f"Activate {name} from {slot} "
                        f"[status: {'healthy' if not status else status}]"
                    ),
                }
            )

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
        {"name": "Injured Bench Star", "status": "INJ", "current_slot": "BN"},
        # Should move to IL (O, sitting in active spot)
        {"name": "Out Active Player", "status": "O", "current_slot": "PF"},
        # Should activate (IL slot, but now healthy)
        {"name": "Recovered IL Player", "status": "healthy", "current_slot": "IL"},
        # Fine — healthy and active
        {"name": "Healthy Active", "status": "healthy", "current_slot": "PG"},
        # Fine — questionable but still active (Q is not a hard move-to-IL trigger)
        {"name": "Questionable Guard", "status": "Q", "current_slot": "SG"},
        # Fine — on IL and still injured
        {"name": "Still Injured IL", "status": "INJ", "current_slot": "IL"},
    ]

    flags = check_il_flags(fake_roster)

    print("\n=== Should move to IL ===")
    for p in flags["should_move_to_il"]:
        print(f"  {p['action']}")

    print("\n=== Should activate from IL ===")
    for p in flags["should_activate_from_il"]:
        print(f"  {p['action']}")

    print("\n=== Alert strings ===")
    for line in summarise_il_flags(flags):
        print(f"  {line}")
