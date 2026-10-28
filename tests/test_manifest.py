"""The extension manifest (phase 3 step 1): `supervisor-extension.toml` says who an extension is, which framework
versions it supports, what it registers and what it needs, and the framework reads it without importing anything the
extension ships. Parsing is strict (an unknown key is a problem, not ignored) and reports every problem at once, so
an author fixes a manifest in one pass. docs/EXTENSION_STANDARD.md is the reference."""
from __future__ import annotations

import sys

import pytest

from supervisor import __version__, manifest

FULL = '''
identity = "text_model"
version = "1.0.0"
framework = ">=1.12,<2"
migrations = "supervisor_ext_textgen.migrations"

[provides]
adapters = ["text_model"]
doctor_checks = ["text_model_server", "textgen"]
hooks = ["overview_panel"]

[legacy_ids]
doctor_checks = ["textgen"]

[config]
section = "extensions.text_model"
schema = "supervisor_ext_textgen.config:Settings"

[requires]
resources = ["gpu"]
services = ["http://127.0.0.1:11434"]
extensions = ["render"]
'''


def problems_of(text: str) -> list[str]:
    with pytest.raises(manifest.ManifestError) as caught:
        manifest.parse(text, source="test.toml")
    return caught.value.problems


def test_a_complete_manifest_reads_as_written():
    m = manifest.parse(FULL, source="supervisor-extension.toml")
    assert (m.identity, m.version, m.framework) == ("text_model", "1.0.0", ">=1.12,<2")
    assert m.provides == {"adapters": ("text_model",), "doctor_checks": ("text_model_server", "textgen"),
                          "hooks": ("overview_panel",)}
    assert m.legacy_ids == {"doctor_checks": ("textgen",)}
    assert (m.config.section, m.config.schema) == ("extensions.text_model", "supervisor_ext_textgen.config:Settings")
    assert m.requires.resources == ("gpu",) and m.requires.extensions == ("render",)
    assert m.requires.services == ("http://127.0.0.1:11434",)
    assert m.migrations == "supervisor_ext_textgen.migrations" and m.source == "supervisor-extension.toml"


def test_the_smallest_manifest_needs_identity_version_and_framework():
    m = manifest.parse('identity = "notes"\nversion = "0.1.0"\nframework = ">=1.12"\n', source="m.toml")
    assert m.provides == {} and m.legacy_ids == {} and m.config is None and m.migrations is None
    assert m.requires.resources == m.requires.services == m.requires.extensions == ()


def test_reading_a_manifest_imports_nothing_it_names(tmp_path):
    """The schema and migrations modules are names, not imports: the loader decides before any extension code
    runs (main tab, "Loading is staged and transactional")."""
    path = tmp_path / "supervisor-extension.toml"
    path.write_text(FULL, encoding="utf-8")
    before = set(sys.modules)
    m = manifest.load(path)
    assert m.source == str(path)
    assert not {name for name in set(sys.modules) - before if name.startswith("supervisor_ext_textgen")}


def test_every_problem_is_reported_at_once():
    text = ('identity = "Bad-Name"\nversion = "not a version"\nframework = "around 1.12"\ncolour = "red"\n'
            '[provides]\nwidgets = ["x"]\n')
    found = "\n".join(problems_of(text))
    for expected in ("identity", "version", "framework", "unknown key 'colour'", "unknown kind 'widgets'"):
        assert expected in found, expected


