"""A named external resource, declared by the project
and claimed by one job at a time -- the gate serialises two tasks that would otherwise both publish to the same
external target (the recorded loss: two accepted heads lost to another merge, because the gate only ever
coordinated owned paths *inside* the project). The claim is held by the open job record and by nothing else, so
every way a job stops releases it, and a project that declares no resource sees no behaviour change."""
from __future__ import annotations

import json
import sys
import time

import pytest
import yaml

from supervisor import commands, gate, jobs, leases, needs as needs_mod, scheduler, tasks as tasks_mod
from supervisor import why_not_runnable
from supervisor import testing
from supervisor.adapters import REGISTRY, Result
from supervisor.adapters.base import Adapter
from supervisor.adapters.process import ProcessAdapter
from supervisor.loop import Supervisor
from tests.conftest import make_task

PY = sys.executable


def _write_routing(layout, doc):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def _write_templates(layout, templates: dict):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text(yaml.safe_dump({"version": 1, "templates": templates}),
                                                       encoding="utf-8")


def _log(layout):
    if not layout.log.exists():
        return []
    return [json.loads(line) for line in layout.log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _claim_events(layout, name="claim_released"):
    return [e for e in _log(layout) if e.get("event") == name]


def _running_claim(layout, task_id, claim, adapter="process", attempt="a1", kind="delivery"):
    """An open (SUBMITTED) job record of `task_id` carrying `claim`, the same shape `dispatch()` writes."""
    return jobs.prepare(layout, task_id, f"{task_id}.{attempt}", adapter, {"kind": kind}, claim=claim) and \
        jobs.set_phase(layout, f"{task_id}.{attempt}", "SUBMITTED", handle={"pid": 999999, "create_time": 0})


class HoldingLane(Adapter):
    """Stays RUNNING until the test flips `done[attempt_id]`; `collect()` returns `None` (still running) until
    then, so a reconcile() in between never closes the record -- the shape the re-adoption test needs."""

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
        return {"kind": "holding", "attempt_id": spec["attempt_id"]}

    def alive(self, handle):
        return not self.done.get(handle["attempt_id"], False)

    def collect(self, spec, handle):
        if self.alive(handle):
            return None
        return Result(status="COMPLETE")


# ---------------------------------------------------------------- serialisation (gate.check, scheduler.plan)

def test_two_tasks_claiming_one_resource_are_serialised(layout):
    _write_routing(layout, {"resources": {"printer": {}}})
    make_task(layout, "A", needs={"resource": "printer"})
    make_task(layout, "B", needs={"resource": "printer"})
    _running_claim(layout, "A", "printer")
    running = jobs.open_records(layout)
    b = tasks_mod.load(layout, "B")
    reason = gate.check(layout, b, "process", "delivery", running)
    assert reason == "resource 'printer' claimed by A"


def test_a_tasks_own_continuation_is_never_blocked_by_its_own_record(layout):
    _write_routing(layout, {"resources": {"printer": {}}})
    make_task(layout, "A", needs={"resource": "printer"})
    _running_claim(layout, "A", "printer")
    running = jobs.open_records(layout)
    a = tasks_mod.load(layout, "A")
    assert gate.check(layout, a, "process", "delivery",
                      [r for r in running if r["task_id"] != "A"]) is None
    # the same exclusion gate.check itself applies -- a record of A never blocks A, whatever `running` holds
    assert gate.check(layout, a, "process", "delivery", running) is None


def test_the_waiters_reason_names_the_claim_and_its_holder(layout):
    _write_routing(layout, {"resources": {"printer": {}}})
    make_task(layout, "A", needs={"resource": "printer"})
    make_task(layout, "B", needs={"resource": "printer"})
    _running_claim(layout, "A", "printer")
    why = why_not_runnable.why_not_runnable(tasks_mod.load(layout, "B"), tasks_mod.load_all(layout), layout)
    assert why["code"] == "resource_claimed"
    assert "printer" in why["detail"] and "A" in why["detail"]
    assert why["blockers"] == ["A"]


def test_scheduler_plan_admits_only_one_claimant_per_tick(layout):
    _write_routing(layout, {"resources": {"printer": {}}})
    make_task(layout, "A", needs={"resource": "printer"}, job={"adapter": "process", "argv": [PY, "-c", "pass"]})
    make_task(layout, "B", needs={"resource": "printer"}, job={"adapter": "process", "argv": [PY, "-c", "pass"]})
    chosen = scheduler.plan(layout)
    assert len(chosen) == 1          # extra_claims threaded through the same tick refuses the second pick


def test_two_tasks_claiming_one_resource_end_to_end(layout):
    _write_routing(layout, {"resources": {"printer": {}}})
    lane = HoldingLane("fake_hold")
    with testing.lane(lane):
        make_task(layout, "A", job={"adapter": "fake_hold"}, needs={"resource": "printer"})
        make_task(layout, "B", job={"adapter": "fake_hold"}, needs={"resource": "printer"})
        sup = Supervisor(layout, sleep_s=0)
        sup.once()
        recs = jobs.open_records(layout)
        assert len(recs) == 1
        holder, waiter = recs[0]["task_id"], ("B" if recs[0]["task_id"] == "A" else "A")
        assert recs[0]["claim"] == "printer"
        why = why_not_runnable.why_not_runnable(tasks_mod.load(layout, waiter), tasks_mod.load_all(layout), layout)
        assert why["code"] == "resource_claimed" and holder in why["detail"]


# ---------------------------------------------------------------- RF-30, RF-31

def _task_doc(tid, **extra):
    doc = {"id": tid, "phase": 0, "title": tid, "objective": "o", "owner_role": "engineer",
           "artifacts": ["x"], "cost_category": "deterministic"}
    doc.update(extra)
    return doc


def test_rf30_an_undeclared_resource_name_is_refused_at_the_write(layout):
    with pytest.raises(tasks_mod.TaskError, match="printer"):
        tasks_mod.create(layout, _task_doc("A", needs={"resource": "printer"}), actor="owner")


def test_a_declared_resource_round_trips_through_import(layout):
    _write_routing(layout, {"resources": {"printer": {}}})
    make_task(layout, "A", needs={"resource": "printer"})
    assert needs_mod.claim_for(layout, tasks_mod.load(layout, "A"), "delivery") == "printer"


def test_rf31_a_plan_may_not_claim_an_owner_only_resource(layout):
    from tests.test_plans import base_plan
    from supervisor import plans
    _write_routing(layout, {"resources": {"printer": {"may_be_claimed_by": ["owner"]}}})
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "create_task", "task": {
        "id": "T2", "title": "T2", "objective": "o", "owner_role": "engineer", "cost_category": "deterministic",
        "artifacts": ["x"], "needs": {"resource": "printer"}}}])
    _, refusals = plans.validate(doc, layout)
    assert any(r["code"] == "RF-31" for r in refusals), refusals
    assert any("printer" in (r.get("message") or "") for r in refusals)


