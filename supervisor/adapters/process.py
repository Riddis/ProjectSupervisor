"""Subprocess adapter base: launches a command detached from the loop, records {pid, create_time} as the handle
(psutil-verified liveness, never pid alone), tails stdout to the run dir, reads result.json when finished.
claude_cli / codex_cli subclass this. A plain `process` adapter exists for tests and simple scripts."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import psutil   # hard dependency: a job is never launched without a verifiable {pid, create_time} handle

from .. import policy
from .._vendor.contract import atomic_json, read_json
from ..store import now_iso
from .base import Adapter, Result, register


def job_spec(task) -> dict:
    extra = getattr(task, "model_extra", None) or {}
    return dict(extra.get("job") or {})


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


class ProcessAdapter(Adapter):
    name = "process"

    def command(self, spec) -> list[str]:
        return spec["argv"]

    def prepare(self, layout, task, attempt_id):
        jobspec = job_spec(task)
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir),
                "argv": jobspec.get("argv", [sys.executable, "-c", "print('hello')"]), "timeout_s": jobspec.get("timeout_s", 3600),
                "root": str(layout.root), "stdin_file": jobspec.get("stdin_file")}

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
            return {k: st.get(k) for k in ("kind", "pid", "create_time", "started", "deadline")}   # alive → adopt; dead → lost
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
        return {**os.environ, "SUPERVISOR_ATTEMPT": str(spec.get("attempt_id", ""))}   # lets recover() find an untracked child

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

    def parse_result(self, spec, handle) -> Result:
        run_dir = Path(spec["run_dir"])
        rp = run_dir / "result.json"
        if rp.exists():
            payload = json.loads(rp.read_text(encoding="utf-8"))
            return Result(status=payload.get("status", "FAILED"), artifacts=payload.get("artifacts", []),
                          notes=payload.get("notes", ""), error=payload.get("error"), raw=payload)
        return Result(status="FAILED", error="process exited without result.json")

    def collect(self, spec, handle) -> Optional[Result]:
        if self.alive(handle):
            if time.time() > handle.get("deadline", float("inf")):
                self.terminate(handle)
                self.on_timeout(spec, handle)
                return Result(status="FAILED", error="timeout")
            return None
        return self.parse_result(spec, handle)


register(ProcessAdapter())
