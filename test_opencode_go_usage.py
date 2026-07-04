"""Runnable checks for the parsing, cookie, and fetch logic.

Run with: python3 test_opencode_go_usage.py
"""

import os
from pathlib import Path
from unittest.mock import patch

import opencode_go_usage as m

# Trimmed, sanitized copy of the real /go page's inlined data.
FIXTURE = (
    'workspaces[]"]=$R[5];...$R[29]=[$R[30]={id:"wrk_TESTWORKSPACE0001",name:"Default"}];'
    '$R[28]($R[18],$R[34]={mine:!0,useBalance:!0,region:$R[35]=["us","eu","sg"],'
    'rollingUsage:$R[36]={status:"ok",resetInSec:14182,usagePercent:22},'
    'weeklyUsage:$R[37]={status:"ok",resetInSec:161609,usagePercent:43},'
    'monthlyUsage:$R[38]={status:"limited",resetInSec:504671,usagePercent:98}});'
    '<main data-page="workspace">bars</main>'
)


class _FakeResponse:
    """Stands in for the object `urllib.request.urlopen` returns."""

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
    return patch("urllib.request.urlopen", return_value=_FakeResponse(url, body))


def test_parse_usage():
    u = m.parse_usage(FIXTURE)
    assert u["rolling"] == {"status": "ok", "reset_in_sec": 14182, "pct": 22}, u["rolling"]
    assert u["weekly"]["pct"] == 43
    assert u["monthly"] == {"status": "limited", "reset_in_sec": 504671, "pct": 98}, u["monthly"]


def test_parse_usage_missing_returns_none():
    assert m.parse_usage("<html>no data here</html>") is None


def test_parse_use_balance():
    assert m.parse_use_balance(FIXTURE) is True
    assert m.parse_use_balance("useBalance:!1,region:[]") is False
    assert m.parse_use_balance("nothing") is None


def test_looks_logged_out():
    assert m._looks_logged_out("https://auth.opencode.ai/authorize", "wrk_x") is True
    assert m._looks_logged_out("https://opencode.ai/", "<html>login</html>") is True
    assert m._looks_logged_out("https://opencode.ai/", FIXTURE) is False


def test_thresholds_fire_on_limit():
    windows = m.parse_usage(FIXTURE)
    alerts = m.check_thresholds(windows)  # monthly at 98% >= default 95%
    assert any("monthly" in a for a in alerts), alerts
    assert not any("rolling" in a for a in alerts), alerts  # 22% is fine


def test_fetch_success():
    url = "https://opencode.ai/workspace/wrk_TESTWORKSPACE0001/go"
    with _mock_urlopen(url, FIXTURE):
        result = m.fetch("auth=fake", "wrk_TESTWORKSPACE0001")
    assert result["workspace_id"] == "wrk_TESTWORKSPACE0001"
    assert result["windows"]["monthly"]["pct"] == 98
    assert result["use_balance"] is True


def test_fetch_no_workspace_exits_1():
    try:
        m.fetch("auth=fake", None)
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert e.code == 1


def test_fetch_logged_out_exits_1():
    with _mock_urlopen("https://auth.opencode.ai/authorize", "<html>login</html>"):
        try:
            m.fetch("auth=fake", "wrk_x")
            raise AssertionError("expected SystemExit")
        except SystemExit as e:
            assert e.code == 1


def test_fetch_page_changed_exits_2():
    with _mock_urlopen("https://opencode.ai/workspace/wrk_x/go", "wrk_x present but no usage data"):
        try:
            m.fetch("auth=fake", "wrk_x")
            raise AssertionError("expected SystemExit")
        except SystemExit as e:
            assert e.code == 2


def test_load_cookie_from_env_var_gets_auth_prefix():
    with patch.dict(os.environ, {"OPENCODE_AUTH_COOKIE": "Fe26.2**abc"}):
        assert m.load_cookie(None) == "auth=Fe26.2**abc"


def test_load_cookie_passes_through_full_cookie_header():
    with patch.dict(os.environ, {"OPENCODE_AUTH_COOKIE": "auth=Fe26.2**abc; oc_locale=en"}):
        assert m.load_cookie(None) == "auth=Fe26.2**abc; oc_locale=en"


def test_load_cookie_reads_cookie_file(tmp_path=None):
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        cookie_path = Path(d) / "cookie"
        cookie_path.write_text("Fe26.2**fromfile\n")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCODE_AUTH_COOKIE", None)
            assert m.load_cookie(str(cookie_path)) == "auth=Fe26.2**fromfile"


def test_load_cookie_missing_everywhere_exits_1():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENCODE_AUTH_COOKIE", None)
        with patch.object(m, "DEFAULT_COOKIE_FILE", Path("/nonexistent/opencode-go-usage-test/auth")):
            try:
                m.load_cookie(None)
                raise AssertionError("expected SystemExit")
            except SystemExit as e:
                assert e.code == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
