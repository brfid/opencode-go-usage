#!/usr/bin/env python3
"""Fetch ClinePass usage limits as JSON — clean REST API, no scraping needed.

ClinePass (api.cline.bot) has a real endpoint at:
    GET /api/v1/users/me/plan/usage-limits

It returns three rolling windows (five_hour / weekly / monthly) with
percentUsed and resetsAt timestamps. Authenticate with the cline_session_id
cookie from your browser.

    export CLINE_SESSION_ID='your-session-id'   # the cline_session_id cookie
    ./clinepass_usage.py

Exit codes:
  0  success (JSON on stdout)
  1  auth cookie missing/expired
  2  API response format changed
  3  network error / timeout
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from _base import check_thresholds, fail, http_get, load_cookie

ENDPOINT = "https://api.cline.bot/api/v1/users/me/plan/usage-limits"
USER_AGENT = "quotactl-cp/1.0 (+https://github.com/brfid/quotactl)"

# Default alert thresholds (percent). Override with ALERT_<WINDOW>_PCT env vars.
DEFAULT_THRESHOLDS = {"five_hour": 90.0, "weekly": 85.0, "monthly": 95.0}


def _iso_to_seconds(iso_str: str) -> int:
    """Convert an ISO 8601 timestamp to seconds until reset.

    Returns 0 if the timestamp cannot be parsed or is in the past.
    """
    try:
        # Handle 'Z' suffix
        if iso_str.endswith("Z"):
            iso_str = iso_str[:-1] + "+00:00"
        reset_dt = datetime.fromisoformat(iso_str)
        now = datetime.now(timezone.utc)
        delta = (reset_dt - now).total_seconds()
        return max(0, int(delta))
    except (ValueError, TypeError):
        return 0


def fetch(cookie: str) -> dict:
    """Hit the usage-limits endpoint and return parsed JSON.

    Args:
        cookie: A ``Cookie:`` header value from :func:`_base.load_cookie`.

    Returns:
        The full result dict — see the module docstring for its shape.

    Raises:
        SystemExit: Code 1 if the cookie is expired, code 2 if the API
            response format changed, code 3 on a network error.
    """
    _, body = http_get(
        ENDPOINT,
        cookie,
        user_agent=USER_AGENT,
        extra_headers={
            "Accept": "application/json",
            "Origin": "https://app.cline.bot",
            "Referer": "https://app.cline.bot/",
        },
    )

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        fail(2, "PAGE_CHANGED", "Response is not valid JSON. API format may have changed.")

    if not isinstance(data, dict):
        fail(2, "PAGE_CHANGED", "Response is not a JSON object. API format may have changed.")
    if not data.get("success"):
        fail(
            2,
            "PAGE_CHANGED",
            f"API returned success=false: {data.get('error', 'unknown error')}",
        )

    limits = data.get("data", {}).get("limits")
    if not limits or not isinstance(limits, list):
        fail(
            2, "PAGE_CHANGED", "No 'data.limits' array in response. API format may have changed."
        )

    # Normalize to match opencode_go_usage.py output shape
    windows = {}
    for limit in limits:
        try:
            name = limit["type"]
            windows[name] = {
                "status": "exhausted" if limit["percentUsed"] >= 100 else "ok",
                "reset_in_sec": _iso_to_seconds(limit["resetsAt"]),
                "pct": limit["percentUsed"],
            }
        except (KeyError, TypeError) as e:
            fail(
                2,
                "PAGE_CHANGED",
                f"Missing or malformed field in API response: {e}. "
                "API format may have changed.",
            )

    return {
        "provider": "clinepass",
        "windows": windows,
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main() -> int:
    """CLI entry point: parse args, fetch usage, emit JSON, warn on threshold breaches.

    Returns:
        Process exit code. Always 0 here — failures exit early via :func:`fail`.
    """
    parser = argparse.ArgumentParser(description="Fetch ClinePass usage limits as JSON.")
    parser.add_argument("--cookie-file", help="File containing the cline_session_id cookie")
    parser.add_argument("--output", help="Write JSON to this file instead of stdout")
    args = parser.parse_args()

    cookie = load_cookie(
        env_var="CLINE_SESSION_ID",
        cookie_name="cline_session_id",
        default_config_dir="clinepass-usage",
        cookie_file=args.cookie_file,
    )

    result = fetch(cookie)
    payload = json.dumps(result, indent=2)

    if args.output:
        Path(args.output).write_text(payload + "\n")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(payload)

    for alert in check_thresholds(result["windows"], DEFAULT_THRESHOLDS, "ALERT_"):
        print(f"⚠ {alert}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
