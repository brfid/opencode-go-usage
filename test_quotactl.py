"""Tests for opencode_go_usage, clinepass_usage, _provider_base, and the watchdog.

Run with: pytest test_opencode_go_usage.py -v
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import _base as base
import scraper_cp as cp
import scraper_go as m

# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

# Trimmed, sanitized copy of the real /go page's inlined data.
GO_FIXTURE = (
    'workspaces[]"]=$R[5];...$R[29]=[$R[30]={id:"wrk_TESTWORKSPACE0001",name:"Default"}];'
    "$R[28]($R[18],$R[34]={mine:!0,useBalance:!0,region:$R[35]=[\"us\",\"eu\",\"sg\"],"
    'rollingUsage:$R[36]={status:"ok",resetInSec:14182,usagePercent:22},'
    'weeklyUsage:$R[37]={status:"ok",resetInSec:161609,usagePercent:43},'
    'monthlyUsage:$R[38]={status:"limited",resetInSec:504671,usagePercent:98}});'
    '<main data-page="workspace">bars</main>'
)

CP_FIXTURE = {
    "success": True,
    "data": {
        "limits": [
            {"type": "five_hour", "percentUsed": 65, "resetsAt": "2026-07-04T21:00:00Z"},
            {"type": "weekly", "percentUsed": 42, "resetsAt": "2026-07-11T00:00:00Z"},
            {"type": "monthly", "percentUsed": 88, "resetsAt": "2026-08-01T00:00:00Z"},
        ]
    },
}


class FakeResponse:
    """Stands in for the object ``urllib.request.urlopen`` returns."""

    def __init__(self, url: str, body: str):
        self._url = url
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def geturl(self) -> str:
        return self._url

    def read(self) -> bytes:
        return self._body.encode()


def _mock_urlopen(url: str, body: str):
    return patch("urllib.request.urlopen", return_value=FakeResponse(url, body))


# ═══════════════════════════════════════════════════════════════════════════
# _provider_base
# ═══════════════════════════════════════════════════════════════════════════


class TestFail:
    def test_exits_with_code(self):
        with pytest.raises(SystemExit) as exc:
            base.fail(2, "TEST", "something broke")
        assert exc.value.code == 2


class TestLoadCookie:
    def test_from_env_var_adds_prefix(self):
        with patch.dict(os.environ, {"TEST_COOKIE": "secret123"}, clear=True):
            result = base.load_cookie(
                env_var="TEST_COOKIE",
                cookie_name="auth",
                default_config_dir="test-usage",
            )
        assert result == "auth=secret123"

    def test_passes_through_full_cookie_header(self):
        with patch.dict(os.environ, {"TEST_COOKIE": "auth=secret; x=y"}, clear=True):
            result = base.load_cookie(
                env_var="TEST_COOKIE",
                cookie_name="auth",
                default_config_dir="test-usage",
            )
        assert result == "auth=secret; x=y"

    def test_reads_cookie_file(self, tmp_path):
        cookie_path = tmp_path / "cookie"
        cookie_path.write_text("fromfile\n")
        with patch.dict(os.environ, {}, clear=True):
            result = base.load_cookie(
                env_var="TEST_COOKIE",
                cookie_name="auth",
                default_config_dir="test-usage",
                cookie_file=str(cookie_path),
            )
        assert result == "auth=fromfile"

    def test_missing_everywhere_exits_1(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TEST_COOKIE", raising=False)
        bogus = tmp_path / "nonexistent"
        with patch.object(base.Path, "home", return_value=bogus):
            with pytest.raises(SystemExit) as exc:
                base.load_cookie(
                    env_var="TEST_COOKIE",
                    cookie_name="auth",
                    default_config_dir="test-usage",
                )
            assert exc.value.code == 1

    def test_bad_cookie_file_exits_1(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit) as exc:
                base.load_cookie(
                    env_var="TEST_COOKIE",
                    cookie_name="auth",
                    default_config_dir="test-usage",
                    cookie_file="/nonexistent/path/no-cookie",
                )
            assert exc.value.code == 1


class TestCheckThresholds:
    def test_fires_on_limit(self):
        windows = {
            "monthly": {"pct": 98, "status": "limited", "reset_in_sec": 504671},
            "rolling": {"pct": 22, "status": "ok", "reset_in_sec": 14182},
        }
        thresholds = {"rolling": 90.0, "monthly": 95.0}
        alerts = base.check_thresholds(windows, thresholds, "TEST_")
        assert any("monthly" in a for a in alerts)
        assert not any("rolling" in a for a in alerts)

    def test_respects_env_var_override(self):
        windows = {"rolling": {"pct": 50}}
        thresholds = {"rolling": 80.0}
        with patch.dict(os.environ, {"TEST_ROLLING_PCT": "45"}, clear=True):
            alerts = base.check_thresholds(windows, thresholds, "TEST_")
        assert len(alerts) == 1
        assert "50%" in alerts[0]

    def test_non_numeric_env_var_falls_back(self):
        windows = {"rolling": {"pct": 50}}
        thresholds = {"rolling": 80.0}
        with patch.dict(os.environ, {"TEST_ROLLING_PCT": "not-a-number"}, clear=True):
            alerts = base.check_thresholds(windows, thresholds, "TEST_")
        assert len(alerts) == 0  # 50 < 80 default

    def test_skips_missing_windows(self):
        windows = {"monthly": {"pct": 90}}
        thresholds = {"monthly": 95.0, "weekly": 85.0}
        alerts = base.check_thresholds(windows, thresholds, "TEST_")
        assert len(alerts) == 0  # weekly not in windows, monthly below threshold


class TestHttpGet:
    def test_success(self):
        with _mock_urlopen("https://example.com", "<html>ok</html>"):
            final_url, body = base.http_get(
                "https://example.com", "auth=test", user_agent="test/1.0"
            )
        assert final_url == "https://example.com"
        assert body == "<html>ok</html>"

    def test_401_exits_1(self):
        import urllib.error

        with patch("urllib.request.urlopen") as mock:
            mock.side_effect = urllib.error.HTTPError(
                "https://example.com", 401, "Unauthorized", {}, None
            )
            with pytest.raises(SystemExit) as exc:
                base.http_get("https://example.com", "auth=test", user_agent="test/1.0")
            assert exc.value.code == 1

    def test_403_exits_1(self):
        import urllib.error

        with patch("urllib.request.urlopen") as mock:
            mock.side_effect = urllib.error.HTTPError(
                "https://example.com", 403, "Forbidden", {}, None
            )
            with pytest.raises(SystemExit) as exc:
                base.http_get("https://example.com", "auth=test", user_agent="test/1.0")
            assert exc.value.code == 1

    def test_urlerror_exits_3(self):
        import urllib.error

        with patch("urllib.request.urlopen") as mock:
            mock.side_effect = urllib.error.URLError("connection refused")
            with pytest.raises(SystemExit) as exc:
                base.http_get("https://example.com", "auth=test", user_agent="test/1.0")
            assert exc.value.code == 3

    def test_500_exits_3(self):
        import urllib.error

        with patch("urllib.request.urlopen") as mock:
            mock.side_effect = urllib.error.HTTPError(
                "https://example.com", 500, "Error", {}, None
            )
            with pytest.raises(SystemExit) as exc:
                base.http_get("https://example.com", "auth=test", user_agent="test/1.0")
            assert exc.value.code == 3


# ═══════════════════════════════════════════════════════════════════════════
# opencode_go_usage
# ═══════════════════════════════════════════════════════════════════════════


class TestParseUsage:
    def test_extracts_all_windows(self):
        u = m.parse_usage(GO_FIXTURE)
        assert u["rolling"] == {"status": "ok", "reset_in_sec": 14182, "pct": 22}
        assert u["weekly"]["pct"] == 43
        assert u["monthly"] == {"status": "limited", "reset_in_sec": 504671, "pct": 98}

    def test_returns_none_on_missing(self):
        assert m.parse_usage("<html>no data here</html>") is None


class TestParseUseBalance:
    def test_true(self):
        assert m.parse_use_balance(GO_FIXTURE) is True

    def test_false(self):
        assert m.parse_use_balance("useBalance:!1,region:[]") is False

    def test_none(self):
        assert m.parse_use_balance("nothing") is None


class TestLooksLoggedOut:
    def test_auth_redirect(self):
        assert m._looks_logged_out("https://auth.opencode.ai/authorize", "wrk_x") is True

    def test_no_workspace_id_in_body(self):
        assert m._looks_logged_out("https://opencode.ai/", "<html>login</html>") is True

    def test_authenticated(self):
        assert m._looks_logged_out("https://opencode.ai/", GO_FIXTURE) is False


class TestFetch:
    def test_success(self):
        url = "https://opencode.ai/workspace/wrk_TESTWORKSPACE0001/go"
        with _mock_urlopen(url, GO_FIXTURE):
            result = m.fetch("auth=fake", "wrk_TESTWORKSPACE0001")
        assert result["workspace_id"] == "wrk_TESTWORKSPACE0001"
        assert result["windows"]["monthly"]["pct"] == 98
        assert result["use_balance"] is True

    def test_no_workspace_exits_1(self):
        with pytest.raises(SystemExit) as exc:
            m.fetch("auth=fake", None)
        assert exc.value.code == 1

    def test_logged_out_exits_1(self):
        with _mock_urlopen("https://auth.opencode.ai/authorize", "<html>login</html>"):
            with pytest.raises(SystemExit) as exc:
                m.fetch("auth=fake", "wrk_x")
            assert exc.value.code == 1

    def test_page_changed_exits_2(self):
        with _mock_urlopen(
            "https://opencode.ai/workspace/wrk_x/go", "wrk_x present but no usage data"
        ):
            with pytest.raises(SystemExit) as exc:
                m.fetch("auth=fake", "wrk_x")
            assert exc.value.code == 2


# ═══════════════════════════════════════════════════════════════════════════
# clinepass_usage
# ═══════════════════════════════════════════════════════════════════════════


class TestIsoToSeconds:
    def test_utc_z_suffix(self):
        future = "2026-12-31T00:00:00Z"
        result = cp._iso_to_seconds(future)
        assert result > 0

    def test_past_returns_0(self):
        past = "2020-01-01T00:00:00Z"
        assert cp._iso_to_seconds(past) == 0

    def test_malformed_returns_0(self):
        assert cp._iso_to_seconds("not-a-date") == 0


class TestClinepassFetch:
    def test_success(self):
        with _mock_urlopen(cp.ENDPOINT, json.dumps(CP_FIXTURE)):
            result = cp.fetch("cline_session_id=test")
        assert result["provider"] == "clinepass"
        assert result["windows"]["five_hour"]["pct"] == 65
        assert result["windows"]["weekly"]["pct"] == 42
        assert result["windows"]["monthly"]["pct"] == 88
        assert result["windows"]["five_hour"]["status"] == "ok"

    def test_exhausted_status(self):
        data = dict(CP_FIXTURE)
        data["data"]["limits"][0]["percentUsed"] = 100
        with _mock_urlopen(cp.ENDPOINT, json.dumps(data)):
            result = cp.fetch("cline_session_id=test")
        assert result["windows"]["five_hour"]["status"] == "exhausted"

    def test_non_json_exits_2(self):
        with _mock_urlopen(cp.ENDPOINT, "not json"):
            with pytest.raises(SystemExit) as exc:
                cp.fetch("cline_session_id=test")
            assert exc.value.code == 2

    def test_json_array_exits_2(self):
        """Valid JSON that isn't a dict (e.g. []) → exit 2."""
        with _mock_urlopen(cp.ENDPOINT, json.dumps([1, 2, 3])):
            with pytest.raises(SystemExit) as exc:
                cp.fetch("cline_session_id=test")
            assert exc.value.code == 2

    def test_success_false_exits_2(self):
        with _mock_urlopen(cp.ENDPOINT, json.dumps({"success": False, "error": "nope"})):
            with pytest.raises(SystemExit) as exc:
                cp.fetch("cline_session_id=test")
            assert exc.value.code == 2

    def test_missing_limits_exits_2(self):
        with _mock_urlopen(cp.ENDPOINT, json.dumps({"success": True, "data": {}})):
            with pytest.raises(SystemExit) as exc:
                cp.fetch("cline_session_id=test")
            assert exc.value.code == 2

    def test_missing_field_in_limit_exits_2(self):
        data = {"success": True, "data": {"limits": [{"type": "test"}]}}  # no percentUsed
        with _mock_urlopen(cp.ENDPOINT, json.dumps(data)):
            with pytest.raises(SystemExit) as exc:
                cp.fetch("cline_session_id=test")
            assert exc.value.code == 2

    def test_401_exits_1(self):
        import urllib.error

        with patch("urllib.request.urlopen") as mock:
            mock.side_effect = urllib.error.HTTPError(
                cp.ENDPOINT, 401, "Unauthorized", {}, None
            )
            with pytest.raises(SystemExit) as exc:
                cp.fetch("cline_session_id=test")
            assert exc.value.code == 1

    def test_500_exits_3(self):
        import urllib.error

        with patch("urllib.request.urlopen") as mock:
            mock.side_effect = urllib.error.HTTPError(
                cp.ENDPOINT, 500, "Error", {}, None
            )
            with pytest.raises(SystemExit) as exc:
                cp.fetch("cline_session_id=test")
            assert exc.value.code == 3


