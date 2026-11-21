"""supervisor/plans.py's applier (RECOVERY_PLAN.md section 2.6, step 2 of the order of work): the plan is its own
journal, apply is ordered, idempotent and resumable, resume is always the last operation, and a plan that cannot
finish stops visibly instead of half-applying. No test here touches the validator's own refusal codes (test_plans.py
owns those); every plan here is otherwise clean and exercises the fixed operation order: create, dependency edits,
contract patches, set_job, resume."""
from __future__ import annotations

import pytest

from supervisor import inbox as inbox_mod
from supervisor import plans, store
from supervisor import tasks as tasks_mod
from supervisor import testing
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus

PLAN_ID = "T1.d1"
TEMPLATE_YAML = "version: 1\ntemplates:\n  noop:\n    adapter: process\n    may_be_set_by: [owner, plan]\n    params: {}\n"


def _write_template(layout):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text(TEMPLATE_YAML, encoding="utf-8")


def _setup(layout, rev=1, subject_status="BLOCKED"):
    """T1 (the subject) and a five-operation plan exercising every category of the fixed order, in that same
    order, so each operation's original index equals its category rank: 0 create_task, 1 add_dependency,
    2 patch_contract, 3 set_job (the task this same plan creates, its first job), 4 resume.

    `subject_status` is the subject's own starting status: BLOCKED and FAILED are both first-class here (the
    stall lane dispatches on either, and the validator refuses neither), and both release to READY by an allowed
    transition."""
    from tests.conftest import make_task
    make_task(layout, "T1", status=subject_status, owned_paths=["."])
    _write_template(layout)
    ops = [
        {"op": "create_task", "task": {"id": "T2", "title": "T2", "objective": "o", "owner_role": "engineer",
                                        "cost_category": "deterministic", "artifacts": ["x"], "tests": ["t"],
                                        "actions": ["a"], "acceptance": "ok"}},
        {"op": "add_dependency", "task": "T1", "depends_on": ["T2"]},
        {"op": "patch_contract", "task": "T1", "fields": {"tests": ["t", "t2"]}},
        {"op": "set_job", "task": "T2", "template": "noop", "params": {}},
        {"op": "resume", "task": "T1", "as": "ready"},
    ]
    return {"plan_schema": 1, "id": PLAN_ID, "subject": {"task": "T1"}, "reason": "repair this task",
            "expected_rev": {"T1": rev}, "operations": ops}


def _release_entries(layout, task_id="T1"):
    """The subject's own history entries written by the applier's release: the only durable proof that this
    plan's `resume` operation actually ran, which the subject's current status cannot carry."""
    return [h for h in tasks_mod.load(layout, task_id).history
            if h.get("by") == "plan" and h.get("note") == f"released by plan {PLAN_ID}"]


def test_propose_writes_refused_status_for_an_invalid_plan(layout):
    from tests.conftest import make_task
    make_task(layout, "T1", status="BLOCKED")
    doc = {"plan_schema": 1, "id": "T1.bad", "subject": {"task": "T1"}, "reason": "x",
           "expected_rev": {"T1": 1}, "operations": [{"op": "bogus", "task": "T1"}]}
    result = plans.propose(layout, doc)
    assert result["status"] == "refused"
    assert any(r["code"] == "RF-01" for r in result["refusals"])
    assert plans.apply(layout, "T1.bad") == result             # terminal: a no-op, never half-applied


def test_apply_raises_for_an_unknown_plan(layout):
    with pytest.raises(plans.PlanError):
        plans.apply(layout, "nope")


def test_apply_twice_is_a_noop(layout):
    doc = _setup(layout)
    plans.propose(layout, doc)
    first = plans.apply(layout, PLAN_ID)
    assert first["status"] == "applied"
    before = store.read(plans.path_for(layout, PLAN_ID))

    second = plans.apply(layout, PLAN_ID)
    after = store.read(plans.path_for(layout, PLAN_ID))
    assert second["status"] == "applied"
    assert before == after, "apply() on an already-applied plan rewrote the journal"


