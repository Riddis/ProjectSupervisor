"""Command files: the UI/CLI never mutate state/ while the loop holds the lock; they drop
state/commands/<ulid>.json and the lock holder applies them, moving them to commands/applied/.

Exactly-once: handlers must be idempotent by command id (payload["_id"]) because a crash between the handler and the
applied-file write would re-run them; the applied-file check closes the second window (applied → unlink)."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import gates as gates_mod
from . import inbox as inbox_mod
from . import jobs as jobs_mod
from . import owner_tasks as owner_tasks_mod
from . import tasks as tasks_mod
from ._vendor.contract import atomic_json
from .locks import FileLock, LockHeld
from .paths import Layout
from .store import now_iso


def new_id() -> str:
    return f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"


def submit(layout: Layout, kind: str, payload: dict, actor: str = "cli") -> Path:
    layout.commands.mkdir(parents=True, exist_ok=True)
    cid = new_id()
    p = layout.commands / f"{cid}.json"
    atomic_json(p, {"id": cid, "kind": kind, "payload": payload, "actor": actor, "submitted": now_iso()})
    try:
        layout.wake.parent.mkdir(parents=True, exist_ok=True)
        layout.wake.write_text(now_iso(), encoding="utf-8")
    except OSError:
        pass
    return p


HANDLERS = {}


def handler(kind):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


@handler("task.set_status")
def _set_status(layout, payload, actor):
    t = tasks_mod.set_status(layout, payload["task_id"], payload["status"], actor=actor, note=payload.get("note", ""),
                             force=bool(payload.get("force")))
    return {"status": t.status.value}


@handler("task.set_priority")
def _set_priority(layout, payload, actor):
    tasks_mod.set_priority(layout, payload["task_id"], int(payload["priority"]), actor)
    return {}


@handler("task.note")
def _note(layout, payload, actor):
    cid = payload.get("_id")
    want_guidance = bool(payload.get("guidance"))
    t = tasks_mod.load(layout, payload["task_id"])
    extra = t.model_extra or {}
    noted = bool(cid) and any(n.get("command_id") == cid for n in extra.get("notes", []))
    guided = bool(cid) and any(g.get("item") == f"note:{cid}" for g in (extra.get("guidance") or []) if isinstance(g, dict))
    if noted and (guided or not want_guidance):
        return {"skipped": "already applied"}
    # note + guidance in one idempotent update: a replay after an interruption completes what is missing
    tasks_mod.add_note(layout, payload["task_id"], payload["note"], actor, command_id=cid, guidance=want_guidance)
    return {"repaired": "guidance"} if noted else {}


@handler("task.approve")
def _approve(layout, payload, actor):
    cid = payload.get("_id")
    t = tasks_mod.load(layout, payload["task_id"])
    if cid and any(a.command_id == cid for a in t.delivery.approvals):
        return {"skipped": "already applied", "status": t.status.value}
    tasks_mod.record_approval(layout, payload["task_id"], payload["digest"], payload.get("response", "ACCEPT"), actor,
                              payload.get("note", ""), command_id=cid)
    t = tasks_mod.advance(layout, payload["task_id"])
    return {"status": t.status.value}


@handler("inbox.answer")
def _inbox_answer(layout, payload, actor):
    cid = payload.get("_id")
    item = inbox_mod.answer(layout, payload["item_id"], payload["response"], payload.get("note", ""), actor,
                            command_id=cid)
    out = {"status": item.get("status")}
    try:
        t = tasks_mod.load(layout, item["task_id"]) if item.get("task_id") else None
    except tasks_mod.TaskError:
        t = None
    if t is not None:
        out["task_status"] = t.status.value
    return out


@handler("inbox.snooze")
def _inbox_snooze(layout, payload, actor):
    item = inbox_mod.snooze(layout, payload["item_id"], actor, payload.get("note", ""))
    return {"status": item.get("status")}


@handler("inbox.reopen")
def _inbox_reopen(layout, payload, actor):
    item = inbox_mod.reopen(layout, payload["item_id"], actor)
    return {"status": item.get("status")}


@handler("task.create")
def _create(layout, payload, actor):
    """Add a task that is not in the seed (probes, owner-added work, Control Center conversions)."""
    return tasks_mod.create(layout, dict(payload["task"]), actor)


def seed_path(layout, seed: str | None) -> Path:
    """The seed file a `task.import` names, resolved against the project root; ValueError unless it is a file
    inside the project."""
    from ._vendor.contract import contained
    path = (layout.root / str(seed or "state/tasks_seed.yaml")).resolve()
    if not contained(path, [layout.root], root=layout.root) or not path.is_file():
        raise ValueError(f"seed {seed!r} is not a file inside the project")
    return path


@handler("task.import")
def _task_import(layout, payload, actor):
    """`supervisor task import` through the queue, so a corrected seed reaches a live supervisor. Before 1.4.0 the
    CLI refused while state/.lock was held, and since every agent runs under the supervisor no agent could ever
    apply a contract fix it had made in the seed."""
    seed = seed_path(layout, payload.get("seed"))
    ids = [str(i) for i in (payload.get("update_contracts") or [])]
    r = tasks_mod.import_seed(layout, seed, ids, create=bool(payload.get("create", True)), actor=actor)
    return {k: r[k] for k in ("created", "updated", "warnings") if k in r}


@handler("task.set_job")
def _set_job(layout, payload, actor):
    """Replace a task's `job` block (adapter/fn/params/model/...): how the Control Center re-routes a task."""
    def fn(doc):
        doc["job"] = dict(payload["job"])
        if payload.get("cost_category"):
            doc["cost_category"] = payload["cost_category"]
    t = tasks_mod._update(layout, payload["task_id"], fn)
    tasks_mod.log(layout, "task_job_set", actor, task=payload["task_id"], job=payload["job"])
    return {"status": t.status.value}


