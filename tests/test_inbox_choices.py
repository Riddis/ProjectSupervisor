"""Typed choices on an inbox item (RECOVERY_PLAN.md section 4.1, step 7 of the order of work): an item's
`choices[]` carry the effect a button actually performs, each one checked and its label/detail rendered *from*
that effect before it is ever offered (section 4.2 — never prose a caller writes beside it), `choices_digest`
binds an answer to what was shown, `allowed_responses` is derived rather than stated, and the effect is applied
through the same journalled applier `plans.apply` uses for a plan — a two-operation choice is one transaction,
not two separate writes. No test here touches the plan validator's own refusal codes for their own sake
(test_plans.py owns RF-01..RF-26); this file owns the inbox answer path and the create()-time checks that route
a choice's effect through `plans.validate`/`plans.render_choice`."""
from __future__ import annotations

import pytest

from supervisor import inbox, plans
from tests.conftest import make_task


def _two_op_choice():
    return {"id": "fix_and_requeue",
            "effect": [{"op": "patch_contract", "task": "T1", "fields": {"tests": ["t", "t2"]}},
                       {"op": "resume", "task": "T1", "as": "ready"}]}


def _single_choice(choice_id="cancel"):
    return {"id": choice_id, "effect": {"op": "cancel_task", "task": "T1"}}


def test_choices_digest_and_allowed_responses_are_derived(layout):
    make_task(layout, "T1", status="BLOCKED")
    choices = [_two_op_choice(), _single_choice()]
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1", choices=choices)
    assert [c["id"] for c in item["choices"]] == ["fix_and_requeue", "cancel"]
    assert [c["effect"] for c in item["choices"]] == [c["effect"] for c in choices]
    assert item["choices_digest"] == plans.choices_digest(item["choices"])
    assert set(item["allowed_responses"]) == {"FIX_AND_REQUEUE", "CANCEL"}


def test_choice_label_is_rendered_from_its_own_effect_not_the_caller(layout):
    """The exact gap R-17 names: a caller writing a label that does not describe its own effect must never reach
    the item — the stored label is always plans.render_choice(effect), whatever the caller wrote (if anything)."""
    make_task(layout, "T1", status="BLOCKED")
    misleading = {"id": "cancel", "label": "Re-run the task", "detail": "nothing to see here",
                 "effect": {"op": "cancel_task", "task": "T1"}}
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1", choices=[misleading])
    label, detail = plans.render_choice(misleading["effect"])
    assert item["choices"][0]["label"] == label != "Re-run the task"
    assert item["choices"][0].get("detail") == detail
    assert "cancel" in item["choices"][0]["label"].lower()
    assert "re-run" not in item["choices"][0]["label"].lower()


def test_two_op_choice_label_is_rendered_from_both_operations(layout):
    make_task(layout, "T1", status="BLOCKED")
    choice = _two_op_choice()
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1", choices=[choice])
    label, detail = plans.render_choice(choice["effect"])
    assert item["choices"][0]["label"] == label
    assert item["choices"][0]["detail"] == detail
    assert "; then " in label


def test_a_diagnose_now_choice_is_offered_at_create(layout):
    """Step 10 (RECOVERY_PLAN.md section 9) wires `diagnose_now` through a `loop.Supervisor` built for the one
    call (`inbox._apply_diagnose_now`) — it is an ordinary owner-only effect at create() time now, not refused
    the way an effect with no path to apply at all would be."""
    make_task(layout, "T1", status="BLOCKED")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[{"id": "diag", "effect": {"op": "diagnose_now", "task": "T1"}}])
    assert item["choices"][0]["id"] == "diag"
    assert "diagnos" in item["choices"][0]["label"].lower()


def test_owner_only_choice_missing_its_required_operand_is_refused_at_create(layout):
    make_task(layout, "T1", status="BLOCKED")
    with pytest.raises(inbox.InboxError, match="cancel_task"):
        inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                    choices=[{"id": "cancel", "effect": {"op": "cancel_task"}}])


def test_plan_tier_choice_carrying_an_owner_only_effect_is_refused_rf26_at_create(layout):
    make_task(layout, "T1", status="BLOCKED")
    mixed = {"id": "cancel_and_resume",
            "effect": [{"op": "cancel_task", "task": "T1"}, {"op": "resume", "task": "T1", "as": "ready"}]}
    with pytest.raises(inbox.InboxError, match="RF-26"):
        inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1", choices=[mixed])


