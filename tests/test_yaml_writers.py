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


# ---------------------------------------------------------------- project.yaml (project.write)

DESCRIPTION = ("A long description of the project that runs well past eighty columns, as an owner writes it on one "
               "line, with a dash - and a colon: inside")
PROJECT = ("# project.yaml - what makes this folder a supervisor project.\r\n"
           'name: "Demo Studio"\r\n'
           f'description: "{DESCRIPTION}"\r\n'
           'framework_version: "1.9.0"           # checked by `supervisor doctor`; `supervisor migrate` updates it\r\n'
           "\r\n"
           "repo:\r\n"
           "  path: .\r\n"
           "  remote: null\r\n"
           "\r\n"
           "# every extension is required\r\n"
           "extensions:\r\n"
           "  - {name: alpha, required: true}\r\n"
           "  - {name: beta, required: true}\r\n"
           "\r\n"
           "ui:\r\n"
           "  port: 8790\r\n"
           "  title: null\r\n")


@pytest.fixture
def project_file(layout):
    path = layout.project_file
    path.write_bytes(PROJECT.encode("utf-8"))
    from supervisor import project
    project._load_cached.cache_clear()
    return path


def test_migrate_changes_only_the_framework_version(layout, project_file):
    """Every migrate with a version change rewrote the whole file before 1.11.0."""
    from supervisor import __version__, migrations
    migrations.run(layout)
    assert project_file.read_bytes() == PROJECT.replace('"1.9.0"', f'"{__version__}"').encode("utf-8")


def test_project_write_of_a_new_value_changes_only_that_value(layout, project_file):
    from supervisor import project
    doc = yaml.safe_load(PROJECT)
    project.write(layout, {**doc, "ui": {"port": 8791, "title": None}})
    assert project_file.read_bytes() == PROJECT.replace("port: 8790", "port: 8791").encode("utf-8")


def test_a_structural_change_keeps_the_rest_of_project_yaml_as_written(layout, project_file):
    """`supervisor extension new` appends to the list: a round trip, which must not reflow, blank nulls or move items."""
    from supervisor import project
    doc = yaml.safe_load(PROJECT)
    doc["extensions"].append("gamma")
    project.write(layout, doc)
    raw = project_file.read_bytes().decode("utf-8")
    assert yaml.safe_load(raw) == doc
    assert "\n" not in raw.replace("\r\n", "")                                   # CRLF throughout
    assert f'description: "{DESCRIPTION}"\r\n' in raw                            # not reflowed
    assert "  remote: null\r\n" in raw and "  title: null\r\n" in raw             # null stays null
    assert ("  - {name: alpha, required: true}\r\n  - {name: beta, required: true}\r\n  - gamma\r\n") in raw
    assert "# every extension is required" in raw and "# checked by `supervisor doctor`" in raw


def test_project_write_without_a_file_writes_the_document(layout):
    from supervisor import project
    layout.project_file.unlink(missing_ok=True)
    project.write(layout, {"name": "fresh", "extensions": ["a"], "remote": None})
    assert yaml.safe_load(layout.project_file.read_text(encoding="utf-8")) == {"name": "fresh", "extensions": ["a"],
                                                                                "remote": None}


# ---------------------------------------------------------------- aliases (review 1.11.0 F1)

def test_an_aliased_list_edited_on_one_key_keeps_the_other(layout):
    """`backup_extensions: *enabled` shares the list with `extensions`: appending to one must not change, or be undone
    by, the other. 1.10.1 wrote two lists; 38c254b lost the appended item on both."""
    from supervisor import project
    layout.project_file.write_bytes(b"name: demo\nextensions: &enabled [alpha]\nbackup_extensions: *enabled\n")
    project._load_cached.cache_clear()
    wanted = {"name": "demo", "extensions": ["alpha", "gamma"], "backup_extensions": ["alpha"]}
    project.write(layout, wanted)
    assert yaml.safe_load(layout.project_file.read_text(encoding="utf-8")) == wanted


def test_an_aliased_mapping_edited_on_one_key_keeps_the_other():
    text = "first: &shared {limit: 1}\nsecond: *shared\n"
    wanted = {"first": {"limit": 2}, "second": {"limit": 1}}
    assert yaml_edit.replace_scalars(text, {("first", "limit"): 2}) is None   # the shared token is not a local edit
    assert yaml.safe_load(yaml_edit.edit(text, wanted)) == wanted


