"""
setup_credentials.py — One-time credential setup.

Run this once to create .env and oauth2.json from interactive prompts.
Nothing is stored in shell history; input() values go directly to files.
"""

import json
import os
import getpass

BASE = os.path.dirname(os.path.abspath(__file__))

print()
print("=" * 60)
print("  Fantasy Hoops Bot — Credential Setup")
print("=" * 60)
print()

# ── Yahoo API ────────────────────────────────────────────────────────────────
print("YAHOO DEVELOPER CREDENTIALS")
print("  Get these from: https://developer.yahoo.com/apps/")
print("  (Create App → Installed Application → Fantasy Sports → Read)")
print()
yahoo_client_id     = input("  Client ID      : ").strip()
yahoo_client_secret = getpass.getpass("  Client Secret  : ")

print()

# ── Yahoo account (for Selenium scraper) ────────────────────────────────────
print("YAHOO ACCOUNT (used by the weekly Selenium scraper)")
print()
yahoo_username = input("  Yahoo email    : ").strip()
yahoo_password = getpass.getpass("  Yahoo password : ")

print()

# ── Gmail ────────────────────────────────────────────────────────────────────
print("GMAIL (for sending the daily report)")
print("  App Password: https://myaccount.google.com/apppasswords")
print("  (requires 2-Step Verification to be enabled first)")
print()
gmail_user         = input("  Gmail address  : ").strip()
gmail_app_password = getpass.getpass("  App Password   : ")
notify_email       = input("  Send reports to: ").strip() or gmail_user

print()

# ── Write .env ───────────────────────────────────────────────────────────────
env_path = os.path.join(BASE, ".env")
env_content = f"""YAHOO_CLIENT_ID={yahoo_client_id}
YAHOO_CLIENT_SECRET={yahoo_client_secret}
YAHOO_LEAGUE_ID=11642
YAHOO_TEAM_NAME=XYZs
GMAIL_USER={gmail_user}
GMAIL_APP_PASSWORD={gmail_app_password}
NOTIFY_EMAIL={notify_email}
YAHOO_USERNAME={yahoo_username}
YAHOO_PASSWORD={yahoo_password}
"""

with open(env_path, "w") as f:
    f.write(env_content)
os.chmod(env_path, 0o600)  # owner-read/write only
print(f"✓  Written: {env_path}  (permissions: 600)")

# ── Write oauth2.json ────────────────────────────────────────────────────────
oauth_path = os.path.join(BASE, "oauth2.json")
oauth_data = {
    "access_token": "",
    "consumer_key": yahoo_client_id,
    "consumer_secret": yahoo_client_secret,
    "refresh_token": "",
    "token_time": 0,
    "token_type": "bearer",
}

with open(oauth_path, "w") as f:
    json.dump(oauth_data, f, indent=2)
os.chmod(oauth_path, 0o600)
print(f"✓  Written: {oauth_path}  (permissions: 600)")

print()
print("Setup complete. Next step:")
print()
print("  python3 yahoo_client.py")
print()
print("That will open a browser tab for Yahoo OAuth.")
print("After you click Agree, copy the localhost URL from")
print("your browser bar and paste it into the terminal.")
print()
