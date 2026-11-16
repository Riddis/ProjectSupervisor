"""P2-1 (round 1 of the 2.7.0 release review): R-19's job tag must reach every CLI verb that queues a command, not
only `supervisor cmd`. With `SUPERVISOR_ATTEMPT` set and the state lock held (as a running loop holds it), each of
`task note`, `task set-status`, `task import`, `task deliver`, `task approve`, `task advance`, `issue resolve` and
`cmd` (the control, already covered before this fix) must queue its command tagged `{"kind": "job", "id": ...}`;
the one of them that fails must open exactly one deduped owner issue and be the record `outcome_for_job` returns.
With the variable unset the same verbs must stay interactive and open nothing — this is R-19's own guarantee,
unchanged."""
from __future__ import annotations

import json

import yaml
from typer.testing import CliRunner

from supervisor import commands, tasks
from supervisor.cli import app
from supervisor.locks import FileLock
from tests.conftest import make_task

# One CLI invocation per verb the finding names, plus `cmd` as the pre-existing control. All but `issue resolve`
# are set up (below) to succeed when applied, so exactly one failure - and one owner issue - is unambiguous.
_INVOCATIONS = [
    ("task.note", ["task", "note", "A", "hello"]),
    ("task.set_status", ["task", "set-status", "A", "NOT_STARTED"]),
    ("task.import", ["task", "import", "--seed", "seed.yaml"]),
    ("task.deliver", ["task", "deliver", "A", "a" * 64]),
    ("task.approve", ["task", "approve", "A", "a" * 64]),
    ("task.advance", ["task", "advance", "A"]),
    ("issue.resolve", ["issue", "resolve", "I-999", "--resolution", "x"]),
    ("task.note", ["cmd", "task.note", "--payload", '{"task_id": "A", "note": "via cmd"}']),
]


def _queue_every_verb(layout):
    """Hold the state lock (as a running loop would) and run each invocation above; every one queues (never
    applies) its command. Returns the `submitted_by` tag of each queued command, in order."""
    make_task(layout, "A")
    tasks.record_delivery(layout, "A", "a" * 64, actor="system")  # so deliver/approve/advance below are no-ops
    (layout.root / "seed.yaml").write_text(yaml.safe_dump({"tasks": [{"id": "A"}]}), encoding="utf-8")
    runner = CliRunner()
    tags = []
    with FileLock(layout.lock, owner="supervisor"):
        for _kind, argv in _INVOCATIONS:
            r = runner.invoke(app, argv)
            assert r.exit_code == 0, r.output
            queued = sorted(layout.commands.glob("*.json"), key=lambda p: p.stat().st_mtime)
            tags.append(json.loads(queued[-1].read_text(encoding="utf-8"))["submitted_by"])
    return tags


def test_every_verb_tags_its_command_as_the_job_when_supervisor_attempt_is_set(layout, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_ATTEMPT", "A.a1")
    tags = _queue_every_verb(layout)
    assert tags == [{"kind": "job", "id": "A.a1"}] * len(_INVOCATIONS)

    outcomes = commands.apply_all(layout)
    failed = [o for o in outcomes if not o["ok"]]
    assert len(failed) == 1 and failed[0]["kind"] == "issue.resolve"

    issues = list(layout.issues.glob("I-*.yaml"))
    assert len(issues) == 1
    assert "issue.resolve" in issues[0].read_text(encoding="utf-8")
    assert commands.outcome_for_job(layout, "A.a1") == failed[0]


def test_the_same_verbs_stay_interactive_with_no_job_id_and_open_nothing(layout, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_ATTEMPT", raising=False)
    tags = _queue_every_verb(layout)
    assert all(t.get("kind") == "interactive" and "actor" in t for t in tags)

    outcomes = commands.apply_all(layout)
    failed = [o for o in outcomes if not o["ok"]]
    assert len(failed) == 1 and failed[0]["kind"] == "issue.resolve"

    assert list(layout.issues.glob("I-*.yaml")) == []
    assert commands.outcome_for_job(layout, "A.a1") is None
