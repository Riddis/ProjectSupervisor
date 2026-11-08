"""The pull-request template is G7's checklist (docs/BOUNDARY.md, "G7 contribution review"): a written answer
to rule 1, a new hook's second user, API/migration/docs impact, which gates were run with their output, and the
plugin-package home if the change adds one."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _text() -> str:
    path = REPO / ".github" / "pull_request_template.md"
    assert path.is_file(), ".github/pull_request_template.md is missing"
    return path.read_text(encoding="utf-8")


def test_asks_would_a_different_project_want_this():
    text = _text().lower()
    assert "would a different project want this" in text


def test_asks_a_new_hooks_second_user():
    text = _text().lower()
    assert "second user" in text
    assert "hook" in text


def test_asks_api_migration_and_docs_impact():
    text = _text().lower()
    for word in ("api", "migration", "docs"):
        assert word in text
    assert "not applicable" in text or "n/a" in text


def test_asks_which_gates_were_run_with_their_output():
    text = _text().lower()
    assert "gate" in text
    assert "output" in text


def test_asks_the_plugin_packages_home_if_the_change_adds_one():
    text = _text().lower()
    assert "plugin" in text
    assert "home" in text
