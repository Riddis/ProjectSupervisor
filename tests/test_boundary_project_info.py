"""Gate G1d (supervisor.boundary): no project information anywhere in the framework repository. The owner's rule
(2026-10-28): the framework repo never contains any project's files or information. A project's words are listed in
a local file outside the repository (`SUPERVISOR_PROJECT_TERMS`, else ~/.config/supervisor/project-terms.toml),
never in boundary.toml, so the list itself is not project information in the repo. Every tracked text file is
scanned, not only supervisor/, and a hit is a violation. Without the local file (CI) the rule reports that it did not
run, and CI keeps the generic rules G1a to G1c.

The words here are invented: a real project's word in this file would itself be a violation."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from supervisor import boundary

CONFIG = """
[imports]
allowed = []
dynamic_loaders = []
[literals]
allowed_url_hosts = []
user_names = []
[vendor]
terms = []
files = []
"""

_REAL_ENV = dict(os.environ)                   # read at import, before the autouse fixture replaces it

TERMS = """
[projects.zorblax]
terms = ["zorblax", "quuxify", "Wobble Works"]
"""


@pytest.fixture(autouse=True)
def _no_real_terms(tmp_path, monkeypatch):
    monkeypatch.setenv(boundary.PROJECT_TERMS_ENV, str(tmp_path / "no-such-terms.toml"))


def framework(tmp_path, files: dict[str, str], config: str = CONFIG, git: bool = True) -> Path:
    root = tmp_path / "fw"
    (root / "supervisor").mkdir(parents=True)
    (root / "supervisor" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (root / "boundary.toml").write_text(config, encoding="utf-8")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def terms_file(tmp_path, monkeypatch, text: str = TERMS) -> Path:
    p = tmp_path / "project-terms.toml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv(boundary.PROJECT_TERMS_ENV, str(p))
    return p


def g1d(report) -> list[tuple[str, int, str]]:
    return sorted((f.path, f.line, f.text) for f in report.violations if f.rule == "G1d")


def test_a_project_word_anywhere_in_the_repository_is_a_violation(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch)
    root = framework(tmp_path, {
        "docs/GUIDE.md": "# Guide\nThe zorblax lane is an example.\n",
        "tests/test_x.py": "def test_x():\n    assert quuxify()  # noqa\n",
        "CHANGELOG.md": "## 1.0\n- fixed for Wobble Works\n",
        "supervisor/mod.py": "# nothing here\n",
    })
    report = boundary.check(root, user_names=[])
    assert not report.ok
    assert g1d(report) == [("CHANGELOG.md", 2, "Wobble Works"), ("docs/GUIDE.md", 2, "zorblax"),
                           ("tests/test_x.py", 2, "quuxify")]


def test_words_match_whole_words_only(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch, '[projects.p]\nterms = ["blorp"]\n')
    root = framework(tmp_path, {"docs/A.md": "Ask blorpishly, not by blorps_list or xblorp.\n"})
    assert g1d(boundary.check(root, user_names=[])) == []


def test_ignored_files_are_not_scanned(tmp_path, monkeypatch):
    """A contributor's scratch file in an ignored folder never reaches the repository."""
    terms_file(tmp_path, monkeypatch)
    root = framework(tmp_path, {".gitignore": "runs/\n"})
    (root / "runs").mkdir()
    (root / "runs" / "notes.txt").write_text("zorblax\n", encoding="utf-8")
    assert g1d(boundary.check(root, user_names=[])) == []


def test_a_new_file_not_yet_added_is_scanned(tmp_path, monkeypatch):
    """1.13.0, a skill-trial finding (boundary-review, round 2): G1d read only what git tracks, so a new file in
    uncommitted work was never checked, and a clean G1d says nothing. Untracked files that are not ignored count
    too: they are what the next commit adds."""
    terms_file(tmp_path, monkeypatch)
    root = framework(tmp_path, {})
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "NEW.md").write_text("A zorblax example.\n", encoding="utf-8")
    assert g1d(boundary.check(root, user_names=[])) == [("docs/NEW.md", 1, "zorblax")]


def test_without_git_every_file_but_caches_and_runs_is_scanned(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch)
    root = framework(tmp_path, {"docs/A.md": "zorblax\n"}, git=False)
    (root / "runs").mkdir()
    (root / "runs" / "log.txt").write_text("zorblax\n", encoding="utf-8")
    assert g1d(boundary.check(root, user_names=[])) == [("docs/A.md", 1, "zorblax")]


def test_project_words_in_boundary_toml_are_a_violation(tmp_path):
    """The old [projects.<name>] list put a project's vocabulary into the framework repo itself."""
    root = framework(tmp_path, {}, config=CONFIG + '[projects.zorblax]\nterms = ["zorblax"]\n')
    report = boundary.check(root, user_names=[])
    assert [(f.rule, f.path) for f in report.violations] == [("G1d", "boundary.toml")]
    assert "project-terms.toml" in report.violations[0].detail or boundary.PROJECT_TERMS_ENV in \
        report.violations[0].detail


def test_without_the_local_file_g1d_says_it_did_not_run(tmp_path):
    root = framework(tmp_path, {"docs/A.md": "zorblax\n"})
    report = boundary.check(root, user_names=[])
    assert report.ok and report.notes == []
    assert report.skipped and "G1d" in report.skipped[0]


def test_an_invalid_local_file_stops_the_check(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch, "[projects.p\n")
    with pytest.raises(boundary.ConfigError, match="project-terms"):
        boundary.check(framework(tmp_path, {}), user_names=[])


def test_the_cli_prints_g1d_hits_and_fails(tmp_path, monkeypatch):
    terms_file(tmp_path, monkeypatch)
    root = framework(tmp_path, {"docs/A.md": "zorblax\n"})
    out = []
    assert boundary.run(root, echo=out.append) == 1
    assert any("docs/A.md:1: G1d" in line for line in out)


def test_the_framework_repository_holds_no_project_information():
    """Runs against the contributor's own local word list; in CI there is none, and the generic rules run instead."""
    terms = boundary.load_project_terms(boundary.project_terms_path(_REAL_ENV))
    if not terms:
        pytest.skip("no local project-terms file: G1d runs on a contributor's machine, not in CI")
    report = boundary.check(Path(__file__).resolve().parents[1], user_names=[], terms=terms)
    assert g1d(report) == [], "\n".join(str(f) for f in report.violations if f.rule == "G1d")