@handler("task.advance")
def _advance(layout, payload, actor):
    """Re-evaluate the completion rule for the current digest (e.g. after the owner unblocks a task)."""
    t = tasks_mod.advance(layout, payload["task_id"], actor=actor)
    return {"status": t.status.value}


@handler("task.deliver")
def _deliver(layout, payload, actor):
    cid = payload.get("_id")
    t = tasks_mod.load(layout, payload["task_id"])
    if cid and t.delivery and t.delivery.digest == payload["digest"]:
        return {"skipped": "already delivered", "status": t.status.value}
    t = tasks_mod.record_delivery(layout, payload["task_id"], payload["digest"], actor=actor, result=payload.get("result"),
                                  paths=payload.get("paths") or [])
    return {"status": t.status.value}


@handler("task.verify")
def _verify(layout, payload, actor):
    t = tasks_mod.load(layout, payload["task_id"])
    v = t.delivery.verification if t.delivery else None
    if v and v.digest == payload["digest"] and v.ok == bool(payload.get("ok", True)):
        return {"skipped": "already verified", "status": t.status.value}
    tasks_mod.record_verification(layout, payload["task_id"], payload["digest"], bool(payload.get("ok", True)),
                                  payload.get("report"), payload.get("detail", ""), actor=actor)
    t = tasks_mod.advance(layout, payload["task_id"])
    return {"status": t.status.value}


@handler("task.review")
def _review(layout, payload, actor):
    t = tasks_mod.load(layout, payload["task_id"])
    if t.delivery and any(r.digest == payload["digest"] and r.role == payload["role"] and r.verdict.value == payload["verdict"]
                          for r in t.delivery.reviews):
        return {"skipped": "already reviewed", "status": t.status.value}
    # A payload that carries `report_sha256` (the CLI hashes at submission) keeps it, None included: a report that
    # could not be hashed when it was submitted is never authenticated by whatever the file holds when this applies.
    provenance = {"report_sha256": payload["report_sha256"]} if "report_sha256" in payload else {}
    tasks_mod.record_review(layout, payload["task_id"], payload["digest"], payload["role"], payload["verdict"],
                            payload.get("vendor"), payload.get("report"), **provenance)
    t = tasks_mod.advance(layout, payload["task_id"])
    return {"status": t.status.value}


@handler("gate.ack")
def _gate_ack(layout, payload, actor):
    gates_mod.ack(layout, payload["gate_id"], actor, payload.get("note", ""))
    return {}


@handler("gate.set")
def _gate_set(layout, payload, actor):
    gates_mod.set_status(layout, payload["gate_id"], payload["status"], payload.get("evidence", []), actor)
    return {}


