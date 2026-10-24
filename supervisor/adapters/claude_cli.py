"""Claude Code CLI adapter: spawns the official `claude` binary non-interactively under the owner's
subscription login. Prompt on stdin, `--output-format stream-json`, per-role tool allow-lists, fresh session id per
attempt, rate_limit_events ingested into capacity, one tool-less follow-up when the structured result is missing.
The supervisor never reads or stores OAuth tokens."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import yaml

from .. import capacity
from .. import policy
from .. import timeouts
from .._vendor.contract import atomic_json, structured_result
from .._vendor.handoff import instructions as handoff_instructions
from ..paths import Layout
from .base import Result, register
from .process import ProcessAdapter, job_spec, scrub_env

AUTH_FAILURE_RE = re.compile(r"failed to authenticate|oauth (session|token).{0,40}expired|invalid api key|please run /login|"
                             r'"type":\s*"authentication_error"', re.IGNORECASE)
LIMIT_RE = re.compile(r"(session|usage|weekly) limit|hit your (usage )?limit|usage limit reached|rate.?limit.{0,20}(exceeded|reached)|"
                      r"out of (extra )?usage|limit.{0,40}resets? (at|in) ", re.IGNORECASE)
REVIEW_STATUS = {"ACCEPT": "COMPLETE", "REVISE": "PARTIAL", "REJECT": "PARTIAL", "BLOCKED": "BLOCKED"}
FALLBACK_LIMIT_S = 3600
FOLLOW_UP_TIMEOUT_S = 120    # synchronous inside settle(); keep short (known limitation, see runbook)          # synthetic window when the CLI reports a limit but no measurement is available


def classify_status(obj: dict) -> str:
    """Map a result object's status/verdict onto the Result status enum (review verdicts never become COMPLETE unless ACCEPT)."""
    if "verdict" in obj:
        return REVIEW_STATUS.get(str(obj.get("verdict")).upper(), "PARTIAL")
    status = str(obj.get("status") or "COMPLETE").upper()
    return status if status in ("COMPLETE", "PARTIAL", "BLOCKED", "FAILED") else "PARTIAL"


def limit_result(layout: Layout, engine: str, message: str, session_id=None, raw=None) -> Result:
    """A CLI-reported usage limit: record a conservative synthetic window unless a measured one already gates, then DEFERRED."""
    from .._vendor import usage_policy as up
    capacity.ensure(layout)
    until = up.reserve_until(engine)
    measured = until is not None
    if until is None:
        up.record(engine, [{"name": "cli_limit", "used_percent": 100, "reset_epoch": time.time() + FALLBACK_LIMIT_S}], message[:200])
        until = up.reserve_until(engine) or time.time() + FALLBACK_LIMIT_S
    return Result(status="DEFERRED", error=f"usage limit reported by the CLI: {message[:200]}", session_id=session_id,
                  raw={**(raw or {}), "measured_window": measured}, reset_epoch=until)


def prior_handoff(layout: Layout, task_id: str, attempt_id: str):
    """Newest HANDOFF.md of an earlier attempt of this task (continuation after capacity/partial handoff)."""
    base = layout.runs / task_id
    if not base.exists():
        return None
    cands = sorted((d / "HANDOFF.md" for d in base.iterdir() if d.is_dir() and d.name != attempt_id and (d / "HANDOFF.md").exists()),
                   key=lambda f: f.stat().st_mtime)
    return str(cands[-1]) if cands else None
