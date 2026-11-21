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


def test_choice_naming_an_owner_only_effect_without_its_running_loop_is_refused_at_create(layout):
    make_task(layout, "T1", status="BLOCKED")
    with pytest.raises(inbox.InboxError, match="diagnose_now"):
        inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                    choices=[{"id": "diag", "effect": {"op": "diagnose_now", "task": "T1"}}])


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


def test_legacy_keyword_answer_still_applies_the_matching_choice(layout):
    from supervisor import tasks as tasks_mod
    make_task(layout, "T1", status="BLOCKED")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[{"id": "requeue", "effect": {"op": "resume", "task": "T1", "as": "ready"}}])
    inbox.answer(layout, item["id"], "REQUEUE")
    answered = inbox.load(layout, item["id"])
    assert answered["choice_applied"]["ok"], answered.get("choice_applied")
    assert tasks_mod.load(layout, "T1").status.value == "READY"