# ═══════════════════════════════════════════════════════════════════════════
# Watchdog (ProviderSpec-based, from the real watchdog)
# ═══════════════════════════════════════════════════════════════════════════

# Import watchdog components for testing pure functions
sys.path.insert(0, str(Path(__file__).parent))
import watchdog as wd  # noqa: E402


class TestTieredUsage:
    def test_any_above(self):
        tu = wd.TieredUsage(short=85, medium=50, long=10)
        assert tu.any_above(80) is True
        assert tu.any_above(90) is False

    def test_any_above_boundary(self):
        """any_above uses >= so exact boundary counts as above."""
        tu = wd.TieredUsage(short=95, medium=50, long=10)
        assert tu.any_above(95) is True

    def test_all_below(self):
        tu = wd.TieredUsage(short=70, medium=80, long=90)
        assert tu.all_below(80, 90, 95) is True
        tu2 = wd.TieredUsage(short=85, medium=50, long=10)
        assert tu2.all_below(80, 90, 95) is False


class TestDecide:
    """Test the pure decide() function — no I/O, no side effects."""

    def _specs(self):
        return [
            wd.ProviderSpec(
                name="opencode-go",
                tool="/fake/go",
                tier_map={"rolling": "short", "weekly": "medium", "monthly": "long"},
                model_prefix="",
            ),
            wd.ProviderSpec(
                name="clinepass",
                tool="/fake/cp",
                tier_map={"five_hour": "short", "weekly": "medium", "monthly": "long"},
                model_prefix="cline-pass/",
            ),
        ]

    def _usage(self, short=50, medium=50, long=50):
        return wd.TieredUsage(short=short, medium=medium, long=long)

    def _state(self, profile="test-profile", provider="opencode-go",
               model="deepseek-v4-pro"):
        return {profile: wd.ProfileInfo(provider=provider, model=model)}

    def test_no_switch_when_below_trigger(self, monkeypatch):
        specs = self._specs()
        state = self._state()
        usage = {
            "opencode-go": self._usage(short=50, medium=50, long=50),
            "clinepass": self._usage(),
        }
        directives = wd.decide(state, usage, specs)
        assert len(directives) == 0

    def test_switch_when_above_trigger_and_dest_safe(self, monkeypatch):
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)
        specs = self._specs()
        state = self._state()
        usage = {
            "opencode-go": self._usage(short=50, medium=50, long=96),
            "clinepass": self._usage(short=50, medium=50, long=50),
        }
        directives = wd.decide(state, usage, specs)
        assert len(directives) == 1
        assert directives[0].profile == "test-profile"
        assert directives[0].target_provider == "clinepass"
        assert directives[0].target_model == "cline-pass/deepseek-v4-pro"

    def test_no_switch_when_dest_unsafe(self, monkeypatch):
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)
        specs = self._specs()
        state = self._state()
        usage = {
            "opencode-go": self._usage(short=50, medium=50, long=96),
            "clinepass": self._usage(short=85, medium=50, long=50),
        }
        directives = wd.decide(state, usage, specs)
        assert len(directives) == 0

    def test_blind_switch_when_dest_unavailable(self, monkeypatch):
        """Blind-switch: destination scraper down → switch anyway to
        avoid guaranteed overage."""
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)
        specs = self._specs()
        state = self._state()
        usage = {
            "opencode-go": self._usage(short=50, medium=50, long=96),
            # clinepass scraper failed — not in usage dict
        }
        directives = wd.decide(state, usage, specs)
        assert len(directives) == 1
        assert directives[0].target_provider == "clinepass"
        assert "BLIND" in directives[0].reason

    def test_no_blind_switch_when_both_unavailable(self, monkeypatch):
        """Both scrapers down → can't assess current, stay put."""
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)
        specs = self._specs()
        state = self._state()
        usage = {}  # neither scraper returned data
        directives = wd.decide(state, usage, specs)
        assert len(directives) == 0

    def test_switch_back_when_both_over_trigger_diff_dest(self, monkeypatch):
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)
        specs = self._specs()
        state = self._state(provider="clinepass", model="cline-pass/deepseek-v4-pro")
        usage = {
            "opencode-go": self._usage(short=10, medium=10, long=10),
            "clinepass": self._usage(short=50, medium=96, long=50),
        }
        directives = wd.decide(state, usage, specs)
        assert len(directives) == 1
        assert directives[0].target_provider == "opencode-go"
        assert directives[0].target_model == "deepseek-v4-pro"

    def test_stay_when_both_over_trigger(self, monkeypatch):
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)
        specs = self._specs()
        state = self._state()
        usage = {
            "opencode-go": self._usage(short=50, medium=50, long=96),
            "clinepass": self._usage(short=50, medium=96, long=50),
        }
        directives = wd.decide(state, usage, specs)
        assert len(directives) == 0

    def test_stale_state_provider_skipped(self, monkeypatch):
        specs = self._specs()
        state = {**self._state(),
                 "ghost": wd.ProfileInfo(provider="nonexistent", model="x")}
        usage = {
            "opencode-go": self._usage(),
            "clinepass": self._usage(),
        }
        directives = wd.decide(state, usage, specs)
        assert len(directives) == 0

    def test_switch_at_exact_trigger_boundary(self, monkeypatch):
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)
        specs = self._specs()
        state = self._state()
        usage = {
            "opencode-go": self._usage(short=50, medium=95, long=50),
            "clinepass": self._usage(short=10, medium=10, long=10),
        }
        directives = wd.decide(state, usage, specs)
        assert len(directives) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ═══════════════════════════════════════════════════════════════════════════
