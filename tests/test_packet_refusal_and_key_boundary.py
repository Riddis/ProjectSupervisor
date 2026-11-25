"""The key-shaped secret patterns gained a left boundary (a match can no longer start mid-word), and a
`PolicyViolation` raised while building one task's own packet (revision, review or diagnosis) is now caught at
the dispatch boundary that raised it instead of propagating out of `settle()` into the loop's own error budget.

Every example string below is synthetic and neutral - no project name, task id or real credential, matching the
one this suite's own `tests/test_policy.py` already uses for the same purpose."""
import pytest

from supervisor import diagnosis, escalation, packets, policy, review, tasks
from supervisor import inbox as inbox_mod
from supervisor import jobs as jobs_mod
from supervisor.adapters import REGISTRY
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_policy_hardening import break_listing, write_secret

# A genuine openai-shaped value (not a real key): used both bare (must still be refused) and glued onto an
# ordinary hyphenated word (must NOT be refused once the pattern has a left boundary).
OPENAI_SHAPE = "sk-" + "a" * 30
# "outcome-sk-aaaa...a": the incident's own shape - a hyphenated identifier whose second half happens to read
# like an openai key. Not a credential; must never be refused.
MIDWORD_OPENAI = "outcome-" + OPENAI_SHAPE
# A genuine anthropic-shaped value (test_policy.py's own SHAPED_KEY) - used to exercise the *containment* path
# with a real match, independently of the mid-word fix (which does not, and must not, affect this one).
REAL_SECRET = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8" * 2

# (pattern name, a real shape, a mid-word construction that glues the same shape onto an ordinary word with no
# separator or with a hyphen) - one row per pattern this task bounds. Each mid-word string is "preceded by a
# word character" at the exact point the shape begins, which is the hazard a left boundary closes.
PATTERNS = [
    ("anthropic_api_key", "sk-ant-" + "A1b2C3d4E5f6G7h8" * 2, "desk-ant-" + "A1b2C3d4E5f6G7h8" * 2),
    ("openai_api_key", OPENAI_SHAPE, MIDWORD_OPENAI),
    ("github_token", "ghp_" + "A" * 24, "weighp_" + "A" * 24),
    ("slack_token", "xoxb-" + "1" * 14, "paradoxoxb-" + "1" * 14),
    ("aws_access_key_id", "AKIA" + "1234567890ABCDEF", "WEAKIA" + "1234567890ABCDEF"),
    ("google_api_key", "AIza" + "x" * 35, "dom" + "AIza" + "x" * 35),
    ("meta_graph_token", "EAA" + "B" * 24, "overEAA" + "B" * 24),
    ("jwt", "eyJ" + "A" * 12 + "." + "B" * 12 + "." + "C" * 12, "obeyJ" + "A" * 12 + "." + "B" * 12 + "." + "C" * 12),
]

# Contexts in which a real shape must still be found and redacted (the actions list's own table): line start,
# after a space, a quote, a backtick, `=`, `:`, `(`, and the two compound forms.
REAL_CONTEXTS = [
    "{shape}",
    "token {shape}",
    '"{shape}"',
    "`{shape}`",
    "KEY={shape}",
    "key: {shape}",
    "({shape})",
    "API_KEY={shape}",
    "Authorization: Bearer {shape}",
]


@pytest.mark.parametrize("name, real_shape, midword", PATTERNS)
def test_pattern_has_a_left_boundary_but_still_catches_every_real_shape(layout, name, real_shape, midword):
    patterns = dict(policy.secret_patterns(layout))
    pattern = patterns[name]
    for ctx in REAL_CONTEXTS:
        text = ctx.format(shape=real_shape)
        assert pattern.search(text), f"{name} must still match its real shape in {ctx!r}"
    assert not pattern.search(midword), (
        f"{name} must not match {midword!r}: the shape starts mid-word (preceded by a letter, digit, "
        "`_` or `-`), which a left boundary refuses")


@pytest.mark.parametrize("name, real_shape, midword", PATTERNS)
def test_find_secrets_no_longer_flags_the_mid_word_false_positive(layout, name, real_shape, midword):
    """The same check at the level `assert_packet_clean` actually calls (`find_secrets`/`_find_in`), not just
    the raw compiled pattern - the regression as a caller of policy.py would see it."""
    assert policy.find_secrets(midword, layout) == []
    assert f"pattern/{name}" in policy.find_secrets(real_shape, layout)


