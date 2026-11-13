"""The GPU lease (supervisor.leases) wired into the ordinary launch path (D-013 R4): `supervisor.gate` refuses to
launch a resource-holding job while another project's lease entry is live, and the loop takes the lease for real
right before `submit()` and lets go of it once none of this project's jobs holds the resource slot any more.
Two project Layouts, pointed at the same lease file, stand in for "two projects on one machine"."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import psutil

from supervisor import jobs, leases, tasks, testing
from supervisor.adapters.base import Adapter, Result
from supervisor.loop import Supervisor
from tests.conftest import make_task


class ControllableGpuLane(Adapter):
    """A gpu=True lane whose jobs stay RUNNING until the test flips them done (unlike FakeLane, always alive=False):
    submit() really runs (so the acquire-before-submit hook is exercised), alive() reports from a shared dict."""
    gpu = True

    def __init__(self, name: str):
        self.name = name
        self.done: dict[str, bool] = {}
        self.submitted: list[str] = []

    def prepare(self, layout, task, attempt_id):
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir)}

    def submit(self, spec):
        self.submitted.append(spec["attempt_id"])
        self.done[spec["attempt_id"]] = False
        return {"kind": "controllable", "attempt_id": spec["attempt_id"]}

    def alive(self, handle):
        return not self.done.get(handle["attempt_id"], False)

    def collect(self, spec, handle):
        if self.alive(handle):
            return None
        return Result(status="COMPLETE")


def _project(tmp_path, name, lease_path, label, stale_after_s=None, hand_over_timeout_s=None):
    lease_cfg = {"path": str(lease_path), "label": label}
    if stale_after_s is not None:
        lease_cfg["stale_after_s"] = stale_after_s
    if hand_over_timeout_s is not None:
        lease_cfg["hand_over_timeout_s"] = hand_over_timeout_s
    routing = {"limits": {"max_resource_jobs": 1}, "resources": {"gpu": {"lease": lease_cfg}}}
    return testing.project(tmp_path / name, routing=routing)


def _simulate_restart(lease_file: Path, label: str) -> None:
    """Stand in for the loop process that launched a RUNNING gpu job being gone (supervisor stop/start, a
    crash, a runtime move): a real python exits, and the lease entry is rewritten to that exited process's own
    pid/create_time -- exactly what the file holds once the old loop process is gone."""
    dead = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
    dead_create_time = psutil.Process(dead.pid).create_time()
    dead.wait(timeout=10)
    doc = json.loads(lease_file.read_text(encoding="utf-8"))
    for e in doc["entries"]:
        if e["label"] == label:
            e["pid"], e["create_time"] = dead.pid, dead_create_time
    lease_file.write_text(json.dumps(doc), encoding="utf-8")


def log_lines(layout) -> list[dict]:
    if not layout.log.exists():
        return []
    return [json.loads(line) for line in layout.log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_second_project_waits_for_the_first_to_let_go_of_the_gpu_lease(tmp_path):
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay_a, lay_b = _project(tmp_path, "a", lease_file, "project-a"), _project(tmp_path, "b", lease_file, "project-b")
    lane_a, lane_b = ControllableGpuLane("gpu_a"), ControllableGpuLane("gpu_b")
    with testing.lane(lane_a), testing.lane(lane_b):
        make_task(lay_a, "A", job={"adapter": "gpu_a"}, cost_category="local")
        make_task(lay_b, "B", job={"adapter": "gpu_b"}, cost_category="local")
        sup_a, sup_b = Supervisor(lay_a, sleep_s=0), Supervisor(lay_b, sleep_s=0)

        sup_a.once()
        assert lane_a.submitted == ["A.a1"]                        # a took the lease and launched
        assert jobs.read(lay_a, "A.a1")["phase"] == "RUNNING"

        sup_b.once()
        assert lane_b.submitted == []                              # b is held: a holds the lease
        assert jobs.for_task(lay_b, "B") == []                     # the scheduler never dispatches it in the first place

        lane_a.done["A.a1"] = True                                 # a's job finishes
        sup_a.once()                                                # settle collects it; reconcile releases the lease
        assert jobs.read(lay_a, "A.a1")["phase"] == "DONE"

        sup_b.once()
        assert lane_b.submitted == ["B.a1"]                         # the lease is free: b may launch now
        assert jobs.read(lay_b, "B.a1")["phase"] == "RUNNING"


def test_a_running_gpu_job_renews_its_lease_entry_so_it_outlives_stale_after_s(tmp_path):
    """F1 (docs/reviews/RELEASE_REVIEW_local-lane.md): the loop must renew its own gpu lease entry on every
    reconcile() while the job it covers is still RUNNING, so stale_after_s never prunes a live hold out from
    under it and another project's loop starts on top of it. Once the job leaves RUNNING the entry is let go,
    not kept alive by renewal."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    stale_after_s = 1.0
    lay_a = _project(tmp_path, "a", lease_file, "project-a", stale_after_s=stale_after_s)
    lay_b = _project(tmp_path, "b", lease_file, "project-b", stale_after_s=stale_after_s)
    lane_a, lane_b = ControllableGpuLane("gpu_a"), ControllableGpuLane("gpu_b")
    with testing.lane(lane_a), testing.lane(lane_b):
        make_task(lay_a, "A", job={"adapter": "gpu_a"}, cost_category="local")
        make_task(lay_b, "B", job={"adapter": "gpu_b"}, cost_category="local")
        sup_a, sup_b = Supervisor(lay_a, sleep_s=0), Supervisor(lay_b, sleep_s=0)

        sup_a.once()
        assert jobs.read(lay_a, "A.a1")["phase"] == "RUNNING"        # a holds the lease and is still running

        deadline = time.monotonic() + stale_after_s * 1.6
        while time.monotonic() < deadline:
            time.sleep(stale_after_s / 4)
            sup_a.once()                                             # each reconcile() must renew the live entry

        # more than stale_after_s has passed since the entry was created: it must still be live, not pruned
        assert leases.holder_label(lay_b, "gpu") == "project-a"       # b's own peek still sees a's hold
        sup_b.once()
        assert lane_b.submitted == []                                 # b is still refused: a's job has not ended
        assert jobs.for_task(lay_b, "B") == []

        lane_a.done["A.a1"] = True                                    # a's job finishes
        sup_a.once()                                                   # settle() collects it; reconcile() releases
        assert jobs.read(lay_a, "A.a1")["phase"] == "DONE"
        assert leases.status(lay_a, "gpu")["live"] == []               # left RUNNING: let go, not kept renewed