def test_rf31_a_plan_may_claim_a_resource_opened_to_it(layout):
    from tests.test_plans import base_plan
    from supervisor import plans
    _write_routing(layout, {"resources": {"printer": {"may_be_claimed_by": ["owner", "plan"]}}})
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "create_task", "task": {
        "id": "T2", "title": "T2", "objective": "o", "owner_role": "engineer", "cost_category": "deterministic",
        "artifacts": ["x"], "needs": {"resource": "printer"}}}])
    _, refusals = plans.validate(doc, layout)
    assert not any(r["code"] == "RF-31" for r in refusals), refusals


def test_rf31_a_plan_may_not_select_a_template_claiming_an_owner_only_resource(layout):
    """Section 6: the effective need, so a plan cannot launder the permission through a template it selects --
    the plan itself writes no resource name at all."""
    from tests.test_plans import base_plan
    from supervisor import plans
    _write_routing(layout, {"resources": {"printer": {"may_be_claimed_by": ["owner"]}}})
    _write_templates(layout, {"release": {"adapter": "process", "argv": [PY, "-c", "pass"],
                                          "may_be_set_by": ["plan"], "params": {}, "needs": {"resource": "printer"}}})
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[
        {"op": "create_task", "task": {"id": "T2", "title": "T2", "objective": "o", "owner_role": "engineer",
                                       "cost_category": "deterministic", "artifacts": ["x"]}},
        {"op": "set_job", "task": "T2", "template": "release", "params": {}},
    ])
    _, refusals = plans.validate(doc, layout)
    assert any(r["code"] == "RF-31" for r in refusals), refusals


