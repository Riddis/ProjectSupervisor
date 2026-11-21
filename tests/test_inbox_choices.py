"""Typed choices on an inbox item (RECOVERY_PLAN.md section 4.1, step 7 of the order of work): an item's
`choices[]` carry the effect a button actually performs, `choices_digest` binds an answer to what was shown,
`allowed_responses` is derived rather than stated, and the effect is applied through the same journalled applier
`plans.apply` uses for a plan — a two-operation choice is one transaction, not two separate writes. No test here
touches the plan validator's own refusal codes (test_plans.py owns RF-01..RF-26); this file owns only the inbox
answer path that calls the three pure functions `plans.py` already exposes for it."""
from __future__ import annotations

import pytest

from supervisor import inbox, plans
from tests.conftest import make_task


def _two_op_choice():
    return {"id": "fix_and_requeue", "label": "Fix the contract and re-run it",
            "effect": [{"op": "patch_contract", "task": "T1", "fields": {"tests": ["t", "t2"]}},
                       {"op": "resume", "task": "T1", "as": "ready"}],
            "detail": "adds a test and releases the task"}


def _single_choice(choice_id="cancel"):
    return {"id": choice_id, "label": "Cancel the task", "effect": {"op": "cancel_task", "task": "T1"}}


def test_choices_digest_and_allowed_responses_are_derived(layout):
    make_task(layout, "T1", status="BLOCKED")
    choices = [_two_op_choice(), _single_choice()]
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1", choices=choices)
    assert item["choices"] == choices
    assert item["choices_digest"] == plans.choices_digest(choices)
    assert set(item["allowed_responses"]) == {"FIX_AND_REQUEUE", "CANCEL"}


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
    assert reopened["choices"] == choices
    assert reopened["choices_digest"] == item["choices_digest"]


def test_legacy_keyword_answer_still_applies_the_matching_choice(layout):
    from supervisor import tasks as tasks_mod
    make_task(layout, "T1", status="BLOCKED")
    item = inbox.create(layout, for_role="owner", subject="s", summary="body", task_id="T1",
                        choices=[{"id": "requeue", "label": "Re-run it",
                                  "effect": {"op": "resume", "task": "T1", "as": "ready"}}])
    inbox.answer(layout, item["id"], "REQUEUE")
    answered = inbox.load(layout, item["id"])
    assert answered["choice_applied"]["ok"], answered.get("choice_applied")
    assert tasks_mod.load(layout, "T1").status.value == "READY"
