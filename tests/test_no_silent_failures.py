"""Nothing stops silently (1.6.0):
every worker outcome other than COMPLETE is routed (BLOCKED → exception route, FAILED/PARTIAL/lost → retry within
budget), and a sweep catches any task left FAILED/BLOCKED with nothing in flight and nothing waiting on the owner."""
import os
import time

import pytest

from supervisor import inbox, jobs, store, tasks
from supervisor.adapters import python as pyad
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_exception_route_and_checkpoints import diag_records, fake_claude, run_until  # noqa: F401  (fixture)


@pyad.job("report_blocked")
def _report_blocked(root, run_dir, **_):
    from pathlib import Path
    Path(run_dir, "HANDOFF.md").write_text("BLOCKED: the tool folder is outside this session's permissions", encoding="utf-8")
    return {"status": "BLOCKED", "artifacts": [], "notes": "cannot reach the tool folder"}


@pyad.job("report_failed")
def _report_failed(root, run_dir, **_):
    return {"status": "FAILED", "artifacts": [], "notes": "the tool crashed half way"}


def job_task(layout, tid, fn):
    make_task(layout, tid)
    tasks._update(layout, tid, lambda d: d.update(job={"adapter": "python", "fn": fn}, artifacts=[f"{tid}.txt"]))


def delivery_records(layout, tid):
    return [r for r in jobs.for_task(layout, tid) if (r.get("spec") or {}).get("kind", "delivery") == "delivery"]


@pytest.mark.usefixtures("fake_claude")
def test_a_blocked_worker_goes_straight_to_diagnosis_with_its_reason(layout):
    os.environ["FAKE_DIAGNOSIS_ACTION"] = "owner"
    job_task(layout, "T", "report_blocked")
    run_until(layout, lambda: inbox.open_items(layout, "owner"))
    assert len(delivery_records(layout, "T")) == 1                 # no blind retry of the same brief
    assert len(diag_records(layout, "T")) == 1
    issue = store.read(layout.issues / "I-001.yaml")
    assert "worker reported BLOCKED" in issue["title"]
    assert "outside this session's permissions" in issue["detail"]  # the worker's own reason travels with it
    t = tasks.load(layout, "T")
    assert "outside this session's permissions" in t.delivery.verification.detail
    items = inbox.open_items(layout, "owner")
    assert len(items) == 1 and items[0]["task_id"] == "T"


@pytest.mark.usefixtures("fake_claude")
def test_a_failed_agent_run_is_retried_within_the_budget_then_diagnosed(layout, monkeypatch):
    monkeypatch.setattr(Supervisor, "IMMEDIATE_RETRY_LANES", ("python",))     # stand in for a model lane
    os.environ["FAKE_DIAGNOSIS_ACTION"] = "defer"
    job_task(layout, "T", "report_failed")
    run_until(layout, lambda: diag_records(layout, "T") and diag_records(layout, "T")[0].get("diag_action"))
    # first run + one revision: the identical failure signature twice stops retrying and opens the exception route
    assert len(delivery_records(layout, "T")) == 2
    assert any("revision after worker_failed" in a.note for a in tasks.load(layout, "T").attempts)


def _stop(layout, tid, status, minutes_ago):
    """A task left FAILED/BLOCKED by a path that routes nothing (the pre-fix behaviour, an adapter that vanished...)."""
    make_task(layout, tid)
    tasks._update(layout, tid, lambda d: d.update(job={"adapter": "python", "fn": "report_failed"}))
    tasks.set_status(layout, tid, "IN_PROGRESS")
    tasks.set_status(layout, tid, status, note="left here by something that routes nothing")
    return time.time() + minutes_ago * 60


class Recorder(Supervisor):
    def __init__(self, layout):
        super().__init__(layout, sleep_s=0)
        self.sent = []

    def dispatch(self, task, adapter_name, note="dispatch", **kw):
        self.sent.append((task.id, kw.get("kind", "delivery"), note))
        return {"attempt_id": f"{task.id}.x{len(self.sent)}"}


def test_sweep_waits_for_the_grace_period(layout):
    now = _stop(layout, "T", "FAILED", minutes_ago=2)
    assert Recorder(layout).sweep_stalled(now=now) == []


def test_sweep_retries_a_stalled_failed_task_once_per_stop(layout):
    now = _stop(layout, "T", "FAILED", minutes_ago=15)
    sup = Recorder(layout)
    assert sup.sweep_stalled(now=now) == ["T"]
    assert sup.sent and sup.sent[0][1] == "delivery" and "revision after stalled" in sup.sent[0][2]
    sup.sweep_stalled(now=now)
    assert [x for x in sup.sent if x[1] == "delivery"] == sup.sent[:1]   # never a second retry for the same stop


