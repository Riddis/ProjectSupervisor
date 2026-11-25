"""P2-3: a `path` slot through a symlink makes `_vendor.contract.contained` raise `ValueError("Paths must not
traverse links")`, and `plans._param_refusals` let that exception out of `validate()`, against its own "never
raises" contract. Case (a) is deterministic (patches `contained` itself, so it runs on every machine); case (b) is
the real shape, a symlink inside the declared root, skipped where the platform or the privilege does not allow
creating one. The rest are controls: every other path-slot refusal (an outside-root relative path, an absolute
path, a `..`-escape, a backslash shape, a `~`-prefixed path) must still hold, and a legitimate in-root path must
still be admitted."""
from __future__ import annotations

import os
import sys

import pytest

from supervisor import plans
from tests.conftest import make_task

PY = sys.executable


def _write_templates(layout, templates: dict):
    import yaml
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text(yaml.safe_dump({"version": 1, "templates": templates}),
                                                       encoding="utf-8")


def _codes(refusals):
    return sorted({r["code"] for r in refusals})


def _base_plan(subject="T1", rev=1, ops=()):
    return {"plan_schema": 1, "id": "T1.d1", "subject": {"task": subject}, "reason": "repair this task",
            "expected_rev": {subject: rev}, "operations": [*ops, {"op": "resume", "task": subject, "as": "ready"}]}


def _set_job_plan(value):
    return _base_plan(ops=[{"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o",
                                                           "owner_role": "engineer", "cost_category": "deterministic"}},
                           {"op": "set_job", "task": "T2", "template": "t1", "params": {"file": value}}])


def _path_slot_task(layout):
    make_task(layout, "T1", status="BLOCKED")
    _write_templates(layout, {"t1": {"adapter": "process", "may_be_set_by": ["plan"],
                                     "argv": [PY, "-c", "pass", "{file}"],
                                     "params": {"file": {"type": "path", "roots": ["data"], "must_exist": False}}}})
    (layout.root / "data").mkdir(exist_ok=True)


# ------------------------------------------------------------------ (a) deterministic: contained() itself raises

def test_rf07_when_contained_raises_value_error(layout, monkeypatch):
    """Any `ValueError` out of `contained` - not only the one a real symlink produces - must become RF-07, never
    an exception out of `validate()`."""
    _path_slot_task(layout)

    def _raises(value, roots, root=None):
        raise ValueError(f"Paths must not traverse links: {value}")

    monkeypatch.setattr("supervisor._vendor.contract.contained", _raises)
    _, refusals = plans.validate(_set_job_plan("data/in.txt"), layout)
    assert _codes(refusals) == ["RF-07"], refusals


# ------------------------------------------------------------------ (b) the real case: a symlink inside the root

def test_rf07_path_slot_through_a_real_symlink_inside_the_root(layout):
    _path_slot_task(layout)
    target = layout.root.parent / "escape_target"
    target.mkdir(exist_ok=True)
    link = layout.root / "data" / "link"
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink not creatable here: {exc}")
    _, refusals = plans.validate(_set_job_plan("data/link/x.txt"), layout)
    assert "RF-07" in _codes(refusals), refusals


# ------------------------------------------------------------------ controls: every other path-slot refusal still holds

@pytest.mark.parametrize("value", ["../outside.txt", "data/../../x.txt", "data/../x.txt", "/etc/passwd",
                                    "C:/Windows/win.ini", "data/./../../x", "..\\x", "data\\..\\..\\x", "~/x"])
def test_other_path_slot_escapes_still_refused(layout, value):
    _path_slot_task(layout)
    _, refusals = plans.validate(_set_job_plan(value), layout)
    c = _codes(refusals)
    assert ("RF-07" in c) or ("RF-03" in c), (value, refusals)


def test_a_legitimate_in_root_relative_path_is_still_admitted(layout):
    _path_slot_task(layout)
    _, refusals = plans.validate(_set_job_plan("data/in.txt"), layout)
    assert refusals == [], refusals