def test_an_edit_that_changes_both_aliased_paths_alike_is_still_exact():
    text = "first: &shared {limit: 1}\nsecond: *shared\n"
    wanted = {"first": {"limit": 2}, "second": {"limit": 2}}
    assert yaml.safe_load(yaml_edit.edit(text, wanted)) == wanted


@pytest.mark.parametrize("old,new", [(1, True), (True, 1), (1, 1.0), ("1", 1), (None, "null")])
def test_the_written_value_has_the_requested_type(old, new):
    """Python's `True == 1` must not let a write that reads back as the wrong YAML type pass verification."""
    text = yaml.safe_dump({"x": old, "y": "keep"})
    out = yaml.safe_load(yaml_edit.edit(text, {"x": new, "y": "keep"}))
    assert out == {"x": new, "y": "keep"} and type(out["x"]) is type(new)


# ---------------------------------------------------------------- YAML 1.1 strings (review 1.11.0 R2-1)

AMBIGUOUS = ("on", "off", "yes", "no", "~", "null", "1.10", "1_000", "0o17", "2026-10-27", ".inf")


def test_a_write_that_breaks_an_alias_keeps_strings_the_reader_would_retype(layout):
    """The framework reads YAML 1.1 (PyYAML), where a plain `on` is `True`; ruamel writes 1.2, where it is a string.
    7240390's whole-document fallback wrote `title: on` unquoted and read back `True`. Strings in values, keys and
    lists must stay strings."""
    from supervisor import project
    lines = ["name: demo", "extensions: &enabled [alpha]", "backup_extensions: *enabled", "ui:",
             '  title: "on" # keep this string']
    lines += [f'  "{s}": key' for s in AMBIGUOUS]
    lines += ["modes: [" + ", ".join(f'"{s}"' for s in AMBIGUOUS) + "]"]
    layout.project_file.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    project._load_cached.cache_clear()
    wanted = yaml.safe_load(layout.project_file.read_text(encoding="utf-8"))
    wanted["backup_extensions"] = ["alpha"]
    wanted["extensions"] = ["alpha", "gamma"]
    assert wanted["ui"]["title"] == "on" and all(s in wanted["ui"] for s in AMBIGUOUS)
    project.write(layout, wanted)
    assert yaml_edit.same(yaml.safe_load(layout.project_file.read_text(encoding="utf-8")), wanted)


def test_a_new_string_the_reader_would_retype_is_quoted_and_the_comments_stay():
    """A key added by the round trip: the new strings must read back as strings, and the comment-keeping round trip
    (not the fallback) must be the write that does it."""
    text = "kept: 1 # owner note\n"
    wanted = {"kept": 1, "mode": "no", "list": list(AMBIGUOUS), **{s: s for s in AMBIGUOUS}}
    out = yaml_edit.edit(text, wanted)
    assert yaml_edit.same(yaml.safe_load(out), wanted)
    assert "# owner note" in out


def test_a_write_no_candidate_can_verify_is_refused_and_the_file_stays(layout, project_file, monkeypatch):
    """The last-resort writer is checked like the others; when nothing reads back as the requested document, the
    write is refused before the file is replaced."""
    from supervisor import project
    before = project_file.read_bytes()
    monkeypatch.setattr(yaml_edit, "dump_round_trip", lambda text, doc: "wrong: document\n")
    monkeypatch.setattr(yaml_edit, "dump_whole", lambda text, doc: "wrong: document\n")
    doc = yaml.safe_load(PROJECT)
    doc["extensions"].append("gamma")
    with pytest.raises(yaml_edit.EditRefused):
        project.write(layout, doc)
    assert project_file.read_bytes() == before


def test_a_settings_save_no_candidate_can_verify_is_a_settings_error(layout, routing, monkeypatch):
    monkeypatch.setattr(yaml_edit, "replace_scalars", lambda text, changes: None)
    monkeypatch.setattr(yaml_edit, "dump_round_trip", lambda text, doc: "wrong: document\n")
    monkeypatch.setattr(yaml_edit, "dump_whole", lambda text, doc: "wrong: document\n")
    with pytest.raises(settings.SettingsError, match="not saved"):
        settings.update(layout, "capacity.claude.reserve_percent", "5")
    assert routing.read_bytes() == ROUTING.encode("utf-8")