def test_a_waiting_loop_records_a_release_request_so_the_holder_yields_and_the_waiter_then_launches(tmp_path):
    """F2 (docs/reviews/RELEASE_REVIEW_local-lane.md): gate.check() calls gate.lease_blocked(), whose peek refuses
    the launch before launch() ever reaches acquire() — the only place _record_waiting() lived. A loop that is
    only ever refused by the peek therefore never asked the holder to yield. Four steps, with B driven only
    through Supervisor.once() (never through the lease primitive, which is exactly what the existing hand-over
    test already did and is not evidence of a loop asking): A holds the lease through a raw handle (its
    long-lived server watchdog, not its own loop); B's loop runs and must make A's release_requested() true and
    status() name B as the waiter; A's watchdog poll then stops and releases; B's next pass launches."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay_a = _project(tmp_path, "a", lease_file, "project-a")
    lay_b = _project(tmp_path, "b", lease_file, "project-b")
    holder = leases.resource_lease(lay_a, "gpu")        # A's watchdog, holding for its server — not A's loop
    assert holder.acquire(wait_s=0) is True

    lane_b = ControllableGpuLane("gpu_pb_wait")
    with testing.lane(lane_b):
        make_task(lay_b, "B", job={"adapter": "gpu_pb_wait"}, cost_category="local")
        sup_b = Supervisor(lay_b, sleep_s=0)

        sup_b.once()                                     # step 1: B's loop runs, refused by the peek
        assert lane_b.submitted == []

        assert holder.release_requested() is True         # step 2: A's watchdog poll would now see a request...
        assert leases.status(lay_a, "gpu")["waiting"] == "project-b"   # ...and the doctor names B as waiting

        holder.release()                                  # step 3: A's watchdog poll stops its server, A releases

        sup_b.once()                                       # step 4: the lease is free, B's next pass launches
        assert lane_b.submitted == ["B.a1"]
        assert jobs.read(lay_b, "B.a1")["phase"] == "RUNNING"


def test_repeated_waiting_passes_record_the_release_request_once_and_do_not_restart_the_window(tmp_path):
    """A loop that polls every couple of seconds must not reset release_requested_at on every pass it is refused:
    a window that restarts each time is a hand-over that never completes."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay_a = _project(tmp_path, "a", lease_file, "project-a")
    lay_b = _project(tmp_path, "b", lease_file, "project-b")
    holder = leases.resource_lease(lay_a, "gpu")
    assert holder.acquire(wait_s=0) is True

    lane_b = ControllableGpuLane("gpu_pb_idem")
    with testing.lane(lane_b):
        make_task(lay_b, "B", job={"adapter": "gpu_pb_idem"}, cost_category="local")
        sup_b = Supervisor(lay_b, sleep_s=0)

        sup_b.once()
        first_at = json.loads(lease_file.read_text(encoding="utf-8"))["release_requested_at"]
        assert first_at is not None

        time.sleep(0.2)
        sup_b.once()
        sup_b.once()
        second_at = json.loads(lease_file.read_text(encoding="utf-8"))["release_requested_at"]
        assert second_at == first_at                   # repeated passes did not restart the hand-over window
    holder.release()


