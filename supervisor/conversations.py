"""Control Center chat: threads with a role, grounded in the live state packet, plus the
OWNER_NOTE protocol for messaging a running agent's attempt.

A thread is `state/conversations/<id>.jsonl`: one JSON object per line — line 0 is the thread's meta record
(role, created), every line after that is a turn, `kind` "owner" or "assistant", alternating. A turn is
dispatched exactly like any other claude_cli job (jobs.py durable records, capacity-aware via the same
`capacity.require()` every claude_cli dispatch goes through, resumable across a crash): `supervisor.loop` special-cases
`kind == "conversation"` because no Task backs a thread, and `ingest()` here writes the reply straight into the
thread file instead of a delivery/review record. Every write to a thread file happens only inside a command
handler (`supervisor/commands.py` `conversation.*`), which keeps `state/` single-writer; `_rewrite()` below takes its
own lock so a direct caller (tests, the loop's `dispatch_conversations()`) cannot race the UI/CLI either."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import store
from . import tasks as tasks_mod
from ._vendor.contract import atomic_write_text
from .locks import FileLock
from .paths import Layout

RESULT_CONTRACT = (
    "## Result contract\nEnd your answer with exactly one fenced JSON block:\n"
    '```json\n{"kind":"conversation","schema_version":1,"reply":"..."}\n```\n'
    "`reply` is the only required field: plain prose, grounded in the live state above — say so plainly when you "
    "do not know rather than guessing.\n")
DEFAULT_ROLE = "senior_pm"


class ConversationError(ValueError):
    pass


# ------------------------------------------------------------------ store

def _new_id() -> str:
    return f"c-{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"


def path_for(layout: Layout, thread_id: str) -> Path:
    return layout.conversations / f"{thread_id}.jsonl"


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _write_lines(path: Path, lines: list[dict]) -> None:
    atomic_write_text(path, "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines))


def _lock(layout: Layout) -> FileLock:
    return FileLock(layout.state / ".conversations.lock", owner="conversations", timeout=5.0)


def create(layout: Layout, role: str = DEFAULT_ROLE, actor: str = "owner") -> dict:
    """A new thread addressed to `role` (default senior_pm, )."""
    layout.conversations.mkdir(parents=True, exist_ok=True)
    with _lock(layout):
        thread_id = _new_id()
        meta = {"kind": "meta", "id": thread_id, "role": role, "created": store.now_iso(), "by": actor}
        _write_lines(path_for(layout, thread_id), [meta])
    tasks_mod.log(layout, "conversation_created", actor, thread=thread_id, role=role)
    return meta


def list_threads(layout: Layout) -> list[dict]:
    out = []
    for p in sorted(layout.conversations.glob("*.jsonl")):
        lines = _read_lines(p)
        if not lines:
            continue
        meta = lines[0]
        turns = lines[1:]
        last = turns[-1] if turns else None
        out.append({**meta, "turn_count": len(turns),
                   "last_at": (last or meta).get("at", meta.get("created")),
                   "last_preview": (last or {}).get("text", "")[:140],
                   "pending": any(t.get("kind") == "assistant" and t.get("status") == "pending" for t in turns)})
    return sorted(out, key=lambda t: t.get("last_at") or "", reverse=True)


def load(layout: Layout, thread_id: str) -> dict:
    lines = _read_lines(path_for(layout, thread_id))
    if not lines:
        raise ConversationError(f"no such conversation {thread_id}")
    return {"meta": lines[0], "turns": lines[1:]}


def ask(layout: Layout, thread_id: str, text: str, actor: str = "owner", task_ref: str | None = None,
       decision_ref: str | None = None, command_id: str | None = None) -> dict:
    """Append the owner's question and a pending placeholder for the reply; `dispatch_conversations()` (the loop)
    picks up the placeholder on its next iteration. Returns the placeholder turn. Idempotent by `command_id`: a
    command re-applied after a crash between the handler and the applied-file write must not ask twice."""
    text = (text or "").strip()
    if not text:
        raise ConversationError("empty question")
    p = path_for(layout, thread_id)
    with _lock(layout):
        lines = _read_lines(p)
        if not lines:
            raise ConversationError(f"no such conversation {thread_id}")
        if command_id and any(l.get("kind") == "owner" and l.get("command_id") == command_id for l in lines):
            return next(l for l in reversed(lines) if l.get("kind") == "assistant")
        owner_turn = {"kind": "owner", "text": text, "at": store.now_iso(), "by": actor}
        if task_ref:
            owner_turn["task_ref"] = task_ref
        if decision_ref:
            owner_turn["decision_ref"] = decision_ref
        if command_id:
            owner_turn["command_id"] = command_id
        assistant_turn = {"kind": "assistant", "status": "pending", "text": "", "at": store.now_iso()}
        lines += [owner_turn, assistant_turn]
        _write_lines(p, lines)
    tasks_mod.log(layout, "conversation_ask", actor, thread=thread_id)
    return assistant_turn


def _finish_turn(layout: Layout, thread_id: str, idx: int, status: str, text: str) -> None:
    with _lock(layout):
        p = path_for(layout, thread_id)
        lines = _read_lines(p)
        if idx >= len(lines) or lines[idx].get("kind") != "assistant":
            return   # thread/turn vanished or was already finalized; nothing to do
        lines[idx] = {**lines[idx], "status": status, "text": text, "at": store.now_iso()}
        _write_lines(p, lines)


# ------------------------------------------------------------------ the live state packet (grounding context)

def _recent_events(layout: Layout, n: int = 10) -> list[dict]:
    if not layout.log.exists():
        return []
    rows = []
    for line in layout.log.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    rows.reverse()
    return rows


def state_packet(layout: Layout) -> dict:
    """phase, open items, capacity, last events, for a chat turn to answer "why is T0-15 blocked?" groundedly — the same facts the Overview screen shows, read straight from the store modules."""
    from . import capacity
    from . import inbox as inbox_mod
    from .models.task import TaskStatus
    tasks = tasks_mod.load_all(layout)
    elig = tasks_mod.eligible(layout, tasks)
    phase = min((t.phase for t in tasks.values() if t.status != TaskStatus.COMPLETE), default=0)
    return {
        "phase": phase,
        "eligible_tasks": [t.id for t in elig],
        "in_progress": [t.id for t in tasks.values() if t.status == TaskStatus.IN_PROGRESS],
        "waiting_for_review": [t.id for t in tasks.values() if t.status == TaskStatus.WAITING_FOR_REVIEW],
        "waiting_for_human": [t.id for t in tasks.values() if t.status == TaskStatus.WAITING_FOR_HUMAN],
        "blocked": [t.id for t in tasks.values() if t.status == TaskStatus.BLOCKED],
        "open_inbox": [{"id": i.get("id"), "subject": i.get("subject")} for i in inbox_mod.open_items(layout)],
        "capacity": capacity.windows(layout),
        "last_events": [{"event": e.get("event"), "actor": e.get("actor"), "task": e.get("task")}
                        for e in _recent_events(layout, 10)],
    }


def thread_brief(layout: Layout, role: str, history: list[dict], question: str, question_turn: dict) -> str:
    from . import packets as packets_mod
    card = packets_mod.role_card(layout, role)
    packet = state_packet(layout)
    convo = "\n".join(f"{'Owner' if t.get('kind') == 'owner' else role}: {t.get('text', '')}"
                      for t in history if t.get("text"))
    refs = [f"referenced task {question_turn['task_ref']}" if question_turn.get("task_ref") else None,
            f"referenced decision {question_turn['decision_ref']}" if question_turn.get("decision_ref") else None]
    ref_line = ("\n" + "; ".join(r for r in refs if r) + "\n") if any(refs) else ""
    return ((card + "\n\n") if card else "") + (
        f"# Control Center conversation\n\nRepository: {layout.root}\n\nThe owner is chatting with you as {role} "
        "in the Control Center. Answer directly and groundedly, using the live state below; say plainly when you "
        "do not know rather than guessing. This is a read-only Q&A turn: you have no write access and nothing you "
        "say here changes the project by itself — the owner converts your answer into a task/decision/issue/owner "
        "task by hand if they want to act on it.\n\n## Live state\n" + json.dumps(packet, indent=2, default=str)
        + "\n\n" + (f"## Conversation so far\n{convo}\n\n" if convo else "") + ref_line
        + f"\n## Owner\n{question}\n\n" + RESULT_CONTRACT)


# ------------------------------------------------------------------ dispatch (loop-side)

def pending_turns(layout: Layout) -> list[tuple[str, int]]:
    """(thread_id, line index) for every assistant placeholder still pending with no job already in flight."""
    from . import jobs as jobs_mod
    in_flight = {(r.get("conversation_thread"), r.get("conversation_turn")) for r in jobs_mod.open_records(layout)
                if (r.get("spec") or {}).get("kind") == "conversation"}
    out = []
    for p in sorted(layout.conversations.glob("*.jsonl")):
        thread_id = p.stem
        lines = _read_lines(p)
        for idx, line in enumerate(lines):
            if line.get("kind") == "assistant" and line.get("status") == "pending" and (thread_id, idx) not in in_flight:
                out.append((thread_id, idx))
    return out


def _task_like(thread_id: str, prompt: str) -> SimpleNamespace:
    """Stand-in for `supervisor.dispatch()`'s `task` argument: no Task backs a conversation, so only the
    attributes the adapter machinery actually reads (`id`, `owner_role`, `model_extra`, `owned_paths`) exist.
    `owner_role="conversation"` pulls the chat lane's own confined profile (`config/routing.yaml` roles.
    conversation: read-only, opus) regardless of which role's card is embedded in the prompt — the target role
    supplies its voice, never its write permissions (routing.yaml's own note on that entry)."""
    return SimpleNamespace(id=f"conv-{thread_id}", owner_role="conversation", owned_paths=[],
                           model_extra={"job": {"prompt": prompt, "result_kind": "conversation",
                                                "write_scope": "run_dir", "timeout_s": 600}})


def dispatch(supervisor, thread_id: str, idx: int) -> dict | None:
    """Dispatch one pending turn, or None if there is nothing left to dispatch (already answered/removed)."""
    from .adapters import REGISTRY
    from .adapters.claude_cli import role_profile
    layout = supervisor.layout
    lines = _read_lines(path_for(layout, thread_id))
    if idx >= len(lines) or lines[idx].get("kind") != "assistant" or lines[idx].get("status") != "pending":
        return None
    role = lines[0].get("role", DEFAULT_ROLE)
    question_turn = lines[idx - 1] if idx >= 1 else {}
    history = lines[1:max(idx - 1, 1)]
    prompt = thread_brief(layout, role, history, question_turn.get("text", ""), question_turn)
    adapter_name = role_profile(layout, "conversation").get("adapter", "claude_cli")
    if adapter_name not in REGISTRY:
        return None
    task_like = _task_like(thread_id, prompt)
    return supervisor.dispatch(task_like, adapter_name, note=f"conversation {thread_id}", kind="conversation",
                              extra={"conversation_thread": thread_id, "conversation_turn": idx})


def ingest(layout: Layout, rec: dict, result) -> None:
    """A finished conversation job: write the reply (or the reason it failed) into the thread. No retry chain —
    a capacity deferral is shown to the owner directly, same as any other failure, because a chat reply is not
    worth spending a continuation budget on; the owner can just ask again."""
    thread_id, idx = rec.get("conversation_thread"), rec.get("conversation_turn")
    if thread_id is None or idx is None:
        return
    if result.status == "COMPLETE":
        obj = result.raw or {}
        text = str(obj.get("reply") or result.notes or "").strip() or "(empty reply)"
        _finish_turn(layout, thread_id, idx, "done", text)
    elif result.status == "DEFERRED":
        when = f" (resets around {time.strftime('%H:%M', time.localtime(result.reset_epoch))})" if result.reset_epoch else ""
        _finish_turn(layout, thread_id, idx, "failed", f"Capacity limit reached{when} — try again shortly.")
    else:
        _finish_turn(layout, thread_id, idx, "failed", result.error or f"turn failed ({result.status})")
    tasks_mod.log(layout, "conversation_reply", "loop", thread=thread_id, turn=idx, status=result.status)


def fail(layout: Layout, rec: dict, reason: str) -> None:
    """Used by the loop when a conversation job's launch itself fails (no result ever came back)."""
    thread_id, idx = rec.get("conversation_thread"), rec.get("conversation_turn")
    if thread_id is not None and idx is not None:
        _finish_turn(layout, thread_id, idx, "failed", reason)


# ------------------------------------------------------------------ convert-to-*

def convert(layout: Layout, thread_id: str, turn_idx: int, into: str, actor: str = "owner", **fields: Any) -> dict:
    """Turn a completed assistant reply into a task, decision, issue or owner task (one click in the Control
    Center). `fields` are the extra bits the target store needs (title/owner_role/phase for a task, severity for
    an issue, ...); the reply text itself becomes the body/objective/detail."""
    lines = _read_lines(path_for(layout, thread_id))
    if turn_idx >= len(lines) or lines[turn_idx].get("kind") != "assistant":
        raise ConversationError(f"{thread_id}: turn {turn_idx} is not an assistant reply")
    turn = lines[turn_idx]
    if turn.get("status") != "done":
        raise ConversationError(f"{thread_id}: turn {turn_idx} has no completed reply to convert")
    text = turn.get("text", "")
    if into == "task":
        out = _convert_task(layout, text, actor, **fields)
    elif into == "decision":
        out = _convert_decision(layout, text, actor, **fields)
    elif into == "issue":
        out = _convert_issue(layout, text, actor, **fields)
    elif into == "owner_task":
        out = _convert_owner_task(layout, text, actor, **fields)
    else:
        raise ConversationError(f"unknown conversion target {into!r}")
    tasks_mod.log(layout, "conversation_convert", actor, thread=thread_id, turn=turn_idx, into=into, id=out.get("id"))
    return out


def _convert_task(layout, text, actor, title=None, owner_role="engineer", phase=0, **_) -> dict:
    n = 0
    import re
    for p in layout.tasks.glob("CC-*.yaml"):
        m = re.match(r"CC-(\d+)$", p.stem)
        if m:
            n = max(n, int(m.group(1)))
    task_id = f"CC-{n + 1:03d}"
    doc = {"id": task_id, "phase": int(phase), "title": (title or text[:80]).strip(), "objective": text,
          "owner_role": owner_role, "acceptance": "", "reviewer": [], "human_approval": "none",
          "escalation": "none", "cost_category": "claude",
          "owned_paths": ["."]}            # a chat-born task has no bounded scope yet: exclusive mode
    result = tasks_mod.create(layout, doc, actor)
    return {"into": "task", "id": result.get("id", task_id)}


def _convert_decision(layout, text, actor, title=None, status="proposed", **_) -> dict:
    from . import decisions as decisions_mod
    doc = decisions_mod.create(layout, title or text.splitlines()[0][:100], text, status=status, decided_by=actor)
    return {"into": "decision", "id": doc["id"]}


def _convert_issue(layout, text, actor, title=None, severity="normal", owner_role="owner", **_) -> dict:
    from . import escalation as escalation_mod
    doc = escalation_mod.open_issue(layout, title or text.splitlines()[0][:100], severity, owner_role, text)
    return {"into": "issue", "id": doc["id"]}


def _convert_owner_task(layout, text, actor, title=None, priority="normal", **_) -> dict:
    from . import owner_tasks as owner_tasks_mod
    doc = owner_tasks_mod.create(layout, title or text.splitlines()[0][:100], text, priority=priority)
    return {"into": "owner_task", "id": doc["id"]}


# ------------------------------------------------------------------ OWNER_NOTE protocol (messages to a running agent)

def owner_note_path(run_dir: Path) -> Path:
    return Path(run_dir) / "OWNER_NOTE.md"


def owner_note_instructions(layout: Layout, run_dir: Path) -> str:
    """The protocol text (`agents/_owner_note_protocol.md`), with this attempt's own note path filled in — injected
    into every delivery/review prompt by `adapters/claude_cli.py` `prepare()`, the same way `_vendor.handoff.
    instructions()` is. '' while the file does not exist (a stripped-down environment), never a hard failure."""
    p = layout.agents / "_owner_note_protocol.md"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8").replace("<OWNER_NOTE_PATH>", str(owner_note_path(run_dir)))


def write_owner_note(run_dir: Path, text: str, by: str = "owner") -> Path:
    """Drop a message for a running agent to pick up between steps. Overwrites any prior
    note: only the latest message is cooperative-checkpoint material, and `owner_note_status()` reports "pending"
    again because this is a fresh, unacknowledged note."""
    p = owner_note_path(run_dir)
    header = f"Message from {by}, {store.now_iso()}:\n\n"
    atomic_write_text(p, header + text.strip() + "\n")
    return p


def owner_note_status(run_dir: Path) -> str:
    """none (no note written) | pending (written, not yet acknowledged) | read (the agent appended an ack line,
    per `agents/_owner_note_protocol.md`)."""
    p = owner_note_path(run_dir)
    if not p.exists():
        return "none"
    try:
        content = p.read_text(encoding="utf-8")
    except OSError:
        return "pending"
    return "read" if any(line.strip().startswith("- [ack]") for line in content.splitlines()) else "pending"
