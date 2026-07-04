# Changelog

## 1.0.0 (2026-07-03)

- Rewrote as a stdlib-only fetcher: request the server-rendered Go page with the
  session cookie and extract the inlined usage data. No Playwright, no browser,
  no dependencies.
- Output reports the three usage windows (rolling / weekly / monthly) as
  `pct` + `reset_in_sec` + `status`, plus `use_balance`.
- Added tests and an MIT license; packaged for `pipx install`.
- Moved the provider-switch watchdog to `examples/` as an adaptable template.

## 0.1.0 (unreleased, superseded)

- Initial Playwright-based scraper scaffold (never completed; the API turned out
  to be unnecessary — the data is server-rendered).