def _revision_task(layout):
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    return tasks.load(layout, "T")


# ---------------------------------------------------------------------------- regression (a): the false positive

def test_a_hyphenated_false_positive_in_a_revision_detail_is_no_longer_refused(layout):
    """The incident, reproduced at the packet level that broke: a REVISE finding whose text happens to contain
    a hyphenated word shaped like `sk-` plus 24+ word characters must not make `revision_brief` refuse the
    packet - it is not a credential. At the start commit (floor patterns with no left boundary) this raises
    `policy.PolicyViolation`; after the fix it returns the text unchanged, finding text included verbatim."""
    task = _revision_task(layout)
    detail = f"code_review (REVISE): acceptance criterion unmet, see the {MIDWORD_OPENAI} field"
    text = packets.revision_brief(task, "prior brief text", "review", detail)
    assert MIDWORD_OPENAI in text


def test_a_genuine_key_shape_in_a_revision_detail_is_still_refused(layout):
    """The control for the test above: bounding the pattern must not blind it to a real match - a value that
    really is shaped like a key, however it got into a review finding, still refuses the packet."""
    task = _revision_task(layout)
    detail = f"code_review (REVISE): leaked value found in logs: {REAL_SECRET}"
    with pytest.raises(policy.PolicyViolation):
        packets.revision_brief(task, "prior brief text", "review", detail)


# ---------------------------------------------------------------------------- regression (b): containment

class FakeSupervisor:
    """escalation.dispatch_revision's own dispatch call, recorded without launching anything real - same shape
    as tests/test_escalation.py's own FakeSupervisor."""

    def __init__(self, layout):
        self.layout = layout
        self.calls = []

    def dispatch(self, task, adapter_name, note="dispatch", **kw):
        self.calls.append({"task": task, "adapter": adapter_name, "note": note})
        return {"attempt_id": f"{task.id}.fake"}


def test_a_refused_revision_packet_is_contained_to_its_own_task(layout):
    """At the start commit this raises `PolicyViolation` out of `escalation.record_failure` (through `_apply`
    -> `dispatch_revision` -> `packets.revision_brief`); after the fix it returns a normal "refused" outcome,
    never dispatches the revision, leaves the task BLOCKED (not retried - a retry would rebuild the identical
    packet from the identical evidence and refuse again), and the violation's own message - never the matched
    text - is what ends up in the note, the log and the owner item."""
    _revision_task(layout)
    sup = FakeSupervisor(layout)
    detail = f"code_review (REVISE): leaked value found in logs: {REAL_SECRET}"
    outcome = escalation.record_failure(sup, "T", "review", detail, event="T.a1")
    assert outcome["action"] == "refused"
    assert sup.calls == []                                  # the revision was never dispatched
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.BLOCKED
    assert REAL_SECRET not in "\n".join(h.get("note", "") for h in t.history)
    log_text = (layout.log).read_text(encoding="utf-8")
    assert REAL_SECRET not in log_text
    assert "packet_refused" in log_text
    assert outcome.get("inbox")                             # routed to the owner, per the existing routing


