"""OpenAI Codex CLI adapter: `codex -a never exec --ephemeral -s <sandbox> -m <model>
-c model_reasoning_effort="<effort>" --json --output-last-message <run>/result.txt -` with the prompt on stdin and
optional `-i <image>` inputs. Used for cross-vendor review lanes, second opinions and `$imagegen` jobs.
Capacity is read with the documented app-server RPC (vendored usage_policy.poll_codex)."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .. import capacity
from .. import policy
from .. import progress as progress_mod
from .. import timeouts
from .._vendor import usage_policy as up
from .._vendor.contract import atomic_json, structured_result
from .._vendor.handoff import instructions as handoff_instructions
from ..paths import Layout
from .base import Result, register
from .claude_cli import (AUTH_FAILURE_RE, LIMIT_RE, classify_status, cost_category, limit_result, model_limits, prior_handoff,
                         role_profile, routing)
from .process import ProcessAdapter, job_spec, scrub_env

CODEX_AUTH_RE = AUTH_FAILURE_RE
POLL_MAX_AGE_S = 600      # re-poll the app-server when the observation is older than this


def codex_env(layout: Layout | None = None) -> dict:
    """ChatGPT login only (no API keys), HOME/USERPROFILE guaranteed for nested shells."""
    env = scrub_env(dict(os.environ), layout)
    home = env.get("USERPROFILE") or env.get("HOME") or str(Path.home())
    env.setdefault("USERPROFILE", home); env.setdefault("HOME", home)
    if os.name == "nt" and "HOMEDRIVE" not in env and len(home) > 1 and home[1] == ":":
        env["HOMEDRIVE"], env["HOMEPATH"] = home[:2], home[2:]
    return env


def executable(layout: Layout) -> str:
    exe = (routing(layout).get("codex") or {}).get("executable")
    if exe and Path(exe).exists():
        return exe
    found = shutil.which("codex") or shutil.which("codex.exe")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    if local.exists():
        return str(local)
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    if base.exists():
        cands = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime)
        if cands:
            return str(cands[-1])
    return "codex"


class CodexCliAdapter(ProcessAdapter):
    name = "codex_cli"
    engine = "codex"
    handoff_before_cap = True
    limits = model_limits

    def prepare(self, layout, task, attempt_id):
        job = job_spec(task)
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        role = job.get("role") or task.owner_role          # review lanes pass role=code_review explicitly
        prof = role_profile(layout, role)
        cfg = routing(layout).get("codex") or {}
        # Model names in the roles block are the role's PRIMARY adapter's (usually Claude's); a codex job takes its
        # model from the role only when that role's adapter is codex_cli, else from routing.codex.model, else the
        # account default (no -m). Codex rejects Claude model aliases such as "sonnet".
        if prof.get("adapter") != "codex_cli":
            prof["model"] = cfg.get("model")
        # The role NAME is only a last-resort default for the sandbox; a review job passes `sandbox` explicitly
        # (supervisor.review.REVIEW_LANE / routing.yaml roles.<role>.review), because roles that both build and review
        # must not review with write capability. Codex has no per-path write mode, so a review runs read-only and
        # its handoff is prose in result.txt (_vendor.handoff.instructions).
        prof.setdefault("sandbox", cfg.get("sandbox", "read-only" if role == "code_review" else "workspace-write"))
        prof.update({k: v for k, v in job.items() if k in ("model", "effort", "sandbox")})
        prompt_file = run_dir / "prompt.md"
        if job.get("prompt_file"):
            text = Path(layout.root / job["prompt_file"]).read_text(encoding="utf-8")
        else:
            text = job.get("prompt") or f"# Task {task.id}: {task.title}\n\n{task.objective}\n\nEnd with exactly one fenced JSON `{job.get('result_kind', 'delivery')}` object (schema_version 1)."
        from .. import packets as packets_mod
        text = packets_mod.worker_prompt(task, job, text)                    # owner/diagnosis guidance, any prompt source
        text += "\n\n" + handoff_instructions(run_dir, prior=prior_handoff(layout, task.id, attempt_id))
        if job.get("result_kind", "delivery") == "delivery":     # only work that can run long reports progress
            text += "\n\n" + progress_mod.instructions(run_dir)
        policy.assert_packet_clean(text, layout, f"{self.name} prompt {task.id}/{attempt_id}",
                                   job=job, task=task, lane=self.name)      # hosted lane: screens see the job too
        prompt_file.write_text(text, encoding="utf-8")
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir), "root": str(layout.root),
                "role": role, "model": prof.get("model"), "effort": prof.get("effort", "high"),
                "sandbox": prof.get("sandbox", "read-only"), "images": job.get("images", []), "stdin_file": str(prompt_file),
                **timeouts.spec_fields(timeouts.resolve(routing(layout), cost_category(task), prof, job)),
                "executable": job.get("executable") or executable(layout),
                "result_kind": job.get("result_kind", "delivery")}

    def command(self, spec):
        argv = [spec["executable"], "-a", "never", "exec", "--ephemeral", "-s", spec["sandbox"]]
        if spec.get("model"):
            argv += ["-m", spec["model"]]
        argv += ["-c", f'model_reasoning_effort="{spec["effort"]}"', "--json", "--output-last-message",
                 str(Path(spec["run_dir"]) / "result.txt")]
        for img in spec.get("images", []):
            argv += ["-i", str(img)]
        return argv + ["-"]

    def env(self, spec):
        env = codex_env()
        env["SUPERVISOR_ATTEMPT"] = str(spec.get("attempt_id", ""))
        env.update(progress_mod.env(spec))                    # scripts the session starts report progress (supervisor.progress)
        return env

    def submit(self, spec):
        capacity.require(Layout(spec["root"]), self.engine)
        return super().submit(spec)

    def agent_handle(self, layout: Layout) -> dict:
        """For supervisor.plugin.agent_lane: what a plugin needs to launch this agent itself, under this lane's
        environment rules (the executable, the configured model, a callable returning the scrubbed environment)."""
        return {"executable": executable(layout), "model": (routing(layout).get(self.engine) or {}).get("model"),
                "environment": lambda: codex_env(layout)}

    @staticmethod
    def events(run_dir: Path) -> list[dict]:
        p = run_dir / "stdout.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def parse_result(self, spec, handle) -> Result:
        run_dir = Path(spec["run_dir"])
        evs = self.events(run_dir)
        stderr = (run_dir / "stderr.txt").read_text(encoding="utf-8", errors="replace") if (run_dir / "stderr.txt").exists() else ""
        session = next((e.get("thread_id") for e in evs if e.get("type") == "thread.started"), None)
        last_done = max((i for i, e in enumerate(evs) if e.get("type") == "turn.completed"), default=-1)
        completed = last_done >= 0
        failed = [e for i, e in enumerate(evs) if e.get("type") == "turn.failed" or (e.get("type") == "error" and i > last_done)]
        usage = next((e.get("usage") for e in reversed(evs) if e.get("type") == "turn.completed" and e.get("usage")), {}) or {}
        text = (run_dir / "result.txt").read_text(encoding="utf-8", errors="replace") if (run_dir / "result.txt").exists() else ""
        meta = {"session_id": session, "completed": completed, "errors": failed[-3:], "usage": usage}
        atomic_json(run_dir / "result_meta.json", meta)
        blob = stderr + "\n" + json.dumps(failed[-3:])          # diagnostics only, never the agent's answer text
        if LIMIT_RE.search(blob):
            layout = Layout(spec["root"])
            poll_capacity(layout)                                # measured windows if the RPC works, else synthetic (limit_result)
            return limit_result(layout, self.engine, blob[-300:], session_id=session, raw=meta)
        if AUTH_FAILURE_RE.search(blob) or "codex login" in blob.lower() or "401 unauthorized" in blob.lower():
            return Result(status="FAILED", error="login_required: run `codex login` on the workstation", session_id=session, raw=meta)
        if not completed or failed:
            return Result(status="FAILED", error=f"codex turn failed: {json.dumps(failed[-1]) if failed else 'no turn.completed'}"[:400],
                          session_id=session, raw=meta)
        try:
            obj = structured_result(text, spec.get("result_kind", "delivery"))
        except ValueError:
            return Result(status="FAILED", error="no structured result in codex answer", session_id=session, usage=usage, raw=meta)
        atomic_json(run_dir / "result.json", obj)
        return Result(status=classify_status(obj),
                      artifacts=list(obj.get("artifacts") or []), notes=str(obj.get("notes") or obj.get("summary") or ""),
                      model=spec.get("model"), session_id=session, usage=usage, raw=obj)


def poll_capacity(layout: Layout, max_age_s: float = 0) -> dict | None:
    """Read Codex windows via app-server (no model turn). Skipped while the last observation is younger than max_age_s.
    Returns the observation (or the fresh-enough cached one), None when the RPC fails."""
    import time
    capacity.ensure(layout)
    if max_age_s:
        prev = up.read_provider("codex")
        if (prev.get("last_poll_epoch") or _last_failed_poll.get(str(layout.runtime), 0)) > time.time() - max_age_s:
            return prev if prev.get("windows") else None     # fresh observation, or recent failure: back off
    try:
        return up.poll_codex(executable(layout), env=codex_env(), timeout=15)   # records usage_codex.json itself
    except (OSError, RuntimeError, ValueError) as exc:
        _last_failed_poll[str(layout.runtime)] = time.time()
        try:                                                       # durable backoff across supervisor restarts
            prev = up.read_provider("codex")
            prev.update({"last_poll_state": "UNKNOWN", "last_poll_epoch": time.time(), "last_error": str(exc)[:200]})
            atomic_json(layout.runtime / "usage_codex.json", prev)
        except OSError:
            pass
        return None


_last_failed_poll: dict[str, float] = {}


register(CodexCliAdapter())
