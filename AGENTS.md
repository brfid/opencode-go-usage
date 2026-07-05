# AGENTS.md — for AI agents working on this repo

## What this is

Quota-aware LLM provider switch advisor. Two stdlib-only usage scrapers
plus a watchdog that checks rate-limit usage across providers and gives
a simple instruction: **stay** or **switch**. Both scrapers follow the
same contract (JSON to stdout, exit codes 0/1/2/3) so the watchdog
treats them interchangeably.

## Layout

| File | Purpose |
|------|---------|
| `_base.py` | Shared helpers: `fail`, `load_cookie`, `check_thresholds`, `http_get` |
| `scraper_go.py` | OpenCode Go scraper (HTML regex extraction, uses `_base`) |
| `scraper_cp.py` | ClinePass scraper (REST API, uses `_base`) |
| `watchdog.py` | Multi-provider switch advisor with auto-discovery and ProviderSpec config |
| `test_quotactl.py` | Full pytest suite for both scrapers, shared base, watchdog logic, and model transforms |
| `README.md` | Full docs, cookie acquisition, switching logic |
| `examples/read-usage.py` | Minimal example: consume scraper JSON output |

## Conventions

- **Stdlib only.** No new dependencies.
- **Exit codes are the contract** (0 ok, 1 auth, 2 format changed, 3 network).
- **Never commit auth cookies.**
- **JSON shape must stay stable.** Don't rename `windows.*.pct` or
  `windows.*.reset_in_sec`.
- **No profile names or workspace IDs in code.** Profiles are auto-discovered
  from `~/.hermes/profiles/*/config.yaml` at runtime. Workspace ID comes from
  `OPENCODE_WORKSPACE_ID` env var.
- Run `pytest` before committing.

## Shared base module (`_base.py`)

New scrapers should import shared helpers rather than copy-pasting:

```python
from _base import fail, load_cookie, check_thresholds, http_get
```

## When a scraper breaks

### OpenCode Go — page format changed (exit 2)

View-source the Go page, search for `monthlyUsage`, update the regex in
`scraper_go.py`. Refresh the `GO_FIXTURE` string in the test.

### ClinePass — API format changed (exit 2)

Endpoint: `GET https://api.cline.bot/api/v1/users/me/plan/usage-limits`.
Check that the JSON shape (`data.limits[]` with `type`/`percentUsed`/`resetsAt`)
still matches. Update `scraper_cp.py` if the structure changed.

### Auth expired (exit 1)

OpenCode: re-grab the `auth` cookie from opencode.ai dev tools.
ClinePass: re-grab the `cline_session_id` cookie from app.cline.bot dev tools.
See README.md § Cookie acquisition for exact steps.

## Adding a third provider

1. Write a scraper that emits the standard JSON shape (with `windows`
   key mapping window names to `{status, reset_in_sec, pct}`). Import
   shared helpers from `_base` — do NOT copy-paste.
2. Add a `ProviderSpec` to the `PROVIDERS` list in `watchdog.py`
   with the new provider's `tier_map` and `model_prefix`.
3. Update README.md with cookie acquisition instructions.

Note: only binary switching is supported (exactly two providers).
The `decide()` function unpacks `specs[0], specs[1]` explicitly.