# ---------------------------------------------------------------- a claim nobody declares costs nothing

def test_a_project_declaring_no_resource_sees_no_behaviour_change(layout):
    make_task(layout, "A")
    task = tasks_mod.load(layout, "A")
    assert needs_mod.claim_for(layout, task, "delivery") is None
    assert needs_mod.resources_declared(layout) == {}
    assert gate.check(layout, task, "process", "delivery", []) is None


def test_claim_is_none_on_a_record_whose_task_declares_none(layout):
    make_task(layout, "A", job={"adapter": "process", "argv": [PY, "-c", "pass"]})
    sup = Supervisor(layout, sleep_s=0)
    rec = sup.dispatch(tasks_mod.load(layout, "A"), "process")
    assert rec["claim"] is None


# ---------------------------------------------------------------- release: every way a claim can stop

def test_claim_released_on_a_normal_close(layout):
    make_task(layout, "A")
    _running_claim(layout, "A", "printer")
    jobs.set_phase(layout, "A.a1", "DONE", result="COMPLETE")
    events = _claim_events(layout)
    assert events and events[-1]["claim"] == "printer" and events[-1]["task"] == "A" and events[-1]["reason"] == "closed"


def test_claim_released_when_the_holder_task_goes_terminal(layout):
    make_task(layout, "A")
    _running_claim(layout, "A", "printer")
    tasks_mod.set_status(layout, "A", "CANCELLED", actor="owner")
    events = _claim_events(layout)
    assert events and events[-1]["reason"] == "terminal"
    assert jobs.read(layout, "A.a1")["phase"] == "DONE"


def test_claim_released_when_the_owner_terminates_the_job(layout):
    make_task(layout, "A")
    _running_claim(layout, "A", "printer")
    out = commands.HANDLERS["agent.terminate"](layout, {"attempt_id": "A.a1"}, "owner")
    assert out.get("terminated") == "A.a1"
    events = _claim_events(layout)
    assert events and events[-1]["reason"] == "cancelled"


def test_claim_released_when_the_holder_died(layout):
    _write_routing(layout, {"resources": {"printer": {}}})
    lane = testing.FakeLane("fake_dead", status="FAILED", error="lost: no result after process exit")
    with testing.lane(lane):
        make_task(layout, "A", job={"adapter": "fake_dead"}, needs={"resource": "printer"})
        sup = Supervisor(layout, sleep_s=0)
        sup.once()
        events = _claim_events(layout)
        assert events and events[-1]["reason"] == "dead"
        rec = jobs.for_task(layout, "A")[-1]
        assert rec["phase"] in ("DONE", "FAILED")


def test_a_loop_restart_with_the_holder_still_running_does_not_release_the_claim(layout):
    _write_routing(layout, {"resources": {"printer": {}}})
    lane = HoldingLane("fake_hold2")
    with testing.lane(lane):
        make_task(layout, "A", job={"adapter": "fake_hold2"}, needs={"resource": "printer"})
        sup = Supervisor(layout, sleep_s=0)
        sup.once()
        attempt_id = jobs.for_task(layout, "A")[-1]["attempt_id"]
        assert jobs.read(layout, attempt_id)["phase"] == "RUNNING"
        # a fresh Supervisor (the "restart") re-adopts the still-running record without touching its claim
        Supervisor(layout, sleep_s=0).once()
        rec = jobs.read(layout, attempt_id)
        assert rec["phase"] == "RUNNING" and rec["claim"] == "printer"
        assert _claim_events(layout) == []


