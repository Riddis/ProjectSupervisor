"""Gate G2 (supervisor.api_contract): the public plugin API's recorded contract. What a contract records, which
differences break callers and which are additions, the check that ties the code, docs/plugin_api.json, the last
release tag's contract, the CHANGELOG and docs/PLUGIN_API.md together, and the repository's own contract
matching its code. Each planted module is a throwaway file, so the rules are tested apart from supervisor.plugin."""
import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from supervisor import api_contract as ac

REPO = Path(__file__).resolve().parents[1]
_count = 0


def planted(tmp_path, source: str) -> dict:
    """describe() of a module built from `source`; its own classes count as framework classes."""
    global _count
    _count += 1
    name = f"contract_probe_{_count}"
    path = tmp_path / f"{name}.py"
    path.write_text("from __future__ import annotations\n" + textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return ac.describe(mod, internal=(name,))


BASE = """
from pathlib import Path
__all__ = ["write", "Lane", "LIMIT"]
LIMIT = 3
def write(path: Path, data: dict, *, newline: str | None = None) -> None:
    pass
class Lane:
    name: str = "x"
    def submit(self, spec: dict) -> str:
        return ""
    @property
    def ready(self) -> bool:
        return True
    def _private(self):
        pass
"""


# ---------------------------------------------------------------- what a contract records

def test_describe_records_kinds_parameters_and_public_members(tmp_path):
    c = planted(tmp_path, BASE)
    assert c["format"] == ac.FORMAT
    names = c["names"]
    assert sorted(names) == ["LIMIT", "Lane", "write"]
    assert names["LIMIT"] == {"kind": "data", "type": "int", "value": "3"}
    write = names["write"]
    assert write["kind"] == "function"
    assert [(p["name"], p["kind"]) for p in write["params"]] == [
        ("path", "positional_or_keyword"), ("data", "positional_or_keyword"), ("newline", "keyword_only")]
    assert write["params"][0]["annotation"] == "Path" and "default" not in write["params"][0]
    assert write["params"][2]["default"] == "None"
    assert write["returns"] == "None"
    members = names["Lane"]["members"]
    assert sorted(members) == ["name", "ready", "submit"]          # private members are not part of the contract
    assert members["name"]["kind"] == "field" and members["ready"]["kind"] == "property"
    assert members["submit"]["kind"] == "method"


def test_describe_skips_members_inherited_from_outside_the_framework(tmp_path):
    c = planted(tmp_path, """
        import json
        __all__ = ["Codec"]
        class Codec(json.JSONEncoder):
            def extra(self) -> int:
                return 1
    """)
    assert sorted(c["names"]["Codec"]["members"]) == ["extra"]       # json.JSONEncoder's methods are not ours
    assert c["names"]["Codec"]["bases"] == ["json.encoder.JSONEncoder"]


def test_a_constructor_records_its_parameters_not_a_return_annotation(tmp_path):
    """A dataclass's generated __init__ carries a return annotation whose text differs between Python versions."""
    c = planted(tmp_path, """
        from dataclasses import dataclass
        __all__ = ["Check"]
        @dataclass
        class Check:
            name: str
            ok: bool = True
    """)
    init = c["names"]["Check"]["init"]
    assert [p["name"] for p in init["params"]] == ["name", "ok"] and "returns" not in init


def test_a_protocol_records_its_methods_and_no_constructor(tmp_path):
    """A Protocol is never constructed; its synthetic __init__ differs between Python versions."""
    c = planted(tmp_path, """
        from typing import Protocol
        __all__ = ["Hooks"]
        class Hooks(Protocol):
            def adapter(self, adapter) -> None:
                ...
    """)
    entry = c["names"]["Hooks"]
    assert "init" not in entry and sorted(entry["members"]) == ["adapter"] and entry["bases"] == ["typing.Protocol"]


def test_describe_refuses_a_name_in_all_that_does_not_exist(tmp_path):
    with pytest.raises(ac.ContractError, match="missing"):
        planted(tmp_path, '__all__ = ["missing"]\n')


def test_describe_is_the_same_whatever_the_platform_or_process(tmp_path):
    """Default values print differently per platform (WindowsPath vs PosixPath) and per process (object addresses):
    the recorded contract must not, or CI on Linux would fail a contract recorded on Windows."""
    c = planted(tmp_path, """
        from pathlib import Path
        __all__ = ["f"]
        _UNSET = object()
        def f(a=Path("x"), b=_UNSET):
            pass
    """)
    defaults = [p["default"] for p in c["names"]["f"]["params"]]
    assert defaults == ["Path('x')", "<object object>"]


# ---------------------------------------------------------------- what breaks a caller

@pytest.mark.parametrize("change", [
    ("remove a name", 'LIMIT = 3', ''),
    ("add a required parameter", "dict, *, newline", "dict, mode: str, *, newline"),
    ("rename a parameter", "def write(path: Path", "def write(target: Path"),
    ("drop a parameter", ", *, newline: str | None = None", ""),
    ("drop a default", "newline: str | None = None", "newline: str | None"),
    ("reorder positional parameters", "path: Path, data: dict", "data: dict, path: Path"),
    ("make a keyword parameter positional-only", "path: Path, data: dict,", "path: Path, data: dict, /,"),
    ("remove a method", "    def submit(self, spec: dict) -> str:\n        return \"\"\n", ""),
    ("remove a field", '    name: str = "x"\n', ""),
    ("turn a function into data", "def write(path: Path, data: dict, *, newline: str | None = None) -> None:\n    pass",
     "write = None"),
], ids=lambda c: c[0])
def test_breaking_changes_are_reported_as_breaking(tmp_path, change):
    _label, old, new = change
    assert old in BASE
    source = BASE.replace(old, new)
    if _label == "remove a name":
        source = source.replace('"LIMIT"', "").replace(', ]', "]")
    diff = ac.compare(planted(tmp_path, BASE), planted(tmp_path, source))
    assert diff.breaking, f"{_label} was not reported as breaking"


@pytest.mark.parametrize("change,name", [
    (("add a name", '"LIMIT"]', '"LIMIT", "extra"]\ndef extra():\n    pass'), "extra"),
    (("add an optional keyword parameter", "newline: str | None = None)", "newline: str | None = None, keep=False)"),
     "write"),
    (("add a method", "    def _private(self):", "    def cancel(self) -> None:\n        pass\n    def _private(self):"),
     "Lane.cancel"),
    (("give a required parameter a default", "data: dict,", "data: dict = None,"), "write"),
], ids=lambda c: c[0] if isinstance(c, tuple) else c)
def test_compatible_changes_are_listed_by_name_and_not_breaking(tmp_path, change, name):
    _label, old, new = change
    assert old in BASE
    diff = ac.compare(planted(tmp_path, BASE), planted(tmp_path, BASE.replace(old, new)))
    assert not diff.breaking
    assert name in [n for n, _what in diff.changes]


def test_a_positional_parameter_that_loses_its_default_breaks_callers(tmp_path):
    source = "__all__ = ['f']\ndef f(a, b=1):\n    pass\n"
    diff = ac.compare(planted(tmp_path, source), planted(tmp_path, source.replace("b=1", "b")))
    assert [n for n, _what in diff.breaking] == ["f"]


def test_identical_contracts_differ_in_nothing(tmp_path):
    diff = ac.compare(planted(tmp_path, BASE), planted(tmp_path, BASE))
    assert not diff.breaking and not diff.changes


# ---------------------------------------------------------------- the check

DOC = ("# Plugin API\n\nIntro.\n\n## Files\n\n### `write`\n\n`write(path, data)` writes `data`; raises OSError when it "
       "cannot.\n\n### `Lane`\n\nA lane.\n\n### `LIMIT`\n\nThree.\n\n### `extra`\n\nExtra.\n")
LOG = "# Changelog\n\n## Unreleased\n\n- **Plugin API:** added `extra`.\n\n## 1.11.0 — 2026-11-01\n\n- old\n"


def run_check(current, recorded="same", baseline=None, version="1.12.0", baseline_version="1.11.0",
              changelog=LOG, api_doc=DOC, baseline_doc=DOC):
    return ac.check(current, current if recorded == "same" else recorded, baseline, version=version,
                    baseline_version=baseline_version, changelog=changelog, api_doc=api_doc,
                    baseline_doc=baseline_doc)


def test_the_check_fails_when_the_recorded_contract_is_not_the_code(tmp_path):
    """A planted change: the code moved on and docs/plugin_api.json was not recorded again."""
    old, new = planted(tmp_path, BASE), planted(tmp_path, BASE.replace("dict, *,", "dict, mode: str, *,"))
    result = run_check(new, recorded=old, baseline=None)
    assert not result.ok
    assert any("record" in p and "write" in p for p in result.problems)


def test_the_check_without_a_previous_contract_passes_and_says_so(tmp_path):
    result = run_check(planted(tmp_path, BASE), baseline=None, baseline_version=None)
    assert result.ok, result.problems
    assert any("no previous" in n for n in result.notes)


def test_a_breaking_change_needs_a_major_version(tmp_path):
    old = planted(tmp_path, BASE)
    new = planted(tmp_path, BASE.replace("def write(path: Path", "def write(target: Path"))
    named = LOG.replace("added `extra`", "changed `write`")
    refused = run_check(new, baseline=old, version="1.12.0", changelog=named)
    assert not refused.ok and any("major" in p for p in refused.problems)
    accepted = run_check(new, baseline=old, version="2.0.0", changelog=named)
    assert accepted.ok, accepted.problems
    assert any("deprecat" in n for n in accepted.notes)            # the reviewer still checks the deprecation window


def test_an_addition_needs_a_plugin_api_changelog_line_naming_it(tmp_path):
    old = planted(tmp_path, BASE)
    new = planted(tmp_path, BASE.replace('"LIMIT"]', '"LIMIT", "extra"]\ndef extra():\n    pass'))
    assert run_check(new, baseline=old).ok
    missing = run_check(new, baseline=old, changelog=LOG.replace("added `extra`", "added things"))
    assert not missing.ok and any("extra" in p and "CHANGELOG" in p for p in missing.problems)
    unlabelled = run_check(new, baseline=old, changelog=LOG.replace("**Plugin API:** ", ""))
    assert not unlabelled.ok


def test_an_addition_is_not_a_patch_release(tmp_path):
    old = planted(tmp_path, BASE)
    new = planted(tmp_path, BASE.replace('"LIMIT"]', '"LIMIT", "extra"]\ndef extra():\n    pass'))
    assert not run_check(new, baseline=old, version="1.11.1").ok
    unreleased = run_check(new, baseline=old, version="1.11.0")      # development before the release commit
    assert unreleased.ok and any("minor" in n for n in unreleased.notes)


def test_every_public_name_is_documented(tmp_path):
    result = run_check(planted(tmp_path, BASE), api_doc="# Plugin API\n\n`write` and `Lane`.\n")
    assert not result.ok and any("LIMIT" in p and "PLUGIN_API.md" in p for p in result.problems)
    assert not run_check(planted(tmp_path, BASE), api_doc=None).ok


# ---------------------------------------------------------------- the previous contract, from git

def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8").stdout


def test_the_previous_contract_is_read_from_the_last_tag(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "README").write_text("x\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "first")
    git(repo, "tag", "-a", "v1.0.0", "-m", "1.0.0")
    assert ac.previous_contract(repo) == ("v1.0.0", None)            # a tag from before the contract existed
    (repo / "docs" / "plugin_api.json").write_text(json.dumps({"format": 1, "names": {}}), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "second")
    git(repo, "tag", "-a", "v1.1.0", "-m", "1.1.0")
    (repo / "docs" / "plugin_api.json").write_text(json.dumps({"format": 1, "names": {"x": {}}}), encoding="utf-8")
    git(repo, "commit", "-qam", "third, untagged")
    assert ac.previous_contract(repo) == ("v1.1.0", {"format": 1, "names": {}})   # the tag's file, not the tree's



def test_a_shallow_clone_cannot_stand_in_for_the_previous_contract(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    for i in range(2):
        (repo / "f").write_text(str(i), encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", str(i))
    git(repo, "tag", "-a", "v1.0.0", "HEAD~1", "-m", "1.0.0")
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", repo.as_uri(), str(shallow))
    with pytest.raises(ac.ContractError, match="shallow"):
        ac.previous_contract(shallow)


# ---------------------------------------------------------------- this repository

def test_the_recorded_contract_is_the_code():
    """docs/plugin_api.json describes supervisor.plugin as it is: G3 catches a stale contract too, not only G2."""
    import supervisor.plugin
    recorded = json.loads((REPO / ac.CONTRACT_FILE).read_text(encoding="utf-8"))
    assert ac.describe(supervisor.plugin) == recorded


def test_the_command_line_check_passes_on_this_repository():
    out = subprocess.run([sys.executable, "-m", "supervisor.api_contract", "check", "--against", "none"], cwd=REPO,
                         capture_output=True, text=True, encoding="utf-8", check=False)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "plugin API contract: OK" in out.stdout


@pytest.mark.parametrize("old,new,broken_call", [
    ("def f(a, /, **kwargs):\n    pass\n", "def f(a, **kwargs):\n    pass\n", "f(1, a=2)"),
    ("def f(*args, **kwargs):\n    pass\n", "def f(args, *rest, **kwargs):\n    pass\n", "f()"),
], ids=["positional-only-name-now-collides-with-kwargs", "required-parameter-named-like-an-old-collector"])
def test_review_1_11_0_f3_signature_changes_that_break_a_call_are_breaking(tmp_path, old, new, broken_call):
    """Review 1.11.0 F3: two changes that make a previously valid call fail, at the comparison and at the gate."""
    namespace_old, namespace_new = {}, {}
    exec(old, namespace_old)                                           # noqa: S102 - the test's own literal source
    exec(new, namespace_new)                                           # noqa: S102 - the test's own literal source
    eval(broken_call, namespace_old)                                   # valid against the old signature
    with pytest.raises(TypeError):
        eval(broken_call, namespace_new)                               # and not against the new one
    before = planted(tmp_path, "__all__ = ['f']\n" + old)
    after = planted(tmp_path, "__all__ = ['f']\n" + new)
    assert {n for n, _w in ac.compare(before, after).breaking} == {"f"}
    result = run_check(after, baseline=before, version="1.12.0", baseline_version="1.11.0",
                       changelog=LOG.replace("added `extra`", "changed `f`"))
    assert not result.ok and any("major" in p for p in result.problems)


@pytest.mark.parametrize("old,new,broken_call", [
    ("def f(*args, a):\n    pass\n", "def f(a, *args):\n    pass\n", "f(1, a=2)"),
    ("def f(x, /, *, a):\n    pass\n", "def f(a):\n    pass\n", "f(1, a=2)"),
], ids=["keyword-only-moved-into-old-args", "keyword-only-moved-into-a-positional-only-slot"])
def test_review_1_11_0_r2_2_a_keyword_only_parameter_a_positional_argument_now_reaches_is_breaking(
        tmp_path, old, new, broken_call):
    """Review 1.11.0 R2-2: a keyword-only parameter that becomes positional-or-keyword in a slot an old call could
    fill by position (old `*args`, or an old positional-only parameter) gets two values from `f(1, a=2)`."""
    namespace_old, namespace_new = {}, {}
    exec(old, namespace_old)                                           # noqa: S102 - the test's own literal source
    exec(new, namespace_new)                                           # noqa: S102 - the test's own literal source
    eval(broken_call, namespace_old)                                   # valid against the old signature
    with pytest.raises(TypeError):
        eval(broken_call, namespace_new)                               # and not against the new one
    before = planted(tmp_path, "__all__ = ['f']\n" + old)
    after = planted(tmp_path, "__all__ = ['f']\n" + new)
    assert {n for n, _w in ac.compare(before, after).breaking} == {"f"}
    result = run_check(after, baseline=before, version="1.12.0", baseline_version="1.11.0",
                       changelog=LOG.replace("added `extra`", "changed `f`"))
    assert not result.ok and any("major" in p for p in result.problems)


@pytest.mark.parametrize("old,new", [
    ("def f(*, a):\n    pass\n", "def f(a):\n    pass\n"),
    ("def f(x, *, a):\n    pass\n", "def f(x, a):\n    pass\n"),
], ids=["no-old-positional-argument", "slot-beyond-every-old-positional-argument"])
def test_a_keyword_only_parameter_no_old_positional_argument_reaches_may_become_positional(tmp_path, old, new):
    """The guard for R2-2: when no call valid against the old signature has a positional argument in the new slot,
    `a=2` still binds alone, and the change is compatible."""
    before = planted(tmp_path, "__all__ = ['f']\n" + old)
    after = planted(tmp_path, "__all__ = ['f']\n" + new)
    assert ac.compare(before, after).breaking == []


# ---------------------------------------------------------------- the human contract (review 1.11.0 F5)

NO_API = "# Changelog\n\n## 1.11.1 - 2026-11-02\n\n- Fixed something else. No API changes.\n\n## 1.11.0\n\n- old\n"


def test_every_public_name_has_its_own_documented_section(tmp_path):
    """A bare mention is not documentation: each name needs a `### `name`` section with text."""
    c = planted(tmp_path, BASE)
    names_only = "# Plugin API\n\n`write`, `Lane`, `LIMIT`, `extra`.\n"
    result = run_check(c, api_doc=names_only, baseline=None)
    assert not result.ok and any("write" in p and "section" in p for p in result.problems)
    empty = DOC.replace("Three.", "")
    result = run_check(c, api_doc=empty, baseline=None)
    assert not result.ok and any("LIMIT" in p for p in result.problems)


def test_the_review_probe_deleting_every_description_is_caught(tmp_path):
    """Review 1.11.0 F5's probe: the same signatures, a document of bare names, a patch release, no API line."""
    c = planted(tmp_path, BASE)
    result = run_check(c, baseline=c, version="1.11.1", baseline_version="1.11.0", changelog=NO_API,
                       api_doc="# Plugin API\n\n`write`, `Lane`, `LIMIT`, `extra`.\n")
    assert not result.ok


GUARANTEE_CHANGED = DOC.replace("raises OSError when it cannot.", "returns None when it cannot.")


def test_a_changed_documented_guarantee_must_be_classified(tmp_path):
    """Same signature, a different documented guarantee: G2 does not take the unchanged path."""
    c = planted(tmp_path, BASE)
    result = run_check(c, baseline=c, version="1.11.1", baseline_version="1.11.0", changelog=NO_API,
                       api_doc=GUARANTEE_CHANGED)
    assert not result.ok
    assert any("write" in p and "documented" in p and "CHANGELOG" in p for p in result.problems)
    assert not any("unchanged since" in n for n in result.notes)


@pytest.mark.parametrize("entry,version,ok", [
    ("- **Plugin API:** `write` returns None instead of raising.", "1.12.0", True),      # compatible: minor
    ("- **Plugin API:** `write` returns None instead of raising.", "1.11.1", False),     # not in a patch
    ("- **Plugin API (editorial):** `write`'s wording.", "1.11.1", True),               # editorial: any version
    ("- **Plugin API (breaking):** `write` no longer raises.", "1.12.0", False),         # breaking: major only
    ("- **Plugin API (breaking):** `write` no longer raises.", "2.0.0", True),
    ("- **Plugin API:** `Lane` gains nothing.", "1.12.0", False),                         # names the wrong thing
])
def test_the_classification_decides_the_version(tmp_path, entry, version, ok):
    c = planted(tmp_path, BASE)
    changelog = f"# Changelog\n\n## {version} - 2026-11-02\n\n{entry}\n\n## 1.11.0\n\n- old\n"
    result = run_check(c, baseline=c, version=version, baseline_version="1.11.0", changelog=changelog,
                       api_doc=GUARANTEE_CHANGED)
    assert result.ok is ok, result.problems
    if ok:
        assert any("documented" in n and "write" in n for n in result.notes)       # surfaced for the reviewer


def test_whitespace_and_reflowing_are_not_a_documented_change(tmp_path):
    c = planted(tmp_path, BASE)
    reflowed = DOC.replace("writes `data`; raises", "writes `data`;\nraises")
    result = run_check(c, baseline=c, version="1.11.1", baseline_version="1.11.0", changelog=NO_API, api_doc=reflowed)
    assert result.ok and any("unchanged since" in n for n in result.notes)


def test_a_tag_with_a_contract_but_no_document_is_an_error(tmp_path):
    c = planted(tmp_path, BASE)
    result = run_check(c, baseline=c, version="1.11.1", baseline_version="1.11.0", changelog=NO_API,
                       baseline_doc=None)
    assert not result.ok and any("PLUGIN_API.md" in p for p in result.problems)


def test_the_previous_document_is_read_from_the_last_tag(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "docs" / "PLUGIN_API.md").write_text("tagged text\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "first")
    git(repo, "tag", "-a", "v1.0.0", "-m", "1.0.0")
    (repo / "docs" / "PLUGIN_API.md").write_text("later text\n", encoding="utf-8")
    git(repo, "commit", "-qam", "second")
    assert ac.document_at(repo, "v1.0.0") == "tagged text\n"

