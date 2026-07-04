#!/usr/bin/env python3
"""Fetch OpenCode Go usage limits as JSON — no browser, no API key, just your cookie.

OpenCode Go (https://opencode.ai/docs/go) meters usage as three rolling windows
(rolling / weekly / monthly), shown as percentages on the workspace "Go" page.
There is no public API, but the page is server-rendered: the numbers are inlined
in the HTML. This script fetches that HTML with your browser session cookie and
extracts them with a regex. Standard library only — nothing to `pip install`.

    export OPENCODE_AUTH_COOKIE='Fe26.2**...'   # the `auth` cookie from your browser
    ./opencode_go_usage.py --workspace wrk_...  # or set OPENCODE_WORKSPACE_ID

Exit codes:
  0  success (JSON on stdout)
  1  auth cookie missing/expired  -> re-grab it from your browser (see README)
  2  page format changed          -> update the regex in parse_usage()
  3  network error / timeout
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://opencode.ai"
TIMEOUT = 30
USER_AGENT = "opencode-go-usage/1.0 (+https://github.com/brfid/opencode-go-usage)"

# The three usage windows, mapped to the keys used in the page's inlined data.
WINDOWS = {"rolling": "rollingUsage", "weekly": "weeklyUsage", "monthly": "monthlyUsage"}

# Default alert thresholds (percent). Override with ALERT_<WINDOW>_PCT env vars.
DEFAULT_THRESHOLDS = {"rolling": 90.0, "weekly": 85.0, "monthly": 95.0}

_USE_BALANCE_RE = re.compile(r"useBalance:(!0|!1),region:")


def _window_re(key: str) -> re.Pattern:
    # Matches e.g.  monthlyUsage:$R[38]={status:"ok",resetInSec:504671,usagePercent:43}
    return re.compile(
        re.escape(key)
        + r':\$R\[\d+\]=\{status:"([^"]*)",resetInSec:(\d+),usagePercent:(\d+)\}'
    )


def _fail(code: int, tag: str, msg: str) -> "typing.NoReturn":  # noqa: F821
    print("FATAL: " + tag, file=sys.stderr)
    print(msg, file=sys.stderr)
    sys.exit(code)


def _http_get(url: str, cookie: str) -> tuple[str, str]:
    """GET a URL with the session cookie. Returns (final_url, body). Exits on error."""
    req = urllib.request.Request(
        url,
        headers={
            "Cookie": cookie,
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.geturl(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _fail(1, "AUTH_EXPIRED", "Server rejected the session cookie (HTTP %d)." % e.code)
        _fail(3, "NETWORK_ERROR", "HTTP %d fetching %s" % (e.code, url))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _fail(3, "NETWORK_ERROR", "%s fetching %s" % (e, url))


def _looks_logged_out(final_url: str, html: str) -> bool:
    if "auth.opencode.ai" in final_url:
        return True
    # Every authenticated page carries the app shell and at least one workspace id.
    return "wrk_" not in html


def parse_usage(html: str) -> dict | None:
    """Extract the three usage windows from the page HTML.

    Returns {window: {"status", "reset_in_sec", "pct"}} or None if the page
    format changed (the values are no longer where we expect them).
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
    """Whether the account falls back to paid balance after limits. None if absent."""
    m = _USE_BALANCE_RE.search(html)
    if not m:
        return None
    return m.group(1) == "!0"  # minified JS: !0 == true, !1 == false


def fetch(cookie: str, workspace_id: str | None) -> dict:
    if not workspace_id:
        _fail(
            1,
            "NO_WORKSPACE",
            "No workspace id. Set OPENCODE_WORKSPACE_ID or pass --workspace. It's the "
            "wrk_... in your Go page URL: https://opencode.ai/workspace/wrk_XXXX/go",
        )

    final_url, html = _http_get(BASE + "/workspace/" + workspace_id + "/go", cookie)
    if _looks_logged_out(final_url, html):
        _fail(1, "AUTH_EXPIRED", "Session cookie missing or expired. Re-grab it (see README).")

    windows = parse_usage(html)
    if windows is None:
        _fail(
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


def load_cookie(cookie_file: str | None) -> str:
    """Resolve the session cookie from env, --cookie-file, or default files."""
    val = os.getenv("OPENCODE_AUTH_COOKIE")
    if not val and cookie_file:
        val = Path(cookie_file).read_text()
    if not val:
        for p in (Path("auth.txt"), Path.home() / ".config/opencode-go-usage/auth"):
            if p.exists():
                val = p.read_text()
                break
    if not val or not val.strip():
        _fail(
            1,
            "NO_COOKIE",
            "No session cookie. Set OPENCODE_AUTH_COOKIE, pass --cookie-file, "
            "or create auth.txt. See README § Get your session cookie.",
        )
    val = val.strip()
    # Accept a bare sealed value, `auth=...`, or a full `k=v; k=v` cookie header.
    if val.startswith("auth=") or (";" in val and "=" in val):
        return val
    return "auth=" + val


def check_thresholds(windows: dict) -> list[str]:
    alerts = []
    for name in WINDOWS:
        threshold = float(os.getenv("ALERT_%s_PCT" % name.upper(), DEFAULT_THRESHOLDS[name]))
        pct = windows[name]["pct"]
        if pct >= threshold:
            alerts.append("%s: %s%% used (threshold %s%%)" % (name, pct, threshold))
    return alerts


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch OpenCode Go usage limits as JSON.")
    parser.add_argument("--workspace", help="Workspace id (default: OPENCODE_WORKSPACE_ID env var)")
    parser.add_argument("--cookie-file", help="File containing the session cookie")
    parser.add_argument("--output", help="Write JSON to this file instead of stdout")
    args = parser.parse_args()

    cookie = load_cookie(args.cookie_file)
    workspace_id = args.workspace or os.getenv("OPENCODE_WORKSPACE_ID") or None

    result = fetch(cookie, workspace_id)
    payload = json.dumps(result, indent=2)

    if args.output:
        Path(args.output).write_text(payload + "\n")
        print("Wrote " + args.output, file=sys.stderr)
    else:
        print(payload)

    for alert in check_thresholds(result["windows"]):
        print("⚠ " + alert, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