_SLEEP_ARGV = [PY, "-c", "import time; time.sleep(30)"]


class _FastCapProcess(ProcessAdapter):
    name = "process"

    def prepare(self, layout, task, attempt_id):
        spec = super().prepare(layout, task, attempt_id)
        spec["timeout_s"] = 1
        return spec


def test_a_cap_stop_releases_the_claim_and_the_continuation_re_takes_it(layout, monkeypatch):
    """§2.4's last residual, stated as behaviour rather than left to be discovered: a model-lane cap-stop and a
    resumable process cap-stop both close the holding record (releasing the claim), and the continuation then
    contends for it again like any other job -- here, with nothing else contending, it re-takes it."""
    _write_routing(layout, {"resources": {"printer": {}}})
    _write_templates(layout, {"t": {"adapter": "process", "argv": _SLEEP_ARGV, "may_be_set_by": ["owner"],
                                     "params": {}, "resumable": True}})
    monkeypatch.setitem(REGISTRY, "process", _FastCapProcess())
    make_task(layout, "T", cost_category="deterministic", status="READY",
              job={"adapter": "process", "template": "t", "params": {}, "argv": _SLEEP_ARGV},
              needs={"resumable": True, "resource": "printer"})
    sup = Supervisor(layout, sleep_s=0)
    end = time.time() + 20
    while time.time() < end and not any(r.get("continued") for r in jobs.for_task(layout, "T")):
        sup.once()
        time.sleep(0.2)
    recs = jobs.for_task(layout, "T")
    first = next(r for r in recs if r.get("seq") == 1)
    assert first["phase"] == "DONE" and first.get("continued")
    events = _claim_events(layout)
    assert events and events[-1]["claim"] == "printer" and events[-1]["task"] == "T" and events[-1]["reason"] == "closed"
    reopened = [r for r in jobs.open_records(layout) if r["task_id"] == "T"]
    assert reopened and reopened[-1]["claim"] == "printer"            # the continuation contended for it and took it
    for r in jobs.open_records(layout):
        if r.get("handle"):
            REGISTRY["process"].terminate(r["handle"])


# ---------------------------------------------------------------- the machine-wide lease (section 5.4)

def test_lease_name_for_the_three_disjoint_cases(layout):
    _write_routing(layout, {"resources": {"printer": {"lease": {"path": str(layout.root / "shared" / "p.lease")}}}})
    assert leases.lease_name_for(layout, False, None) is None
    assert leases.lease_name_for(layout, True, None) == "gpu"
    assert leases.lease_name_for(layout, False, "printer") == "printer"


def test_a_slot_holder_carrying_a_leaseless_claim_still_takes_gpu(layout):
    _write_routing(layout, {"resources": {"printer": {}}})       # no lease: block under printer
    assert leases.lease_name_for(layout, True, "printer") == "gpu"


def test_a_leaseless_claim_on_a_job_holding_no_slot_takes_no_lease(layout):
    _write_routing(layout, {"resources": {"printer": {}}})       # no lease: block under printer
    assert leases.lease_name_for(layout, False, "printer") is None


def test_a_slot_holder_carrying_a_claim_with_a_lease_block_takes_the_claims_name_not_gpu(layout):
    """Section 5.4's stated cost: one record, one lease, and the claim -- the narrower, newer statement -- wins
    over the slot's own literal even when the record also holds the slot."""
    _write_routing(layout, {"resources": {"printer": {"lease": {"path": str(layout.root / "shared" / "p.lease")}}}})
    assert leases.lease_name_for(layout, True, "printer") == "printer"


