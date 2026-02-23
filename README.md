# Fantasy Hoops Bot

A fully automated Yahoo Fantasy Basketball daily lineup optimizer. Each morning at 2 AM it fetches your roster, checks the NBA schedule, scrapes Basketball Monster for per-category 9-cat value scores, optimises your active lineup, scans the waiver wire for upgrades, checks IL moves, and emails you a styled HTML report.

On Mondays it also incorporates the weekly MVP untouchables scraped from Yahoo's "Keys to Success" page.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Yahoo Developer Console Setup](#2-yahoo-developer-console-setup)
3. [OAuth2 First-Run Setup](#3-oauth2-first-run-setup)
4. [Gmail App Password Setup](#4-gmail-app-password-setup)
5. [Installation](#5-installation)
6. [Configuration](#6-configuration)
7. [Running Manually](#7-running-manually)
8. [Cron Setup (Mac)](#8-cron-setup-mac)
9. [File Overview](#9-file-overview)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Prerequisites

- **Python 3.9+** — check with `python3 --version`
- **Google Chrome** installed (for the weekly Selenium scraper)
- A **Yahoo Fantasy Basketball** team in an active league
- A **Gmail** account with App Passwords enabled (see Section 4)
- A **Yahoo Developer** account (free) to create an API application

---

## 2. Yahoo Developer Console Setup

The Yahoo Fantasy API requires an OAuth2 application. Follow these steps exactly:

### a. Go to the Yahoo Developer Portal

Navigate to [https://developer.yahoo.com/apps/](https://developer.yahoo.com/apps/)

### b. Sign in

Use the same Yahoo account that manages your fantasy team. If you do not have a Yahoo Developer account, your regular Yahoo account works — just click "Sign In".

### c. Create a New App

Click **"Create App"** (or "My Apps" → "Create an App").

### d. Fill in App Details

| Field | Value |
|---|---|
| **App Name** | Fantasy Basketball Bot (or anything you like) |
| **App Type** | **Installed Application** (not Web Application) |
| **Callback Domain** | `localhost` |
| **API Permissions** | Check **Fantasy Sports** → select **Read** |

> Important: Select "Installed Application" not "Web Application". Web apps require a publicly accessible redirect URI which complicates the auth flow.

### e. Click "Create App"

### f. Copy Your Credentials

After creation you will see your **Client ID** (also called Consumer Key) and **Client Secret** (also called Consumer Secret). Keep these safe — you will need them in the next steps.

---

## 3. OAuth2 First-Run Setup

The bot uses `yahoo_oauth` which handles token management automatically via a local `oauth2.json` file.

### Step 1: Copy the example file

```bash
cd /Users/will/Code/fantasy-bot
cp oauth2.json.example oauth2.json
```

### Step 2: Fill in your credentials

Open `oauth2.json` in any text editor and replace the placeholder values:

```json
{
  "access_token": "",
  "consumer_key": "YOUR_ACTUAL_CLIENT_ID_HERE",
  "consumer_secret": "YOUR_ACTUAL_CLIENT_SECRET_HERE",
  "refresh_token": "",
  "token_time": 0,
  "token_type": "bearer"
}
```

Leave `access_token`, `refresh_token`, and `token_time` as-is — `yahoo_oauth` will populate these automatically.

### Step 3: Run the client to trigger first-time auth

```bash
python yahoo_client.py
```

On the **first run only**, `yahoo_oauth` will open a browser tab asking you to authorise the application. After you click "Agree", it will redirect to a `localhost` URL. Copy the full URL from your browser address bar and paste it into the terminal prompt.

`yahoo_oauth` will write the access and refresh tokens back to `oauth2.json`. All subsequent runs will silently refresh the token without any browser interaction.

> Note: If your token expires (after ~1 hour of inactivity), the bot automatically calls `refresh_access_token()` before each run.

---

## 4. Gmail App Password Setup

Google requires an App Password (not your regular password) for SMTP access when 2-Step Verification is enabled.

### Step 1: Enable 2-Step Verification

Go to [https://myaccount.google.com/security](https://myaccount.google.com/security) and enable **2-Step Verification** if not already on.

### Step 2: Generate an App Password

1. Go to [https://myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. Select **"Mail"** as the app and **"Mac"** (or "Other") as the device
3. Click **"Generate"**
4. Copy the 16-character password shown (spaces don't matter — remove them)

### Step 3: Add to .env

Paste it as `GMAIL_APP_PASSWORD` in your `.env` file (see Section 6).

> If you do not see "App Passwords" in your account settings, 2-Step Verification is not enabled, or your organisation manages your Google account and has disabled this feature.

---

## 5. Installation

### Clone / copy the project

The project lives at `/Users/will/Code/fantasy-bot/`. If you cloned it elsewhere, adjust paths accordingly.

### Create a virtual environment (recommended)

```bash
cd /Users/will/Code/fantasy-bot
python3 -m venv venv
source venv/bin/activate
```

### Install dependencies

```bash
pip install -r requirements.txt
```

This installs:
- `yahoo-fantasy-api` — Python wrapper for the Yahoo Fantasy Sports API
- `yahoo-oauth` — OAuth2 token management for Yahoo
- `selenium` + `webdriver-manager` — headless Chrome for Monday scraper
- `nba_api` — NBA official stats API (game schedules, player info)
- `python-dotenv` — `.env` file loading
- `requests` + `beautifulsoup4` — HTTP and HTML parsing utilities
- `thefuzz` + `python-Levenshtein` — fuzzy string matching for player names

---

## 6. Configuration

### Step 1: Copy the example env file

```bash
cp .env.example .env
```

### Step 2: Fill in all values

Open `.env` in any text editor:

```bash
# Yahoo API credentials (from Section 2)
YAHOO_CLIENT_ID=dj0yJmk9...          # Your Client ID
YAHOO_CLIENT_SECRET=abc123...         # Your Client Secret

# Your league and team
YAHOO_LEAGUE_ID=11642                 # Found in your league URL
YAHOO_TEAM_NAME=XYZs                  # Exact team name (prefix match is fine)

# Gmail delivery
GMAIL_USER=you@gmail.com
GMAIL_APP_PASSWORD=abcdabcdabcdabcd   # 16-char App Password from Section 4
NOTIFY_EMAIL=you@gmail.com            # Where to send the report

# Yahoo account (for weekly.py Selenium scraper)
YAHOO_USERNAME=you@yahoo.com
YAHOO_PASSWORD=your_yahoo_password
```

**Finding your League ID:** Look at the URL when you're on your league page:
`https://basketball.fantasysports.yahoo.com/nba/11642/` → League ID is `11642`

**Finding your Team Name:** Use the exact name shown in Yahoo, or a unique prefix. The bot does a case-insensitive prefix match.

---

## 7. Running Manually

Make sure your virtual environment is activated first:

```bash
source /Users/will/Code/fantasy-bot/venv/bin/activate
```

### Run the daily optimizer

```bash
python /Users/will/Code/fantasy-bot/main.py
```

This fetches your roster, scrapes Basketball Monster rankings, runs the optimizer, scans waivers, and emails the report.

### Run the Monday MVP scraper manually

```bash
python /Users/will/Code/fantasy-bot/weekly.py
```

This opens a headless browser, logs in to Yahoo, scrapes the Keys to Success page, and saves `untouchables.json`. Run this before `main.py` on Mondays (or let cron handle it — see Section 8).

### Preview the email without sending

```bash
python /Users/will/Code/fantasy-bot/emailer.py
```

This runs the smoke-test at the bottom of `emailer.py` and writes a preview to `/tmp/report_preview.html`. Open it in your browser to see what the email looks like.

### Test individual modules

```bash
# Test Yahoo client / OAuth
python /Users/will/Code/fantasy-bot/yahoo_client.py

# Test NBA schedule fetcher (today's games + weekly remaining)
python /Users/will/Code/fantasy-bot/nba_schedule.py

# Test Basketball Monster scraper (prints top 20, caches to bm_cache.json)
python /Users/will/Code/fantasy-bot/bm_scraper.py

# Test fuzzy name matching with known variants
python /Users/will/Code/fantasy-bot/name_matcher.py

# Test optimizer logic with fake data (incl. BM scores)
python /Users/will/Code/fantasy-bot/optimizer.py

# Test waiver scanner with fake data (incl. BM scores)
python /Users/will/Code/fantasy-bot/waiver_scanner.py

# Test IL manager with fake data
python /Users/will/Code/fantasy-bot/il_manager.py
```

---

## 8. Cron Setup (Mac)

### Step 1: Find your Python path

```bash
which python
# Example output: /Users/will/Code/fantasy-bot/venv/bin/python
```

### Step 2: Create a log directory

```bash
mkdir -p /Users/will/Code/fantasy-bot/logs
```

### Step 3: Open crontab

```bash
crontab -e
```

This opens your user crontab in `vi`. Press `i` to enter insert mode.

### Step 4: Add these two lines

```cron
# Fantasy Hoops: Monday scraper at 1:45 AM
45 1 * * 1 /Users/will/Code/fantasy-bot/venv/bin/python /Users/will/Code/fantasy-bot/weekly.py >> /Users/will/Code/fantasy-bot/logs/weekly.log 2>&1

# Fantasy Hoops: Daily optimizer at 2:00 AM
0 2 * * * /Users/will/Code/fantasy-bot/venv/bin/python /Users/will/Code/fantasy-bot/main.py >> /Users/will/Code/fantasy-bot/logs/cron.log 2>&1
```

Press `Esc`, then type `:wq` and press Enter to save.

### Step 5: Verify cron is set up

```bash
crontab -l
```

### Mac-specific notes

- On macOS, `cron` requires **Full Disk Access** to run scripts. Go to:
  **System Settings → Privacy & Security → Full Disk Access** → add `/usr/sbin/cron`
- Alternatively, use **launchd** (macOS preferred scheduler). A sample `.plist` would go in `~/Library/LaunchAgents/`.
- Your Mac must be **awake** at 2 AM. Go to **System Settings → Battery → Schedule** and set it to wake before 2 AM. Or use the `caffeinate` command in a wrapper script.

### Wrapper script for reliability (optional)

Create `/Users/will/Code/fantasy-bot/run.sh`:

```bash
#!/bin/bash
cd /Users/will/Code/fantasy-bot
source venv/bin/activate
python main.py
```

Make it executable:
```bash
chmod +x /Users/will/Code/fantasy-bot/run.sh
```

Then use `run.sh` in crontab instead of calling Python directly.

---

## 9. File Overview

| File | Purpose |
|---|---|
| `main.py` | Daily entry point — orchestrates all 9 steps and sends report |
| `weekly.py` | Monday only — Selenium scraper for Yahoo MVP/Keys to Success page |
| `yahoo_client.py` | Yahoo Fantasy API wrapper (OAuth2, roster, free agents) |
| `nba_schedule.py` | Fetches today's NBA games + weekly remaining games per team |
| `bm_scraper.py` | Scrapes Basketball Monster per-category 9-cat value scores; caches to `bm_cache.json` (20h TTL) |
| `name_matcher.py` | Fuzzy name matching between BM and Yahoo player names (exact, thefuzz, last+initial) |
| `optimizer.py` | Greedy bipartite lineup builder using BM scores (fallback: Yahoo rank); bench shape analyser |
| `waiver_scanner.py` | Finds FA upgrades using BM scores and weekly value; filters out negative-BM FAs |
| `il_manager.py` | Flags players who should move to/from IL |
| `emailer.py` | Builds styled HTML email with BM scores and sends via Gmail SMTP |
| `requirements.txt` | Python package dependencies |
| `.env.example` | Template for environment variables |
| `oauth2.json.example` | Template for Yahoo OAuth2 token file |
| `untouchables.json` | Auto-generated by weekly.py; read by main.py (gitignored) |
| `bm_cache.json` | Auto-generated by bm_scraper.py; cached BM rankings (gitignored) |
| `logs/` | Created manually; cron output is redirected here |

---

## 10. Troubleshooting

### "oauth2.json not found"

You haven't copied the example file yet:
```bash
cp oauth2.json.example oauth2.json
```
Then fill in `consumer_key` and `consumer_secret`.

### "Could not find team 'XYZs' in league"

The `YAHOO_TEAM_NAME` in `.env` doesn't match. Run `python yahoo_client.py` and look at the "Available teams" list in the error message. Update `.env` with the exact name (or a unique prefix).

### Yahoo OAuth browser tab doesn't open

When running headlessly (in cron or SSH), the browser can't open. Run the first-time auth interactively on your local machine:
```bash
source venv/bin/activate
python yahoo_client.py
```
After the first successful auth, `oauth2.json` will have valid tokens and subsequent runs (including cron) will silently refresh without browser interaction.

### "SMTP Authentication Error"

- Make sure `GMAIL_APP_PASSWORD` is a **16-character App Password** from Google, not your regular Gmail password.
- Verify 2-Step Verification is enabled on your Google account.
- The App Password only works for the Gmail account it was generated for.

### nba_api returns no games / times out

`stats.nba.com` occasionally throttles requests or changes its API. The bot tries ESPN first, then falls back to `nba_api`. If both fail, `has_game_today` will default to `False` for all players (conservative — no one will be benched incorrectly, but you may see active players without games flagged as alerts).

### weekly.py / Selenium hits a CAPTCHA or 2FA challenge

Yahoo's login detection is aggressive for headless browsers. Options:
1. Log in manually in a real browser on the same machine first, then run `weekly.py` immediately (session cookies may carry over in some configs).
2. Run `weekly.py` with the `--headless=new` flag removed temporarily so you can see and interact with the browser.
3. Use a Yahoo session cookie approach: log in manually, extract the cookie, and inject it into Selenium's cookie store before navigating to the fantasy page.

### "Module not found" errors

Make sure the virtual environment is activated:
```bash
source /Users/will/Code/fantasy-bot/venv/bin/activate
```

### Logs show nothing / cron not running

- Verify cron has Full Disk Access (see Section 8).
- Test the cron command manually in a fresh terminal (not in your virtualenv):
  ```bash
  /Users/will/Code/fantasy-bot/venv/bin/python /Users/will/Code/fantasy-bot/main.py
  ```
- Check the log file: `cat /Users/will/Code/fantasy-bot/logs/cron.log`

### Token expires mid-day

The bot calls `refresh_token_if_needed()` at startup. Yahoo access tokens last ~1 hour but refresh tokens last much longer. If you see auth errors in cron logs, try running `python yahoo_client.py` interactively to force a fresh token cycle.
