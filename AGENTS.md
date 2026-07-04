# AGENTS.md — for AI agents working on this repo

## What this is

A stdlib-only Python tool that fetches OpenCode Go usage limits by requesting
the server-rendered Go page with the user's session cookie and extracting the
inlined usage data with a regex. There is no OpenCode API; this is the whole
mechanism. No browser, no dependencies.

## Layout

| File | Purpose |
| ---- | ------- |
| `opencode_go_usage.py` | The tool: fetch, parse, JSON output |
| `test_opencode_go_usage.py` | Runnable checks with an HTML fixture |
| `examples/switch-provider-watchdog.py` | Template: act on the JSON |

## Conventions

- **Stdlib only.** No new dependencies — that's the point of the design.
- **One regex is the fragile part.** When the page format changes the tool
  exits `2`; fix `parse_usage()` and update the fixture in the test.
- **Exit codes are the contract** (0 ok, 1 auth, 2 page changed, 3 network).
  Downstream consumers depend on them; don't repurpose them.
- **Never commit the `auth` cookie.** It's a full login credential.
- Run `python3 test_opencode_go_usage.py` before committing.

## When the page format changes (exit 2)

View-source the Go page, search for `monthlyUsage`, and update the pattern in
`_window_re()` / `parse_usage()` to match the new shape. Refresh the `FIXTURE`
string in the test to a sanitized copy of the new HTML.
