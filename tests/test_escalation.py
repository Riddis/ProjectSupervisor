import yaml

from supervisor import escalation, jobs as jobs_mod, tasks
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


class FakeSupervisor:
    """Records what escalation would have dispatched without launching anything real."""

    def __init__(self, layout):
        self.layout = layout
        self.calls = []
        self.diagnoses = []

    def dispatch(self, task, adapter_name, note="dispatch", **kw):
        if kw.get("kind") == "diagnosis":            # the exception route's job, counted separately
            self.diagnoses.append({"task": task, "adapter": adapter_name, "note": note, **kw})
            return {"attempt_id": f"{task.id}.diag{len(self.diagnoses)}"}
        self.calls.append({"task": task, "adapter": adapter_name, "note": note})
        return {"attempt_id": f"{task.id}.fake"}


def _senior_routing(layout, senior_model="opus"):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(
        {"roles": {"engineer": {"adapter": "claude_cli", "model": "sonnet", "senior_model": senior_model}}}))


def test_normalize_collapses_hashes_and_numbers():
    a = escalation.normalize("missing artifacts: out/9f8e7d6c5b4a3210.txt (attempt 1)")
    b = escalation.normalize("missing artifacts: out/aaaaaaaaaaaaaaaa.txt (attempt 2)")
    assert a == b


def test_signature_is_stable_and_order_independent_of_whitespace():
    assert escalation.signature("a  b\tc") == escalation.signature("a b c")


def test_open_issue_allocates_sequential_ids(layout):
    first = escalation.open_issue(layout, "first problem", "high", "engineer", "detail 1")
    second = escalation.open_issue(layout, "second problem", "high", "engineer", "detail 2")
    assert first["id"] == "I-001" and second["id"] == "I-002"
    assert (layout.issues / "I-001.yaml").exists()


def test_first_failure_retries_on_the_normal_model(layout):
    _senior_routing(layout)
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    outcome = escalation.record_failure(sup, "T", "verification", "missing artifacts: out.txt")
    assert outcome == {"action": "retry", "revision": 1, "senior": False, "signature": outcome["signature"]}
    assert len(sup.calls) == 1
    job = (sup.calls[0]["task"].model_extra or {}).get("job") or {}
    assert job.get("model") != "opus" and "Revision required" in job.get("prompt", "")


def test_second_revision_of_a_distinct_failure_uses_the_senior_model(layout):
    _senior_routing(layout)
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    escalation.record_failure(sup, "T", "verification", "missing artifacts: out.txt")
    outcome = escalation.record_failure(sup, "T", "verification", "a completely different problem: timeout in step 2")
    assert outcome["action"] == "retry" and outcome["senior"] is True
    job = (sup.calls[1]["task"].model_extra or {}).get("job") or {}
    assert job.get("model") == "opus"


def test_same_signature_twice_escalates_and_opens_an_issue(layout):
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    escalation.record_failure(sup, "T", "verification", "missing artifacts: out.txt")
    outcome = escalation.record_failure(sup, "T", "verification", "missing artifacts: out.txt")
    assert outcome["action"] == "escalate" and outcome["issue"] == "I-001"
    assert len(sup.calls) == 1                             # no second dispatch — retries stopped
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED
    assert (layout.issues / "I-001.yaml").exists()


def test_revision_budget_exhausted_escalates_even_with_distinct_signatures(layout):
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    escalation.record_failure(sup, "T", "verification", "problem one")
    escalation.record_failure(sup, "T", "verification", "problem two")
    outcome = escalation.record_failure(sup, "T", "verification", "problem three")
    assert outcome["action"] == "escalate" and len(sup.calls) == 2


def test_replay_ignores_a_sibling_review_attempt_when_checking_whether_the_revision_already_dispatched(layout):
    """a crash between writing the decision and dispatching it replays through
    _replay(); a sibling REVIEW attempt numbered higher than the failing delivery must not be mistaken for the
    revision having already gone out — only a later DELIVERY attempt proves that (dispatch_revision's own guard
    already applies this filter; _replay must apply the same one)."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    outcome = escalation.record_failure(sup, "T", "verification", "missing artifacts: out.txt", event="T.a1")
    assert outcome["action"] == "retry" and len(sup.calls) == 1
    # simulate: the crash happened before the dispatch above was durable; meanwhile a review job (not the
    # revision) was queued for the same task and given a higher attempt number
    jobs_mod.prepare(layout, "T", "T.a2", "codex_cli", {"kind": "review", "run_dir": str(layout.runs / "T" / "T.a2")})
    replay = escalation.record_failure(sup, "T", "verification", "missing artifacts: out.txt", event="T.a1")
    assert replay["action"] == "retry" and len(sup.calls) == 2    # really re-dispatched, not assumed-already-sent


def test_escalate_on_a_failed_task_adds_a_note_instead_of_an_illegal_transition(layout):
    """verification failures leave the task FAILED (tasks.advance()); FAILED -> BLOCKED is not an allowed manual
    transition, so escalation must not attempt it — a note is enough, since FAILED already stops automatic work."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.set_status(layout, "T", "FAILED")
    sup = FakeSupervisor(layout)
    escalation.record_failure(sup, "T", "verification", "x")
    outcome = escalation.record_failure(sup, "T", "verification", "x")
    assert outcome["action"] == "escalate"
    assert tasks.load(layout, "T").status == TaskStatus.FAILED
