"""`contract_rev` is a generation counter distinct from `rev` —
`rev` (`store.update`) answers "did this document move", `contract_rev` answers "did the brief move". It moves
only when a CONTRACT_FIELDS value actually changes, and exists to close a gap the first contract writer that
needs no human in the loop would otherwise open: a plan may tighten a contract, and without this field an ACCEPT
recorded under the looser one would still count afterwards."""
from __future__ import annotations

from supervisor import commands, jobs, packets, tasks
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
    assert tasks._evaluate(t)[0] == TaskStatus.COMPLETE, "the recorded ACCEPT already satisfies completion"

    tasks.patch_task(layout, "A", {"tests": ["a new criterion"]}, actor="plan")   # tests is a judged field

    t2 = tasks.load(layout, "A")
    assert t2.delivery.reviews[-1].superseded, "a judged-set change must supersede the recorded ACCEPT"
    assert tasks._evaluate(t2) == (TaskStatus.WAITING_FOR_REVIEW, f"awaiting review from {role}")


def test_an_accept_against_an_older_contract_rev_stands_when_only_actions_changes(layout):
    _, _ = _delivered_and_reviewed(layout)
    before_rev = tasks.load(layout, "A").contract_rev

    tasks.patch_task(layout, "A", {"actions": ["do something else"]}, actor="plan")   # not a judged field

    t2 = tasks.load(layout, "A")
    assert t2.contract_rev == before_rev + 1, "actions is still a contract field: contract_rev moves"
    assert not t2.delivery.reviews[-1].superseded, "actions is outside the judged set: the ACCEPT still stands"
    assert tasks._evaluate(t2)[0] == TaskStatus.COMPLETE


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


# ------------------------------------------------------------------ revision round: every stored writer of a
# CONTRACT_FIELDS value reaches the one bump site (F1), the diff ignores an absent key vs its own default (F2),
# a judged-field change supersedes a live approval exactly as it does a review (F3), and a review a contract
# change superseded still reaches the next review packet (F4)

def test_apply_jobs_cost_category_write_moves_contract_rev(layout):
    """`commands.apply_job` writes `cost_category` (a CONTRACT_FIELDS member) through `tasks._update`, never
    through `_apply_patch` — it must still reach `_bump_contract_rev`, not only a `patch_task` call."""
    make_task(layout, "A", cost_category="deterministic")
    before = tasks.load(layout, "A")

    commands.apply_job(layout, "A", {"adapter": "process", "argv": ["ok"]}, "owner", cost_category="codex")

    after = tasks.load(layout, "A")
    assert after.cost_category.value == "codex"
    assert after.contract_rev == before.contract_rev + 1


def test_migrate_ownerships_owned_paths_write_moves_contract_rev(layout):
    """`migrate_ownership` writes `owned_paths` (a CONTRACT_FIELDS member) through `tasks._update` too — the
    same bypass as the case above, a different writer, reused from
    test_execution_safeguards.test_migrate_ownership_makes_live_writing_tasks_explicitly_exclusive's own setup."""
    make_task(layout, "A")   # conftest.make_task's default cost_category ("claude") is a writing lane
    before = tasks.load(layout, "A")

    out = tasks.migrate_ownership(layout)

    assert out["migrated"] == ["A"]
    after = tasks.load(layout, "A")
    assert after.owned_paths == ["."]
    assert after.contract_rev == before.contract_rev + 1


def test_a_plans_set_job_effect_never_moves_contract_rev(layout):
    """`job` is deliberately outside CONTRACT_FIELDS (it names what a task runs, not what it is judged against):
    `plans._effect_set_job`'s own writer, `tasks._update`, must not bump the counter for it, the declared
    exclusion rather than a silent gap."""
    make_task(layout, "A", job={"adapter": "process", "argv": ["before"]})
    before = tasks.load(layout, "A")

    def fn(doc):
        doc["job"] = {"adapter": "process", "argv": ["after"]}
    tasks._update(layout, "A", fn)

    after = tasks.load(layout, "A")
    assert after.job.argv == ["after"]
    assert after.contract_rev == before.contract_rev


def test_patch_resending_an_absent_fields_own_default_moves_neither(layout):
    """F2: `make_task` never sets `checks`, so the stored document carries no `checks` key at all. Patching it
    to the schema's own default (`[]`) resends the value already in force — comparing raw `doc.get("checks")`
    (`None`) against the sent `[]` would misread this as a change and wrongly bump/supersede."""
    from supervisor import store
    make_task(layout, "A", reviewer=["chief_architect"], tests=[], owned_paths=["."])
    raw = store.read(tasks.path_for(layout, "A"))
    assert "checks" not in raw, "the premise: a task written without a checks key at all"
    digest = "d" * 64
    tasks.set_status(layout, "A", "IN_PROGRESS")
    tasks.record_delivery(layout, "A", digest, paths=[])
    tasks.record_verification(layout, "A", digest, True)
    tasks.advance(layout, "A")
    tasks.record_review(layout, "A", digest, "chief_architect", "ACCEPT")
    before = tasks.load(layout, "A")

    tasks.patch_task(layout, "A", {"checks": []}, actor="plan")

    after = tasks.load(layout, "A")
    assert after.rev == before.rev, "resending a field's own default moved nothing: not even rev should move"
    assert after.contract_rev == before.contract_rev
    assert not after.delivery.reviews[-1].superseded


