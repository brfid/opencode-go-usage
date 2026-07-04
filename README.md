# opencode-go-usage

Fetch your [OpenCode Go](https://opencode.ai/docs/go) usage limits as JSON — no
browser automation, no API key, just your session cookie. Standard library
only; nothing to install.

OpenCode Go meters usage as three rolling windows (rolling / weekly / monthly),
shown as percentages on your workspace's **Go** page. There is no public API,
but the page is server-rendered, so the numbers are sitting in the HTML. This
tool fetches that HTML with your session cookie and pulls them out.

## Install

Clone and run it directly (Python 3.9+):

```bash
git clone https://github.com/brfid/opencode-go-usage
cd opencode-go-usage
./opencode_go_usage.py
```

Or install the `opencode-go-usage` command with [pipx](https://pipx.pypa.io):

```bash
pipx install .
```

## Get your session cookie

The tool authenticates as you, using the `auth` cookie your browser already has.

1. Log in at <https://opencode.ai> and open your workspace **Go** page.
2. Open DevTools (F12) → **Application** → **Cookies** → `https://opencode.ai`.
3. Copy the value of the **`auth`** cookie (a long `Fe26.2**…` string).
4. Give it to the tool one of these ways:

```bash
export OPENCODE_AUTH_COOKIE='Fe26.2**...'          # env var, or
mkdir -p ~/.config/opencode-go-usage
echo 'Fe26.2**...' > ~/.config/opencode-go-usage/auth   # the default file, or
./opencode_go_usage.py --cookie-file /path/to/cookie    # any file you choose
```

A `.env.example` is included as a template — copy it to `.env`, fill it in, then
load it into your shell before running the tool (there's no dependency to parse
it automatically): `set -a; source .env; set +a`.

**This cookie is a full login credential.** It's checked in that order —
env var, then `--cookie-file`, then `~/.config/opencode-go-usage/auth` — and
none of those locations are inside this repo, so cloning or sharing the repo
never risks the secret. Treat it like a password. It's long-lived but does
eventually expire — when it does, the tool exits `1` and you re-grab it.

## Usage

```bash
./opencode_go_usage.py --workspace wrk_...   # JSON to stdout
./opencode_go_usage.py --workspace wrk_... --output usage.json
```

You also need your **workspace id** — the `wrk_…` in your Go page URL,
`https://opencode.ai/workspace/wrk_XXXX/go`. Pass it with `--workspace` or set
`OPENCODE_WORKSPACE_ID`.

### Output

```json
{
  "provider": "opencode-go",
  "workspace_id": "wrk_...",
  "windows": {
    "rolling": { "status": "ok", "reset_in_sec": 14182,  "pct": 22 },
    "weekly":  { "status": "ok", "reset_in_sec": 161609, "pct": 43 },
    "monthly": { "status": "ok", "reset_in_sec": 504671, "pct": 43 }
  },
  "use_balance": true,
  "scraped_at": "2026-07-04T02:30:00Z"
}
```

- `pct` — percent of the window's limit used (0–100).
- `reset_in_sec` — seconds until the window resets.
- `status` — `"ok"` while the window has budget.
- `use_balance` — whether the account bills paid balance after limits are hit.

### Alerts

Set `ALERT_ROLLING_PCT`, `ALERT_WEEKLY_PCT`, or `ALERT_MONTHLY_PCT` to print a
warning to stderr when a window crosses that percent. Defaults: 90 / 85 / 95.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | Success — JSON on stdout |
| 1 | Not configured or auth expired — missing cookie/workspace, or re-grab the cookie |
| 2 | Page format changed — the regex needs updating |
| 3 | Network error / timeout |

## Cron example

With the cookie in `~/.config/opencode-go-usage/auth` (the default path), a
cron job only needs the workspace id:

```cron
# Refresh usage every 30 minutes
*/30 * * * * OPENCODE_WORKSPACE_ID=wrk_... /path/to/opencode_go_usage.py --output ~/.cache/opencode-go-usage.json
```

See [`examples/switch-provider-watchdog.py`](examples/switch-provider-watchdog.py)
for a template that reads that JSON and auto-switches your coding agent to
another provider when Go runs out, then back when it resets.

## Maintenance

This tool depends on one thing: the shape of the data OpenCode inlines in the Go
page. If OpenCode changes it, the tool exits `2` and you update one regex in
[`parse_usage()`](opencode_go_usage.py). To see the current shape, view-source
the Go page and search for `monthlyUsage` — the values live in a small inlined
JSON object. `test_opencode_go_usage.py` has a fixture you can update to match.

```bash
python3 test_opencode_go_usage.py   # run the checks
```

## License

MIT — see [LICENSE](LICENSE).