@pytest.mark.usefixtures("fake_claude")
def test_sweep_sends_a_stalled_blocked_task_to_diagnosis(layout):
    now = _stop(layout, "T", "BLOCKED", minutes_ago=15)
    sup = Recorder(layout)
    assert sup.sweep_stalled(now=now) == ["T"]
    assert [s[1] for s in sup.sent] == ["diagnosis"]
    assert store.read(layout.issues / "I-001.yaml")["title"].endswith("stopped with nothing routing it")


def test_sweep_hands_an_escalated_but_unrouted_task_to_the_owner_once(layout):
    now = _stop(layout, "T", "BLOCKED", minutes_ago=15)
    sup = Recorder(layout)
    sup.sweep_stalled(now=now)                                      # escalated; the recorder launched nothing
    assert inbox.open_items(layout, "owner") == []
    later = now + 15 * 60
    assert sup.sweep_stalled(now=later) == ["T"]
    items = inbox.open_items(layout, "owner")
    assert len(items) == 1 and items[0]["kind"] == inbox.DECISION_KIND and items[0]["task_id"] == "T"
    assert sup.sweep_stalled(now=later + 3600) == []                # open item = a route


def test_sweep_respects_an_owner_defer(layout):
    now = _stop(layout, "T", "BLOCKED", minutes_ago=15)
    item = inbox.create(layout, for_role="owner", subject="s", summary="s", kind=inbox.DECISION_KIND,
                        allowed_responses=list(inbox.DECISION_RESPONSES), task_id="T")
    inbox.answer(layout, item["id"], "DEFER")
    inbox.apply_decision(layout, inbox.load(layout, item["id"]))
    assert Recorder(layout).sweep_stalled(now=now) == []


def test_sweep_ignores_tasks_with_work_in_flight(layout):
    now = _stop(layout, "T", "FAILED", minutes_ago=15)
    jobs.prepare(layout, "T", "T.a9", "python", {"kind": "delivery"})
    assert Recorder(layout).sweep_stalled(now=now) == []


def test_idle_pipeline_raises_one_owner_item_per_stuck_set(layout):
    now = _stop(layout, "T", "BLOCKED", minutes_ago=60)
    sup = Recorder(layout)
    assert sup.alert_if_idle(now=now) is None                      # idle clock starts
    assert sup.alert_if_idle(now=now + 60) is None                 # not long enough yet
    item = sup.alert_if_idle(now=now + 3600)
    assert item and "Nothing can run" in item["summary"] and "T BLOCKED" in item["summary"]
    inbox.answer(layout, item["id"], "OK")
    assert sup.alert_if_idle(now=now + 7200) is None                # same stuck set: said once


def test_idle_alert_stays_quiet_while_work_is_eligible(layout):
    make_task(layout, "A")
    assert Recorder(layout).alert_if_idle(now=time.time() + 99999) is None


def test_a_failed_deterministic_run_waits_for_the_sweep_then_retries(layout):
    job_task(layout, "T", "report_failed")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.FAILED and len(delivery_records(layout, "T")) == 1     # no immediate retry
    assert "the tool crashed half way" in t.delivery.verification.detail
    assert sup.sweep_stalled(now=time.time() + 15 * 60) == ["T"]
    assert len(delivery_records(layout, "T")) == 2                                          # backed-off retry


def test_an_owner_rejection_of_a_human_task_is_not_auto_retried(layout):
    from tests.test_inbox import human_task
    human_task(layout, "H")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    item = inbox.open_items(layout, "owner")[0]
    inbox.answer(layout, item["id"], "REJECT", note="no")
    sup.once()
    assert tasks.load(layout, "H").status == TaskStatus.FAILED
    assert sup.sweep_stalled(now=time.time() + 3600) == []                                 # the owner's answer stands


def test_role_and_cost_category_dirs_are_added_to_the_session(layout):
    """A task that must work in a folder outside the repository (a tool installation) gets it via the role's
    `add_dirs` or routing.yaml `claude.add_dirs_by_cost`; nothing is added by default."""
    import yaml
    from supervisor.adapters.claude_cli import extra_dirs
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({
        "claude": {"add_dirs_by_cost": {"local": ["D:\\Tools\\Renderer"]}}}), encoding="utf-8")
    make_task(layout, "G")
    tasks._update(layout, "G", lambda d: d.update(cost_category="local"))
    dirs = extra_dirs(layout, tasks.load(layout, "G"), {"add_dirs": ["shared"]})
    assert dirs[0] == str(layout.root) and "D:\\Tools\\Renderer" in dirs and str(layout.root / "shared") in dirs
    make_task(layout, "C")
    assert extra_dirs(layout, tasks.load(layout, "C"), {}) == [str(layout.root)]