# ProfileInfo dataclass
# ═══════════════════════════════════════════════════════════════════════════


class TestProfileInfo:
    def test_fields_accessible(self):
        info = wd.ProfileInfo(provider="opencode-go", model="deepseek-v4-pro")
        assert info.provider == "opencode-go"
        assert info.model == "deepseek-v4-pro"

    def test_equality(self):
        a = wd.ProfileInfo(provider="opencode-go", model="m")
        b = wd.ProfileInfo(provider="opencode-go", model="m")
        assert a == b

    def test_inequality(self):
        a = wd.ProfileInfo(provider="opencode-go", model="m1")
        b = wd.ProfileInfo(provider="clinepass", model="m1")
        assert a != b


# ═══════════════════════════════════════════════════════════════════════════
# model_transform()
# ═══════════════════════════════════════════════════════════════════════════


class TestModelTransform:
    def _specs(self):
        go = wd.ProviderSpec(name=wd.GO, tool="/fake/go", model_prefix="")
        cp = wd.ProviderSpec(name=wd.CP, tool="/fake/cp", model_prefix="cline-pass/")
        return go, cp

    def test_go_to_cp_prepends_prefix(self):
        go, cp = self._specs()
        assert wd.model_transform(go, cp, "deepseek-v4-pro") == "cline-pass/deepseek-v4-pro"

    def test_cp_to_go_strips_prefix(self):
        go, cp = self._specs()
        assert wd.model_transform(cp, go, "cline-pass/deepseek-v4-pro") == "deepseek-v4-pro"

    def test_double_prefix_stripped_idempotently(self):
        go, cp = self._specs()
        assert wd.model_transform(cp, go, "cline-pass/cline-pass/deepseek-v4-pro") == "deepseek-v4-pro"

    def test_go_to_cp_already_prefixed_no_double(self):
        go, cp = self._specs()
        assert wd.model_transform(go, cp, "cline-pass/deepseek-v4-pro") == "cline-pass/deepseek-v4-pro"

    def test_both_empty_prefix_unchanged(self):
        a = wd.ProviderSpec(name="a", tool="/a", model_prefix="")
        b = wd.ProviderSpec(name="b", tool="/b", model_prefix="")
        assert wd.model_transform(a, b, "some-model") == "some-model"

    def test_go_to_go_noop(self):
        go, _ = self._specs()
        assert wd.model_transform(go, go, "deepseek-v4-pro") == "deepseek-v4-pro"

    def test_from_prefix_not_in_model_unchanged(self):
        x = wd.ProviderSpec(name="x", tool="/x", model_prefix="other/")
        go = wd.ProviderSpec(name=wd.GO, tool="/go", model_prefix="")
        assert wd.model_transform(x, go, "deepseek-v4-pro") == "deepseek-v4-pro"

    def test_empty_model_string(self):
        go, cp = self._specs()
        assert wd.model_transform(go, cp, "") == "cline-pass/"

    def test_cp_to_cp_noop(self):
        _, cp = self._specs()
        assert wd.model_transform(cp, cp, "cline-pass/deepseek-v4-pro") == "cline-pass/deepseek-v4-pro"

    def test_triple_prefix_fully_stripped(self):
        go, cp = self._specs()
        result = wd.model_transform(cp, go, "cline-pass/cline-pass/cline-pass/m")
        assert result == "m"