@handler("capacity.reset")
def _capacity_reset(layout, payload, actor):
    from . import capacity
    cleared = capacity.owner_reset(layout, payload["engine"], payload.get("message", f"reset by {actor}"))
    return {"cleared": len(cleared)}


@handler("lane.reset")
def _lane_reset(layout, payload, actor):
    """Forget a lane-level unavailability (a spent credit balance, say): jobs that lane deferred are released on the
    loop's next launch of them when the lane reports itself ready (Adapter.lane_ready)."""
    from .adapters import REGISTRY
    lane = str(payload["lane"])
    if lane not in REGISTRY:
        raise ValueError(f"unknown lane {lane!r}")
    return {"cleared": bool(REGISTRY[lane].reset_lane(layout))}


@handler("settings.update_routing")
def _settings_update_routing(layout, payload, actor):
    from . import settings as settings_mod
    return settings_mod.update(layout, payload["field"], payload["value"], actor)


@handler("owner_task.set_status")
def _owner_task_set_status(layout, payload, actor):
    row = owner_tasks_mod.set_status(layout, payload["owner_task_id"], payload["status"], actor)
    out = {"status": row.get("status")}
    if row.get("released"):
        out["released"] = row["released"]                  # project tasks re-queued by this "done"
    return out


@handler("agent.terminate")
def _agent_terminate(layout, payload, actor):
    """Terminate a running job's process and fail its task (Control Center Agents panel, ). Applied only by
    the lock holder, same as every other command, so this never races the loop's own collect()/settle()."""
    from .adapters import REGISTRY
    attempt_id = payload["attempt_id"]
    try:
        rec = jobs_mod.read(layout, attempt_id)
    except (OSError, ValueError):
        return {"skipped": "no such job"}
    if rec.get("phase") not in ("PREPARED", "SUBMITTED", "RUNNING"):
        return {"skipped": "already finished", "phase": rec.get("phase")}
    ad = REGISTRY.get(rec["adapter"])
    if ad is not None and rec.get("handle"):
        ad.terminate(rec["handle"])
    jobs_mod.set_phase(layout, attempt_id, "FAILED", error=f"terminated by {actor}", finished=now_iso())
    try:
        tasks_mod.set_status(layout, rec["task_id"], "FAILED", actor=actor, note=f"{attempt_id}: terminated by {actor}")
    except tasks_mod.TaskError:
        pass  # task already in a terminal/unreachable state; the job record still reflects the termination
    return {"terminated": attempt_id}


@handler("agent.extend")
def _agent_extend(layout, payload, actor):
    """Give a running job more time (Control Center **Extend**, 1.7.0): pushes its effective hard cap
    (timeouts.effective_limits — for a job prepared before activity limits, the current policy's cap, not the old
    hour still in its handle) back by `seconds` (default one hour, supervisor.timeouts.EXTEND_STEP_S). The idle limit
    is unchanged — a job that stops working is still stopped. A pending time-limit handoff request is withdrawn when
    the new cap is far enough away, so the session keeps working instead of wrapping up.

    Idempotent by command id, and a replay repairs: the job record is written first, then status.json and the
    handoff request. A crash between those writes leaves the hour recorded in the job but not in status.json (which
    recover() re-adopts from) and the stop request still in place; replaying the command re-applies those two
    effects from the job record without adding the hour again."""
    from . import timeouts
    attempt_id = payload["attempt_id"]
    try:
        rec = jobs_mod.read(layout, attempt_id)
    except (OSError, ValueError):
        return {"skipped": "no such job"}
    if rec.get("phase") not in ("SUBMITTED", "RUNNING") or not rec.get("handle"):
        return {"skipped": "not running", "phase": rec.get("phase")}
    seconds = int(payload.get("seconds") or timeouts.EXTEND_STEP_S)
    if seconds <= 0 or seconds > 24 * 3600:
        raise ValueError("extend by 1 s to 24 h")
    spec = rec.get("spec") or {}
    base = timeouts.effective_limits(rec.get("adapter"), spec, rec["handle"])[0]
    handle, changed = timeouts.extend(rec["handle"], seconds, payload.get("_id"), actor, now_iso(), base=base)
    if changed:
        jobs_mod.set_phase(layout, attempt_id, rec["phase"], handle=handle)
    deadline = timeouts.effective_limits(rec.get("adapter"), spec, handle)[0]
    repaired = _sync_extension(spec, handle, deadline)
    if not changed:
        if repaired:
            tasks_mod.log(layout, "job_extend_repaired", actor, task=rec.get("task_id"), attempt=attempt_id,
                          repaired=repaired, deadline=deadline)
        return {"skipped": "already applied", "deadline": deadline, "repaired": repaired}
    tasks_mod.log(layout, "job_extended", actor, task=rec.get("task_id"), attempt=attempt_id, seconds=seconds,
                  deadline=deadline)
    return {"attempt_id": attempt_id, "extended_s": seconds, "deadline": deadline}