@pytest.mark.parametrize("text,expected", [
    ('version = "1.0.0"\nframework = ">=1.12"\n', "identity is required"),
    ('identity = "x"\nframework = ">=1.12"\n', "version is required"),
    ('identity = "x"\nversion = "1.0.0"\n', "framework is required"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\n[provides]\nadapters = "x"\n', "a list of names"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\n[provides]\nadapters = ["x", "x"]\n', "twice"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\n[provides]\nroutes = ["x"]\n', "start with /"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\n[provides]\nhooks = ["teleport"]\n', "unknown hook"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\n[legacy_ids]\nadapters = ["old"]\n', "not in provides"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\n[legacy_ids]\nhooks = ["overview_panel"]\n',
     "unknown kind 'hooks'"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\n[config]\nsection = "x"\nschema = "m:C"\n',
     "extensions.x"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\n[config]\nsection = "extensions.x"\nschema = "m.C"\n',
     "module:Class"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\n[config]\nsection = "extensions.x"\nschema = "m:C"\n'
     'legacy_sections = ["ok", "not a key"]\n', "legacy_sections"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\n[requires]\nresources = ["tpu"]\n', "unknown resource"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\n[requires]\nextensions = ["x"]\n', "itself"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\nmigrations = "not a module"\n', "migrations"),
    ('identity = "x"\nversion = "1.0.0"\nframework = ">=1.12"\nprovides = "all"\n', "provides must be a table"),
], ids=["no-identity", "no-version", "no-framework", "provides-not-a-list", "duplicate", "route-without-slash",
        "unknown-hook", "legacy-not-provided", "legacy-hooks", "config-section-not-owned", "schema-format",
        "legacy-sections-format",
        "unknown-resource", "requires-itself", "migrations-not-a-module", "provides-not-a-table"])
def test_each_rule_names_its_problem(text, expected):
    assert any(expected in p for p in problems_of(text)), problems_of(text)


def test_a_toml_syntax_error_names_the_file(tmp_path):
    path = tmp_path / "supervisor-extension.toml"
    path.write_text('identity = "x\n', encoding="utf-8")
    with pytest.raises(manifest.ManifestError) as caught:
        manifest.load(path)
    assert str(path) in str(caught.value) and "TOML" in str(caught.value)


# ---------------------------------------------------------------- identity and identifiers

@pytest.mark.parametrize("kind,name,ok", [
    ("adapters", "ballots", True),
    ("doctor_checks", "ballots_queue", True),
    ("commands", "ballots.set_verdict", True),
    ("static", "ballots/css", True),
    ("routes", "/ballots", True),
    ("routes", "/ballots/item", True),
    ("commands", "vote.cast", False),          # an older kind: a legacy identifier, not namespaced
    ("routes", "/review", False),
    ("adapters", "ballotsx", False),              # a longer name is not the identity
    ("routes", "/ballotsx", False),
])
def test_an_identifier_belongs_to_the_identity_or_is_not_namespaced(kind, name, ok):
    assert manifest.namespaced("ballots", kind, name) is ok


def test_an_identifier_neither_namespaced_nor_legacy_is_refused_and_a_legacy_one_is_accepted():
    """The namespace rule: an extension older than the rule keeps its existing names as legacy_ids."""
    base = 'identity = "ballots"\nversion = "1.0.0"\nframework = ">=1.12"\n[provides]\n'
    text = base + 'commands = ["vote.cast", "ballots.close"]\nroutes = ["/review"]\n'
    found = problems_of(text)
    assert any("vote.cast" in p and "legacy_ids" in p for p in found)
    assert any("/review" in p for p in found) and not any("ballots.close" in p for p in found)
    legacy = text + '[legacy_ids]\ncommands = ["vote.cast"]\nroutes = ["/review"]\n'
    assert manifest.parse(legacy, source="m.toml").legacy_ids == {"commands": ("vote.cast",),
                                                                  "routes": ("/review",)}


# ---------------------------------------------------------------- the framework range (D2: `packaging`)

@pytest.mark.parametrize("framework,version,ok", [
    (">=1.12,<2", "1.12.0", True),
    (">=1.12,<2", "1.13.4", True),
    (">=1.12,<2", "1.11.0", False),
    (">=1.12,<2", "2.0.0", False),
    (">=1.9", "1.10.0", True),                          # by number, not as text: 1.10 is above 1.9
    ("~=1.12.0", "1.12.3", True),
    ("~=1.12.0", "1.13.0", False),
])
def test_the_framework_range_is_decided_by_version_number(framework, version, ok):
    m = manifest.parse(f'identity = "x"\nversion = "1.0.0"\nframework = "{framework}"\n', source="m.toml")
    assert m.supports(version) is ok
    assert (m.unsupported_reason(version) is None) is ok


def test_the_installed_framework_is_the_default_and_the_reason_names_both_sides():
    m = manifest.parse('identity = "x"\nversion = "1.0.0"\nframework = ">=99"\n', source="m.toml")
    assert not m.supports()
    reason = m.unsupported_reason()
    assert __version__ in reason and ">=99" in reason and "x" in reason
