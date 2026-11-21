"""`set_job` is the one plan operation that touches the D-018 frontier (RECOVERY_PLAN.md section 2.2's `set_job`
row, step 6 of section 9): it may only correct a parameter of the job an existing task already has (class A), or
set the first job of a task this same plan created (class B, exercised in test_plans_apply.py's `_setup`).
test_plans.py already owns the validator's RF-05..RF-09/RF-11/RF-12 refusals for a template that is unknown, not
plannable, out of a bad parameter, or out of scope; this module owns what those tests do not: a class A parameter
diff carried all the way through `apply()`, the two identity-change shapes (adapter, template) on an *existing*
job, and the real preflight — not a bare resolve check — behind RF-08, with its transcript digest landing on the
plan document itself whether the plan is refused by it or applies clean."""
from __future__ import annotations

import sys

import yaml

from supervisor import plans, store
from supervisor import tasks as tasks_mod
from tests.conftest import make_task

PY = sys.executable


def _write_templates(layout, templates: dict):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text(yaml.safe_dump({"version": 1, "templates": templates}),
                                                       encoding="utf-8")


def _plan(subject, rev, set_job_op):
    return {"plan_schema": 1, "id": f"{subject}.d1", "subject": {"task": subject}, "reason": "repair this task",
            "expected_rev": {subject: rev}, "operations": [set_job_op, {"op": "resume", "task": subject, "as": "ready"}]}


def _one_param_diff_entry(report_dict):
    entries = report_dict.get("preflight") or []
    assert len(entries) == 1, f"expected exactly one preflight transcript entry, got {entries}"
    return entries[0]


# ------------------------------------------------------------------ class A: a parameter diff on an existing job

def test_a_parameter_diff_on_an_existing_jobs_template_applies(layout):
    """The one shape class A admits: the task keeps its adapter and template, only `params` moves."""
    _write_templates(layout, {"t1": {"adapter": "process", "argv": [PY, "-c", "pass", "{mode}"],
                                     "may_be_set_by": ["plan"],
                                     "params": {"mode": {"type": "enum", "values": ["a", "b"]}}}})
    make_task(layout, "T1", status="BLOCKED", job={"adapter": "process", "template": "t1", "params": {"mode": "a"}})
    rev = tasks_mod.load(layout, "T1").rev
    doc = _plan("T1", rev, {"op": "set_job", "task": "T1", "template": "t1", "params": {"mode": "b"}})

    proposed = plans.propose(layout, doc)
    assert proposed["status"] == "awaiting_approval", proposed.get("refusals")
    entry = _one_param_diff_entry(proposed["report"])
    assert entry["ok"] is True and len(entry["digest"]) == 64

    applied = plans.apply(layout, doc["id"])
    assert applied["status"] == "applied"
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.adapter == "process" and t1.job.template == "t1" and t1.job.params == {"mode": "b"}
    assert t1.status.value == "READY"


# ------------------------------------------------------------------ RF-11: the two identity-change shapes on an
# existing job (its first job is test_plans.py's own test_rf11_job_identity_change_first_job)

def test_rf11_changing_an_existing_jobs_adapter_is_refused(layout):
    _write_templates(layout, {"t1": {"adapter": "process", "argv": [PY, "-c", "pass"], "may_be_set_by": ["plan"],
                                     "params": {}},
                              "t2": {"adapter": "python", "fn": "whatever", "may_be_set_by": ["plan"], "params": {}}})
    make_task(layout, "T1", status="BLOCKED", job={"adapter": "process", "template": "t1", "params": {}})
    rev = tasks_mod.load(layout, "T1").rev
    doc = _plan("T1", rev, {"op": "set_job", "task": "T1", "template": "t2", "params": {}})
    _, refusals = plans.validate(doc, layout)
    assert refusals and all(r["code"] == "RF-11" for r in refusals), refusals


def test_rf11_changing_an_existing_jobs_template_is_refused(layout):
    _write_templates(layout, {"t1": {"adapter": "process", "argv": [PY, "-c", "pass"], "may_be_set_by": ["plan"],
                                     "params": {}},
                              "t3": {"adapter": "process", "argv": [PY, "-c", "pass"], "may_be_set_by": ["plan"],
                                    "params": {}}})
    make_task(layout, "T1", status="BLOCKED", job={"adapter": "process", "template": "t1", "params": {}})
    rev = tasks_mod.load(layout, "T1").rev
    doc = _plan("T1", rev, {"op": "set_job", "task": "T1", "template": "t3", "params": {}})
    _, refusals = plans.validate(doc, layout)
    assert refusals and all(r["code"] == "RF-11" for r in refusals), refusals


# ------------------------------------------------------------------ RF-08: the real preflight, not a resolve check

def test_set_job_whose_probe_fails_is_refused_rf08_with_its_transcript_digest_on_the_plan(layout):
    """A template whose `argv[0]` resolves perfectly well but whose declared `probe` does not: a bare
    "does the executable resolve" check (what this validator used to run) would wave this plan through, and the
    failure would only surface later, as a stall, when the applier's own write re-runs the real preflight. The
    validator must catch it itself, under RF-08, with the failing transcript's digest recorded on the plan
    document regardless of the refusal (RECOVERY_PLAN.md section 2.2's `set_job` row; D-027 decision 6)."""
    _write_templates(layout, {"t1": {"adapter": "process", "argv": [PY, "-c", "pass", "{mode}"],
                                     "probe": [PY, "-c", "import sys; sys.exit(1)"],
                                     "may_be_set_by": ["plan"],
                                     "params": {"mode": {"type": "enum", "values": ["a", "b"]}}}})
    make_task(layout, "T1", status="BLOCKED", job={"adapter": "process", "template": "t1", "params": {"mode": "a"}})
    rev = tasks_mod.load(layout, "T1").rev
    doc = _plan("T1", rev, {"op": "set_job", "task": "T1", "template": "t1", "params": {"mode": "b"}})

    _, refusals = plans.validate(doc, layout)
    assert refusals and all(r["code"] == "RF-08" for r in refusals), refusals

    proposed = plans.propose(layout, doc)
    assert proposed["status"] == "refused"
    entry = _one_param_diff_entry(proposed["report"])
    assert entry["ok"] is False and len(entry["digest"]) == 64

    on_disk = store.read(plans.path_for(layout, doc["id"]))
    assert on_disk["report"]["preflight"] == proposed["report"]["preflight"], \
        "the transcript digest must be the one actually read back from the plan document on disk"
