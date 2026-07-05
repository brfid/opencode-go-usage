"""
Example: consume scraper JSON output for a custom watchdog.

See the real ProviderSpec-based watchdog at ../opencode_go_watchdog.py
for a production-ready multi-provider implementation with symmetric
switching logic. This file is kept as a minimal example of reading the
JSON output from a single scraper.
"""

import json
import os
import sys
from pathlib import Path

USAGE_JSON = Path(os.getenv("USAGE_JSON", "opencode-usage.json"))


def main() -> int:
    try:
        data = json.loads(USAGE_JSON.read_text())
    except (OSError, ValueError) as e:
        print(f"Failed to read usage data: {e}", file=sys.stderr)
        return 1

    windows = data.get("windows", {})
    for name, w in windows.items():
        print(f"{name}: {w['pct']}% used, resets in {w['reset_in_sec']}s ({w['status']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
