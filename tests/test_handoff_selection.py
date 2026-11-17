"""Which prior HANDOFF.md (if any) a job is pointed at must depend on its kind and its place in the task's own
attempt history, not on which sibling run directory happens to have the newest mtime (R-15): a review must never
read a producer's notes, a continuation reads only its own predecessor's, a revision starts from nothing unless
the attempt it follows produced no delivery at all, and a diagnosis reads its parent's."""
import os
import time

from supervisor import tasks
from supervisor.adapters import REGISTRY
from tests.conftest import make_task

ADAPTER = REGISTRY["claude_cli"]
PRIOR_SUFFIX = " first and verify its claims against the referenced files. "


def _write_handoff(layout, tid, job_id, text="work in progress", mtime=None):
    d = layout.runs / tid / job_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "HANDOFF.md"
    p.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _set_attempts(layout, tid, entries):
    def fn(doc):
        doc["attempts"] = entries
    tasks._update(layout, tid, fn)


def _set_job(layout, tid, job):
    def fn(doc):
        doc["job"] = job
    tasks._update(layout, tid, fn)


def _given_prior(layout, tid, job_id) -> str | None:
    """The path named in this job's "Read <path> first..." sentence, or None when its prompt carries no such
    sentence (handoff.select returned nothing for it)."""
    text = (layout.runs / tid / job_id / "prompt.md").read_text(encoding="utf-8")
    i, j = text.find("Read "), text.find(PRIOR_SUFFIX)
    return text[i + len("Read "):j] if i != -1 and j != -1 and j > i else None


def test_review_job_is_never_given_a_prior_handoff(layout):
    """Even when the delivery it is reviewing wrote a HANDOFF.md, and even when it is the only, newest one on disk."""
    tid = "REV"
    make_task(layout, tid, reviewer=())
    _write_handoff(layout, tid, f"{tid}.a1", mtime=time.time())   # the only candidate, and the newest file on disk
    _set_attempts(layout, tid, [{"id": f"{tid}.a1", "resource": "claude_cli", "note": f"dispatch:engineer {tid}.a1"}])
    _set_job(layout, tid, {"adapter": "claude_cli", "role": "code_review", "tools": "reviewer",
                           "write_scope": "run_dir", "result_kind": "review"})
    ADAPTER.prepare(layout, tasks.load(layout, tid), f"{tid}.a2")
    assert _given_prior(layout, tid, f"{tid}.a2") is None


def test_continuation_session_is_given_only_its_own_predecessor(layout):
    """A second session of the same attempt (a capacity/time-limit continuation) is pointed at the session it
    continues, and nothing else — not a newer, unrelated handoff sitting in a sibling run directory."""
    tid = "CONT"
    make_task(layout, tid, reviewer=(), job={"adapter": "claude_cli", "role": "engineer"})
    now = time.time()
    _write_handoff(layout, tid, f"{tid}.a1", text="session 1 stopped here", mtime=now - 100)
    _write_handoff(layout, tid, f"{tid}.decoy", text="unrelated, newer", mtime=now)   # newest by mtime, must be ignored
    _set_attempts(layout, tid, [
        {"id": f"{tid}.a1", "resource": "claude_cli", "note": f"dispatch:engineer {tid}.a1"},
        {"id": f"{tid}.a2", "resource": "claude_cli", "note": f"continuation after capacity limit {tid}.a2"},
    ])
    ADAPTER.prepare(layout, tasks.load(layout, tid), f"{tid}.a2")
    assert _given_prior(layout, tid, f"{tid}.a2") == str(layout.runs / tid / f"{tid}.a1" / "HANDOFF.md")


def test_revision_after_a_verified_attempt_gets_no_handoff(layout):
    """A revision opens a new attempt; the previous one produced a delivery (it was verified and reviewed), so the
    next session starts from the review's findings, not from anyone's run-directory notes — even a reviewer's own
    HANDOFF.md, which is the newest file on disk."""
    tid = "REVN"
    make_task(layout, tid, reviewer=(), job={"adapter": "claude_cli", "role": "engineer"})
    now = time.time()
    _write_handoff(layout, tid, f"{tid}.a1", text="first delivery", mtime=now - 200)
    _write_handoff(layout, tid, f"{tid}.a3", text="reviewer's own notes", mtime=now)   # newest by mtime
    _set_attempts(layout, tid, [
        {"id": f"{tid}.a1", "resource": "claude_cli", "note": f"dispatch:engineer {tid}.a1"},
        {"id": f"{tid}.a2", "resource": "process", "note": f"verify delivery {tid}.a1"},
        {"id": f"{tid}.a3", "resource": "claude_cli", "note": "review:chief_architect"},
        {"id": f"{tid}.a4", "resource": "claude_cli", "note": f"revision after review failure {tid}.a4"},
    ])
    ADAPTER.prepare(layout, tasks.load(layout, tid), f"{tid}.a4")
    assert _given_prior(layout, tid, f"{tid}.a4") is None


