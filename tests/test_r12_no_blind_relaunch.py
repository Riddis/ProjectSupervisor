"""R-12: a failed deterministic job is not relaunched unchanged, and a contract change stops a running attempt.

Reproduces, at the head this fix lands on top of:
  - a process job that exits non-zero, with unchanged inputs, gets a second, identical launch from the stall
    sweep instead of going straight to diagnosis with its own output;
  - a running delivery attempt survives a `task.set_job`/`task.patch` that changes what it is actually running.
A timeout or a declared-retryable exit still gets its one retry, unchanged - covered here too, so the fix cannot
narrow that case while widening this one."""
import sys
import time

from supervisor import commands, jobs, store, tasks
from supervisor.adapters import REGISTRY
from supervisor.escalation import process_outcome_retryable
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def delivery_records(layout, tid):
    return [r for r in jobs.for_task(layout, tid) if (r.get("spec") or {}).get("kind", "delivery") == "delivery"]


def process_task(layout, tid, argv, retry_on=None):
    make_task(layout, tid)
    job = {"adapter": "process", "argv": argv}
    if retry_on is not None:
        job["retry_on"] = list(retry_on)
    tasks._update(layout, tid, lambda d: d.update(job=job, artifacts=[f"{tid}.txt"]))


def run_to_failed(layout, tid, timeout=20):
    sup = Supervisor(layout, sleep_s=0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        sup.once()
        if tasks.load(layout, tid).status == TaskStatus.FAILED:
            return sup
        time.sleep(0.05)
    raise AssertionError(f"{tid} never reached FAILED")


class Recorder(Supervisor):
    """Stands in for the loop's own dispatch during a sweep (same pattern as test_no_silent_failures.py): the
    real escalation decision runs (issue, diagnosis budget, ...) but nothing is actually launched, so a retry and
    a diagnosis are told apart by name alone."""
    def __init__(self, layout):
        super().__init__(layout, sleep_s=0)
        self.sent = []

    def dispatch(self, task, adapter_name, note="dispatch", **kw):
        self.sent.append((task.id, kw.get("kind", "delivery"), note))
        return {"attempt_id": f"{task.id}.x{len(self.sent)}"}


def test_a_deterministic_exit_is_not_relaunched_and_diagnosis_carries_the_output(layout):
    process_task(layout, "T", [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"])
    run_to_failed(layout, "T")
    assert len(delivery_records(layout, "T")) == 1
    assert process_outcome_retryable(layout, "T", "T.a1") is False       # classified at the time it failed
    sup = Recorder(layout)
    assert sup.sweep_stalled(now=time.time() + 15 * 60) == ["T"]
    assert [s[1] for s in sup.sent] == ["diagnosis"]                     # not a second delivery
    assert len(delivery_records(layout, "T")) == 1                       # no second launch, ever
    issue = store.read(layout.issues / "I-001.yaml")
    assert "boom" in issue["detail"]                                     # the worker's own output travels with it


def test_a_declared_retryable_exit_still_gets_one_retry_after_the_grace_period(layout):
    process_task(layout, "T", [sys.executable, "-c", "import sys; sys.exit(7)"], retry_on=[7])
    run_to_failed(layout, "T")
    assert process_outcome_retryable(layout, "T", "T.a1") is True
    sup = Recorder(layout)
    assert sup.sweep_stalled(now=time.time() + 15 * 60) == ["T"]
    assert sup.sent and sup.sent[0][1] == "delivery" and "revision after stalled" in sup.sent[0][2]


def test_a_lost_process_is_still_retried_not_diagnosed(layout):
    """A process the adapter never got a result from (killed, crashed, adopted after a restart) is unknown, not
    ruled out: the same back-off as today, never an immediate exception route."""
    process_task(layout, "T", [sys.executable, "-c", "import time; time.sleep(30)"])
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    rec = jobs.read(layout, "T.a1")
    REGISTRY["process"].terminate(rec["handle"])
    time.sleep(0.5)
    Supervisor(layout, sleep_s=0).once()
    assert tasks.load(layout, "T").status == TaskStatus.FAILED
    assert process_outcome_retryable(layout, "T", "T.a1") is True


def test_a_running_attempt_is_superseded_when_its_job_changes(layout):
    process_task(layout, "T", [sys.executable, "-c", "import time; time.sleep(5)"])
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    rec = jobs.read(layout, "T.a1")
    assert rec["phase"] in ("SUBMITTED", "RUNNING")
    handle = rec["handle"]
    commands.apply_job(layout, "T", {"adapter": "process", "argv": [sys.executable, "-c", "pass"]}, actor="owner")
    sup.once()
    rec = jobs.read(layout, "T.a1")
    assert rec["phase"] == "DONE" and rec.get("result") == "SUPERSEDED"
    assert not REGISTRY["process"].alive(handle)                         # actually terminated, not just marked
    assert not (layout.runtime / "escalation" / "T.json").exists()       # no retry budget spent


def test_a_running_attempt_is_superseded_when_a_dependency_is_added(layout):
    make_task(layout, "DEP", status="COMPLETE")       # terminal: never itself dispatched, only named as a dependency
    process_task(layout, "T", [sys.executable, "-c", "import time; time.sleep(5)"])
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    assert jobs.read(layout, "T.a1")["phase"] in ("SUBMITTED", "RUNNING")
    tasks.patch_task(layout, "T", {"add_dependency": "DEP"}, actor="owner")
    sup.once()
    rec = jobs.read(layout, "T.a1")
    assert rec["phase"] == "DONE" and rec.get("result") == "SUPERSEDED"
