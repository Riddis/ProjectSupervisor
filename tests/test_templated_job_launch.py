"""A job block written as `{adapter, template, params}` -- the only shape a plan's `set_job` can write (RF-04,
`plans._set_job_block`) -- must launch the template's own argv/fn, substituted through the same
`preflight._substitute` the preflight itself runs, never the adapter's hard-coded default
(`[sys.executable, "-c", "print('hello')"]` / `fn="noop"`). `preflight.resolve_for_launch` is the one place both
adapters' `prepare()` resolve a templated block, and it refuses -- rather than guesses -- when the template has
gone or the block has changed since its preflight was recorded."""
from __future__ import annotations

import sys

import pytest
import yaml

from supervisor import gate, preflight
from supervisor import tasks as tasks_mod
from supervisor.adapters.process import ProcessAdapter
from supervisor.adapters.python import PythonAdapter
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

PY = sys.executable


def _write_templates(layout, templates):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text(yaml.safe_dump({"version": 1, "templates": templates}),
                                                       encoding="utf-8")


# ----------------------------------------------------------------------- a templated block prepares the template's own command

def test_a_templated_process_job_prepares_the_templates_argv(layout):
    """The shape a plan's `set_job` writes: `{adapter, template, params}`, no `argv` at all. The stored preflight
    is `ok: True` and the gate clears it; `ProcessAdapter.prepare()` must return the template's own argv, not the
    adapter's hard-coded default."""
    _write_templates(layout, {"harness": {"adapter": "process",
                                          "argv": [PY, "-c", "open('MARKER.txt','w').write('ran')"],
                                          "may_be_set_by": ["owner", "plan"], "params": {}}})
    make_task(layout, "T1", status="NOT_STARTED", job={"adapter": "process", "template": "harness", "params": {}})
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.template == "harness" and t1.job.argv is None
    assert t1.job.preflight.ok is True, t1.job.preflight
    assert gate.check(layout, t1, "process", "delivery", running=[]) is None, "the gate lets it launch"

    spec = ProcessAdapter().prepare(layout, t1, "T1.a1")
    assert spec["argv"] == [PY, "-c", "open('MARKER.txt','w').write('ran')"], \
        f"the lane prepares {spec['argv']!r}, not the template's own argv"


def test_a_param_value_with_spaces_is_substituted_as_one_argv_element(layout):
    """`_substitute`'s own rule -- an argv element that is exactly `{name}` becomes one element, the value
    stringified and never split on whitespace -- now decides a real command line through `prepare()`, not just
    the preflight's own, separate call to the same function."""
    _write_templates(layout, {"harness": {"adapter": "process",
                                          "argv": [PY, "-c", "import sys; print(len(sys.argv) - 1)", "{message}"],
                                          "may_be_set_by": ["owner", "plan"],
                                          "params": {"message": {"type": "str"}}}})
    make_task(layout, "T1", status="NOT_STARTED",
              job={"adapter": "process", "template": "harness", "params": {"message": "two words"}})
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.preflight.ok is True, t1.job.preflight
    spec = ProcessAdapter().prepare(layout, t1, "T1.a1")
    assert spec["argv"] == [PY, "-c", "import sys; print(len(sys.argv) - 1)", "two words"], \
        f"a value with spaces must stay exactly one argv element, got {spec['argv']!r}"


def test_a_templated_python_job_prepares_the_templates_fn(layout, monkeypatch):
    """`fn` names a registered function the template declares, not the adapter's own `"noop"` default."""
    from supervisor.adapters import python as python_mod
    seen = {}

    def marker(root, run_dir, **params):
        seen["ran"] = True
        return {"status": "COMPLETE"}
    monkeypatch.setitem(python_mod.JOBS, "marker_fn", marker)
    _write_templates(layout, {"pyjob": {"adapter": "python", "fn": "marker_fn", "may_be_set_by": ["owner", "plan"],
                                        "params": {}}})
    make_task(layout, "T1", status="NOT_STARTED", job={"adapter": "python", "template": "pyjob", "params": {}})
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.preflight.ok is True, t1.job.preflight   # the preflight resolved `marker_fn` through the template
    spec = PythonAdapter().prepare(layout, t1, "T1.a1")
    assert spec["fn"] == "marker_fn", f"the lane prepares fn={spec['fn']!r}, not the template's own fn"


