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

**Two cron entry points:**
- `weekly.py` (Mon 1:45 AM) — Selenium scrapes Yahoo "Keys to Success" for MVP untouchables → saves `untouchables.json`
- `main.py` (daily 2:00 AM) — orchestrates all modules in sequence:

```
main.py (9 steps)
 ├─ Load untouchables.json (from Monday's weekly.py)
 ├─ yahoo_client.py    → OAuth2, fetch roster + top 150 free agents with global avg ranks
 ├─ nba_schedule.py    → ESPN API (fallback: nba_api) → today's games + weekly remaining games
 ├─ hashtag_scraper.py → Scrape Hashtag Basketball z-scores + 30d/14d rank positions (cached 20h)
 ├─ name_matcher.py    → Fuzzy-match HT names to Yahoo names, attach ht_score to player dicts
 ├─ optimizer.py       → Two-phase lineup builder (stable 30-day + flex 14-day)
 ├─ il_manager.py      → Flag IL moves (read-only, never mutates roster)
 ├─ waiver_scanner.py  → Compare FAs vs active/bench for upgrade opportunities
 └─ emailer.py         → Build styled HTML email, send via Gmail SMTP
```

**Key design decisions:**
- **Hashtag Basketball (HT) as primary ranking signal.** HT provides full-season z-scores with per-category breakdowns for 9-cat leagues (higher = better). Also scrapes 30-day and 14-day ranking positions (R#) which are used as fallback rank signals. Used as primary sort key in optimizer and scoring metric in waiver scanner. Falls back to HT time-window rank → Yahoo rank when HT z-score data is missing. FAs with negative HT scores (hurt your 9-cat value) are never recommended as upgrades over unscored players.
- **HT scraper** caches to `ht_cache.json` (gitignored) with a 20-hour TTL. Falls back to stale cache on scrape failure. Scrapes via `requests` + `BeautifulSoup` (no Selenium needed). Uses ASP.NET form POSTs to switch time-period views.
- **Name matcher** uses 3-tier strategy: exact normalized match → thefuzz ratio >= 90 → last-name + first-initial. Handles accents, Jr./Sr./III suffixes, C.J./CJ variants.
- **Weekly remaining games** (`get_weekly_remaining_games()`) queries ESPN for each remaining day (today through Sunday). Used to compute `ht_weekly_value = ht_score * games_remaining` for bench waiver comparisons.
- **Two-phase optimizer** uses a stable/flex split:
  - **Stable slots** (C, PG, SG, SF, PF): filled using **30-day avg rank** — consistent, reliable floor players. Flagged if rank > 60 (5 stable × 12 teams).
  - **Flex slots** (C, G, F, UTIL, UTIL): filled using **14-day avg rank** — ride the hot hand. No low-rank flag (streaky players expected).
  - HT z-score is primary signal for both phases; the 30-day vs 14-day split applies to the rank fallback (HT rank → Yahoo rank).
  - Untouchables get a +10,000 HT bonus (or -10,000 rank bonus in rank fallback) to ensure they stay active.
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
