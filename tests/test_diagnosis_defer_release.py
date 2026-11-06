"""A diagnosis `defer` names what releases the task (2.2.x).

Before, a defer only wrote its `blocked_on` prose on the task and left it BLOCKED, the stall sweep skipped it as
"parked on purpose", and nothing ever created the work the prose described: the loop went idle until someone wrote
that task by hand. Now a defer carries a `new_task` contract (created like `supervisor task create`) and/or
`wait_for` ids; they join the task's depends_on and the task goes back to READY. A defer that names nothing, or a
proposal the supervisor refuses, reaches the owner as one decision item instead of parking silently."""
import json
import time

import pytest

from supervisor import diagnosis, escalation, inbox, jobs, store, tasks
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_exception_route_and_checkpoints import diag_records, failing_task, fake_claude, run_until  # noqa: F401
from tests.test_no_silent_failures import Recorder, _stop


def proposal(tid="FIX-1", **fields):
    doc = {"id": tid, "title": "Repair the shared parser", "objective": "the parser accepts the documented input",
           "owner_role": "engineer", "owned_paths": ["src/parser"], "artifacts": ["src/parser/core.py"],
           "actions": ["fix the parser"], "tests": ["the documented input parses"], "reviewer": [],
           "acceptance": "the parser test passes"}
    doc.update(fields)
    return doc


def stopped(layout, tid="T", **fields):
    make_task(layout, tid, priority=5, **fields)
    tasks.set_status(layout, tid, "IN_PROGRESS")
    tasks.set_status(layout, tid, "BLOCKED", actor="escalation", note="retries stopped")


def diag_rec(layout, tid="T", attempt="T.a9", level="manager", role="senior_pm", **extra):
    return jobs.write(layout, {"attempt_id": attempt, "task_id": tid, "adapter": "claude_cli", "phase": "RUNNING",
                               "spec": {"kind": "diagnosis"}, "handle": {}, "diag_level": level, "diag_role": role,
                               "diag_signature": "s", "diag_issue": None, **extra})


def defer(layout, rec=None, **obj):
    rec = rec or diag_rec(layout)
    raw = {"kind": "diagnosis", "schema_version": 1, "action": "defer", "summary": "a defect in delivered code",
           "blocked_on": "the parser must accept the documented input", **obj}
    return diagnosis.ingest(Supervisor(layout, sleep_s=0), rec, Result(status="COMPLETE", raw=raw))


def events(layout):
    return [json.loads(e)["event"] for e in layout.log.read_text().splitlines()]


def test_a_new_task_proposal_is_created_wired_and_the_task_released(layout):
    stopped(layout)
    out = defer(layout, new_task=proposal())
    assert out["action"] == "defer" and out["created"] == "FIX-1" and out["depends_on"] == ["FIX-1"]

    fix = tasks.load(layout, "FIX-1")                     # created through the task-create path, nothing started
    assert fix.status == TaskStatus.NOT_STARTED and fix.phase == 0 and fix.priority == 5
    assert any("diagnosis of T" in n["note"] for n in (fix.model_extra or {}).get("notes", []))
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.READY and t.depends_on == ["FIX-1"]
    assert t.history[-1]["by"] == "escalation" and "FIX-1" in t.history[-1]["note"]
    rec = jobs.read(layout, "T.a9")
    assert rec["diag_action"] == "defer" and rec["diag_release"]["depends_on"] == ["FIX-1"]
    assert escalation.current_grant(layout, "T") == 1    # a fresh retry budget for when it runs again
    assert inbox.open_items(layout, "owner") == []
    assert [x.id for x in tasks.eligible(layout)] == ["FIX-1"]      # T waits for the fix

    # a replay of the same result (crash before the job record closed) creates and grants nothing twice
    again = defer(layout, rec=jobs.read(layout, "T.a9"), new_task=proposal())
    assert again["action"] == "defer"
    assert tasks.load(layout, "T").depends_on == ["FIX-1"] and escalation.current_grant(layout, "T") == 1
    assert len(list(layout.tasks.glob("FIX-1*.yaml"))) == 1

    tasks.set_status(layout, "FIX-1", "COMPLETE", actor="owner", force=True, note="fixed")
    assert "T" in [x.id for x in tasks.eligible(layout)]           # released once the fix is complete


def test_wait_for_an_existing_unfinished_task_releases_the_task_behind_it(layout):
    make_task(layout, "W")
    stopped(layout)
    out = defer(layout, wait_for=["W"])
    assert out["action"] == "defer" and out["created"] is None
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.READY and t.depends_on == ["W"]
    assert inbox.open_items(layout, "owner") == []


