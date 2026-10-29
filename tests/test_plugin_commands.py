"""plugin.submit_command and plugin.log_event (phase 2 step 5). A command submitted twice with the same command id is
applied once, by whoever holds the state lock, and the second submission is reported as a replay; a log line from an
plugin carries the plugin's name as the prefix of its actor, and goes through the framework's single log path."""
from __future__ import annotations

import json

import pytest

from supervisor import commands, plugin, plugins, testing
from supervisor.locks import FileLock
from supervisor.paths import Layout

COUNTER = '''
from supervisor import plugin

def count(layout, payload, actor):
    with open(layout.root / "applied.txt", "a", encoding="utf-8") as f:
        f.write(payload["_id"] + "\\n")
    return {"n": payload.get("n")}

def setup(registry):
    registry.command("counter.bump", count)

def log(layout, event, actor=None, **fields):
    return plugin.log_event(layout, event, actor, **fields)
'''


@pytest.fixture
def lay(layout):
    testing.add_plugins(layout, {"counter": COUNTER})
    assert plugins.load(layout).errors == []
    yield layout
    plugins.reset()


def applied(lay: Layout) -> list[str]:
    p = lay.root / "applied.txt"
    return p.read_text(encoding="utf-8").split() if p.exists() else []


def test_a_command_submitted_twice_with_one_id_is_applied_once(lay):
    first = plugin.submit_command(lay, "counter.bump", {"n": 1}, command_id="form-7")
    again = plugin.submit_command(lay, "counter.bump", {"n": 1}, command_id="form-7")
    assert first["ok"] and first["id"] == "form-7" and not first.get("replayed")
    assert again["replayed"] is True and again["id"] == "form-7" and again["result"] == {"n": 1}
    assert applied(lay) == ["form-7"]


def test_a_replay_while_the_loop_holds_the_lock_is_applied_once_by_the_loop(lay):
    with FileLock(lay.lock, owner="loop"):
        queued = plugin.submit_command(lay, "counter.bump", {"n": 2}, command_id="form-8")
        replay = plugin.submit_command(lay, "counter.bump", {"n": 2}, command_id="form-8")
        assert queued["queued"] and replay["queued"] and replay["replayed"]
        assert commands.apply_all(lay)[0]["id"] == "form-8"
        late = plugin.submit_command(lay, "counter.bump", {"n": 2}, command_id="form-8")
        assert late["replayed"] and late["ok"]
        assert commands.apply_all(lay) == []
    assert applied(lay) == ["form-8"]


def test_without_a_command_id_every_submission_is_a_new_command(lay):
    plugin.submit_command(lay, "counter.bump", {})
    plugin.submit_command(lay, "counter.bump", {})
    assert len(applied(lay)) == 2


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a" * 81, ".", 7])
def test_a_command_id_must_be_a_plain_name(lay, bad):
    with pytest.raises(ValueError, match="command_id"):
        plugin.submit_command(lay, "counter.bump", {}, command_id=bad)
    assert applied(lay) == []


def test_a_plugin_log_line_names_the_plugin(lay):
    module = plugins._import(lay, "counter")
    module.log(lay, "verdict", "owner-ui", item="H-001", verdict="ACCEPT")
    module.log(lay, "tick")
    lines = [json.loads(line) for line in lay.log.read_text(encoding="utf-8").splitlines()]
    verdict = next(e for e in lines if e["event"] == "verdict")
    assert verdict["actor"] == "counter:owner-ui" and verdict["item"] == "H-001" and verdict["verdict"] == "ACCEPT"
    assert next(e for e in lines if e["event"] == "tick")["actor"] == "counter"


def test_log_event_is_for_plugin_code(lay):
    with pytest.raises(plugin.PluginError, match="plugin's code"):
        plugin.log_event(lay, "spoofed", "owner")
