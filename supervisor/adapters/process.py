"""Subprocess adapter base: launches a command detached from the loop, records {pid, create_time} as the handle
(psutil-verified liveness, never pid alone), tails stdout to the run dir, reads result.json when finished.
claude_cli / codex_cli subclass this. A plain `process` adapter exists for tests and simple scripts."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import psutil   # hard dependency: a job is never launched without a verifiable {pid, create_time} handle

from .. import envconfig
from .. import policy
from .. import timeouts
from .._vendor import handoff
from .._vendor.contract import atomic_json, read_json
from ..paths import Layout
from ..progress import env as progress_env
from ..store import now_iso
from .base import Adapter, Result, register


def job_spec(task) -> dict:
    job = getattr(task, "job", None)
    return job.model_dump(exclude_none=True, exclude_unset=True) if job is not None else {}


def _proc(handle: dict):
    if not handle.get("pid") or handle.get("create_time") is None:
        return None
    try:
        p = psutil.Process(handle["pid"])
        if abs(p.create_time() - float(handle.get("create_time", 0))) > 1.0:
            return None               # pid reused by another process
        return p
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


NESTING_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
               "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")


def scrub_env(env: dict, layout=None) -> dict:
    """Agent children get subscription logins only: no API keys/tokens, no vendor-nesting markers.

    The suffix list is `policy.env_key_suffixes()` - the same data that decides what is screened out of a packet
    and redacted from a log - so the documented guarantee and this filter cannot drift apart."""
    suffixes = policy.env_key_suffixes(layout)
    return {k: v for k, v in env.items()
            if not k.upper().endswith(suffixes) and k not in NESTING_ENV}


def _topmost_tagged(attempt: str):
    """Among processes tagged SUPERVISOR_ATTEMPT=attempt, the one whose parent is not tagged (the agent, not its Bash grandchild)."""
    tagged = {}
    for p in psutil.process_iter(["pid", "create_time"]):
        try:
            if p.environ().get("SUPERVISOR_ATTEMPT") == attempt:
                tagged[p.pid] = p
        except (psutil.Error, OSError):
            continue
    roots = [p for p in tagged.values() if _ppid(p) not in tagged]
    roots.sort(key=lambda p: p.create_time())
    return roots[0] if roots else None


def _ppid(p):
    try:
        return p.ppid()
    except (psutil.Error, OSError):
        return None


def job_identity_env(spec: dict) -> dict:
    """`SUPERVISOR_ATTEMPT` (kept, same value), `SUPERVISOR_JOB` (added, the same value under its own name) and
    `SUPERVISOR_ATTEMPT_NO` (added, the delivery attempt number this job belongs to) -- the three names every
    dispatched job's own child process carries so `recover()` and anything that parsed the old suffix keep working
    for one more major version (ATTEMPT_MODEL.md section 7). `attempt`, written onto `spec` by `loop.dispatch`
    from the id the allocator minted, not parsed back out of the id string — empty for a task-scoped job (a
    diagnosis raised before any delivery) or a conversation turn, same as an id with no such suffix read before
    this. Shared by every agent lane's own `env()`."""
    attempt_id = str(spec.get("attempt_id", ""))
    attempt = spec.get("attempt")
    return {"SUPERVISOR_ATTEMPT": attempt_id, "SUPERVISOR_JOB": attempt_id,
            "SUPERVISOR_ATTEMPT_NO": str(attempt) if attempt is not None else ""}


def _started(handle: dict, now: float) -> float:
    return timeouts._epoch(handle.get("started")) or float(handle.get("create_time") or now)


def _minutes(seconds: float) -> int:
    return max(0, int(round(seconds / 60)))


