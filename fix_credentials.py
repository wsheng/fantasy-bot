"""
fix_credentials.py — Sanitize oauth2.json and .env in place.

Fixes the most common paste-artifact problems:
  • Leading/trailing whitespace on every value
  • consumer_secret pasted multiple times (takes the first unique token)
  • Syncs .env CLIENT_ID/SECRET to match oauth2.json after cleaning

Run once:
    python3 fix_credentials.py
"""

import json
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
OAUTH_FILE = os.path.join(BASE, "oauth2.json")
ENV_FILE = os.path.join(BASE, ".env")


def first_token(value: str) -> str:
    """
    Strip whitespace and return the first whitespace-separated token.

    Handles the case where a value was pasted multiple times:
      "  abc123  abc123  " -> "abc123"
    """
    tokens = value.strip().split()
    if not tokens:
        return value.strip()
    # If all tokens are the same, just return one of them
    unique = list(dict.fromkeys(tokens))  # preserve order, deduplicate
    if len(unique) == 1:
        return unique[0]
    # Multiple distinct tokens — return the longest (usually the complete one)
    longest = max(unique, key=len)
    print(f"  ⚠  Multiple distinct tokens found — using longest: {longest[:6]}…")
    return longest


# ── Fix oauth2.json ──────────────────────────────────────────────────────────

print(f"\nReading {OAUTH_FILE} …")
with open(OAUTH_FILE) as f:
    data = json.load(f)

changed = False
for field in ("consumer_key", "consumer_secret", "access_token", "refresh_token"):
    raw = data.get(field, "")
    if not isinstance(raw, str):
        continue
    cleaned = first_token(raw) if raw.strip() else raw.strip()
    if cleaned != raw:
        print(f"  Fixed  : {field}  ({repr(raw[:30])} → {repr(cleaned[:30])})")
        data[field] = cleaned
        changed = True
    else:
        print(f"  OK     : {field}")

if changed:
    with open(OAUTH_FILE, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(OAUTH_FILE, 0o600)
    print(f"  ✓  Wrote cleaned oauth2.json")
else:
    print("  ✓  oauth2.json looks clean — no changes needed")


# ── Sync .env to match oauth2.json ──────────────────────────────────────────

if os.path.exists(ENV_FILE):
    print(f"\nChecking {ENV_FILE} …")
    with open(ENV_FILE) as f:
        env_lines = f.readlines()

    new_lines = []
    env_changed = False
    for line in env_lines:
        if line.startswith("YAHOO_CLIENT_ID="):
            value = line.split("=", 1)[1].strip()
            cleaned = first_token(value) if value else value
            if cleaned != value:
                line = f"YAHOO_CLIENT_ID={cleaned}\n"
                print(f"  Fixed  : YAHOO_CLIENT_ID")
                env_changed = True
            else:
                print(f"  OK     : YAHOO_CLIENT_ID")
        elif line.startswith("YAHOO_CLIENT_SECRET="):
            value = line.split("=", 1)[1].strip()
            cleaned = first_token(value) if value else value
            if cleaned != value:
                line = f"YAHOO_CLIENT_SECRET={cleaned}\n"
                print(f"  Fixed  : YAHOO_CLIENT_SECRET")
                env_changed = True
            else:
                print(f"  OK     : YAHOO_CLIENT_SECRET")
        new_lines.append(line)

    if env_changed:
        with open(ENV_FILE, "w") as f:
            f.writelines(new_lines)
        os.chmod(ENV_FILE, 0o600)
        print(f"  ✓  Wrote cleaned .env")
    else:
        print("  ✓  .env looks clean — no changes needed")

print("\nDone. Now run:\n")
print("    python3 yahoo_client.py\n")