def _sync_extension(spec: dict, handle: dict, deadline: float) -> list[str]:
    """Bring the run dir in line with the job record after an Extend (first run or replay): status.json carries the
    handle's deadline and extensions (recover() re-adopts from it after a loop restart), and a time-limit handoff
    request is withdrawn while the effective cap is more than HANDOFF_WARN_S away. Returns what it changed."""
    from . import timeouts
    from ._vendor.contract import read_json
    run_dir = spec.get("run_dir")
    if not run_dir:
        return []
    repaired = []
    status_path = Path(run_dir) / "status.json"
    try:
        st = read_json(status_path)
        if (isinstance(st, dict) and st.get("status") == "RUNNING"
                and (st.get("deadline") != handle.get("deadline") or st.get("extensions") != handle.get("extensions"))):
            atomic_json(status_path, {**st, "deadline": handle.get("deadline"), "extensions": handle.get("extensions")})
            repaired.append("status")
    except (OSError, ValueError):
        pass
    req = Path(run_dir) / "HANDOFF_REQUEST.json"
    try:
        if read_json(req).get("reason") == "time limit" and deadline - time.time() > timeouts.HANDOFF_WARN_S:
            req.unlink()
            repaired.append("handoff_request")
    except (OSError, ValueError, AttributeError):
        pass
    return repaired


@handler("agent.note")
def _agent_note(layout, payload, actor):
    """Write a message to a running agent's run dir (Control Center Agents panel, ): cooperative, not
    guaranteed delivered — the agent checks `OWNER_NOTE.md` between steps per `agents/_owner_note_protocol.md`."""
    from . import conversations as conversations_mod
    attempt_id = payload["attempt_id"]
    try:
        rec = jobs_mod.read(layout, attempt_id)
    except (OSError, ValueError):
        return {"skipped": "no such job"}
    run_dir = (rec.get("spec") or {}).get("run_dir")
    if not run_dir:
        return {"skipped": "job has no run directory"}
    conversations_mod.write_owner_note(run_dir, payload["text"], by=actor)
    tasks_mod.log(layout, "owner_note", actor, attempt=attempt_id)
    return {"attempt_id": attempt_id, "status": "pending"}


@handler("conversation.create")
def _conversation_create(layout, payload, actor):
    from . import conversations as conversations_mod
    return conversations_mod.create(layout, payload.get("role", conversations_mod.DEFAULT_ROLE), actor)


@handler("conversation.ask")
def _conversation_ask(layout, payload, actor):
    from . import conversations as conversations_mod
    turn = conversations_mod.ask(layout, payload["thread_id"], payload["text"], actor,
                                 task_ref=payload.get("task_ref"), decision_ref=payload.get("decision_ref"),
                                 command_id=payload.get("_id"))
    return {"status": turn.get("status")}


@handler("conversation.convert")
def _conversation_convert(layout, payload, actor):
    from . import conversations as conversations_mod
    return conversations_mod.convert(layout, payload["thread_id"], int(payload["turn"]), payload["into"], actor,
                                     **(payload.get("fields") or {}))


