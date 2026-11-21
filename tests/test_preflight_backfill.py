"""RECOVERY_PLAN.md section 3.3's backfill, D-027 decision 9, step 5: the launch rule the previous step adds would
stop every existing `process`/`python` task on upgrade day, since none of them has a stored preflight. A bounded,
one-time sweep (oldest task first), a completion mark the store carries once, a deduped owner item for a block that
cannot pass, and a run-on-absent window at the gate that closes the moment the mark is written -- so an existing
store survives the upgrade with no manual rewrite and no `supervisor migrate` step."""
from __future__ import annotations

import json
import sys
import time

from supervisor import gate, inbox, preflight, tasks as tasks_mod
from supervisor.loop import Supervisor
from tests.conftest import make_task

PY = sys.executable
GOOD_ARGV = [PY, "-c", "pass"]
BAD_ARGV = ["definitely-not-a-real-binary-xyz123"]


def _legacy_task(layout, tid, argv=GOOD_ARGV, created=None, adapter="process", **extra):
    """A task shaped exactly as one written before this release: a job block with no `preflight` key at all."""
    job = {"adapter": adapter, "argv": argv, "preflight": None}
    job.update(extra)
    fields = {"job": job}
    if created is not None:
        fields["created"] = created
    return make_task(layout, tid, **fields)


# ------------------------------------------------------------------- the sweep itself: bounded, oldest first, once

def test_a_store_of_pre_release_tasks_launches_after_the_upgrade_with_no_manual_rewrite(layout):
    for i in range(5):
        _legacy_task(layout, f"T{i}", created=f"2026-01-{i + 1:02d}T00:00:00Z")
    sup = Supervisor(layout, sleep_s=0)
    turns = 0
    while not preflight.backfill_complete(layout) and turns < 20:
        sup.preflight_backfill()
        turns += 1
    assert preflight.backfill_complete(layout)
    for i in range(5):
        t = tasks_mod.load(layout, f"T{i}")
        assert t.job.preflight is not None and t.job.preflight.ok is True
        assert gate.check(layout, t, "process", "delivery", running=[]) is None
    assert inbox.open_items(layout, "owner") == []


def test_the_sweep_is_bounded_per_turn_and_takes_the_oldest_task_first(layout, monkeypatch):
    monkeypatch.setattr(preflight, "BACKFILL_BATCH", 2)
    for i in range(5):
        _legacy_task(layout, f"T{i}", created=f"2026-02-{i + 1:02d}T00:00:00Z")
    sup = Supervisor(layout, sleep_s=0)
    acted = sup.preflight_backfill()
    assert acted == ["T0", "T1"]                     # the two oldest, and no more than the bound
    for i in range(2, 5):
        assert tasks_mod.load(layout, f"T{i}").job.preflight is None
    assert not preflight.backfill_complete(layout)


def test_the_sweep_records_its_own_completion_exactly_once(layout, monkeypatch):
    monkeypatch.setattr(preflight, "BACKFILL_BATCH", 2)
    for i in range(3):
        _legacy_task(layout, f"T{i}", created=f"2026-03-{i + 1:02d}T00:00:00Z")
    sup = Supervisor(layout, sleep_s=0)
    sup.preflight_backfill()                          # turn 1: T0, T1
    assert not preflight.backfill_complete(layout)
    assert preflight.backfill_progress(layout) == 2            # the running total, not just this turn's batch
    sup.preflight_backfill()                           # turn 2: T2, nothing left -> marked complete
    assert preflight.backfill_complete(layout)
    marked_at = preflight._backfill_state_path(layout).read_text(encoding="utf-8")
    assert json.loads(marked_at)["swept"] == 3          # the sweep's own total across both turns, not turn 2 alone
    sup.preflight_backfill()                           # turn 3: already complete, a no-op
    assert preflight._backfill_state_path(layout).read_text(encoding="utf-8") == marked_at


def test_a_fresh_store_with_nothing_to_backfill_completes_on_its_first_turn(layout):
    make_task(layout, "T1")                            # no job at all
    sup = Supervisor(layout, sleep_s=0)
    assert sup.preflight_backfill() == []
    assert preflight.backfill_complete(layout)


# ------------------------------------------------------------------- this project's own upgrade case