def test_a_refused_review_packet_does_not_stop_the_loop_serving_another_task(layout, monkeypatch):
    """The loop-level claim: in one `settle()` turn, task A's completed review carries a genuine key-shaped
    value (REAL_SECRET - containment must hold for a real match too, not only the mid-word false positive the
    other tests in this file cover; using the mid-word text here would, correctly, no longer refuse at all
    after the pattern fix, which would test nothing). Before containment this raised `PolicyViolation` out of
    `settle()`, tripping the loop-error breaker after enough repeats; task B is a plain python job with nothing
    wrong with it. After the fix, settle() finishes both without raising, A ends up BLOCKED/FAILED (never
    retried, never actually dispatching a revision), and B is unaffected."""
    from supervisor.adapters import python as pyad

    @pyad.job("refusal_test_write_artifact")
    def _write(root, run_dir, **_):
        (root / "b.txt").write_text("ok")
        return {"status": "COMPLETE", "artifacts": ["b.txt"], "notes": "wrote b.txt"}

    make_task(layout, "A", reviewer=("code_review",))
    tasks.set_status(layout, "A", "IN_PROGRESS")
    tasks.record_delivery(layout, "A", "d" * 64, actor="test", paths=["A.txt"])
    tasks.record_verification(layout, "A", "d" * 64, True, detail="ok")
    tasks.advance(layout, "A")                               # -> WAITING_FOR_REVIEW
    digest = tasks.load(layout, "A").delivery.digest

    make_task(layout, "B")
    def fn(doc):
        doc["job"] = {"adapter": "python", "fn": "refusal_test_write_artifact"}
        doc["artifacts"] = ["b.txt"]
    tasks._update(layout, "B", fn)

    attempt_id = "A.a2"
    run_dir = layout.runs / "A" / attempt_id
    run_dir.mkdir(parents=True, exist_ok=True)
    jobs_mod.prepare(layout, "A", attempt_id, "claude_cli", {"kind": "review", "run_dir": str(run_dir)},
                     review_role="code_review", review_digest=digest)
    jobs_mod.set_phase(layout, attempt_id, "RUNNING")
    hazardous = {"kind": "review", "schema_version": 1, "verdict": "REVISE",
                "findings": [{"category": "requirement", "blocking": True,
                             "message": f"leaked value found in logs: {REAL_SECRET}", "evidence": "n/a"}],
                "summary": "verdict REVISE"}
    monkeypatch.setattr(REGISTRY["claude_cli"], "collect",
                        lambda spec, handle: Result(status="COMPLETE", raw=hazardous))

    sup = Supervisor(layout, sleep_s=0)
    sup.once()                                              # schedules B's delivery AND settles A's review
    assert sup.errors == []                                 # the loop-error breaker was never charged
    assert jobs_mod.read(layout, attempt_id)["phase"] == "DONE"   # A's review was not left open to re-ingest
    deadline_task = tasks.load(layout, "A")
    assert deadline_task.status in (TaskStatus.BLOCKED, TaskStatus.FAILED)
    for _ in range(5):
        if tasks.load(layout, "B").status == TaskStatus.COMPLETE:
            break
        sup.once()
    assert tasks.load(layout, "B").status == TaskStatus.COMPLETE   # unaffected by A's refusal


# -------------------------------------------------------- regression (b), the other three refusal boundaries
# Round 2: the review above enters through `review.ingest` -> `escalation`, i.e. the REVISION boundary. These
# cover the three that reach `packets.review_brief` / `diagnosis.brief` themselves - a fresh review dispatch, a
# diagnosis dispatch, and the continuation rebuild of either - each of which refuses on its own text and each of
# which repeated every turn when the refusal did not take the task off the path it was on.

DIGEST = "d" * 64
# An owner's note to reviewers that happens to quote a key-shaped value: ordinary packet text (`review_brief`
# -> `_review_note_block`), screened like all of it, and the shortest realistic way to make the REAL review
# brief of a task refuse - no monkeypatching of the function under test.
HAZARDOUS_NOTE = f"compare the handler's output against the recorded value {REAL_SECRET}"


def _waiting_for_review(layout, tid="A", **fields):
    """A task parked in WAITING_FOR_REVIEW with one reviewer role, the state `review.pending()` reads."""
    make_task(layout, tid, reviewer=("code_review",), **fields)
    tasks.set_status(layout, tid, "IN_PROGRESS")
    tasks.record_delivery(layout, tid, DIGEST, actor="test", paths=[f"{tid}.txt"])
    tasks.record_verification(layout, tid, DIGEST, True, detail="ok")
    tasks.advance(layout, tid)
    return tasks.load(layout, tid)


def _in_progress_diagnosis_subject(layout, tid):
    """A task a diagnosis job can be continued for: in progress, with a delivery recorded (the shape
    `diagnosis.rebuild` loads and `diagnosis.brief` describes)."""
    make_task(layout, tid)
    tasks.set_status(layout, tid, "IN_PROGRESS")
    tasks.record_delivery(layout, tid, DIGEST, actor="test", paths=[f"{tid}.txt"])
    return tasks.load(layout, tid)


def _owner_items(layout, task_id):
    return [i for i in inbox_mod.open_items(layout, "owner") if i.get("task_id") == task_id]


