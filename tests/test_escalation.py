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
    """verification failures leave the task FAILED (tasks.advance()); `_apply`'s escalate branch only moves a
    task off IN_PROGRESS, never a FAILED one (that is for the owner's own Hold to do, R-18) — a note is enough,
    since FAILED already stops automatic work."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.set_status(layout, "T", "FAILED")
    sup = FakeSupervisor(layout)
    escalation.record_failure(sup, "T", "verification", "x")
    outcome = escalation.record_failure(sup, "T", "verification", "x")
    assert outcome["action"] == "escalate"
    assert tasks.load(layout, "T").status == TaskStatus.FAILED


# ---- R-14: retry budgets split by cause (ATTEMPT_MODEL.md section 4.2, step 10 of "Order of work") ----
#
# One counter used to charge infrastructure failures and content revisions alike: four infrastructure
# failures could spend the budget a content review needed, and every blocked review round cost a
# diagnosis session that only repeated what the review had already said. The fix splits one counter into
# three, by cause (`substance`, `infra`, `stall`), plus a `total` across all of them, with the limits and
# the cause table as data in config/routing.yaml, not constants.

def _budget_routing(layout, limits=None, causes=None):
    layout.config.mkdir(parents=True, exist_ok=True)
    budget = {}
    if limits:
        budget["limits"] = limits
    if causes:
        budget["causes"] = causes
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({"escalation": {"budget": budget}}))


def test_four_infrastructure_failures_do_not_spend_the_content_review_budget(layout):
    """The recorded defect (R-14): T2-08's first content review arrived after four verifier-condition
    failures had already spent the one shared budget. With the budget split by cause, four `infra`
    failures leave the `substance` counter untouched, so the first content review still gets its own,
    full budget."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    # four distinct quoting failures — normalize() collapses digits, so these differ by word, not by number
    details = ["quoting failure in the basetemp argument", "quoting failure in the cwd argument",
              "quoting failure in the interpreter argument", "quoting failure in the allow-list match"]
    for detail in details:
        outcome = escalation.record_failure(sup, "T", "verification", detail, reason="infra")
        assert outcome["action"] == "retry"
    content_review = escalation.record_failure(sup, "T", "review", "finding: the brief is missing a section")
    assert content_review["action"] == "retry"
    assert not sup.diagnoses


def test_revise_with_new_findings_continues_without_diagnosis_while_substance_remains(layout):
    """Trigger 2's own boundary, from the other side: as long as `substance` is not yet spent, a REVISE
    round with a new finding each time is a plain retry — the counter is spent, the next brief is the
    reviewer's findings, and no diagnosis session is raised to decide what the review already said."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    first = escalation.record_failure(sup, "T", "review", "finding: missing error handling")
    second = escalation.record_failure(sup, "T", "review", "finding: missing a test for the edge case")
    assert first["action"] == "retry" and second["action"] == "retry"
    assert not sup.diagnoses


def test_substance_exhausted_escalates_even_with_new_findings(layout):
    """Trigger 2: once `substance` (default budget 2) is spent, the next REVISE escalates even though its
    finding is new — "this contract is not landing" is more likely than "one more round will land it"."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    escalation.record_failure(sup, "T", "review", "finding: missing error handling")
    escalation.record_failure(sup, "T", "review", "finding: missing a test for the edge case")
    third = escalation.record_failure(sup, "T", "review", "finding: yet another, brand new issue")
    assert third["action"] == "escalate"


def test_a_repeated_finding_signature_escalates_at_any_count(layout):
    """Trigger 1: the same defect twice is the signal that another round will not fix it — it escalates
    immediately, however much of the budget remains (here, on the very first retry)."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    escalation.record_failure(sup, "T", "review", "finding: missing error handling")
    repeat = escalation.record_failure(sup, "T", "review", "finding: missing error handling")
    assert repeat["action"] == "escalate"


def test_a_reject_escalates_immediately_regardless_of_the_counters(layout):
    """Trigger 4: a REJECT is a verdict on the approach, not a list of repairs — it escalates at once even
    on the very first round, with the whole budget still unspent."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    outcome = escalation.record_failure(sup, "T", "review", "verdict REJECT: wrong approach entirely", immediate=True)
    assert outcome["action"] == "escalate" and len(sup.calls) == 0


def test_total_budget_exhausted_escalates_across_causes(layout):
    """Trigger 3: whatever the mix of causes, a `total` cap bounds the whole chain — set low here so it is
    reached before any single cause's own (much higher) limit would be."""
    _budget_routing(layout, limits={"substance": 100, "infra": 100, "stall": 100, "total": 3})
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    escalation.record_failure(sup, "T", "verification", "condition one", reason="infra")
    escalation.record_failure(sup, "T", "stalled", "stop one")
    escalation.record_failure(sup, "T", "review", "finding: something distinct")
    fourth = escalation.record_failure(sup, "T", "worker_failed", "crashed again, distinctly")
    assert fourth["action"] == "escalate"


def test_escalation_event_keys_are_never_rekeyed_by_the_budget_split(layout):
    """The event key stays the id of the job that failed (or the stall key) — durable, and the replay key
    for every decision already recorded. Splitting one counter into three must not touch it."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = FakeSupervisor(layout)
    escalation.record_failure(sup, "T", "verification", "problem one", event="T.a1", reason="infra")
    escalation.record_failure(sup, "T", "verification", "problem one", event="T.a1", reason="infra")  # replay
    state = escalation._load(layout, "T")
    assert list(state["events"].keys()) == ["T.a1"]