_CHILD = ("import json, os, pathlib; run = pathlib.Path(os.environ['SUPERVISOR_RUN_DIR']); "
          "pathlib.Path('MARKER.txt').write_text('ran', encoding='utf-8'); "
          "(run / 'result.json').write_text(json.dumps("
          "{'status': 'COMPLETE', 'artifacts': ['MARKER.txt'], 'notes': 'ran'}), encoding='utf-8')")


def test_end_to_end_a_templated_process_task_runs_the_templates_command(layout):
    """Through the real loop: a READY task whose job is a template reference. The template's command writes
    the declared artifact in the project root; the task must not end FAILED for a missing artifact the
    adapter's own hard-coded default (`print('hello')`) would never have written."""
    _write_templates(layout, {"harness": {"adapter": "process", "argv": [PY, "-c", _CHILD],
                                          "may_be_set_by": ["owner", "plan"], "params": {}}})
    make_task(layout, "P", status="READY", artifacts=["MARKER.txt"], owned_paths=["MARKER.txt"],
              job={"adapter": "process", "template": "harness", "params": {}})
    assert tasks_mod.load(layout, "P").job.preflight.ok is True
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(30):
        sup.once()
        if tasks_mod.load(layout, "P").status in (TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.BLOCKED):
            break
    t = tasks_mod.load(layout, "P")
    assert (layout.root / "MARKER.txt").exists(), f"task ended {t.status.value}; the template's command never ran"
    assert t.status == TaskStatus.COMPLETE, t.status


def test_end_to_end_a_param_value_with_spaces_reaches_the_child_as_one_argument(layout):
    """Through the real loop: the template's `{message}` slot is substituted with a value that contains spaces,
    and the child process must receive it as exactly one `sys.argv` element -- `len(sys.argv) == 2` -- never
    split into two by a shell or by the adapter's own launch."""
    child = ("import sys, json, os, pathlib; run = pathlib.Path(os.environ['SUPERVISOR_RUN_DIR']); "
             "pathlib.Path('MARKER.txt').write_text(sys.argv[1], encoding='utf-8'); "
             "(run / 'result.json').write_text(json.dumps("
             "{'status': 'COMPLETE' if len(sys.argv) == 2 else 'FAILED', "
             "'artifacts': ['MARKER.txt'], 'notes': 'ran'}), encoding='utf-8')")
    _write_templates(layout, {"harness": {"adapter": "process", "argv": [PY, "-c", child, "{message}"],
                                          "may_be_set_by": ["owner", "plan"],
                                          "params": {"message": {"type": "str"}}}})
    make_task(layout, "P", status="READY", artifacts=["MARKER.txt"], owned_paths=["MARKER.txt"],
              job={"adapter": "process", "template": "harness", "params": {"message": "two words"}})
    assert tasks_mod.load(layout, "P").job.preflight.ok is True
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(30):
        sup.once()
        if tasks_mod.load(layout, "P").status in (TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.BLOCKED):
            break
    t = tasks_mod.load(layout, "P")
    assert (layout.root / "MARKER.txt").read_text(encoding="utf-8") == "two words", \
        "a param value with spaces must reach the child as exactly one argument, not split across two"
    assert t.status == TaskStatus.COMPLETE, t.status


# ----------------------------------------------------------------------- the control: a literal block is unaffected

def test_a_literal_argv_block_with_no_template_launches_exactly_what_it_declares(layout):
    """Unchanged by this fix: a literal, owner-authored block carries its own `argv` directly and is launched
    byte for byte, never touched by template resolution."""
    make_task(layout, "T1", status="NOT_STARTED",
              job={"adapter": "process", "argv": [PY, "-c", "pass", "--flag", "value"]})
    t1 = tasks_mod.load(layout, "T1")
    spec = ProcessAdapter().prepare(layout, t1, "T1.a1")
    assert spec["argv"] == [PY, "-c", "pass", "--flag", "value"]


def test_a_literal_fn_block_with_no_template_launches_exactly_what_it_declares(layout):
    make_task(layout, "T1", status="NOT_STARTED", job={"adapter": "python", "fn": "noop"})
    t1 = tasks_mod.load(layout, "T1")
    spec = PythonAdapter().prepare(layout, t1, "T1.a1")
    assert spec["fn"] == "noop"


