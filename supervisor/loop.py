"""The supervisor loop: token-free; holds state/.lock; one iteration =
apply commands → honour decision (pause/stop/reload) → reconcile job records → settle finished jobs → dispatch →
render → sleep (wake file shortens the sleep). Resumable after a kill at any point (jobs.py)."""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

from . import capacity, checkpoint as checkpoint_mod, commands as commands_mod, conversations as conversations_mod
from . import diagnosis as diagnosis_mod
from . import escalation as escalation_mod
from . import gate, inbox as inbox_mod, jobs as jobs_mod, render, review as review_mod
from . import runtime, scheduler, shutdown as shutdown_mod, tasks as tasks_mod, verify as verify_mod
from ._vendor.contract import CapacityDeferred, atomic_json, digest as digest_of, manifest, read_json
from .locks import LockHeld as _LockHeld  # noqa: F401
from ._vendor import handoff
from .adapters import REGISTRY, Result
from .locks import FileLock, LockHeld
from .models.task import HumanApproval, Task, TaskStatus
from .paths import Layout
from .store import now_iso

EXIT_RELOAD = 3


def _attempt_no(attempt_id: str) -> int:
    try:
        return int(str(attempt_id).rsplit(".a", 1)[1])
    except (IndexError, ValueError):
        return 0