def test_literal_argv_blocks_naming_not_yet_written_output_files_are_swept_clean(layout):
    """A literal `argv`/`fn` job declares no parameter slots (RECOVERY_PLAN.md section 1.4): a positional argument
    that happens to name a file nobody has written yet is not a path parameter the backfill checks for existence,
    so it is swept clean, launches, and never reaches the owner."""
    for i in range(3):
        _legacy_task(layout, f"T{i}", argv=[PY, "-c", "pass", f"reports/not-written-yet-{i}.md"],
                    created=f"2026-04-{i + 1:02d}T00:00:00Z")
    sup = Supervisor(layout, sleep_s=0)
    while not preflight.backfill_complete(layout):
        sup.preflight_backfill()
    for i in range(3):
        t = tasks_mod.load(layout, f"T{i}")
        assert t.job.preflight.ok is True
        assert gate.check(layout, t, "process", "delivery", running=[]) is None
    assert inbox.open_items(layout, "owner") == []


# ------------------------------------------------------------------- a block that cannot pass

def test_a_block_that_fails_the_backfill_never_launches_and_gets_exactly_one_owner_item(layout, monkeypatch):
    monkeypatch.setattr(preflight, "BACKFILL_BATCH", 1)
    _legacy_task(layout, "T1", argv=BAD_ARGV, created="2026-05-01T00:00:00Z")
    _legacy_task(layout, "T2", argv=BAD_ARGV, created="2026-05-02T00:00:00Z")
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(10):                                # however many turns pass
        sup.preflight_backfill()
        if preflight.backfill_complete(layout):
            break
    items = [i for i in inbox.all_items(layout) if i.get("kind") == "preflight_failed"]
    assert sorted(i["task_id"] for i in items) == ["T1", "T2"]
    assert len(items) == 2                             # one each, never duplicated across turns
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.preflight.ok is False
    assert gate.check(layout, t1, "process", "delivery", running=[]) == \
          "preflight failed (executable_not_allowed): " + f"{BAD_ARGV[0]!r} does not resolve"


# ------------------------------------------------------------------- a block whose task can never launch again

def test_a_terminal_tasks_failing_backfill_is_stored_but_opens_no_owner_item(layout):
    """Shaped like a real store's own worst case: a `COMPLETE` task whose job block names an interpreter from an
    environment that no longer exists (a worktree torn down since, a drive unmounted) -- `pending_backfill`
    does not filter by status, so this is still swept and its failure still stored, but `ALLOWED[COMPLETE]`
    (models/task.py) is empty: the task can never launch again to act on an owner item, so the sweep opens
    none. `gate.check` refuses it on status alone (R-04), never reaching the preflight at all."""
    dead_interpreter = "/no/such/worktree/anymore/.venv/bin/python"
    job = {"adapter": "process", "argv": [dead_interpreter], "preflight": None}
    make_task(layout, "T1", status="COMPLETE", job=job, created="2026-05-10T00:00:00Z")
    sup = Supervisor(layout, sleep_s=0)
    while not preflight.backfill_complete(layout):
        sup.preflight_backfill()
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.preflight is not None and t1.job.preflight.ok is False
    assert gate.check(layout, t1, "process", "delivery", running=[]) == "task is COMPLETE"
    assert [i for i in inbox.all_items(layout) if i.get("kind") == "preflight_failed"] == []
    assert inbox.open_items(layout, "owner") == []


def test_a_cancelled_tasks_failing_backfill_is_stored_with_no_owner_item_and_no_lockout(layout):
    """`CANCELLED` is terminal too (no owner item, same as `COMPLETE`), but `ALLOWED[CANCELLED]` is not empty: it
    can return to `NOT_STARTED` later. It must still be swept now, not skipped, or it would be permanently
    refused (`preflight missing`) once the mark closes the run-on-absent window, with no route back in even
    after it returns to a status that can launch."""
    job = {"adapter": "process", "argv": BAD_ARGV, "preflight": None}
    make_task(layout, "T1", status="CANCELLED", job=job, created="2026-05-11T00:00:00Z")
    sup = Supervisor(layout, sleep_s=0)
    while not preflight.backfill_complete(layout):
        sup.preflight_backfill()
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.preflight is not None and t1.job.preflight.ok is False       # swept, not skipped
    assert [i for i in inbox.all_items(layout) if i.get("kind") == "preflight_failed"] == []     # none while cancelled
    tasks_mod.set_status(layout, "T1", "NOT_STARTED", actor="owner", note="retry")
    t1 = tasks_mod.load(layout, "T1")
    assert gate.check(layout, t1, "process", "delivery", running=[]) == \
          "preflight failed (executable_not_allowed): " + f"{BAD_ARGV[0]!r} does not resolve"    # not "preflight missing"


# ------------------------------------------------------------------- the python lane, not only process

