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
python nba_schedule.py      # Schedule API test
python optimizer.py         # Optimizer with fake data
python waiver_scanner.py    # Waiver scanner with fake data
python il_manager.py        # IL flags with fake data
python emailer.py           # Writes preview to /tmp/report_preview.html
```

No formal test suite — each module has inline `if __name__ == "__main__"` smoke tests.

## Architecture

**Two cron entry points:**
- `weekly.py` (Mon 1:45 AM) — Selenium scrapes Yahoo "Keys to Success" for MVP untouchables → saves `untouchables.json`
- `main.py` (daily 2:00 AM) — orchestrates all modules in sequence:

```
main.py
 ├─ Load untouchables.json (from Monday's weekly.py)
 ├─ yahoo_client.py    → OAuth2, fetch roster + top 150 free agents with global avg ranks
 ├─ nba_schedule.py    → ESPN API (fallback: nba_api) → set of teams playing today
 ├─ optimizer.py       → Greedy lineup builder (most-restrictive slots first)
 ├─ il_manager.py      → Flag IL moves (read-only, never mutates roster)
 ├─ waiver_scanner.py  → Compare FAs vs active/bench for upgrade opportunities
 └─ emailer.py         → Build styled HTML email, send via Gmail SMTP
```

**Key design decisions:**
- **Greedy optimizer** fills slots C→PG→SG→SF→PF→G→F→UTIL (most restrictive first). Untouchables get a -10,000 rank bonus to ensure they stay active.
- **yahoo_client.py** is the largest/most complex module (~840 lines). It paginates Yahoo's `sort=AR` endpoint for global player rankings, fetches GP/MPG stats separately, and computes category-average ranks to match Yahoo's displayed rank.
- **9-cat league stats:** FG%, FT%, 3PTM, PTS, REB, AST, ST, BLK, TO (configured in `LEAGUE_CAT_IDS`)
- **NBA schedule** uses a fallback chain: ESPN scoreboard API → nba_api → empty set
- **IL manager** only flags moves, never executes them
- **Roster structure:** 10 active (PG, SG, G, SF, PF, F, C, C, UTIL, UTIL), 3 BN, up to 3 IL

## Credentials & Secrets

All credentials live in `.env` and `oauth2.json` (both gitignored, file permissions 0o600). Never commit these. `untouchables.json` is also gitignored (auto-generated).

Key env vars: `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`, `YAHOO_LEAGUE_ID`, `YAHOO_TEAM_NAME`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL`, `YAHOO_USERNAME`, `YAHOO_PASSWORD`.

## Dependencies

`requirements.txt`: yahoo-fantasy-api, yahoo-oauth, selenium, webdriver-manager, nba_api, python-dotenv, requests, beautifulsoup4. Python 3.11+.
