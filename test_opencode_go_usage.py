"""Runnable checks for the parsing and cookie logic. `python3 test_opencode_go_usage.py`."""

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


def test_thresholds_fire_on_limit(monkeypatch=None):
    windows = m.parse_usage(FIXTURE)
    alerts = m.check_thresholds(windows)  # monthly at 98% >= default 95%
    assert any("monthly" in a for a in alerts), alerts
    assert not any("rolling" in a for a in alerts), alerts  # 22% is fine


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
