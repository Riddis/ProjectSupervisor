"""The supervisor loop: token-free; holds state/.lock; one iteration =
apply commands → honour decision (pause/stop/reload) → reconcile job records → settle finished jobs → dispatch →
render → sleep (wake file shortens the sleep). Resumable after a kill at any point (jobs.py)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

from . import capacity, checkpoint as checkpoint_mod, commands as commands_mod, conversations as conversations_mod
from . import diagnosis as diagnosis_mod
from . import escalation as escalation_mod
from . import plans as plans_mod
from . import progress as progress_mod
from . import gate, inbox as inbox_mod, jobs as jobs_mod, leases, needs as needs_mod, preflight as preflight_mod
from . import render, review as review_mod
from . import runtime, scheduler, shutdown as shutdown_mod, state_commit as state_commit_mod, tasks as tasks_mod
from . import verify as verify_mod
from . import why_not_runnable as why_not_runnable_mod
from ._vendor.contract import CapacityDeferred, LaneUnavailable, atomic_json, digest as digest_of, manifest, read_json
from ._vendor import handoff
from .adapters import REGISTRY, Result
from .locks import FileLock, LockHeld
from .models.task import HumanApproval, TERMINAL, Task, TaskStatus
from .paths import Layout
from .store import now_iso

EXIT_RELOAD = 3


class UnmigratedJobsError(RuntimeError):
    """ATTEMPT_MODEL.md §8.6's start-up guard: a job record under `state/runtime/jobs/` or its archive
    (`jobs.archive_dir`, step 14's retention work, §4.5) is neither migration-stamped nor allocator-written, so
    it has no `attempt`/`kind`/`seq` for the writer switch's readers to sort or group it by. Checks both: a
    record that moved into the archive before `migrate attempts` ever ran is exactly the shape this guard exists
    to catch, not a reason to stop looking. Raised by `cli.run()` before it ever constructs a `Supervisor` or
    enters the loop — "start-up" names the supervisor *process*, not a `Supervisor` object: a test (or a library
    caller) that builds one directly, the way the suite's own fixtures plant a bare, un-migrated job record for a
    dozen unrelated scenarios, is not the thing this guard exists to stop. `python -m supervisor.boundary` and
    similar one-shot tools construct no `Supervisor` either and are likewise untouched by it."""


def _unmigrated_job_files(layout: Layout) -> list[str]:
    bad = []
    candidates = sorted(list(layout.jobs.glob("*.json")) + list(layout.jobs.glob("archive/*/*.json")))
    for p in candidates:
        try:
            rec = read_json(p)
        except (OSError, ValueError):
            continue           # an unreadable record is jobs.py's own concern (R-03), not this guard's
        if jobs_mod.kind_of(rec) == "conversation":
            continue           # backs no Task document; stays outside this model entirely (§2.2), always has
        if (rec.get("migrated") or {}).get("attempts"):
            continue
        if rec.get("kind") is not None and isinstance(rec.get("seq"), int):
            continue
        bad.append(p.name)
    return bad


def _check_writer_guard(layout: Layout) -> None:
    bad = _unmigrated_job_files(layout)
    if not bad:
        return
    shown = ", ".join(bad[:10]) + (f" and {len(bad) - 10} more" if len(bad) > 10 else "")
    raise UnmigratedJobsError(
        f"{len(bad)} job record(s) under state/runtime/jobs/ carry neither a migration stamp "
        f"(migrated.attempts) nor an allocator-written kind/seq: {shown}. Run "
        f"`supervisor migrate attempts --dry-run --out <dir>`, review the mapping, then "
        f"`supervisor migrate attempts --apply --mapping <dir>/mapping.json --out <dir>` and "
        f"`supervisor migrate attempts --check --mapping <dir>/mapping.json --out <dir>` before dispatch resumes.")


class ForeignEnvironmentError(RuntimeError):
    """ATTEMPT_MODEL.md section 4.4's start-up guard: the launching environment this `supervisor run` process was
    started with already carries a job's own identity markers (`envconfig.FOREIGN_MARKERS`) — the shape H-001
    found by hand ("the loop had been launched from the desktop session with that session's environment"), a
    shell or a session that still holds another job's `SUPERVISOR_ATTEMPT`/`SUPERVISOR_JOB`/`SUPERVISOR_RUN_DIR`/
    `SUPERVISOR_PROGRESS_FILE` from earlier. Raised by `cli.run()` before it ever constructs a `Supervisor`, the
    same point `UnmigratedJobsError` is raised from — a test (or a library caller) that builds one directly is
    not the thing this guard exists to stop. `SUPERVISOR_ROOT` is never one of these markers: it is this loop's
    own self-reference (`envconfig.FOREIGN_MARKERS` excludes it), set by its own launcher or the owner to point
    this very process at a project, never evidence of being nested inside another supervisor's job."""


def _check_launch_environment_guard() -> None:
    from . import envconfig
    found = envconfig.foreign_markers()
    if not found:
        return
    raise ForeignEnvironmentError(
        f"this process's own launching environment already carries {', '.join(found)} — another supervisor's "
        "job identity, not this loop's own. Start the loop from a clean shell (or one that never ran a job), "
        "not from inside an agent session or a terminal that still holds a finished job's environment.")


def _job_fingerprint(task: Task) -> str:
    """A stable fingerprint of the one contract a delivery attempt actually runs under (R-12): its own job block
    and its dependencies. `dispatch()` stamps it on the attempt from the task's STORED document, never a caller's
    decorated view (a revision's prompt, a fallback's model/effort override): those change what the SAME attempt
    carries, not the contract a later `task.patch`/`task.set_job` could change out from under it. Compared again
    every turn (`supersede_changed_attempts`) against the task as it is now."""
    payload = {"job": scheduler.job_spec(task), "depends_on": sorted(task.depends_on)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _output_tail(run_dir: Path, lines: int = 40) -> str:
    """The tail of a process job's own stdout/stderr (R-12): what a plain subprocess leaves behind when it never
    wrote a HANDOFF.md or self-reported notes of its own — the only account of why it failed that exists."""
    parts = []
    for name in ("stderr.txt", "stdout.jsonl"):
        try:
            text = (run_dir / name).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            parts.append(f"{name}:\n" + "\n".join(text.splitlines()[-lines:]))
    return "\n\n".join(parts)


def worker_report(result, run_dir: str | None, limit: int = 2500, lane_adapter: str | None = None) -> str:
    """What the worker said about why it stopped — its notes, the head of its HANDOFF.md, or (R-12) the tail of
    its own stdout/stderr when it left neither — so the failure detail (revision brief, diagnosis brief, task
    record, owner item) carries the reason, not just "status BLOCKED". The output-tail fallback is scoped to a
    plain-process lane (`lane_adapter in gate.PLAIN_PROCESS_LANES`: `process`/`python`), not merely "not a model
    lane" (P3-1) — a plugin model lane or `human` is neither, and leaking its own raw transcript (e.g. an
    incidental "run /login" line) is exactly what R-12's own finding was never about."""
    parts = []
    if getattr(result, "notes", None):
        parts.append(str(result.notes).strip())
    if run_dir:
        hp = Path(run_dir) / "HANDOFF.md"
        try:
            if hp.exists():
                parts.append(hp.read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            pass
        if not parts and lane_adapter in gate.PLAIN_PROCESS_LANES:
            tail = _output_tail(Path(run_dir))
            if tail:
                parts.append(tail)
    text = "\n\n".join(p for p in parts if p)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _handoff_reason(run_dir) -> str | None:
    """Why the supervisor asked this session for a handoff (`HANDOFF_REQUEST.json`), if it did."""
    try:
        return str(read_json(Path(run_dir) / "HANDOFF_REQUEST.json").get("reason") or "") or None
    except (OSError, ValueError, AttributeError):
        return None


def _epoch(iso: str) -> float:
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()                       # unparseable: treat as "just now" (never acts early)


def _residue_reasons(by_reason: dict[str, list[str]]) -> str:
    """The paths a revert left exactly as found, each under its own reason — one shared rendering for the task note
    and the owner item, so the two can never drift into saying different things about the same residue."""
    return "; ".join(f"{reason}: {', '.join(sorted(paths))}" for reason, paths in sorted(by_reason.items()))


def _restore_current_bytes(path: Path, current: bytes | None) -> bool:
    """Best-effort: put `path` back to hold exactly `current` (None: absent), after an `OSError` struck once a
    quarantine or restore step had already moved, copied, written or deleted something. Called only when a
    mutation actually happened — a failure before any of them already leaves the path exactly as found, nothing
    to undo. True when `path` now matches `current` (the "quarantine or restore error" reason stays honest);
    false when even this fails, so the caller can say the path is in an unknown state rather than call it found."""
    try:
        if current is None:
            if path.exists():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(current)
        return True
    except OSError:
        return False


def scheduler_routing(layout: Layout) -> dict:
    import yaml
    cfg = layout.config / "routing.yaml"
    try:
        return (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}) if cfg.exists() else {}
    except (OSError, yaml.YAMLError):
        return {}


RENDERED_PROJECTIONS = {"STATE.md", "TASKS.md", "ISSUES.md", "DECISIONS.md", "HUMAN_INBOX.md", "OWNER_TASKS.md"}

class Supervisor:
    def __init__(self, layout: Layout, sleep_s: float = 5.0, render_every: int = 1, unattended: bool = False):
        from . import plugins
        self.layout = layout.ensure()
        plugins.load(self.layout)                           # project lanes/checks register before any dispatch
        self.sleep_s = sleep_s
        self.render_every = render_every
        self.unattended = unattended
        self.iteration = 0
        self.errors: list[float] = []

    def plugins_blocked(self) -> str | None:
        """Why this loop must not dispatch, or None: a required plugin (project.yaml `plugins:`) failed to
        load, so the lane, screen or check it provides is missing. `supervisor doctor` shows the error; the owner
        fixes the module or marks it `required: false`."""
        from . import plugins
        return plugins.load(self.layout).blocking_reason()

    def _require_plugins(self) -> None:
        from . import plugins
        reason = self.plugins_blocked()
        if reason:
            raise plugins.PluginError(reason)

    # ------------------------------------------------------------ dispatch and ingestion

    def _adapter_failed(self, rec: dict, stage: str, exc: BaseException) -> None:
        """An adapter call raised: that is this attempt's failure (recorded on the job and the task through the
        normal FAILED path), never a loop error — a broken or flaky lane must not spend the loop's error budget."""
        error = f"adapter {rec['adapter']}.{stage}() raised {type(exc).__name__}: {exc}"[:1000]
        tasks_mod.log(self.layout, "adapter_error", "loop", task=rec["task_id"], attempt=rec["attempt_id"],
                      adapter=rec["adapter"], stage=stage, error=error, traceback=traceback.format_exc()[-2000:])
        rec = jobs_mod.set_phase(self.layout, rec["attempt_id"], rec["phase"], error=error, failed_stage=stage)
        self.ingest(rec, Result(status="FAILED", error=error))

    def dispatch(self, task: Task, adapter_name: str, deferred_until: float | None = None, note: str = "dispatch",
                kind: str = "delivery", extra: dict | None = None, reason: str | None = None,
                cause: str | None = None, attempt: int | None = None, parent: str | None = None) -> dict:
        """`cause` (§2.4/§2.6 of the attempt-identity design) is why this job exists; `attempt` is the delivery
        attempt it belongs to (omitted for a fresh delivery — allocate_job's `new_attempt` then opens the next
        one — or for a diagnosis raised before any delivery, which stays task-scoped, §2.5); `parent` is the job
        this one's cause names. Every caller that already knows these (a continuation, a reroute, a revision, a
        verify or a review attached to a known delivery, a diagnosis resolved from its failing job) passes them;
        one that has none to give (an ordinary scheduled delivery, a conversation turn) leaves them unset."""
        self._require_plugins()
        ad = REGISTRY[adapter_name]
        # `reason` (scheduler.resolve_lane, R-16) is the one line naming why this lane and model: durable on the
        # job record and the task's own attempt entry alike, so a model change (a senior revision, the return to
        # the base model, a fallback) is never silent again. Omitted, not written as a null, when a caller (a
        # review, a verify, a diagnosis, a conversation) has none to give — those records stay exactly as narrow
        # as before this.
        job_extra = {**(extra or {}), "reason": reason} if reason else dict(extra or {})
        # prior_round/digest_diff (R-15, ATTEMPT_MODEL.md 4.3) are this dispatch's own audit output, written beside
        # the run below; review_text_snapshot is the one of the three a LATER round still needs, so it alone stays
        # on the durable job record.
        run_dir_payload = {k: job_extra.pop(k) for k in ("prior_round", "digest_diff") if k in job_extra}
        if kind == "conversation":          # no Task document backs a conversation thread; nothing to count on
            n = len(jobs_mod.for_task(self.layout, task.id)) + 1
            attempt_id = f"{task.id}.a{n}"
            job_attempt = job_seq = job_scope = None
        else:
            # The id comes from the task's own `counters` block, allocated and recorded in one locked update
            # that also appends the attempt/job entry (`tasks.allocate_job`, §2.3), so the job record and the
            # task's own attempt list can never disagree (R-03) — the id itself is monotonic, so a lost record
            # between two allocations can never cause the next one to reuse it.
            new_attempt = kind == "delivery" and attempt is None
            job_id = tasks_mod.allocate_job(self.layout, task.id, kind, attempt=attempt, cause=cause,
                                            new_attempt=new_attempt, resource=adapter_name, parent=parent, note=note)
            attempt_id = job_id.id
            job_attempt, job_seq, job_scope = job_id.attempt, job_id.seq, job_id.scope
            # R-12 / RECOVERY_PLAN.md section 5: the STORED contract, re-read after allocate_job's own write — a
            # revision's prompt or a fallback's model override decorates the SAME attempt, never changes what a
            # `task.patch`/`task.set_job` could. `contract_rev` names the generation this job's brief was built
            # from, on every kind that has a task; `job_fingerprint` stays delivery-only (R-12's own narrower use).
            stored = tasks_mod.load(self.layout, task.id)
            job_extra["contract_rev"] = stored.contract_rev
            if kind == "delivery":
                job_extra["job_fingerprint"] = _job_fingerprint(stored)
        claim = needs_mod.claim_for(self.layout, task, kind)
        run_dir = self.layout.runs / task.id / attempt_id
        try:
            if run_dir.exists():            # never launch a second job into a directory one left behind (R-03)
                raise FileExistsError(f"run directory already exists: {run_dir}")
            spec = ad.prepare(self.layout, task, attempt_id)
        except Exception as exc:                              # noqa: BLE001 - the attempt fails, the loop goes on
            spec = {"adapter": adapter_name, "task_id": task.id, "attempt_id": attempt_id, "kind": kind}
            rec = jobs_mod.prepare(self.layout, task.id, attempt_id, adapter_name, spec,
                                   gpu=gate.holds_resource(self.layout, task, adapter_name, kind), claim=claim,
                                   kind=kind, attempt=job_attempt, seq=job_seq, parent=parent, cause=cause,
                                   scope=job_scope, **job_extra)
            self._adapter_failed(rec, "prepare", exc)
            return jobs_mod.read(self.layout, attempt_id)
        spec["kind"] = kind
        if job_attempt is not None:
            spec["attempt"] = job_attempt           # process.job_identity_env: SUPERVISOR_ATTEMPT_NO, no id parse
        if gate.owned_for(task, adapter_name, kind):          # every writing job: declared roots or exclusive
            atomic_json(Path(spec["run_dir"]) / "before_manifest.json", verify_mod.snapshot(self.layout, task))
        if extra and extra.get("review_frozen") is not None and spec.get("run_dir"):
            atomic_json(Path(spec["run_dir"]) / "frozen_inputs.json", extra["review_frozen"])   # audit trail
        if run_dir_payload.get("prior_round") is not None and spec.get("run_dir"):
            atomic_json(Path(spec["run_dir"]) / "prior_round.json", run_dir_payload["prior_round"])   # audit trail
        if run_dir_payload.get("digest_diff") and spec.get("run_dir"):
            (Path(spec["run_dir"]) / "digest_diff.patch").write_text(run_dir_payload["digest_diff"], encoding="utf-8")
        # One durable write carries everything a continuation or ingestion needs: a crash between prepare() and a
        # second record write used to leave a review job without its role and discard the finished verdict.
        rec = jobs_mod.prepare(self.layout, task.id, attempt_id, adapter_name, spec,
                               gpu=gate.holds_resource(self.layout, task, adapter_name, kind), claim=claim,
                               kind=kind, attempt=job_attempt, seq=job_seq, parent=parent, cause=cause,
                               scope=job_scope, env_digest=spec.get("env_digest"),
                               **job_extra)                   # durable BEFORE launch
        if claim:
            tasks_mod.log(self.layout, "claim_taken", "loop", claim=claim, task=task.id, attempt=attempt_id)
        if kind != "conversation":            # the attempt itself was already recorded when the id was allocated
            tasks_mod.annotate_attempt(self.layout, task.id, attempt_id, run_dir=spec.get("run_dir"))
        if deferred_until:
            return jobs_mod.set_phase(self.layout, attempt_id, "PREPARED", deferred_until=deferred_until, deferred_reason=note)
        return self.launch(rec)

    def dispatch_delivery(self, task: Task, name: str) -> dict:
        """A scheduled delivery job on `name` - scheduler.plan() already resolved the owner role's own fallback
        when its primary lane was unavailable (D-012 F-B Change 1), the same rule a fallback review already
        applies. Re-derive the fallback's model/effort overrides the same way plan() picked `name`, so a role's
        own task runs with the fallback's own keys whatever `flag: same_vendor` says on the block - the flag
        decides only the attempt note and the `same_vendor` extra on the record (review.dispatch() and
        _lane_login_failed()'s reroute already work this way; release review 2.2.0 F2). An ordinary pick with no
        overrides at all (e.g. the unattended claude_api switch, which is a different kind of lane change)
        dispatches with no job override.

        `home` (scheduler.adapter_name(task), ignoring the unattended switch) can disagree with `name` (what
        plan() actually picked, e.g. claude_api, whose own lane_for() call saw no `engine` and so no capacity
        check at all): the overrides and the same-vendor flag are only ever this job's own if the lane they
        were resolved for is the lane it is actually dispatching on.

        `scheduler.resolve_lane` (R-16) is the one answer this, an automatic revision, a continuation and a
        fallback all ask the same way; its `reason` is stored on the job record and shown where the job is
        listed."""
        role = scheduler.job_spec(task).get("role") or task.owner_role
        resolved = scheduler.resolve_lane(self.layout, task, role=role)
        if not (resolved.model or resolved.effort) or resolved.adapter != name:
            reason = resolved.reason if resolved.adapter == name else f"scheduled on {name}"
            return self.dispatch(task, name, reason=reason, cause="scheduled")
        overrides = {k: v for k, v in (("model", resolved.model), ("effort", resolved.effort)) if v}
        doc = task.model_dump(mode="json")
        doc["job"] = {**scheduler.job_spec(task), "adapter": name, **overrides}
        view = Task.model_validate(doc)
        note = f"dispatch:{role}" + (" (same-vendor fallback)" if resolved.same_vendor else "")
        return self.dispatch(view, name, note=note, reason=resolved.reason, extra={"same_vendor": resolved.same_vendor},
                             cause="scheduled")

    MAX_CONTINUATIONS = 3

    @staticmethod
    def _continuations(recs: list[dict], attempt: int | None = None) -> list[dict]:
        """Continuation sessions among `recs`: the `cause` field, fields only (§6: the `deferred_reason` prefix
        fallback is deleted — the start-up guard already refuses to dispatch on a store holding a record with no
        `cause` field at all). `attempt`, when given, scopes the count to that delivery attempt's own sessions
        instead of every continuation the task ever queued."""
        out = [r for r in recs if r.get("cause") == "continuation"]
        if attempt is not None:
            out = [r for r in out if r.get("attempt", attempt) == attempt]
        return out

    def requeue(self, rec: dict, result: Result, reason: str) -> None:
        """Capacity limit or handoff-incomplete: queue a continuation attempt FIRST (crash-safe), then close this attempt
        without a delivery; the adapter's prepare() points the fresh session at the prior HANDOFF.md. Capped per task:
        after MAX_CONTINUATIONS the task is BLOCKED for the owner."""
        task_id = rec["task_id"]
        all_recs = jobs_mod.for_task(self.layout, task_id)
        if any(r.get("parent") == rec["attempt_id"] for r in all_recs):
            jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(),
                               error=result.error, continued=True)       # continuation already queued before a crash
            return
        prior = self._continuations(all_recs, rec.get("attempt"))
        n = len(prior)
        until = result.reset_epoch or time.time()
        if result.status == "DEFERRED" and not result.raw.get("measured_window"):
            until = max(until, time.time() + 3600 * (2 ** n))              # synthetic window grows per repeat
        tasks_mod.log(self.layout, "job_requeued", "loop", task=task_id, attempt=rec["attempt_id"], reason=reason,
                      result=result.status, error=result.error, until=until, continuation=n + 1)
        if n >= self.MAX_CONTINUATIONS:
            # D-010 Consequences: this used to go BLOCKED-for-owner directly, bypassing the manager's diagnosis.
            # retry=False because a blind re-dispatch of the same brief needs a continuation chain too — the
            # diagnosis route (bounded by its own DIAGNOSIS_BUDGET) decides requeue/defer/architect/owner instead.
            # `continued`+`capped` (R4): an intentionally capped attempt, never a torn write repair_continuations()
            # must re-dispatch — a fresh continuation grant (escalation.grant_continuations) is the only way past it.
            jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(),
                               error=result.error, continued=True, capped=True)
            escalation_mod.record_failure(self, task_id, "continuations_exhausted",
                                          f"{rec['attempt_id']}: {reason} after {n} continuations",
                                          event=f"{rec['attempt_id']}:continuations", retry=False)
            return
        # Tagged with the task's CURRENT grant generation (F2), not whatever a later replay of some other call
        # finds: grant_continuations() only ever retires a marker whose own tag is older than the generation in
        # force when it runs, so a fresh continuation this chain just earned can never be mistaken for one an
        # earlier, unrelated grant left behind.
        grant = escalation_mod.current_grant(self.layout, task_id)
        kind = (rec.get("spec") or {}).get("kind", "delivery")
        if kind == "review":
            task = review_mod.rebuild(self.layout, rec)
            if task is None:                                  # inputs moved on; pending() queues a fresh review instead
                jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(), error=result.error)
                return
            extra = {k: rec[k] for k in ("review_role", "review_digest", "same_vendor", "review_frozen") if k in rec}
            extra["continuation_grant"] = grant
            note = f"continuation after {reason}" + (" (same-vendor fallback)" if rec.get("same_vendor") else "")
            self.dispatch(task, rec["adapter"], deferred_until=until, note=note, kind="review", extra=extra,
                         cause="continuation", attempt=rec.get("attempt"), parent=rec["attempt_id"])
        elif kind == diagnosis_mod.KIND:
            task = diagnosis_mod.rebuild(self.layout, rec)
            if task is None:
                jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(), error=result.error)
                return
            extra = {k: rec[k] for k in rec if k.startswith("diag_")}
            extra["continuation_grant"] = grant
            self.dispatch(task, rec["adapter"], deferred_until=until, note=f"continuation after {reason}", kind=diagnosis_mod.KIND, extra=extra,
                         cause="continuation", attempt=rec.get("attempt"), parent=rec["attempt_id"])
        else:
            task = tasks_mod.load(self.layout, task_id)
            overrides = review_mod.fallback_overrides(rec)
            if overrides:
                doc = task.model_dump(mode="json")
                doc["job"] = {**scheduler.job_spec(task), **overrides, "adapter": rec["adapter"]}
                task = Task.model_validate(doc)
            extra = {"continuation_grant": grant}
            note = f"continuation after {reason}"
            lane_reason = f"role {task.owner_role}'s own attempt, continued on its lane ({rec['adapter']})"
            if "same_vendor" in rec:
                extra["same_vendor"] = rec["same_vendor"]
                if rec["same_vendor"]:
                    note += " (same-vendor fallback)"
                    lane_reason += "; same-vendor fallback, carried over from the attempt being continued"
            self.dispatch(task, rec["adapter"], deferred_until=until, note=note, extra=extra, reason=lane_reason,
                         cause="continuation", attempt=rec.get("attempt"), parent=rec["attempt_id"])
        jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(), error=result.error,
                           continued=True)
        # The continuation is queued either way (crash-safe, same as before F11): a held task's queued record simply
        # never launches (gate.check's held condition, below) until the owner releases it, so the note says so
        # instead of leaving the queue silent about why nothing ran.
        held = tasks_mod.load(self.layout, task_id).held
        held_note = ""
        if held:
            held_detail = f" ({held['note']})" if held.get("note") else ""
            held_note = f" — held by the owner{held_detail}; it will launch once they release the Hold"
        tasks_mod.add_note(self.layout, task_id, f"{rec['attempt_id']}: {reason}; continuation {n + 1} queued{held_note}", actor="loop")

    def home_lane(self, rec: dict) -> str | None:
        """The lane a delivery runs on when nothing moved it (scheduler.adapter_name: the task's job.adapter, role or
        cost lane), if that is not the lane of `rec` — i.e. the unattended switch put it on `rec`'s lane — and is
        registered. None for every other job: a review, a diagnosis or a task pinned to this lane waits instead."""
        if (rec.get("spec") or {}).get("kind", "delivery") != "delivery":
            return None
        try:
            lane = scheduler.adapter_name(tasks_mod.load(self.layout, rec["task_id"]), self.layout)
        except tasks_mod.TaskError:
            return None
        return lane if lane != rec["adapter"] and lane in REGISTRY else None

    def reroute(self, rec: dict, lane: str, reason: str, job_overrides: dict | None = None,
               extra: dict | None = None, kind: str = "delivery") -> dict:
        """The lane refused every job for now (LaneUnavailable: no model ran, nothing was spent) and the task has a lane
        of its own: close the refused record FIRST, naming where the work goes, then queue it there. No attempt failed,
        so nothing escalates. The new attempt names the refused one (`rerouted_from`), so repair_continuations()
        can tell a reroute whose successor exists from one a crash interrupted before it, and queues only the
        latter; the refused record is never launched on its old lane again. `job_overrides` (a role's fallback's
        own model/effort, D-012 F-B Change 2) are merged into the dispatched job; `extra` (`same_vendor`) reaches
        the new job record the same way a fallback review's does. Both are written into the SAME set_phase that
        names the lane (`rerouted_job`, `rerouted_extra`), so a crash before the successor lands leaves them as
        durable as `rerouted_to` itself: `_repair_reroutes()` replays them instead of launching the fallback lane
        with the role profile's own model and no flag — an attempt that fails and spends a revision slot, in the
        one path that exists to recover a reroute.

        `kind`, when `diagnosis_mod.KIND`, rebuilds the record's own diagnosis the way `requeue()`'s diagnosis
        branch does (`diagnosis_mod.rebuild`, carrying every `diag_*` extra) instead of re-dispatching the task
        as a fresh plain delivery attempt — a diagnosis has its own prompt and decision to make, not the task's
        original brief. `job_overrides` is not applied there, matching `requeue()`'s own diagnosis branch (a
        continuation already loses it the same way). If the task has moved on and nothing rebuilds, the refused
        record simply closes with no successor, exactly as `requeue()`'s diagnosis branch does. The per-kind
        rebuild itself (`_reroute_rebuild`) is shared with `_repair_reroutes()`, so a crash between this
        record's own close and its successor's write recovers exactly the same way."""
        rec = jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result="REROUTED", finished=now_iso(),
                                 error=reason, rerouted_to=lane, rerouted_job=job_overrides or None,
                                 rerouted_extra=extra or None)
        tasks_mod.log(self.layout, "lane_rerouted", "loop", task=rec["task_id"], attempt=rec["attempt_id"],
                      adapter=rec["adapter"], to=lane, reason=reason)
        try:
            tasks_mod.add_note(self.layout, rec["task_id"], f"{rec['attempt_id']}: {rec['adapter']} unavailable "
                               f"({reason}); rerouted to {lane}", actor="loop")
        except Exception:                             # noqa: BLE001 - F10: the record is already closed REROUTED;
            pass                                       # an unreadable task file must not raise past this turn
        task, kind_extra = self._reroute_rebuild(rec, kind)
        if task is None:
            jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", continued=True)
            return jobs_mod.read(self.layout, rec["attempt_id"])
        self.dispatch(task, lane, note=f"rerouted from {rec['adapter']}", kind=kind,
                      extra={"rerouted_from": rec["attempt_id"], **kind_extra},
                      reason=f"fallback: {rec['adapter']} unavailable ({reason}); rerouted to {lane}",
                      cause="reroute", attempt=rec.get("attempt"), parent=rec["attempt_id"])
        jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", continued=True)
        return jobs_mod.read(self.layout, rec["attempt_id"])

    def _reroute_rebuild(self, rec: dict, kind: str) -> tuple[Task | None, dict]:
        """The task and extras a reroute's successor dispatches with, by `kind` - diagnosis rebuilds via
        `diagnosis_mod.rebuild` (it tolerates a BLOCKED task on purpose, the common status for one, and returns
        None only once the task has moved past it: COMPLETE or CANCELLED) and carries its `diag_*` extras;
        every other kind just reloads the task, with the fallback's own model/effort (`rerouted_job`) merged
        into its job block. Both shapes carry the dispatch extras the reroute resolved (`rerouted_extra`:
        D-012 F-B's same-vendor flag). All of it is read back from the refused record, where `reroute()` wrote
        it in the one set_phase that named the lane, so `reroute()` (the live path) and `_repair_reroutes()`
        (torn-write recovery) can never drift about what a reroute's successor carries - not its kind, not its
        extras and not its job. A review never reaches either (reroute() is never called for one).

        `rerouted_job` is not applied to a diagnosis, matching `requeue()`'s own diagnosis branch (its prompt
        and lane keys come from the diagnosis itself, not the task's job block)."""
        extra = dict(rec.get("rerouted_extra") or {})
        if kind == diagnosis_mod.KIND:
            task = diagnosis_mod.rebuild(self.layout, rec)
            if task is None:
                return None, {}
            return task, {**{k: rec[k] for k in rec if k.startswith("diag_")}, **extra}
        task = tasks_mod.load(self.layout, rec["task_id"])
        overrides = rec.get("rerouted_job") or {}
        if overrides:
            doc = task.model_dump(mode="json")
            doc["job"] = {**scheduler.job_spec(task), **overrides, "adapter": rec["rerouted_to"]}
            task = Task.model_validate(doc)
        return task, extra

    def _ensure_in_progress(self, task_id: str, note: str, kind: str = "delivery") -> None:
        t = tasks_mod.load(self.layout, task_id)
        if t.held:
            return     # an owner Hold is durable on the task (F9) - the loop's own launch never reverses it (F11)
        if kind == "delivery" and tasks_mod.unmet_dependency(self.layout, t) is not None:
            return     # G6, 2.4.0: the gate refuses the launch that follows; never flip FAILED/BLOCKED first
        if t.status in (TaskStatus.NOT_STARTED, TaskStatus.READY, TaskStatus.FAILED, TaskStatus.BLOCKED):
            tasks_mod.set_status(self.layout, task_id, TaskStatus.IN_PROGRESS, actor="loop", note=note)

    @staticmethod
    def _agent_role(rec: dict, kind: str) -> str | None:
        """The role a job record runs as, for the gate's `active: false` check: a review's reviewer, a diagnosis's
        diagnosing role; a delivery defaults to the task's owner role inside the gate. A verify job runs for no agent
        role, and neither does a chat turn: the `conversation` lane is the owner's own channel, not a role that has
        or lacks work, so its `active` flag means nothing (1.2.2 read it and parked every reply)."""
        if kind == "review":
            return rec.get("review_role")
        if kind == diagnosis_mod.KIND:
            return rec.get("diag_role")
        return None

    def launch(self, rec: dict) -> dict:
        ad = REGISTRY[rec["adapter"]]
        job_kind = (rec.get("spec") or {}).get("kind", "delivery")
        conversation = job_kind == "conversation"
        try:
            handle = ad.recover(rec["spec"])                  # a previous loop may have launched it already
        except Exception as exc:                              # noqa: BLE001 - this attempt's failure, not the loop's
            self._adapter_failed(rec, "recover", exc)
            return jobs_mod.read(self.layout, rec["attempt_id"])
        if handle is not None:
            rec = jobs_mod.set_phase(self.layout, rec["attempt_id"], "SUBMITTED", handle=handle, submitted=now_iso(), recovered=True)
            tasks_mod.log(self.layout, "job_recovered", "loop", task=rec["task_id"], attempt=rec["attempt_id"])
            return rec
        if rec.get("deferred_until") and float(rec["deferred_until"]) > time.time():
            engine = getattr(ad, "engine", None)
            try:
                if engine:
                    capacity.require(self.layout, engine)     # measured/owner state is authoritative over the hint
                elif not (rec.get("deferred_lane") and ad.lane_ready(self.layout)):
                    return rec                                # a timed wait, or the lane still unavailable
            except CapacityDeferred:
                return rec                                    # still gated: wait, costs nothing
            except Exception:                                 # noqa: BLE001 - a broken readiness check means wait
                return rec
            jobs_mod.set_phase(self.layout, rec["attempt_id"], "PREPARED", deferred_until=0, deferred_lane=None)
        # The one gate: decision, capacity, limits and ownership — for every launch path alike. A refused
        # record stays PREPARED and is retried by reconcile() each iteration; the reason is recorded once per change.
        kind = job_kind
        try:
            task = None if conversation else tasks_mod.load(self.layout, rec["task_id"])
        except tasks_mod.TaskError:                           # record without a readable task: owns nothing, still gated
            task = None
        running = [r for r in jobs_mod.open_records(self.layout)
                   if r["phase"] in gate.ACTIVE_PHASES and r["attempt_id"] != rec["attempt_id"]]
        reason = gate.check(self.layout, task, rec["adapter"], kind, running, role=self._agent_role(rec, kind))
        if reason:
            if rec.get("held") != reason:
                tasks_mod.log(self.layout, "launch_held", "loop", task=rec["task_id"], attempt=rec["attempt_id"], reason=reason)
            return jobs_mod.set_phase(self.layout, rec["attempt_id"], "PREPARED", held=reason)
        # Captured before the reset below rebinds `rec` to a record whose `held` is already None - every
        # later refusal in this method compares against this, not against `rec.get("held")`, so each one still
        # logs once per reason, not once per turn for as long as the same refusal repeats.
        prior_held = rec.get("held")
        if prior_held:
            rec = jobs_mod.set_phase(self.layout, rec["attempt_id"], "PREPARED", held=None)
        lease_name = leases.lease_name_for(self.layout, bool(rec.get("gpu")), rec.get("claim"))
        if lease_name and not leases.resource_lease(self.layout, lease_name).acquire(wait_s=0):
            # gate.lease_blocked() already peeked this a moment ago; taking it for real happens only here, right
            # before submit(), so a job that never reaches this line never claims a lease gate.check() did not mean.
            reason = f"{lease_name} lease unavailable: another project holds it"
            if prior_held != reason:
                tasks_mod.log(self.layout, "launch_held", "loop", task=rec["task_id"], attempt=rec["attempt_id"], reason=reason)
            return jobs_mod.set_phase(self.layout, rec["attempt_id"], "PREPARED", held=reason)
        if not conversation and job_kind != diagnosis_mod.KIND:   # a diagnosis judges a BLOCKED task
            try:
                self._ensure_in_progress(rec["task_id"], f"launch {rec['attempt_id']} via {rec['adapter']}", kind=job_kind)
            except tasks_mod.TaskError as exc:
                # F1: the task file itself is unreadable (an editor's mid-write save, most likely on an active
                # task - exactly this one). load_all()'s own turn scan already reports and dedupes the owner
                # item; this record is simply held until the file is readable again, the same shape gate.check's
                # own held branch above uses, rather than spending the loop's error budget on a bad input file.
                # Run only after the one gate and the GPU lease both pass: a refused launch must never flip a
                # FAILED/BLOCKED task's status when nothing is actually about to run.
                reason = f"task file unreadable: {exc}"
                if prior_held != reason:
                    tasks_mod.log(self.layout, "launch_held", "loop", task=rec["task_id"], attempt=rec["attempt_id"], reason=reason)
                return jobs_mod.set_phase(self.layout, rec["attempt_id"], "PREPARED", held=reason)
        attempts = int(rec.get("launch_attempts", 0)) + 1
        jobs_mod.set_phase(self.layout, rec["attempt_id"], "PREPARED", launch_attempts=attempts)
        if attempts > 2:
            jobs_mod.set_phase(self.layout, rec["attempt_id"], "FAILED", error="launch attempted twice without obtaining a handle")
            if conversation:
                conversations_mod.fail(self.layout, rec, "launch attempted twice without obtaining a handle")
            else:
                tasks_mod.set_status(self.layout, rec["task_id"], TaskStatus.FAILED, actor="loop",
                                     note=f"{rec['attempt_id']}: launch attempted twice without a handle (submit error or repeated crash)")
            return rec
        runtime.heartbeat(self.layout, "RUNNING", busy=rec["attempt_id"])   # synchronous adapters may take a while
        try:
            handle = ad.submit(rec["spec"])
        except CapacityDeferred as exc:                       # no model was launched; not an attempt
            lane = self.home_lane(rec) if isinstance(exc, LaneUnavailable) else None
            if lane:
                return self.reroute(rec, lane, str(exc))
            # deferred_lane: the lane, not the clock, gates this record, so lane_ready() may release it early.
            rec = jobs_mod.set_phase(self.layout, rec["attempt_id"], "PREPARED", launch_attempts=attempts - 1,
                                     deferred_until=exc.reset_epoch, deferred_reason=str(exc),
                                     deferred_lane=isinstance(exc, LaneUnavailable) or None)
            tasks_mod.log(self.layout, "capacity_deferred", "loop", task=rec["task_id"], attempt=rec["attempt_id"], until=exc.reset_epoch)
            return rec
        except Exception as exc:                              # noqa: BLE001 - nothing launched: the attempt fails here
            self._adapter_failed(rec, "submit", exc)
            return jobs_mod.read(self.layout, rec["attempt_id"])
        rec = jobs_mod.set_phase(self.layout, rec["attempt_id"], "SUBMITTED", handle=handle, submitted=now_iso())
        tasks_mod.log(self.layout, "job_submitted", "loop", task=rec["task_id"], attempt=rec["attempt_id"], adapter=rec["adapter"])
        return rec

    def _expand_artifacts(self, patterns: list[str]) -> tuple[list[str], list[str]]:
        """Glob-expand artifact patterns relative to the root; returns (files, patterns_without_match)."""
        files, missing = [], []
        for pat in patterns:
            matches = sorted(p for p in self.layout.root.glob(pat) if p.is_file()) if any(ch in pat for ch in "*?[") else \
                      ([self.layout.root / pat] if (self.layout.root / pat).is_file() else [])
            if matches:
                files += [str(m.relative_to(self.layout.root).as_posix()) for m in matches]
            elif (self.layout.root / pat).is_dir():                 # a directory never matches: say so, not "missing"
                missing.append(f"{pat} (a directory — the task contract must name files or a glob)")
            else:
                missing.append(pat)
        return sorted(set(files)), missing

    def _before_manifest(self, rec: dict) -> dict | None:
        run_dir = (rec.get("spec") or {}).get("run_dir")
        if not run_dir:
            return None
        p = Path(run_dir) / "before_manifest.json"
        return read_json(p) if p.exists() else None

    def _revert_residue(self, task_id: str, residue: dict[str, dict], run_dir: str | None) -> None:
        """M11(a), first shape (2.5.0): restore the unowned paths a rejected attempt changed, now, before the retry's
        own baseline is taken. Nothing is deleted: the path's current content is quarantined under
        `<run dir>/undeclared/<path>` first — moved there when the attempt created the path, copied there when it
        changed one that already existed — so the owner can audit and undo every action. A path is restored only
        when `git`'s own content for it at HEAD hashes to the `before` digest the snapshot recorded (never guessed
        at); a link, a directory, a file above LARGE_FILE_BYTES, one git cannot account for, or one a quarantine/
        restore OSError hit is left exactly as found instead — one reason string per cause (not one blended
        sentence), because the next attempt will otherwise fail on something it cannot fix. A quarantine/restore
        OSError raised after a move, copy, write or unlink already happened (the created-path move in particular,
        which takes the path out of the tree before the error can strike) is put back to the content it held
        before this call, so "left exactly as found" stays true of it too; only when that put-back itself fails
        does the reason say the path's state is no longer known, rather than call it found regardless.

        Each restore is itself a supervisor write into the working tree, at a path outside the rejected attempt's
        owned roots — exactly the paths a second, concurrently writing task's own verification also scans and is
        not excused on (its `ignore` only covers the roots another job *owns*, never a stranger's residue). Left
        unrecorded, this write re-creates the class M11(a) repairs: a rejection of one attempt inside another's
        verification window would charge that attempt for the revert. So the current content is read and the
        transition recorded through `verify.record_own_write()` — which can now express a deletion (`after=None`)
        as well as a replacement — before the quarantine copy and the restore/unlink, per that function's own
        contract (a crash between the record and the write leaves a transition nothing took, never one blamed on
        an agent). The quarantine copy itself is not recorded: it is written under the run directory, which
        `repo_manifest`'s `EXCLUDED_DIRS` already prunes from every scan, so no concurrent verification ever sees
        it. After each filesystem write this re-reads what was just written and compares it to the bytes it was
        meant to hold; a mismatch is folded into the same "a quarantine or restore error" case as an `OSError`,
        since a silently truncated copy or restore is exactly that kind of failure with no exception raised.

        Which case gets which record (`CONTRIBUTOR_RULING.md` §5.2's Log row). A path that *was* reverted: one
        `residue_reverted` event carrying both of the snapshot's digests and the quarantine location, and its name
        in one note on the task — and no owner item, because an automatic repair that files an inbox item against
        itself is not a repair. A path left exactly as found: its own reason in that task's "not reverted" note and
        one deduped owner decision item, and no `residue_reverted` event, because a half-repair is not silently
        called a repair."""
        root = self.layout.root
        quarantine = Path(run_dir) / "undeclared" if run_dir else None
        reverted: list[tuple[str, str | None, str | None]] = []
        left: list[tuple[str, str]] = []
        for rel, entry in residue.items():
            before_e, after_e = entry.get("before") or {}, entry.get("after") or {}
            if quarantine is None:
                left.append((rel, "no run directory to quarantine the current content into"))
                continue
            kinds = {before_e.get("kind"), after_e.get("kind")}
            if kinds - {None, "missing", "file"}:
                reason = "a file above LARGE_FILE_BYTES" if "large" in kinds else "a link or a directory"
                left.append((rel, reason))             # not this mechanism's to restore
                continue
            path = root / rel
            content = None
            if before_e.get("kind") == "file":
                content = self._git_blob_at_head(rel, str(before_e.get("sha256") or ""))
                if content is None:
                    left.append((rel, "a path git cannot account for"))   # never guessed at
                    continue
            current = None
            mutated = False                         # set just before a move/copy/unlink/write starts, not after
                                                      # it returns, so a failure inside the call still counts
            try:
                current = path.read_bytes() if path.exists() else None
                verify_mod.record_own_write(self.layout, rel, before=current, after=content, actor="loop",
                                            why=f"M11(a) residue revert for {task_id}")
                if current is not None:
                    dest = quarantine / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    mutated = True              # set before the move/copy, not after: a failure inside it
                    if before_e.get("kind") in (None, "missing"):
                        path.replace(dest)          # the attempt created it: moved, nothing is deleted
                    else:
                        shutil.copy2(path, dest)    # the attempt changed it: copied before the restore
                    if dest.read_bytes() != current:
                        raise OSError("quarantine copy does not match the content it was meant to hold")
                if content is None:                 # before == missing: restoring means the path does not exist
                    if path.exists():
                        mutated = True               # set before the unlink, not after
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    mutated = True                   # set before the write, not after: a partial write still counts
                    path.write_bytes(content)
                    if path.read_bytes() != content:
                        raise OSError("restored content does not match the content it was meant to hold")
            except OSError:
                # a failure before any mutation already leaves the path exactly as found; a failure after one
                # (the created-path move, in particular, already took the path out of the tree) is put back to
                # `current` before this is called "found" rather than described wrongly.
                if mutated and not _restore_current_bytes(path, current):
                    reason = ("a quarantine or restore error, and the path could not be put back as it stood "
                              "before the revert")
                    left.append((rel, reason))
                else:
                    left.append((rel, "a quarantine or restore error"))
                continue
            reverted.append((rel, before_e.get("sha256"), after_e.get("sha256")))
        for rel, before_sha, after_sha in reverted:
            tasks_mod.log(self.layout, "residue_reverted", "loop", task=task_id, path=rel,
                          before_sha256=before_sha, after_sha256=after_sha,
                          quarantine=(quarantine / rel).as_posix() if quarantine else None)
        if reverted and quarantine is not None:    # a revert needs a quarantine: with no run directory nothing moves
            tasks_mod.add_note(self.layout, task_id,
                               f"residue reverted ({', '.join(rel for rel, _, _ in reverted)}); the content found "
                               f"there is quarantined under {quarantine.as_posix()}", actor="loop")
        if left:
            by_reason: dict[str, list[str]] = {}
            for rel, reason in left:
                by_reason.setdefault(reason, []).append(rel)
            tasks_mod.add_note(self.layout, task_id, f"residue not reverted ({_residue_reasons(by_reason)})",
                               actor="loop")
            self._notify_residue_left(task_id, by_reason)

    def _notify_residue_left(self, task_id: str, by_reason: dict[str, list[str]]) -> None:
        """`CONTRIBUTOR_RULING.md` §5.2's Limits row: a path left exactly as found needs an owner decision, because
        the next attempt will otherwise fail on something it cannot fix. One deduped item per (task, this exact set
        of left paths) — not one per rejection, so a retry rejected again for the same untouched residue does not
        stack a second identical question (the login-required notice, `_notify_login_required`, is the same shape).
        The item names each path under its own reason, as the task note does: the reason is the thing the owner has
        to act on, and it is this item, not the note, that the Limits row exists to route."""
        key = sorted(rel for paths in by_reason.values() for rel in paths)
        if any(i.get("task_id") == task_id and (i.get("residue_left") or []) == key
               for i in inbox_mod.open_items(self.layout, "owner")):
            return
        summary, detail = inbox_mod.fit(
            f"{task_id}'s rejected delivery left {len(key)} unowned path(s) exactly as found — "
            f"{_residue_reasons(by_reason)}. "
            "The next attempt will fail on this residue unless it is cleared by hand.")
        try:
            inbox_mod.create(self.layout, for_role="owner", subject=f"Residue left on {task_id}",
                             summary=summary, detail=detail, kind="question", from_="loop", task_id=task_id,
                             extra={"residue_left": key})
        except inbox_mod.InboxFull:
            tasks_mod.log(self.layout, "inbox_full", "loop", reason="residue_left", task=task_id)

    def _git_blob_at_head(self, rel: str, before_sha256: str) -> bytes | None:
        """`rel`'s content at HEAD, gated on its own digest matching `before_sha256` — the snapshot's own claim about
        what the path held before the rejected attempt touched it. None when git has no such path, the read fails,
        or the content does not hash to the digest the snapshot recorded: a restore never guesses."""
        import hashlib
        import subprocess
        try:
            proc = subprocess.run(["git", "-C", str(self.layout.root), "show", f"HEAD:{rel}"],
                                  capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        content = proc.stdout
        if hashlib.sha256(content).hexdigest() != before_sha256:
            return None
        return content

    def _concurrent_owned(self, rec: dict) -> tuple[str, ...]:
        """Ownership of OTHER tasks' delivery jobs that overlapped this attempt: their writes are not this delivery's
        undeclared changes (supervisor.verify `ignore`). Reviews and chat turns own nothing. An exclusive job that ran
        alongside (only possible next to a deterministic job) yields "." — the scan then proves nothing and says so."""
        started = str(rec.get("created") or "")
        out: list[str] = []
        for other in jobs_mod.all_records(self.layout):
            if other["task_id"] == rec["task_id"] or (other.get("spec") or {}).get("kind", "delivery") != "delivery":
                continue
            if other["phase"] == "DONE" and str(other.get("finished") or "") < started:
                continue
            try:
                task = tasks_mod.load(self.layout, other["task_id"])
            except Exception:                            # noqa: BLE001 - a task we cannot read narrows nothing
                continue
            out += gate.owned_for(task, other.get("adapter", ""), "delivery")
        return tuple(sorted(set(out)))

    def _notify_login_required(self, layout: Layout, engine: str | None, until: float, error: str) -> None:
        """D-010's first category: a credential only the owner can renew. One deduped item per engine per login
        window - not one per failed attempt, since mark_login_failed() moves `until` forward on every call and
        fingerprinting on it would stack one item per failure. Every branch of _lane_login_failed calls this
        alike (review / fallback reroute / no-fallback wait), so a role with a fallback configured no longer
        spends the Claude subscription silently once Codex's free month lapses. A full owner inbox must never
        turn this token-free notification into anything that touches the attempt budget.

        The dedupe only ever matches an item that named this `engine` by itself: with `engine` None (an
        adapter with no `engine` of its own), `.get("login_engine")` returns None for any ordinary item that
        never carried the key at all, and `None == None` would silently swallow this notice behind whatever
        unrelated question the owner already had open."""
        if engine is not None and any(i.get("login_engine") == engine for i in inbox_mod.open_items(layout, "owner")):
            return
        summary, detail = inbox_mod.fit(
            f"{engine or 'a lane'} reports it is not logged in; dispatch to it waits (or hands over to the "
            f"configured fallback) until the credential is renewed. Run the login the error names, on the "
            f"workstation, to clear this. {error}")
        try:
            inbox_mod.create(layout, for_role="owner", subject=f"Login needed: {engine or 'lane'}",
                             summary=summary, detail=detail, kind="question", from_="loop",
                             extra={"login_engine": engine, "login_until": until})
        except inbox_mod.InboxFull:
            tasks_mod.log(layout, "inbox_full", "loop", reason="login_required", engine=engine)

    def _lane_login_failed(self, rec: dict, result: Result, kind: str) -> None:
        """D-012 F-B Change 2: the lane's own login failed (adapters' AUTH_FAILURE_RE, matched only against
        diagnostics - stderr, system/error events - never a model's ordinary answer, so a real failure or an
        answer that merely mentions a login never reaches here). Unavailable, not failed: mark the engine so
        every capacity.require() caller (review.lane_for, gate.check, scheduler.plan) treats it exactly like a
        measured usage window, then hand off with no attempt spent, and tell the owner a credential needs
        renewing (D-010's first category) - once per engine per login window, whichever branch below is taken.
        A review just closes: pending()/dispatch_reviews() redispatch it next tick and lane_for() resolves the
        fallback itself (or returns None, token-free, exactly as a capacity-deferred review already does with
        none). A delivery with a configured fallback reroutes to it with the fallback's own model/effort and the
        same-vendor flag; a diagnosis with one reroutes keeping its own kind, prompt and diag_* extras
        (reroute()'s `kind` parameter); with none, either waits on the same lane through the ordinary
        continuation path - the same one an ordinary capacity deferral already uses.

        An adapter with no `engine` (REGISTRY is a plugin surface; the two shipped CLI adapters both have one)
        is treated the same as 'no fallback configured' rather than asked about through `lane_for(primary=...)`:
        that call's own capacity check is skipped whenever `engine` is falsy (review.py), so it would simply
        hand the adapter_name straight back - rerouting the job right back to the lane that just refused it,
        with nothing marked unavailable and no deferral."""
        layout = self.layout
        task_id = rec["task_id"]
        engine = getattr(REGISTRY.get(rec["adapter"]), "engine", None)
        until = capacity.mark_login_failed(layout, engine, result.error) if engine else time.time() + 900
        spec = rec.get("spec") or {}
        role = rec.get("review_role") or rec.get("diag_role") or spec.get("role") or tasks_mod.load(layout, task_id).owner_role
        tasks_mod.log(layout, "lane_login_required", "loop", task=task_id, attempt=rec["attempt_id"],
                      adapter=rec["adapter"], role=role, error=result.error, until=until)
        self._notify_login_required(layout, engine, until, str(result.error))
        if kind == "review":
            jobs_mod.set_phase(layout, rec["attempt_id"], "DONE", result="DEFERRED", finished=now_iso(),
                               error=result.error, login_required=True)
            return
        if not engine:
            return self.requeue(rec, Result(status="DEFERRED", error=result.error, reset_epoch=until,
                                            raw={"measured_window": True}), "login required")
        try:
            lane, overrides, same_vendor = review_mod.lane_for(layout, role, primary=rec["adapter"])
        except CapacityDeferred as exc:
            return self.requeue(rec, Result(status="DEFERRED", error=result.error, reset_epoch=exc.reset_epoch,
                                            raw={"measured_window": True}), "login required")
        # Reached only via lane_for()'s own fallback branch: mark_login_failed() just made `require()` raise for
        # this engine, so a normal return here (not the `except CapacityDeferred` above) can only be the fallback
        # tuple - `same_vendor` (True or False, the flag's actual value) is therefore always this reroute's own,
        # matching dispatch_delivery()/review.dispatch() (never absent on a fallback dispatch; release review
        # 2.2.0 F2).
        extra = {"same_vendor": same_vendor}
        self.reroute(rec, lane, f"login required ({result.error})", job_overrides=overrides, extra=extra, kind=kind)

    def ingest(self, rec: dict, result: Result) -> None:
        """Turn an adapter Result into task records, then advance(). A delivery job gets a delivery (digest over
        artifacts) and a verification record (supervisor.verify); a review job gets a review record (supervisor.review).
        Either kind of substantive failure hands off to supervisor.escalation for the retry/issue decision."""
        task_id = rec["task_id"]
        kind = (rec.get("spec") or {}).get("kind", "delivery")
        # docs/plan/LONG_WORK.md section 5.3's "dying" row: `settle()` already wrote this prefix onto `result.error`
        # once `collect()` found the process gone, so this is the one place `ingest()` can tell a claim's holder
        # died from an ordinary finish, without a second liveness check of its own.
        release_reason = "dead" if isinstance(result.error, str) and result.error.startswith("lost:") else None
        if kind == "conversation":
            # No Task backs a thread and no retry chain applies (supervisor.conversations.ingest handles COMPLETE/
            # DEFERRED/FAILED itself, straight into the thread file) — bypass the delivery/review machinery below.
            conversations_mod.ingest(self.layout, rec, result)
            jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso())
            tasks_mod.log(self.layout, "job_done", "loop", task=task_id, attempt=rec["attempt_id"], result=result.status, error=result.error)
            return
        try:
            terminal = tasks_mod.load(self.layout, task_id).status in TERMINAL
        except tasks_mod.TaskError:
            terminal = False
        if terminal:
            # R-04: the task was cancelled (or owner-force-completed) after this job was dispatched - tasks.set_status
            # already tried to retire it; reaching a result here means the race window between that write and this
            # one. Record the result on the job alone: no delivery, verification, review or diagnosis for a task
            # that is already done.
            jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(),
                               error=result.error, release_reason=release_reason)
            tasks_mod.log(self.layout, "job_done", "loop", task=task_id, attempt=rec["attempt_id"], result=result.status,
                          error=result.error, terminal_task=True)
            return
        job_outcome = commands_mod.outcome_for_job(self.layout, rec["attempt_id"])
        if job_outcome == "pending":
            # The job itself finished, but a command it queued (SUPERVISOR_ATTEMPT at submit time) has not been
            # applied yet: declaring this result done now would complete the task before its own queued work is
            # known to have worked (R-19). Leave the record open; collect() is idempotent and tries again next turn.
            return
        if isinstance(job_outcome, dict):
            # One of the job's own queued commands failed after the job itself returned; nothing else saw that
            # (the owner item an interactive-less failure needs is opened in commands.apply_all). The job's result
            # fails with the command's own error instead of whatever the job otherwise reported.
            tasks_mod.log(self.layout, "job_command_failed", "loop", task=task_id, attempt=rec["attempt_id"],
                          command=job_outcome.get("kind"), error=job_outcome.get("error"))
            result = Result(status="FAILED", error=f"{job_outcome.get('kind')} failed: {job_outcome.get('error')}")
        if kind != diagnosis_mod.KIND:
            try:
                self._ensure_in_progress(task_id, f"ingest {rec['attempt_id']}", kind=kind)
            except tasks_mod.TaskError:
                # F1: the task file turned unreadable while this job ran (e.g. the job itself wrote a broken
                # document before returning). Close the record with the result that did arrive, the same shape
                # the terminal-task branch above already uses, rather than raising past once().
                jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(),
                                   error=result.error, release_reason=release_reason)
                tasks_mod.log(self.layout, "job_done", "loop", task=task_id, attempt=rec["attempt_id"], result=result.status,
                              error=result.error, task_unreadable=True)
                return
        if result.status == "FAILED" and str(result.error or "").startswith("login_required:"):
            return self._lane_login_failed(rec, result, kind)
        run_dir = (rec.get("spec") or {}).get("run_dir")
        if result.status == "DEFERRED":
            return self.requeue(rec, result, "capacity limit")
        timed_out = (result.raw or {}).get("timeout") if result.status == "FAILED" else None
        # docs/plan/LONG_WORK.md section 4.4: a `process`-lane hard-cap stop on a declared-resumable job
        # (`adapters.process._time_out`'s own `raw["resumable"]`) is a continuation too, never a spent attempt --
        # an idle stop on that lane stays a failure, so only `timed_out == "cap"` ever carries the flag at all.
        resumable_cap = (timed_out == "cap" and bool((result.raw or {}).get("resumable"))
                        and rec.get("adapter") not in gate.MODEL_LANES)
        if timed_out and (rec.get("adapter") in gate.MODEL_LANES or resumable_cap) and kind != self.VERIFY_KIND:
            # 1.7.0: a session stopped by its time limit (supervisor.timeouts) was busy or stalled, not wrong — a
            # fresh session continues from its HANDOFF.md instead of spending a revision.
            return self.requeue(rec, result, "time limit (hard cap)" if timed_out == "cap" else "time limit (idle)")
        if result.status in ("PARTIAL", "BLOCKED") and run_dir and handoff.incomplete(Path(run_dir)):
            return self.requeue(rec, result, "time limit handoff" if _handoff_reason(run_dir) == "time limit" else "capacity handoff")
        if kind == "review":
            review_mod.ingest(self, rec, result)
        elif kind == diagnosis_mod.KIND:
            if diagnosis_mod.ingest(self, rec, result) is None:
                return      # F7: diagnosis.ingest() already closed the record itself (task file unreadable)
        elif kind == self.VERIFY_KIND:
            self.ingest_verify(rec, result)
        else:
            task = tasks_mod.load(self.layout, task_id)
            # The contract is the floor: every declared artifact is expected whatever the worker reports;
            # a worker may add deliverables, never narrow the list.
            patterns = list(dict.fromkeys(list(task.artifacts) + list(result.artifacts or [])))
            files, missing = self._expand_artifacts(patterns)
            rendered = RENDERED_PROJECTIONS | verify_mod.excluded_files(self.layout)      # built-in and plugin views
            volatile = [f for f in files if tasks_mod.volatile_state(f) or f in rendered]
            if volatile:                                           # the store/projections change on every tick: never an
                files = [f for f in files if f not in volatile]    # artifact, never a frozen review input
                tasks_mod.log(self.layout, "artifacts_dropped", "loop", task=task_id, dropped=volatile,
                              reason="state/ and rendered projections are not deliverables")
            in_run_dir = [f for f in files if f == "runs" or f.startswith("runs/")]
            if in_run_dir:                                          # G4, 2.4.0: evidence (handoff, logs), never a
                files = [f for f in files if f not in in_run_dir]  # deliverable — admitting it would make ownership
                tasks_mod.log(self.layout, "artifacts_dropped", "loop", task=task_id, dropped=in_run_dir,
                              reason="run-directory files are evidence, not deliverables; cite them in notes")
            try:
                dg = digest_of(manifest([self.layout.root / a for a in files], root=self.layout.root)) if files else digest_of({"attempt": rec["attempt_id"]})
            except ValueError as exc:
                dg, result = digest_of({"attempt": rec["attempt_id"]}), Result(status="FAILED", error=f"manifest: {exc}")
            findings_response = [r for r in (result.raw or {}).get("findings_response") or [] if isinstance(r, dict)]
            tasks_mod.record_delivery(self.layout, task_id, dg, actor="loop", result=result.notes or None,
                                      findings_response=findings_response or None, paths=files)
            if result.status == "COMPLETE" and not missing:
                # a model lane must show its work: the brief asks for the commands it ran
                # (packets.result_contract_delivery(layout, task)), and a task that declares tests is not verified
                # without them. Deterministic adapters (python/process/...) declare no tests and are judged on
                # their artifacts.
                model_lane = rec["adapter"] in gate.MODEL_LANES
                tracked = verify_mod.as_job(self.layout) and "process" in REGISTRY
                outcome = verify_mod.verify(self.layout, task, files, (result.raw or {}).get("tests") or [],
                                            self._before_manifest(rec),
                                            require_tests=task.requires_commands(model_lane),
                                            ignore=self._concurrent_owned(rec),
                                            checks=[c.model_dump() for c in task.checks], run_commands=not tracked)
                if outcome.ok and outcome.report.get("pending_commands"):
                    # the file checks passed now; the commands run in a tracked process job so the loop stays
                    # responsive and PAUSE/DRAIN/STOP apply while tests run. Verification is recorded when it returns.
                    self.dispatch_verify(task, rec, dg, outcome.report["pending_commands"], outcome.report.get("fixed") or [])
                    jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso())
                    tasks_mod.log(self.layout, "job_done", "loop", task=task_id, attempt=rec["attempt_id"], result=result.status,
                                  error=result.error, verification="pending")
                    return
            else:
                head = result.error or f"status {result.status}" + (f"; missing: {', '.join(missing)}" if missing else "")
                report = worker_report(result, run_dir, lane_adapter=rec["adapter"]) if result.status != "COMPLETE" else ""
                outcome = verify_mod.Outcome(False, head + (f"\n\nWorker report:\n{report}" if report else ""))
            self.record_outcome(task_id, dg, outcome, rec, substantive=result.status == "COMPLETE",
                                worker_status=result.status, worker_raw=result.raw)
        jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(),
                           release_reason=release_reason)
        tasks_mod.log(self.layout, "job_done", "loop", task=task_id, attempt=rec["attempt_id"], result=result.status, error=result.error)

    def record_outcome(self, task_id: str, dg: str, outcome, rec: dict, substantive: bool, event: str | None = None,
                       worker_status: str | None = None, worker_raw: dict | None = None) -> None:
        """Write a verification Outcome for digest `dg` into the task store, advance, and hand EVERY failure to
        escalation (keyed by the delivery attempt, `event`, so a replay never spends a second revision).

        Before 1.6.0 only a substantive failure (the worker claimed COMPLETE, verification disagreed) reached
        escalation; a worker that ended BLOCKED/FAILED/PARTIAL, or crashed, timed out or was lost, left the task
        FAILED with no retry, no diagnosis and no owner item.

        M11(a), first shape (2.5.0): when the rejection is for writes outside owned paths, `outcome.report["residue"]`
        (verify.undeclared_residue) names each one with its before/after entry. Reverted here, once, before the
        verification record is written — not at the retry's dispatch — so a rejected attempt's residue never reaches
        the next attempt's own `before_manifest.json` baseline at all; there is nothing left for it to clean up, and
        so nothing it can be blamed for cleaning up."""
        if not outcome.ok and (getattr(outcome, "report", None) or {}).get("residue"):
            self._revert_residue(task_id, outcome.report["residue"], (rec.get("spec") or {}).get("run_dir"))
        report_path = None
        if getattr(outcome, "report", None) and (rec.get("spec") or {}).get("run_dir"):
            rp = Path(rec["spec"]["run_dir"]) / "verification.json"
            atomic_json(rp, {"ok": outcome.ok, "detail": outcome.detail, **outcome.report})
            report_path = rp.relative_to(self.layout.root).as_posix() if rp.is_relative_to(self.layout.root) else str(rp)
        delivering_attempt = event or rec["attempt_id"]       # the delivery attempt this digest belongs to, whether
        tasks_mod.record_verification(self.layout, task_id, dg, outcome.ok, report=report_path, detail=outcome.detail,
                                      actor="loop")           # `rec` is that job itself or its separate verify job
        tasks_mod.advance(self.layout, task_id, actor="advance", attempt_id=delivering_attempt)
        if outcome.ok:
            return
        lane = rec.get("adapter", "")
        if substantive:
            # the agent believed it was done and deterministic verification disagreed; R-14's `reason` tells
            # a verifier-condition failure (interpreter, cwd, allow-list, basetemp, a timed-out budget) apart
            # from one on the work itself, so it charges its own counter instead of the review budget
            escalation_mod.record_failure(self, task_id, "verification", outcome.detail, event=delivering_attempt,
                                          reason=verify_mod.failure_reason(outcome))
        elif lane == "human":
            # the owner answered REJECT / MORE_LIKE: that is the owner's decision, never auto-retried
            return
        elif worker_status == "BLOCKED":
            # the worker says it cannot proceed: no blind retry, straight to the exception route (diagnosis → owner)
            escalation_mod.record_failure(self, task_id, "worker_blocked", outcome.detail, event=delivering_attempt,
                                          retry=False)
        elif lane in self.IMMEDIATE_RETRY_LANES:
            # an agent run that ended FAILED/PARTIAL, crashed, timed out or was lost: retried within the revision
            # budget, then the exception route. (Capacity limits never get here — DEFERRED and handoff-incomplete runs
            # are continuations, see requeue().)
            escalation_mod.record_failure(self, task_id, "worker_failed", outcome.detail, event=delivering_attempt)
        else:
            # A deterministic lane (process/python/plugin adapters) that FAILED is left to sweep_stalled(): its
            # grace period is the back-off. R-12: classified now, while the worker's own report is still in
            # hand, so the sweep's later retry never repeats a run that cannot pass twice — a timeout, a lost
            # process (no exit code ever came back) or an exit code the job's own `retry_on` names is transient
            # and gets the one retry as before; any other reported exit goes straight to the exception route.
            escalation_mod.note_process_outcome(self.layout, task_id, delivering_attempt,
                                                retryable=self._process_failure_retryable(rec, worker_raw))

    @staticmethod
    def _process_failure_retryable(rec: dict, raw: dict | None) -> bool:
        """Whether a deterministic lane's FAILED result (R-12) is worth the stall sweep's one automatic retry: a
        timeout, an exit the adapter never captured (lost — a crash, an adopted handle after a restart, the loop's
        own termination), or an exit code the job's own `retry_on` names. Any other captured exit is assumed to
        repeat itself."""
        raw = raw or {}
        if raw.get("timeout"):
            return True
        exit_code = raw.get("exit_code")
        if exit_code is None:
            return True
        return exit_code in ((rec.get("spec") or {}).get("retry_on") or [])

    VERIFY_KIND = "verify"
    IMMEDIATE_RETRY_LANES = gate.MODEL_LANES

    def dispatch_verify(self, task: Task, delivery_rec: dict, dg: str, commands: list[dict], fixed: list[str]) -> dict:
        """Queue the tracked verify job for one delivery digest: a `process` job running supervisor.verify_job
        on an input file written next to the delivery's run. It owns nothing and passes the gate under DRAIN too, so a
        drain finishes verifying what was delivered before stopping."""
        run_dir = Path((delivery_rec.get("spec") or {}).get("run_dir") or (self.layout.runs / task.id / delivery_rec["attempt_id"]))
        run_dir.mkdir(parents=True, exist_ok=True)
        input_path = run_dir / "verify_input.json"
        atomic_json(input_path, {"root": str(self.layout.root), "task_id": task.id, "digest": dg, "commands": commands,
                                 "fixed": fixed, "delivery_attempt": delivery_rec["attempt_id"],
                                 "roots": list(task.test_roots)})
        # The child runs with the task root as cwd, which need not contain the supervisor package: import it by path.
        pkg_parent = str(Path(verify_mod.__file__).resolve().parents[1])
        code = (f"import sys; sys.path.insert(0, {pkg_parent!r}); from supervisor.verify_job import main; "
                f"sys.exit(main(['verify_job', {str(input_path)!r}]))")
        doc = task.model_dump(mode="json")
        doc["job"] = {"adapter": "process", "argv": [sys.executable, "-c", code],
                      "timeout_s": verify_mod.limits(self.layout)["budget_s"] + 120, "result_kind": "verify"}
        view = Task.model_validate(doc)
        prior_verify = any(jobs_mod.kind_of(r) == "verify" and r.get("verify_delivery") == delivery_rec["attempt_id"]
                          for r in jobs_mod.for_task(self.layout, task.id))
        rec = self.dispatch(view, "process", note=f"verify delivery {delivery_rec['attempt_id']}", kind=self.VERIFY_KIND,
                            extra={"verify_digest": dg, "verify_delivery": delivery_rec["attempt_id"], "verify_commands": len(commands)},
                            cause="reverify" if prior_verify else None, attempt=delivery_rec.get("attempt"),
                            parent=delivery_rec["attempt_id"])
        tasks_mod.log(self.layout, "verify_dispatched", "loop", task=task.id, attempt=rec.get("attempt_id"), digest=dg, commands=len(commands))
        return rec

    def ingest_verify(self, rec: dict, result: Result) -> None:
        """A finished verify job: record the verification for its digest (unless a newer delivery superseded it)."""
        task_id, dg = rec["task_id"], rec.get("verify_digest")
        task = tasks_mod.load(self.layout, task_id)
        if task.delivery.digest != dg:
            tasks_mod.log(self.layout, "verify_stale", "loop", task=task_id, attempt=rec["attempt_id"], digest=dg)
            return
        payload = (result.raw or {}).get("verification") if result.status == "COMPLETE" and isinstance(result.raw, dict) else None
        if payload is None:
            outcome = verify_mod.Outcome(False, f"verify job {result.status}: {result.error or 'no verification payload'}")
        else:
            outcome = verify_mod.Outcome(bool(payload.get("ok")), str(payload.get("detail") or ""), dict(payload.get("report") or {}))
        self.record_outcome(task_id, dg, outcome, rec, substantive=True, event=rec.get("verify_delivery"))

    def supersede_changed_attempts(self) -> list[str]:
        """A task's job or dependencies changed (`task.patch`, `task.set_job`) while one of its delivery attempts,
        dispatched under the OLD contract, was still open: close it as SUPERSEDED before it does more work on
        inputs that no longer apply — a queued one simply never launches, a running one is terminated first — and
        spend no retry budget on it (R-12). Called before `reconcile()` so a now-stale PREPARED record is never
        launched and a RUNNING one is stopped before this turn collects it. Every other open record (no
        `job_fingerprint`: a review, a verify, a diagnosis, a conversation, or a delivery from before this fix)
        is left alone.

        Closing the task's only open attempt must never park it IN_PROGRESS with nothing routing it — neither
        `tasks.eligible()` (NOT_STARTED/READY only) nor `sweep_stalled()` (FAILED/BLOCKED only) ever revisits an
        IN_PROGRESS task on its own. So, exactly when no other open record is left for the task, this releases
        it the same way a diagnosis `defer` already does (`escalation._release`): IN_PROGRESS -> BLOCKED ->
        READY, the only legal route (`models.task.ALLOWED` has no direct IN_PROGRESS -> READY edge). `eligible()`
        then holds the task behind whatever dependency just changed, same as any other READY task, or — if the
        change was to `job` alone — dispatches it again next turn with the new contract. Returns the attempt ids
        closed."""
        acted = []
        for rec in jobs_mod.open_records(self.layout):
            fp = rec.get("job_fingerprint")
            if fp is None:
                continue
            try:
                task = tasks_mod.load(self.layout, rec["task_id"])
            except tasks_mod.TaskError:
                continue
            if _job_fingerprint(task) == fp:
                continue
            if rec["phase"] in ("SUBMITTED", "RUNNING") and rec.get("handle"):
                ad = REGISTRY.get(rec["adapter"])
                if ad is not None:
                    try:
                        ad.terminate(rec["handle"])
                    except Exception:                       # noqa: BLE001 - the record closes regardless
                        pass
            jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result="SUPERSEDED", finished=now_iso(),
                               error="superseded: the task's job or dependencies changed while this attempt was open")
            tasks_mod.log(self.layout, "attempt_superseded", "loop", task=rec["task_id"], attempt=rec["attempt_id"])
            note = f"{rec['attempt_id']}: superseded — its job or dependencies changed while it was open"
            tasks_mod.add_note(self.layout, rec["task_id"], note, actor="loop")
            acted.append(rec["attempt_id"])
            still_open = [r for r in jobs_mod.for_task(self.layout, rec["task_id"]) if r["phase"] in jobs_mod.OPEN_PHASES]
            if still_open or task.status != TaskStatus.IN_PROGRESS:
                continue
            tasks_mod.set_status(self.layout, rec["task_id"], TaskStatus.BLOCKED, actor="loop", note=note)
            tasks_mod.set_status(self.layout, rec["task_id"], TaskStatus.READY, actor="loop", note=note)
        return acted

    def reconcile(self) -> None:
        """Before any dispatch: settle every non-DONE job record (crash-safe)."""
        for rec in jobs_mod.open_records(self.layout):
            if rec["phase"] == "PREPARED" and rec["adapter"] in REGISTRY:
                self.launch(rec)                                      # same attempt id: never a duplicate
        self.settle()
        self.repair_continuations()
        self._renew_gpu_lease_if_active()
        self._release_gpu_lease_if_idle()

    def _active_lease_names(self) -> set[str]:
        """Every machine-wide lease name one of this project's own open records currently takes
        (`leases.lease_name_for`, docs/plan/LONG_WORK.md section 5.4), from exactly the two fields dispatch wrote
        onto each record (`gpu`, `claim`) — never a second derivation of the same two facts `gate.lease_blocked`'s
        own peek already makes from `holds_resource`/`needs.claim_for`, so the peek and this sweep can never
        disagree about which lease a job takes."""
        return {name for r in jobs_mod.open_records(self.layout) if r["phase"] in gate.ACTIVE_PHASES
               for name in [leases.lease_name_for(self.layout, bool(r.get("gpu")), r.get("claim"))] if name}

    def _renew_gpu_lease_if_active(self) -> None:
        """Refresh this project's own entry in every machine-wide lease (R4) one of its own open records currently
        takes, each `reconcile()`, for as long as that record still holds it — so a job that outlives
        `stale_after_s` (a render, a local-model benchmark, a long claim) is not pruned as abandoned while it is
        still running. The same set `_release_gpu_lease_if_idle()` computes right after decides whether to renew
        or to let go for each name, so the two can never disagree about whether this project is still active. A
        record that has left RUNNING (settled into SUBMITTED only transiently, or gone to DONE/FAILED) drops out
        of `open_records()` and is never renewed, and — for the same reason — never re-taken below.

        F19: the entry `launch()` wrote names the pid of the process that called `submit()`; a loop that
        restarts while that job is still RUNNING (`supervisor stop`/start, a crash, a runtime move) is a new
        process, so `renew()` finds no `(holder, pid)` match on its first tick and reports False — not because
        the job ended, but because this entry was never this process's to begin with. `leases._reclaim()` is
        the re-take: re-entrant within the project, so it succeeds at once whenever the cards are still this
        project's (no other project's live entry contests it) and re-establishes exactly the entry renew()
        could not refresh — unlike the ordinary `acquire()` a brand-new launch uses, it is never refused merely
        because another project's hand-over request stands, since that request is about this project yielding
        the job it is already holding, not a free slot up for grabs. It is a private function of `leases`, not
        a method of the plugin-facing `ResourceLease` (the same reasoning as `leases._record_waiting_from_peek`):
        a plugin must never be handed an operation that silently skips the fairness window `acquire()`
        documents. A refusal means another project's own entry is already live there — the job already lost
        the cards to someone else — and is logged with whatever holder the peek finds rather than passed over
        in silence; the job record itself is not touched either way, so a refusal never wedges it."""
        for name in self._active_lease_names():
            lease = leases.resource_lease(self.layout, name)
            if lease.renew():
                continue
            if not leases._reclaim(self.layout, name):
                tasks_mod.log(self.layout, "gpu_lease_retake_refused", "loop", lease=name,
                              holder=leases.holder_label(self.layout, name))

    def _release_gpu_lease_if_idle(self) -> None:
        """Let go of every machine-wide lease name none of this project's own open records takes any more (the
        `gpu` slot's own literal lease, and any claim's own configured one): a quiet project never keeps another
        one waiting longer than its own last job's work. Every declared resource name is tried (`release()` is a
        no-op when unconfigured or not held, the same bargain the single `"gpu"` release already made), not only
        the ones a record of this turn happens to carry, so a name a crash dropped from `open_records()` without
        ever reaching `collect()` is still released."""
        from . import needs as needs_mod
        active = self._active_lease_names()
        for name in {"gpu"} | set(needs_mod.resources_declared(self.layout)):
            if name not in active:
                leases.resource_lease(self.layout, name).release()

    def dispatch_reviews(self) -> None:
        """Review dispatch: one review job per (WAITING_FOR_REVIEW task, missing reviewer role) under
        the same agent-job concurrency limit as primary dispatch — reviews and deliveries share one budget."""
        lim = scheduler.limits(self.layout)
        agent_busy = sum(1 for r in jobs_mod.open_records(self.layout) if r.get("adapter") in gate.MODEL_LANES)
        for task, role in review_mod.pending(self.layout):
            if review_mod.salvage(self, task, role):                 # a verdict already on disk costs no new review
                continue
            if agent_busy >= lim["max_agent_jobs"]:
                break
            if review_mod.dispatch(self, task, role) is not None:
                agent_busy += 1

    def dispatch_conversations(self) -> None:
        """Conversation turns: dispatched like any other claude_cli job, sharing the same
        agent-job budget and the same measured capacity reserve (`capacity.require()` inside the adapter's
        `submit()`) — a chat turn never bypasses the 10 % handoff reserve other dispatch goes through."""
        lim = scheduler.limits(self.layout)
        agent_busy = sum(1 for r in jobs_mod.open_records(self.layout) if r.get("adapter") in gate.MODEL_LANES)
        for thread_id, idx in conversations_mod.pending_turns(self.layout):
            if agent_busy >= lim["max_agent_jobs"]:
                break
            if conversations_mod.dispatch(self, thread_id, idx) is not None:
                agent_busy += 1

    def repair_continuations(self) -> None:
        """A requeue that crashed after queuing nothing (pre-fix records or a torn write): the last record of a task is
        DONE with result DEFERRED but no continuation exists and the task is still IN_PROGRESS → queue it now.

        A reroute (result REROUTED) not yet marked `continued`: when an attempt names it (`rerouted_from`), or any
        later attempt exists, the work already went on and the record is only marked; when it is the task's newest
        attempt, a crash came before its successor was written and it is queued on `rerouted_to` now. Attempts are
        ordered by number (jobs.for_task), so attempt 10 is newer than attempt 9.

        F3: the cap branches below (here and in requeue()) both mark an attempt DONE/continued/capped BEFORE calling
        escalation.record_failure() for it, so a crash between those two writes leaves a capped attempt with
        `continued` already set — invisible to the torn-write repair above, which skips anything already
        `continued` — while the task is still IN_PROGRESS with no issue or diagnosis to show for it. Nothing else
        ever revisits such a record (it is not open, not a reroute, and the stall sweep excludes IN_PROGRESS
        tasks), so _repair_capped_escalation() finishes it here: the task being stuck IN_PROGRESS behind its own
        last, capped record is exactly the symptom of that missing call, whichever cap branch left it, and
        record_failure() is itself replay-safe, so finishing it again when it already succeeded is a no-op."""
        # One pass over the job directory, grouped by task (R-09): this used to call jobs_mod.for_task() once per
        # task seen, each call re-globbing the whole directory — 352 globs of 1,380 files on a store this task's
        # own evidence sized, for work this one grouped scan already did.
        for task_id, recs in jobs_mod.all_grouped(self.layout).items():
            last = recs[-1]
            if self._repair_reroutes(task_id, recs):
                continue
            if last["phase"] == "DONE" and last.get("capped") and last.get("continued"):
                self._repair_capped_escalation(task_id, last, recs)
                continue
            run_dir = (last.get("spec") or {}).get("run_dir")
            needs = last.get("result") == "DEFERRED" or (last.get("result") in ("PARTIAL", "BLOCKED") and run_dir
                                                           and handoff.incomplete(Path(run_dir)))
            if last["phase"] == "DONE" and needs and not last.get("continued") and last["adapter"] in REGISTRY:
                try:
                    task = tasks_mod.load(self.layout, task_id)
                except Exception:
                    continue
                if task.status != TaskStatus.IN_PROGRESS:
                    continue
                prior = self._continuations(recs, last.get("attempt"))
                if len(prior) >= self.MAX_CONTINUATIONS:
                    # R4: recovery enforces the same cap as requeue() — a torn write exactly at the cap boundary is
                    # still a capped attempt, never a fresh continuation repair may dispatch past the budget.
                    jobs_mod.set_phase(self.layout, last["attempt_id"], "DONE", continued=True, capped=True)
                    escalation_mod.record_failure(self, task_id, "continuations_exhausted",
                                                  f"{last['attempt_id']}: capacity limit after {len(prior)} continuations (recovered)",
                                                  event=f"{last['attempt_id']}:continuations", retry=False)
                    continue
                overrides = review_mod.fallback_overrides(last)
                if overrides:
                    doc = task.model_dump(mode="json")
                    doc["job"] = {**scheduler.job_spec(task), **overrides, "adapter": last["adapter"]}
                    task = Task.model_validate(doc)
                extra = {"continuation_grant": escalation_mod.current_grant(self.layout, task_id)}
                note = "continuation after capacity limit"
                lane_reason = f"role {task.owner_role}'s own attempt, continued on its lane ({last['adapter']})"
                if "same_vendor" in last:
                    extra["same_vendor"] = last["same_vendor"]
                    if last["same_vendor"]:
                        note += " (same-vendor fallback)"
                        lane_reason += "; same-vendor fallback, carried over from the attempt being continued"
                self.dispatch(task, last["adapter"], deferred_until=time.time(), note=note, extra=extra, reason=lane_reason,
                             cause="continuation", attempt=last.get("attempt"), parent=last["attempt_id"])
                jobs_mod.set_phase(self.layout, last["attempt_id"], "DONE", continued=True)

    def _repair_capped_escalation(self, task_id: str, last: dict, recs: list[dict]) -> None:
        """F3/F6: finish a capped attempt (requeue()'s or repair_continuations()'s own cap branch) whose
        escalation.record_failure() call never completed. That call's own side effects — the issue, the
        IN_PROGRESS->BLOCKED transition, and the diagnosis or owner routing that follows — do not all land in one
        write (escalation.py's `_apply`), so leaving IN_PROGRESS is not proof the event is finished: a crash
        between the issue write and the status write leaves the task IN_PROGRESS with the issue already open, and
        a crash between the status write and the routing leaves it BLOCKED with nothing routing it (round-3 F6).
        `escalation.event_outcome` reads the event's own durable record instead of the task's current status, so
        a BLOCKED task whose routing already landed is told apart from one still owed its routing: only the
        latter — pending, and still sitting in the one status (IN_PROGRESS) or two statuses (IN_PROGRESS,
        BLOCKED) this event itself can leave it in — is replayed here. A deliberate later cancellation is told
        apart the same way CANCELLED always is: it is outside `allowed` below. A deliberate later Hold is a
        separate, durable record on the task itself (`held`, round-4 F9) - `event_outcome`'s pending/routed
        carries no such information, and BLOCKED is this event's own intermediate state as much as it is a stop
        for any other reason, so `held` is checked directly rather than inferred from the event's outcome or the
        task's status. record_failure() replays its own durable decision instead of repeating it, so calling it
        again — whether this is the first real attempt or a replay of one a crash interrupted — never opens a
        second issue or dispatches a second diagnosis."""
        try:
            task = tasks_mod.load(self.layout, task_id)
        except Exception:
            return
        event = f"{last['attempt_id']}:continuations"
        outcome = escalation_mod.event_outcome(self.layout, task_id, event)
        if outcome == "routed":
            return                                        # the cap's own routing already landed
        if task.held:
            return                                        # an owner Hold is durably on the task - not ours to replay over
        allowed = (TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED) if outcome == "pending" else (TaskStatus.IN_PROGRESS,)
        if task.status not in allowed:
            return                                        # a cancellation (or anything else) moved it since - not ours
        prior = self._continuations(recs, last.get("attempt"))
        escalation_mod.record_failure(self, task_id, "continuations_exhausted",
                                      f"{last['attempt_id']}: capacity limit after {len(prior)} continuations (recovered)",
                                      event=event, retry=False)

    def _repair_reroutes(self, task_id: str, recs: list[dict]) -> bool:
        """repair_continuations() for REROUTED records; True when the task's newest record was one (handled here).

        A diagnosis reroute's successor is rebuilt the same way `reroute()`'s own live path does
        (`_reroute_rebuild`, sharing one mechanism between the two callers): it is never gated on IN_PROGRESS
        (diagnosis.rebuild tolerates a BLOCKED task on purpose, the common status for one) and it is always
        marked `continued` once rebuilt — the diagnosis-shaped bug this guards against had two faces: a task
        that WAS IN_PROGRESS got redispatched through the plain delivery branch below (losing the diagnosis's
        own kind and every `diag_*` extra), and a task that was BLOCKED, as a diagnosis more commonly leaves it,
        hit the delivery branch's `continue` before ever reaching the `continued` write and was revisited,
        unrepaired, on every later tick. A delivery reroute keeps its own, narrower rule unchanged: it is only
        ever redispatched while the task is still IN_PROGRESS.

        Either shape dispatches the payload the refused record itself carries - the fallback's own model and
        effort (`rerouted_job`) and the same-vendor flag (`rerouted_extra`) - not a plain attempt on the
        fallback lane with the role profile's model: that one fails and spends a revision slot, exactly the
        "no attempt spent" guarantee this path exists to preserve."""
        last = recs[-1]
        for r in recs:
            if r["phase"] != "DONE" or r.get("result") != "REROUTED" or r.get("continued"):
                continue
            succeeded = r is not last or any(o.get("rerouted_from") == r["attempt_id"] for o in recs)
            lane = r.get("rerouted_to")
            if not succeeded:
                if lane not in REGISTRY:
                    continue
                kind = (r.get("spec") or {}).get("kind", "delivery")
                if kind != diagnosis_mod.KIND:
                    try:
                        status = tasks_mod.load(self.layout, task_id).status
                    except Exception:
                        continue
                    if status != TaskStatus.IN_PROGRESS:
                        continue
                task, kind_extra = self._reroute_rebuild(r, kind)
                if task is not None:
                    self.dispatch(task, lane, note=f"rerouted from {r['adapter']}", kind=kind,
                                  extra={"rerouted_from": r["attempt_id"], **kind_extra},
                                  reason=f"fallback: {r['adapter']} unavailable ({r.get('error')}); rerouted to {lane}",
                                  cause="reroute", attempt=r.get("attempt"), parent=r["attempt_id"])
            jobs_mod.set_phase(self.layout, r["attempt_id"], "DONE", continued=True)
        return last.get("result") == "REROUTED"

    def request_owner_decisions(self) -> None:
        """A task BLOCKED with "owner decision needed" (review budget spent, continuations exhausted, ...) gets exactly
        one `decision` inbox item per block (keyed by the history entry's timestamp), so the owner actually hears
        about it; inbox.apply_decision acts on the answer. Before 1.4.0 such a task only had an issue and a note —
        nothing reached the inbox. Also re-applies an answered item whose effect never landed."""
        items = [i for i in inbox_mod.all_items(self.layout) if inbox_mod.is_decision(i)]
        for i in items:
            if i.get("status") == "answered" and not i.get("applied"):
                inbox_mod.apply_decision(self.layout, i, by="loop")
        for task in tasks_mod.load_all(self.layout).values():
            if task.status != TaskStatus.BLOCKED:
                continue
            history = task.history or []
            last = next((h for h in reversed(history) if h.get("to") == TaskStatus.BLOCKED.value), None)
            note = str((last or {}).get("note") or "")
            if not last or inbox_mod.OWNER_DECISION_MARK not in note:
                continue
            if any(i.get("task_id") == task.id and i.get("blocked_at") == last.get("at") for i in items):
                continue
            # an invalidated review means the delivered files changed after delivery: re-reviewing the same digest can
            # only be invalidated again, so REQUEUE re-runs the task to record a fresh delivery instead
            if note.startswith("review of digest") and "invalidated" not in note:
                resume = "review"
            else:
                resume = "delivery" if "continuations" in note else "task"
            action = {"review": "run the review again (its retry budget is reset)",
                      "delivery": "resume the work with a fresh continuation budget",
                      "task": "re-run the task so it records a fresh delivery" if "invalidated" in note
                              else "put the task back in the queue"}[resume]
            # the options lead: a long block note must never push them out of the summary (fit() keeps the rest)
            summary, detail = inbox_mod.fit(
                f"REQUEUE = {action}; CANCEL = cancel the task; DEFER = leave it blocked. "
                "Text you add is passed on as guidance. "
                f"{task.id} is blocked: {note.replace('; ' + inbox_mod.OWNER_DECISION_MARK, '')}.")
            try:
                inbox_mod.create(self.layout, for_role="owner", subject=f"Decision needed: {task.id} — {task.title}",
                                 summary=summary, detail=detail, kind=inbox_mod.DECISION_KIND,
                                 allowed_responses=list(inbox_mod.DECISION_RESPONSES), free_text=True,
                                 from_="loop", task_id=task.id,
                                 extra={"resume": resume, "blocked_at": last.get("at")})
            except inbox_mod.InboxFull:
                return                                            # try again next iteration

    # ------------------------------------------------------------ nothing stops silently

    def resume_plans(self) -> list[str]:
        """RECOVERY_PLAN.md §2.6 rule 5: every plan still `applying` (a crash, or an operation that stalled and
        has not yet exhausted `plan.max_resumes`) is continued from its own journal. A `stuck` plan is left alone
        here — only the owner's apply_plan/cancel_plan choice decides what happens to it next."""
        return plans_mod.resume_plans(self.layout)

    def preflight_backfill(self) -> list[str]:
        """RECOVERY_PLAN.md section 3.3's one-time sweep (D-027 decision 9, step 5): the first turns after an
        upgrade preflight every existing `process`/`python` job block that predates the check -- oldest task
        first, a bounded number per turn (`preflight.BACKFILL_BATCH`) -- and record the sweep's own completion
        once nothing is left pending (`preflight.mark_backfill_complete`), so the gate's run-on-absent rule
        (`gate.preflight_reason`) is a bounded migration window rather than a permanent one, and no store stops
        on upgrade day with no `supervisor migrate` step to run. A block that fails gets no retry loop here,
        only one deduped owner item (`preflight.report_backfill_failure`); the block itself is never touched
        again -- this fills an absence, it does not retry a failure. A task already in `TERMINAL` (`COMPLETE`
        has no route back to launching at all; `CANCELLED` is swept the same as any other so it is never
        locked out if it later returns to `NOT_STARTED`) still gets its result stored, just no owner item: one
        nobody can act on now is noise, not a finding. The count this records at completion is the sweep's own
        running total across every turn, not just this one's batch. Returns the task ids this turn wrote a
        preflight for."""
        if preflight_mod.backfill_complete(self.layout):
            return []
        pending = preflight_mod.pending_backfill(tasks_mod.load_all(self.layout))
        if not pending:
            preflight_mod.mark_backfill_complete(self.layout, swept=preflight_mod.backfill_progress(self.layout))
            return []
        batch = pending[:preflight_mod.BACKFILL_BATCH]
        acted = []
        for task in batch:
            pf = tasks_mod.backfill_job_preflight(self.layout, task.id)
            if pf is None:
                continue
            acted.append(task.id)
            if not pf.ok and task.status not in TERMINAL:
                preflight_mod.report_backfill_failure(self.layout, task, pf)
        total = preflight_mod.record_backfill_progress(self.layout, swept_this_turn=len(acted))
        if len(batch) == len(pending):
            preflight_mod.mark_backfill_complete(self.layout, swept=total)
        return acted

    STALL_GRACE_S = 600

    def _stall_grace(self) -> float:
        esc = (scheduler_routing(self.layout).get("escalation") or {})
        try:
            return float(esc.get("stall_grace_s", self.STALL_GRACE_S))
        except (TypeError, ValueError):
            return float(self.STALL_GRACE_S)

    def sweep_stalled(self, now: float | None = None) -> list[str]:
        """Safety net under escalation: a task FAILED or BLOCKED with nothing in flight for it and nothing waiting on
        the owner is a silent stall, whatever left it there (an adapter that vanished, a launch that never got a
        handle, a diagnosis deferred for capacity, a record from before this sweep existed). After a grace period:

          1. no escalation since it stopped → escalation.record_failure("stalled"), keyed by the stop, so it is
             decided once: a FAILED task gets a retry within the budget, a BLOCKED one goes to diagnosis;
          2. escalated since it stopped and still nothing routing it → one owner decision item.

        Deliberate holds are left alone: a stop the owner made themselves (Hold / set-status), an open or snoozed owner
        item for the task, a failure of the task's `human` attempt (the owner's REJECT/MORE_LIKE — linked through
        the attempt, not by comparing timestamps, because the loop collects the answer after it was given). A
        REQUEUE or CANCEL answer moves the task off FAILED/BLOCKED at once (inbox.apply_decision), so it is never
        seen here at all — this sweep only ever loads tasks still FAILED/BLOCKED. A DEFER or free-text answer is
        the opposite: it is recorded as a comment and the task does not move, so it is never treated as a resolved
        stop here, whether or not the fresh decision item that answer tries to open could itself be opened — that
        create() can fail for want of room in the owner's inbox, and this sweep's guarantee must not depend on it
        succeeding (R-06). A diagnosis `defer` is not a hold either: it either releases the task to READY behind
        the dependencies it named or goes to the owner (supervisor.diagnosis), so a task still stopped after one —
        a defer recorded before that rule, parked on prose that nothing acts on — is a stall like any other.
        Returns the task ids acted on."""
        now = time.time() if now is None else now
        grace = self._stall_grace()
        busy = {r["task_id"] for r in jobs_mod.open_records(self.layout)}
        items = inbox_mod.all_items(self.layout)
        acted = []
        for task in tasks_mod.load_all(self.layout).values():
            if task.status not in (TaskStatus.FAILED, TaskStatus.BLOCKED) or task.id in busy:
                continue
            stop = next((h for h in reversed(task.history or []) if h.get("to") == task.status.value), None)
            at = str((stop or {}).get("at") or task.updated or "")
            if not at or now - _epoch(at) < grace:
                continue
            if task.held:
                continue                                          # a deliberate hold (F9/F11/F13's durable identity) by
                                                                    # any actor, not just a guess at the actor's name (M11(b), 2.5.0)
            if str((stop or {}).get("by") or "").startswith("owner"):
                continue                      # a `set-status` stop by the owner leaves no `held`: their call too
            mine = [i for i in items if i.get("task_id") == task.id]
            if any(i.get("status") in ("open", "snoozed") for i in mine):
                continue                                          # waiting on the owner (a snooze defers, it is not a stall)
            if any(max(str((i.get("applied") or {}).get("at") or ""), str(i.get("answered_at") or "")) >= at
                   and (i.get("applied") or {}).get("skipped") != "deferred" for i in mine):
                continue          # a REQUEUE/CANCEL answered this stop; a DEFER/free-text one is a comment, not one
            records = jobs_mod.for_task(self.layout, task.id)
            deliveries = [r for r in records if jobs_mod.kind_of(r) == "delivery"]
            # the newest attempt's newest session: jobs_mod.sort_key (attempt, kind, seq fields only, no
            # id-suffix fallback, §6) is already oldest-first, so the max is the newest.
            latest_delivery = max(deliveries, key=jobs_mod.sort_key) if deliveries else None
            if latest_delivery and latest_delivery.get("adapter") == "human":
                continue                                          # the owner's own answer (REJECT/MORE_LIKE) failed it
            key = f"stall:{at}"
            state = escalation_mod._load(self.layout, task.id)
            last_note = str((stop or {}).get("note") or "")
            run_dir = next((a.run_dir for a in reversed(task.attempts) if a.run_dir), None)
            report = worker_report(Result(status=task.status.value), run_dir,
                                   lane_adapter=latest_delivery.get("adapter") if latest_delivery else None)
            detail = (f"{task.id} has been {task.status.value} since {at} with nothing in flight and no owner item. "
                      f"Last status note: {last_note or '(none)'}" + (f"\n\nLatest run report:\n{report}" if report else ""))
            if key not in state["events"] and escalation_mod.last_decision_at(self.layout, task.id) < at:
                tasks_mod.log(self.layout, "stall_detected", "loop", task=task.id, status=task.status.value, since=at)
                # R-12: a FAILED task's own deterministic failure was already classified (loop.record_outcome) —
                # a timeout, a lost process or a declared retryable exit still gets the one retry here; anything
                # else goes straight to the exception route instead of relaunching a run that cannot pass twice.
                # No delivery record at all (a path that routes nothing - an adapter that vanished, a crash
                # before launch) carries no classification either way, so it keeps today's default: retry.
                retryable = (not latest_delivery) or escalation_mod.process_outcome_retryable(
                    self.layout, task.id, latest_delivery["attempt_id"])
                escalation_mod.record_failure(self, task.id, "stalled", detail, event=key,
                                              retry=task.status == TaskStatus.FAILED and retryable)
                acted.append(task.id)
                continue
            if state.get("stall_owner", {}).get(at):
                continue                                          # already handed to the owner for this stop
            if now - _epoch(escalation_mod.last_decision_at(self.layout, task.id) or at) < grace:
                continue                                          # the escalation is still settling
            out = diagnosis_mod.route_to_owner(self.layout, task, {"diag_role": "supervisor"}, detail, "",
                                               "escalated, but nothing picked it up")
            if out.get("inbox"):
                state = escalation_mod._load(self.layout, task.id)
                state.setdefault("stall_owner", {})[at] = out["inbox"]
                escalation_mod._save(self.layout, task.id, state)
                tasks_mod.log(self.layout, "stall_to_owner", "loop", task=task.id, item=out["inbox"])
                acted.append(task.id)
        return acted

    def commit_state(self, after_checkpoint: bool) -> dict | None:
        """The loop's own records to git (state_commit.py): at once after a checkpoint, else at most every
        `checkpoint.state_interval_minutes`. A git problem is logged and never stops the loop."""
        try:
            return state_commit_mod.maybe(self.layout, after_checkpoint=after_checkpoint)
        except Exception as exc:                            # noqa: BLE001 - git trouble must never kill the loop
            tasks_mod.log(self.layout, "state_commit_error", "state_commit", error=str(exc)[:300])
            return None

    def alert_if_idle(self, now: float | None = None) -> dict | None:
        """The whole pipeline stopped: nothing running or queued, nothing eligible, nothing waiting on the owner, yet
        work remains. One owner item per distinct stuck set (fingerprint), raised after twice the stall grace so the
        per-task sweep gets its chance first. It lists every non-terminal task that is not an owner hold (R-17: the
        pre-1.7.0 version counted only FAILED/BLOCKED/WAITING_FOR_HUMAN, so an IN_PROGRESS or WAITING_FOR_REVIEW task
        with nothing actually running for it never appeared; and a task the owner is deliberately holding is the
        owner's own call, not the pipeline's problem, so it is excluded from both the idle test and the list). A
        paused, draining or stopped loop is excluded the same way (`why_not_runnable.decision_blocked`): the owner
        put it there, so this never fires while it holds, rather than raising with every task's reason wrongly
        computed as if the loop's decision were still RUN.

        The reason named for each task is `why_not_runnable`'s own (RECOVERY_PLAN.md section 4.3, D-027 decision
        11) — a computed sentence from the closed nine-code set, the same one the Overview and the task page read,
        never a quoted note an agent wrote about something else. The item's own buttons are the choices that
        reason admits (`why_not_runnable.choices_for`), never the four legacy keywords: none of ACCEPT/REJECT/
        MODIFY/REDO does anything for "nothing can run"."""
        now = time.time() if now is None else now
        path = self.layout.runtime / "idle_alert.json"
        state = read_json(path) if path.exists() else {}
        tasks = tasks_mod.load_all(self.layout)
        remaining = [t for t in tasks.values() if t.status not in TERMINAL]
        waiting = [t for t in remaining if not t.held]
        # A paused, draining or stopped loop is the owner's own call, exactly as a held task is (R-17's own
        # precedent): never raised as "idle" while it holds, so no task is ever classified by why_not_runnable
        # while gate.check's own decision != RUN refusal would apply identically to every one of them.
        idle = (not jobs_mod.open_records(self.layout) and not tasks_mod.eligible(self.layout, tasks)
                and not inbox_mod.open_items(self.layout, "owner") and waiting
                and why_not_runnable_mod.decision_blocked(self.layout) is None)
        if not idle:
            if state.get("idle_since"):
                atomic_json(path, {k: v for k, v in state.items() if k != "idle_since"})
            return None
        if not state.get("idle_since"):
            state["idle_since"] = now
            atomic_json(path, state)
            return None
        if now - float(state["idle_since"]) < 2 * self._stall_grace():
            return None
        stuck = sorted(waiting, key=lambda t: t.id)
        fingerprint = "|".join(f"{t.id}:{t.status.value}:{t.updated}" for t in stuck) or "deps-or-gates"
        if state.get("fingerprint") == fingerprint:
            return None
        lines, choices = [], []
        for t in stuck[:8]:
            why = why_not_runnable_mod.why_not_runnable(t, tasks, self.layout)
            # Never cut short here: `inbox.fit` bounds the whole item below, so a long reason still reaches the
            # alert byte-identical to the Overview and the task page (a per-line cut here would not).
            lines.append(f"{t.id} {t.status.value}: {why_not_runnable_mod.render_reason(why)}")
            choices += [{**c, "id": f"{t.id}.{c['id']}"} for c in why_not_runnable_mod.choices_for(t, why)]
        held_ct = len(remaining) - len(waiting)
        held_note = f" ({held_ct} held by you, not listed)" if held_ct else ""
        summary, detail = inbox_mod.fit(
            f"Nothing can run: {len(waiting)} task(s) remain{held_note}, none is eligible, nothing is in flight. "
            + " · ".join(lines))
        try:
            item = inbox_mod.create(self.layout, for_role="owner", subject="Pipeline idle — nothing can run",
                                    summary=summary, detail=detail, kind="question", from_="loop",
                                    allowed_responses=[], choices=choices or None,
                                    extra={"idle_fingerprint": fingerprint})
        except inbox_mod.InboxFull:
            return None
        state.update(fingerprint=fingerprint, item=item["id"], raised=now_iso())
        atomic_json(path, state)
        tasks_mod.log(self.layout, "pipeline_idle", "loop", item=item["id"], stuck=[t.id for t in stuck])
        return item

    def request_approvals(self) -> None:
        """Owner approval dispatch: a task that advance() parked in
        WAITING_FOR_HUMAN because its current digest is verified and reviewed but unapproved gets exactly one `approval`
        inbox item per digest. Answering that item (inbox.apply_approval) writes the digest-bound ApprovalRecord and
        re-advances the task. Also self-heals: an already-answered approval item whose record never landed (crash
        between the two writes) is re-applied here. Human-adapter tasks are not touched — their open attempt's
        collect() resumes them. Respects the per-role inbox cap: a full inbox means try again next iteration."""
        for task in tasks_mod.load_all(self.layout).values():
            if task.status != TaskStatus.WAITING_FOR_HUMAN or task.human_approval == HumanApproval.none:
                continue
            digest = task.delivery.digest
            if not digest or any(a.digest == digest and not a.superseded for a in task.delivery.approvals):
                continue
            if any(r["task_id"] == task.id and r.get("adapter") == "human" for r in jobs_mod.open_records(self.layout)):
                continue
            items = [i for i in inbox_mod.all_items(self.layout)
                     if i.get("task_id") == task.id and i.get("approval_digest") == digest]
            if any(i.get("status") == "open" for i in items):
                continue
            answered = [i for i in items if i.get("status") == "answered"]
            if answered:
                inbox_mod.apply_approval(self.layout, answered[-1], by="loop")
                continue
            reviews = ", ".join(f"{r.role} {r.verdict.value}" for r in task.delivery.reviews
                                if r.digest == digest and not r.superseded) or "none required"
            summary, detail = inbox_mod.fit(
                f"{task.id} is verified and reviewed ({reviews}) on digest {digest[:12]} and needs your approval "
                f"to complete. {task.approval_note or ''} Acceptance: {task.acceptance} Artifacts: "
                + ", ".join(task.artifacts))
            try:
                inbox_mod.create(self.layout, for_role="owner", subject=f"Approve delivery: {task.id} — {task.title}",
                                 summary=summary, detail=detail, kind=inbox_mod.APPROVAL_KIND,
                                 allowed_responses=inbox_mod.APPROVAL_RESPONSES, free_text=True, from_="loop",
                                 task_id=task.id, approval_digest=digest)
            except inbox_mod.InboxFull:
                continue                                      # owner attention is the scarce resource: wait

    def settle(self) -> None:
        """Collect finished jobs; adopt running ones."""
        for rec in jobs_mod.open_records(self.layout):
            ad = REGISTRY.get(rec["adapter"])
            if ad is None:
                jobs_mod.set_phase(self.layout, rec["attempt_id"], "FAILED", error="adapter unavailable")
                if (rec.get("spec") or {}).get("kind") == "conversation":
                    conversations_mod.fail(self.layout, rec, f"adapter {rec['adapter']} unavailable")
                else:
                    tasks_mod.set_status(self.layout, rec["task_id"], TaskStatus.FAILED, actor="loop", note=f"{rec['attempt_id']}: adapter {rec['adapter']} unavailable")
                continue
            if rec["phase"] == "PREPARED":
                continue
            handle = rec.get("handle") or {}
            try:
                result = ad.collect(rec["spec"], handle)
                if result is None:
                    if rec["phase"] == "SUBMITTED":
                        rec = jobs_mod.set_phase(self.layout, rec["attempt_id"], "RUNNING")
                elif result.status == "FAILED" and result.error and "without result" in result.error and not ad.alive(handle):
                    result.error = f"lost: {result.error}"
            except Exception as exc:                          # noqa: BLE001 - the job's failure, not the loop's
                self._adapter_failed(rec, "collect", exc)
                continue
            if result is None:
                # Outside the adapter boundary above: a job's own progress report is advisory and must never be
                # mistaken for the adapter failing (observe() does not raise; this keeps it that way structurally).
                progress_mod.observe(self.layout, rec)         # one log line per reported stage
                continue
            self.ingest(rec, result)

    # ------------------------------------------------------------ one iteration

    def once(self) -> str:
        """One iteration; returns the decision in force (RUN/PAUSE/STOP/RELOAD). Raises PluginError (before
        touching anything) while a required plugin is missing: `run()` reports that as BLOCKED instead."""
        start = time.perf_counter()

        def turn_s() -> float:
            return round(time.perf_counter() - start, 3)

        self._require_plugins()
        self.iteration += 1
        commands_mod.apply_all(self.layout)
        decision = runtime.read(self.layout).get("decision", "RUN")
        if decision in ("STOP", "RELOAD"):
            runtime.heartbeat(self.layout, "STOPPED" if decision == "STOP" else "RUNNING", iteration=self.iteration,
                              turn_s=turn_s())
            return decision
        # One real load of the store and one real scan of the open jobs for the whole turn (R-09): every step
        # below that used to call tasks_mod.load_all()/jobs_mod.open_records() on its own now works from this
        # same snapshot, and a write any step makes is visible to every step after it (tasks._update(),
        # jobs.write()) — never a second parse of 365 task files or a second scan of 1,380 job files per turn.
        tasks_snapshot = tasks_mod.load_all(self.layout)
        jobs_snapshot = jobs_mod.open_records(self.layout)
        with tasks_mod.turn_snapshot(tasks_snapshot), jobs_mod.turn_snapshot(jobs_snapshot):
            self.supersede_changed_attempts()  # R-12: before reconcile ever launches or collects a now-stale attempt
            self.reconcile()             # collects finished work under every decision; launches only when the gate allows
            tasks_mod.reconcile_advance(self.layout, tasks_snapshot, {r["task_id"] for r in jobs_snapshot})
            # evidence-complete tasks never wait on a replay (R-08); a task with an open attempt stays put (P1-1)
            self.poll_capacity()
            if decision in ("PAUSE", "DRAIN"):
                if decision == "PAUSE":
                    runtime.heartbeat(self.layout, "PAUSED", iteration=self.iteration, turn_s=turn_s())
                else:
                    decision = self.drain()
                # Commits launch nothing, so they go on while paused or draining (1.13.0): the owner's commands and
                # the work reconcile() collected are records too, and a completed drain commits its last ones at once.
                done = checkpoint_mod.run(self.layout)
                self.commit_state(after_checkpoint=bool(done) or decision == "STOP")
            else:
                for task, name in scheduler.plan(self.layout, unattended=self.unattended):
                    self.dispatch_delivery(task, name)
                self.dispatch_reviews()
                self.dispatch_conversations()
                self.settle()                                             # synchronous adapters finish at once
                self.request_approvals()
                self.request_owner_decisions()
                self.resume_plans()                                       # a crashed or stalled plan finishes here
                self.preflight_backfill()                                 # RECOVERY_PLAN.md 3.3's one-time sweep
                self.sweep_stalled()                                      # nothing stops silently
                self.alert_if_idle()
                done = checkpoint_mod.run(self.layout)                    # accepted work → git, exactly as accepted
                self.commit_state(after_checkpoint=bool(done))           # the loop's own records → git (1.12.1)
                runtime.heartbeat(self.layout, "RUNNING", iteration=self.iteration, busy=None,
                                  open_jobs=[r["attempt_id"] for r in jobs_mod.open_records(self.layout)],
                                  turn_s=turn_s())
            if getattr(self, "_lock", None):
                self._lock.touch()                                # cross-host liveness of the lock (locks.foreign_holder)
            if self.iteration % self.render_every == 0:
                render.render_all(self.layout, runtime=runtime.read(self.layout))
        return decision

    def drain(self) -> str:
        """DRAIN: nothing launches (the gate refuses every path), running jobs are collected and ingested by
        reconcile(); once no job is SUBMITTED/RUNNING the decision becomes STOP — and the workstation powers off when
        the owner asked for that (`shutdown_on_drain`). Held PREPARED records simply wait for the next start."""
        # A `human` adapter record waits for the owner, not for a machine: it must never keep the PC on.
        active = [r["attempt_id"] for r in jobs_mod.open_records(self.layout)
                  if r["phase"] in gate.ACTIVE_PHASES and r.get("adapter") != "human"]
        doc = runtime.read(self.layout)
        if active:
            runtime.heartbeat(self.layout, "DRAINING", iteration=self.iteration, open_jobs=active,
                              shutdown_on_drain=bool(doc.get("shutdown_on_drain")))
            return "DRAIN"
        tasks_mod.log(self.layout, "drain_complete", "loop", shutdown=bool(doc.get("shutdown_on_drain")))
        if doc.get("shutdown_on_drain"):
            shutdown_mod.schedule(self.layout, actor="loop")
        runtime.set_decision(self.layout, "STOP", "loop-drain")
        runtime.heartbeat(self.layout, "STOPPED", iteration=self.iteration, reason="drain complete", open_jobs=[])
        return "STOP"

    def poll_capacity(self) -> None:
        """Token-free codex window refresh (app-server RPC) when the observation is stale and something could send work
        to the Codex lane (scheduler.lane_in_use); claude windows arrive via streams."""
        try:
            from .adapters import codex_cli
            if "codex_cli" in REGISTRY and scheduler.lane_in_use(self.layout, "codex_cli"):
                codex_cli.poll_capacity(self.layout, max_age_s=codex_cli.POLL_MAX_AGE_S)
        except Exception as exc:                         # never let a usage probe kill the loop
            tasks_mod.log(self.layout, "capacity_poll_error", "loop", error=str(exc)[:200])

    def sleep(self) -> None:
        deadline = time.monotonic() + self.sleep_s
        while time.monotonic() < deadline:
            if self.layout.wake.exists():
                try:
                    self.layout.wake.unlink()
                except OSError:
                    pass
                return
            time.sleep(0.2)

    def run(self, max_iterations: int | None = None, reset_decision: bool = False) -> int:
        try:
            lock = FileLock(self.layout.lock, owner=f"supervisor pid {os.getpid()}").acquire()
            self._lock = lock
        except LockHeld:
            print("another supervisor holds state/.lock", file=sys.stderr)
            return 2
        try:
            blocked = self.plugins_blocked()
            if blocked:                                       # a missing protection is not a loop error to budget: refuse
                tasks_mod.log(self.layout, "plugins_blocked", "loop", reason=blocked)
                runtime.heartbeat(self.layout, "BLOCKED", reason=blocked)
                print(f"supervisor refuses to run: {blocked}", file=sys.stderr)
                return 1
            runtime.write(self.layout, status="RUNNING", pid=os.getpid(), started=now_iso(), decision=runtime.read(self.layout).get("decision", "RUN"))
            doc = runtime.read(self.layout)
            if reset_decision and doc.get("decision") == "RELOAD":     # a reload the loop never got to finish
                runtime.set_decision(self.layout, doc.get("after_reload") or "RUN", "loop-start", after_reload=None)
            elif reset_decision and doc.get("decision") == "STOP":
                runtime.set_decision(self.layout, "RUN", "loop-start")   # `supervisor run` = owner Start
            while True:
                try:
                    decision = self.once()
                except Exception:  # noqa: BLE001 - loop error budget
                    tasks_mod.log(self.layout, "loop_error", "loop", traceback=traceback.format_exc()[-2000:])
                    self.errors = [t for t in self.errors if time.time() - t < 600] + [time.time()]
                    if len(self.errors) >= 5:
                        runtime.heartbeat(self.layout, "BLOCKED", reason="5 loop errors in 10 minutes")
                        return 1
                    decision = "RUN"
                if decision == "STOP":
                    runtime.heartbeat(self.layout, "STOPPED")
                    return 0
                if decision == "RELOAD":
                    runtime.heartbeat(self.layout, "STOPPED", reason="reload requested")
                    # back with the decision the reload replaced: a paused loop stays paused (1.13.0)
                    after = runtime.read(self.layout).get("after_reload") or "RUN"
                    runtime.set_decision(self.layout, after, "loop-reload", after_reload=None)
                    return EXIT_RELOAD
                if max_iterations is not None and self.iteration >= max_iterations:
                    runtime.heartbeat(self.layout, "STOPPED", reason="max iterations")
                    return 0
                self.sleep()
        finally:
            lock.release()
