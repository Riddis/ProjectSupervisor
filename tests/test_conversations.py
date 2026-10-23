"""Control Center chat: thread store, state-packet grounding, dispatch under kind "conversation",
convert-to-* and the OWNER_NOTE protocol. Mirrors tests/test_review.py's pattern for a fake claude_cli stand-in."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
import yaml

from supervisor import commands, conversations, owner_tasks, tasks
from supervisor import jobs as jobs_mod
from supervisor._vendor.contract import CapacityDeferred
from supervisor.adapters import REGISTRY
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

FAKE_CHAT = str(Path(__file__).resolve().parent / "fixtures" / "fake_conversation.py")


class FakeChatClaude(REGISTRY["claude_cli"].__class__):
    name = "claude_cli"

    def command(self, spec):
        argv = super().command(spec)
        return [sys.executable, FAKE_CHAT] + argv[1:]


@pytest.fixture(autouse=True)
def fake_claude(monkeypatch):
    monkeypatch.setitem(REGISTRY, "claude_cli", FakeChatClaude())
    yield
    for k in ("FAKE_CONVERSATION_REPLY", "FAKE_CONVERSATION_DEFER"):
        os.environ.pop(k, None)


@pytest.fixture(autouse=True)
def routing(layout):
    """roles.conversation matches config/routing.yaml in the real repo (read-only chat lane)."""
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({
        "roles": {"conversation": {"adapter": "claude_cli", "model": "opus", "effort": "medium", "tools": "reader"},
                 "senior_pm": {"adapter": "claude_cli", "model": "opus", "effort": "high", "tools": "writer"}},
        "limits": {"max_agent_jobs": 2, "max_gpu_jobs": 1}}), encoding="utf-8")
    return layout


def run_until(layout, predicate, timeout=20):
    sup = Supervisor(layout, sleep_s=0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        sup.once()
        if predicate():
            return sup
        time.sleep(0.1)
    raise AssertionError("condition never became true")


def _turn_done(layout, thread_id):
    def check():
        turns = conversations.load(layout, thread_id)["turns"]
        return turns and turns[-1].get("kind") == "assistant" and turns[-1].get("status") != "pending"
    return check


# ------------------------------------------------------------------ store + ask + dispatch + ingest


def test_ask_dispatches_the_turn_with_the_state_packet_and_stores_the_reply(layout):
    make_task(layout, "T0-99")   # gives the state packet something concrete to report
    meta = conversations.create(layout, role="senior_pm")
    thread_id = meta["id"]
    os.environ["FAKE_CONVERSATION_REPLY"] = "T0-99 is in progress because nothing blocks it."
    conversations.ask(layout, thread_id, "why is T0-99 blocked?")

    run_until(layout, _turn_done(layout, thread_id))

    turns = conversations.load(layout, thread_id)["turns"]
    assert turns[0] == {"kind": "owner", "text": "why is T0-99 blocked?", "at": turns[0]["at"], "by": "owner"}
    assert turns[1]["status"] == "done"
    assert turns[1]["text"] == "T0-99 is in progress because nothing blocks it."

    # the dispatched prompt actually carried the live state packet and the question, not a canned brief
    [job] = jobs_mod.for_task(layout, f"conv-{thread_id}")
    prompt = Path(job["spec"]["run_dir"], "prompt.md").read_text(encoding="utf-8")
    assert "T0-99" in prompt and "why is T0-99 blocked?" in prompt and '"phase"' in prompt


def test_a_reply_is_produced_with_the_shipped_routing_template(layout):
    """The template's `conversation` lane (whatever its informational `active` flag says, and 1.2.1 projects still
    carry active: false) must never leave a reply pending with its job PREPARED."""
    import shutil
    from supervisor import template as template_mod
    layout.config.mkdir(parents=True, exist_ok=True)
    shutil.copy(template_mod.TEMPLATE_DIR / "config" / "routing.yaml", layout.config / "routing.yaml")
    text = (layout.config / "routing.yaml").read_text(encoding="utf-8")
    (layout.config / "routing.yaml").write_text(text.replace("active: true                    # informational",
                                                             "active: false                   # informational"), encoding="utf-8")
    meta = conversations.create(layout, role="senior_pm")
    thread_id = meta["id"]
    os.environ["FAKE_CONVERSATION_REPLY"] = "replied"
    conversations.ask(layout, thread_id, "are you there?")
    run_until(layout, _turn_done(layout, thread_id))
    assert conversations.load(layout, thread_id)["turns"][1]["text"] == "replied"


def test_pending_turns_skips_a_turn_with_an_in_flight_job(layout):
    meta = conversations.create(layout)
    thread_id = meta["id"]
    conversations.ask(layout, thread_id, "hello")
    assert conversations.pending_turns(layout) == [(thread_id, 2)]
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    # the job exists and is open (PREPARED/SUBMITTED/RUNNING) now; no second dispatch for the same turn
    assert conversations.pending_turns(layout) == []


def test_conversation_history_carries_into_the_second_turn(layout):
    meta = conversations.create(layout)
    thread_id = meta["id"]
    os.environ["FAKE_CONVERSATION_REPLY"] = "first answer"
    conversations.ask(layout, thread_id, "first question")
    run_until(layout, _turn_done(layout, thread_id))

    os.environ["FAKE_CONVERSATION_REPLY"] = "second answer"
    conversations.ask(layout, thread_id, "second question")
    run_until(layout, lambda: len(conversations.load(layout, thread_id)["turns"]) == 4
             and conversations.load(layout, thread_id)["turns"][-1]["status"] != "pending")

    [_, job2] = jobs_mod.for_task(layout, f"conv-{thread_id}")
    prompt = Path(job2["spec"]["run_dir"], "prompt.md").read_text(encoding="utf-8")
    assert "first question" in prompt and "first answer" in prompt and "second question" in prompt


# ------------------------------------------------------------------ capacity (10% reserve)


def test_capacity_deferral_leaves_the_turn_pending_not_failed(layout, monkeypatch):
    """A chat turn never bypasses the measured capacity reserve: a deferral at submit() leaves the job PREPARED
    (waiting, no attempt spent) and the turn stays "pending", not "failed"."""
    meta = conversations.create(layout)
    thread_id = meta["id"]
    conversations.ask(layout, thread_id, "hello")

    def deny(layout_, engine):
        raise CapacityDeferred(time.time() + 3600, "out of capacity")
    monkeypatch.setattr("supervisor.capacity.require", deny)

    sup = Supervisor(layout, sleep_s=0)
    sup.once()

    turns = conversations.load(layout, thread_id)["turns"]
    assert turns[-1]["status"] == "pending"
    [job] = jobs_mod.for_task(layout, f"conv-{thread_id}")
    assert job["phase"] == "PREPARED"


def test_deferred_reply_from_the_model_is_shown_to_the_owner_without_a_retry_loop(layout):
    """A CLI-reported usage limit mid-turn (not a pre-submit gate): no continuation chain for a chat turn, the
    owner just sees why it failed."""
    meta = conversations.create(layout)
    thread_id = meta["id"]
    os.environ["FAKE_CONVERSATION_DEFER"] = "1"
    conversations.ask(layout, thread_id, "hello")
    run_until(layout, _turn_done(layout, thread_id))
    turns = conversations.load(layout, thread_id)["turns"]
    assert turns[-1]["status"] == "failed"
    assert "capacity" in turns[-1]["text"].lower()
    [job] = jobs_mod.for_task(layout, f"conv-{thread_id}")
    assert job["phase"] == "DONE"   # closed, not stuck open waiting for a continuation that will never come


# ------------------------------------------------------------------ crash / restart (resumable)


def test_a_fresh_supervisor_recovers_a_submitted_turn_after_a_restart(layout):
    """The job record is durable before launch (jobs.prepare): a brand-new Supervisor instance (simulating a
    restarted process) adopts the already-SUBMITTED job instead of dispatching a duplicate."""
    meta = conversations.create(layout)
    thread_id = meta["id"]
    os.environ["FAKE_CONVERSATION_REPLY"] = "recovered reply"
    conversations.ask(layout, thread_id, "hello")
    first = Supervisor(layout, sleep_s=0)
    first.once()                                     # dispatches a1, likely still SUBMITTED/RUNNING
    assert len(jobs_mod.for_task(layout, f"conv-{thread_id}")) == 1

    run_until(layout, _turn_done(layout, thread_id))  # run_until builds its own Supervisor, as a restarted process would
    assert len(jobs_mod.for_task(layout, f"conv-{thread_id}")) == 1   # still exactly one attempt, never duplicated
    assert conversations.load(layout, thread_id)["turns"][-1]["text"] == "recovered reply"


# ------------------------------------------------------------------ convert-to-*


def test_convert_done_reply_to_task_creates_a_valid_task_yaml(layout):
    meta = conversations.create(layout)
    thread_id = meta["id"]
    conversations.ask(layout, thread_id, "what should we build next?")
    conversations._finish_turn(layout, thread_id, 2, "done", "Add a retry budget to the publishing adapter.")

    out = conversations.convert(layout, thread_id, 2, "task", actor="owner", title="Retry budget",
                                owner_role="engineer")
    assert out["into"] == "task"
    t = tasks.load(layout, out["id"])
    assert t.title == "Retry budget"
    assert t.objective == "Add a retry budget to the publishing adapter."
    assert t.status == TaskStatus.NOT_STARTED


def test_convert_refuses_a_turn_that_has_not_finished(layout):
    meta = conversations.create(layout)
    thread_id = meta["id"]
    conversations.ask(layout, thread_id, "hello")   # still pending
    with pytest.raises(conversations.ConversationError):
        conversations.convert(layout, thread_id, 2, "task", actor="owner")


def test_convert_to_owner_task_and_issue(layout):
    meta = conversations.create(layout)
    thread_id = meta["id"]
    conversations.ask(layout, thread_id, "owner-only thing?")
    conversations._finish_turn(layout, thread_id, 2, "done", "Only you can renew the Console org link.")
    out = conversations.convert(layout, thread_id, 2, "owner_task", actor="owner", title="Renew Console org")
    assert out["into"] == "owner_task"
    rows = owner_tasks.load(layout)
    assert any(r["id"] == out["id"] and r["title"] == "Renew Console org" for r in rows)

    out2 = conversations.convert(layout, thread_id, 2, "issue", actor="owner", title="Console org expiring")
    assert out2["into"] == "issue"
    assert (layout.issues / f"{out2['id']}.yaml").exists()


def test_convert_to_decision_writes_a_front_matter_markdown_file(layout):
    meta = conversations.create(layout)
    thread_id = meta["id"]
    conversations.ask(layout, thread_id, "should we switch models?")
    conversations._finish_turn(layout, thread_id, 2, "done", "Stay on opus for now; revisit after the bake-off.")
    out = conversations.convert(layout, thread_id, 2, "decision", actor="owner", title="Stay on opus")
    files = list(layout.decisions.glob(f"{out['id']}-*.md"))
    assert len(files) == 1
    assert "Stay on opus" in files[0].read_text(encoding="utf-8")


# ------------------------------------------------------------------ commands wiring


def test_commands_ask_and_convert_round_trip(layout):
    r = commands.submit_or_apply(layout, "conversation.create", {"role": "senior_pm"}, actor="owner-ui")
    thread_id = r["result"]["id"]
    r2 = commands.submit_or_apply(layout, "conversation.ask", {"thread_id": thread_id, "text": "hi"}, actor="owner-ui")
    assert r2["ok"]
    conversations._finish_turn(layout, thread_id, 2, "done", "hello back")
    r3 = commands.submit_or_apply(layout, "conversation.convert",
                                  {"thread_id": thread_id, "turn": 2, "into": "owner_task", "fields": {"title": "x"}},
                                  actor="owner-ui")
    assert r3["ok"] and r3["result"]["into"] == "owner_task"


# ------------------------------------------------------------------ OWNER_NOTE protocol


def test_owner_note_written_and_marked_read_when_the_agent_acknowledges(layout, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert conversations.owner_note_status(run_dir) == "none"
    conversations.write_owner_note(run_dir, "please pause and wait for my review", by="owner")
    assert conversations.owner_note_status(run_dir) == "pending"
    p = conversations.owner_note_path(run_dir)
    with p.open("a", encoding="utf-8") as f:
        f.write("\n- [ack] 2026-10-20T06:00:00+00:00\n")
    assert conversations.owner_note_status(run_dir) == "read"


def test_agent_note_command_writes_into_the_jobs_run_dir(layout):
    make_task(layout, "T0-50")
    run_dir = layout.runs / "T0-50" / "T0-50.a1"
    jobs_mod.prepare(layout, "T0-50", "T0-50.a1", "process", {"run_dir": str(run_dir)})
    jobs_mod.set_phase(layout, "T0-50.a1", "SUBMITTED", handle={"pid": 999999, "create_time": 0}, submitted="now")
    r = commands.submit_or_apply(layout, "agent.note", {"attempt_id": "T0-50.a1", "text": "check the acceptance criteria"},
                                 actor="owner-ui")
    assert r["ok"]
    assert conversations.owner_note_path(run_dir).exists()
    assert conversations.owner_note_status(run_dir) == "pending"


def test_prepare_injects_the_owner_note_protocol_for_delivery_but_not_for_a_chat_turn(layout):
    (layout.agents / "_owner_note_protocol.md").parent.mkdir(parents=True, exist_ok=True)
    note_text = (Path(__file__).resolve().parents[1] / "supervisor" / "template" / "agents" / "_owner_note_protocol.md")
    if not note_text.exists():
        pytest.skip("agents/_owner_note_protocol.md not present in this checkout")
    import shutil
    shutil.copy(note_text, layout.agents / "_owner_note_protocol.md")
    make_task(layout, "T0-51")
    ad = REGISTRY["claude_cli"]
    spec = ad.prepare(layout, tasks.load(layout, "T0-51"), "T0-51.a1")
    delivery_prompt = Path(spec["stdin_file"]).read_text(encoding="utf-8")
    assert "OWNER_NOTE" in delivery_prompt

    meta = conversations.create(layout)
    task_like = conversations._task_like(meta["id"], "a chat prompt")
    chat_spec = ad.prepare(layout, task_like, f"conv-{meta['id']}.a1")
    chat_prompt = Path(chat_spec["stdin_file"]).read_text(encoding="utf-8")
    assert "OWNER_NOTE" not in chat_prompt