@pytest.mark.parametrize("obj, why", [
    ({"new_task": {"id": "FIX-1", "title": "no objective or owner"}}, "not a valid task contract"),
    ({"new_task": proposal(job={"adapter": "process", "command": "anything"})}, "only contract fields"),
    ({"new_task": proposal(status="COMPLETE")}, "only contract fields"),
    ({"new_task": proposal(id="W")}, "already a task"),
    ({"new_task": proposal(depends_on=["T"])}, "dependency cycle"),
    ({"new_task": proposal(artifacts=["/abs/out.txt"])}, "relative to the project root"),
    ({"new_task": proposal(owned_paths=[], cost_category="claude")}, "must declare owned_paths"),
    ({"new_task": proposal(phase=1)}, "later-phase"),
    ({"new_task": "write a fix task"}, "must be an object"),
    ({"wait_for": ["NOPE"]}, "no live task"),
    ({"wait_for": ["T"]}, "this task"),
    ({"wait_for": ["DONE"]}, "already complete"),
    ({"wait_for": "W"}, "list of task ids"),
])
def test_a_proposal_the_supervisor_refuses_goes_to_the_owner(layout, obj, why):
    make_task(layout, "W")
    make_task(layout, "DONE", status="COMPLETE")
    stopped(layout)
    out = defer(layout, **obj)
    assert out["action"] == "owner" and out["inbox"]
    items = inbox.open_items(layout, "owner")
    assert len(items) == 1 and items[0]["kind"] == inbox.DECISION_KIND and items[0]["task_id"] == "T"
    text = items[0]["summary"] + (items[0].get("detail") or "")
    assert why in text and "depends_on" in text
    assert not (layout.tasks / "FIX-1.yaml").exists()
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.BLOCKED and t.depends_on == []
    assert jobs.read(layout, "T.a9")["diag_action"] == "owner"
    assert "diagnosis_defer_refused" in events(layout)


def test_a_role_routing_does_not_configure_is_a_refusal_not_a_warning(layout):
    """`supervisor task create` only warns about an unconfigured role (the owner may be about to add it); a
    machine-proposed task that would wait for it forever is refused, since it would only move the stall."""
    (layout.config).mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text("roles:\n  engineer: {}\n", encoding="utf-8")
    stopped(layout)
    out = defer(layout, new_task=proposal(owner_role="nobody"))
    assert out["action"] == "owner"
    assert not (layout.tasks / "FIX-1.yaml").exists()


def test_a_defer_that_names_no_task_goes_to_the_owner_not_into_a_silent_park(layout):
    """Prose alone is what stalled the loop: no component acts on `blocked_on`, and before this a deferred task was
    skipped by the stall sweep, so the only signal was a generic 'Pipeline idle' item much later. The owner gets
    one decision item at once, naming what the diagnosis said it waits for."""
    stopped(layout)
    out = defer(layout)
    assert out["action"] == "owner"
    items = inbox.open_items(layout, "owner")
    assert len(items) == 1 and "the parser must accept the documented input" in items[0]["summary"]
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED
    jobs.set_phase(layout, "T.a9", "DONE")                                  # the diagnosis job closed, as the loop does
    assert Recorder(layout).sweep_stalled(now=time.time() + 3600) == []    # the open item routes it
    inbox.answer(layout, items[0]["id"], "DEFER", "", "owner")
    assert Recorder(layout).sweep_stalled(now=time.time() + 3600) == []    # and so does the owner's answer


def test_a_held_task_declines_the_defer_and_creates_nothing(layout):
    stopped(layout)
    tasks.hold(layout, "T", "owner", "looking at it myself")
    out = defer(layout, new_task=proposal())
    assert out["action"] == "held"
    assert not (layout.tasks / "FIX-1.yaml").exists()
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.BLOCKED and t.depends_on == [] and t.held
    assert jobs.read(layout, "T.a9")["diag_action"] == "held"


def test_the_defer_budget_sends_the_third_release_to_the_owner(layout):
    stopped(layout)
    for n in (1, 2):
        diag_rec(layout, attempt=f"T.a{n}", phase="DONE", diag_action="defer",
                 diag_release={"new_task": None, "depends_on": [f"OLD-{n}"], "warnings": []})
    out = defer(layout, new_task=proposal())
    assert out["action"] == "owner" and "budget" in inbox.open_items(layout, "owner")[0]["summary"]
    assert not (layout.tasks / "FIX-1.yaml").exists()


def test_a_legacy_prose_defer_no_longer_hides_a_stall_from_the_sweep(layout):
    """A defer recorded before this rule left its task BLOCKED on prose; the sweep used to skip it forever."""
    now = _stop(layout, "T", "BLOCKED", minutes_ago=15)
    diag_rec(layout, phase="DONE", diag_action="defer", finished="9999-01-01T00:00:00+00:00")
    assert Recorder(layout).sweep_stalled(now=now) == ["T"]


def test_the_manager_result_contract_offers_new_task_and_wait_for():
    assert '"new_task":{' in diagnosis.RESULT_CONTRACT and '"wait_for":' in diagnosis.RESULT_CONTRACT
    assert "goes to the owner" in diagnosis.RESULT_CONTRACT
    assert "job" not in diagnosis.NEW_TASK_FIELDS and "status" not in diagnosis.NEW_TASK_FIELDS


@pytest.mark.usefixtures("fake_claude")
def test_a_manager_defer_through_the_loop_creates_the_task_it_names(layout, monkeypatch):
    """End to end: the manager's fenced JSON (tests/fixtures/fake_diagnosis.py) carries the proposal. The new task
    waits on an owner-blocked task, so nothing else runs while the outcome is checked."""
    make_task(layout, "W")
    tasks.hold(layout, "W", "owner", "not yet")
    monkeypatch.setenv("FAKE_DIAGNOSIS_ACTION", "defer")
    monkeypatch.setenv("FAKE_DIAGNOSIS_NEW_TASK", json.dumps(proposal(depends_on=["W"])))
    failing_task(layout, "T")
    run_until(layout, lambda: any(r.get("diag_action") for r in diag_records(layout, "T")))
    assert [r["diag_action"] for r in diag_records(layout, "T")] == ["defer"]
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.READY and t.depends_on == ["FIX-1"]
    assert tasks.load(layout, "FIX-1").depends_on == ["W"]
    assert inbox.open_items(layout, "owner") == []
    assert store.read(layout.issues / "I-001.yaml")["diagnosis"][0]["action"] == "defer"