def test_a_task_with_no_job_block_at_all_still_prepares_the_adapters_default(layout):
    """Invariant 7's own exemption (gate.py, `preflight_binding`): a task with no `job` block at all is not
    refused, and still gets the adapter's harmless no-op default -- this release never touches that case."""
    make_task(layout, "T1", status="NOT_STARTED")
    t1 = tasks_mod.load(layout, "T1")
    spec = ProcessAdapter().prepare(layout, t1, "T1.a1")
    assert spec["argv"] == [PY, "-c", "print('hello')"]


# ----------------------------------------------------------------------- the launch refuses rather than guesses

def test_launch_refuses_rather_than_guesses_when_the_template_is_gone(layout):
    """The template is deleted from `config/job_templates.yaml` after the preflight passed against it; the job
    block on disk is untouched. The launch must refuse outright, never fall back to the adapter's own default."""
    _write_templates(layout, {"harness": {"adapter": "process", "argv": [PY, "-c", "pass"],
                                          "may_be_set_by": ["owner"], "params": {}}})
    make_task(layout, "T1", status="NOT_STARTED", job={"adapter": "process", "template": "harness", "params": {}})
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.preflight.ok is True
    _write_templates(layout, {})
    t1 = tasks_mod.load(layout, "T1")
    with pytest.raises(preflight.LaunchResolutionError):
        ProcessAdapter().prepare(layout, t1, "T1.a1")


def test_launch_refuses_rather_than_guesses_when_the_block_changed_since_its_preflight(layout):
    """The job block's own digest no longer matches the one its stored preflight was recorded against -- the
    same staleness `gate.stored_preflight_reason` refuses on, repeated inside the adapter so `prepare()` never
    resolves a block its own stored preflight does not describe, whatever path reaches it."""
    _write_templates(layout, {"harness": {"adapter": "process", "argv": [PY, "-c", "pass"],
                                          "may_be_set_by": ["owner"], "params": {}}})
    make_task(layout, "T1", status="NOT_STARTED", job={"adapter": "process", "template": "harness", "params": {}})
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.preflight.ok is True
    stale = t1.model_copy(deep=True)
    stale.job.params = {"never": "declared"}   # the block changed in memory; its preflight still describes the old one
    with pytest.raises(preflight.LaunchResolutionError):
        ProcessAdapter().prepare(layout, stale, "T1.a1")


def test_launch_refuses_rather_than_guesses_when_the_templates_own_body_changed_since_the_preflight(layout):
    """The job block itself is byte-identical to the one its preflight was recorded against (so `block_digest`
    still matches), but the *template* it names was edited afterwards -- its argv now resolves to a different
    command. Neither `block_digest` nor "the template id is still present" catches this; `template_digest` must."""
    _write_templates(layout, {"harness": {"adapter": "process", "argv": [PY, "-c", "pass"],
                                          "may_be_set_by": ["owner"], "params": {}}})
    make_task(layout, "T1", status="NOT_STARTED", job={"adapter": "process", "template": "harness", "params": {}})
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.preflight.ok is True
    assert t1.job.preflight.detail.get("template_digest"), "a templated pass must record a template_digest"
    # The template's own body is edited -- same id, still present, different argv -- the job block on disk is
    # untouched, so its stored preflight (and block_digest) still describe the pre-edit block exactly.
    _write_templates(layout, {"harness": {"adapter": "process", "argv": [PY, "-c", "import os; os.remove('x')"],
                                          "may_be_set_by": ["owner"], "params": {}}})
    t1 = tasks_mod.load(layout, "T1")
    with pytest.raises(preflight.LaunchResolutionError):
        ProcessAdapter().prepare(layout, t1, "T1.a1")


def test_launch_does_not_refuse_a_preflight_recorded_before_template_digest_existed(layout):
    """A stored preflight predating this check carries no `template_digest` at all; its absence is read as "not
    recorded", never as a mismatch, so a block already on disk is not held by the upgrade that adds the check."""
    _write_templates(layout, {"harness": {"adapter": "process", "argv": [PY, "-c", "pass"],
                                          "may_be_set_by": ["owner"], "params": {}}})
    make_task(layout, "T1", status="NOT_STARTED", job={"adapter": "process", "template": "harness", "params": {}})
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.preflight.ok is True
    stale = t1.model_copy(deep=True)
    stale.job.preflight.detail.pop("template_digest", None)   # simulates a preflight recorded before this release
    spec = ProcessAdapter().prepare(layout, stale, "T1.a1")
    assert spec["argv"] == [PY, "-c", "pass"]