# ═══════════════════════════════════════════════════════════════════════════
# _parse_model_block()
# ═══════════════════════════════════════════════════════════════════════════


class TestParseModelBlock:
    def test_basic_config(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "model:\n"
            "  provider: opencode-go\n"
            "  default: deepseek-v4-pro\n"
            "other:\n"
            "  key: value\n"
        )
        provider, model = wd._parse_model_block(cfg)
        assert provider == "opencode-go"
        assert model == "deepseek-v4-pro"

    def test_clinepass_prefixed_model(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "model:\n"
            "  provider: clinepass\n"
            "  default: cline-pass/deepseek-v4-pro\n"
        )
        provider, model = wd._parse_model_block(cfg)
        assert provider == "clinepass"
        assert model == "cline-pass/deepseek-v4-pro"

    def test_stops_at_next_top_level_key(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "model:\n"
            "  provider: opencode-go\n"
            "  default: mymodel\n"
            "auxiliary:\n"
            "  provider: should-be-ignored\n"
        )
        provider, model = wd._parse_model_block(cfg)
        assert provider == "opencode-go"
        assert model == "mymodel"

    def test_extra_whitespace_in_default_trimmed(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "model:\n"
            "  provider: opencode-go\n"
            "  default:   deepseek-v4-pro   \n"
        )
        _, model = wd._parse_model_block(cfg)
        assert model == "deepseek-v4-pro"

    def test_missing_provider_raises(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("model:\n  default: mymodel\n")
        with pytest.raises(ValueError):
            wd._parse_model_block(cfg)

    def test_missing_default_raises(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("model:\n  provider: opencode-go\n")
        with pytest.raises(ValueError):
            wd._parse_model_block(cfg)

    def test_no_model_block_raises(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("other:\n  key: value\n")
        with pytest.raises(ValueError):
            wd._parse_model_block(cfg)

    def test_model_key_not_at_start_ignored(self, tmp_path):
        # model: indented → not a top-level key, should raise ValueError
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "top:\n"
            "  model:\n"
            "    provider: opencode-go\n"
            "    default: m\n"
        )
        with pytest.raises(ValueError):
            wd._parse_model_block(cfg)

    def test_provider_after_default_still_parsed(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "model:\n"
            "  default: deepseek-v4-pro\n"
            "  provider: opencode-go\n"
        )
        provider, model = wd._parse_model_block(cfg)
        assert provider == "opencode-go"
        assert model == "deepseek-v4-pro"


# ═══════════════════════════════════════════════════════════════════════════
# discover_profiles()
# ═══════════════════════════════════════════════════════════════════════════


class TestDiscoverProfiles:
    def _write_config(self, path, provider, model):
        path.write_text(
            f"model:\n"
            f"  provider: {provider}\n"
            f"  default: {model}\n"
        )

    def _specs(self):
        return [
            wd.ProviderSpec(name="opencode-go", tool="/fake/go", model_prefix=""),
            wd.ProviderSpec(name="clinepass", tool="/fake/cp", model_prefix="cline-pass/"),
        ]

    def test_finds_managed_profile(self, tmp_path, monkeypatch):
        profiles_dir = tmp_path / "profiles"
        p = profiles_dir / "test-profile"
        p.mkdir(parents=True)
        self._write_config(p / "config.yaml", "opencode-go", "deepseek-v4-pro")
        monkeypatch.setattr(wd, "PROFILES_DIR", profiles_dir)
        result = wd.discover_profiles(self._specs())
        assert "test-profile" in result
        assert result["test-profile"].provider == "opencode-go"
        assert result["test-profile"].model == "deepseek-v4-pro"

    def test_skips_unmanaged_provider(self, tmp_path, monkeypatch):
        profiles_dir = tmp_path / "profiles"
        p = profiles_dir / "other"
        p.mkdir(parents=True)
        self._write_config(p / "config.yaml", "anthropic", "claude-3-5-sonnet")
        monkeypatch.setattr(wd, "PROFILES_DIR", profiles_dir)
        result = wd.discover_profiles(self._specs())
        assert len(result) == 0

    def test_empty_profiles_dir(self, tmp_path, monkeypatch):
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        monkeypatch.setattr(wd, "PROFILES_DIR", profiles_dir)
        assert wd.discover_profiles(self._specs()) == {}

    def test_nonexistent_profiles_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wd, "PROFILES_DIR", tmp_path / "nonexistent")
        assert wd.discover_profiles(self._specs()) == {}

    def test_skips_profile_without_config_yaml(self, tmp_path, monkeypatch):
        profiles_dir = tmp_path / "profiles"
        (profiles_dir / "no-config").mkdir(parents=True)
        monkeypatch.setattr(wd, "PROFILES_DIR", profiles_dir)
        assert wd.discover_profiles(self._specs()) == {}

    def test_skips_malformed_config(self, tmp_path, monkeypatch):
        profiles_dir = tmp_path / "profiles"
        p = profiles_dir / "bad"
        p.mkdir(parents=True)
        (p / "config.yaml").write_text("no model block here\n")
        monkeypatch.setattr(wd, "PROFILES_DIR", profiles_dir)
        assert wd.discover_profiles(self._specs()) == {}

    def test_skips_files_not_directories(self, tmp_path, monkeypatch):
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        (profiles_dir / "notadir.txt").write_text("stray file")
        monkeypatch.setattr(wd, "PROFILES_DIR", profiles_dir)
        assert wd.discover_profiles(self._specs()) == {}

    def test_mixed_profiles_returns_only_managed(self, tmp_path, monkeypatch):
        profiles_dir = tmp_path / "profiles"
        for name, prov, model in [
            ("alpha", "opencode-go", "m1"),
            ("beta", "clinepass", "cline-pass/m2"),
            ("gamma", "anthropic", "claude-3-5-sonnet"),
        ]:
            p = profiles_dir / name
            p.mkdir(parents=True)
            self._write_config(p / "config.yaml", prov, model)
        monkeypatch.setattr(wd, "PROFILES_DIR", profiles_dir)
        result = wd.discover_profiles(self._specs())
        assert set(result.keys()) == {"alpha", "beta"}
        assert result["alpha"].provider == "opencode-go"
        assert result["beta"].provider == "clinepass"

    def test_results_sorted_by_profile_name(self, tmp_path, monkeypatch):
        profiles_dir = tmp_path / "profiles"
        for name in ["zzz", "aaa", "mmm"]:
            p = profiles_dir / name
            p.mkdir(parents=True)
            self._write_config(p / "config.yaml", "opencode-go", "m")
        monkeypatch.setattr(wd, "PROFILES_DIR", profiles_dir)
        result = wd.discover_profiles(self._specs())
        assert list(result.keys()) == ["aaa", "mmm", "zzz"]


# ═══════════════════════════════════════════════════════════════════════════
# load_state() — migration and fallback behaviour
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadState:
    def _specs(self):
        return [
            wd.ProviderSpec(name="opencode-go", tool="/fake/go", model_prefix=""),
            wd.ProviderSpec(name="clinepass", tool="/fake/cp", model_prefix="cline-pass/"),
        ]

    def _profiles(self):
        return {"test-profile": wd.ProfileInfo(provider="opencode-go", model="deepseek-v4-pro")}

    def test_no_state_file_uses_discovered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wd, "STATE_FILE", tmp_path / "nonexistent.json")
        state = wd.load_state(self._profiles(), self._specs())
        assert state["test-profile"].provider == "opencode-go"
        assert state["test-profile"].model == "deepseek-v4-pro"

    def test_new_format_loaded(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "test-profile": {"provider": "clinepass", "model": "cline-pass/deepseek-v4-pro"}
        }))
        monkeypatch.setattr(wd, "STATE_FILE", state_file)
        state = wd.load_state(self._profiles(), self._specs())
        assert state["test-profile"].provider == "clinepass"
        assert state["test-profile"].model == "cline-pass/deepseek-v4-pro"

    def test_old_format_string_migrates_provider(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"test-profile": "clinepass"}))
        monkeypatch.setattr(wd, "STATE_FILE", state_file)
        state = wd.load_state(self._profiles(), self._specs())
        assert state["test-profile"].provider == "clinepass"
        # model falls back to discovered value when old format has none
        assert state["test-profile"].model == "deepseek-v4-pro"

    def test_old_format_go_string(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"test-profile": "opencode-go"}))
        monkeypatch.setattr(wd, "STATE_FILE", state_file)
        state = wd.load_state(self._profiles(), self._specs())
        assert state["test-profile"].provider == "opencode-go"

    def test_stale_profile_in_state_file_dropped(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "test-profile": {"provider": "opencode-go", "model": "m"},
            "ghost": {"provider": "opencode-go", "model": "m"},
        }))
        monkeypatch.setattr(wd, "STATE_FILE", state_file)
        state = wd.load_state(self._profiles(), self._specs())
        assert "ghost" not in state
        assert "test-profile" in state

    def test_unmanaged_provider_in_state_resets_to_first_spec(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "test-profile": {"provider": "defunct-provider", "model": "old-model"},
        }))
        monkeypatch.setattr(wd, "STATE_FILE", state_file)
        state = wd.load_state(self._profiles(), self._specs())
        assert state["test-profile"].provider == "opencode-go"   # resets to specs[0]
        assert state["test-profile"].model == "deepseek-v4-pro"  # falls back to discovered

    def test_corrupted_json_uses_discovered(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text("{ not valid json !!!")
        monkeypatch.setattr(wd, "STATE_FILE", state_file)
        state = wd.load_state(self._profiles(), self._specs())
        assert state["test-profile"].provider == "opencode-go"

    def test_empty_profiles_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wd, "STATE_FILE", tmp_path / "state.json")
        assert wd.load_state({}, self._specs()) == {}

    def test_partial_new_format_missing_model_falls_back(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"test-profile": {"provider": "clinepass"}}))
        monkeypatch.setattr(wd, "STATE_FILE", state_file)
        state = wd.load_state(self._profiles(), self._specs())
        assert state["test-profile"].provider == "clinepass"
        assert state["test-profile"].model == "deepseek-v4-pro"  # falls back to discovered