def test_owner_requeue_text_reaches_the_next_brief(layout):
    from supervisor import packets
    _stop(layout, "T", "BLOCKED", minutes_ago=15)
    item = inbox.create(layout, for_role="owner", subject="s", summary="s", kind=inbox.DECISION_KIND,
                        allowed_responses=list(inbox.DECISION_RESPONSES), task_id="T")
    inbox.answer(layout, item["id"], "REQUEUE the server is running now, go ahead")
    inbox.apply_decision(layout, inbox.load(layout, item["id"]))
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.READY
    assert "the server is running now" in packets.delivery_brief(layout, t)


def test_a_note_command_can_carry_guidance(layout):
    from supervisor import commands, packets
    make_task(layout, "T")
    commands.submit_or_apply(layout, "task.note", {"task_id": "T", "note": "use the runbook script", "guidance": True},
                             actor="owner")
    assert "use the runbook script" in packets.delivery_brief(layout, tasks.load(layout, "T"))


# ---- deliberate holds (independent review of 1.6.0, finding 1)

def test_sweep_leaves_an_owner_hold_alone(layout):
    make_task(layout, "T")
    tasks._update(layout, "T", lambda d: d.update(job={"adapter": "python", "fn": "report_failed"}))
    tasks.set_status(layout, "T", "BLOCKED", actor="owner-ui", note="held by owner")     # Control Center Hold
    sup = Recorder(layout)
    assert sup.sweep_stalled(now=time.time() + 3600) == [] and sup.sent == []


def test_sweep_leaves_a_snoozed_decision_alone(layout):
    now = _stop(layout, "T", "BLOCKED", minutes_ago=15)
    item = inbox.create(layout, for_role="owner", subject="s", summary="s", kind=inbox.DECISION_KIND,
                        allowed_responses=list(inbox.DECISION_RESPONSES), task_id="T")
    inbox.snooze(layout, item["id"])
    sup = Recorder(layout)
    assert sup.sweep_stalled(now=now) == [] and sup.sent == []


def test_a_human_rejection_collected_after_it_was_given_is_not_retried(layout):
    """The loop collects the answer later than it was given, so the rejection's timestamp precedes the FAILED stop;
    the sweep must link the stop to the human attempt, not compare times."""
    from tests.test_inbox import human_task
    human_task(layout, "H")
    loop = Supervisor(layout, sleep_s=0)
    loop.once()
    item = inbox.open_items(layout, "owner")[0]
    inbox.answer(layout, item["id"], "REJECT", note="no")
    store.update(inbox.path_for(layout, item["id"]),
                 lambda d: {**d, "answered_at": "2000-01-01T00:00:00+00:00",
                            "answer": {**d["answer"], "at": "2000-01-01T00:00:00+00:00"}},
                 lock_path=layout.state / ".inbox.lock")
    loop.once()
    assert tasks.load(layout, "H").status == TaskStatus.FAILED
    sup = Recorder(layout)
    assert sup.sweep_stalled(now=time.time() + 3600) == [] and sup.sent == []


def test_a_replayed_guidance_note_repairs_missing_guidance(layout):
    """Interrupted after the note but before the guidance (state written by the pre-fix two-step path): the replay
    must complete the guidance, not answer "already applied" (independent review of 1.6.0, finding 4)."""
    from supervisor import commands, packets
    make_task(layout, "T")
    tasks.add_note(layout, "T", "use the runbook script", actor="owner", command_id="C1")       # note only
    out = commands.HANDLERS["task.note"](layout, {"task_id": "T", "note": "use the runbook script", "guidance": True,
                                                  "_id": "C1"}, "owner")
    assert out == {"repaired": "guidance"}
    t = tasks.load(layout, "T")
    assert len(t.model_extra["notes"]) == 1 and len(t.model_extra["guidance"]) == 1
    assert "use the runbook script" in packets.delivery_brief(layout, t)
    again = commands.HANDLERS["task.note"](layout, {"task_id": "T", "note": "use the runbook script", "guidance": True,
                                                    "_id": "C1"}, "owner")
    assert again == {"skipped": "already applied"}
    assert len(tasks.load(layout, "T").model_extra["guidance"]) == 1
