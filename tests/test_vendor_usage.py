import json
import pytest
from supervisor._vendor import usage_policy as u


@pytest.fixture(autouse=True)
def state(tmp_path):
    u.configure(tmp_path, {"claude": 10, "codex": 10})
    return tmp_path


def test_claude_event_records_and_gates(monkeypatch):
    now = 1_000_000.0
    ev = {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": [
        {"rateLimitType": "five_hour", "utilization": 0.95, "resetsAt": now + 3600},
        {"rateLimitType": "seven_day", "utilization": 0.5, "resetsAt": now + 86400}]}}
    import time
    monkeypatch.setattr(time, "time", lambda: now)
    assert u.claude_event(ev)["state"] == "KNOWN"
    assert u.reserve_until("claude", now) == now + 3600 + 60      # 5 % remaining ≤ 10 % reserve → gated
    assert u.engine_available("claude", now + 3700)               # after reset


def test_weekly_gates_only_at_zero():
    now = 2_000_000.0
    obs = u.record("claude", [{"name": "seven_day", "used_percent": 95, "reset_epoch": now + 5000},
                              {"name": "five_hour", "used_percent": 50, "reset_epoch": now + 100}], "t", now)
    assert u.reserve_until("claude", now, obs) is None
    obs = u.record("claude", [{"name": "seven_day", "used_percent": 100, "reset_epoch": now + 5000}], "t", now)
    assert u.reserve_until("claude", now, obs) == now + 5000 + 60


def test_carry_unreported_keeps_depleted_window(monkeypatch):
    now = 3_000_000.0
    import time
    monkeypatch.setattr(time, "time", lambda: now)
    u.record("codex", [{"name": "secondary", "used_percent": 100, "reset_epoch": now + 9000}], "t", now)
    windows = u.carry_unreported("codex", [{"name": "primary", "used_percent": 10, "reset_epoch": now + 100}])
    assert {w["name"] for w in windows} == {"primary", "secondary"}
    obs = u.record("codex", windows, "t", now)
    assert u.reserve_until("codex", now, obs) == now + 9000 + 60


def test_owner_reset_clears_gating_windows():
    now = 4_000_000.0
    u.record("claude", [{"name": "five_hour", "used_percent": 99, "reset_epoch": now + 1000}], "t", now)
    cleared = u.owner_reset("claude", "used reset", now)
    assert len(cleared) == 1 and u.engine_available("claude", now)
    assert u.read_provider("claude")["owner_resets"][-1]["message"] == "used reset"


def test_codex_windows_shapes():
    r = {"rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 20, "resetsAt": 1}, "secondary": {"usedPercent": 5, "resetsAt": 2}},
                                 "other": {"primary": {"usedPercent": 100, "resetsAt": 3}}}}
    assert [w["name"] for w in u.codex_windows(r)] == ["primary", "secondary"]
    legacy = {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 1, "resetsAt": 1}}}
    assert u.codex_windows(legacy)[0]["used_percent"] == 1


def test_ingest_stream_partial_line(tmp_path, monkeypatch):
    import time
    now = 5_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    p = tmp_path / "stdout.jsonl"
    line = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": [
        {"rateLimitType": "five_hour", "utilization": 0.2, "resetsAt": now + 100}]}})
    p.write_bytes((line + "\r\n" + line[:10]).encode())   # CRLF: offsets must be byte offsets on every platform
    off = u.ingest_claude_stream(p, 0)
    assert off == len(line) + 2          # incomplete tail is retried later
    assert u.read_provider("claude")["windows"][0]["remaining_percent"] == 80


def test_capacity_lines_render():
    now = 6_000_000.0
    u.record("claude", [{"name": "five_hour", "used_percent": 30, "reset_epoch": now + 100}], "t", now)
    lines = u.capacity_lines(now)
    assert any("Claude 5-hour: 70% remaining" in l for l in lines)
    assert any("Codex: remaining capacity unknown" in l for l in lines)


def test_expired_windows_are_dropped_on_carry(monkeypatch):
    import time
    now = 7_000_000.0
    monkeypatch.setattr(time, "time", lambda: now - 10_000)
    u.record("claude", [{"name": "five_hour", "used_percent": 99, "reset_epoch": now - 5000}], "t", now - 10_000)
    monkeypatch.setattr(time, "time", lambda: now)
    obs = u.record("claude", [{"name": "seven_day", "used_percent": 10, "reset_epoch": now + 5000}], "t", now)
    assert [w["name"] for w in obs["windows"]] == ["seven_day"]