def _leaks(layout, needle, task_id):
    """Where the refusal path must never put the text that tripped the scanner: the event log, the owner's inbox,
    any run directory (a prompt, a frozen input) and the stopped task's own history notes. The violation's own
    message goes to all four; the text it matched goes to none of them. (The text itself legitimately stays
    wherever its author put it - an artifact, a review record, the owner's note on the task - which is why this
    looks at the four places the refusal writes, not at every file under the root.)"""
    out = []
    if needle in layout.log.read_text(encoding="utf-8"):
        out.append("state/log.jsonl")
    for p in sorted(list(layout.inbox.rglob("*")) + list(layout.runs.rglob("*"))):
        if p.is_file() and needle in p.read_text(encoding="utf-8", errors="ignore"):
            out.append(str(p.relative_to(layout.root)))
    if any(needle in (h.get("note") or "") for h in tasks.load(layout, task_id).history):
        out.append(f"{task_id}: history note")
    return out


def test_a_refused_review_dispatch_leaves_the_queue_and_the_owner_inbox_alone(layout):
    """The round-1 finding: `refuse_packet` only moved an IN_PROGRESS task, but `review.dispatch` is only ever
    reached for a WAITING_FOR_REVIEW one - so the refusal added a note, left the status alone, and `pending()`
    offered the same (task, role) pair again on the very next turn. Nothing was dispatched, so no in-flight job
    and no live ReviewRecord stopped it either: every turn refused again and created ANOTHER owner decision item,
    and after three the per-role cap was spent and every OTHER task's decisions and approvals hit InboxFull.

    Four turns now leave: one refusal, one BLOCKED task that `pending()` no longer offers, one owner card, and
    room in the owner's inbox for the tasks that have nothing wrong with them."""
    _waiting_for_review(layout, "A", review_note=HAZARDOUS_NOTE)
    sup = Supervisor(layout, sleep_s=0)
    assert [(t.id, role) for t, role in review.pending(layout)] == [("A", "code_review")]
    for _ in range(4):
        sup.dispatch_reviews()
    assert tasks.load(layout, "A").status == TaskStatus.BLOCKED
    assert review.pending(layout) == []                      # the repeat is over: nothing re-offers it
    assert jobs_mod.open_records(layout) == []               # the review was never dispatched
    log_text = layout.log.read_text(encoding="utf-8")
    assert log_text.count("packet_refused") == 1             # once, not once per turn
    assert len(_owner_items(layout, "A")) == 1
    # The real point of the cap: another task's own decision item must still fit.
    inbox_mod.create(layout, for_role="owner", subject="Decision needed: another task", summary="unrelated",
                     kind=inbox_mod.DECISION_KIND, allowed_responses=list(inbox_mod.DECISION_RESPONSES),
                     task_id="Z")
    assert _leaks(layout, REAL_SECRET, "A") == []


def test_a_refused_diagnosis_dispatch_is_contained_to_its_own_task(layout):
    """The diagnosis boundary, with the incident's own shape: the failure `detail` a diagnosis brief quotes is
    exactly the text that tripped the scanner, so the brief refuses. `dispatch` returns None (no diagnosis is
    running for this), nothing is launched, and the task stops for the owner instead of the escalation leaving a
    silent issue behind."""
    task = _revision_task(layout)
    sup = FakeSupervisor(layout)
    detail = f"verification failed: the command echoed {REAL_SECRET} into its output"
    assert diagnosis.dispatch(sup, task, "verification", detail, "sig-1") is None
    assert sup.calls == []
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED
    assert len(_owner_items(layout, "T")) == 1
    assert "packet_refused" in layout.log.read_text(encoding="utf-8")
    assert _leaks(layout, REAL_SECRET, "T") == []


