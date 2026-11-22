"""RECOVERY_PLAN.md section 5, D-027 decision 12: `contract_rev` is a generation counter distinct from `rev` —
`rev` (`store.update`) answers "did this document move", `contract_rev` answers "did the brief move". It moves
only when a CONTRACT_FIELDS value actually changes, and exists to close a gap the recovery plan's own first
no-human-in-the-loop contract writer would otherwise open: a plan may tighten a contract (RECOVERY_PLAN.md
section 2.4), and without this field an ACCEPT recorded under the looser one would still count afterwards."""
from __future__ import annotations

from supervisor import jobs, packets, tasks
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_loop import ptask

# ------------------------------------------------------------------ what moves contract_rev, and what does not

def test_status_change_attempt_entry_and_note_move_rev_not_contract_rev(layout):
    make_task(layout, "A")
    t0 = tasks.load(layout, "A")
    crev0 = t0.contract_rev

    tasks.set_status(layout, "A", "IN_PROGRESS")
    t1 = tasks.load(layout, "A")
    assert t1.rev > t0.rev and t1.contract_rev == crev0

    tasks.allocate_job(layout, "A", "delivery", new_attempt=True)
    t2 = tasks.load(layout, "A")
    assert t2.rev > t1.rev and t2.contract_rev == crev0

    tasks.add_note(layout, "A", "a note", actor="owner")
    t3 = tasks.load(layout, "A")
    assert t3.rev > t2.rev and t3.contract_rev == crev0


def test_adding_a_test_moves_contract_rev(layout):
    make_task(layout, "A", tests=["existing"], owned_paths=["."])
    before = tasks.load(layout, "A")

    tasks.patch_task(layout, "A", {"tests": ["existing", "a new one"]}, actor="plan")

    after = tasks.load(layout, "A")
    assert after.contract_rev == before.contract_rev + 1
    assert after.rev > before.rev


def test_a_patch_that_changes_no_value_moves_neither(layout):
    make_task(layout, "A", owned_paths=["."])
    before = tasks.load(layout, "A")

    tasks.patch_task(layout, "A", {"title": before.title}, actor="owner")   # resends the value already stored

    after = tasks.load(layout, "A")
    assert after.rev == before.rev, "nothing changed: not even rev should move"
    assert after.contract_rev == before.contract_rev


# ------------------------------------------------------------------ the judged-set supersession rule

def _delivered_and_reviewed(layout, role="chief_architect"):
    make_task(layout, "A", reviewer=[role], tests=[], owned_paths=["."])
    tasks.set_status(layout, "A", "IN_PROGRESS")
    digest = "a" * 64
    tasks.record_delivery(layout, "A", digest, paths=[])
    tasks.record_verification(layout, "A", digest, True)
    tasks.advance(layout, "A")
    assert tasks.load(layout, "A").status == TaskStatus.WAITING_FOR_REVIEW
    tasks.record_review(layout, "A", digest, role, "ACCEPT")
    return digest, role


def test_an_accept_against_an_older_contract_rev_is_superseded_when_a_judged_field_changes(layout):
    _, role = _delivered_and_reviewed(layout)
    t = tasks.load(layout, "A")
    assert not t.delivery.reviews[-1].superseded
    assert tasks.completion_check(t) is None, "the recorded ACCEPT already satisfies completion"

    tasks.patch_task(layout, "A", {"tests": ["a new criterion"]}, actor="plan")   # tests is a judged field

    t2 = tasks.load(layout, "A")
    assert t2.delivery.reviews[-1].superseded, "a judged-set change must supersede the recorded ACCEPT"
    assert tasks.completion_check(t2) == f"awaiting review from {role}"


def test_an_accept_against_an_older_contract_rev_stands_when_only_actions_changes(layout):
    _, _ = _delivered_and_reviewed(layout)
    before_rev = tasks.load(layout, "A").contract_rev

    tasks.patch_task(layout, "A", {"actions": ["do something else"]}, actor="plan")   # not a judged field

    t2 = tasks.load(layout, "A")
    assert t2.contract_rev == before_rev + 1, "actions is still a contract field: contract_rev moves"
    assert not t2.delivery.reviews[-1].superseded, "actions is outside the judged set: the ACCEPT still stands"
    assert tasks.completion_check(t2) is None


def test_a_verification_is_never_superseded_by_a_contract_change(layout):
    digest, _ = _delivered_and_reviewed(layout)

    tasks.patch_task(layout, "A", {"tests": ["a new criterion"]}, actor="plan")

    t2 = tasks.load(layout, "A")
    v = t2.delivery.verification
    assert v is not None and v.digest == digest and v.ok, (
        "a contract change must never touch the recorded verification: it re-runs the CURRENT checks against the "
        "delivered bytes every time a digest is verified, so only a stale review's reading of the contract goes "
        "stale, never the verification itself"
    )


# ------------------------------------------------------------------ what reads it

def test_the_job_record_and_the_delivery_brief_state_the_contract_rev_they_were_built_from(layout):
    ptask(layout, "A")
    tasks.patch_task(layout, "A", {"tests": ["something new"]}, actor="plan")
    task = tasks.load(layout, "A")
    assert task.contract_rev == 2

    assert f"Contract rev: {task.contract_rev}" in packets.delivery_brief(layout, task)

    Supervisor(layout, sleep_s=0).once()
    rec = jobs.read(layout, "A.a1.s1")
    assert rec.get("contract_rev") == 2