@handler("loop.pause")
@handler("loop.resume")
@handler("loop.stop")
@handler("loop.reload")
@handler("loop.drain")
def _loop_control(layout, payload, actor):
    # Interpreted by the loop itself (runtime.py); applying it here just records the decision. `loop.drain` with
    # payload {"shutdown": true} arms the workstation power-off that follows the drain; any other decision
    # disarms it, and `loop.resume` also aborts an OS shutdown already counting down.
    from . import runtime, shutdown as shutdown_mod
    decision = {"loop.pause": "PAUSE", "loop.resume": "RUN", "loop.stop": "STOP", "loop.reload": "RELOAD",
                "loop.drain": "DRAIN"}[payload["_kind"]]
    extra = {"shutdown_on_drain": bool(payload.get("shutdown"))} if decision == "DRAIN" else {}
    runtime.set_decision(layout, decision, actor, **extra)
    out = {"decision": decision, **extra}
    if decision == "RUN":
        out["shutdown"] = shutdown_mod.cancel(layout, actor)
    return out


@handler("system.shutdown")
def _system_shutdown(layout, payload, actor):
    """Power the workstation off now (grace period first). Only sensible when nothing is running — the Control Center
    offers it while the supervisor is offline; with a live supervisor use loop.drain {"shutdown": true}."""
    from . import runtime, shutdown as shutdown_mod
    if runtime.is_live(layout) and not payload.get("force"):
        raise ValueError("supervisor is live: drain it (loop.drain shutdown=true) instead of powering off under it")
    return shutdown_mod.schedule(layout, actor=actor)


@handler("system.shutdown_cancel")
def _system_shutdown_cancel(layout, payload, actor):
    from . import shutdown as shutdown_mod
    return shutdown_mod.cancel(layout, actor)


def pending(layout: Layout) -> list[Path]:
    return sorted(p for p in layout.commands.glob("*.json"))


def apply_all(layout: Layout, actor_override: str | None = None) -> list[dict]:
    """Apply every pending command exactly once (caller must hold the state lock). Returns outcomes."""
    outcomes = []
    layout.commands_applied.mkdir(parents=True, exist_ok=True)
    for p in pending(layout):
        done = layout.commands_applied / p.name
        if done.exists():                          # crash after applied-file write, before unlink
            p.unlink(missing_ok=True)
            continue
        try:
            cmd = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            atomic_json(done, {"id": p.stem, "ok": False, "error": f"unreadable command: {exc}", "applied": now_iso()})
            p.unlink(missing_ok=True)
            continue
        fn = HANDLERS.get(cmd.get("kind"))
        payload = dict(cmd.get("payload") or {}, _kind=cmd.get("kind"), _id=cmd.get("id"))
        with tasks_mod.command_scope(cmd.get("id")) as scope:
            try:
                if fn is None:
                    raise KeyError(f"unknown command {cmd.get('kind')!r}")
                result = fn(layout, payload, actor_override or cmd.get("actor", "cli"))
                cmd.update({"applied": now_iso(), "ok": True, "result": result})
            except Exception as exc:  # noqa: BLE001 - a bad command must not kill the loop
                cmd.update({"applied": now_iso(), "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        # One line per action: a handler that logged its own event (job_extended, status, ...) already wrote it,
        # tagged with this command's id. The generic line remains for failures and for handlers that log nothing.
        if not cmd["ok"] or not scope["logged"]:
            tasks_mod.log(layout, "command", cmd.get("actor", "cli"), kind=cmd.get("kind"), id=cmd.get("id"),
                          ok=cmd["ok"], error=cmd.get("error"))
        atomic_json(layout.commands_applied / p.name, cmd)
        try:
            p.unlink()
        except OSError:
            pass
        outcomes.append(cmd)
    return outcomes


def submit_or_apply(layout: Layout, kind: str, payload: dict, actor: str = "cli") -> dict:
    """CLI/UI entry point: queue the command; if no loop holds the lock, take it and apply immediately."""
    p = submit(layout, kind, payload, actor)
    try:
        with FileLock(layout.lock, owner=f"{actor}-direct"):
            outcomes = apply_all(layout)
            mine = [o for o in outcomes if o.get("id") == p.stem]
            return mine[0] if mine else {"id": p.stem, "queued": True}
    except LockHeld:
        return {"id": p.stem, "queued": True, "note": "a supervisor holds the lock; it will apply the command"}


def status_of(layout: Layout, command_id: str) -> dict | None:
    p = layout.commands_applied / f"{command_id}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    if (layout.commands / f"{command_id}.json").exists():
        return {"id": command_id, "pending": True}
    return None
