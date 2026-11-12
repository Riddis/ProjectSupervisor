"""The GPU lease (supervisor.leases) wired into the ordinary launch path (D-013 R4): `supervisor.gate` refuses to
launch a resource-holding job while another project's lease entry is live, and the loop takes the lease for real
right before `submit()` and lets go of it once none of this project's jobs holds the resource slot any more.
Two project Layouts, pointed at the same lease file, stand in for "two projects on one machine"."""
from __future__ import annotations

import json
import time
from pathlib import Path

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


def _project(tmp_path, name, lease_path, label, stale_after_s=None):
    lease_cfg = {"path": str(lease_path), "label": label}
    if stale_after_s is not None:
        lease_cfg["stale_after_s"] = stale_after_s
    routing = {"limits": {"max_resource_jobs": 1}, "resources": {"gpu": {"lease": lease_cfg}}}
    return testing.project(tmp_path / name, routing=routing)


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


def test_a_lone_project_is_unaffected_by_no_lease_configured(tmp_path):
    lay = testing.project(tmp_path / "solo")
    lane = ControllableGpuLane("gpu_solo")
    with testing.lane(lane):
        make_task(lay, "A", job={"adapter": "gpu_solo"}, cost_category="local")
        sup = Supervisor(lay, sleep_s=0)
        sup.once()
        assert lane.submitted == ["A.a1"]
        assert jobs.read(lay, "A.a1")["phase"] == "RUNNING"