def test_choice_applied_failure_is_visible_on_the_item_and_reopens_a_fresh_one(layout):
    """The symptom the finding names ('answering correctly changed nothing') must not survive on the failure
    path: a choice whose effect raises at apply time is recorded ok=False and the owner gets a fresh item."""
    make_task(layout, "T1", status="BLOCKED")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[{"id": "cancel_missing", "effect": {"op": "cancel_task", "task": "T-missing"}}])
    inbox.answer(layout, item["id"], choice="cancel_missing", choices_digest=item["choices_digest"])
    answered = inbox.load(layout, item["id"])
    applied = answered["choice_applied"]
    assert applied["ok"] is False
    assert applied.get("error")
    assert applied.get("reopened")
    reopened = inbox.load(layout, applied["reopened"])
    assert reopened["choices"] == answered["choices"]
    assert reopened["status"] == "open"


def test_two_operation_choice_applies_as_one_journalled_transaction(layout):
    from supervisor import tasks as tasks_mod
    make_task(layout, "T1", status="BLOCKED", owned_paths=["."])
    choices = [_two_op_choice()]
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1", choices=choices)
    inbox.answer(layout, item["id"], choice="fix_and_requeue", choices_digest=item["choices_digest"])
    answered = inbox.load(layout, item["id"])
    assert answered["choice_applied"]["ok"], answered.get("choice_applied")
    t1 = tasks_mod.load(layout, "T1")
    assert t1.status.value == "READY"
    assert "t2" in t1.tests
    # one journal document, both operations recorded done by the same applier a plan uses
    all_plans = plans.all_plans(layout)
    assert len(all_plans) == 1
    assert all_plans[0]["status"] == "applied"
    assert sorted(all_plans[0]["done"]) == [0, 1]


def test_unoffered_choice_is_refused_rf24(layout):
    make_task(layout, "T1", status="BLOCKED")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[_single_choice()])
    with pytest.raises(inbox.InboxError, match="RF-24"):
        inbox.answer(layout, item["id"], choice="not_offered", choices_digest=item["choices_digest"])


def test_stale_choices_digest_is_refused_rf25_and_says_reload(layout):
    make_task(layout, "T1", status="BLOCKED")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[_single_choice()])
    with pytest.raises(inbox.InboxError, match="RF-25") as exc:
        inbox.answer(layout, item["id"], choice="cancel", choices_digest="not-the-shown-digest")
    assert "reload" in str(exc.value)


def test_item_with_no_choices_behaves_exactly_as_today(layout):
    item = inbox.create(layout, for_role="owner", subject="s", summary="body")
    assert "choices" not in item
    assert item["allowed_responses"] == inbox.DEFAULT_RESPONSES
    answered = inbox.answer(layout, item["id"], "ACCEPT")
    assert answered["status"] == "answered"
    assert "choice_applied" not in answered


def test_free_text_still_opens_a_fresh_item_now_carrying_the_same_choices(layout):
    make_task(layout, "T1", status="BLOCKED")
    choices = [_single_choice()]
    item = inbox.create(layout, for_role="owner", subject="Decision needed: T1", summary="pick one",
                        kind=inbox.DECISION_KIND, allowed_responses=None, choices=choices, free_text=True,
                        from_="loop", task_id="T1")
    inbox.answer(layout, item["id"], "please look at this again")
    answered = inbox.load(layout, item["id"])
    assert answered["applied"]["skipped"] == "deferred"
    reopened_id = answered["applied"]["reopened"]
    reopened = inbox.load(layout, reopened_id)
    assert reopened["choices"] == item["choices"]
    assert reopened["choices_digest"] == item["choices_digest"]


def test_a_keyword_answer_carrying_the_shown_digest_still_applies_the_matching_choice(layout):
    """Compatibility (section 4.1): the `inbox.answer` command keeps naming a choice by its derived keyword — but
    on that path too, what makes the answer an effect is the digest of the choices it was shown."""
    from supervisor import tasks as tasks_mod
    make_task(layout, "T1", status="BLOCKED")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[{"id": "requeue", "effect": {"op": "resume", "task": "T1", "as": "ready"}}])
    inbox.answer(layout, item["id"], "REQUEUE", choices_digest=item["choices_digest"])
    answered = inbox.load(layout, item["id"])
    assert answered["choice_applied"]["ok"], answered.get("choice_applied")
    assert answered["answer"]["choice"] == "requeue"
    assert tasks_mod.load(layout, "T1").status.value == "READY"