# ═══════════════════════════════════════════════════════════════════════════
# save_state() — atomicity
# ═══════════════════════════════════════════════════════════════════════════


class TestSaveState:
    def test_writes_new_format(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(wd, "STATE_FILE", state_file)
        wd.save_state({"test-profile": wd.ProfileInfo(provider="clinepass", model="cline-pass/m1")})
        data = json.loads(state_file.read_text())
        assert data["test-profile"] == {"provider": "clinepass", "model": "cline-pass/m1"}

    def test_tmp_file_not_left_behind(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(wd, "STATE_FILE", state_file)
        wd.save_state({"a": wd.ProfileInfo(provider="opencode-go", model="m")})
        assert not state_file.with_suffix(".tmp").exists()

    def test_creates_parent_directories(self, tmp_path, monkeypatch):
        state_file = tmp_path / "deep" / "nested" / "state.json"
        monkeypatch.setattr(wd, "STATE_FILE", state_file)
        wd.save_state({"a": wd.ProfileInfo(provider="opencode-go", model="m")})
        assert state_file.exists()

    def test_overwrites_existing_state(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(wd, "STATE_FILE", state_file)
        wd.save_state({"p": wd.ProfileInfo(provider="opencode-go", model="m1")})
        wd.save_state({"p": wd.ProfileInfo(provider="clinepass", model="m2")})
        data = json.loads(state_file.read_text())
        assert data["p"]["provider"] == "clinepass"

    def test_roundtrip_via_load(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(wd, "STATE_FILE", state_file)
        specs = [
            wd.ProviderSpec(name="opencode-go", tool="/go", model_prefix=""),
            wd.ProviderSpec(name="clinepass", tool="/cp", model_prefix="cline-pass/"),
        ]
        profiles = {"p1": wd.ProfileInfo(provider="opencode-go", model="m1")}
        saved = {"p1": wd.ProfileInfo(provider="clinepass", model="cline-pass/m1")}
        wd.save_state(saved)
        loaded = wd.load_state(profiles, specs)
        assert loaded["p1"].provider == "clinepass"
        assert loaded["p1"].model == "cline-pass/m1"


# ═══════════════════════════════════════════════════════════════════════════
# decide() — blind-switch edge cases not covered in TestDecide
# ═══════════════════════════════════════════════════════════════════════════


class TestDecideBlindSwitchEdgeCases:
    def _specs(self):
        return [
            wd.ProviderSpec(name="opencode-go", tool="/fake/go", model_prefix=""),
            wd.ProviderSpec(name="clinepass", tool="/fake/cp", model_prefix="cline-pass/"),
        ]

    def test_current_scraper_down_no_switch(self, monkeypatch):
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)
        state = {"p": wd.ProfileInfo(provider="opencode-go", model="m")}
        usage = {"clinepass": wd.TieredUsage(short=10, medium=10, long=10)}
        directives = wd.decide(state, usage, self._specs())
        assert len(directives) == 0

    def test_blind_switch_reason_contains_label(self, monkeypatch):
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)
        state = {"p": wd.ProfileInfo(provider="opencode-go", model="deepseek-v4-pro")}
        usage = {"opencode-go": wd.TieredUsage(short=50, medium=50, long=96)}
        directives = wd.decide(state, usage, self._specs())
        assert len(directives) == 1
        assert "[BLIND: destination scraper down]" in directives[0].reason

    def test_blind_switch_model_still_transformed(self, monkeypatch):
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)
        state = {"p": wd.ProfileInfo(provider="opencode-go", model="deepseek-v4-pro")}
        usage = {"opencode-go": wd.TieredUsage(short=50, medium=50, long=96)}
        directives = wd.decide(state, usage, self._specs())
        assert directives[0].target_model == "cline-pass/deepseek-v4-pro"

    def test_no_switch_when_at_trigger_minus_one(self, monkeypatch):
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)
        state = {"p": wd.ProfileInfo(provider="opencode-go", model="m")}
        usage = {
            "opencode-go": wd.TieredUsage(short=94, medium=94, long=94),
            "clinepass": wd.TieredUsage(short=10, medium=10, long=10),
        }
        directives = wd.decide(state, usage, self._specs())
        assert len(directives) == 0