def worker_report(result, run_dir: str | None, limit: int = 2500) -> str:
    """What the worker said about why it stopped — its notes plus the head of its HANDOFF.md — so the failure detail
    (revision brief, diagnosis brief, task record, owner item) carries the reason, not just "status BLOCKED"."""
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
        from . import extensions
        self.layout = layout.ensure()
        extensions.load(self.layout)                           # project lanes/checks register before any dispatch
        self.sleep_s = sleep_s
        self.render_every = render_every
        self.unattended = unattended
        self.iteration = 0
        self.errors: list[float] = []

    def extensions_blocked(self) -> str | None:
        """Why this loop must not dispatch, or None: a required extension (project.yaml `extensions:`) failed to
        load, so the lane, screen or check it provides is missing. `supervisor doctor` shows the error; the owner
        fixes the module or marks it `required: false`."""
        from . import extensions
        return extensions.load(self.layout).blocking_reason()

    def _require_extensions(self) -> None:
        from . import extensions
        reason = self.extensions_blocked()
        if reason:
            raise extensions.ExtensionError(reason)

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
                kind: str = "delivery", extra: dict | None = None) -> dict:
        self._require_extensions()
        ad = REGISTRY[adapter_name]
        n = len(jobs_mod.for_task(self.layout, task.id)) + 1
        attempt_id = f"{task.id}.a{n}"
        try:
            spec = ad.prepare(self.layout, task, attempt_id)
        except Exception as exc:                              # noqa: BLE001 - the attempt fails, the loop goes on
            spec = {"adapter": adapter_name, "task_id": task.id, "attempt_id": attempt_id, "kind": kind}
            rec = jobs_mod.prepare(self.layout, task.id, attempt_id, adapter_name, spec,
                                   gpu=bool(getattr(ad, "gpu", False)), **(extra or {}))
            if kind != "conversation":
                tasks_mod.add_attempt(self.layout, task.id, adapter_name, note=f"{note} {attempt_id}: prepare failed")
            self._adapter_failed(rec, "prepare", exc)
            return jobs_mod.read(self.layout, attempt_id)
        spec["kind"] = kind
        if gate.owned_for(task, adapter_name, kind):          # every writing job: declared roots or exclusive
            atomic_json(Path(spec["run_dir"]) / "before_manifest.json", verify_mod.snapshot(self.layout, task))
        if extra and extra.get("review_frozen") is not None and spec.get("run_dir"):
            atomic_json(Path(spec["run_dir"]) / "frozen_inputs.json", extra["review_frozen"])   # audit trail
        # One durable write carries everything a continuation or ingestion needs: a crash between prepare() and a
        # second record write used to leave a review job without its role and discard the finished verdict.
        rec = jobs_mod.prepare(self.layout, task.id, attempt_id, adapter_name, spec, gpu=ad.gpu,
                               **(extra or {}))                                   # durable BEFORE launch
        if kind != "conversation":            # no Task backs a conversation thread (supervisor.conversations)
            tasks_mod.add_attempt(self.layout, task.id, adapter_name, note=f"{note} {attempt_id}", run_dir=spec.get("run_dir"))
        if deferred_until:
            return jobs_mod.set_phase(self.layout, attempt_id, "PREPARED", deferred_until=deferred_until, deferred_reason=note)
        return self.launch(rec)

    MAX_CONTINUATIONS = 3

    def requeue(self, rec: dict, result: Result, reason: str) -> None:
        """Capacity limit or handoff-incomplete: queue a continuation attempt FIRST (crash-safe), then close this attempt
        without a delivery; the adapter's prepare() points the fresh session at the prior HANDOFF.md. Capped per task:
        after MAX_CONTINUATIONS the task is BLOCKED for the owner."""
        task_id = rec["task_id"]
        all_recs = jobs_mod.for_task(self.layout, task_id)
        if any(_attempt_no(r["attempt_id"]) > _attempt_no(rec["attempt_id"]) for r in all_recs):
            jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(),
                               error=result.error, continued=True)       # continuation already queued before a crash
            return
        prior = [r for r in all_recs if str(r.get("deferred_reason", "")).startswith("continuation")]
        n = len(prior)
        until = result.reset_epoch or time.time()
        if result.status == "DEFERRED" and not result.raw.get("measured_window"):
            until = max(until, time.time() + 3600 * (2 ** n))              # synthetic window grows per repeat
        tasks_mod.log(self.layout, "job_requeued", "loop", task=task_id, attempt=rec["attempt_id"], reason=reason,
                      result=result.status, error=result.error, until=until, continuation=n + 1)
        if n >= self.MAX_CONTINUATIONS:
            jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(), error=result.error)
            tasks_mod.set_status(self.layout, task_id, TaskStatus.BLOCKED, actor="loop",
                                 note=f"{rec['attempt_id']}: {reason} after {n} continuations — owner decision needed")
            return
        kind = (rec.get("spec") or {}).get("kind", "delivery")
        if kind == "review":
            task = review_mod.rebuild(self.layout, rec)
            if task is None:                                  # inputs moved on; pending() queues a fresh review instead
                jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(), error=result.error)
                return
            extra = {k: rec[k] for k in ("review_role", "review_digest", "same_vendor", "review_frozen") if k in rec}
            self.dispatch(task, rec["adapter"], deferred_until=until, note=f"continuation after {reason}", kind="review", extra=extra)
        elif kind == diagnosis_mod.KIND:
            task = diagnosis_mod.rebuild(self.layout, rec)
            if task is None:
                jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(), error=result.error)
                return
            extra = {k: rec[k] for k in rec if k.startswith("diag_")}
            self.dispatch(task, rec["adapter"], deferred_until=until, note=f"continuation after {reason}", kind=diagnosis_mod.KIND, extra=extra)
        else:
            task = tasks_mod.load(self.layout, task_id)
            self.dispatch(task, rec["adapter"], deferred_until=until, note=f"continuation after {reason}")
        jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso(), error=result.error,
                           continued=True)
        tasks_mod.add_note(self.layout, task_id, f"{rec['attempt_id']}: {reason}; continuation {n + 1} queued", actor="loop")

    def _ensure_in_progress(self, task_id: str, note: str) -> None:
        t = tasks_mod.load(self.layout, task_id)
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
        conversation = (rec.get("spec") or {}).get("kind") == "conversation"
        if not conversation and (rec.get("spec") or {}).get("kind") != diagnosis_mod.KIND:   # a diagnosis judges a BLOCKED task
            self._ensure_in_progress(rec["task_id"], f"launch {rec['attempt_id']} via {rec['adapter']}")
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
                else:
                    return rec
            except CapacityDeferred:
                return rec                                    # still gated: wait, costs nothing
            jobs_mod.set_phase(self.layout, rec["attempt_id"], "PREPARED", deferred_until=0)
        # The one gate: decision, capacity, limits and ownership — for every launch path alike. A refused
        # record stays PREPARED and is retried by reconcile() each iteration; the reason is recorded once per change.
        kind = (rec.get("spec") or {}).get("kind", "delivery")
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
        if rec.get("held"):
            rec = jobs_mod.set_phase(self.layout, rec["attempt_id"], "PREPARED", held=None)
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
            rec = jobs_mod.set_phase(self.layout, rec["attempt_id"], "PREPARED", launch_attempts=attempts - 1,
                                     deferred_until=exc.reset_epoch, deferred_reason=str(exc))
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

    def ingest(self, rec: dict, result: Result) -> None:
        """Turn an adapter Result into task records, then advance(). A delivery job gets a delivery (digest over
        artifacts) and a verification record (supervisor.verify); a review job gets a review record (supervisor.review).
        Either kind of substantive failure hands off to supervisor.escalation for the retry/issue decision."""
        task_id = rec["task_id"]
        kind = (rec.get("spec") or {}).get("kind", "delivery")
        if kind == "conversation":
            # No Task backs a thread and no retry chain applies (supervisor.conversations.ingest handles COMPLETE/
            # DEFERRED/FAILED itself, straight into the thread file) — bypass the delivery/review machinery below.
            conversations_mod.ingest(self.layout, rec, result)
            jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso())
            tasks_mod.log(self.layout, "job_done", "loop", task=task_id, attempt=rec["attempt_id"], result=result.status, error=result.error)
            return
        if kind != diagnosis_mod.KIND:
            self._ensure_in_progress(task_id, f"ingest {rec['attempt_id']}")
        run_dir = (rec.get("spec") or {}).get("run_dir")
        if result.status == "DEFERRED":
            return self.requeue(rec, result, "capacity limit")
        timed_out = (result.raw or {}).get("timeout") if result.status == "FAILED" else None
        if timed_out and rec.get("adapter") in gate.MODEL_LANES and kind != self.VERIFY_KIND:
            # 1.7.0: a session stopped by its time limit (supervisor.timeouts) was busy or stalled, not wrong — a
            # fresh session continues from its HANDOFF.md instead of spending a revision.
            return self.requeue(rec, result, "time limit (hard cap)" if timed_out == "cap" else "time limit (idle)")
        if result.status in ("PARTIAL", "BLOCKED") and run_dir and handoff.incomplete(Path(run_dir)):
            return self.requeue(rec, result, "time limit handoff" if _handoff_reason(run_dir) == "time limit" else "capacity handoff")
        if kind == "review":
            review_mod.ingest(self, rec, result)
        elif kind == diagnosis_mod.KIND:
            diagnosis_mod.ingest(self, rec, result)
        elif kind == self.VERIFY_KIND:
            self.ingest_verify(rec, result)
        else:
            task = tasks_mod.load(self.layout, task_id)
            # The contract is the floor: every declared artifact is expected whatever the worker reports;
            # a worker may add deliverables, never narrow the list.
            patterns = list(dict.fromkeys(list(task.artifacts) + list(result.artifacts or [])))
            files, missing = self._expand_artifacts(patterns)
            rendered = RENDERED_PROJECTIONS | verify_mod.excluded_files(self.layout)      # built-in and extension views
            volatile = [f for f in files if f.startswith("state/") or f in rendered]
            if volatile:                                           # the store/projections change on every tick: never an
                files = [f for f in files if f not in volatile]    # artifact, never a frozen review input
                tasks_mod.log(self.layout, "artifacts_dropped", "loop", task=task_id, dropped=volatile,
                              reason="state/ and rendered projections are not deliverables")
            try:
                dg = digest_of(manifest([self.layout.root / a for a in files], root=self.layout.root)) if files else digest_of({"attempt": rec["attempt_id"]})
            except ValueError as exc:
                dg, result = digest_of({"attempt": rec["attempt_id"]}), Result(status="FAILED", error=f"manifest: {exc}")
            tasks_mod.record_delivery(self.layout, task_id, dg, actor="loop", result=result.notes or None, paths=files)
            if result.status == "COMPLETE" and not missing:
                # a model lane must show its work: the brief asks for the commands it ran (packets.RESULT_CONTRACT_
                # DELIVERY), and a task that declares tests is not verified without them. Deterministic adapters
                # (python/process/...) declare no tests and are judged on their artifacts.
                model_lane = rec["adapter"] in gate.MODEL_LANES
                tracked = verify_mod.as_job(self.layout) and "process" in REGISTRY
                outcome = verify_mod.verify(self.layout, task, files, (result.raw or {}).get("tests") or [],
                                            self._before_manifest(rec),
                                            require_tests=bool(model_lane and task.tests),
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
                report = worker_report(result, run_dir) if result.status != "COMPLETE" else ""
                outcome = verify_mod.Outcome(False, head + (f"\n\nWorker report:\n{report}" if report else ""))
            self.record_outcome(task_id, dg, outcome, rec, substantive=result.status == "COMPLETE",
                                worker_status=result.status)
        jobs_mod.set_phase(self.layout, rec["attempt_id"], "DONE", result=result.status, finished=now_iso())
        tasks_mod.log(self.layout, "job_done", "loop", task=task_id, attempt=rec["attempt_id"], result=result.status, error=result.error)

    def record_outcome(self, task_id: str, dg: str, outcome, rec: dict, substantive: bool, event: str | None = None,
                       worker_status: str | None = None) -> None:
        """Write a verification Outcome for digest `dg` into the task store, advance, and hand EVERY failure to
        escalation (keyed by the delivery attempt, `event`, so a replay never spends a second revision).

        Before 1.6.0 only a substantive failure (the worker claimed COMPLETE, verification disagreed) reached
        escalation; a worker that ended BLOCKED/FAILED/PARTIAL, or crashed, timed out or was lost, left the task
        FAILED with no retry, no diagnosis and no owner item."""
        report_path = None
        if getattr(outcome, "report", None) and (rec.get("spec") or {}).get("run_dir"):
            rp = Path(rec["spec"]["run_dir"]) / "verification.json"
            atomic_json(rp, {"ok": outcome.ok, "detail": outcome.detail, **outcome.report})
            report_path = rp.relative_to(self.layout.root).as_posix() if rp.is_relative_to(self.layout.root) else str(rp)
        tasks_mod.record_verification(self.layout, task_id, dg, outcome.ok, report=report_path, detail=outcome.detail, actor="loop")
        tasks_mod.advance(self.layout, task_id, actor="advance")
        if outcome.ok:
            return
        lane = rec.get("adapter", "")
        if substantive:
            # the agent believed it was done and deterministic verification disagreed
            escalation_mod.record_failure(self, task_id, "verification", outcome.detail, event=event or rec["attempt_id"])
        elif lane == "human":
            # the owner answered REJECT / MORE_LIKE: that is the owner's decision, never auto-retried
            return
        elif worker_status == "BLOCKED":
            # the worker says it cannot proceed: no blind retry, straight to the exception route (diagnosis → owner)
            escalation_mod.record_failure(self, task_id, "worker_blocked", outcome.detail, event=event or rec["attempt_id"],
                                          retry=False)
        elif lane in self.IMMEDIATE_RETRY_LANES:
            # an agent run that ended FAILED/PARTIAL, crashed, timed out or was lost: retried within the revision
            # budget, then the exception route. (Capacity limits never get here — DEFERRED and handoff-incomplete runs
            # are continuations, see requeue().)
            escalation_mod.record_failure(self, task_id, "worker_failed", outcome.detail, event=event or rec["attempt_id"])
        # a deterministic lane (process/python/extension adapters) that FAILED is left to sweep_stalled(): its grace
        # period is the back-off — a timeout or a busy resource usually clears by itself — and it then retries within the same
        # budget and escalates the same way

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
                                 "fixed": fixed, "delivery_attempt": delivery_rec["attempt_id"]})
        # The child runs with the task root as cwd, which need not contain the supervisor package: import it by path.
        pkg_parent = str(Path(verify_mod.__file__).resolve().parents[1])
        code = (f"import sys; sys.path.insert(0, {pkg_parent!r}); from supervisor.verify_job import main; "
                f"sys.exit(main(['verify_job', {str(input_path)!r}]))")
        doc = task.model_dump(mode="json")
        doc["job"] = {"adapter": "process", "argv": [sys.executable, "-c", code], "timeout_s": verify_mod.TEST_BUDGET_S + 120}
        view = Task.model_validate(doc)
        rec = self.dispatch(view, "process", note=f"verify delivery {delivery_rec['attempt_id']}", kind=self.VERIFY_KIND,
                            extra={"verify_digest": dg, "verify_delivery": delivery_rec["attempt_id"], "verify_commands": len(commands)})
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

    def reconcile(self) -> None:
        """Before any dispatch: settle every non-DONE job record (crash-safe)."""
        for rec in jobs_mod.open_records(self.layout):
            if rec["phase"] == "PREPARED" and rec["adapter"] in REGISTRY:
                self.launch(rec)                                      # same attempt id: never a duplicate
        self.settle()
        self.repair_continuations()

    def dispatch_reviews(self) -> None:
        """Review dispatch: one review job per (WAITING_FOR_REVIEW task, missing reviewer role) under
        the same agent-job concurrency limit as primary dispatch — reviews and deliveries share one budget."""
        lim = scheduler.limits(self.layout)
        agent_busy = sum(1 for r in jobs_mod.open_records(self.layout) if r.get("adapter") in gate.MODEL_LANES)
        for task, role in review_mod.pending(self.layout):
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
        DONE with result DEFERRED but no continuation exists and the task is still IN_PROGRESS → queue it now."""
        seen = set()
        for rec in sorted(jobs_mod.all_records(self.layout), key=lambda r: r["attempt_id"]):
            seen.add(rec["task_id"])
        for task_id in seen:
            recs = jobs_mod.for_task(self.layout, task_id)
            last = recs[-1]
            run_dir = (last.get("spec") or {}).get("run_dir")
            needs = last.get("result") == "DEFERRED" or (last.get("result") in ("PARTIAL", "BLOCKED") and run_dir
                                                           and handoff.incomplete(Path(run_dir)))
            if last["phase"] == "DONE" and needs and not last.get("continued") and last["adapter"] in REGISTRY:
                try:
                    task = tasks_mod.load(self.layout, task_id)
                except Exception:
                    continue
                if task.status == TaskStatus.IN_PROGRESS:
                    self.dispatch(task, last["adapter"], deferred_until=time.time(), note="continuation after capacity limit")
                    jobs_mod.set_phase(self.layout, last["attempt_id"], "DONE", continued=True)

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
        item for the task, an owner answer (DEFER / note) given after the stop, a failure of the task's `human`
        attempt (the owner's REJECT/MORE_LIKE — linked through the attempt, not by comparing timestamps, because the
        loop collects the answer after it was given), or a diagnosis that deferred it after the stop. Returns the task
        ids acted on."""
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
            if str((stop or {}).get("by") or "").startswith("owner"):
                continue                                          # the owner put it here (Hold, set-status): their call
            mine = [i for i in items if i.get("task_id") == task.id]
            if any(i.get("status") in ("open", "snoozed") for i in mine):
                continue                                          # waiting on the owner (a snooze defers, it is not a stall)
            if any(max(str((i.get("applied") or {}).get("at") or ""), str(i.get("answered_at") or "")) >= at for i in mine):
                continue                                          # the owner already answered this stop
            records = jobs_mod.for_task(self.layout, task.id)
            deliveries = [r for r in records if (r.get("spec") or {}).get("kind", "delivery") == "delivery"]
            if deliveries and max(deliveries, key=lambda r: _attempt_no(r["attempt_id"])).get("adapter") == "human":
                continue                                          # the owner's own answer (REJECT/MORE_LIKE) failed it
            diags = [r for r in records
                     if (r.get("spec") or {}).get("kind") == diagnosis_mod.KIND and r.get("diag_action")]
            if any(r.get("diag_action") == "defer" and str(r.get("finished") or r.get("updated") or "") >= at for r in diags):
                continue                                          # a diagnosis parked it on purpose
            key = f"stall:{at}"
            state = escalation_mod._load(self.layout, task.id)
            last_note = str((stop or {}).get("note") or "")
            run_dir = next((a.run_dir for a in reversed(task.attempts) if a.run_dir), None)
            report = worker_report(Result(status=task.status.value), run_dir)
            detail = (f"{task.id} has been {task.status.value} since {at} with nothing in flight and no owner item. "
                      f"Last status note: {last_note or '(none)'}" + (f"\n\nLatest run report:\n{report}" if report else ""))
            if key not in state["events"] and escalation_mod.last_decision_at(self.layout, task.id) < at:
                tasks_mod.log(self.layout, "stall_detected", "loop", task=task.id, status=task.status.value, since=at)
                escalation_mod.record_failure(self, task.id, "stalled", detail, event=key,
                                              retry=task.status == TaskStatus.FAILED)
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

    def alert_if_idle(self, now: float | None = None) -> dict | None:
        """The whole pipeline stopped: nothing running or queued, nothing eligible, nothing waiting on the owner, yet
        work remains. One owner item per distinct stuck set (fingerprint), raised after twice the stall grace so the
        per-task sweep gets its chance first. It lists what each stuck task is waiting for."""
        now = time.time() if now is None else now
        path = self.layout.runtime / "idle_alert.json"
        state = read_json(path) if path.exists() else {}
        tasks = tasks_mod.load_all(self.layout)
        remaining = [t for t in tasks.values() if t.status not in (TaskStatus.COMPLETE, TaskStatus.CANCELLED)]
        idle = (not jobs_mod.open_records(self.layout) and not tasks_mod.eligible(self.layout, tasks)
                and not inbox_mod.open_items(self.layout, "owner") and remaining)
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
        stuck = sorted((t for t in remaining if t.status in (TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.WAITING_FOR_HUMAN)),
                       key=lambda t: t.id)
        fingerprint = "|".join(f"{t.id}:{t.status.value}:{t.updated}" for t in stuck) or "deps-or-gates"
        if state.get("fingerprint") == fingerprint:
            return None
        lines = []
        for t in stuck[:8]:
            notes = (t.model_extra or {}).get("notes") or []
            last = (notes[-1].get("note") if notes else "") or next(
                (h.get("note") for h in reversed(t.history or []) if h.get("to") == t.status.value), "")
            lines.append(f"{t.id} {t.status.value}: {str(last)[:220]}")
        if not stuck:
            lines.append("No task is failed or blocked: the remaining work waits on a phase gate or unfinished dependencies.")
        summary, detail = inbox_mod.fit(
            f"Nothing can run: {len(remaining)} task(s) remain, none is eligible, nothing is in flight. "
            + " · ".join(lines))
        try:
            item = inbox_mod.create(self.layout, for_role="owner", subject="Pipeline idle — nothing can run",
                                    summary=summary, detail=detail, kind="question", from_="loop",
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
                        jobs_mod.set_phase(self.layout, rec["attempt_id"], "RUNNING")
                    continue
                if result.status == "FAILED" and result.error and "without result" in result.error and not ad.alive(handle):
                    result.error = f"lost: {result.error}"
            except Exception as exc:                          # noqa: BLE001 - the job's failure, not the loop's
                self._adapter_failed(rec, "collect", exc)
                continue
            self.ingest(rec, result)

    # ------------------------------------------------------------ one iteration

    def once(self) -> str:
        """One iteration; returns the decision in force (RUN/PAUSE/STOP/RELOAD). Raises ExtensionError (before
        touching anything) while a required extension is missing: `run()` reports that as BLOCKED instead."""
        self._require_extensions()
        self.iteration += 1
        commands_mod.apply_all(self.layout)
        decision = runtime.read(self.layout).get("decision", "RUN")
        if decision in ("STOP", "RELOAD"):
            runtime.heartbeat(self.layout, "STOPPED" if decision == "STOP" else "RUNNING", iteration=self.iteration)
            return decision
        self.reconcile()                 # collects finished work under every decision; launches only when the gate allows
        self.poll_capacity()
        if decision == "PAUSE":
            runtime.heartbeat(self.layout, "PAUSED", iteration=self.iteration)
        elif decision == "DRAIN":
            decision = self.drain()
        else:
            for task, name in scheduler.plan(self.layout, unattended=self.unattended):
                self.dispatch(task, name)
            self.dispatch_reviews()
            self.dispatch_conversations()
            self.settle()                                             # synchronous adapters finish at once
            self.request_approvals()
            self.request_owner_decisions()
            self.sweep_stalled()                                      # nothing stops silently
            self.alert_if_idle()
            checkpoint_mod.run(self.layout)                           # accepted work → git, exactly as accepted
            runtime.heartbeat(self.layout, "RUNNING", iteration=self.iteration, busy=None,
                              open_jobs=[r["attempt_id"] for r in jobs_mod.open_records(self.layout)])
        if getattr(self, "_lock", None):
            self._lock.touch()                                    # cross-host liveness of the lock (locks.foreign_holder)
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
        """Token-free codex window refresh (app-server RPC) when the observation is stale; claude windows arrive via streams."""
        try:
            from .adapters import codex_cli
            if "codex_cli" in REGISTRY:
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
            blocked = self.extensions_blocked()
            if blocked:                                       # a missing protection is not a loop error to budget: refuse
                tasks_mod.log(self.layout, "extensions_blocked", "loop", reason=blocked)
                runtime.heartbeat(self.layout, "BLOCKED", reason=blocked)
                print(f"supervisor refuses to run: {blocked}", file=sys.stderr)
                return 1
            runtime.write(self.layout, status="RUNNING", pid=os.getpid(), started=now_iso(), decision=runtime.read(self.layout).get("decision", "RUN"))
            if reset_decision and runtime.read(self.layout).get("decision") in ("STOP", "RELOAD"):
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
                    runtime.set_decision(self.layout, "RUN", "loop-reload")
                    return EXIT_RELOAD
                if max_iterations is not None and self.iteration >= max_iterations:
                    runtime.heartbeat(self.layout, "STOPPED", reason="max iterations")
                    return 0
                self.sleep()
        finally:
            lock.release()
