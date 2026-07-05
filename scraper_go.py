#!/usr/bin/env python3
"""Fetch OpenCode Go usage limits as JSON — no browser, no API key, just your cookie.

OpenCode Go (https://opencode.ai/docs/go) meters usage as three rolling windows
(rolling / weekly / monthly), shown as percentages on the workspace "Go" page.
There is no public API, but the page is server-rendered: the numbers are inlined
in the HTML. This script fetches that HTML with your browser session cookie and
extracts them with a regex. Standard library only — nothing to ``pip install``.

    export OPENCODE_AUTH_COOKIE='your-auth-cookie'   # the `auth` cookie from your browser
    ./opencode_go_usage.py --workspace wrk_...  # or set OPENCODE_WORKSPACE_ID

Exit codes:
  0  success (JSON on stdout)
  1  auth cookie missing/expired  → re-grab it from your browser (see README)
  2  page format changed          → update the regex in parse_usage()
  3  network error / timeout
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from _base import check_thresholds, fail, http_get, load_cookie

BASE = "https://opencode.ai"
USER_AGENT = "quotactl-go/1.0 (+https://github.com/brfid/quotactl)"

# The three usage windows, mapped to the keys used in the page's inlined data.
WINDOWS = {"rolling": "rollingUsage", "weekly": "weeklyUsage", "monthly": "monthlyUsage"}

# Default alert thresholds (percent). Override with ALERT_<WINDOW>_PCT env vars.
DEFAULT_THRESHOLDS = {"rolling": 90.0, "weekly": 85.0, "monthly": 95.0}

_USE_BALANCE_RE = re.compile(r"useBalance:(!0|!1),region:")


def _window_re(key: str) -> re.Pattern:
    """Build the regex that finds one usage window's inlined data.

    The Go page is a SolidJS app; each window's data is inlined in a
    minified ``<script>`` blob in a form like::

        monthlyUsage:$R[38]={status:"ok",resetInSec:504671,usagePercent:43}

    Args:
        key: The window's JS property name, e.g. ``"monthlyUsage"``
            (see :data:`WINDOWS`).

    Returns:
        A compiled pattern with three capture groups: status, reset_in_sec,
        usagePercent, in that order.
    """
    return re.compile(
        re.escape(key)
        + r':\$R\[\d+\]=\{status:"([^"]*)",resetInSec:(\d+),usagePercent:(\d+)\}'
    )


def _looks_logged_out(final_url: str, html: str) -> bool:
    """Detect a still-200 "you're not logged in" response.

    Some auth failures redirect to ``auth.opencode.ai`` (HTTP-level); others
    render an HTTP 200 page with no workspace content (e.g. a generic app
    shell or login prompt). This catches the second case: every authenticated
    page embeds at least one ``wrk_...`` workspace id.

    Args:
        final_url: The URL after following any redirects.
        html: The response body.

    Returns:
        True if the response looks like a logged-out state.
    """
    if "auth.opencode.ai" in final_url:
        return True
    return "wrk_" not in html


def parse_usage(html: str) -> dict | None:
    """Extract the three usage windows from the Go page HTML.

    Args:
        html: The page body.

    Returns:
        A dict keyed by window name (``"rolling"``, ``"weekly"``,
        ``"monthly"``), each mapping to ``{"status", "reset_in_sec", "pct"}``.
        Returns None if any window's data isn't found — i.e. OpenCode
        changed the page format and :func:`_window_re` no longer matches.
    """
    out = {}
    for name, key in WINDOWS.items():
        m = _window_re(key).search(html)
        if not m:
            return None
        out[name] = {
            "status": m.group(1),
            "reset_in_sec": int(m.group(2)),
            "pct": int(m.group(3)),
        }
    return out


def parse_use_balance(html: str) -> bool | None:
    """Whether the account falls back to paid balance after limits.

    Returns:
        True/False if the ``useBalance`` flag is found in the page's inlined
        data, otherwise None (e.g. the page format changed).
    """
    m = _USE_BALANCE_RE.search(html)
    if not m:
        return None
    return m.group(1) == "!0"  # minified JS: !0 == true, !1 == false


def fetch(cookie: str, workspace_id: str | None) -> dict:
    """Fetch and parse OpenCode Go usage for one workspace.

    Args:
        cookie: A ``Cookie:`` header value from :func:`_base.load_cookie`.
        workspace_id: The ``wrk_...`` workspace id, or None/empty.

    Returns:
        The full result dict — see the module docstring for its shape.

    Raises:
        SystemExit: Code 1 if *workspace_id* is missing or the cookie is
            expired, code 2 if the page format changed, code 3 on a network
            error (raised by :func:`_base.http_get`).
    """
    if not workspace_id:
        fail(
            1,
            "NO_WORKSPACE",
            "No workspace id. Set OPENCODE_WORKSPACE_ID or pass --workspace. It's the "
            "wrk_... in your Go page URL: https://opencode.ai/workspace/wrk_XXXX/go",
        )

    final_url, html = http_get(
        f"{BASE}/workspace/{workspace_id}/go",
        cookie,
        user_agent=USER_AGENT,
        extra_headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en",
        },
    )
    if _looks_logged_out(final_url, html):
        fail(1, "AUTH_EXPIRED", "Session cookie missing or expired. Re-grab it (see README).")

    windows = parse_usage(html)
    if windows is None:
        fail(
            2,
            "PAGE_CHANGED",
            "Usage data not found in the page. OpenCode changed the layout; "
            "update parse_usage() in opencode_go_usage.py (see README § Maintenance).",
        )

    return {
        "provider": "opencode-go",
        "workspace_id": workspace_id,
        "windows": windows,
        "use_balance": parse_use_balance(html),
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main() -> int:
    """CLI entry point: parse args, fetch usage, emit JSON, warn on threshold breaches.

    Returns:
        Process exit code. Always 0 here — failures exit early via :func:`fail`.
    """
    parser = argparse.ArgumentParser(description="Fetch OpenCode Go usage limits as JSON.")
    parser.add_argument(
        "--workspace", help="Workspace id (default: OPENCODE_WORKSPACE_ID env var)"
    )
    parser.add_argument("--cookie-file", help="File containing the session cookie")
    parser.add_argument("--output", help="Write JSON to this file instead of stdout")
    args = parser.parse_args()

    cookie = load_cookie(
        env_var="OPENCODE_AUTH_COOKIE",
        cookie_name="auth",
        default_config_dir="opencode-go-usage",
        cookie_file=args.cookie_file,
    )
    workspace_id = args.workspace or os.getenv("OPENCODE_WORKSPACE_ID") or None

    result = fetch(cookie, workspace_id)
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
