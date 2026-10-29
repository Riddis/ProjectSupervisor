"""plugin.policy (phase 2 step 5): a plugin merges its own policy floor with the framework's merge rule (lists
unioned, a wrong-kinded entry dropped and reported), reads the effective policy and its sections as copies, reads the
framework's config and credential-scan diagnostics, and refuses a packet by raising PolicyViolation from a screen."""
from __future__ import annotations

import copy

import pytest
import yaml

from supervisor import plugin, plugins, policy, project

FLOOR = {"hosted": {"deny": ["NUDE"], "enabled": False, "limit": 3}, "channels": {}}


def test_merge_floor_is_the_framework_rule():
    configured = {"hosted": {"deny": ["GORE", "NUDE"], "enabled": "yes", "limit": 5}, "extra": {"a": 1}}
    merged, problems = plugin.policy.merge_floor(FLOOR, configured)
    assert (merged, problems) == policy._merge(copy.deepcopy(FLOOR), copy.deepcopy(configured))
    assert merged["hosted"]["deny"] == ["NUDE", "GORE"]                  # lists unioned with the floor
    assert merged["hosted"]["enabled"] is False                          # a string cannot replace a flag
    assert merged["hosted"]["limit"] == 5 and merged["extra"] == {"a": 1}
    assert len(problems) == 1 and "hosted.enabled" in problems[0]


def test_merge_floor_changes_neither_input():
    """The merged result shares nothing with its inputs: a floor list the config did not touch, and a section the
    floor does not know, are both handed back as copies."""
    floor, configured = copy.deepcopy(FLOOR), {"own": {"names": ["a"]}}
    merged, _ = plugin.policy.merge_floor(floor, configured)
    merged["hosted"]["deny"].append("X")
    merged["own"]["names"].append("b")
    merged["channels"]["new"] = True
    assert floor == FLOOR and configured == {"own": {"names": ["a"]}}


def test_the_effective_policy_is_read_as_copies(layout):
    (layout.config).mkdir(parents=True, exist_ok=True)
    (layout.config / "policy.yaml").write_text(yaml.safe_dump({"channels": {"blog": {"allowed": True}}}),
                                               encoding="utf-8")
    doc = plugin.policy.load(layout)
    assert doc == policy.load(layout)
    doc["channels"]["blog"]["allowed"] = False
    doc.setdefault("secrets", {})["patterns"] = []
    assert plugin.policy.load(layout) == policy.load(layout)
    assert policy.load(layout)["channels"]["blog"]["allowed"] is True    # the framework's cached policy is untouched
    section = plugin.policy.section(layout, "channels")
    section["blog"] = None
    assert plugin.policy.section(layout, "channels") == {"blog": {"allowed": True}}
    assert plugin.policy.section(layout, "missing") == {}


def test_the_diagnostics_are_the_frameworks(layout):
    (layout.config).mkdir(parents=True, exist_ok=True)
    (layout.config / "policy.yaml").write_text(yaml.safe_dump({"secrets": {"patterns": None}}), encoding="utf-8")
    assert plugin.policy.config_problems(layout) == policy.config_problems(layout) != []
    assert plugin.policy.secret_scan_problems(layout) == policy.secret_scan_problems(layout)


def test_a_screen_raising_policy_violation_refuses_the_packet(layout):
    assert plugin.policy.PolicyViolation is policy.PolicyViolation
    code = '''
from supervisor import plugin

def setup(registry):
    def screen(layout, text, where):
        if "SECRET-PROJECT" in text:
            raise plugin.policy.PolicyViolation(f"{where}: may not leave this machine")
    registry.packet_screen(screen)
'''
    (layout.root / "plugins").mkdir(parents=True, exist_ok=True)
    (layout.root / "plugins" / "screen.py").write_text(code, encoding="utf-8")
    project.write(layout, {"name": "proj", "plugins": ["screen"]})
    plugins.reset()
    try:
        policy.assert_packet_clean("an ordinary brief", layout, "packet")
        with pytest.raises(policy.PolicyViolation, match="may not leave"):
            policy.assert_packet_clean("about SECRET-PROJECT", layout, "packet")
    finally:
        plugins.reset()
