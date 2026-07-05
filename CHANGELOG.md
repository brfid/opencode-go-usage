# Changelog

## 1.1.0 (unreleased)

- **Renamed to quotactl** — quota-aware LLM provider switch advisor.
  Repo, package, and all modules renamed accordingly.
- **Shared base module** — extracted `fail`, `load_cookie`, `check_thresholds`,
  and `http_get` into `_base.py`. Both scrapers import from a single source
  of truth instead of copy-pasting.
- **Bug fix** — ClinePass `check_thresholds` now respects `ALERT_*_PCT` env
  vars (previously silently ignored them).
- **Bug fix** — ClinePass `fetch()` now guards raw dict access with try/except
  and exits with code 2 instead of raising `KeyError` on missing fields.
- **Bug fix** — `load_cookie` in `_base.py` handles TOCTOU gracefully.
- **Bug fix** — `any_above` uses `>=` not `>` so exact-95% triggers correctly.
- **Bug fix** — `data.get()` crash on non-dict JSON response fixed.
- **Bug fix** — `lookup[current_provider]` KeyError on stale state fixed.
- **Bug fix** — `float()` crash on non-numeric env var fixed.
- **Moved real watchdog into repo** — the ProviderSpec-based symmetric watchdog
  (`watchdog.py`) is now the canonical implementation.
- **Tests** — 50 pytest tests covering both scrapers, shared base, and watchdog
  `decide()` logic (9 scenarios including boundary and error cases).
- **Packaging** — `pyproject.toml` covers all modules with entry points
  (`quotactl`, `quotactl-go`, `quotactl-cp`).
- **Documentation** — README and AGENTS.md rewritten for the new name and
  advisor framing.

## 1.0.0 (2026-07-03)

- Initial release as `opencode-go-usage`: stdlib-only fetcher for OpenCode
  Go usage limits via cookie-authenticated HTML scraping.
- Three windows (rolling/weekly/monthly) as `pct` + `reset_in_sec` + `status`.
- Tests, MIT license, `pipx install` support.
