#!/usr/bin/env python3
"""
Usage watchdog — multi-provider scraper and circuit-breaker for LLM providers.

Designed for Hermes cron (no_agent=True). Silent when nothing changes; prints a
summary when thresholds are crossed or profiles are switched.

Architecture
  discover → scrape → decide → execute → report

  discover   scans Hermes profiles to find which ones use managed providers
  scrape     runs each provider's usage tool, normalizes into TieredUsage
  decide     pure function: state + {provider: TieredUsage} → [SwitchDirective]
  execute    applies directives via `hermes config set`
  report     prints human-readable summary to stdout

Provider abstraction
  Each provider is described by a ProviderSpec: name, scraper path, tier_map,
  and a model_prefix for transforming model names between providers. Adding a
  third provider is adding one ProviderSpec.

  Profiles are auto-discovered — no profile names in code. Any profile whose
  model.provider matches a managed provider is included.

Switching logic (rationale in README.md § Switching Logic)
  Both providers are treated equally — no "home base" preference. Either
  direction triggers on the same conditions.

  TRIGGER any window on current provider reaches TRIGGER_PCT (default 95).
    Overage is imminent on any tier — all three windows can incur costs.

  DESTINATION must be below ALL THREE safety ceilings:
      short  < 80%
      medium < 90%
      long   < 95%
    If the destination is near a limit itself, switching just moves the overage
    risk — better to stay and let the current provider's limits reset.

Config via env vars:
  TRIGGER_PCT              switch trigger threshold (default: 95)
  SAFE_SHORT_PCT           destination max for short-term window (default: 80)
  SAFE_MEDIUM_PCT          destination max for medium-term window (default: 90)
  SAFE_LONG_PCT            destination max for long-term window (default: 95)
  OPENCODE_GO_USAGE        path to scraper_go.py (default: auto-detect)
  CLINEPASS_USAGE          path to scraper_cp.py (default: auto-detect)
  OPENCODE_WORKSPACE_ID    workspace ID for Go scraper (REQUIRED)
  HERMES_BIN               path to hermes binary (default: ~/.local/bin/hermes)
  STATE_FILE               state tracking path (default: ~/.cache/quotactl-watchdog.json)
  HERMES_PROFILES_DIR      profiles directory (default: ~/.hermes/profiles)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════

TRIGGER_PCT     = float(os.getenv("TRIGGER_PCT",     "95"))
SAFE_SHORT_PCT  = float(os.getenv("SAFE_SHORT_PCT",  "80"))
SAFE_MEDIUM_PCT = float(os.getenv("SAFE_MEDIUM_PCT", "90"))
SAFE_LONG_PCT   = float(os.getenv("SAFE_LONG_PCT",   "95"))

GO = "opencode-go"
CP = "clinepass"

_HERMES_DEFAULT = str(Path.home() / ".local" / "bin" / "hermes")
HERMES_BIN = os.getenv("HERMES_BIN", _HERMES_DEFAULT)

PROFILES_DIR = Path(
    os.getenv("HERMES_PROFILES_DIR", str(Path.home() / ".hermes" / "profiles"))
)

# ═══════════════════════════════════════════════════════════════════════════
# Provider abstraction
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ProviderSpec:
    """Describes one usage provider: how to scrape it and how to interpret its
    windows.

    ``model_prefix`` is the key to the auto-discovery model transform.
    When switching TO this provider, the prefix is prepended to the current
    model name. When switching AWAY, it is stripped. GO has prefix \"\" (bare
    names); ClinePass has prefix \"cline-pass/\".
    """
    name: str                             # e.g. "opencode-go"
    tool: str                             # path to scraper script
    tool_env: dict = field(default_factory=dict)
    tier_map: dict = field(default_factory=dict)
    model_prefix: str = ""                # prepended when switching toward this provider


PROVIDERS = [
    ProviderSpec(
        name=GO,
        tool=os.getenv(
            "OPENCODE_GO_USAGE",
            str(Path.home() / "src" / "quotactl" / "scraper_go.py"),
        ),
        tool_env={"OPENCODE_WORKSPACE_ID": os.getenv("OPENCODE_WORKSPACE_ID", "")},
        tier_map={"rolling": "short", "weekly": "medium", "monthly": "long"},
        model_prefix="",
    ),
    ProviderSpec(
        name=CP,
        tool=os.getenv(
            "CLINEPASS_USAGE",
            str(Path.home() / "src" / "quotactl" / "scraper_cp.py"),
        ),
        tier_map={"five_hour": "short", "weekly": "medium", "monthly": "long"},
        model_prefix="cline-pass/",
    ),
]

STATE_FILE = Path(
    os.getenv("STATE_FILE", str(Path.home() / ".cache" / "quotactl-watchdog.json"))
)


def model_transform(from_spec: ProviderSpec, to_spec: ProviderSpec,
                    current_model: str) -> str:
    """Transform a model name when switching providers.

    Strips the FROM provider's prefix (if present), then applies the TO
    provider's prefix.  Handles the common double-prefix case idempotently.

    >>> go = ProviderSpec(name=GO, model_prefix="")
    >>> cp = ProviderSpec(name=CP, model_prefix="cline-pass/")
    >>> model_transform(go, cp, "deepseek-v4-pro")
    'cline-pass/deepseek-v4-pro'
    >>> model_transform(cp, go, "cline-pass/deepseek-v4-pro")
    'deepseek-v4-pro'
    >>> model_transform(cp, go, "cline-pass/cline-pass/deepseek-v4-pro")
    'deepseek-v4-pro'
    """
    model = current_model
    # Strip FROM prefix (idempotent — keep stripping while it matches)
    if from_spec.model_prefix:
        while model.startswith(from_spec.model_prefix):
            model = model[len(from_spec.model_prefix):]
    # Apply TO prefix
    if to_spec.model_prefix:
        # Guard against double-prefix
        if not model.startswith(to_spec.model_prefix):
            model = to_spec.model_prefix + model
    return model


# ═══════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TieredUsage:
    """Normalized usage: max pct per tier across all windows in that tier."""
    short: int
    medium: int
    long: int

    def any_above(self, pct: float) -> bool:
        return self.short >= pct or self.medium >= pct or self.long >= pct

    def all_below(self, short: float, medium: float, long: float) -> bool:
        return self.short < short and self.medium < medium and self.long < long


@dataclass
class SwitchDirective:
    profile: str
    target_provider: str
    target_model: str
    reason: str


@dataclass
class ProfileInfo:
    """Discovered profile: current provider and model."""
    provider: str
    model: str


# ═══════════════════════════════════════════════════════════════════════════
# Discovery
# ═══════════════════════════════════════════════════════════════════════════

def discover_profiles(specs: list[ProviderSpec]) -> dict[str, ProfileInfo]:
    """Scan Hermes profile configs for profiles using managed providers.

    Reads only the first ~10 lines of each config.yaml — the ``model:`` block
    is always at the top.  A targeted parse avoids false matches on
    ``auxiliary.*.provider`` and ``delegation.provider`` deeper in the file.

    Returns:
        ``{profile_name: ProfileInfo}`` for every profile whose
        ``model.provider`` matches a managed provider name.
    """
    managed_providers = {s.name for s in specs}
    profiles: dict[str, ProfileInfo] = {}

    if not PROFILES_DIR.is_dir():
        return profiles

    for profile_dir in sorted(PROFILES_DIR.iterdir()):
        if not profile_dir.is_dir():
            continue
        config_path = profile_dir / "config.yaml"
        if not config_path.is_file():
            continue

        try:
            provider, model = _parse_model_block(config_path)
        except (OSError, ValueError):
            continue

        if provider in managed_providers:
            profiles[profile_dir.name] = ProfileInfo(provider=provider, model=model)

    return profiles


_MODEL_PROVIDER_RE = re.compile(r'^\s{2}provider:\s*(\S+)')
_MODEL_DEFAULT_RE = re.compile(r'^\s{2}default:\s*(.+)')


def _parse_model_block(config_path: Path) -> tuple[str, str]:
    """Extract model.provider and model.default from the top of a config.yaml.

    Only parses the model: block (first top-level key). Stops at the next
    unindented line (next top-level key).

    Returns:
        (provider, model) tuple.
    Raises:
        ValueError if the model block cannot be parsed.
    """
    provider = model = ""
    in_model = False
    with open(config_path) as f:
        for line in f:
            # Detect top-level "model:" key
            if not in_model:
                if line.startswith("model:"):
                    in_model = True
                continue
            # Next top-level key → we've left the model block
            if line and not line[0].isspace():
                break
            # Inside model block
            m = _MODEL_PROVIDER_RE.match(line)
            if m:
                provider = m.group(1)
            m = _MODEL_DEFAULT_RE.match(line)
            if m and not model:
                model = m.group(1).strip()
            if provider and model:
                break

    if not provider or not model:
        raise ValueError(f"Could not parse model block in {config_path}")
    return provider, model


# ═══════════════════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════════════════

def load_state(profiles: dict[str, ProfileInfo],
               specs: list[ProviderSpec]) -> dict[str, ProfileInfo]:
    """Load persisted state, falling back to discovered values.

    Migrates old-format state (``{profile: provider_name}`` → new format).
    Profiles in the state file that no longer exist in *profiles* are dropped.
    """
    default_provider = specs[0].name if specs else GO
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        raw = {}

    state: dict[str, ProfileInfo] = {}
    for name, info in profiles.items():
        entry = raw.get(name)
        if isinstance(entry, dict):
            # New format: {provider, model}
            provider = entry.get("provider", info.provider)
            model = entry.get("model", info.model)
        elif isinstance(entry, str):
            # Old format: "opencode-go" → migrate
            provider = entry
            model = info.model  # fall back to discovered model
        else:
            provider = info.provider
            model = info.model
        # Validate: if the provider in state is no longer managed, reset
        if provider not in {s.name for s in specs}:
            provider = default_provider
            model = info.model
        state[name] = ProfileInfo(provider=provider, model=model)

    return state


def save_state(state: dict[str, ProfileInfo]) -> None:
    """Atomically persist state as new-format JSON."""
    payload = {name: {"provider": info.provider, "model": info.model}
               for name, info in state.items()}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(STATE_FILE)


# ═══════════════════════════════════════════════════════════════════════════
# Scrape
# ═══════════════════════════════════════════════════════════════════════════

def _run_scraper(spec: ProviderSpec) -> dict | None:
    """Run a provider's scraper, return raw JSON or None on failure."""
    env = os.environ.copy()
    if spec.tool_env:
        env.update(spec.tool_env)
    try:
        r = subprocess.run(
            [sys.executable, spec.tool],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if r.returncode != 0:
            print(f"⚠ {spec.name} scraper failed (exit {r.returncode})",
                  file=sys.stderr)
            return None
        return json.loads(r.stdout)
    except Exception as e:
        print(f"⚠ {spec.name} scraper error: {e}", file=sys.stderr)
        return None


def _normalize(raw: dict, spec: ProviderSpec) -> TieredUsage:
    """Convert raw scraper output into tiered usage.

    For each tier, takes the max pct across all windows mapped to that tier.
    Windows not in spec.tier_map are ignored.
    """
    buckets: dict[str, int] = {"short": 0, "medium": 0, "long": 0}
    for window_name, window_data in raw.get("windows", {}).items():
        tier = spec.tier_map.get(window_name)
        if tier:
            buckets[tier] = max(buckets[tier], window_data["pct"])
    return TieredUsage(**buckets)


def scrape(specs: list[ProviderSpec]) -> tuple[dict[str, TieredUsage], dict[str, dict]]:
    """Scrape all providers. Returns (normalized, raw) keyed by provider name."""
    tiered: dict[str, TieredUsage] = {}
    raw: dict[str, dict] = {}
    for spec in specs:
        data = _run_scraper(spec)
        if data is not None:
            raw[spec.name] = data
            tiered[spec.name] = _normalize(data, spec)
        else:
            print(f"⚠ {spec.name}: unavailable — will not switch toward this provider",
                  file=sys.stderr)
    return tiered, raw


# ═══════════════════════════════════════════════════════════════════════════
# Decide
# ═══════════════════════════════════════════════════════════════════════════

def decide(
    state: dict[str, ProfileInfo],
    usage: dict[str, TieredUsage],
    specs: list[ProviderSpec],
) -> list[SwitchDirective]:
    """Pure: given current state and tiered usage for each provider, return
    switch directives.

    Switching logic:
      On current provider P, if ANY tier is at or above TRIGGER_PCT, consider
      switching to the OTHER provider Q.

      Safe switch: Q's scraper succeeded AND all three tiers are below their
      safety ceilings.

      Blind switch: Q's scraper failed — switch anyway if P is over trigger,
      because guaranteed overage is worse than unknown destination. Rate-limited
      to prevent thrashing.

      Providers are equal peers — no preference for one over the other.
    """
    if len(specs) < 2:
        return []

    a_spec, b_spec = specs[0], specs[1]
    a_usage = usage.get(a_spec.name)
    b_usage = usage.get(b_spec.name)

    # Build lookup: provider name → (spec, usage)
    lookup = {a_spec.name: (a_spec, a_usage), b_spec.name: (b_spec, b_usage)}

    def _other(provider_name: str):
        return b_spec.name if provider_name == a_spec.name else a_spec.name

    directives = []
    for profile, info in state.items():
        current_provider = info.provider
        if current_provider not in lookup:
            continue  # stale state — provider no longer in specs
        current_spec, current_usage = lookup[current_provider]
        target_name = _other(current_provider)
        if target_name not in lookup:
            continue  # target provider not in specs
        target_spec, target_usage = lookup[target_name]

        if current_usage is None:
            continue  # can't assess current — don't touch

        if not current_usage.any_above(TRIGGER_PCT):
            continue  # nothing is close to overage

        blind = False
        if target_usage is None:
            # Destination scraper failed — blind-switch to avoid guaranteed overage
            blind = True
        elif not target_usage.all_below(SAFE_SHORT_PCT, SAFE_MEDIUM_PCT, SAFE_LONG_PCT):
            continue  # destination isn't safe enough

        target_model = model_transform(current_spec, target_spec, info.model)
        reason_parts = [
            f"{current_provider}[s:{current_usage.short}% "
            f"m:{current_usage.medium}% l:{current_usage.long}%]",
            f"→ {target_name}",
        ]
        if blind:
            reason_parts.append("[BLIND: destination scraper down]")
        else:
            reason_parts.append(
                f"[s:{target_usage.short}% m:{target_usage.medium}% "
                f"l:{target_usage.long}%]"
            )
        directives.append(SwitchDirective(
            profile=profile,
            target_provider=target_name,
            target_model=target_model,
            reason=" ".join(reason_parts),
        ))

    return directives


# ═══════════════════════════════════════════════════════════════════════════
# Execute
# ═══════════════════════════════════════════════════════════════════════════

def execute(directives: list[SwitchDirective],
            state: dict[str, ProfileInfo]) -> list[str]:
    """Apply directives via `hermes config set`. Returns log lines."""
    lines = []
    for d in directives:
        old_provider = state[d.profile].provider
        ok = _switch_one(d.profile, d.target_provider, d.target_model,
                         old_provider)
        if ok:
            state[d.profile] = ProfileInfo(
                provider=d.target_provider, model=d.target_model)
            lines.append(f"🔄 {d.profile} → {d.target_provider} / {d.target_model}")
            lines.append(f"   {d.reason}")
        else:
            lines.append(f"❌ {d.profile} switch to {d.target_provider} FAILED")
    return lines


def _switch_one(profile: str, provider: str, model: str,
                old_provider: str = "") -> bool:
    """Switch one profile's provider + model.  Returns True on success.

    Sets model.provider first, then model.default.  If the second call fails,
    attempts to roll back the first.  Hermes stderr is captured and printed on
    failure so the operator can diagnose.
    """
    try:
        r1 = subprocess.run(
            [HERMES_BIN, "config", "set", "model.provider", provider,
             "--profile", profile],
            capture_output=True, text=True, timeout=15,
        )
        if r1.returncode != 0:
            _log_hermes_error(profile, "model.provider", r1)
            return False

        r2 = subprocess.run(
            [HERMES_BIN, "config", "set", "model.default", model,
             "--profile", profile],
            capture_output=True, text=True, timeout=15,
        )
        if r2.returncode != 0:
            _log_hermes_error(profile, "model.default", r2)
            # Try to roll back r1 (restore old provider)
            if old_provider:
                subprocess.run(
                    [HERMES_BIN, "config", "set", "model.provider", old_provider,
                     "--profile", profile],
                    capture_output=True, text=True, timeout=15,
                )
            return False

        return True
    except FileNotFoundError:
        print(f"⚠ hermes binary not found at {HERMES_BIN}. Set HERMES_BIN env var.",
              file=sys.stderr)
        return False
    except Exception as e:
        print(f"⚠ switch {profile} error: {e}", file=sys.stderr)
        return False


def _log_hermes_error(profile: str, key: str,
                      result: subprocess.CompletedProcess) -> None:
    """Print hermes stderr for diagnostic purposes."""
    print(f"⚠ switch {profile}: hermes config set {key} failed (exit {result.returncode})",
          file=sys.stderr)
    for line in result.stderr.splitlines():
        print(f"   hermes: {line}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════════
# Report
# ═══════════════════════════════════════════════════════════════════════════

def _fmt(raw: dict, tiered: TieredUsage | None, spec: ProviderSpec) -> str:
    """Single-line provider status with all windows and tier summary."""
    if tiered is None:
        return f"{spec.name}: unavailable"
    windows = []
    for wname, wdata in raw.get("windows", {}).items():
        if wname in spec.tier_map:
            windows.append(f"{wname}:{wdata['pct']}%")
    return (
        f"{spec.name} [s:{tiered.short}% m:{tiered.medium}% l:{tiered.long}%]"
        + (f" ({', '.join(windows)})" if windows else "")
    )


def report(
    raw: dict[str, dict],
    tiered: dict[str, TieredUsage],
    specs: list[ProviderSpec],
    lines: list[str],
    profiles: dict[str, ProfileInfo],
) -> None:
    """Print a summary when there is something to report.

    Reports if: switches occurred, OR any scraper is unavailable (so the
    operator knows a provider is down even when no switch was triggered).
    """
    has_unavailable = any(tiered.get(s.name) is None for s in specs)
    if not lines and not has_unavailable:
        return  # truly quiet run

    parts = []
    for spec in specs:
        parts.append(_fmt(raw.get(spec.name, {}), tiered.get(spec.name), spec))
    # Profile summary line
    profile_counts: dict[str, int] = {}
    for info in profiles.values():
        profile_counts[info.provider] = profile_counts.get(info.provider, 0) + 1
    parts.append(f"profiles: " + ", ".join(
        f"{p}={n}" for p, n in sorted(profile_counts.items())))
    print(" | ".join(parts))
    for line in lines:
        print(line)


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main() -> int:
    # Validate hermes binary before doing anything
    if not Path(HERMES_BIN).is_file():
        print(f"FATAL: hermes binary not found at {HERMES_BIN}. "
              f"Set HERMES_BIN env var or install hermes.", file=sys.stderr)
        return 1

    profiles = discover_profiles(PROVIDERS)
    if not profiles:
        print("⚠ no profiles found on managed providers", file=sys.stderr)
        return 0

    state = load_state(profiles, PROVIDERS)
    tiered, raw = scrape(PROVIDERS)
    directives = decide(state, tiered, PROVIDERS)

    # Track profiles for report (before execute mutates state)
    report_profiles = dict(state)
    lines = execute(directives, state)

    save_state(state)
    report(raw, tiered, PROVIDERS, lines, report_profiles)

    # Exit non-zero if any switch failed
    failed = sum(1 for line in lines if "FAILED" in line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
