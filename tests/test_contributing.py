"""CONTRIBUTING.md: the three layers and the one-way dependency rule, rules 1 and 3, which gates apply to
which kind of change, the contributor-then-maintainer review order, and the combined licence with no CLA
(docs/BOUNDARY.md, the owner's boundary and plugin standard decisions)."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _text() -> str:
    path = REPO / "CONTRIBUTING.md"
    assert path.is_file(), "CONTRIBUTING.md is missing"
    return path.read_text(encoding="utf-8")


def test_states_the_three_layers_and_the_one_way_dependency():
    text = _text()
    assert "project" in text and "plugin package" in text and "framework" in text
    assert "project → plugin packages → framework" in text or "project → plugin packages → framework" in text


def test_states_rule_1():
    text = _text()
    assert "would a project about something completely different want this" in text.lower()


def test_states_rule_3():
    text = _text()
    assert "second user" in text.lower()
    assert "hook" in text.lower()


def test_states_which_gates_apply_to_which_change():
    text = _text()
    assert "Which gates apply" in text or "which gates apply" in text.lower()
    for gate in ("G1", "G2", "G3", "G4", "G5", "G6", "G7"):
        assert gate in text


def test_states_the_review_order():
    text = _text()
    lowered = text.lower()
    assert "boundary-review" in lowered
    contributor_idx = lowered.find("boundary-review")
    maintainers_idx = lowered.find("maintainers")
    assert contributor_idx != -1 and maintainers_idx != -1
    assert "rerun every gate" in lowered


def test_states_the_licence_with_no_cla():
    text = _text()
    assert "Apache License 2.0 with the Commons Clause" in text
    assert "no contributor licence agreement" in text.lower()
    assert "combined licence" in text.lower()
