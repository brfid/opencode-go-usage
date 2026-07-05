"""Shared helpers for LLM provider usage scrapers.

Stdlib only — no dependencies. Provides cookie loading, HTTP fetching,
threshold checking, and error-handling that both scrapers use identically.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

TIMEOUT = 30


def fail(code: int, tag: str, msg: str) -> NoReturn:
    """Print a FATAL: message to stderr and exit with *code*."""
    print(f"FATAL: {tag}", file=sys.stderr)
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_cookie(
    *,
    env_var: str,
    cookie_name: str,
    default_config_dir: str,
    cookie_file: str | None = None,
) -> str:
    """Resolve a session cookie, normalized to a ``Cookie:`` header value.

    Checked in order, so credentials never have to live inside the repo:

    1. *env_var* environment variable
    2. *cookie_file* (from a ``--cookie-file`` CLI flag), if given
    3. ``~/.config/<default_config_dir>/auth``

    Args:
        env_var: Environment variable name (e.g. ``"OPENCODE_AUTH_COOKIE"``).
        cookie_name: Cookie name for the header (e.g. ``"auth"``).
        default_config_dir: Directory under ``~/.config/`` for the auth file
            (e.g. ``"opencode-go-usage"`` → ``~/.config/opencode-go-usage/auth``).
        cookie_file: Override path from CLI, or ``None``.

    Returns:
        A value usable as an HTTP ``Cookie`` header, e.g.
        ``"auth=your-auth-cookie"``.

    Raises:
        SystemExit: Code 1 if no cookie is found anywhere.
    """
    default_path = Path.home() / ".config" / default_config_dir / "auth"

    val = os.getenv(env_var)
    if not val and cookie_file:
        try:
            val = Path(cookie_file).read_text()
        except OSError as e:
            fail(1, "NO_COOKIE", f"Can't read --cookie-file {cookie_file}: {e}")
    if not val:
        try:
            if default_path.exists():
                val = default_path.read_text()
        except OSError as e:
            fail(1, "NO_COOKIE", f"Can't read {default_path}: {e}")
    if not val or not val.strip():
        fail(
            1,
            "NO_COOKIE",
            f"No session cookie. Set {env_var}, pass --cookie-file, "
            f"or write it to {default_path}.",
        )
    val = val.strip()
    # Accept a bare value, `name=...`, or a full `k=v; k=v` cookie header.
    if val.startswith(f"{cookie_name}=") or (";" in val and "=" in val):
        return val
    return f"{cookie_name}={val}"


def check_thresholds(
    windows: dict,
    thresholds: dict[str, float],
    env_prefix: str = "ALERT_",
) -> list[str]:
    """Compare each window's usage against its alert threshold.

    Each window's threshold comes from ``{env_prefix}{WINDOW}_PCT`` env var,
    falling back to *thresholds*.

    Args:
        windows: ``{window_name: {"pct": int, ...}}`` from a scraper result.
        thresholds: ``{window_name: default_threshold}``.
        env_prefix: Prefix for per-window env-var overrides
            (e.g. ``"ALERT_"`` → ``ALERT_ROLLING_PCT``).

    Returns:
        One human-readable string per window that's at or above its threshold.
        Empty list if nothing crossed.
    """
    alerts = []
    for name, default_pct in thresholds.items():
        if name not in windows:
            continue
        env_name = f"{env_prefix}{name.upper()}_PCT"
        try:
            threshold = float(os.getenv(env_name, str(default_pct)))
        except (ValueError, TypeError):
            threshold = default_pct
        pct = windows[name]["pct"]
        if pct >= threshold:
            alerts.append(f"{name}: {pct}% used (threshold {threshold}%)")
    return alerts


def http_get(
    url: str,
    cookie: str,
    *,
    user_agent: str = "provider-scraper/1.0",
    extra_headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Fetch a URL as an authenticated user.

    Args:
        url: The URL to fetch.
        cookie: A ``Cookie:`` header value from :func:`load_cookie`.
        user_agent: ``User-Agent`` header value.
        extra_headers: Additional headers to include in the request.

    Returns:
        A ``(final_url, body)`` tuple. *final_url* differs from *url* if
        the server redirected (e.g. to a login page).

    Raises:
        SystemExit: Code 1 on HTTP 401/403, code 3 on any other HTTP error,
            timeout, or connection failure.
    """
    headers: dict[str, str] = {
        "Cookie": cookie,
        "User-Agent": user_agent,
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.geturl(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            fail(1, "AUTH_EXPIRED", f"Server rejected the session cookie (HTTP {e.code}).")
        fail(3, "NETWORK_ERROR", f"HTTP {e.code} fetching {url}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        fail(3, "NETWORK_ERROR", f"{e} fetching {url}")
