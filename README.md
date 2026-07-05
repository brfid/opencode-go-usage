# quotactl

Quota-aware LLM provider switch advisor. Checks rate-limit usage
across multiple LLM API providers and gives a simple instruction:
**stay** on your current provider, or **switch** to another.

Stdlib-only. No dependencies. Two scrapers (OpenCode Go via HTML regex,
ClinePass via REST API) plus a multi-provider watchdog that compares
tiered usage and advises when switching would reduce overage risk.

## Quick start

### Install

```bash
pipx install git+https://github.com/brfid/quotactl.git
```

Or clone and run directly:

```bash
git clone https://github.com/brfid/quotactl.git
cd quotactl
```

### Check OpenCode Go usage

```bash
python3 scraper_go.py --workspace wrk_YOURWORKSPACEID
```

### Check ClinePass usage

```bash
python3 scraper_cp.py
```

### Get a switch recommendation

```bash
python3 watchdog.py
```

Output when nothing needs to change: *(silent)*

Output when a switch is advised:

```
opencode-go [s:22% m:43% l:98%] (rolling:22%, weekly:43%, monthly:98%) | clinepass [s:0% m:15% l:42%] (five_hour:0%, weekly:15%, monthly:42%)
🔄 example-profile → clinepass / cline-pass/example-model
   opencode-go[s:22% m:43% l:98%] → clinepass[s:0% m:15% l:42%]
```

### JSON output from any scraper

```json
{
  "provider": "opencode-go",
  "windows": {
    "rolling":  {"status": "ok", "reset_in_sec": 16485, "pct": 22},
    "weekly":   {"status": "ok", "reset_in_sec": 548927, "pct": 43},
    "monthly":  {"status": "ok", "reset_in_sec": 2536127, "pct": 98}
  },
  "scraped_at": "2026-07-04T16:17:55Z"
}
```

## File layout

| File | Purpose |
|------|---------|
| `_base.py` | Shared helpers: cookie loading, HTTP fetch, threshold checks, error handling |
| `scraper_go.py` | OpenCode Go scraper (HTML regex extraction) |
| `scraper_cp.py` | ClinePass scraper (REST API) |
| `watchdog.py` | Multi-provider switch advisor with `ProviderSpec`-based config |
| `test_quotactl.py` | Full pytest suite (50 tests) covering both scrapers, shared base, and watchdog logic |
| `examples/read-usage.py` | Minimal example: read scraper JSON from a file |

## Cookie acquisition

### OpenCode Go

1. Open <https://opencode.ai/workspace/WORKSPACE_ID/go> in a browser
2. Dev tools → Application → Cookies → `opencode.ai`
3. Copy the `auth` cookie value
4. Write to `~/.config/opencode-go-usage/auth` (or set `OPENCODE_AUTH_COOKIE`)

### ClinePass

1. Open <https://app.cline.bot/dashboard/subscription> in a browser
2. Dev tools → Application → Cookies → `app.cline.bot`
3. Copy the `cline_session_id` cookie value (not `unify_session_id`)
4. Write to `~/.config/clinepass-usage/auth` (or set `CLINE_SESSION_ID`)

---

## Exit codes

Both scrapers use the same contract:

| Code | Meaning |
|------|---------|
| 0 | Success (JSON on stdout) |
| 1 | Auth cookie missing or expired |
| 2 | Provider changed its format |
| 3 | Network error or timeout |

---

## Watchdog

`watchdog.py` is the switch advisor. It scrapes usage from all configured
providers, runs a pure `decide()` function to determine whether a switch
reduces overage risk, and prints a recommendation.

### Architecture

```
scrape → decide → execute → report
```

- **scrape** — runs each provider's scraper, normalizes into `TieredUsage`
- **decide** — pure function: state + `{provider: TieredUsage}` → `[SwitchDirective]`
- **execute** — applies directives via `hermes config set --profile`
- **report** — prints human-readable summary (silent when nothing changes)

### Provider abstraction

Providers are defined as `ProviderSpec` dataclasses. Each spec declares:

- `name` — provider identifier (e.g. `"opencode-go"`)
- `tool` — path to the scraper script
- `tool_env` — extra env vars for the scraper
- `tier_map` — raw window names → `"short"` / `"medium"` / `"long"`
- `model_map` — Hermes profile name → model name for this provider

Adding a third provider is adding one `ProviderSpec` to the `PROVIDERS` list
plus a scraper script that emits the standard JSON shape.

### Provider windows

Each provider has three rate-limit windows mapped to generic tiers:

| Tier | Go window | CP window | Reset horizon |
|------|-----------|-----------|---------------|
| short | `rolling` | `five_hour` | hours |
| medium | `weekly` | `weekly` | days |
| long | `monthly` | `monthly` | weeks |

### Switching logic

Both providers are equal peers — no "home base" preference. A switch
from provider A to B is advised when **all** of these hold:

1. **Trigger.** Any tier on A is at or above `TRIGGER_PCT` (default 95).
2. **Destination safety.** All three tiers on B are below their ceilings
   (short 80%, medium 90%, long 95%).
3. **Availability.** B's scraper returned successfully.

When both are over trigger with no safe destination, the advice is to stay
put — a switch would move the overage risk without reducing it.

### Hermes integration

The watchdog runs as a `no_agent` cron job every 10 minutes:

```
cronjob(action='create',
        schedule='*/10 * * * *',
        script='watchdog.py',
        no_agent=True,
        deliver='telegram:CHAT_ID')
```

### Config env vars

| Var | Default | Purpose |
|-----|---------|---------|
| `TRIGGER_PCT` | 95 | Switch trigger on any tier |
| `SAFE_SHORT_PCT` | 80 | Destination short ceiling |
| `SAFE_MEDIUM_PCT` | 90 | Destination medium ceiling |
| `SAFE_LONG_PCT` | 95 | Destination long ceiling |
| `OPENCODE_GO_USAGE` | auto | Path to Go scraper |
| `CLINEPASS_USAGE` | auto | Path to CP scraper |
| `STATE_FILE` | `~/.cache/quotactl-watchdog.json` | Switch state |

---

## Conventions for contributors

- **Stdlib only.** No new dependencies.
- **Exit codes are the contract** (0 ok, 1 auth, 2 format changed, 3 network).
- **Never commit auth cookies.**
- **JSON shape must stay stable.** Don't rename `windows.*.pct` or `windows.*.reset_in_sec`.
- Run `pytest` before committing.

### Adding a third provider

1. Write a scraper that emits the standard JSON shape. Import shared helpers
   from `_base`.
2. Add a `ProviderSpec` to the `PROVIDERS` list in `watchdog.py`.
3. Update this README with cookie acquisition instructions.

### When a scraper breaks

**OpenCode Go — page format changed (exit 2):** View-source the Go page,
search for `monthlyUsage`, update the regex in `scraper_go.py`. Refresh
the `GO_FIXTURE` string in the test.

**ClinePass — API format changed (exit 2):** Check the JSON shape at
`GET /api/v1/users/me/plan/usage-limits`. Update `scraper_cp.py` if
`data.limits[]` keys change.

**Auth expired (exit 1):** Re-grab the cookie from browser dev tools
(see § Cookie acquisition above).
