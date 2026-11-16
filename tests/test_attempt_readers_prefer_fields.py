"""Five readers recover meaning from a job id suffix today: `jobs.for_task`'s sort, the escalation replay's
"a later delivery already exists" check, the continuation count, the stall sweep's "newest delivery" and the
review salvage sort. Each gets a store of the same scenario told two ways -- once with no attempt-identity fields
at all (the suffix is the only signal, as every record is today) and once with the fields set and the suffix
deliberately misleading (as a migrated-but-not-renamed record can be, since a record is never renamed) -- and must
answer identically either way. `jobs.open_records` is also covered here: it is already read-only (an earlier
release's fix), and this is the regression that keeps it that way.

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

def test_for_task_sorts_by_attempt_field_with_the_suffix_as_the_fallback(layout):
    # no fields at all: the suffix is the only signal, and it already matches the true order
    _write(layout, "A.a1", "A", marker="first")
    _write(layout, "A.a2", "A", marker="second")
    without_fields = [r["marker"] for r in jobs.for_task(layout, "A")]

    # the fields are set and the suffix is misleading: a1 is really the later attempt
    _write(layout, "B.a1", "B", attempt=2, seq=1, marker="second")
    _write(layout, "B.a2", "B", attempt=1, seq=1, marker="first")
    with_fields = [r["marker"] for r in jobs.for_task(layout, "B")]

    assert without_fields == ["first", "second"]
    assert with_fields == without_fields


# ---------------------------------------------------------------- 2. escalation replay

def test_escalation_replay_compares_attempt_fields_with_the_suffix_as_the_fallback(layout):
    _write(layout, "A.a1", "A")                      # the failing delivery, no fields
    _write(layout, "A.a2", "A")                      # a later delivery already exists
    without_fields = escalation._newer_delivery_exists(layout, "A", "A.a1")

    _write(layout, "B.a5", "B", attempt=1)            # the failing delivery: attempt 1, a misleading high suffix
    _write(layout, "B.a2", "B", attempt=2)            # the later delivery: attempt 2, a lower suffix
    with_fields = escalation._newer_delivery_exists(layout, "B", "B.a5")

    assert without_fields is True
    assert with_fields == without_fields


# ---------------------------------------------------------------- 3. continuation count

def test_continuation_count_reads_cause_and_scopes_to_the_attempt():
    # no fields: a text prefix, counted across the task exactly as today (unscoped: attempt is None)
    recs_no_fields = [{"attempt_id": "A.a1", "deferred_reason": "continuation after capacity limit"}]
    without_fields = len(Supervisor._continuations(recs_no_fields, None))

    # fields set: an earlier attempt's own continuation (never reset) must not inflate this attempt's count
    recs_with_fields = [{"attempt_id": "B.a1", "cause": "continuation", "attempt": 1},
                        {"attempt_id": "B.a2", "cause": "continuation", "attempt": 2}]
    with_fields = len(Supervisor._continuations(recs_with_fields, 2))

    assert without_fields == 1
    assert with_fields == without_fields


# ---------------------------------------------------------------- 4. stall sweep's "newest delivery"

def test_stall_sweep_newest_delivery_selection_reads_fields_with_the_suffix_as_the_fallback(layout):
    """Drives `Supervisor.sweep_stalled` itself, not a copy of its selection expression. A FAILED task's human
    delivery (the owner's own REJECT/MORE_LIKE) is a deliberate hold the sweep must leave alone -- but only when
    it is read as the newest delivery. On one store it is newest by suffix (no fields at all); on the other its
    suffix is the lower of the two and only its `attempt` field marks it newest. Either way the sweep's own
    decision -- no action -- must be identical."""
    now = _stop(layout, "T", "FAILED", minutes_ago=15)
    _write(layout, "T.a1", "T", adapter="claude_cli")
    _write(layout, "T.a2", "T", adapter="human")                       # newest by suffix: no fields set
    without_fields = Recorder(layout).sweep_stalled(now=now)

    now2 = _stop(layout, "U", "FAILED", minutes_ago=15)
    _write(layout, "U.a9", "U", adapter="claude_cli", attempt=1, seq=1)   # high suffix, but the older attempt
    _write(layout, "U.a1", "U", adapter="human", attempt=2, seq=1)       # low suffix, newest by field
    with_fields = Recorder(layout).sweep_stalled(now=now2)

    assert without_fields == []
    assert with_fields == without_fields


# ---------------------------------------------------------------- 5. review salvage sort

def test_review_salvage_sorts_by_attempt_then_seq_with_the_suffix_as_the_fallback():
    """`seq` alone cannot tell two attempts' review jobs apart -- it is only the ordinal of a kind inside one
    attempt (ATTEMPT_MODEL.md section 6), and a revision that re-delivers a byte-identical answer keeps the same
    role, digest and `seq == 1` on both attempts' review records. Only `attempt` orders them correctly."""
    no_fields = [{"attempt_id": "A.a1", "marker": "older"}, {"attempt_id": "A.a2", "marker": "newer"}]
    no_fields.sort(key=review._salvage_sort_key, reverse=True)
    without_fields = [r["marker"] for r in no_fields]

    # both attempts' review jobs carry seq == 1 (seq is per-attempt), listed oldest first exactly as jobs.for_task
    # would hand them to salvage() -- a `seq`-only (or stable-sort) tie-break leaves them in that order, newest
    # last, not first.
    with_fields = [{"attempt_id": "B.a2", "attempt": 1, "seq": 1, "marker": "older"},
                  {"attempt_id": "B.a1", "attempt": 2, "seq": 1, "marker": "newer"}]
    with_fields.sort(key=review._salvage_sort_key, reverse=True)
    with_fields_markers = [r["marker"] for r in with_fields]

    assert without_fields == ["newer", "older"]
    assert with_fields_markers == without_fields


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
