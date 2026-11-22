"""Five readers recover meaning from a job id suffix today: `jobs.for_task`'s sort, the escalation replay's
"a later delivery already exists" check, the continuation count, the stall sweep's "newest delivery" and the
review salvage sort. At the reader release each also tolerated a record with none of the attempt-identity fields
(the suffix was the only signal such a record carried) with the fields set and the suffix deliberately misleading
(as a migrated-but-not-renamed record can be, since a record is never renamed) answering identically. The writer
switch deletes every one of those suffix fallbacks:
`jobs.attempt_no` is gone, and the start-up guard it ships with refuses to dispatch on a store holding even one
record with neither a migration stamp nor an allocator-written `kind`/`seq` — so a fallback for "no fields at all"
no longer has a reachable input to answer for. What is left below tests the fields-only behaviour each reader
settles on; `jobs.open_records` is also covered here: it is already read-only (an earlier release's fix), and
this is the regression that keeps it that way.

Two further cases, carried over from the prior release's round-2 review as observations rather than findings, each
get a test that states what the tree does today: a caller of `reconcile_advance` that passes an explicit job set
omitting a task with an open attempt restores the flip the function exists to prevent (nothing in the tree does
this); and evidence complete beside a stale open record of the same task defers the safety net until that record
closes."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from unittest.mock import patch

from supervisor import escalation, jobs, review, tasks
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus

from tests.conftest import make_task
from tests.test_no_silent_failures import Recorder, _stop

D = "a" * 64


def _write(layout, attempt_id, task_id, phase="DONE", kind="delivery", **fields):
    return jobs.write(layout, {"attempt_id": attempt_id, "task_id": task_id, "adapter": "claude_cli",
                               "phase": phase, "spec": {"kind": kind}, "handle": None, **fields})


# ---------------------------------------------------------------- 1. job sort (jobs.for_task)

def test_for_task_sorts_by_attempt_field(layout):
    # the suffix is misleading: a1 is really the later attempt. No fallback reads it (§6/§8.6): only the fields do.
    _write(layout, "B.a1", "B", attempt=2, seq=1, marker="second")
    _write(layout, "B.a2", "B", attempt=1, seq=1, marker="first")
    assert [r["marker"] for r in jobs.for_task(layout, "B")] == ["first", "second"]


# ---------------------------------------------------------------- 2. escalation replay

def test_escalation_replay_compares_attempt_fields(layout):
    _write(layout, "B.a5", "B", attempt=1)            # the failing delivery: attempt 1, a misleading high suffix
    _write(layout, "B.a2", "B", attempt=2)            # the later delivery: attempt 2, a lower suffix
    assert escalation._newer_delivery_exists(layout, "B", "B.a5") is True


# ---------------------------------------------------------------- 3. continuation count

def test_continuation_count_reads_cause_and_scopes_to_the_attempt():
    # an earlier attempt's own continuation (never reset) must not inflate this attempt's count
    recs = [{"attempt_id": "B.a1", "cause": "continuation", "attempt": 1},
           {"attempt_id": "B.a2", "cause": "continuation", "attempt": 2}]
    assert len(Supervisor._continuations(recs, 2)) == 1


# ---------------------------------------------------------------- 4. stall sweep's "newest delivery"

def test_stall_sweep_newest_delivery_selection_reads_the_attempt_field(layout):
    """Drives `Supervisor.sweep_stalled` itself, not a copy of its selection expression. A FAILED task's human
    delivery (the owner's own REJECT/MORE_LIKE) is a deliberate hold the sweep must leave alone -- but only when
    it is read as the newest delivery. Its id suffix is the lower of the two on purpose; only its `attempt` field
    marks it newest, and only that field is read."""
    now = _stop(layout, "U", "FAILED", minutes_ago=15)
    _write(layout, "U.a9", "U", adapter="claude_cli", attempt=1, seq=1)   # high suffix, but the older attempt
    _write(layout, "U.a1", "U", adapter="human", attempt=2, seq=1)       # low suffix, newest by field
    assert Recorder(layout).sweep_stalled(now=now) == []


# ---------------------------------------------------------------- 5. review salvage sort

def test_review_salvage_sorts_by_attempt_then_seq():
    """`seq` alone cannot tell two attempts' review jobs apart -- it is only the ordinal of a kind inside one
    attempt, and a revision that re-delivers a byte-identical answer keeps the same
    role, digest and `seq == 1` on both attempts' review records. Only `attempt` orders them correctly; the id
    suffix (misleading here on purpose) is never read."""
    recs = [{"attempt_id": "B.a2", "attempt": 1, "seq": 1, "marker": "older"},
           {"attempt_id": "B.a1", "attempt": 2, "seq": 1, "marker": "newer"}]
    recs.sort(key=review._salvage_sort_key, reverse=True)
    assert [r["marker"] for r in recs] == ["newer", "older"]


# ---------------------------------------------------------------- job enumeration: already read-only

def test_open_records_leaves_an_unreadable_record_in_place_and_reports_it_once(layout):
    bad = layout.jobs / "A.a1.json"
    bad.write_text("{not valid json", encoding="utf-8")
    before = bad.read_text(encoding="utf-8")

    assert jobs.open_records(layout) == []          # reported, never raised
    assert bad.exists()
    assert bad.read_text(encoding="utf-8") == before   # never rewritten, never renamed, never deleted

    jobs.open_records(layout)                        # a second page view: still there, still unreadable
    assert bad.read_text(encoding="utf-8") == before

    Supervisor(layout, sleep_s=0).once()              # a full loop turn, not just the reader in isolation
    assert bad.read_text(encoding="utf-8") == before

    events = [json.loads(e) for e in layout.log.read_text(encoding="utf-8").splitlines() if e.strip()]
    reports = [e for e in events if e.get("event") == "job_unreadable"]
    assert len(reports) == 1                          # deduped across every scan above, not one line per scan


# ---------------------------------------------------------------- the two round-2 observations, stated as tests

def test_an_explicit_job_set_omitting_the_task_restores_the_pre_guard_flip(layout):
    """Observation, not a finding: the guard inside reconcile_advance is the whole fix, and it only holds because
    every caller in the tree either passes nothing (a fresh scan) or the turn's own live set. A caller that passes
    an explicit set omitting a task with an open attempt gets the pre-guard flip back."""
    make_task(layout, "A", status="IN_PROGRESS", tests=[])
    tasks.record_delivery(layout, "A", D, paths=[])
    tasks.record_verification(layout, "A", D, False, detail="missing artifact x")
    tasks.advance(layout, "A")
    assert tasks.load(layout, "A").status == TaskStatus.FAILED

    jobs.write(layout, {"attempt_id": "A.a2", "task_id": "A", "adapter": "claude_cli", "phase": "RUNNING",
                        "spec": {"kind": "delivery"}, "handle": {"pid": 0}, "deferred_until": 0})
    tasks.set_status(layout, "A", "IN_PROGRESS", actor="loop", note="launch A.a2 via claude_cli")

    advanced = tasks.reconcile_advance(layout, None, set())   # an explicit set, omitting "A"

    assert advanced == ["A"]
    assert tasks.load(layout, "A").status == TaskStatus.FAILED
    failed_entries = [h for h in tasks.load(layout, "A").history if "verification failed" in (h.get("note") or "")]
    assert len(failed_entries) == 2, "an explicit set omitting the task restores the flip the guard exists to prevent"


def test_the_loops_own_call_passes_the_turns_live_snapshot_not_a_fixed_set():
    """The observation's other half: nobody in the tree calls reconcile_advance the way the test above does. The
    loop's one real call site builds its set from the turn's own open-job scan, every turn."""
    import inspect
    src = inspect.getsource(Supervisor.once)
    assert 'reconcile_advance(self.layout, tasks_snapshot, {r["task_id"] for r in jobs_snapshot})' in src


@contextmanager
def _fake_process():
    from supervisor.adapters import REGISTRY
    ad = REGISTRY["process"]
    with patch.object(ad, "recover", return_value=None), patch.object(ad, "submit", return_value={"kind": "fake", "pid": 0}), \
         patch.object(ad, "alive", return_value=True), patch.object(ad, "collect", return_value=None):
        yield


@contextmanager
def _no_launch():
    with patch.object(Supervisor, "launch", lambda self, rec: rec):
        yield


def test_evidence_complete_beside_a_stale_open_record_defers_the_safety_net_until_it_closes(layout):
    """The second observation: evidence already complete for the digest on record, and a record of the same task
    still open, defers reconcile_advance's safety net until that record closes. Nothing in the tree leaves such a
    pair behind on purpose; the record's own ingest advances the task once it does."""
    make_task(layout, "A", tests=[])
    tasks.set_status(layout, "A", "IN_PROGRESS")
    tasks.record_delivery(layout, "A", D, paths=[])
    tasks.record_verification(layout, "A", D, True)
    jobs.write(layout, {"attempt_id": "A.a2", "task_id": "A", "adapter": "process", "phase": "PREPARED",
                        "spec": {"kind": "delivery", "run_dir": str(layout.runs / "A" / "A.a2"), "root": str(layout.root)},
                        "deferred_until": time.time() + 3600})
    with _fake_process(), _no_launch():
        sup = Supervisor(layout, sleep_s=0)
        sup.once()
        assert tasks.load(layout, "A").status == TaskStatus.IN_PROGRESS   # deferred, not advanced
        jobs.set_phase(layout, "A.a2", "FAILED", error="closed by the test")
        sup.once()
    assert tasks.load(layout, "A").status == TaskStatus.COMPLETE
