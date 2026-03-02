# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Fantasy Hoops Bot — automated Yahoo Fantasy Basketball daily lineup optimizer. Runs via cron, fetches roster/schedule data, optimizes lineups, scans waivers, checks IL moves, and emails a styled HTML report.

## Running

```bash
# Activate venv first
source /Users/will/Code/fantasy-bot/venv/bin/activate

# Daily optimizer (main entry point)
python main.py

# Monday MVP scraper (runs before main.py)
python weekly.py

# Test individual modules (each has __main__ smoke tests)
python yahoo_client.py      # OAuth + roster/FA fetch
python nba_schedule.py      # Schedule API + weekly remaining games
python hashtag_scraper.py   # Hashtag Basketball scrape (prints top 20 + 30d/14d ranks)
python name_matcher.py      # Fuzzy name matching smoke test
python optimizer.py         # Optimizer with fake data (incl. HT scores)
python waiver_scanner.py    # Waiver scanner with fake data (incl. HT scores)
python il_manager.py        # IL flags with fake data
python emailer.py           # Writes preview to /tmp/report_preview.html
```

No formal test suite — each module has inline `if __name__ == "__main__"` smoke tests.

## Architecture

**One cron entry point:**
- `main.py` (daily 2:00 AM) — orchestrates all modules in sequence:

```
main.py (8 steps)
 ├─ yahoo_client.py    → OAuth2, fetch roster + top 150 free agents with global avg ranks
 ├─ nba_schedule.py    → ESPN API (fallback: nba_api) → today's games + weekly remaining games
 ├─ hashtag_scraper.py → Scrape Hashtag Basketball z-scores + 30d/14d rank positions (cached 20h)
 ├─ name_matcher.py    → Fuzzy-match HT names to Yahoo names, attach ht_score to player dicts
 ├─ _compute_do_not_drop() → Auto-flag top 6 roster players by composite rank
 ├─ optimizer.py       → Three-tier lineup builder (stable/flex/util composites)
 ├─ il_manager.py      → Flag IL moves (read-only, never mutates roster)
 ├─ waiver_scanner.py  → Compare FAs vs rank-based starters/non-starters for upgrades
 └─ emailer.py         → Build styled HTML email, send via Gmail SMTP
```

Note: `weekly.py` still exists for Selenium-based Yahoo MVP scraping but is no longer required.
The do-not-drop list is now computed automatically from HT rankings each run.

**Key design decisions:**
- **Hashtag Basketball (HT) as primary ranking signal.** HT provides full-season z-scores with per-category breakdowns for 9-cat leagues (higher = better). Also scrapes 30-day and 14-day ranking positions (R#). Used as primary signal in optimizer composite ranking and scoring metric in waiver scanner. FAs with negative HT scores (hurt your 9-cat value) are never recommended as upgrades over unscored players.
- **Auto do-not-drop.** Top 6 non-IL roster players by recency-weighted composite (30% season rank + 70% 14-day rank, fallback chain: 14d → 30d → season → 999). Replaces the old `weekly.py` MVP scraping. Do-not-drop players get optimizer bonus and are filtered out of waiver drop recommendations.
- **HT scraper** caches to `ht_cache.json` (gitignored) with a 20-hour TTL. Falls back to stale cache on scrape failure. Scrapes via `requests` + `BeautifulSoup` (no Selenium needed). Uses ASP.NET form POSTs to switch time-period views.
- **Name matcher** uses 3-tier strategy: exact normalized match → thefuzz ratio >= 90 (or >= 85 with team match) → last-name + first-initial (team-preferred). Handles accents, Jr./Sr./III suffixes, C.J./CJ variants, and nickname variants (Nic/Nicolas) via team-validated fuzzy threshold.
- **Weekly remaining games** (`get_weekly_remaining_games()`) queries ESPN for each remaining day (today through Sunday). Used to compute `ht_weekly_value = ht_score * games_remaining` for bench waiver comparisons.
- **Three-tier optimizer + game-day swaps** uses a weighted composite ranking: `composite = α × season_rank + (1-α) × window_rank` (lower = better).
  - **Stable slots** (PG, SG, SF, PF, C1): `α=0.4 season + 0.6 × 30-day rank` — consistent, reliable floor. Flagged if yahoo 30-day rank > 60.
  - **Flex slots** (G, F, C2): `α=0.4 season + 0.6 × 14-day rank` — moderate recency bias.
  - **Util slots** (UTIL × 2, bench): `α=0.2 season + 0.8 × 14-day rank` — ride the hot hand.
  - **Season rank** = position in HT z-score list (attached as `ht_season_rank` by main.py). Window rank = HT 30d/14d rank → Yahoo rank → 999 fallback chain.
  - **Game-day swaps**: after filling by rank, bench players with games swap into active slots of players without games, following swap priority: **UTIL first → G/F/C2 → PG/SG/SF/PF/C1 last**. Core starters stay in place unless no other option exists.
  - **Rank-based lineup snapshot**: before game-day swaps, the optimizer snapshots `rank_active` (5 stable starters) and `rank_bench` (5 flex/util active + 3 bench = 8 non-starters). Waiver scanner compares FAs against these rank-based lists, not the post-swap lineup. Active upgrades only target starter slots; bench upgrades cover the rest.
  - **Display order**: active lineup sorted PG → SG → G → SF → PF → F → C → C → UTIL × 2 (Yahoo website order).
  - Untouchables get a −10,000 composite bonus to ensure they stay active.
- **yahoo_client.py** is the largest/most complex module (~640 lines). It paginates Yahoo's `sort=AR` endpoint for global player rankings, fetches GP/MPG stats separately.
- **9-cat league stats:** FG%, FT%, 3PTM, PTS, REB, AST, ST, BLK, TO (configured in `LEAGUE_CAT_IDS`)
- **NBA schedule** uses a fallback chain: ESPN scoreboard API → nba_api → empty set
- **IL manager** only flags moves, never executes them
- **Roster structure:** 10 active (PG, SG, G, SF, PF, F, C, C, UTIL, UTIL), 3 BN, up to 3 IL

## Credentials & Secrets

All credentials live in `.env` and `oauth2.json` (both gitignored, file permissions 0o600). Never commit these. `untouchables.json`, `bm_cache.json`, and `ht_cache.json` are also gitignored (auto-generated).

Key env vars: `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`, `YAHOO_LEAGUE_ID`, `YAHOO_TEAM_NAME`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL`, `YAHOO_USERNAME`, `YAHOO_PASSWORD`.

## Dependencies

`requirements.txt`: yahoo-fantasy-api, yahoo-oauth, selenium, webdriver-manager, nba_api, python-dotenv, requests, beautifulsoup4, thefuzz, python-Levenshtein. Python 3.9+.