def test_a_restarted_loop_retakes_the_entry_for_its_still_running_gpu_job(tmp_path):
    """F19 (docs/reviews/RELEASE_REVIEW_local-lane_round2.md): launch() writes the lease entry once, with this
    process's own pid/create_time; F1's renew() refreshes only an entry whose (holder, pid) is this process's, and
    nothing in the loop ever called acquire() for a record it merely re-adopted. So when the loop process that
    launched a RUNNING gpu job is gone — supervisor stop/start, a crash, a runtime move — the entry is pruned as
    dead on the next read even though the job is still running on the cards, and the other project's peek sees
    nobody there. The restart is stood in for, as the reviewer's own probe does, by spawning a real python,
    letting it exit, and rewriting the entry's pid/create_time to that exited process's identity — exactly what
    the lease file holds once the old loop process is gone. The assertion is the holder B's peek names, not only
    the absence of B's own record, so a timing accident cannot pass for the fix."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay_a = _project(tmp_path, "a", lease_file, "project-a")
    lay_b = _project(tmp_path, "b", lease_file, "project-b")
    lane_a, lane_b = ControllableGpuLane("gpu_a_restart"), ControllableGpuLane("gpu_b_restart")
    with testing.lane(lane_a), testing.lane(lane_b):
        make_task(lay_a, "A", job={"adapter": "gpu_a_restart"}, cost_category="local")
        sup_a = Supervisor(lay_a, sleep_s=0)
        sup_a.once()
        assert jobs.read(lay_a, "A.a1")["phase"] == "RUNNING"          # a holds the lease and is still running

        _simulate_restart(lease_file, "project-a")                     # the old loop's process is now gone

        sup_a.once()                                                   # the restarted loop's own reconcile() tick
        assert jobs.read(lay_a, "A.a1")["phase"] == "RUNNING"           # the job itself never stopped

        assert leases.holder_label(lay_b, "gpu") == "project-a"         # b's peek names the real holder, not None

        make_task(lay_b, "B", job={"adapter": "gpu_b_restart"}, cost_category="local")
        sup_b = Supervisor(lay_b, sleep_s=0)
        sup_b.once()
        assert lane_b.submitted == []                                   # b does not launch on top of a's job
        assert jobs.for_task(lay_b, "B") == []
        assert jobs.read(lay_a, "A.a1")["phase"] == "RUNNING"


def test_a_refused_retake_is_logged_with_the_holder_it_found_and_leaves_the_record_alone(tmp_path):
    """F19: a refusal is not passed over in silence. When the restarted loop's renew() finds nothing to refresh
    and leases._reclaim() is refused because another project's own entry is already live on the cards -- the job
    already lost them to someone else -- the refusal is logged naming that holder, and the refused job's own
    record is left exactly as it was (still RUNNING): the re-take never touches jobs_mod, only the lease file,
    so a refusal never wedges the record; the next reconcile() simply tries again."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay_a = _project(tmp_path, "a", lease_file, "project-a")
    lay_c = _project(tmp_path, "c", lease_file, "project-c")
    lane_a = ControllableGpuLane("gpu_a_refused")
    with testing.lane(lane_a):
        make_task(lay_a, "A", job={"adapter": "gpu_a_refused"}, cost_category="local")
        sup_a = Supervisor(lay_a, sleep_s=0)
        sup_a.once()
        assert jobs.read(lay_a, "A.a1")["phase"] == "RUNNING"

        _simulate_restart(lease_file, "project-a")

        holder_c = leases.resource_lease(lay_c, "gpu")
        assert holder_c.acquire(wait_s=0) is True            # c already started on top before a's loop ticks again

        before = len(log_lines(lay_a))
        sup_a.once()                                          # the restarted loop's tick: renew fails, retake refused

        assert leases.holder_label(lay_a, "gpu") == "project-c"     # the cards really are c's now
        events = [e for e in log_lines(lay_a)[before:] if e["event"] == "gpu_lease_retake_refused"]
        assert events and events[-1]["holder"] == "project-c"        # named, not silent
        assert jobs.read(lay_a, "A.a1")["phase"] == "RUNNING"         # the job record itself is not wedged
        holder_c.release()