def test_revision_after_an_attempt_with_no_delivery_at_all_gets_its_last_session(layout):
    """The attempt being revised never produced a delivery (its continuations were exhausted and a diagnosis
    requeued it): the only record of where the work stopped is its last session's handoff."""
    tid = "REVB"
    make_task(layout, tid, reviewer=(), job={"adapter": "claude_cli", "role": "engineer"})
    _write_handoff(layout, tid, f"{tid}.a2", text="stopped here, never delivered", mtime=time.time() - 50)
    _set_attempts(layout, tid, [
        {"id": f"{tid}.a1", "resource": "claude_cli", "note": f"dispatch:engineer {tid}.a1"},
        {"id": f"{tid}.a2", "resource": "claude_cli", "note": f"continuation after capacity limit {tid}.a2"},
        {"id": f"{tid}.a3", "resource": "claude_cli", "note": "diagnosis:manager"},
        {"id": f"{tid}.a4", "resource": "claude_cli", "note": f"dispatch:engineer {tid}.a4"},
    ])
    ADAPTER.prepare(layout, tasks.load(layout, tid), f"{tid}.a4")
    assert _given_prior(layout, tid, f"{tid}.a4") == str(layout.runs / tid / f"{tid}.a2" / "HANDOFF.md")


def test_revision_after_an_inline_verified_attempt_gets_no_handoff(layout):
    """The attempt being revised declared no test commands: its delivery was verified and reviewed the same tick
    its worker claimed COMPLETE (verify.verify() returns ok with nothing to track), so the store never gained a
    'verify delivery' entry for it — only a 'review:' one. The revision still starts from nothing, not from the
    rejected author's own run-directory notes, even though no 'verify delivery' entry exists to say a delivery
    ever happened."""
    tid = "REVI"
    make_task(layout, tid, reviewer=(), job={"adapter": "claude_cli", "role": "engineer"})
    now = time.time()
    _write_handoff(layout, tid, f"{tid}.a1", text="first delivery", mtime=now - 100)
    _write_handoff(layout, tid, f"{tid}.a2", text="reviewer's own notes", mtime=now)   # newest by mtime
    _set_attempts(layout, tid, [
        {"id": f"{tid}.a1", "resource": "claude_cli", "note": f"dispatch:engineer {tid}.a1"},
        {"id": f"{tid}.a2", "resource": "claude_cli", "note": "review:chief_architect"},
        {"id": f"{tid}.a3", "resource": "claude_cli", "note": f"revision after review failure {tid}.a3"},
    ])
    ADAPTER.prepare(layout, tasks.load(layout, tid), f"{tid}.a3")
    assert _given_prior(layout, tid, f"{tid}.a3") is None


def test_continuation_after_an_intervening_entry_is_given_its_own_predecessor(layout):
    """A continuation dispatched after some other job for the same task was recorded in between it and the
    session it continues (e.g. a crash-recovery diagnosis) still reads that session's own handoff — not the
    entry that happens to sit immediately before it in the attempt list."""
    tid = "CONT2"
    make_task(layout, tid, reviewer=(), job={"adapter": "claude_cli", "role": "engineer"})
    now = time.time()
    _write_handoff(layout, tid, f"{tid}.a1", text="session 1 stopped here", mtime=now - 100)
    _set_attempts(layout, tid, [
        {"id": f"{tid}.a1", "resource": "claude_cli", "note": f"dispatch:engineer {tid}.a1"},
        {"id": f"{tid}.a2", "resource": "claude_cli", "note": "diagnosis:manager"},
        {"id": f"{tid}.a3", "resource": "claude_cli", "note": f"continuation after capacity limit {tid}.a3"},
    ])
    ADAPTER.prepare(layout, tasks.load(layout, tid), f"{tid}.a3")
    assert _given_prior(layout, tid, f"{tid}.a3") == str(layout.runs / tid / f"{tid}.a1" / "HANDOFF.md")


def test_diagnosis_is_given_its_parents_handoff(layout):
    """A diagnosis reads the handoff of the job it is explaining, if that job wrote one."""
    tid = "DIAG"
    make_task(layout, tid, reviewer=())
    _write_handoff(layout, tid, f"{tid}.a1", text="blocked here", mtime=time.time())
    _set_attempts(layout, tid, [
        {"id": f"{tid}.a1", "resource": "claude_cli", "note": f"dispatch:engineer {tid}.a1"},
        {"id": f"{tid}.a2", "resource": "claude_cli", "note": "diagnosis:manager"},
    ])
    _set_job(layout, tid, {"adapter": "claude_cli", "role": "code_review", "tools": "reviewer",
                           "write_scope": "run_dir", "result_kind": "diagnosis"})
    ADAPTER.prepare(layout, tasks.load(layout, tid), f"{tid}.a2")
    assert _given_prior(layout, tid, f"{tid}.a2") == str(layout.runs / tid / f"{tid}.a1" / "HANDOFF.md")


def test_diagnosis_given_no_handoff_when_its_parent_wrote_none(layout):
    tid = "DIAGN"
    make_task(layout, tid, reviewer=())
    _set_attempts(layout, tid, [
        {"id": f"{tid}.a1", "resource": "claude_cli", "note": f"dispatch:engineer {tid}.a1"},
        {"id": f"{tid}.a2", "resource": "claude_cli", "note": "diagnosis:manager"},
    ])
    _set_job(layout, tid, {"adapter": "claude_cli", "role": "code_review", "tools": "reviewer",
                           "write_scope": "run_dir", "result_kind": "diagnosis"})
    ADAPTER.prepare(layout, tasks.load(layout, tid), f"{tid}.a2")
    assert _given_prior(layout, tid, f"{tid}.a2") is None
