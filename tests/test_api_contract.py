"""Gate G2 (supervisor.api_contract): the public extension API's recorded contract. What a contract records, which
differences break callers and which are additions, the check that ties the code, docs/extension_api.json, the last
release tag's contract, the CHANGELOG and docs/EXTENSION_API.md together, and the repository's own contract
matching its code. Each planted module is a throwaway file, so the rules are tested apart from supervisor.ext."""
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

DOC = "# Extension API\n\n`write`, `Lane` and `LIMIT`, `extra`.\n"
LOG = "# Changelog\n\n## Unreleased\n\n- **Extension API:** added `extra`.\n\n## 1.11.0 — 2026-11-01\n\n- old\n"


def run_check(current, recorded="same", baseline=None, version="1.12.0", baseline_version="1.11.0",
              changelog=LOG, api_doc=DOC):
    return ac.check(current, current if recorded == "same" else recorded, baseline, version=version,
                    baseline_version=baseline_version, changelog=changelog, api_doc=api_doc)


def test_the_check_fails_when_the_recorded_contract_is_not_the_code(tmp_path):
    """A planted change: the code moved on and docs/extension_api.json was not recorded again."""
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


def test_an_addition_needs_an_extension_api_changelog_line_naming_it(tmp_path):
    old = planted(tmp_path, BASE)
    new = planted(tmp_path, BASE.replace('"LIMIT"]', '"LIMIT", "extra"]\ndef extra():\n    pass'))
    assert run_check(new, baseline=old).ok
    missing = run_check(new, baseline=old, changelog=LOG.replace("added `extra`", "added things"))
    assert not missing.ok and any("extra" in p and "CHANGELOG" in p for p in missing.problems)
    unlabelled = run_check(new, baseline=old, changelog=LOG.replace("**Extension API:** ", ""))
    assert not unlabelled.ok


def test_an_addition_is_not_a_patch_release(tmp_path):
    old = planted(tmp_path, BASE)
    new = planted(tmp_path, BASE.replace('"LIMIT"]', '"LIMIT", "extra"]\ndef extra():\n    pass'))
    assert not run_check(new, baseline=old, version="1.11.1").ok
    unreleased = run_check(new, baseline=old, version="1.11.0")      # development before the release commit
    assert unreleased.ok and any("minor" in n for n in unreleased.notes)


def test_every_public_name_is_documented(tmp_path):
    result = run_check(planted(tmp_path, BASE), api_doc="# Extension API\n\n`write` and `Lane`.\n")
    assert not result.ok and any("LIMIT" in p and "EXTENSION_API.md" in p for p in result.problems)
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
    (repo / "docs" / "extension_api.json").write_text(json.dumps({"format": 1, "names": {}}), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "second")
    git(repo, "tag", "-a", "v1.1.0", "-m", "1.1.0")
    (repo / "docs" / "extension_api.json").write_text(json.dumps({"format": 1, "names": {"x": {}}}), encoding="utf-8")
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
    """docs/extension_api.json describes supervisor.ext as it is: G3 catches a stale contract too, not only G2."""
    import supervisor.ext
    recorded = json.loads((REPO / ac.CONTRACT_FILE).read_text(encoding="utf-8"))
    assert ac.describe(supervisor.ext) == recorded


def test_the_command_line_check_passes_on_this_repository():
    out = subprocess.run([sys.executable, "-m", "supervisor.api_contract", "check", "--against", "none"], cwd=REPO,
                         capture_output=True, text=True, encoding="utf-8", check=False)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "extension API contract: OK" in out.stdout


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