def test_the_retake_only_fires_for_a_record_still_running_not_once_it_has_left_running(tmp_path):
    """Keeps T5-18's rule for the new re-take path too, half 1: _renew_gpu_lease_if_active's own gate (an open
    gpu job in gate.ACTIVE_PHASES) decides whether to renew AND, now, whether to re-take -- never one without
    the other. While the job is RUNNING, a restarted loop's tick re-takes what renew() could not refresh; once
    the job has left RUNNING (settled to DONE), the same gate releases the entry rather than kept alive by
    either path -- the second half this record's sibling test proves in isolation, since finishing this
    attempt here would hand the still-open task its own next continuation and reopen the gate for a different
    reason than the one under test."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay_a = _project(tmp_path, "a", lease_file, "project-a")
    lane_a = ControllableGpuLane("gpu_a_running_only")
    with testing.lane(lane_a):
        make_task(lay_a, "A", job={"adapter": "gpu_a_running_only"}, cost_category="local")
        sup_a = Supervisor(lay_a, sleep_s=0)
        sup_a.once()
        assert jobs.read(lay_a, "A.a1")["phase"] == "RUNNING"

        _simulate_restart(lease_file, "project-a")

        sup_a.once()                                            # RUNNING -> re-taken
        assert leases.status(lay_a, "gpu")["live"] != []

        lane_a.done["A.a1"] = True
        sup_a.once()                                             # settle() collects it; reconcile() releases
        assert jobs.read(lay_a, "A.a1")["phase"] == "DONE"
        assert leases.status(lay_a, "gpu")["live"] == []          # left RUNNING: already let go, not kept alive


def test_a_stale_looking_entry_with_no_active_gpu_job_is_not_revived(tmp_path):
    """The other half, isolated from any task or continuation: a project with no gpu job in gate.ACTIVE_PHASES
    at all must not have a stale-looking entry for itself revived by either renew() or the new leases._reclaim()
    fallback -- the active-job gate that guards both in _renew_gpu_lease_if_active() returns before either runs,
    so a ghost entry under a different (dead) pid, as a crash racing a real release might leave one, is simply
    left alone: not live, not re-taken, never re-establishing a hold after the job it covered has ended."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay_a = _project(tmp_path, "a", lease_file, "project-a")
    own_holder = leases.resource_lease(lay_a, "gpu")
    assert own_holder.acquire(wait_s=0) is True              # this process's own real (holder, pid) entry
    holder_hash = json.loads(lease_file.read_text(encoding="utf-8"))["entries"][0]["holder"]
    own_holder.release()

    now = time.time()
    lease_file.write_text(json.dumps({
        "entries": [{"holder": holder_hash, "label": "project-a", "pid": 999999, "create_time": 1.0,
                     "since": now, "renewed": now}],
        "release_requested_by": None, "release_requested_label": None, "release_requested_at": None,
    }), encoding="utf-8")

    sup_a = Supervisor(lay_a, sleep_s=0)
    sup_a.once()                                               # no gpu job at all: neither renewed nor re-taken
    assert leases.status(lay_a, "gpu")["live"] == []