# ═══════════════════════════════════════════════════════════════════════════
# execute()
# ═══════════════════════════════════════════════════════════════════════════


class TestExecute:
    def _directive(self, profile="test-profile", target_provider="clinepass",
                   target_model="cline-pass/deepseek-v4-pro"):
        return wd.SwitchDirective(
            profile=profile,
            target_provider=target_provider,
            target_model=target_model,
            reason="test reason",
        )

    def test_successful_switch_updates_state(self, monkeypatch):
        state = {"test-profile": wd.ProfileInfo(provider="opencode-go", model="deepseek-v4-pro")}
        monkeypatch.setattr(wd, "_switch_one", lambda *a: True)
        wd.execute([self._directive()], state)
        assert state["test-profile"].provider == "clinepass"
        assert state["test-profile"].model == "cline-pass/deepseek-v4-pro"

    def test_successful_switch_log_line_contains_profile_and_provider(self, monkeypatch):
        state = {"test-profile": wd.ProfileInfo(provider="opencode-go", model="deepseek-v4-pro")}
        monkeypatch.setattr(wd, "_switch_one", lambda *a: True)
        lines = wd.execute([self._directive()], state)
        assert any("test-profile" in l and "clinepass" in l for l in lines)

    def test_failed_switch_does_not_update_state(self, monkeypatch):
        state = {"test-profile": wd.ProfileInfo(provider="opencode-go", model="deepseek-v4-pro")}
        monkeypatch.setattr(wd, "_switch_one", lambda *a: False)
        wd.execute([self._directive()], state)
        assert state["test-profile"].provider == "opencode-go"

    def test_failed_switch_log_line_contains_failed(self, monkeypatch):
        state = {"test-profile": wd.ProfileInfo(provider="opencode-go", model="deepseek-v4-pro")}
        monkeypatch.setattr(wd, "_switch_one", lambda *a: False)
        lines = wd.execute([self._directive()], state)
        assert any("FAILED" in l for l in lines)

    def test_empty_directives_returns_empty(self, monkeypatch):
        monkeypatch.setattr(wd, "_switch_one", lambda *a: True)
        assert wd.execute([], {}) == []

    def test_partial_failure_only_successful_updated(self, monkeypatch):
        state = {
            "p1": wd.ProfileInfo(provider="opencode-go", model="m"),
            "p2": wd.ProfileInfo(provider="opencode-go", model="m"),
        }
        calls = {"n": 0}
        def fake_switch(profile, provider, model, old_provider=""):
            calls["n"] += 1
            return calls["n"] == 1  # first succeeds, second fails
        monkeypatch.setattr(wd, "_switch_one", fake_switch)
        d1 = wd.SwitchDirective("p1", "clinepass", "cline-pass/m", "r")
        d2 = wd.SwitchDirective("p2", "clinepass", "cline-pass/m", "r")
        lines = wd.execute([d1, d2], state)
        assert state["p1"].provider == "clinepass"
        assert state["p2"].provider == "opencode-go"
        assert any("FAILED" in l for l in lines)


