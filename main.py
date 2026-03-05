"""
main.py — Daily entry point for the Fantasy Hoops optimizer.

Intended to run via cron at 2:00 AM every day:
    0 2 * * * /path/to/venv/bin/python /path/to/fantasy-bot/main.py >> /path/to/fantasy-bot/logs/cron.log 2>&1
"""

from __future__ import annotations

import sys
import traceback
from datetime import date, datetime
from dotenv import load_dotenv

load_dotenv()

from optimizer import STABLE_LOW_RANK_THRESHOLD

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _ts() -> str:
    """Return a formatted timestamp string for log lines."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    """Print a timestamped log line to stdout (captured by cron redirect)."""
    print(f"[{_ts()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Number of roster players to flag as "do not drop"
DO_NOT_DROP_COUNT = 6

# Composite weights for do-not-drop ranking: 30% season + 70% 14-day
_DND_ALPHA_SEASON = 0.3
_DND_ALPHA_WINDOW = 0.7
_DND_DEFAULT_RANK = 999
_DND_MIN_HT_ZSCORE = 0.5  # must have season z-score >= 0.5 to be untouchable


def _compute_do_not_drop(roster: list[dict]) -> tuple[dict[str, float], list[dict]]:
    """
    Auto-compute the top roster players as "do not drop".

    Uses a recency-weighted composite: 30% season rank + 70% 14-day rank.
    Fallback chain for missing ranks: 14d → 30d → season → 999.
    Players with HT z-score below 0.5 are excluded (hot-hand only, not stable enough).

    Returns
    -------
    untouchables : {player_name: composite_score} for optimizer bonus
    dnd_list     : list of dicts for the email report, sorted best-first
    """
    candidates = []
    for p in roster:
        slot = p.get("current_slot", "BN")
        if slot in ("IL", "IL+"):
            continue

        # Exclude players with low/negative season z-score (hot-hand only)
        ht_score = p.get("ht_score")
        if ht_score is None or ht_score < _DND_MIN_HT_ZSCORE:
            continue

        szn = p.get("ht_season_rank", _DND_DEFAULT_RANK)

        # Window rank: prefer 14d, fall back to 30d, then season rank
        window = p.get("ht_rank_14d")
        if window is None:
            window = p.get("ht_rank_30d")
        if window is None:
            window = szn  # last resort: use season rank for both

        composite = _DND_ALPHA_SEASON * szn + _DND_ALPHA_WINDOW * window
        candidates.append({
            "name": p["name"],
            "composite": composite,
            "ht_score": p.get("ht_score"),
            "ht_season_rank": p.get("ht_season_rank"),
            "ht_rank_30d": p.get("ht_rank_30d"),
            "ht_rank_14d": p.get("ht_rank_14d"),
        })

    candidates.sort(key=lambda c: c["composite"])
    top_n = candidates[:DO_NOT_DROP_COUNT]

    untouchables = {c["name"]: c["composite"] for c in top_n}
    return untouchables, top_n


def _build_alerts(
    active_lineup: list[dict],
    bench: list[dict],
    il_flags: dict,
    bench_shape_met: bool,
    bench_shape_desc: str,
) -> list[str]:
    """Collect all alert strings to surface in the email."""
    alerts: list[str] = []

    # IL flags
    for entry in il_flags.get("should_move_to_il", []):
        alerts.append(entry["action"])
    for entry in il_flags.get("should_activate_from_il", []):
        alerts.append(entry["action"])

    # Injured active players
    injured_active = [
        p for p in active_lineup
        if p.get("flag_injured") and not p.get("flag_already_alerted")
    ]
    for p in injured_active:
        alerts.append(
            f"{p['name']} is in active slot '{p['slot']}' but has status "
            f"'{p['injury_status']}' — consider sitting or dropping to IL."
        )

    # Low-rank active players
    low_rank_active = [p for p in active_lineup if p.get("flag_low_rank")]
    for p in low_rank_active:
        alerts.append(
            f"{p['name']} (slot {p['slot']}) has a 30-day rank of "
            f"{p.get('rank_30day', '?')} — outside top {STABLE_LOW_RANK_THRESHOLD}."
        )

    # Bench shape
    if not bench_shape_met:
        alerts.append(f"Bench shape target not met: {bench_shape_desc}")

    # No-game active players
    no_game = [
        p for p in active_lineup
        if not p.get("has_game_today") and p.get("injury_status") not in ("INJ", "O")
    ]
    if no_game:
        names = ", ".join(p["name"] for p in no_game)
        alerts.append(f"Active players with NO game today: {names}")

    return alerts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    today = date.today()
    today_str = today.isoformat()

    log("=" * 60)
    log(f"Fantasy Hoops Bot starting — {today_str}")
    log("=" * 60)

    # ------------------------------------------------------------------
    # Step 1: Initialise Yahoo client and fetch data
    # ------------------------------------------------------------------
    log("Step 1/8  Initialising Yahoo Fantasy client …")
    from yahoo_client import YahooFantasyClient

    client = YahooFantasyClient()
    client.refresh_token_if_needed()

    log("Step 1/8  Fetching roster …")
    roster = client.get_my_roster()
    log(f"          Roster: {len(roster)} players.")

    log("Step 1/8  Fetching free agents …")
    free_agents = client.get_free_agents(limit=150)
    log(f"          Free agents: {len(free_agents)} players.")

    # ------------------------------------------------------------------
    # Step 3: Get today's NBA schedule + weekly remaining games
    # ------------------------------------------------------------------
    log("Step 2/8  Fetching today's NBA schedule …")
    from nba_schedule import get_todays_games, get_weekly_remaining_games

    games_today = get_todays_games()
    log(f"          {len(games_today)} teams playing today: {sorted(games_today)}")

    log("Step 2/8  Fetching weekly remaining games …")
    weekly_games = get_weekly_remaining_games()
    log(f"          {len(weekly_games)} teams with remaining games this week.")

    # Annotate roster players with has_game_today and games_remaining
    for player in roster:
        abbr = player.get("team_abbr", "").upper()
        player["has_game_today"] = bool(abbr and abbr in games_today)
        player["games_remaining"] = weekly_games.get(abbr, 0)

    # Annotate FAs with games_remaining
    for fa in free_agents:
        abbr = fa.get("team_abbr", "").upper()
        fa["games_remaining"] = weekly_games.get(abbr, 0)

    # ------------------------------------------------------------------
    # Step 4: Scrape Hashtag Basketball rankings and attach scores
    # ------------------------------------------------------------------
    log("Step 3/8  Fetching Hashtag Basketball rankings …")
    from hashtag_scraper import fetch_hashtag_rankings
    from name_matcher import match_bm_to_yahoo

    ht_data = fetch_hashtag_rankings()
    ht_players = ht_data["players"]
    ht_ranks_30d = ht_data["ranks_30d"]
    ht_ranks_14d = ht_data["ranks_14d"]
    log(f"          HT: {len(ht_players)} players, {len(ht_ranks_30d)} 30d ranks, {len(ht_ranks_14d)} 14d ranks.")

    # Combine roster + FAs for name matching
    all_players = roster + free_agents
    ht_matches = match_bm_to_yahoo(ht_players, all_players)
    log(f"          HT matched to {len(ht_matches)} Yahoo players.")

    # Build season rank lookup: position in z-score list (1-indexed)
    ht_name_to_season_rank = {p["name"]: i + 1 for i, p in enumerate(ht_players)}

    # Attach ht_score, ht_cat_values, ht_season_rank, and ht_rank_30d/14d to roster and FA dicts
    for player in roster + free_agents:
        ht_match = ht_matches.get(player["name"])
        if ht_match:
            player["ht_score"] = ht_match["value"]
            player["ht_cat_values"] = ht_match.get("cat_values", {})
            player["ht_season_rank"] = ht_name_to_season_rank.get(ht_match["name"], 999)
        # HT 30d/14d ranking positions (looked up by matched HT name)
        ht_name = ht_match["name"] if ht_match else player["name"]
        if ht_name in ht_ranks_30d:
            player["ht_rank_30d"] = ht_ranks_30d[ht_name]
        if ht_name in ht_ranks_14d:
            player["ht_rank_14d"] = ht_ranks_14d[ht_name]
        # Compute weekly value for waiver comparisons
        if player.get("ht_score") is not None:
            gr = player.get("games_remaining", 0)
            player["ht_weekly_value"] = player["ht_score"] * gr if gr else 0.0

    ht_roster_count = sum(1 for p in roster if p.get("ht_score") is not None)
    ht_fa_count = sum(1 for p in free_agents if p.get("ht_score") is not None)
    log(f"          HT scores attached: {ht_roster_count} roster, {ht_fa_count} FAs.")

    # ------------------------------------------------------------------
    # Step 4: Compute "do not drop" list from roster rankings
    # ------------------------------------------------------------------
    log("Step 4/8  Computing do-not-drop list …")
    untouchables, dnd_list = _compute_do_not_drop(roster)
    dnd_names = [c["name"] for c in dnd_list]
    log(f"          Do not drop ({len(dnd_names)}): {dnd_names}")

    # ------------------------------------------------------------------
    # Step 5: Run optimizer
    # ------------------------------------------------------------------
    log("Step 5/8  Running lineup optimizer …")
    from optimizer import build_lineup, check_bench_shape

    lineup = build_lineup(roster, untouchables, games_today)
    active_lineup = lineup["active"]
    bench = lineup["bench"]
    on_il = lineup["on_il"]

    bench_shape, bench_shape_met, bench_shape_desc = check_bench_shape(bench)
    log(f"          Lineup built: {len(active_lineup)} active, {len(bench)} bench, {len(on_il)} IL.")
    log(f"          Bench shape: {bench_shape_desc}  target_met={bench_shape_met}")

    # ------------------------------------------------------------------
    # Step 6: Run IL manager
    # ------------------------------------------------------------------
    log("Step 6/8  Checking IL flags …")
    from il_manager import check_il_flags

    il_flags = check_il_flags(roster, bench=bench, free_agents=free_agents, untouchables=untouchables)

    # ------------------------------------------------------------------
    # Step 7: Run waiver scanner
    # ------------------------------------------------------------------
    log("Step 7/8  Scanning waiver wire …")
    from waiver_scanner import scan_active_upgrades, scan_bench_upgrades

    rank_active = lineup["rank_active"]
    rank_bench = lineup["rank_bench"]
    waiver_active = scan_active_upgrades(free_agents, rank_active, untouchables)
    waiver_bench = scan_bench_upgrades(free_agents, rank_bench, untouchables)

    log(f"          Active upgrades: {len(waiver_active)}, Bench upgrades: {len(waiver_bench)}")

    # ------------------------------------------------------------------
    # Step 8: Assemble report and send email
    # ------------------------------------------------------------------
    log("Step 8/8  Assembling report …")

    alerts = _build_alerts(
        active_lineup, bench, il_flags, bench_shape_met, bench_shape_desc
    )
    log(f"          {len(alerts)} alert(s) generated.")

    report_data = {
        "date": today_str,
        "do_not_drop": dnd_list,
        "active_lineup": active_lineup,
        "bench": bench,
        "bench_shape_desc": bench_shape_desc,
        "on_il": on_il,
        "il_flags": il_flags,
        "waiver_active_upgrades": waiver_active,
        "waiver_bench_upgrades": waiver_bench,
        "alerts": alerts,
    }

    log("Step 8/8  Sending email report …")
    from emailer import send_daily_report

    send_daily_report(report_data)

    log("=" * 60)
    log("Fantasy Hoops Bot finished successfully.")
    log("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted by user.")
        sys.exit(0)
    except Exception:
        log("FATAL ERROR — traceback follows:")
        traceback.print_exc()
        sys.exit(1)