def test_a_live_gpu_section_with_only_plugin_keys_loads_with_no_refusal(layout):
    _write_routing(layout, {"resources": {"gpu": {"single_job": True, "usable_vram_floor_mb": 1000, "devices": [0]}}})
    assert needs_mod.resources_declared(layout)["gpu"]["single_job"] is True
    assert leases.lease_name_for(layout, True, None) == "gpu"       # the slot's own case, untouched


def _claim_lease_project(tmp_path, name, lease_path, resource="printer", label=None, stale_after_s=None):
    lease_cfg = {"path": str(lease_path), "label": label or name}
    if stale_after_s is not None:
        lease_cfg["stale_after_s"] = stale_after_s
    routing = {"resources": {resource: {"lease": lease_cfg}}}
    return testing.project(tmp_path / name, routing=routing)


def test_the_plan_time_peek_and_the_dispatch_time_sweep_agree_on_one_claims_lease_name(tmp_path):
    """Section 5.4, section 9 step 6: `gate.lease_blocked`'s plan-time peek (before any record exists) and
    `Supervisor._active_lease_names`'s dispatch-time sweep (the record's own `gpu`/`claim` fields) both resolve
    through the one `leases.lease_name_for`, so the two can never disagree about which lease a claim-carrying job
    takes -- no stand-in record is built anywhere to make the peek callable."""
    lease_file = tmp_path / "shared" / "printer.lease"
    lay_a = _claim_lease_project(tmp_path, "a", lease_file, label="project-a")
    lane_a = HoldingLane("claim_lease_peek")
    with testing.lane(lane_a):
        make_task(lay_a, "A", job={"adapter": "claim_lease_peek"}, needs={"resource": "printer"})
        task = tasks_mod.load(lay_a, "A")
        peeked = leases.lease_name_for(lay_a, gate.holds_resource(lay_a, task, "claim_lease_peek", "delivery"),
                                       needs_mod.claim_for(lay_a, task, "delivery"))
        assert peeked == "printer"
        assert gate.lease_blocked(lay_a, task, "claim_lease_peek", "delivery") is None     # free: nobody holds it yet

        sup = Supervisor(lay_a, sleep_s=0)
        sup.once()
        rec = jobs.read(lay_a, "A.a1.s1")
        assert rec["phase"] == "RUNNING" and rec["claim"] == "printer"
        swept = leases.lease_name_for(lay_a, bool(rec.get("gpu")), rec.get("claim"))
        assert swept == peeked == "printer"
        assert sup._active_lease_names() == {"printer"}


def test_a_job_carrying_a_claims_lease_is_peeked_acquired_and_released_across_projects(tmp_path):
    """The same question the gpu-lease suite already answers for the slot, now answered for a claim whose
    declaration carries a `lease:` block: two projects neither can see each other's job records, so only the
    machine-wide lease file serialises them."""
    lease_file = tmp_path / "shared" / "printer.lease"
    lay_a = _claim_lease_project(tmp_path, "a", lease_file, label="project-a")
    lay_b = _claim_lease_project(tmp_path, "b", lease_file, label="project-b")
    lane_a, lane_b = HoldingLane("claim_lease_a"), HoldingLane("claim_lease_b")
    with testing.lane(lane_a), testing.lane(lane_b):
        # artifacts=[]: nothing declared, so a COMPLETE result closes the record outright instead of the loop's
        # ordinary missing-deliverable revision -- irrelevant to this test, which is about the lease, not a delivery.
        make_task(lay_a, "A", job={"adapter": "claim_lease_a"}, needs={"resource": "printer"}, artifacts=[])
        make_task(lay_b, "B", job={"adapter": "claim_lease_b"}, needs={"resource": "printer"}, artifacts=[])
        sup_a, sup_b = Supervisor(lay_a, sleep_s=0), Supervisor(lay_b, sleep_s=0)

        sup_a.once()
        assert lane_a.submitted == ["A.a1.s1"]
        assert jobs.read(lay_a, "A.a1.s1")["phase"] == "RUNNING"

        sup_b.once()
        assert lane_b.submitted == []                              # b is held: a holds the claim's lease, cross-project
        assert jobs.for_task(lay_b, "B") == []

        lane_a.done["A.a1.s1"] = True
        sup_a.once()                                                # settle() collects it; reconcile() releases
        assert jobs.read(lay_a, "A.a1.s1")["phase"] == "DONE"

        sup_b.once()
        assert lane_b.submitted == ["B.a1.s1"]                        # the lease is free: b may launch now
        assert jobs.read(lay_b, "B.a1.s1")["phase"] == "RUNNING"


