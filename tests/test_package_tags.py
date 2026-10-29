"""1.14.0: plugin packages live in the framework repository (packages/<distribution>/, docs/PLUGIN_STANDARD.md)
and each package release is tagged `<identity>-vX.Y.Z`. Only `vX.Y.Z` tags are framework releases. A package tag
never counts as one: not in the pinned update check, not as the release a runtime checkout runs (doctor), not as the
previous release G2 compares the plugin API with, and not in the skills history.

An identity starts with a lowercase letter, so `vision-v1.0.0` (identity `vision`) and `v2-v1.0.0` (identity `v2`)
are package tags that a `v*` or `v[0-9]*` pattern would take."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from supervisor import api_contract, doctor, updates
from tests import test_updates
from tests.test_updates import git, release, tag

world, pinned = test_updates.world, test_updates.pinned       # the pinned runtime checkout's fixtures

REPO = Path(__file__).resolve().parents[1]
PACKAGE_TAGS = ("render-v2.0.0", "vision-v3.0.0", "v2-v4.0.0")


def test_the_pinned_update_check_never_announces_a_package_tag(pinned):
    for name in PACKAGE_TAGS:
        (pinned["author"] / "packages.txt").write_text(name + "\n", encoding="utf-8")
        git(pinned["author"], "add", "-A")
        git(pinned["author"], "commit", "-q", "-m", name)
        tag(pinned["author"], name)
        git(pinned["author"], "push", "-q", "origin", "main", name)
    st = updates.check(pinned["layout"], fetch=True, max_age_s=0)
    assert st.ok and st.pinned_tag == "v1.0.0" and not st.newer_release and not st.available


def test_the_release_a_runtime_checkout_runs_is_its_framework_tag_not_a_package_tag(pinned):
    """A package released on the same merge commit as a framework release tags that commit twice."""
    release(pinned["author"], "1.1.0")
    for name in PACKAGE_TAGS:
        git(pinned["author"], "tag", "-a", name, "-m", name, "v1.1.0^{commit}")
        git(pinned["author"], "push", "-q", "origin", name)
    git(pinned["clone"], "fetch", "-q", "--tags", "origin")
    git(pinned["clone"], "checkout", "-q", "v1.1.0")
    assert updates.runtime_checkout()["tag"] == "v1.1.0"
    st = updates.check(pinned["layout"], fetch=True, max_age_s=0)
    assert st.ok and st.pinned_tag == "v1.1.0" and not st.newer_release


def test_a_checkout_at_a_package_tag_alone_is_not_at_a_framework_release(pinned, monkeypatch):
    (pinned["author"] / "packages.txt").write_text("package only\n", encoding="utf-8")
    git(pinned["author"], "add", "-A")
    git(pinned["author"], "commit", "-q", "-m", "a package release")
    tag(pinned["author"], "render-v1.0.0")
    git(pinned["author"], "push", "-q", "origin", "main", "render-v1.0.0")
    git(pinned["clone"], "fetch", "-q", "--tags", "origin")
    git(pinned["clone"], "checkout", "-q", "render-v1.0.0")
    assert updates.runtime_checkout()["tag"] is None
    c = doctor.check_framework(pinned["layout"])
    assert not c.ok and "not at a tag" in c.detail
    st = updates.check(pinned["layout"], fetch=True, max_age_s=0)
    assert not st.ok and "not at a release tag" in st.reason


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@example.com"), ("config", "user.name", "t")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def _commit(repo: Path, files: dict[str, str], message: str, *tags: str) -> None:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True, capture_output=True)
    for name in tags:
        subprocess.run(["git", "tag", "-a", name, "-m", name], cwd=repo, check=True, capture_output=True)


def test_g2_compares_with_the_last_framework_release_not_a_later_package_tag(tmp_path):
    repo = _repo(tmp_path)
    released = {"format": 1, "names": {}}
    _commit(repo, {"docs/plugin_api.json": json.dumps(released)}, "framework release", "v1.0.0")
    for name in PACKAGE_TAGS:
        _commit(repo, {f"packages/{name}.txt": name}, name, name)
    assert api_contract.previous_contract(repo) == ("v1.0.0", released)


@pytest.fixture
def skills_history(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("skills_history_under_test", REPO / "tools" / "skills_history.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    repo = _repo(tmp_path)
    monkeypatch.setattr(mod, "ROOT", repo)
    monkeypatch.setattr(mod, "OUT", repo / "supervisor" / "skills_history.json")
    return mod, repo


def test_the_skills_history_reads_framework_release_tags_only(skills_history):
    mod, repo = skills_history
    skill = mod.PREFIX + "alpha/SKILL.md"
    _commit(repo, {skill: "alpha as released\n"}, "framework release", "v1.0.0")
    for name in PACKAGE_TAGS:
        _commit(repo, {skill: f"alpha at {name}\n"}, name, name)
    assert mod.main() == 0
    history = json.loads(mod.OUT.read_text(encoding="utf-8"))["files"]
    assert history == {"alpha/SKILL.md": [hashlib.sha256(b"alpha as released\n").hexdigest()]}