def test_a_keyword_answer_without_the_shown_digest_is_refused_rf25_and_applies_nothing(layout):
    """The staleness rule holds wherever a choice is named, not only where a `choice` argument is passed: a bare
    keyword answer on an item that showed a digest carries none, so it cannot be bound to what was shown."""
    from supervisor import tasks as tasks_mod
    make_task(layout, "T1", status="BLOCKED")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[{"id": "requeue", "effect": {"op": "resume", "task": "T1", "as": "ready"}}])
    with pytest.raises(inbox.InboxError, match="RF-25") as exc:
        inbox.answer(layout, item["id"], "REQUEUE")
    assert "reload" in str(exc.value)
    assert inbox.load(layout, item["id"])["status"] == "open", "the item was spent by a refused answer"
    assert tasks_mod.load(layout, "T1").status.value == "BLOCKED"


def test_prose_that_merely_begins_with_a_choice_keyword_is_a_comment_never_an_effect(layout):
    """Free text is still never an effect (section 4.1), and `allowed_responses` being derived from the choice ids
    must not turn prose about a choice into that choice: a sentence whose first word happens to be a choice's
    keyword is recorded as a comment, and nothing is applied."""
    from supervisor import tasks as tasks_mod
    make_task(layout, "T1", status="BLOCKED")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[_single_choice()])
    inbox.answer(layout, item["id"], "CANCEL the other one instead", choices_digest=item["choices_digest"])
    answered = inbox.load(layout, item["id"])
    assert answered["answer"]["kind"] == "text"
    assert "choice" not in answered["answer"]
    assert "choice_applied" not in answered
    assert tasks_mod.load(layout, "T1").status.value == "BLOCKED"


def test_the_applier_refuses_an_answer_whose_recorded_digest_no_longer_matches(layout):
    """The digest is checked where the effect is about to be applied, not only where the answer is recorded: an
    answered item whose choices no longer hash to what the answer carried is refused RF-25 by the applier, so no
    path reaches an effect without the check."""
    from supervisor import store
    from supervisor import tasks as tasks_mod
    make_task(layout, "T1", status="BLOCKED")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[_single_choice()])

    def fn(doc):
        doc["status"] = "answered"
        doc["answered_at"] = store.now_iso()
        doc["answer"] = {"response": "cancel", "kind": "CANCEL", "note": "", "by": "owner", "at": store.now_iso(),
                         "choice": "cancel", "choices_digest": "a-digest-this-item-never-showed"}
        return doc

    answered = store.update(inbox.path_for(layout, item["id"]), fn, lock_path=layout.state / ".inbox.lock")
    out = inbox.apply_choice(layout, answered)
    assert out is not None and out["ok"] is False
    assert "RF-25" in out["error"] and "reload" in out["error"]
    assert tasks_mod.load(layout, "T1").status.value == "BLOCKED"


def test_a_reopen_that_itself_fails_still_leaves_the_failure_on_the_answered_item(layout, monkeypatch):
    """The failure of the failure path: the marker is written before the reopen is attempted and the reopen's own
    error is recorded on it, so an answered item never ends up durable with neither an effect nor a record."""
    make_task(layout, "T1", status="BLOCKED")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[{"id": "cancel_missing", "effect": {"op": "cancel_task", "task": "T-absent"}}])

    def refuses(*_a, **_k):
        raise inbox.InboxError("the reopened item does not validate")

    monkeypatch.setattr(inbox, "create", refuses)
    inbox.answer(layout, item["id"], choice="cancel_missing", choices_digest=item["choices_digest"])
    applied = inbox.load(layout, item["id"])["choice_applied"]
    assert applied["ok"] is False
    assert applied.get("error")
    assert "does not validate" in (applied.get("reopen_error") or "")
    assert not applied.get("reopened")


# ------------------------------------------------------------------ the owner-only tier, each effect applied
#
# Section 4.1's second tier is offered as a button only where the answer path can actually apply it, so each
# offerable effect is exercised here through `inbox.answer` end to end — not only in its refusal branch.


