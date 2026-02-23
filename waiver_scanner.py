"""
waiver_scanner.py — Free-agent upgrade analysis.

Compares available free agents against your current active lineup and
bench to surface actionable waiver-wire pickups.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# FA qualification thresholds
# ---------------------------------------------------------------------------

# Shared filters for active-upgrade candidates
FA_MAX_RANK_30 = 96      # must be inside top 96 by 30-day rank
FA_MIN_MPG = 28.0        # must average 28+ minutes per game
FA_MIN_GAMES_30 = 5      # must have played 5+ games in last 30 days

# Bench upgrade uses 14-day rank instead of 30-day
FA_MAX_RANK_14 = 96

# Injury statuses that disqualify a FA from consideration
# "O" = Out, "INJ" = Injured (hard out), "NA" = not available, "SUSP" = suspended
# "IL" = on another team's IL (Yahoo sometimes surfaces these)
DISQUALIFY_STATUSES = {"INJ", "O", "NA", "SUSP", "IL"}

# Slot eligibility (same as optimizer.py — kept local to avoid circular import)
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fa_qualifies_active(fa: dict) -> bool:
    """Return True if a FA meets the baseline criteria for active upgrades."""
    if fa.get("status", "healthy") in DISQUALIFY_STATUSES:
        return False
    if fa.get("yahoo_30day_rank", 999) > FA_MAX_RANK_30:
        return False
    # Only apply MPG/games filters when we actually have data (non-zero means known)
    mpg = fa.get("mpg", 0.0)
    if mpg > 0 and mpg < FA_MIN_MPG:
        return False
    games = fa.get("games_last_30", 0)
    if games > 0 and games < FA_MIN_GAMES_30:
        return False
    return True


def _fa_qualifies_bench(fa: dict) -> bool:
    """Return True if a FA meets the baseline criteria for bench upgrades."""
    if fa.get("status", "healthy") in DISQUALIFY_STATUSES:
        return False
    if fa.get("yahoo_14day_rank", 999) > FA_MAX_RANK_14:
        return False
    mpg = fa.get("mpg", 0.0)
    if mpg > 0 and mpg < FA_MIN_MPG:
        return False
    games = fa.get("games_last_30", 0)
    if games > 0 and games < FA_MIN_GAMES_30:
        return False
    return True


def _shares_slot_eligibility(fa_positions: list[str], slot: str) -> bool:
    """
    Return True if the FA can fill `slot` (i.e. they share at least one
    position with what the slot accepts).
    """
    slot_accepts = SLOT_ELIGIBILITY.get(slot, [])
    return any(pos in slot_accepts for pos in fa_positions)


def _bench_category(positions: list[str]) -> Optional[str]:
    """Classify a player into bench target category: G, F, or C."""
    if "C" in positions:
        return "C"
    if any(p in positions for p in ("SF", "PF", "F")):
        return "F"
    if any(p in positions for p in ("PG", "SG", "G")):
        return "G"
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_active_upgrades(
    free_agents: list[dict],
    active_lineup: list[dict],
    untouchables: dict[str, float],
) -> list[dict]:
    """
    Find free agents that would upgrade a current active-lineup spot.

    Parameters
    ----------
    free_agents   : from YahooFantasyClient.get_free_agents()
    active_lineup : result['active'] from optimizer.build_lineup()
    untouchables  : {player_name: mvp_percent}

    Returns
    -------
    List of upgrade-opportunity dicts, sorted best-improvement-first:
        fa_name, fa_positions, fa_30day_rank, fa_mpg,
        replace_player_name, replace_player_rank, replace_slot,
        rank_improvement (positive = FA is better),
        is_untouchable_replace
    """
    # Filter qualifying FAs
    qualified_fas = [fa for fa in free_agents if _fa_qualifies_active(fa)]
    print(f"[waiver_scanner] {len(qualified_fas)} FAs qualify for active-upgrade check.")

    opportunities: list[dict] = []

    for fa in qualified_fas:
        fa_positions = fa.get("positions", [])
        fa_rank_30 = fa.get("yahoo_30day_rank", 999)
        fa_mpg = fa.get("mpg", 0.0)
        fa_bm = fa.get("bm_score")

        # Find every active slot the FA could potentially fill
        for active_player in active_lineup:
            slot = active_player.get("slot", "")
            if not _shares_slot_eligibility(fa_positions, slot):
                continue

            current_bm = active_player.get("bm_score")
            current_rank = active_player.get("rank_30day", 999)

            # Compare using BM when both sides have it; fall back to rank
            if fa_bm is not None and current_bm is not None:
                score_improvement = fa_bm - current_bm
            elif fa_bm is not None and fa_bm <= 0:
                # FA has a negative BM score — don't recommend over unscored player
                continue
            else:
                # Both unscored or only current has BM: use rank (lower=better)
                score_improvement = current_rank - fa_rank_30

            if score_improvement <= 0:
                continue  # FA is not better than current occupant

            opp = {
                    "fa_name": fa["name"],
                    "fa_positions": fa_positions,
                    "fa_30day_rank": fa_rank_30,
                    "fa_mpg": fa_mpg,
                    "fa_percent_owned": fa.get("percent_owned", 0.0),
                    "replace_player_name": active_player["name"],
                    "replace_player_rank": current_rank,
                    "replace_slot": slot,
                    "rank_improvement": round(score_improvement, 2),
                    "is_untouchable_replace": active_player["name"] in untouchables,
                }
            if fa_bm is not None:
                opp["fa_bm_score"] = fa_bm
            opportunities.append(opp)

    # Deduplicate: keep best opportunity per FA (highest improvement)
    seen_fa: dict[str, dict] = {}
    for opp in opportunities:
        name = opp["fa_name"]
        if name not in seen_fa or opp["rank_improvement"] > seen_fa[name]["rank_improvement"]:
            seen_fa[name] = opp

    deduped = sorted(seen_fa.values(), key=lambda x: -x["rank_improvement"])
    print(f"[waiver_scanner] Found {len(deduped)} active-upgrade opportunities.")
    return deduped


def scan_bench_upgrades(
    free_agents: list[dict],
    bench: list[dict],
    untouchables: dict[str, float],
) -> list[dict]:
    """
    Find free agents that would upgrade a current bench spot.

    Uses 14-day ranks for shorter-term relevance.

    Parameters
    ----------
    free_agents : from YahooFantasyClient.get_free_agents()
    bench       : result['bench'] from optimizer.build_lineup()
    untouchables: {player_name: mvp_percent}

    Returns
    -------
    List of bench-upgrade-opportunity dicts, sorted best-first:
        fa_name, fa_positions, fa_14day_rank, fa_mpg,
        replace_player_name, replace_player_rank,
        position_fit (G/F/C matching target bench shape),
        rank_improvement,
        is_untouchable_replace
    """
    qualified_fas = [fa for fa in free_agents if _fa_qualifies_bench(fa)]
    print(f"[waiver_scanner] {len(qualified_fas)} FAs qualify for bench-upgrade check.")

    opportunities: list[dict] = []

    for fa in qualified_fas:
        fa_positions = fa.get("positions", [])
        fa_rank_14 = fa.get("yahoo_14day_rank", 999)
        fa_mpg = fa.get("mpg", 0.0)
        fa_bench_cat = _bench_category(fa_positions)
        fa_bm = fa.get("bm_score")
        fa_games = fa.get("games_remaining", 0)
        fa_weekly = fa.get("bm_weekly_value")

        # Weekly value for FA: bm_score * games_remaining (if available)
        if fa_weekly is None and fa_bm is not None and fa_games:
            fa_weekly = fa_bm * fa_games

        for bench_player in bench:
            bench_positions = bench_player.get("positions", [])
            bench_rank_14 = bench_player.get("rank_14day", 999)

            # FA must share positional eligibility with this bench player
            if not any(pos in bench_positions for pos in fa_positions):
                # Also allow if they fill the same bench category
                bench_cat = _bench_category(bench_positions)
                if fa_bench_cat != bench_cat:
                    continue

            # Use weekly value if both sides have BM data, else fall back to rank
            bench_bm = bench_player.get("bm_score")
            bench_games = bench_player.get("games_remaining", 0)
            if fa_weekly is not None and bench_bm is not None:
                bench_weekly = bench_bm * bench_games if bench_games else 0
                improvement = fa_weekly - bench_weekly
            elif fa_bm is not None and fa_bm <= 0:
                # FA has a negative BM score — don't recommend over unscored player
                continue
            else:
                improvement = bench_rank_14 - fa_rank_14

            if improvement <= 0:
                continue

            opp = {
                    "fa_name": fa["name"],
                    "fa_positions": fa_positions,
                    "fa_14day_rank": fa_rank_14,
                    "fa_mpg": fa_mpg,
                    "fa_percent_owned": fa.get("percent_owned", 0.0),
                    "replace_player_name": bench_player["name"],
                    "replace_player_rank": bench_rank_14,
                    "position_fit": fa_bench_cat or "?",
                    "rank_improvement": round(improvement, 2),
                    "is_untouchable_replace": bench_player["name"] in untouchables,
                }
            if fa_bm is not None:
                opp["fa_bm_score"] = fa_bm
            if fa_games:
                opp["fa_games_remaining"] = fa_games
            if fa_weekly is not None:
                opp["fa_weekly_value"] = round(fa_weekly, 2)
            opportunities.append(opp)

    # Deduplicate per FA, keep best
    seen_fa: dict[str, dict] = {}
    for opp in opportunities:
        name = opp["fa_name"]
        if name not in seen_fa or opp["rank_improvement"] > seen_fa[name]["rank_improvement"]:
            seen_fa[name] = opp

    deduped = sorted(seen_fa.values(), key=lambda x: -x["rank_improvement"])
    print(f"[waiver_scanner] Found {len(deduped)} bench-upgrade opportunities.")
    return deduped


# ---------------------------------------------------------------------------
# Quick smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    fake_active = [
        {"name": "Bench Guard", "slot": "PG", "rank_30day": 80, "rank_14day": 75,
         "positions": ["PG", "G"], "bm_score": 1.5},
        {"name": "Star Center", "slot": "C", "rank_30day": 3, "rank_14day": 4,
         "positions": ["C"], "bm_score": 10.0},
    ]
    fake_bench = [
        {"name": "Fringe Guard", "slot": "BN", "rank_30day": 110, "rank_14day": 115,
         "positions": ["PG", "G"], "bm_score": 0.5, "games_remaining": 2},
    ]
    fake_fas = [
        {"name": "Hot FA Guard", "positions": ["PG", "G"], "status": "healthy",
         "yahoo_30day_rank": 40, "yahoo_14day_rank": 35, "mpg": 32.0,
         "games_last_30": 12, "percent_owned": 45.0, "bm_score": 4.2, "games_remaining": 3},
        {"name": "Injured FA", "positions": ["SF"], "status": "O",
         "yahoo_30day_rank": 20, "yahoo_14day_rank": 18, "mpg": 34.0,
         "games_last_30": 10, "percent_owned": 30.0},
        {"name": "Low MPG FA", "positions": ["PF"], "status": "healthy",
         "yahoo_30day_rank": 50, "yahoo_14day_rank": 48, "mpg": 22.0,
         "games_last_30": 14, "percent_owned": 10.0},
    ]

    untouchables = {}

    active_upgrades = scan_active_upgrades(fake_fas, fake_active, untouchables)
    print("\n=== ACTIVE UPGRADES ===")
    for u in active_upgrades:
        print(
            f"  Add {u['fa_name']} (rank {u['fa_30day_rank']}) "
            f"-> drop {u['replace_player_name']} (rank {u['replace_player_rank']}) "
            f"[slot={u['replace_slot']}, +{u['rank_improvement']} ranks]"
        )

    bench_upgrades = scan_bench_upgrades(fake_fas, fake_bench, untouchables)
    print("\n=== BENCH UPGRADES ===")
    for u in bench_upgrades:
        print(
            f"  Add {u['fa_name']} (rank14={u['fa_14day_rank']}) "
            f"-> drop {u['replace_player_name']} (rank14={u['replace_player_rank']}) "
            f"[fit={u['position_fit']}, +{u['rank_improvement']} ranks]"
        )