def test_a_judged_field_change_also_supersedes_a_live_approval(layout):
    """F3: `record_delivery` already supersedes reviews AND approvals on a new digest; a judged-field contract
    change must do the same, or a human_approval task's owner ACCEPT, given under a looser contract, keeps
    counting after the bar tightens — the exact gap this task exists to close, for the approval lane too."""
    make_task(layout, "A", reviewer=["chief_architect"], tests=[], owned_paths=["."], human="required")
    digest = "e" * 64
    tasks.set_status(layout, "A", "IN_PROGRESS")
    tasks.record_delivery(layout, "A", digest, paths=[])
    tasks.record_verification(layout, "A", digest, True)
    tasks.advance(layout, "A")
    tasks.record_review(layout, "A", digest, "chief_architect", "ACCEPT")
    tasks.record_approval(layout, "A", digest, "ACCEPT", by="owner")
    t = tasks.load(layout, "A")
    assert not t.delivery.approvals[-1].superseded

    tasks.patch_task(layout, "A", {"tests": ["a new criterion"]}, actor="plan")   # tests is a judged field

    t2 = tasks.load(layout, "A")
    assert t2.delivery.approvals[-1].superseded, "a judged-set change must supersede the recorded owner ACCEPT too"
    assert t2.delivery.approvals[-1].superseded_contract_rev == t2.contract_rev
    assert t2.delivery.approvals[-1].superseded_fields == ["tests"]


def test_an_approval_stands_when_only_actions_changes(layout):
    """Parity's other half: `actions` is a contract field outside the judged set, so it must leave a recorded
    owner ACCEPT standing, exactly as it already leaves a review's ACCEPT standing."""
    make_task(layout, "A", reviewer=["chief_architect"], tests=[], owned_paths=["."], human="required")
    digest = "e2" + "0" * 62
    tasks.set_status(layout, "A", "IN_PROGRESS")
    tasks.record_delivery(layout, "A", digest, paths=[])
    tasks.record_verification(layout, "A", digest, True)
    tasks.advance(layout, "A")
    tasks.record_review(layout, "A", digest, "chief_architect", "ACCEPT")
    tasks.record_approval(layout, "A", digest, "ACCEPT", by="owner")

    tasks.patch_task(layout, "A", {"actions": ["do something else"]}, actor="plan")   # not a judged field

    t2 = tasks.load(layout, "A")
    assert not t2.delivery.approvals[-1].superseded, "actions is outside the judged set: the ACCEPT still stands"


def test_a_contract_superseded_review_still_appears_in_the_next_review_packet(layout):
    """F4: `_prior_rounds` groups by digest and excludes the digest now under review, so a review superseded by
    a contract change (same digest, no new delivery) would otherwise vanish from the re-dispatched packet,
    rendered as `## Round 1` as if no one had reviewed this digest before. The superseded record must carry
    enough (`superseded_contract_rev`, `superseded_fields`) for the next packet to say what changed and still
    show the prior findings."""
    make_task(layout, "A", reviewer=["chief_architect"], tests=[], owned_paths=["."])
    digest = "f" * 64
    tasks.set_status(layout, "A", "IN_PROGRESS")
    tasks.record_delivery(layout, "A", digest, paths=[])
    tasks.record_verification(layout, "A", digest, True)
    tasks.advance(layout, "A")
    tasks.record_review(layout, "A", digest, "chief_architect", "REVISE",
                        findings=[{"id": "f1", "category": "correctness", "blocking": True,
                                  "message": "fix the thing", "evidence": "x.py:1"}])

    tasks.patch_task(layout, "A", {"tests": ["a new criterion"]}, actor="plan")   # tests is a judged field

    task = tasks.load(layout, "A")
    rec = task.delivery.reviews[-1]
    assert rec.superseded
    assert rec.superseded_contract_rev == task.contract_rev
    assert rec.superseded_fields == ["tests"]

    ctx = packets.prior_round_context(layout, task, digest, [])
    block = packets.prior_round_block(ctx)
    assert "fix the thing" in block, "the superseded pass's own findings must still reach the next review packet"
    assert f"rev {task.contract_rev}" in block, "the packet must say which contract_rev superseded the pass"