def test_the_hold_effect_applies_through_the_answer_path(layout):
    from supervisor import tasks as tasks_mod
    make_task(layout, "T1", status="READY")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[{"id": "hold", "effect": {"op": "hold", "task": "T1"}}])
    inbox.answer(layout, item["id"], choice="hold", choices_digest=item["choices_digest"])
    applied = inbox.load(layout, item["id"])["choice_applied"]
    assert applied["ok"], applied
    task = tasks_mod.load(layout, "T1")
    assert task.held and task.status.value == "BLOCKED"


def test_the_release_hold_effect_applies_through_the_answer_path(layout):
    from supervisor import tasks as tasks_mod
    make_task(layout, "T1", status="BLOCKED")
    tasks_mod.hold(layout, "T1", "owner", note="stopped by the owner")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[{"id": "release_hold", "effect": {"op": "release_hold", "task": "T1"}}])
    inbox.answer(layout, item["id"], choice="release_hold", choices_digest=item["choices_digest"])
    applied = inbox.load(layout, item["id"])["choice_applied"]
    assert applied["ok"], applied
    task = tasks_mod.load(layout, "T1")
    assert not task.held
    assert task.status.value == "BLOCKED", "release_hold moved the task as well as clearing the hold"


def test_the_cancel_task_effect_applies_through_the_answer_path(layout):
    from supervisor import tasks as tasks_mod
    make_task(layout, "T1", status="BLOCKED")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[_single_choice()])
    inbox.answer(layout, item["id"], choice="cancel", choices_digest=item["choices_digest"])
    applied = inbox.load(layout, item["id"])["choice_applied"]
    assert applied["ok"], applied
    assert applied["result"]["status"] == "CANCELLED"
    assert tasks_mod.load(layout, "T1").status.value == "CANCELLED"


def _plan_doc(layout, plan_id, ops):
    from supervisor import tasks as tasks_mod
    return {"plan_schema": plans.PLAN_SCHEMA, "id": plan_id, "subject": {"task": "T1"},
            "reason": "a repair for this task", "expected_rev": {"T1": tasks_mod.load(layout, "T1").rev},
            "operations": ops}


def test_the_apply_plan_effect_applies_the_named_plan_through_the_plan_applier(layout):
    from supervisor import tasks as tasks_mod
    make_task(layout, "T1", status="BLOCKED", owned_paths=["."])
    proposed = plans.propose(layout, _plan_doc(layout, "P1", [{"op": "resume", "task": "T1", "as": "ready"}]))
    assert proposed["status"] == "awaiting_approval", proposed
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[{"id": "apply", "effect": {"op": "apply_plan", "plan": "P1"}}])
    inbox.answer(layout, item["id"], choice="apply", choices_digest=item["choices_digest"])
    applied = inbox.load(layout, item["id"])["choice_applied"]
    assert applied["ok"], applied
    assert plans.load_plan(layout, "P1")["status"] == "applied"
    assert tasks_mod.load(layout, "T1").status.value == "READY"


def test_the_cancel_plan_effect_abandons_a_stuck_plan_through_the_answer_path(layout, monkeypatch):
    make_task(layout, "T1", status="BLOCKED", owned_paths=["."])
    plans.propose(layout, _plan_doc(layout, "P1",
                                    [{"op": "patch_contract", "task": "T1", "fields": {"tests": ["t", "t2"]}},
                                     {"op": "resume", "task": "T1", "as": "ready"}]))

    def always_fails(_layout, _op, _plan_doc):
        raise RuntimeError("permanent failure")

    monkeypatch.setitem(plans._EFFECTS, "patch_contract", always_fails)
    for _ in range(plans.DEFAULT_MAX_RESUMES):
        plans.apply(layout, "P1")
    assert plans.load_plan(layout, "P1")["status"] == "stuck"
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[{"id": "abandon", "effect": {"op": "cancel_plan", "plan": "P1"}}])
    inbox.answer(layout, item["id"], choice="abandon", choices_digest=item["choices_digest"])
    applied = inbox.load(layout, item["id"])["choice_applied"]
    assert applied["ok"], applied
    assert plans.load_plan(layout, "P1")["status"] == "abandoned"
