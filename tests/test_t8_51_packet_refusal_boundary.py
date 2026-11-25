"""T8-51: the `sk-`-shaped secret patterns gained a left boundary (a match can no longer start mid-word), and a
`PolicyViolation` raised while building one task's own packet (revision, review or diagnosis) is now caught at
the dispatch boundary that raised it instead of propagating out of `settle()` into the loop's own error budget.

Every example string below is synthetic and neutral - no project name, task id or real credential, matching the
one this project's own suite (`tests/test_policy.py`) already uses for the same purpose."""
import pytest

from supervisor import escalation, packets, policy, tasks
from supervisor import jobs as jobs_mod
from supervisor.adapters import REGISTRY
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

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

    @pyad.job("t8_51_write_artifact")
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
        doc["job"] = {"adapter": "python", "fn": "t8_51_write_artifact"}
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