def test_a_retake_leaves_another_projects_pending_release_request_untouched(tmp_path):
    """The re-take must not disturb another project's pending hand-over, and must still succeed -- the job on
    the cards never stopped, only the bookkeeping entry went stale, so the retake cannot be refused just
    because B is already waiting its turn (the exact shape the reviewer's own probe drives: B's loop is
    refused once, which records its wait, *before* A's restart). B's loop has already recorded its own
    release_requested_by/_at (F2) while A's entry was still live; when A's restarted loop then re-takes,
    leases._reclaim() -- unlike acquire()/_try_take() -- never reads or writes release_requested_by/_at at all,
    only A's own entries list, so B's requester-clearing branch in _try_take() (which only fires for "pending ==
    self._holder, or the window has expired") is never reached by this path to begin with: the fields on disk
    are untouched because _reclaim() has no code path that could touch them, not because a check happened to
    refuse first. The request B's loop recorded is not quietly cleared by A's own re-entrance, and A's job
    stays visible to B rather than invisible while B's window runs down."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay_a = _project(tmp_path, "a", lease_file, "project-a", hand_over_timeout_s=60)
    lay_b = _project(tmp_path, "b", lease_file, "project-b", hand_over_timeout_s=60)
    lane_a = ControllableGpuLane("gpu_a_handover")
    with testing.lane(lane_a):
        make_task(lay_a, "A", job={"adapter": "gpu_a_handover"}, cost_category="local")
        sup_a = Supervisor(lay_a, sleep_s=0)
        sup_a.once()
        assert jobs.read(lay_a, "A.a1")["phase"] == "RUNNING"

        waiter_b = leases.resource_lease(lay_b, "gpu")              # b's own wait, recorded while a's entry is live
        assert waiter_b.acquire(wait_s=0) is False
        before = json.loads(lease_file.read_text(encoding="utf-8"))
        requested_by, requested_label, requested_at = (before["release_requested_by"],
                                                         before["release_requested_label"], before["release_requested_at"])
        assert requested_by is not None

        _simulate_restart(lease_file, "project-a")

        sup_a.once()                                                 # a's restarted loop retakes while b's ask stands

        after = json.loads(lease_file.read_text(encoding="utf-8"))
        assert after["release_requested_by"] == requested_by         # b's hand-over request: untouched
        assert after["release_requested_label"] == requested_label
        assert after["release_requested_at"] == requested_at
        assert jobs.read(lay_a, "A.a1")["phase"] == "RUNNING"
        assert leases.status(lay_a, "gpu")["live"] != []              # and the retake still succeeded
        assert leases.holder_label(lay_b, "gpu") == "project-a"       # b's own peek names a, not nobody


def test_a_lone_project_is_unaffected_by_no_lease_configured(tmp_path):
    lay = testing.project(tmp_path / "solo")
    lane = ControllableGpuLane("gpu_solo")
    with testing.lane(lane):
        make_task(lay, "A", job={"adapter": "gpu_solo"}, cost_category="local")
        sup = Supervisor(lay, sleep_s=0)
        sup.once()
        assert lane.submitted == ["A.a1"]
        assert jobs.read(lay, "A.a1")["phase"] == "RUNNING"