# ═══════════════════════════════════════════════════════════════════════════
# _switch_one()
# ═══════════════════════════════════════════════════════════════════════════


class TestSwitchOne:
    def _result(self, returncode=0, stderr=""):
        from types import SimpleNamespace
        return SimpleNamespace(returncode=returncode, stderr=stderr, stdout="")

    def test_success_returns_true(self, tmp_path, monkeypatch):
        import subprocess
        fake_hermes = tmp_path / "hermes"
        fake_hermes.touch()
        monkeypatch.setattr(wd, "HERMES_BIN", str(fake_hermes))
        results = [self._result(0), self._result(0)]
        idx = {"i": 0}
        def fake_run(cmd, **kw):
            r = results[idx["i"]]; idx["i"] += 1; return r
        monkeypatch.setattr(subprocess, "run", fake_run)
        assert wd._switch_one("test-profile", "clinepass", "cline-pass/m") is True

    def test_first_command_failure_returns_false(self, tmp_path, monkeypatch):
        import subprocess
        fake_hermes = tmp_path / "hermes"
        fake_hermes.touch()
        monkeypatch.setattr(wd, "HERMES_BIN", str(fake_hermes))
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: self._result(1, "err"))
        assert wd._switch_one("test-profile", "clinepass", "cline-pass/m") is False

    def test_second_command_failure_attempts_rollback(self, tmp_path, monkeypatch):
        import subprocess
        fake_hermes = tmp_path / "hermes"
        fake_hermes.touch()
        monkeypatch.setattr(wd, "HERMES_BIN", str(fake_hermes))
        calls = {"n": 0}
        def fake_run(cmd, **kw):
            calls["n"] += 1
            return self._result(0 if calls["n"] == 1 else 1, "err")
        monkeypatch.setattr(subprocess, "run", fake_run)
        result = wd._switch_one("test-profile", "clinepass", "cline-pass/m",
                                old_provider="opencode-go")
        assert result is False
        assert calls["n"] >= 3  # set provider, set model (fail), rollback

    def test_file_not_found_returns_false(self, tmp_path, monkeypatch):
        import subprocess
        fake_hermes = tmp_path / "hermes"
        fake_hermes.touch()
        monkeypatch.setattr(wd, "HERMES_BIN", str(fake_hermes))
        def fake_run(cmd, **kw):
            raise FileNotFoundError("hermes not found")
        monkeypatch.setattr(subprocess, "run", fake_run)
        assert wd._switch_one("test-profile", "clinepass", "cline-pass/m") is False


# ═══════════════════════════════════════════════════════════════════════════
# report()
# ═══════════════════════════════════════════════════════════════════════════