def test_a_claims_lease_entry_is_renewed_so_it_outlives_stale_after_s(tmp_path):
    lease_file = tmp_path / "shared" / "printer.lease"
    stale_after_s = 1.0
    lay_a = _claim_lease_project(tmp_path, "a", lease_file, label="project-a", stale_after_s=stale_after_s)
    lay_b = _claim_lease_project(tmp_path, "b", lease_file, label="project-b", stale_after_s=stale_after_s)
    lane_a = HoldingLane("claim_lease_renew")
    with testing.lane(lane_a):
        make_task(lay_a, "A", job={"adapter": "claim_lease_renew"}, needs={"resource": "printer"}, artifacts=[])
        sup_a = Supervisor(lay_a, sleep_s=0)
        sup_a.once()
        assert jobs.read(lay_a, "A.a1.s1")["phase"] == "RUNNING"

        deadline = time.monotonic() + stale_after_s * 1.6
        while time.monotonic() < deadline:
            time.sleep(stale_after_s / 4)
            sup_a.once()                                            # each reconcile() must renew the live claim lease

        assert leases.holder_label(lay_b, "printer") == "project-a"   # b's own peek still sees a's hold

        lane_a.done["A.a1.s1"] = True
        sup_a.once()                                                 # settle() collects it; reconcile() releases
        assert leases.status(lay_a, "printer")["live"] == []          # left RUNNING: let go, not kept renewed


# ---------------------------------------------------------------- doctor

def test_doctor_lists_each_declared_resource_its_holder_its_waiter_and_a_misconfigured_template(layout):
    """Section 5, §9 step 6: `doctor` names the waiter too -- the same `claim_for`-against-open-records read
    `why_not_runnable` makes, computed here with no task to ask it for."""
    from supervisor import doctor
    _write_routing(layout, {"resources": {"printer": {"may_be_claimed_by": ["owner"]}}})
    _write_templates(layout, {"release": {"adapter": "process", "argv": [PY, "-c", "pass"],
                                          "may_be_set_by": ["plan"], "params": {}, "needs": {"resource": "printer"}}})
    make_task(layout, "A", needs={"resource": "printer"})
    make_task(layout, "B", needs={"resource": "printer"})      # not dispatched: no open record of its own
    _running_claim(layout, "A", "printer")
    check = doctor.check_resource_claims(layout)
    assert check.ok
    assert "printer" in check.detail and "held by A" in check.detail
    assert "waited on by B" in check.detail
    assert "release" in check.detail and "plan" in check.detail


def test_doctor_names_no_waiter_when_nobody_else_claims_the_resource(layout):
    from supervisor import doctor
    _write_routing(layout, {"resources": {"printer": {}}})
    make_task(layout, "A", needs={"resource": "printer"})
    _running_claim(layout, "A", "printer")
    check = doctor.check_resource_claims(layout)
    assert check.ok and "held by A" in check.detail and "waited on by" not in check.detail


def test_doctor_reports_no_resources_declared_when_the_project_opens_none(layout):
    from supervisor import doctor
    check = doctor.check_resource_claims(layout)
    assert check.ok and "no resources declared" in check.detail
