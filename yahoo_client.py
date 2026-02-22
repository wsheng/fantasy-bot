"""
yahoo_client.py — Yahoo Fantasy API wrapper.

Data sources used:
  • team.roster()                                        → current slots, eligible positions
  • /players;sort=AR;sort_type=lastmonth                 → global avg rank (30-day / 14-day)
  • /players;player_keys=.../stats;type=…                → stat_id 0 (GP) + stat_id 2 (MIN) → MPG

Rank note: Yahoo's displayed "Current" column uses a proprietary calculation that
cannot be replicated via the API. We use sort=AR sort position as the rank, which
is Yahoo's own average-rank sort order. Both roster and FA ranks come from the same
sort=AR call (no status filter) so they're on the same scale.
"""

import os
from typing import Optional, List
from dotenv import load_dotenv
import yahoo_fantasy_api as yfa
from yahoo_oauth import OAuth2

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INJURY_STATUSES = {"INJ", "O", "Q", "DTD", "GTD", "NA", "SUSP"}

YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"


def _normalize_pos(pos: str) -> str:
    """Normalize Yahoo's mixed-case 'Util' → 'UTIL'."""
    return "UTIL" if pos.lower() == "util" else pos


class YahooFantasyClient:
    """
    Wrapper around yahoo_fantasy_api + direct Yahoo Fantasy API calls.

    Usage:
        client = YahooFantasyClient()
        roster = client.get_my_roster()
        fas    = client.get_free_agents()
    """

    def __init__(self) -> None:
        load_dotenv()
        self.league_id: str = os.environ["YAHOO_LEAGUE_ID"]
        self.team_name: str = os.environ["YAHOO_TEAM_NAME"]

        print("[yahoo_client] Initialising OAuth2 …")
        self.oauth = self._init_oauth()

        print("[yahoo_client] Connecting to Yahoo Fantasy (NBA) …")
        self.game = yfa.Game(self.oauth, "nba")

        self.game_id: str = self.game.game_id()
        self.league_key: str = f"{self.game_id}.l.{self.league_id}"
        print(f"[yahoo_client] League key: {self.league_key}")

        self.league = self.game.to_league(self.league_key)
        print("[yahoo_client] League connected.")

        self.team_display_name: str = ""
        self.team = self._find_my_team()
        print(f"[yahoo_client] Team found: {self.team_display_name}")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _init_oauth(self) -> OAuth2:
        oauth_file = os.path.join(os.path.dirname(__file__), "oauth2.json")
        if not os.path.exists(oauth_file):
            raise FileNotFoundError(
                f"oauth2.json not found at {oauth_file}. "
                "Copy oauth2.json.example → oauth2.json and fill in credentials."
            )

        # Patch oauth2_access_parser to surface Yahoo's real error on bad credentials
        import yahoo_oauth.oauth as _yoauth
        _orig = _yoauth.BaseOAuth.oauth2_access_parser

        def _patched(self_inner, raw_access):
            import json as _j
            body = raw_access.content.decode("utf-8")
            parsed = _j.loads(body)
            if "access_token" not in parsed:
                raise RuntimeError(
                    f"Yahoo token exchange failed (HTTP {raw_access.status_code}):\n"
                    f"  {body}\n\n"
                    "Fix: check consumer_key/secret in oauth2.json, or re-run OAuth flow."
                )
            return _orig(self_inner, raw_access)

        _yoauth.BaseOAuth.oauth2_access_parser = _patched

        sc = OAuth2(None, None, from_file=oauth_file)
        if not sc.token_is_valid():
            print("[yahoo_client] Token expired — refreshing …")
            sc.refresh_access_token()
        return sc

    def refresh_token_if_needed(self) -> None:
        if not self.oauth.token_is_valid():
            print("[yahoo_client] Token expired — refreshing …")
            self.oauth.refresh_access_token()
        else:
            print("[yahoo_client] OAuth2 token is still valid.")

    # ------------------------------------------------------------------
    # Team finder
    # ------------------------------------------------------------------

    def _find_my_team(self):
        result = self.league.get_team(self.team_name)
        if result:
            self.team_display_name = list(result.keys())[0]
            return list(result.values())[0]

        teams = self.league.teams()
        target = self.team_name.lower()
        for team_key, info in teams.items():
            name = info.get("name", "")
            if name.lower() == target or name.lower().startswith(target):
                self.team_display_name = name
                return self.league.to_team(team_key)

        available = [v.get("name", k) for k, v in teams.items()]
        raise ValueError(
            f"Team '{self.team_name}' not found. Available: {available}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_my_roster(self) -> list[dict]:
        """
        Return current roster as a list of player dicts.

        Fields:
            player_id       int
            player_key      str  e.g. '466.p.5161'
            name            str
            positions       list[str]  eligible, normalized ('UTIL' not 'Util')
            status          str  'healthy' | 'INJ' | 'O' | 'Q' | 'GTD' | 'DTD'
            current_slot    str  'PG' | 'BN' | 'IL' | …
            team_abbr       str  e.g. 'LAL'  (for has_game_today lookup)
            yahoo_30day_rank int  True global avg rank (lower = better)
            yahoo_14day_rank int  True global 14-day avg rank
            mpg             float  minutes per game (last 30 days)
            games_last_30   int    games played in last 30 days
            percent_owned   float  0-100
        """
        print("[yahoo_client] Fetching roster …")
        raw_roster = self.team.roster()

        # Build player_keys in Yahoo format  e.g. '466.p.5161'
        player_keys = [f"{self.game_id}.p.{p['player_id']}" for p in raw_roster]

        # 1. Global average rank (sort=AR, no status filter = all players)
        #    30-day: sort_type=lastmonth  |  14-day: sort_type=biweekly
        #    Both roster and FA use the same rank lists for consistency.
        if not hasattr(self, "_cached_ranks_30"):
            print("[yahoo_client]   Fetching global 30-day avg ranks …")
            self._cached_ranks_30 = self._fetch_yahoo_ranked_players("lastmonth", status="", count=400)
            print("[yahoo_client]   Fetching global 14-day avg ranks …")
            self._cached_ranks_14 = self._fetch_yahoo_ranked_players("biweekly",  status="", count=400)

        # Fallback direct fetch for any roster player not captured in top-400
        missing_keys = [k for k in player_keys if k not in self._cached_ranks_30]
        player_info_fallback: dict = {}
        if missing_keys:
            print(f"[yahoo_client]   Fetching direct info for {len(missing_keys)} unranked roster players …")
            player_info_fallback = self._fetch_players_by_keys_info(missing_keys)

        # 2. GP and MPG from raw Yahoo stats API (stat_id 0=GP, 2=total MIN)
        print("[yahoo_client]   Fetching GP/MPG stats …")
        gp_mpg = self._fetch_gp_mpg(player_keys, "lastmonth")

        # 3. percent_owned for informational display
        pct_map: dict[int, float] = {}
        try:
            player_ids = [p["player_id"] for p in raw_roster]
            for entry in self.league.percent_owned(player_ids):
                pid = entry.get("player_id")
                if pid is not None:
                    pct_map[int(pid)] = float(entry.get("percent_owned", 0) or 0)
        except Exception as exc:
            print(f"[yahoo_client] Warning: percent_owned fetch failed: {exc}")

        roster = []
        for player in raw_roster:
            pid = player["player_id"]
            pkey = f"{self.game_id}.p.{pid}"

            raw_status = player.get("status", "")
            status = raw_status if raw_status in INJURY_STATUSES else "healthy"
            slot = _normalize_pos(player.get("selected_position", "BN"))

            eligible = player.get("eligible_positions", [])
            if isinstance(eligible, str):
                eligible = [eligible]
            eligible = [_normalize_pos(p) for p in eligible
                        if p not in ("IL", "IL+", "BN")]
            seen: set = set()
            eligible = [p for p in eligible if not (p in seen or seen.add(p))]

            r30      = self._cached_ranks_30.get(pkey, {})
            r14      = self._cached_ranks_14.get(pkey, {})
            gm       = gp_mpg.get(pkey, {})
            fallback = player_info_fallback.get(pkey, {})

            # team_abbr: prefer ranked result, fall back to direct fetch
            team_abbr = r30.get("team_abbr", "") or fallback.get("team_abbr", "")

            roster.append({
                "player_id":       pid,
                "player_key":      pkey,
                "name":            player.get("name", "Unknown"),
                "positions":       eligible,
                "status":          status,
                "current_slot":    slot,
                "team_abbr":       team_abbr,
                "yahoo_30day_rank": r30.get("rank", 999),
                "yahoo_14day_rank": r14.get("rank", 999),
                "mpg":             gm.get("mpg", 0.0),
                "games_last_30":   gm.get("gp", 0),
                "percent_owned":   pct_map.get(pid, 0.0),
            })

        print(f"[yahoo_client] Roster loaded: {len(roster)} players.")
        return roster

    def get_free_agents(self, limit: int = 150) -> list[dict]:
        """
        Return top free agents with TRUE global avg rank (comparable to roster ranks).

        Strategy:
          1. Reuse the global rank lists from _fetch_yahoo_ranked_players() (same
             source as get_my_roster) so roster and FA ranks are on identical scales.
          2. Fetch available-player keys (status=A) to know who is a FA in this league.
          3. Walk the global list in rank order; keep only FAs until we have `limit`.

        Fields: player_id, player_key, name, positions, status, team_abbr,
                yahoo_30day_rank, yahoo_14day_rank, mpg, games_last_30, percent_owned
        """
        print(f"[yahoo_client] Fetching top {limit} free agents …")

        # Use the same Yahoo sort=AR rankings that get_my_roster() uses.
        if not hasattr(self, "_cached_ranks_30"):
            print("[yahoo_client]   Fetching global 30-day avg ranks …")
            self._cached_ranks_30 = self._fetch_yahoo_ranked_players("lastmonth", status="", count=400)
            print("[yahoo_client]   Fetching global 14-day avg ranks …")
            self._cached_ranks_14 = self._fetch_yahoo_ranked_players("biweekly",  status="", count=400)

        # Identify which player_keys are available (FA) in this league
        print("[yahoo_client]   Fetching available FA keys …")
        fa_keys = self._fetch_fa_keys("lastmonth", count=400)

        # Walk the cached 30-day rank list in rank order, keep only FAs
        ordered = sorted(
            self._cached_ranks_30.items(),
            key=lambda kv: kv[1]["rank"],
        )

        # GP/MPG for FAs
        fa_candidate_keys = [k for k, _ in ordered if k in fa_keys][:limit]
        print("[yahoo_client]   Fetching FA GP/MPG stats …")
        gp_mpg = self._fetch_gp_mpg(fa_candidate_keys, "lastmonth")

        fas = []
        for pkey, info30 in ordered:
            if pkey not in fa_keys:
                continue

            info14 = self._cached_ranks_14.get(pkey, {})
            gm     = gp_mpg.get(pkey, {})

            raw_status = info30.get("injury_status", "")
            status = raw_status if raw_status in INJURY_STATUSES else "healthy"

            eligible = info30.get("positions", [])
            seen: set = set()
            eligible = [p for p in eligible if not (p in seen or seen.add(p))]

            fas.append({
                "player_id":       info30.get("player_id", 0),
                "player_key":      pkey,
                "name":            info30.get("name", "Unknown"),
                "positions":       eligible,
                "status":          status,
                "team_abbr":       info30.get("team_abbr", ""),
                "yahoo_30day_rank": info30.get("rank", 999),
                "yahoo_14day_rank": info14.get("rank", 999),
                "mpg":             gm.get("mpg", 0.0),
                "games_last_30":   gm.get("gp", 0),
                "percent_owned":   info30.get("percent_owned", 0.0),
            })

            if len(fas) >= limit:
                break

        print(f"[yahoo_client] Free agents loaded: {len(fas)} players.")
        return fas

    # ------------------------------------------------------------------
    # Core data-fetching helpers
    # ------------------------------------------------------------------

    def _fetch_fa_keys(self, sort_type: str, count: int = 400) -> set:
        """
        Return the set of player_keys that are available (FA) in this league.

        Uses status=A with sort=AR to paginate through available players.
        Only collects keys — no rank data needed here.
        """
        keys: set = set()
        page_size = 25

        for start in range(0, count, page_size):
            url = (
                f"{YAHOO_API_BASE}/league/{self.league_key}/players"
                f";sort=AR;sort_type={sort_type}"
                f";start={start};count={page_size}"
                f";status=A"
            )
            try:
                resp = self.oauth.session.get(url, params={"format": "json"})
                if not resp.ok:
                    break
                data = resp.json()
                players_blob = data["fantasy_content"]["league"][1]["players"]
                if not isinstance(players_blob, dict):
                    break
                n = int(players_blob.get("count", 0))
            except Exception as exc:
                print(f"[yahoo_client] fa_keys fetch error at start={start}: {exc}")
                break

            for i in range(n):
                try:
                    pdata_list = players_blob[str(i)]["player"][0]
                    pkey = next(
                        (x["player_key"] for x in pdata_list
                         if isinstance(x, dict) and "player_key" in x),
                        None,
                    )
                    if pkey:
                        keys.add(pkey)
                except (KeyError, IndexError):
                    continue

            if n < page_size:
                break

        return keys

    def _fetch_players_by_keys_info(self, player_keys: List[str]) -> dict:
        """
        Fetch team_abbr, name, positions, injury_status for specific player_keys.

        Used as a fallback for roster players who fall outside the top-N of the
        sorted rank endpoint.

        Returns {player_key: {team_abbr, name, positions, injury_status}}
        """
        result: dict = {}
        batch_size = 25

        for i in range(0, len(player_keys), batch_size):
            batch = player_keys[i: i + batch_size]
            keys_str = ",".join(batch)
            url = f"{YAHOO_API_BASE}/league/{self.league_key}/players;player_keys={keys_str}"
            try:
                resp = self.oauth.session.get(url, params={"format": "json"})
                if not resp.ok:
                    print(f"[yahoo_client] player info HTTP {resp.status_code}")
                    continue
                data = resp.json()
                players_blob = data["fantasy_content"]["league"][1]["players"]
                n = int(players_blob.get("count", 0))
            except Exception as exc:
                print(f"[yahoo_client] player info fetch error: {exc}")
                continue

            for j in range(n):
                try:
                    pdata_list = players_blob[str(j)]["player"][0]
                except (KeyError, IndexError):
                    continue

                pkey = ""
                name = ""
                team_abbr = ""
                injury_status = ""
                positions: list = []

                for item in pdata_list:
                    if not isinstance(item, dict):
                        continue
                    if "player_key" in item:
                        pkey = item["player_key"]
                    elif "name" in item:
                        name = item["name"].get("full", "")
                    elif "editorial_team_abbr" in item:
                        team_abbr = item["editorial_team_abbr"]
                    elif "status" in item:
                        injury_status = item["status"]
                    elif "eligible_positions" in item:
                        raw_pos = item["eligible_positions"]
                        if isinstance(raw_pos, list):
                            for ep in raw_pos:
                                if isinstance(ep, dict) and "position" in ep:
                                    pos = _normalize_pos(ep["position"])
                                    if pos not in ("IL", "IL+", "BN") and pos not in positions:
                                        positions.append(pos)

                if pkey:
                    result[pkey] = {
                        "team_abbr":      team_abbr,
                        "name":           name,
                        "positions":      positions,
                        "injury_status":  injury_status,
                    }

        return result

    def _fetch_yahoo_ranked_players(
        self,
        sort_type: str,       # 'lastmonth' | 'biweekly'
        status: str = "",     # ''=all players (global)  'A'=available(FA)  'T'=taken
        count: int = 400,
    ) -> dict:
        """
        Paginate Yahoo's sort=AR endpoint to collect true global avg ranks.

        status="" (no filter) returns ALL players globally sorted — this gives
        true global rank directly comparable across roster and FA players.

        Returns {player_key: {rank, name, team_abbr, injury_status,
                               positions, player_id, percent_owned}}

        The player's rank equals its 1-based position in the sorted response.
        """
        result: dict = {}
        page_size = 25  # Yahoo caps responses at 25 players per page

        for start in range(0, count, page_size):
            url = (
                f"{YAHOO_API_BASE}/league/{self.league_key}/players"
                f";sort=AR;sort_type={sort_type}"
                f";start={start};count={page_size}"
            )
            if status:  # only append status filter when non-empty
                url += f";status={status}"
            try:
                resp = self.oauth.session.get(url, params={"format": "json"})
                if not resp.ok:
                    print(f"[yahoo_client] rank fetch HTTP {resp.status_code} at start={start}")
                    break
                data = resp.json()
                players_blob = data["fantasy_content"]["league"][1]["players"]
                # Yahoo returns a list (usually empty) when the end of results is reached
                if not isinstance(players_blob, dict):
                    break
                n = int(players_blob.get("count", 0))
            except Exception as exc:
                print(f"[yahoo_client] rank fetch error at start={start}: {exc}")
                break

            for i in range(n):
                try:
                    pdata_list = players_blob[str(i)]["player"][0]
                except (KeyError, IndexError):
                    continue

                # Parse the list of field dicts
                pkey = ""
                pid  = 0
                name = ""
                team_abbr = ""
                injury_status = ""
                positions: list[str] = []
                pct_owned = 0.0

                for item in pdata_list:
                    if not isinstance(item, dict):
                        continue
                    if "player_key" in item:
                        pkey = item["player_key"]
                    elif "player_id" in item:
                        try:
                            pid = int(item["player_id"])
                        except (ValueError, TypeError):
                            pass
                    elif "name" in item:
                        name = item["name"].get("full", "")
                    elif "editorial_team_abbr" in item:
                        team_abbr = item["editorial_team_abbr"]
                    elif "status" in item:
                        injury_status = item["status"]
                    elif "eligible_positions" in item:
                        raw_pos = item["eligible_positions"]
                        if isinstance(raw_pos, list):
                            for ep in raw_pos:
                                if isinstance(ep, dict) and "position" in ep:
                                    pos = _normalize_pos(ep["position"])
                                    if pos not in ("IL", "IL+", "BN") and pos not in positions:
                                        positions.append(pos)
                    elif "percent_owned" in item:
                        try:
                            pct_owned = float(item.get("percent_owned", 0) or 0)
                        except (ValueError, TypeError):
                            pass

                if pkey:
                    result[pkey] = {
                        "rank":           start + i + 1,
                        "name":           name,
                        "player_id":      pid,
                        "team_abbr":      team_abbr,
                        "injury_status":  injury_status,
                        "positions":      positions,
                        "percent_owned":  pct_owned,
                    }

            if n < page_size:
                break  # last page

        return result

    def _fetch_gp_mpg(
        self,
        player_keys: List[str],   # e.g. ['466.p.5161', …]
        req_type: str = "lastmonth",
    ) -> dict:
        """
        Return GP and MPG for each player from Yahoo's raw stats API.

        Yahoo stat IDs (global, not league-specific):
            0  → GP   (games played)
            2  → MIN  (total minutes for the period)

        MPG = total_MIN / GP

        Returns {player_key: {'gp': int, 'mpg': float}}
        """
        if not player_keys:
            return {}

        result: dict = {}
        batch_size = 25

        for i in range(0, len(player_keys), batch_size):
            batch = player_keys[i: i + batch_size]
            keys_str = ",".join(batch)
            url = f"{YAHOO_API_BASE}/players;player_keys={keys_str}/stats;type={req_type}"
            try:
                resp = self.oauth.session.get(url, params={"format": "json"})
                if not resp.ok:
                    continue
                data = resp.json()
                players_blob = data["fantasy_content"]["players"]
            except Exception as exc:
                print(f"[yahoo_client] gp_mpg fetch error batch {i}: {exc}")
                continue

            for k, v in players_blob.items():
                if k == "count":
                    continue
                try:
                    player_info = v["player"]
                    # player_info[0] = list of field dicts, player_info[1] = stats dict
                    pkey = ""
                    for field in player_info[0]:
                        if isinstance(field, dict) and "player_key" in field:
                            pkey = field["player_key"]
                            break

                    stats = player_info[1].get("player_stats", {}).get("stats", [])
                    gp = 0
                    total_min = 0.0
                    for stat_entry in stats:
                        s = stat_entry.get("stat", {})
                        sid = str(s.get("stat_id", ""))
                        val = s.get("value", "") or "0"
                        if sid == "0":   # GP
                            try:
                                gp = int(float(val))
                            except (ValueError, TypeError):
                                pass
                        elif sid == "2":  # total MIN
                            try:
                                total_min = float(val)
                            except (ValueError, TypeError):
                                pass

                    mpg = round(total_min / gp, 1) if gp > 0 else 0.0
                    if pkey:
                        result[pkey] = {"gp": gp, "mpg": mpg}
                except (KeyError, IndexError, TypeError):
                    continue

        return result

    @staticmethod
    def _today() -> str:
        from datetime import date
        return date.today().isoformat()


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Yahoo Fantasy Client — smoke test ===\n")
    client = YahooFantasyClient()

    roster = client.get_my_roster()
    print(f"\nMy roster ({len(roster)} players):\n")
    print(f"  {'Name':<25} {'Slot':<5} {'Status':<7} {'Team':<5} {'Rank30':<7} {'Rank14':<7} {'MPG':<6} {'GP'}")
    print("  " + "-" * 70)
    for p in roster:
        print(
            f"  {p['name']:<25} {p['current_slot']:<5} {p['status']:<7} "
            f"{p['team_abbr']:<5} {p['yahoo_30day_rank']:<7} "
            f"{p['yahoo_14day_rank']:<7} {p['mpg']:<6} {p['games_last_30']}"
        )

    fas = client.get_free_agents(limit=20)
    print(f"\nTop 20 free agents (global rank):\n")
    print(f"  {'Name':<25} {'Status':<7} {'Team':<5} {'Rank30':<7} {'Rank14':<7} {'MPG':<6} {'GP'}")
    print("  " + "-" * 70)
    for p in fas:
        print(
            f"  {p['name']:<25} {p['status']:<7} "
            f"{p['team_abbr']:<5} {p['yahoo_30day_rank']:<7} "
            f"{p['yahoo_14day_rank']:<7} {p['mpg']:<6} {p['games_last_30']}"
        )