class ProcessAdapter(Adapter):
    name = "process"

    def __init__(self):
        # This session's own children (R-12): a real exit code, when the child reported none of its own, is
        # trustworthy only while the Popen that spawned it is still in hand (gone across a loop restart, same as
        # a handle with no result.json at all) and only when the loop itself did not kill it — a timeout or an
        # explicit terminate() (`_killed_pids`, by pid: `terminate()` gets only the handle, never the attempt id)
        # proves nothing about why the child would have exited on its own, so that case stays "lost", not a
        # verdict on the job's own determinism.
        self._procs: dict[str, subprocess.Popen] = {}
        self._killed_pids: set[int] = set()

    def command(self, spec) -> list[str]:
        return spec["argv"]

    def prepare(self, layout, task, attempt_id):
        jobspec = job_spec(task)
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        _, env_digest = envconfig.build(layout, self.name)
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir),
                "argv": jobspec.get("argv", [sys.executable, "-c", "print('hello')"]),
                "timeout_s": jobspec.get("timeout_s", timeouts.PROCESS_CAP_S), "idle_s": jobspec.get("idle_timeout_s"),
                "retry_on": list(jobspec.get("retry_on") or []),
                "root": str(layout.root), "stdin_file": jobspec.get("stdin_file"), "env_digest": env_digest}

    def submit(self, spec):
        run_dir = Path(spec["run_dir"])
        status_path = run_dir / "status.json"
        atomic_json(status_path, {"status": "LAUNCHING", "attempt_id": spec.get("attempt_id"), "at": now_iso()})
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        with open(run_dir / "stdout.jsonl", "ab") as stdout, open(run_dir / "stderr.txt", "ab") as stderr:
            stdin_ctx = open(spec["stdin_file"], "rb") if spec.get("stdin_file") else None
            try:
                proc = subprocess.Popen(self.command(spec), stdin=stdin_ctx or subprocess.DEVNULL, stdout=stdout,
                                        stderr=stderr, cwd=spec["root"], creationflags=flags, env=self.env(spec))
            finally:
                if stdin_ctx:
                    stdin_ctx.close()
        if spec.get("attempt_id"):
            self._procs[spec["attempt_id"]] = proc
        create_time = None
        for _ in range(20):                                   # a just-spawned process can be AccessDenied briefly on Windows
            try:
                create_time = psutil.Process(proc.pid).create_time()
                break
            except psutil.Error:
                time.sleep(0.05)
        if create_time is None:
            proc.kill()
            raise RuntimeError(f"could not capture create_time for pid {proc.pid}; job not launched")
        handle = {"kind": "process", "pid": proc.pid, "create_time": create_time, "started": now_iso(),
                  "deadline": time.time() + spec["timeout_s"]}
        atomic_json(status_path, {"status": "RUNNING", **handle})
        return handle

    def recover(self, spec):
        """Handle of a child launched by a previous (crashed) loop, if any."""
        run_dir = Path(spec["run_dir"])
        try:
            st = read_json(run_dir / "status.json")
        except (OSError, ValueError):
            return None
        if st.get("status") == "RUNNING" and st.get("pid"):
            handle = {k: st.get(k) for k in ("kind", "pid", "create_time", "started", "deadline")}   # alive → adopt; dead → lost
            if st.get("extensions"):
                handle["extensions"] = st["extensions"]          # owner Extend clicks (agent.extend) survive a re-adoption
            return handle
        if st.get("status") == "LAUNCHING":
            finished = {"kind": "process", "pid": None, "create_time": None, "started": st.get("at"), "deadline": float("inf")}
            if (run_dir / "result.json").exists():
                return finished
            attempt = str(spec.get("attempt_id", ""))
            top = _topmost_tagged(attempt) if attempt else None      # killed between Popen and the RUNNING write
            if top is not None:
                handle = {"kind": "process", "pid": top.pid, "create_time": top.create_time(), "started": st.get("at"),
                          "deadline": time.time() + spec.get("timeout_s", 3600)}
                atomic_json(run_dir / "status.json", {"status": "RUNNING", **handle})
                return handle
            # The scan takes time; a child that finished while it ran is no longer in the process table but has
            # left its result — look again before declaring the attempt lost, or the caller launches it a second time.
            if (run_dir / "result.json").exists():
                return finished
        return None

    def env(self, spec):
        """The declared base plus this lane's own additions (`envconfig.build`, R-24) — never `os.environ`:
        a vendor variable the project did not name in `config/routing.yaml` does not reach this child, however
        many new ones the loop's own launching environment picks up. `job_identity_env` (lets recover() find an
        untracked child) and the run-directory/progress markers are the framework's own, added after."""
        declared, _ = envconfig.build(Layout(spec.get("root")), spec.get("adapter", self.name))
        return {**declared, **job_identity_env(spec),
                # where the job writes result.json (1.13.0); before, only the progress file's parent said so
                **({"SUPERVISOR_RUN_DIR": str(spec["run_dir"])} if spec.get("run_dir") else {}),
                **progress_env(spec)}

    def on_timeout(self, spec, handle) -> None:
        """Hook: adapters may salvage partial output (e.g. rate-limit events) before the timeout Result."""

    def alive(self, handle):
        p = _proc(handle)
        try:                                        # the child can exit between is_running() and status() (I-004)
            return bool(p and p.is_running() and p.status() != getattr(psutil, "STATUS_ZOMBIE", "zombie"))
        except psutil.Error:
            return False

    def terminate(self, handle):
        p = _proc(handle)
        if p:
            try:
                for child in p.children(recursive=True):
                    child.kill()
                p.kill()
            except psutil.Error:
                pass
        if handle.get("pid") is not None:
            self._killed_pids.add(handle["pid"])

    def parse_result(self, spec, handle) -> Result:
        run_dir = Path(spec["run_dir"])
        rp = run_dir / "result.json"
        proc = self._procs.pop(spec.get("attempt_id"), None)
        killed = handle.get("pid") in self._killed_pids
        self._killed_pids.discard(handle.get("pid"))
        if rp.exists():
            payload = json.loads(rp.read_text(encoding="utf-8"))
            return Result(status=payload.get("status", "FAILED"), artifacts=payload.get("artifacts", []),
                          notes=payload.get("notes", ""), error=payload.get("error"), raw=payload)
        # R-12: a child that exited on its own, with no result.json of its own, still reports a real exit code -
        # this session's own Popen, never the loop's own termination (killed, or adopted after a restart, where
        # `proc` is None): those stay the generic "lost" message, exactly as before, because nothing here can
        # tell whether they would have exited this way on their own.
        code = proc.poll() if (proc is not None and not killed) else None
        if code is not None:
            return Result(status="FAILED", error=f"process exited {code} without result.json", raw={"exit_code": code})
        return Result(status="FAILED", error="process exited without result.json")

    handoff_before_cap = False   # model lanes: ask the session for its handoff shortly before the hard cap

    def limits(self, spec, handle) -> tuple[float, Optional[int], float]:
        """(hard-cap deadline, idle limit in seconds or None, idle CPU threshold) of a running job (supervisor.timeouts).
        The deadline lives in the handle — Extend (`agent.extend`) moves it there."""
        deadline = handle.get("deadline")
        return (float("inf") if deadline is None else float(deadline), spec.get("idle_s"),
                float(spec.get("idle_cpu_s") or timeouts.DEFAULTS["idle_cpu_s"]))

    def _time_out(self, spec, handle, kind: str, error: str) -> Result:
        self.terminate(handle)
        self.on_timeout(spec, handle)
        return Result(status="FAILED", error=error, raw={"timeout": kind})

    def collect(self, spec, handle) -> Optional[Result]:
        """A job ends on time only when it is idle (no output, no CPU work in its process tree for `idle_s`) or past
        its hard cap (supervisor.timeouts). Before 1.7.0 a fixed 3600 s wall clock killed sessions mid-work."""
        if self.alive(handle):
            now = time.time()
            deadline, idle_s, idle_cpu_s = self.limits(spec, handle)
            if now > deadline:
                return self._time_out(spec, handle, "cap", f"timeout: hard cap reached ({_minutes(now - _started(handle, now))} min "
                                      "after launch); still busy — Extend in the Control Center gives a running job more time")
            run_dir = spec.get("run_dir")
            if idle_s and run_dir:
                last = timeouts.last_active(Path(run_dir), handle, idle_cpu_s, now)
                if now - last > idle_s:
                    return self._time_out(spec, handle, "idle", f"timeout: idle for {_minutes(now - last)} min (no new output and "
                                          f"under {idle_cpu_s:g} s of CPU work in its processes)")
            if (self.handoff_before_cap and run_dir and deadline - now <= timeouts.HANDOFF_WARN_S
                    and int(spec.get("timeout_s") or 0) > 2 * timeouts.HANDOFF_WARN_S
                    and not (Path(run_dir) / "HANDOFF_REQUEST.json").exists()):
                handoff.request(Path(run_dir), reason="time limit")       # wrap up: the continuation picks up the handoff
            return None
        return self.parse_result(spec, handle)


register(ProcessAdapter())
