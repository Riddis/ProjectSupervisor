"""Round-5 review F13 (docs/reviews/RELEASE_REVIEW_2.1.0_round5.md lines 153-159): `cli.py:204`'s `task
set-status` defaults `actor="cli"`, and `tasks.py:476` keyed the release of `held` on `actor.startswith("owner")`
- so a held task moved to READY from the CLI (or any other manual, non-owner-prefixed actor) read READY with
`held` still set, and the next automatic requeue for it would be declined at `diagnosis.py:384` for a Hold
nobody holds. The same half-state F11 leaves, reachable from the owner's own CLI.

`held` is now released on any move off BLOCKED by an actor that is not one of the loop's own automatic writers
(`AUTOMATIC_ACTORS = {"loop", "escalation"}`, `tasks.py`) - a question of which actors may decide to release a
Hold, not a name-prefix guess at "the owner". The automatic actors themselves are kept off a held task's move at
all by the gate and `_ensure_in_progress` (F11) and by `diagnosis.ingest`'s own decline (F9); this clause is a
second, independent guard that they may never release one even if some future automatic write reached it."""
from supervisor import tasks
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def test_a_non_owner_actor_moving_a_held_task_off_blocked_clears_the_hold(layout):
    """The probe's own `cli_release` case (r5_hold.py section C): the CLI's default actor, "cli", starts with
    neither "owner" nor an automatic writer's name."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.hold(layout, "T", "owner-ui", "held by owner")
    assert tasks.load(layout, "T").held is not None

    tasks.set_status(layout, "T", "READY", actor="cli", note="re-queued from the CLI")

    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.READY
    assert task.held is None                     # never left READY with `held` still set


def test_the_real_cli_command_route_also_clears_the_hold(layout):
    """The same move, through the real command queue `cli.py`'s `task set-status` submits to
    (`commands.submit_or_apply`, actor defaulting to "cli"), not `tasks.set_status` called directly."""
    from supervisor import commands

    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.hold(layout, "T", "owner-ui", "held by owner")

    commands.submit_or_apply(layout, "task.set_status", {"task_id": "T", "status": "READY", "note": "", "force": False})

    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.READY
    assert task.held is None


def test_the_owners_own_release_still_clears_the_hold(layout):
    """Unchanged control (F9): the owner's own actors still release a Hold they set."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.hold(layout, "T", "owner-ui", "held by owner")

    tasks.set_status(layout, "T", "READY", actor="owner-ui", note="re-queued by owner")

    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.READY
    assert task.held is None


def test_an_automatic_actor_cannot_release_a_hold_by_moving_off_blocked(layout):
    """Unchanged control: the loop's own automatic actor must never release a Hold it did not set, even on a
    direct `set_status` call (F11 already keeps the loop from reaching a held task's move in practice; this is
    the release clause's own independent guard, not a repeat of F11's gate)."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.hold(layout, "T", "owner-ui", "held by owner")

    tasks.set_status(layout, "T", "IN_PROGRESS", actor="loop", note="the loop's own move")

    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.held is not None                  # still held - only a non-automatic actor releases one


def test_a_hold_on_an_already_blocked_task_still_changes_the_task_record(layout):
    """Unchanged control (hold_nocrash.txt:7, `tasks.py:498-500`): a Hold on an already-BLOCKED task still leaves
    a durable history entry and changes the task bytes - this fix touches only the release clause, not `hold()`."""
    make_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.set_status(layout, "T", "BLOCKED", actor="escalation", note="capped")
    before = tasks.load(layout, "T")

    tasks.hold(layout, "T", "owner-ui", "held by owner")

    after = tasks.load(layout, "T")
    assert after.model_dump() != before.model_dump()
    assert after.held is not None
    assert after.history[-1]["note"] == "held by owner"