DEFAULT_TOOLS = {
    "writer": ["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
    "reader": ["Read", "Glob", "Grep"],
    # A review lane: reads the repository it judges, writes only where `write_scope` allows (supervisor.review
    # REVIEW_LANE scopes it to the attempt's run dir, which is where HANDOFF.md belongs).
    "reviewer": ["Read", "Glob", "Grep", "Write"],
    "web": ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Write", "Edit", "Bash"],
    "none": [],
}
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# Claude Code matches each segment of a compound command (a; b && c) against these prefixes. The first live run
# was denied 9 times on mkdir/rm/where/tasklist/echo and env-prefixed python — so the list covers the
# ordinary shell vocabulary of an engineer; destructive git stays denied via GIT_DENY (deny rules win).
BASH_ALLOW = ["Bash(pwd)", "Bash(ls *)", "Bash(ls)", "Bash(dir *)", "Bash(cat *)", "Bash(head *)", "Bash(tail *)", "Bash(wc *)",
              "Bash(echo *)", "Bash(mkdir *)", "Bash(rm *)", "Bash(cp *)", "Bash(mv *)", "Bash(touch *)", "Bash(find *)", "Bash(grep *)",
              "Bash(rg *)", "Bash(where *)", "Bash(which *)", "Bash(tasklist *)", "Bash(sed *)", "Bash(sort *)", "Bash(diff *)",
              "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git add *)", "Bash(git stash list)",
              "Bash(python *)", "Bash(python3 *)", "Bash(py *)", "Bash(python.exe *)", "Bash(.venv/Scripts/python.exe *)",
              "Bash(SUPERVISOR_ROOT=*)", "Bash(PYTHONDONTWRITEBYTECODE=*)", "Bash(pytest *)", "Bash(pip *)", "Bash(powershell.exe *)",
              "Bash(powershell *)", "Bash(cmd *)", "Bash(node *)", "Bash(npm *)", "Bash(timeout *)", "Bash(sleep *)", "Bash(true)",
              # installs and archives (a project adds its own tool prefixes through routing.yaml `claude.bash_allow`)
              "Bash(git clone *)", "Bash(git -C * pull)", "Bash(git -C * checkout *)", "Bash(git submodule *)", "Bash(curl *)", "Bash(curl.exe *)",
              "Bash(tar *)", "Bash(unzip *)", "Bash(7z *)", "Bash(winget *)", "Bash(uv *)", "Bash(ren *)", "Bash(xcopy *)", "Bash(robocopy *)",
              "Bash(make *)", "Bash(cargo *)", "Bash(go *)", "Bash(dotnet *)", "Bash(npx *)", "Bash(pnpm *)", "Bash(yarn *)"]
GIT_DENY = ["Bash(git push *)", "Bash(git reset *)", "Bash(git checkout *)", "Bash(git rebase *)", "Bash(git commit *)"]


def routing(layout: Layout) -> dict:
    cfg = layout.config / "routing.yaml"
    return yaml.safe_load(cfg.read_text(encoding="utf-8")) or {} if cfg.exists() else {}


def cost_category(task) -> str:
    cost = getattr(task, "cost_category", "")
    return str(getattr(cost, "value", cost) or "")


def model_limits(adapter, spec: dict, handle: dict):
    """ProcessAdapter.limits for a model lane. A job prepared before 1.7.0 carries no `idle_s` and a fixed 3600 s
    deadline: it is moved onto the current policy (routing.yaml `timeouts`) instead of being killed at the old
    hour — unless its spec set a different cap on purpose (a chat turn's 600 s)."""
    deadline, idle_s, idle_cpu_s = ProcessAdapter.limits(adapter, spec, handle)
    if "idle_s" in spec:
        return deadline, idle_s, idle_cpu_s
    try:
        layout = Layout(spec["root"])
        lim = timeouts.resolve(routing(layout), None, role_profile(layout, spec.get("role") or ""), {})
    except Exception:                                    # unreadable config must never kill a running job
        lim = timeouts.resolve({})
    if int(spec.get("timeout_s") or 3600) == 3600:
        started = timeouts._epoch(handle.get("started")) or float(handle.get("create_time") or time.time())
        deadline = max(deadline, started + lim["hard_cap_s"] + sum(int(e.get("seconds") or 0) for e in handle.get("extensions") or []))
    return deadline, lim["idle_s"], lim["idle_cpu_s"]


def role_profile(layout: Layout, role: str) -> dict:
    prof = dict((routing(layout).get("roles") or {}).get(role) or {})
    prof.setdefault("adapter", "claude_cli")
    prof.setdefault("model", "sonnet")
    prof.setdefault("effort", "high")
    # Builders write; strategists research AND write; reviewers/QA read.
    prof.setdefault("tools", "web" if role in ("research_strategist", "senior_pm") else
                    "reader" if role == "code_review" else "writer")
    return prof


def extra_dirs(layout: Layout, task, prof: dict) -> list[str]:
    """Directories the session may work in besides the repository (Claude Code `--add-dir`): the role's `add_dirs`
    and routing.yaml `claude.add_dirs_by_cost.<cost_category>` — e.g. a tool installation outside the repo that a
    GPU task must install into and start. Without them the CLI denies even `ls` there and the worker can only end
    BLOCKED. Relative entries resolve against the project root; nothing is added by default."""
    dirs = [str(layout.root)]
    cost = getattr(task, "cost_category", "")
    cost = str(getattr(cost, "value", cost) or "")
    by_cost = ((routing(layout).get("claude") or {}).get("add_dirs_by_cost") or {}).get(cost) or []
    for d in list(prof.get("add_dirs") or []) + list(by_cost):
        d = str(d or "")
        if not d:
            continue
        p = Path(d)
        p = p if p.is_absolute() or (len(d) > 1 and d[1] == ":") else layout.root / p
        if str(p) not in dirs:
            dirs.append(str(p))
    return dirs


def write_scope(layout: Layout, run_dir: Path, requested) -> list[str] | None:
    """Permission-rule bodies for the writing tools, or None for 'the whole workspace'. `run_dir` is the sentinel
    the review lane uses (supervisor.review): only this attempt's run directory is writable. Claude Code matches a
    tool's path argument literally against the rule, and Write/Edit receive an absolute path on this platform — a
    bare cwd-relative gitignore pattern (`runs/T/T.a1/**`) never matches that, so the confined lane's own HANDOFF.md
    write was silently denied in practice. Emit both forms: the relative pattern
    for parity with the documented gitignore syntax, and the absolute `//`-rooted form the CLI actually resolves
    the tool's argument against."""
    if not requested:
        return None
    if requested != "run_dir":
        return [str(requested)]
    try:
        rel = Path(run_dir).resolve().relative_to(Path(layout.root).resolve()).as_posix() + "/**"
    except ValueError:
        return None                         # a run dir outside the repo cannot be expressed as a rule; deny instead
    abs_rule = "//" + Path(run_dir).resolve().as_posix() + "/**"
    return [rel, abs_rule]


def executable(layout: Layout) -> str:
    exe = (routing(layout).get("claude") or {}).get("executable") if routing(layout) else None
    if exe and Path(exe).exists():
        return exe
    found = shutil.which("claude") or shutil.which("claude.exe")
    if found:
        return found
    home = Path.home() / ".local" / "bin" / "claude.exe"
    return str(home) if home.exists() else "claude"


class ClaudeCliAdapter(ProcessAdapter):
    name = "claude_cli"
    engine = "claude"
    handoff_before_cap = True
    limits = model_limits

    def prepare(self, layout, task, attempt_id):
        job = job_spec(task)
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        role = job.get("role") or task.owner_role
        prof = role_profile(layout, role)
        prof.update({k: v for k, v in job.items() if k in ("model", "effort", "tools")})
        prompt_file = run_dir / "prompt.md"
        if job.get("prompt_file"):
            prompt_text = Path(layout.root / job["prompt_file"]).read_text(encoding="utf-8")
        else:
            prompt_text = job.get("prompt") or self.default_brief(layout, task, run_dir)
        from .. import packets as packets_mod
        prompt_text = packets_mod.worker_prompt(task, job, prompt_text)     # owner/diagnosis guidance, any prompt source
        prompt_text += "\n\n" + handoff_instructions(run_dir, prior=prior_handoff(layout, task.id, attempt_id))
        if job.get("result_kind", "delivery") != "conversation":   # a one-shot chat turn has nothing to check between steps
            from .. import conversations as conversations_mod
            note = conversations_mod.owner_note_instructions(layout, run_dir)
            if note:
                prompt_text += "\n\n" + note
        policy.assert_packet_clean(prompt_text, layout, f"{self.name} prompt {task.id}/{attempt_id}")
        prompt_file.write_text(prompt_text, encoding="utf-8")
        tools = prof["tools"] if isinstance(prof["tools"], list) else DEFAULT_TOOLS.get(prof["tools"], DEFAULT_TOOLS["reader"])
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir), "root": str(layout.root),
                "role": role, "model": prof["model"], "effort": prof["effort"], "tools": tools,
                "write_scope": write_scope(layout, run_dir, job.get("write_scope")),
                "write_confined": bool(job.get("write_scope")),
                "session_id": str(uuid.uuid4()), "stdin_file": str(prompt_file),
                **timeouts.spec_fields(timeouts.resolve(routing(layout), cost_category(task), prof, job)),
                "executable": job.get("executable") or executable(layout), "result_kind": job.get("result_kind", "delivery"),
                "add_dirs": job.get("add_dirs") or extra_dirs(layout, task, prof),
                "bash_allow": list((routing(layout).get("claude") or {}).get("bash_allow") or []),
                "effort_flag": bool((routing(layout).get("claude") or {}).get("supports_effort", True))}

    def default_brief(self, layout, task, run_dir) -> str:
        from .. import packets
        return packets.delivery_brief(layout, task)

    def command(self, spec):
        """`dontAsk` auto-denies anything that would prompt, so every tool the role may use is pre-allowed explicitly
        (Edit/Write/WebFetch prompt by default); Bash only through the BASH_ALLOW patterns, never git mutations."""
        effort = ["--effort", spec["effort"]] if spec.get("effort_flag", True) else []
        if spec.get("resume"):
            return [spec["executable"], "-p", "--resume", spec["resume"], "--model", spec["model"], "--tools", "",
                    "--strict-mcp-config", "--permission-mode", "dontAsk", "--output-format", "stream-json", "--verbose"]
        argv = [spec["executable"], "-p", "--model", spec["model"], *effort, "--tools", ",".join(spec["tools"]),
                "--strict-mcp-config", "--permission-mode", "dontAsk",
                "--output-format", "stream-json", "--verbose", "--session-id", spec["session_id"]]
        allow = [t for t in spec["tools"] if t != "Bash" and not (spec.get("write_confined") and t in WRITE_TOOLS)]
        if spec.get("write_confined") and spec.get("write_scope"):      # review lane: writes only inside the scope
            # write_scope() has returned a list since chief_architect round 3 #1, but a job record prepared before
            # that fix and relaunched after it (a crash-recovery continuation) still carries the old single string
            # on disk; iterating a string yields one rule per character, silently stripping all write capability
            # (fails closed, no security impact, but an undetected capability loss nonetheless).
            scopes = spec["write_scope"] if isinstance(spec["write_scope"], list) else [spec["write_scope"]]
            allow += [f"{t}({scope})" for t in spec["tools"] if t in WRITE_TOOLS for scope in scopes]
        allow += (BASH_ALLOW + [str(x) for x in (spec.get("bash_allow") or [])]) if "Bash" in spec["tools"] else []
        if allow:
            argv += ["--allowedTools", *allow]
        if "Bash" in spec["tools"]:
            argv += ["--disallowedTools", *GIT_DENY]
        for d in spec.get("add_dirs", []):
            argv += ["--add-dir", d]
        return argv

    def env(self, spec):
        env = scrub_env(dict(os.environ))                 # subscription login, never API keys; never a nested session
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        env["SUPERVISOR_ATTEMPT"] = str(spec.get("attempt_id", ""))
        return env

    def submit(self, spec):
        capacity.require(Layout(spec["root"]), self.engine)   # CapacityDeferred costs no attempt (loop handles it)
        return super().submit(spec)

    # ------------------------------------------------------------ live stream + parsing

    def ingest_live(self, spec) -> None:
        """Feed rate_limit_events while the child runs (offset persisted per run dir); harmless to call repeatedly."""
        run_dir = Path(spec["run_dir"])
        stream = run_dir / "stdout.jsonl"
        if not stream.exists():
            return
        off_file = run_dir / "stream_offset.json"
        try:
            offset = int(json.loads(off_file.read_text(encoding="utf-8")).get("offset", 0)) if off_file.exists() else 0
        except (OSError, ValueError):
            offset = 0
        try:
            new = capacity.ingest_claude_stream(Layout(spec["root"]), stream, offset)
        except OSError:
            return
        if new != offset:
            atomic_json(off_file, {"offset": new})

    def collect(self, spec, handle):
        if self.alive(handle):
            self.ingest_live(spec)
        return super().collect(spec, handle)

    def on_timeout(self, spec, handle) -> None:
        self.ingest_live(spec)

    @staticmethod
    def events(run_dir: Path) -> list[dict]:
        out = []
        p = run_dir / "stdout.jsonl"
        if not p.exists():
            return out
        for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def parse_result(self, spec, handle) -> Result:
        run_dir = Path(spec["run_dir"])
        layout = Layout(spec["root"])
        self.ingest_live(spec)
        evs = self.events(run_dir)
        final = next((e for e in reversed(evs) if e.get("type") == "result"), None)
        stderr = (run_dir / "stderr.txt").read_text(encoding="utf-8", errors="replace") if (run_dir / "stderr.txt").exists() else ""
        # Diagnostics are matched ONLY against stderr, system/error events and an is_error result — never against the
        # model's ordinary answer (a runbook that mentions "/login" must not fail the task).  Review #5.
        diag = stderr + "\n" + "\n".join(json.dumps(e) for e in evs if e.get("type") in ("system", "error"))
        if final is None:
            if AUTH_FAILURE_RE.search(diag):
                return Result(status="FAILED", error="login_required: run `claude /login` on the workstation", raw={"stderr": stderr[-2000:]})
            if LIMIT_RE.search(diag):
                return limit_result(layout, self.engine, diag[-300:], raw={"stderr": stderr[-2000:]})
            return Result(status="FAILED", error="no result event in stream", raw={"stderr": stderr[-2000:]})
        text = final.get("result") or ""
        denials = final.get("permission_denials") or []
        meta = {"session_id": final.get("session_id"), "model_usage": final.get("modelUsage"), "permission_denials": denials,
                "is_error": final.get("is_error", False), "duration_ms": final.get("duration_ms")}
        atomic_json(run_dir / "result_meta.json", meta)
        (run_dir / "result.txt").write_text(text, encoding="utf-8")
        if final.get("is_error"):
            diag += "\n" + text
            if final.get("subtype") == "error_max_turns":
                return Result(status="FAILED", error="max turns reached", session_id=meta["session_id"], raw=meta)
            if LIMIT_RE.search(diag):                     # limits arrive as an is_error result; a limit is not a failure
                return limit_result(layout, self.engine, text[-300:], session_id=meta["session_id"], raw=meta)
            if AUTH_FAILURE_RE.search(diag):
                return Result(status="FAILED", error="login_required: run `claude /login` on the workstation",
                              session_id=meta["session_id"], raw=meta)
            return Result(status="FAILED", error=f"CLI error: {text[:300]}", session_id=meta["session_id"],
                          usage=meta["model_usage"] or {}, raw=meta)
        try:
            obj = structured_result(text, spec.get("result_kind", "delivery"))
        except ValueError:
            obj = self.follow_up(spec, meta["session_id"])
            if obj is None:
                return Result(status="FAILED", error="no structured result after follow-up", session_id=meta["session_id"],
                              usage=meta["model_usage"] or {}, raw=meta)
        status = classify_status(obj)
        notes = str(obj.get("notes") or obj.get("summary") or "")
        error = None
        if denials:                                        # visible, never fatal: the artifact check and reviews judge the work
            summary = f"{len(denials)} tool permission denial(s): " + \
                ", ".join(sorted({str(d.get("tool_name", "?")) for d in denials if isinstance(d, dict)}))[:200]
            notes = (notes + "\n\n[" + summary + "]").strip()
            obj["permission_denials"] = [str((d.get("tool_input") or {}).get("command", d.get("tool_name")))[:160]
                                         for d in denials if isinstance(d, dict)][:20]
        atomic_json(run_dir / "result.json", obj)
        return Result(status=status, artifacts=list(obj.get("artifacts") or []), notes=notes, error=error,
                      model=spec.get("model"), session_id=meta["session_id"], usage=meta["model_usage"] or {}, raw=obj)

    def follow_up(self, spec, session_id):
        """One tool-less resume asking only for the JSON block."""
        if not session_id:
            return None
        run_dir = Path(spec["run_dir"])
        out_file = run_dir / "followup_stdout.jsonl"
        if out_file.exists():                              # exactly one follow-up per attempt, crash-safe
            return self._parse_follow_up(spec, out_file)
        ask = run_dir / "followup_prompt.md"
        ask.write_text(f"Your previous answer lacked the required fenced JSON `{spec.get('result_kind', 'delivery')}` object "
                       "(schema_version 1). Reply with ONLY that JSON block describing what you actually completed.", encoding="utf-8")
        argv = self.command({**spec, "resume": session_id})
        tmp = run_dir / "followup_stdout.partial"
        try:
            with open(ask, "rb") as stdin, open(tmp, "wb") as out:
                subprocess.run(argv, stdin=stdin, stdout=out, stderr=subprocess.DEVNULL, cwd=spec["root"], env=self.env(spec),
                               timeout=FOLLOW_UP_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            tmp.replace(out_file)                          # marker: the follow-up happened (even if it failed)
        except OSError:
            return None
        return self._parse_follow_up(spec, out_file)

    @staticmethod
    def _parse_follow_up(spec, out_file: Path):
        for line in reversed(out_file.read_text(encoding="utf-8-sig", errors="replace").splitlines()):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "result":
                try:
                    return structured_result(e.get("result") or "", spec.get("result_kind", "delivery"))
                except ValueError:
                    return None
        return None


register(ClaudeCliAdapter())