@pytest.mark.parametrize("kind", ["review", "diagnosis"])
def test_a_refused_continuation_rebuild_never_charges_the_loop_error_budget(layout, monkeypatch, kind):
    """Round-1 finding 2: `loop.requeue()` rebuilt a deferred review's or diagnosis's brief with no guard, so a
    refusal there propagated requeue -> ingest -> settle -> once() and the deferred record was left open and
    uncontinued - the same turn raised again on every tick until the "5 loop errors in 10 minutes" breaker set
    the loop BLOCKED. That is the incident this release closes, reached by nothing more unusual than a capacity
    deferral of a job whose brief quotes a key-shaped value.

    Now the refused rebuild ends the continuation with no successor (what both callers already do for inputs
    that moved on), the task stops for the owner, and three further turns raise nothing."""
    task = (_waiting_for_review(layout, "A", review_note=HAZARDOUS_NOTE) if kind == "review"
            else _in_progress_diagnosis_subject(layout, "A"))
    attempt_id = "A.a2"
    run_dir = layout.runs / "A" / attempt_id
    run_dir.mkdir(parents=True, exist_ok=True)
    spec_kind = "review" if kind == "review" else diagnosis.KIND
    extra = ({"review_role": "code_review", "review_digest": task.delivery.digest} if kind == "review" else
             {"diag_role": diagnosis.settings(layout)["roles"]["manager"], "diag_level": "manager",
              "diag_source": "verification", "diag_signature": "sig-1", "diag_grant": 0,
              "diag_detail": f"verification failed: the command echoed {REAL_SECRET} into its output"})
    jobs_mod.prepare(layout, "A", attempt_id, "claude_cli", {"kind": spec_kind, "run_dir": str(run_dir)}, **extra)
    jobs_mod.set_phase(layout, attempt_id, "RUNNING")
    monkeypatch.setattr(REGISTRY["claude_cli"], "collect",
                        lambda spec, handle: Result(status="DEFERRED", error="window spent"))

    sup = Supervisor(layout, sleep_s=0)
    for _ in range(3):
        sup.once()
    assert sup.errors == []                                  # the breaker was never charged
    assert jobs_mod.read(layout, attempt_id)["phase"] == "DONE"
    assert jobs_mod.open_records(layout) == []               # no successor: the continuation ends here
    assert tasks.load(layout, "A").status == TaskStatus.BLOCKED
    assert len(_owner_items(layout, "A")) == 1
    assert _leaks(layout, REAL_SECRET, "A") == []


# -------------------------------------------------------- a machine-wide refusal is NOT one task's refusal

def test_a_machine_wide_refusal_is_not_contained_to_whichever_task_dispatched_next(layout):
    """`assert_packet_clean` also refuses for two conditions that say nothing about the packet's own text: the
    credential store could not be enumerated, and a required packet-screen plugin would not load. Containing
    those per task would BLOCK every task in turn and spend the owner's inbox on identical cards while the
    misconfiguration stood, so `refuse_packet` re-raises them: still fail-closed (nothing dispatches either
    way), with the loop's own error path - not the task list - carrying a machine-wide stop."""
    task = _revision_task(layout)
    sup = FakeSupervisor(layout)
    machine_wide = policy.PolicyViolation("packet refused: the credential store could not be screened (denied)",
                                          scope="machine")
    with pytest.raises(policy.PolicyViolation):
        escalation.refuse_packet(sup, task, "review packet", machine_wide)
    assert tasks.load(layout, "T").status == TaskStatus.IN_PROGRESS      # untouched: it is not this task's fault
    assert inbox_mod.open_items(layout, "owner") == []


def test_which_refusals_are_machine_wide_and_which_belong_to_one_task(layout, monkeypatch):
    """The scope each raise site actually carries, at the real sites - a per-task refusal must not become a
    machine-wide one by accident (the loop would stop on one task's text) or the other way round."""
    assert policy.PolicyViolation("plain").scope == "task"               # a plugin screen's own refusal
    with pytest.raises(policy.PolicyViolation) as per_task:
        policy.assert_packet_clean(f"the value is {REAL_SECRET}", layout, "review packet T")
    assert per_task.value.scope == "task"

    from supervisor import plugins

    def wont_load(_layout):
        raise plugins.PluginError("a required plugin failed to import")
    monkeypatch.setattr(plugins, "assert_loaded", wont_load)
    with pytest.raises(policy.PolicyViolation) as plugin_gone:
        policy.plugin_screens(layout)
    assert plugin_gone.value.scope == "machine"
    monkeypatch.undo()

    make_task(layout, "T")
    write_secret(layout)
    break_listing(monkeypatch, layout.secrets)
    with pytest.raises(policy.PolicyViolation) as unscreenable:
        packets.delivery_brief(layout, tasks.load(layout, "T"))          # the brief itself is clean
    assert unscreenable.value.scope == "machine" and "could not be screened" in str(unscreenable.value)