@pytest.mark.parametrize("op_name,op_index", [("create_task", 0), ("add_dependency", 1), ("patch_contract", 2),
                                              ("set_job", 3), ("resume", 4)])
def test_a_crash_between_every_pair_of_operations_leaves_the_subject_stopped_and_resumes(
        layout, monkeypatch, op_name, op_index):
    """A crash (here: the operation's own effect raising once) between the previous operation's journal write and
    this one's leaves `done` exactly where it was and the subject exactly where it already was — never half
    released. The next turn (a second call to apply()) finishes the plan from the journal."""
    doc = _setup(layout)
    plans.propose(layout, doc)
    real = plans._EFFECTS[op_name]
    calls = {"n": 0}

    def flaky(layout_, op, plan_doc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return real(layout_, op, plan_doc)
    monkeypatch.setitem(plans._EFFECTS, op_name, flaky)

    first = plans.apply(layout, PLAN_ID)
    assert first["status"] == "applying"
    assert sorted(first["done"]) == list(range(op_index)), "an operation after the crash point was journalled"
    assert tasks_mod.load(layout, "T1").status == TaskStatus.BLOCKED, "the subject moved on a half-applied plan"

    second = plans.apply(layout, PLAN_ID)
    assert second["status"] == "applied"
    assert sorted(second["done"]) == [0, 1, 2, 3, 4]
    assert tasks_mod.load(layout, "T1").status == TaskStatus.READY, "resume did not run last turn finished it"
    assert tasks_mod.load(layout, "T2").id == "T2"


def test_stale_expected_rev_refuses_with_rf19_before_the_first_mutation(layout):
    doc = _setup(layout)
    plans.propose(layout, doc)
    tasks_mod.add_note(layout, "T1", "an unrelated change", actor="owner")   # moves T1's rev under the plan
    result = plans.apply(layout, PLAN_ID)
    assert result["status"] == "refused"
    assert any(r["code"] == "RF-19" for r in result["refusals"])
    assert not (layout.tasks / "T2.yaml").exists(), "create_task ran despite the stale rev"
    assert tasks_mod.load(layout, "T1").status == TaskStatus.BLOCKED


def test_stuck_after_max_resumes_opens_one_owner_item_and_rolls_back_nothing(layout, monkeypatch):
    doc = _setup(layout)
    plans.propose(layout, doc)

    def always_fails(layout_, op, plan_doc):
        raise RuntimeError("permanent failure")
    monkeypatch.setitem(plans._EFFECTS, "patch_contract", always_fails)

    result = None
    for _ in range(plans.DEFAULT_MAX_RESUMES):
        result = plans.apply(layout, PLAN_ID)

    assert result["status"] == "stuck"
    assert sorted(result["done"]) == [0, 1], "an operation past the failing one was journalled"
    open_owner_items = [i for i in inbox_mod.all_items(layout) if i.get("status") == "open" and i.get("for") == "owner"]
    assert len(open_owner_items) == 1
    item = open_owner_items[0]
    assert PLAN_ID in item["summary"]
    assert "patch_contract" in item["summary"]
    assert tasks_mod.load(layout, "T2").id == "T2"                      # the completed create_task stays applied
    assert "T2" in tasks_mod.load(layout, "T1").depends_on              # the completed add_dependency stays applied
    assert tasks_mod.load(layout, "T1").status == TaskStatus.BLOCKED    # resume never ran — nothing released

    # a second round of failing attempts does not open a second item (dedup_key)
    for _ in range(plans.DEFAULT_MAX_RESUMES):
        plans.apply(layout, PLAN_ID)
    still_open = [i for i in inbox_mod.all_items(layout) if i.get("status") == "open" and i.get("for") == "owner"]
    assert len(still_open) == 1

    cancelled = plans.cancel_plan(layout, PLAN_ID)
    assert cancelled["status"] == "abandoned"
    assert sorted(cancelled["done"]) == [0, 1]
    assert tasks_mod.load(layout, "T2").id == "T2"
    assert "T2" in tasks_mod.load(layout, "T1").depends_on

    # cancel_plan is a no-op from here; apply() on an abandoned plan does nothing
    assert plans.apply(layout, PLAN_ID)["status"] == "abandoned"


def test_cancel_plan_refuses_off_a_plan_that_is_not_stuck(layout):
    doc = _setup(layout)
    plans.propose(layout, doc)
    with pytest.raises(plans.PlanError):
        plans.cancel_plan(layout, PLAN_ID)


def test_replayed_propose_and_apply_applies_the_plan_once(layout):
    doc = _setup(layout)
    first_propose = plans.propose(layout, doc)
    replay_propose = plans.propose(layout, doc)
    assert replay_propose == first_propose, "a second propose() of the same id re-validated or rewrote the plan"

    applied = plans.apply(layout, PLAN_ID)
    assert applied["status"] == "applied"

    replay_propose_after = plans.propose(layout, doc)
    assert replay_propose_after["status"] == "applied"
    replay_apply = plans.apply(layout, PLAN_ID)
    assert replay_apply["status"] == "applied"

    t1 = tasks_mod.load(layout, "T1")
    assert t1.depends_on.count("T2") == 1
    assert t1.tests.count("t2") == 1
    assert t1.status == TaskStatus.READY


def test_resume_plans_sweep_continues_an_applying_plan(layout, monkeypatch):
    doc = _setup(layout)
    plans.propose(layout, doc)
    real = plans._EFFECTS["resume"]
    calls = {"n": 0}

    def flaky(layout_, op, plan_doc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return real(layout_, op, plan_doc)
    monkeypatch.setitem(plans._EFFECTS, "resume", flaky)

    plans.apply(layout, PLAN_ID)                                         # stops with resume still pending
    assert store.read(plans.path_for(layout, PLAN_ID))["status"] == "applying"

    acted = plans.resume_plans(layout)
    assert acted == [PLAN_ID]
    assert store.read(plans.path_for(layout, PLAN_ID))["status"] == "applied"
    assert tasks_mod.load(layout, "T1").status == TaskStatus.READY


def test_resume_plans_leaves_a_stuck_plan_for_the_owner(layout, monkeypatch):
    doc = _setup(layout)
    plans.propose(layout, doc)

    def always_fails(layout_, op, plan_doc):
        raise RuntimeError("permanent failure")
    monkeypatch.setitem(plans._EFFECTS, "patch_contract", always_fails)
    for _ in range(plans.DEFAULT_MAX_RESUMES):
        plans.apply(layout, PLAN_ID)
    assert store.read(plans.path_for(layout, PLAN_ID))["status"] == "stuck"

    acted = plans.resume_plans(layout)
    assert acted == [], "the sweep auto-retried a stuck plan instead of leaving it for the owner"
    assert store.read(plans.path_for(layout, PLAN_ID))["status"] == "stuck"


def test_the_loops_own_turn_resumes_an_applying_plan(layout, monkeypatch):
    doc = _setup(layout)
    plans.propose(layout, doc)
    real = plans._EFFECTS["resume"]
    calls = {"n": 0}

    def flaky(layout_, op, plan_doc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return real(layout_, op, plan_doc)
    monkeypatch.setitem(plans._EFFECTS, "resume", flaky)

    plans.apply(layout, PLAN_ID)                                         # stops with resume still pending
    assert store.read(plans.path_for(layout, PLAN_ID))["status"] == "applying"

    Supervisor(layout, sleep_s=0).once()
    assert store.read(plans.path_for(layout, PLAN_ID))["status"] == "applied"
    assert tasks_mod.load(layout, "T1").status == TaskStatus.READY


def test_a_full_owner_inbox_does_not_crash_apply_and_the_plan_still_goes_stuck(layout, monkeypatch):
    """F1: `_open_stuck_item`'s own `inbox.create` raises `InboxFull` whenever the owner's three-item cap
    (`inbox.MAX_OPEN_PER_ROLE`) is already spent -- a routine condition `loop.py` catches everywhere else it opens
    an item. `apply()` and `resume_plans()` must still reach `stuck` and never let that exception out; the item
    opens on a later sweep, once the inbox has room."""
    doc = _setup(layout)
    plans.propose(layout, doc)
    for i in range(inbox_mod.MAX_OPEN_PER_ROLE):
        inbox_mod.create(layout, for_role="owner", subject=f"unrelated {i}", summary=f"unrelated {i}", detail="",
                         kind="question", from_="test")

    def always_fails(layout_, op, plan_doc):
        raise RuntimeError("permanent failure")
    monkeypatch.setitem(plans._EFFECTS, "patch_contract", always_fails)

    result = None
    for _ in range(plans.DEFAULT_MAX_RESUMES):
        result = plans.apply(layout, PLAN_ID)              # must never raise InboxFull
    assert result["status"] == "stuck", "a full owner inbox stopped the plan from reaching stuck"
    assert not result.get("owner_item"), "an item was opened despite the cap being spent"

    acted = plans.resume_plans(layout)                      # the sweep retries the item; still must not raise
    assert acted == []
    still = store.read(plans.path_for(layout, PLAN_ID))
    assert still["status"] == "stuck"
    assert not still.get("owner_item")

    for item in inbox_mod.open_items(layout, "owner"):
        inbox_mod.answer(layout, item["id"], "ACCEPT", by="owner")        # the owner clears the inbox
    plans.resume_plans(layout)
    final = store.read(plans.path_for(layout, PLAN_ID))
    assert final.get("owner_item"), "the owner item was never opened once the inbox had room"
    open_owner_items = inbox_mod.open_items(layout, "owner")
    assert len(open_owner_items) == 1
    assert open_owner_items[0]["id"] == final["owner_item"]


def test_a_subject_held_after_promotion_is_never_released_by_resume(layout, monkeypatch):
    """F2: RF-22 is only re-checked once, at awaiting_approval -> applying. A Hold placed afterwards -- the
    ordinary case for a plan `resume_plans` continues on a later turn, or a stuck plan the owner retries by hand
    -- must still stop the release, and the applier must never clear the Hold itself (`resume`'s own
    `actor="plan"` is outside `tasks.AUTOMATIC_ACTORS`, which is otherwise exactly what `set_status` would do)."""
    doc = _setup(layout)
    plans.propose(layout, doc)
    real = plans._EFFECTS["resume"]
    calls = {"n": 0}

    def flaky(layout_, op, plan_doc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return real(layout_, op, plan_doc)
    monkeypatch.setitem(plans._EFFECTS, "resume", flaky)

    plans.apply(layout, PLAN_ID)                              # stops with resume still pending
    tasks_mod.hold(layout, "T1", actor="owner", note="owner wants to look first")

    result = plans.apply(layout, PLAN_ID)
    assert result["status"] in ("applying", "stuck"), "resume released a subject the owner now holds"
    t1 = tasks_mod.load(layout, "T1")
    assert t1.status == TaskStatus.BLOCKED, "resume moved a held subject off BLOCKED"
    assert t1.held is not None, "the applier cleared a Hold it has no authority to release"


def test_a_crash_after_resumes_effect_succeeds_before_done_is_persisted(layout, monkeypatch):
    """F3: the window inside `resume`'s own effect, between the subject already moving to READY and `done`'s
    index being persisted. If the scheduler claims the now-READY subject before the retry runs (IN_PROGRESS by
    the time apply() is called again), replaying `resume` must find the subject already past the state it would
    have released it to, not try to force it back to READY and fail."""
    doc = _setup(layout)
    plans.propose(layout, doc)
    real_append = plans._append_done
    calls = {"n": 0}

    def flaky_append(layout_, plan_id_, idx):
        if idx == 4:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash after the effect ran, before the journal caught up")
        return real_append(layout_, plan_id_, idx)
    monkeypatch.setattr(plans, "_append_done", flaky_append)

    first = plans.apply(layout, PLAN_ID)
    assert sorted(first["done"]) == [0, 1, 2, 3]
    assert tasks_mod.load(layout, "T1").status == TaskStatus.READY, "resume's own effect did not run"

    tasks_mod.set_status(layout, "T1", TaskStatus.IN_PROGRESS, actor="loop")   # the scheduler claims it first

    second = plans.apply(layout, PLAN_ID)
    assert second["status"] == "applied", "a plan whose every operation already landed was recorded as stalled"
    assert sorted(second["done"]) == [0, 1, 2, 3, 4]
    assert tasks_mod.load(layout, "T1").status == TaskStatus.IN_PROGRESS, "resume forced the subject backwards"


def test_a_failed_subject_is_released_like_a_blocked_one(layout):
    """F9: FAILED is the other first-class subject status for this capability -- the stall lane dispatches on
    FAILED as well as BLOCKED, the validator refuses neither, and FAILED -> READY is an allowed transition the
    framework's own repair path already makes. A plan reaching `applied` with its subject still FAILED is exactly
    the silent finish this step exists to eliminate: every operation ran, nothing stalled, nothing is visible."""
    doc = _setup(layout, subject_status="FAILED")
    plans.propose(layout, doc)

    result = plans.apply(layout, PLAN_ID)
    assert result["status"] == "applied"
    assert sorted(result["done"]) == [0, 1, 2, 3, 4]
    assert tasks_mod.load(layout, "T1").status == TaskStatus.READY, "the plan reported applied, subject unreleased"
    assert len(_release_entries(layout)) == 1, "no release was recorded against the subject"


def test_a_crash_before_the_journal_catches_up_on_a_failed_subject_replays_as_a_noop(layout, monkeypatch):
    """F9's crash row: the same window as the BLOCKED case (`resume`'s effect succeeds, `done` never catches up,
    the scheduler then claims the now-READY subject) over a FAILED subject. The retry must find this plan's own
    release already recorded and be a no-op, and it must not record a second release."""
    doc = _setup(layout, subject_status="FAILED")
    plans.propose(layout, doc)
    real_append = plans._append_done
    calls = {"n": 0}

    def flaky_append(layout_, plan_id_, idx):
        if idx == 4:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash after the effect ran, before the journal caught up")
        return real_append(layout_, plan_id_, idx)
    monkeypatch.setattr(plans, "_append_done", flaky_append)

    first = plans.apply(layout, PLAN_ID)
    assert sorted(first["done"]) == [0, 1, 2, 3]
    assert tasks_mod.load(layout, "T1").status == TaskStatus.READY, "resume's own effect did not run"

    tasks_mod.set_status(layout, "T1", TaskStatus.IN_PROGRESS, actor="loop")   # the scheduler claims it first

    second = plans.apply(layout, PLAN_ID)
    assert second["status"] == "applied", "a plan whose every operation already landed was recorded as stalled"
    assert sorted(second["done"]) == [0, 1, 2, 3, 4]
    assert tasks_mod.load(layout, "T1").status == TaskStatus.IN_PROGRESS, "resume forced the subject backwards"
    assert len(_release_entries(layout)) == 1, "the replayed release wrote a second history entry"


def test_a_subject_another_actor_moved_off_blocked_is_not_treated_as_released(layout, monkeypatch):
    """F9's second shape: `resume`'s effect never ran at all (it crashed), and some other actor then moved the
    subject off BLOCKED. The subject's status alone cannot tell that apart from this plan's own release having
    already landed -- only the subject's history can -- and a plan whose last operation never happened must stop
    visibly rather than report `applied`."""
    doc = _setup(layout)
    plans.propose(layout, doc)
    real = plans._EFFECTS["resume"]
    calls = {"n": 0}

    def flaky(layout_, op, plan_doc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return real(layout_, op, plan_doc)
    monkeypatch.setitem(plans._EFFECTS, "resume", flaky)

    plans.apply(layout, PLAN_ID)                              # stops with resume still pending, nothing released
    tasks_mod.set_status(layout, "T1", TaskStatus.IN_PROGRESS, actor="loop")   # another actor claims the subject

    result = plans.apply(layout, PLAN_ID)
    assert result["status"] != "applied", "a plan reported applied though its release never ran"
    assert 4 not in (result.get("done") or []), "resume was journalled without ever running"
    assert _release_entries(layout) == [], "a release was recorded that never happened"
    assert (result.get("last_error") or {}).get("op") == "resume", "the stall does not name the release"


@pytest.mark.parametrize("tampered", [[], [4, 0, 1, 2, 3]], ids=["truncated", "release_first"])
def test_a_stored_order_the_applier_disagrees_with_stops_the_plan(layout, monkeypatch, tampered):
    """F4: the execution order was computed once at promotion and then trusted on every later call. A truncated
    one reaches `applied` with operations never attempted -- a silent finish, not a visible stop -- and one with
    the release moved to the front would release the subject before the work it releases has landed. The order is
    re-derived from the operations and cross-checked on every call, and a disagreement stops the plan instead of
    being executed. (`state/plans/` is written only by the applier today, so this stands in for a partial write
    rather than a reachable input.)"""
    doc = _setup(layout)
    plans.propose(layout, doc)
    real = plans._EFFECTS["create_task"]
    calls = {"n": 0}

    def flaky(layout_, op, plan_doc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return real(layout_, op, plan_doc)
    monkeypatch.setitem(plans._EFFECTS, "create_task", flaky)

    plans.apply(layout, PLAN_ID)                              # promoted to applying; nothing journalled yet
    assert store.read(plans.path_for(layout, PLAN_ID))["status"] == "applying"
    store.update(plans.path_for(layout, PLAN_ID), lambda d: {**d, "order": list(tampered)})

    result = plans.apply(layout, PLAN_ID)
    assert result["status"] != "applied", "the plan finished on an order the applier never computed"
    assert sorted(result.get("done") or []) == [], "an operation ran off an order the applier never computed"
    assert not (layout.tasks / "T2.yaml").exists()
    assert tasks_mod.load(layout, "T1").status == TaskStatus.BLOCKED, "the release ran before the work it releases"
    assert _release_entries(layout) == []


def test_a_failing_log_write_in_the_stall_chain_never_escapes_apply(layout, monkeypatch):
    """The residual on F1: three `tasks.log` lines in the stall chain sat outside the try blocks that contain
    everything else, and `log()` does its own file I/O on a lock that can time out. Each sits after its own status
    write has already landed, so a lost line costs the record and never the plan -- but it would still traceback
    out of a hand-run apply."""
    doc = _setup(layout)
    plans.propose(layout, doc)

    def always_fails(layout_, op, plan_doc):
        raise RuntimeError("permanent failure")
    monkeypatch.setitem(plans._EFFECTS, "patch_contract", always_fails)
    real_log = tasks_mod.log
    stall_events = {"plan_apply_stalled", "plan_stuck", "plan_stuck_item_opened"}

    def flaky_log(layout_, event, actor, **fields):
        if event in stall_events:
            raise RuntimeError("the log's own lock timed out")
        return real_log(layout_, event, actor, **fields)
    monkeypatch.setattr(tasks_mod, "log", flaky_log)

    result = None
    for _ in range(plans.DEFAULT_MAX_RESUMES):
        result = plans.apply(layout, PLAN_ID)                 # must never raise
    assert result["status"] == "stuck", "a failing log line stopped the plan from reaching stuck"
    assert result.get("owner_item"), "the owner item was lost with the log line that records it"
    assert sorted(result["done"]) == [0, 1]


def test_the_control_center_plans_page_shows_the_current_status(layout):
    doc = _setup(layout)
    plans.propose(layout, doc)
    client = testing.ui_client(layout)

    listing = client.get("/plans")
    assert listing.status_code == 200
    assert PLAN_ID in listing.text

    detail = client.get(f"/plans/{PLAN_ID}")
    assert detail.status_code == 200
    assert "awaiting_approval" in detail.text

    plans.apply(layout, PLAN_ID)
    detail = client.get(f"/plans/{PLAN_ID}")
    assert "applied" in detail.text

    missing = client.get("/plans/nope")
    assert missing.status_code == 404
