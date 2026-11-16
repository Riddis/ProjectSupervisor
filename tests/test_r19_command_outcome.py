"""R-19: a command's outcome must reach whoever queued it. A job that queues a command and finishes before the
command is applied must not be declared complete until the command resolves, and must fail with the command's own
error if it fails; a failed command with no interactive submitter must open one deduped owner item; and the Control
Center's action result must stop saying "queued" once the loop has processed the command."""
from __future__ import annotations

from supervisor import commands, tasks
from supervisor.locks import FileLock
from supervisor import testing
from tests.conftest import make_task


class _QueuesACommand(testing.FakeLane):
    """A delivery job that, like a real process job calling `supervisor cmd` from inside its own run, submits one
    command tagged with its own attempt id and then reports its own result as COMPLETE. Submits only once, so a
    retried collect() (the record stays open while the command is pending) does not queue a second one."""

    def __init__(self, layout, kind: str, payload: dict, **kw):
        super().__init__(**kw)
        self.layout, self.kind, self.payload = layout, kind, payload
        self._submitted = False

    def collect(self, spec, handle):
        if not self._submitted:
            self._submitted = True
            commands.submit(self.layout, self.kind, self.payload, actor="worker", job_id=spec["attempt_id"])
        return super().collect(spec, handle)


def test_a_jobs_pending_command_blocks_completion_and_a_failed_one_fails_the_task(layout):
    lane = _QueuesACommand(layout, "task.set_status", {"task_id": "no-such-task", "status": "COMPLETE"},
                           artifacts=["x"])
    with testing.lane(lane):
        make_task(layout, "A", job={"adapter": "fake_lane"}, human="none")
        sup = testing.drive(layout, iterations=1)
        # The job's own command has not been applied yet: this result must not complete the task on this turn.
        assert tasks.load(layout, "A").status != "COMPLETE"

        sup.once()  # apply_all() now applies the pending command, which fails: no such task
        t = tasks.load(layout, "A")
        assert t.status != "COMPLETE"
        detail = (t.delivery.verification.detail or "") if t.delivery and t.delivery.verification else ""
        assert "task.set_status" in detail and "no-such-task" in detail

        issues = list(layout.issues.glob("I-*.yaml"))
        assert len(issues) == 1
        assert "task.set_status" in issues[0].read_text(encoding="utf-8")


def test_a_failed_job_command_opens_exactly_one_deduped_owner_item(layout):
    make_task(layout, "A")
    for _ in range(2):  # two separate commands, same failure, as a relaunch of the same job would produce
        commands.submit(layout, "task.set_status", {"task_id": "no-such-task", "status": "COMPLETE"},
                        actor="worker", job_id="A.a1")
    outcomes = commands.apply_all(layout)
    assert all(not o["ok"] for o in outcomes)

    issues = list(layout.issues.glob("I-*.yaml"))
    assert len(issues) == 1
    text = issues[0].read_text(encoding="utf-8")
    assert "task.set_status" in text and "no-such-task" in text


def test_an_interactive_commands_failure_opens_no_owner_item(layout):
    make_task(layout, "A")
    commands.submit(layout, "task.set_status", {"task_id": "no-such-task", "status": "COMPLETE"}, actor="owner-ui")
    commands.apply_all(layout)
    assert list(layout.issues.glob("I-*.yaml")) == []


def test_control_center_action_result_resolves_from_queued_to_an_outcome(layout):
    testing.ui_client  # imported lazily by the helper; keeps the ui extra optional for the rest of the suite
    client = testing.ui_client(layout)
    make_task(layout, "A")
    with FileLock(layout.lock, owner="test supervisor"):
        r = client.post("/overview/pause", data={"csrf_token": testing.CSRF}, headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert 'data-pending' in r.text and 'data-command-id="' in r.text
        command_id = r.text.split('data-command-id="')[1].split('"')[0]
        status = client.get(f"/commands/{command_id}/status").json()
        assert status.get("pending")

    commands.apply_all(layout)  # the way the loop would, once it is free to take the lock again
    status = client.get(f"/commands/{command_id}/status").json()
    assert status.get("pending") is not True
    assert status["ok"] is True
