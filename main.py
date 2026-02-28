"""
main.py — Daily entry point for the Fantasy Hoops optimizer.

Intended to run via cron at 2:00 AM every day:
    0 2 * * * /path/to/venv/bin/python /path/to/fantasy-bot/main.py >> /path/to/fantasy-bot/logs/cron.log 2>&1

On Mondays it also loads the untouchables scraped by weekly.py.
"""

from __future__ import annotations

import json
import os
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


def _load_untouchables() -> dict[str, float]:
    """
    Load untouchables.json.

    Returns a {player_name: mvp_percent} dict.
    Falls back to empty dict if the file is missing or malformed.
    """
    path = os.path.join(os.path.dirname(__file__), "untouchables.json")
    if not os.path.exists(path):
        log("untouchables.json not found — running without untouchables.")
        return {}

    try:
        with open(path) as fh:
            data = json.load(fh)
        result = {
            entry["name"]: entry.get("mvp_percent", 0.0)
            for entry in data.get("untouchables", [])
        }
        log(f"Loaded {len(result)} untouchables: {list(result.keys())}")
        return result
    except (json.JSONDecodeError, KeyError) as exc:
        log(f"WARNING: Could not parse untouchables.json: {exc} — using empty dict.")
        return {}


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
    is_monday = today.weekday() == 0  # 0 = Monday

    log("=" * 60)
    log(f"Fantasy Hoops Bot starting — {today_str}")
    if is_monday:
        log("It's Monday — untouchables will be included in the report.")
    log("=" * 60)

    # ------------------------------------------------------------------
    # Step 1: Load untouchables
    # ------------------------------------------------------------------
    log("Step 1/9  Loading untouchables …")
    untouchables = _load_untouchables()

    # ------------------------------------------------------------------
    # Step 2: Initialise Yahoo client and fetch data
    # ------------------------------------------------------------------
    log("Step 2/9  Initialising Yahoo Fantasy client …")
    from yahoo_client import YahooFantasyClient

    client = YahooFantasyClient()
    client.refresh_token_if_needed()

    log("Step 2/9  Fetching roster …")
    roster = client.get_my_roster()
    log(f"          Roster: {len(roster)} players.")

    log("Step 2/9  Fetching free agents …")
    free_agents = client.get_free_agents(limit=150)
    log(f"          Free agents: {len(free_agents)} players.")

    # ------------------------------------------------------------------
    # Step 3: Get today's NBA schedule + weekly remaining games
    # ------------------------------------------------------------------
    log("Step 3/9  Fetching today's NBA schedule …")
    from nba_schedule import get_todays_games, get_weekly_remaining_games

    games_today = get_todays_games()
    log(f"          {len(games_today)} teams playing today: {sorted(games_today)}")

    log("Step 3/9  Fetching weekly remaining games …")
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
    log("Step 4/9  Fetching Hashtag Basketball rankings …")
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

    # Attach ht_score, ht_cat_values, and ht_rank_30d/14d to roster and FA dicts
    for player in roster + free_agents:
        ht_match = ht_matches.get(player["name"])
        if ht_match:
            player["ht_score"] = ht_match["value"]
            player["ht_cat_values"] = ht_match.get("cat_values", {})
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
    # Step 5: Run optimizer
    # ------------------------------------------------------------------
    log("Step 5/9  Running lineup optimizer …")
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
    log("Step 6/9  Checking IL flags …")
    from il_manager import check_il_flags

    il_flags = check_il_flags(roster)

    # ------------------------------------------------------------------
    # Step 7: Run waiver scanner
    # ------------------------------------------------------------------
    log("Step 7/9  Scanning waiver wire …")
    from waiver_scanner import scan_active_upgrades, scan_bench_upgrades

    waiver_active = scan_active_upgrades(free_agents, active_lineup, untouchables)
    waiver_bench = scan_bench_upgrades(free_agents, bench, untouchables)
    log(f"          Active upgrades: {len(waiver_active)}, Bench upgrades: {len(waiver_bench)}")

    # ------------------------------------------------------------------
    # Step 8: Assemble report and send email
    # ------------------------------------------------------------------
    log("Step 8/9  Assembling report …")

    alerts = _build_alerts(
        active_lineup, bench, il_flags, bench_shape_met, bench_shape_desc
    )
    log(f"          {len(alerts)} alert(s) generated.")

    # Convert untouchables dict to list for the report
    untouchables_list = [
        {"name": name, "mvp_percent": pct} for name, pct in untouchables.items()
    ]

    report_data = {
        "date": today_str,
        "untouchables": untouchables_list,
        "active_lineup": active_lineup,
        "bench": bench,
        "bench_shape_desc": bench_shape_desc,
        "on_il": on_il,
        "il_flags": il_flags,
        "waiver_active_upgrades": waiver_active,
        "waiver_bench_upgrades": waiver_bench,
        "alerts": alerts,
    }

    log("Step 9/9  Sending email report …")
    from emailer import send_daily_report

    send_daily_report(report_data, is_monday=is_monday)

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