def test_legacy_python_blocks_are_swept_the_same_as_process_ones(layout):
    _legacy_task(layout, "T1", argv=None, adapter="python", fn="noop", created="2026-05-20T00:00:00Z")
    sup = Supervisor(layout, sleep_s=0)
    while not preflight.backfill_complete(layout):
        sup.preflight_backfill()
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.preflight.ok is True
    assert gate.check(layout, t1, "python", "delivery", running=[]) is None
    assert inbox.open_items(layout, "owner") == []


def test_a_legacy_python_block_naming_an_unresolvable_fn_fails_with_exactly_one_owner_item(layout):
    _legacy_task(layout, "T1", argv=None, adapter="python", fn="not-a-registered-fn",
                created="2026-05-21T00:00:00Z")
    sup = Supervisor(layout, sleep_s=0)
    while not preflight.backfill_complete(layout):
        sup.preflight_backfill()
    t1 = tasks_mod.load(layout, "T1")
    assert t1.job.preflight.ok is False
    assert gate.check(layout, t1, "python", "delivery", running=[]) == \
          "preflight failed (fn_unregistered): python job 'not-a-registered-fn' is not registered in JOBS"
    items = [i for i in inbox.all_items(layout) if i.get("kind") == "preflight_failed"]
    assert [i["task_id"] for i in items] == ["T1"]


# ------------------------------------------------------------------- the gate's run-on-absent window (invariant 7)

def test_gate_preflights_an_absent_block_synchronously_before_the_mark(layout):
    _legacy_task(layout, "T1", created="2026-06-01T00:00:00Z")
    assert not preflight.backfill_complete(layout)
    t = tasks_mod.load(layout, "T1")
    assert t.job.preflight is None
    reason = gate.check(layout, t, "process", "delivery", running=[])
    assert reason is None
    assert tasks_mod.load(layout, "T1").job.preflight.ok is True        # the result is stored, not just checked


def test_gate_refuses_and_records_a_failing_absent_block_before_the_mark(layout):
    _legacy_task(layout, "T1", argv=BAD_ARGV, created="2026-06-02T00:00:00Z")
    t = tasks_mod.load(layout, "T1")
    reason = gate.check(layout, t, "process", "delivery", running=[])
    assert reason and "preflight failed" in reason
    assert tasks_mod.load(layout, "T1").job.preflight.ok is False
    items = [i for i in inbox.all_items(layout) if i.get("kind") == "preflight_failed" and i.get("task_id") == "T1"]
    assert len(items) == 1


def test_gate_refuses_an_absent_block_outright_after_the_mark(layout):
    _legacy_task(layout, "T1", created="2026-06-03T00:00:00Z")
    preflight.mark_backfill_complete(layout, swept=0)
    t = tasks_mod.load(layout, "T1")
    reason = gate.check(layout, t, "process", "delivery", running=[])
    assert reason == "preflight missing: write the job block again to record one"
    assert tasks_mod.load(layout, "T1").job.preflight is None           # never backfilled after the mark


def test_a_present_and_failed_preflight_refuses_in_both_periods(layout):
    failed = {"ok": False, "at": "2026-01-01T00:00:00Z", "template": None, "probe_digest": None,
              "detail": {"code": "path_missing", "message": "nope", "block_digest": "x"}}
    make_task(layout, "T1", job={"adapter": "process", "argv": GOOD_ARGV, "preflight": failed})
    t = tasks_mod.load(layout, "T1")
    assert not preflight.backfill_complete(layout)
    assert "preflight failed" in gate.check(layout, t, "process", "delivery", running=[])
    preflight.mark_backfill_complete(layout, swept=0)
    assert "preflight failed" in gate.check(layout, t, "process", "delivery", running=[])


# ------------------------------------------------------------------- the measured cost on a larger store

def test_the_sweeps_measured_cost_on_a_450_task_store(layout):
    """Not a correctness assertion with a timing threshold (flaky by nature) -- the elapsed time is only printed,
    for this step's own release record to quote, with `-s`."""
    n = 450
    for i in range(n):
        _legacy_task(layout, f"T{i}", created=f"2026-07-01T00:{i // 60:02d}:{i % 60:02d}Z")
    sup = Supervisor(layout, sleep_s=0)
    start = time.perf_counter()
    turns = 0
    while not preflight.backfill_complete(layout):
        sup.preflight_backfill()
        turns += 1
    elapsed = time.perf_counter() - start
    print(f"\n450-task backfill: {turns} turns, {elapsed:.3f}s, batch={preflight.BACKFILL_BATCH}")
    assert turns == -(-n // preflight.BACKFILL_BATCH)          # ceil division: exactly this many turns, no more
    assert inbox.open_items(layout, "owner") == []
