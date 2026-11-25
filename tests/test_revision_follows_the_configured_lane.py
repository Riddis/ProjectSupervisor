import yaml

from supervisor import escalation, scheduler, tasks
from tests.conftest import make_task


class FakeSupervisor:
    """Records what escalation would have dispatched without launching anything real."""

    def __init__(self, layout):
        self.layout = layout
        self.calls = []

    def dispatch(self, task, adapter_name, note="dispatch", **kw):
        self.calls.append({"task": task, "adapter": adapter_name, "note": note, **kw})
        return {"attempt_id": f"{task.id}.fake{len(self.calls)}"}


def _routed_to_another_lane(layout, senior_model="opus"):
    """routing.yaml sends the engineer role to codex_cli, distinct from the claude cost category's own
    default lane (claude_cli) — the shape R-16's probe reproduced it with."""
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(
        {"roles": {"engineer": {"adapter": "codex_cli", "senior_model": senior_model}},
         "cost_lanes": {"claude": "claude_cli"}}))


def test_a_role_routed_to_another_lane_gets_its_revision_there(layout):
    """R-16: dispatch_revision called scheduler.adapter_name(task) with no layout, so routing.yaml's
    roles.<role>.adapter never reached an automatic revision - only the role's first, scheduled delivery
    (scheduler.plan / Supervisor.dispatch_delivery, which do pass the layout) ever read it. Every lookup here
    falls back to the cost category's own default lane (claude_cli) instead."""
    _routed_to_another_lane(layout)
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    outcome = escalation.record_failure(sup, "T", "verification", "missing artifacts: out.txt")
    assert outcome["action"] == "retry"
    assert len(sup.calls) == 1
    assert sup.calls[0]["adapter"] == "codex_cli"


def test_the_senior_revision_of_a_routed_role_stays_on_its_own_lane(layout):
    _routed_to_another_lane(layout)
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    escalation.record_failure(sup, "T", "verification", "missing artifacts: out.txt")
    outcome = escalation.record_failure(sup, "T", "verification", "a completely different problem: timeout in step 2")
    assert outcome["action"] == "retry" and outcome["senior"] is True
    call = sup.calls[1]
    assert call["adapter"] == "codex_cli"
    job = scheduler.job_spec(call["task"])
    assert job.get("model") == "opus"


def test_every_automatic_revision_records_why_it_picked_its_lane_and_model(layout):
    """The second half of R-16: a model change (or the deliberate absence of one) used to leave no record at
    all on the job it changed."""
    _routed_to_another_lane(layout)
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    escalation.record_failure(sup, "T", "verification", "missing artifacts: out.txt")
    outcome = escalation.record_failure(sup, "T", "verification", "a completely different problem: timeout in step 2")
    assert outcome["senior"] is True
    first_reason = sup.calls[0].get("reason") or ""
    second_reason = sup.calls[1].get("reason") or ""
    assert "codex_cli" in first_reason and "base model" in first_reason
    assert "codex_cli" in second_reason and "senior model" in second_reason
