"""YAML writers that change only what they edit (1.11.0). Settings (settings.update) and project.yaml
(project.write) are hand-authored files with owner-decision comments; the round-trip dump that wrote them back
reflowed long lines past 80 columns, wrote `null` as an empty value, moved list items to the parent's indentation
and, for routing.yaml, rewrote every line ending in place. An edit of existing values now replaces exactly those
values' tokens; anything else keeps the file's own long lines, nulls, indentation and line endings; both writes
replace the file atomically."""
from __future__ import annotations

import pytest
import yaml

from supervisor import settings, yaml_edit

LONG = "bash_allow: [" + ", ".join(f'"Bash(tool{i} *)"' for i in range(12)) + "]"
ROUTING = ("# routing.yaml (owner decision D-011)\r\n"
           "capacity:\r\n"
           "  claude: {reserve_percent: 1, weekly_gate_percent: 20}    # D-011: both windows\r\n"
           "  codex: {reserve_percent: 10}\r\n"
           "limits:\r\n"
           "  max_agent_jobs: 2          # owner 2026-10-20\r\n"
           "  max_resource_jobs: 1\r\n"
           "claude:\r\n"
           "  " + LONG + "\r\n"
           "  executable: null\r\n"
           "roles:\r\n"
           "  engineer:\r\n"
           "    tools:\r\n"
           "      - Read\r\n"
           "      - Edit\r\n")


@pytest.fixture
def routing(layout):
    layout.config.mkdir(parents=True, exist_ok=True)
    path = layout.config / "routing.yaml"
    path.write_bytes(ROUTING.encode("utf-8"))
    return path


def test_a_settings_edit_in_a_flow_mapping_changes_only_that_value(layout, routing):
    settings.update(layout, "capacity.claude.weekly_gate_percent", "10")
    assert routing.read_bytes() == ROUTING.replace("weekly_gate_percent: 20}", "weekly_gate_percent: 10}").encode()


def test_a_settings_edit_in_a_block_mapping_changes_only_that_value(layout, routing):
    settings.update(layout, "limits.max_agent_jobs", "3")
    assert routing.read_bytes() == ROUTING.replace("max_agent_jobs: 2 ", "max_agent_jobs: 3 ").encode()


def test_a_setting_the_file_lacks_is_added_and_the_rest_stays_as_written(layout, routing):
    settings.update(layout, "capacity.codex.weekly_gate_percent", "15")
    raw = routing.read_bytes().decode("utf-8")
    assert yaml.safe_load(raw)["capacity"]["codex"] == {"reserve_percent": 10, "weekly_gate_percent": 15}
    assert "\r\n" in raw and "\n" not in raw.replace("\r\n", "")              # CRLF kept, nothing mixed in
    assert "  " + LONG + "\r\n" in raw                                        # no reflow of a long line
    assert "executable: null" in raw                                          # null stays null
    assert "      - Read\r\n      - Edit\r\n" in raw                          # list items keep their indentation
    assert "# D-011: both windows" in raw and "# owner 2026-10-20" in raw


def test_an_interrupted_settings_write_leaves_the_file_whole(layout, routing, monkeypatch):
    from supervisor._vendor import contract

    def refuse(*_a, **_k):
        raise OSError("disk went away")
    monkeypatch.setattr(contract.os, "replace", refuse)
    with pytest.raises(OSError):
        settings.update(layout, "limits.max_agent_jobs", "3")
    monkeypatch.undo()
    assert routing.read_bytes() == ROUTING.encode("utf-8")


# ---------------------------------------------------------------- the helper

def test_replace_scalars_keeps_quote_style_and_quotes_what_must_be(tmp_path):
    text = 'name: "Demo"\nversion: \'1.9.0\'\nplain: 1.9.0\nnum: 3\n'
    out = yaml_edit.replace_scalars(text, {("name",): "Demo 2", ("version",): "1.11.0", ("plain",): "1.10",
                                           ("num",): 4})
    assert out == 'name: "Demo 2"\nversion: \'1.11.0\'\nplain: \'1.10\'\nnum: 4\n'
    assert yaml.safe_load(out) == {"name": "Demo 2", "version": "1.11.0", "plain": "1.10", "num": 4}


@pytest.mark.parametrize("path", [("missing",), ("limits", "nope"), ("limits",), ("list", "0")])
def test_replace_scalars_refuses_what_is_not_an_existing_scalar(path):
    text = "limits:\n  max: 2\nlist:\n  - a\n"
    assert yaml_edit.replace_scalars(text, {path: 5}) is None


def test_scalar_changes_lists_value_edits_and_refuses_structure():
    old = {"a": 1, "b": {"c": "x", "d": None}, "e": [1, 2]}
    assert yaml_edit.scalar_changes(old, {"a": 2, "b": {"c": "y", "d": None}, "e": [1, 2]}) == {("a",): 2,
                                                                                               ("b", "c"): "y"}
    assert yaml_edit.scalar_changes(old, {**old, "f": 1}) is None              # a key added
    assert yaml_edit.scalar_changes(old, {"a": 1, "b": {"c": "x"}, "e": [1, 2]}) is None   # a key removed
    assert yaml_edit.scalar_changes(old, {**old, "e": [1, 2, 3]}) is None      # a list changed
    assert yaml_edit.scalar_changes(old, {**old, "a": {"x": 1}}) is None       # a scalar became a mapping
    assert yaml_edit.scalar_changes(old, dict(old)) == {}