class TestReport:
    def _specs(self):
        return [
            wd.ProviderSpec(
                name="opencode-go", tool="/fake/go",
                tier_map={"rolling": "short", "weekly": "medium", "monthly": "long"},
                model_prefix="",
            ),
            wd.ProviderSpec(
                name="clinepass", tool="/fake/cp",
                tier_map={"five_hour": "short", "weekly": "medium", "monthly": "long"},
                model_prefix="cline-pass/",
            ),
        ]

    def _tiered(self, short=50, medium=50, long=50):
        return wd.TieredUsage(short=short, medium=medium, long=long)

    def test_silent_when_no_lines_all_scrapers_ok(self, capsys):
        tiered = {"opencode-go": self._tiered(), "clinepass": self._tiered()}
        raw = {"opencode-go": {}, "clinepass": {}}
        profiles = {"test-profile": wd.ProfileInfo(provider="opencode-go", model="m")}
        wd.report(raw, tiered, self._specs(), [], profiles)
        assert capsys.readouterr().out == ""

    def test_prints_when_scraper_unavailable(self, capsys):
        tiered = {"opencode-go": self._tiered()}   # clinepass absent
        raw = {"opencode-go": {}}
        profiles = {"test-profile": wd.ProfileInfo(provider="opencode-go", model="m")}
        wd.report(raw, tiered, self._specs(), [], profiles)
        assert "unavailable" in capsys.readouterr().out

    def test_prints_when_switch_lines_present(self, capsys):
        tiered = {"opencode-go": self._tiered(), "clinepass": self._tiered()}
        raw = {"opencode-go": {}, "clinepass": {}}
        profiles = {"test-profile": wd.ProfileInfo(provider="clinepass", model="cline-pass/m")}
        lines = ["🔄 test-profile → clinepass / cline-pass/m", "   some reason"]
        wd.report(raw, tiered, self._specs(), lines, profiles)
        out = capsys.readouterr().out
        assert "test-profile" in out

    def test_profile_counts_in_header(self, capsys):
        tiered = {}   # all unavailable → will print
        raw = {}
        profiles = {
            "p1": wd.ProfileInfo(provider="opencode-go", model="m"),
            "p2": wd.ProfileInfo(provider="opencode-go", model="m"),
            "p3": wd.ProfileInfo(provider="clinepass", model="cline-pass/m"),
        }
        wd.report(raw, tiered, self._specs(), [], profiles)
        out = capsys.readouterr().out
        assert "opencode-go=2" in out
        assert "clinepass=1" in out

    def test_both_scrapers_unavailable_prints(self, capsys):
        profiles = {"p": wd.ProfileInfo(provider="opencode-go", model="m")}
        wd.report({}, {}, self._specs(), [], profiles)
        assert capsys.readouterr().out != ""

    def test_switch_lines_printed_on_separate_lines(self, capsys):
        tiered = {"opencode-go": self._tiered(), "clinepass": self._tiered()}
        raw = {"opencode-go": {}, "clinepass": {}}
        profiles = {"p": wd.ProfileInfo(provider="clinepass", model="m")}
        lines = ["line1", "line2"]
        wd.report(raw, tiered, self._specs(), lines, profiles)
        out = capsys.readouterr().out
        assert "line1" in out
        assert "line2" in out


# ═══════════════════════════════════════════════════════════════════════════
# main() — exit codes and startup validation
# ═══════════════════════════════════════════════════════════════════════════


class TestMain:
    def _write_config(self, path, provider, model):
        path.write_text(
            f"model:\n"
            f"  provider: {provider}\n"
            f"  default: {model}\n"
        )

    def _minimal_specs(self):
        return [
            wd.ProviderSpec(
                name="opencode-go", tool="/fake/go",
                tier_map={"rolling": "short", "weekly": "medium", "monthly": "long"},
                model_prefix="",
            ),
            wd.ProviderSpec(
                name="clinepass", tool="/fake/cp",
                tier_map={"five_hour": "short", "weekly": "medium", "monthly": "long"},
                model_prefix="cline-pass/",
            ),
        ]

    def test_exit_1_when_hermes_binary_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wd, "HERMES_BIN", str(tmp_path / "nonexistent"))
        assert wd.main() == 1

    def test_exit_0_when_no_managed_profiles(self, tmp_path, monkeypatch):
        fake_hermes = tmp_path / "hermes"
        fake_hermes.touch()
        monkeypatch.setattr(wd, "HERMES_BIN", str(fake_hermes))
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        monkeypatch.setattr(wd, "PROFILES_DIR", profiles_dir)
        monkeypatch.setattr(wd, "PROVIDERS", self._minimal_specs())
        assert wd.main() == 0

    def test_exit_0_on_clean_run_no_switch_needed(self, tmp_path, monkeypatch):
        fake_hermes = tmp_path / "hermes"
        fake_hermes.touch()
        monkeypatch.setattr(wd, "HERMES_BIN", str(fake_hermes))

        profiles_dir = tmp_path / "profiles"
        p = profiles_dir / "test-profile"
        p.mkdir(parents=True)
        self._write_config(p / "config.yaml", "opencode-go", "deepseek-v4-pro")
        monkeypatch.setattr(wd, "PROFILES_DIR", profiles_dir)

        monkeypatch.setattr(wd, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(wd, "PROVIDERS", self._minimal_specs())
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)

        go_usage = wd.TieredUsage(short=50, medium=50, long=50)
        cp_usage = wd.TieredUsage(short=50, medium=50, long=50)
        monkeypatch.setattr(wd, "scrape", lambda specs: (
            {"opencode-go": go_usage, "clinepass": cp_usage},
            {"opencode-go": {}, "clinepass": {}},
        ))
        monkeypatch.setattr(wd, "save_state", lambda s: None)
        assert wd.main() == 0

    def test_exit_1_when_switch_fails(self, tmp_path, monkeypatch):
        fake_hermes = tmp_path / "hermes"
        fake_hermes.touch()
        monkeypatch.setattr(wd, "HERMES_BIN", str(fake_hermes))

        profiles_dir = tmp_path / "profiles"
        p = profiles_dir / "test-profile"
        p.mkdir(parents=True)
        self._write_config(p / "config.yaml", "opencode-go", "deepseek-v4-pro")
        monkeypatch.setattr(wd, "PROFILES_DIR", profiles_dir)

        monkeypatch.setattr(wd, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(wd, "PROVIDERS", self._minimal_specs())
        monkeypatch.setattr(wd, "TRIGGER_PCT", 95)
        monkeypatch.setattr(wd, "SAFE_SHORT_PCT", 80)
        monkeypatch.setattr(wd, "SAFE_MEDIUM_PCT", 90)
        monkeypatch.setattr(wd, "SAFE_LONG_PCT", 95)

        # GO over trigger, CP safe → switch directive generated, then fails
        monkeypatch.setattr(wd, "scrape", lambda specs: (
            {
                "opencode-go": wd.TieredUsage(short=50, medium=50, long=96),
                "clinepass":   wd.TieredUsage(short=10, medium=10, long=10),
            },
            {"opencode-go": {}, "clinepass": {}},
        ))
        monkeypatch.setattr(wd, "_switch_one", lambda *a: False)
        monkeypatch.setattr(wd, "save_state", lambda s: None)
        assert wd.main() == 1

    def test_exit_0_on_empty_profiles_dir(self, tmp_path, monkeypatch):
        fake_hermes = tmp_path / "hermes"
        fake_hermes.touch()
        monkeypatch.setattr(wd, "HERMES_BIN", str(fake_hermes))
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        monkeypatch.setattr(wd, "PROFILES_DIR", profiles_dir)
        monkeypatch.setattr(wd, "PROVIDERS", self._minimal_specs())
        assert wd.main() == 0
