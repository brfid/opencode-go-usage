#!/usr/bin/env python3
"""Example: auto-switch your coding agent's provider when Go usage runs out.

This is a template, not a turn-key tool — it shows one way to *act* on the JSON
that opencode_go_usage.py produces. It was written for a personal Hermes setup;
adapt SWITCH_AWAY / SWITCH_BACK to whatever "change provider" means for you
(edit a config file, call your agent's CLI, hit a webhook, etc.).

Flow (run it on a cron, e.g. every 30 min):
  1. Read the usage JSON written by the scraper.
  2. If the monthly window is exhausted, switch away from Go.
  3. When the monthly window resets (usage drops well below the threshold),
     switch back to Go.

It only acts on fresh data and only prints on a state change, so it is quiet
and safe to run unattended.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# --- Configure these -------------------------------------------------------
USAGE_JSON = Path(os.getenv("USAGE_JSON", "opencode-usage.json"))
STATE_FILE = Path(os.getenv("WATCHDOG_STATE", "watchdog-state.json"))
THRESHOLD_PCT = float(os.getenv("WATCHDOG_THRESHOLD_PCT", "95"))
HYSTERESIS_PCT = float(os.getenv("WATCHDOG_HYSTERESIS_PCT", "20"))  # reset if pct drops this far below threshold
MAX_AGE_SEC = int(os.getenv("WATCHDOG_MAX_AGE_SEC", "3600"))

PRIMARY = "opencode-go"      # the provider we prefer while Go has budget
FALLBACK = "clinepass"       # where we go when Go is exhausted


def switch_provider(provider: str) -> bool:
    """Replace this with your own 'set provider' action. Returns True on success."""
    try:
        r = subprocess.run(
            ["hermes", "config", "set", "model.provider", provider],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False
# --------------------------------------------------------------------------


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def is_fresh(usage: dict) -> bool:
    """Whether `usage["scraped_at"]` (a UTC timestamp) is within MAX_AGE_SEC.

    `time.mktime` assumes local time, so `- time.timezone` corrects the
    parsed UTC struct_time back to a true Unix epoch offset.
    """
    stamped = usage.get("scraped_at", "")
    try:
        t = time.strptime(stamped, "%Y-%m-%dT%H:%M:%SZ")
        age = time.time() - (time.mktime(t) - time.timezone)
        return age <= MAX_AGE_SEC
    except (ValueError, TypeError):
        return False


def main() -> int:
    usage = load_json(USAGE_JSON)
    if not usage or not is_fresh(usage):
        # No decisions on missing or stale data.
        print("no fresh usage data; doing nothing", file=sys.stderr)
        return 0

    monthly = usage["windows"]["monthly"]
    pct = monthly["pct"]
    exhausted = pct >= THRESHOLD_PCT or monthly["status"] != "ok"

    state = load_json(STATE_FILE) or {"current": PRIMARY}
    current = state["current"]

    if exhausted and current == PRIMARY:
        if switch_provider(FALLBACK):
            print("Go exhausted (%s%%) — switched %s → %s" % (pct, PRIMARY, FALLBACK))
            state["current"] = FALLBACK
        else:
            print("ERROR: failed to switch to %s" % FALLBACK, file=sys.stderr)
    elif current == FALLBACK and pct < THRESHOLD_PCT - HYSTERESIS_PCT and monthly["status"] == "ok":
        # Monthly window reset — Go has budget again.
        if switch_provider(PRIMARY):
            print("Go reset (%s%%) — switched %s → %s" % (pct, FALLBACK, PRIMARY))
            state["current"] = PRIMARY
        else:
            print("ERROR: failed to switch to %s" % PRIMARY, file=sys.stderr)

    STATE_FILE.write_text(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
