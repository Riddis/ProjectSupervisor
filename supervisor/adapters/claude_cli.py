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
from typing import Optional

from .. import capacity
from .. import handoff
from .. import jobs as jobs_mod
from .. import policy
from .. import progress as progress_mod
from .. import routing_config
from .. import tasks as tasks_mod
from .. import timeouts
from .._vendor.contract import atomic_json, structured_result
from .._vendor.handoff import instructions as handoff_instructions
from ..paths import Layout
from .base import Result, ReviewLaneKeys, register, register_job_schema
from .process import ProcessAdapter, job_identity_env, job_spec, scrub_env


class ClaudeCliJobExtra(ReviewLaneKeys):
    """`job` keys this lane reads beyond the framework's typed `Job` fields (docs/TASK_CONTRACT.md), plus the
    review lane's own (`ReviewLaneKeys`): this lane confines through `write_scope` and ignores `sandbox`."""
    role: Optional[str] = None
    prompt_file: Optional[str] = None
    executable: Optional[str] = None
    add_dirs: Optional[list[str]] = None


register_job_schema("claude_cli", ClaudeCliJobExtra)

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


def limit_result(layout: Layout, engine: str, message: str, raw=None) -> Result:
    """A CLI-reported usage limit: record a conservative synthetic window unless a measured one already gates, then DEFERRED."""
    from .._vendor import usage_policy as up
    capacity.ensure(layout)
    until = up.reserve_until(engine)
    measured = until is not None
    if until is None:
        up.record(engine, [{"name": "cli_limit", "used_percent": 100, "reset_epoch": time.time() + FALLBACK_LIMIT_S}], message[:200])
        until = up.reserve_until(engine) or time.time() + FALLBACK_LIMIT_S
    return Result(status="DEFERRED", error=f"usage limit reported by the CLI: {message[:200]}",
                  raw={**(raw or {}), "measured_window": measured}, reset_epoch=until)


_NOTE_KINDS = (("review:", "review"), ("diagnosis:", "diagnosis"), ("verify delivery", "verify"))


def _kind_from_note(note: str) -> str:
    """The kind an attempt note alone suggests — the fallback of `_entry_kind`, and never better than a guess:
    a note says what caused a job, not what kind of job it is."""
    for prefix, kind in _NOTE_KINDS:
        if note.startswith(prefix):
            return kind
    return "delivery"


def _entry_kind(layout: Layout, entry) -> str:
    """One attempt-list entry's job kind, taken from its own durable job record: `jobs.kind_of` reads the
    promoted `kind` field, else the `spec["kind"]` that `loop.dispatch` writes on every dispatch, and nothing
    ever deletes a job record. The note decides only for an entry whose record is missing or names no kind at
    all (a store written before the field existed), because a note cannot tell the kinds apart: a review's and a
    diagnosis's own continuation is dispatched with the same "continuation after <reason>" note a delivery's
    is, and a rerouted job of any kind with the same "rerouted from <lane>"."""
    rec = jobs_mod.resolve(layout, entry.id)
    if rec is not None and (rec.get("kind") or (rec.get("spec") or {}).get("kind")):
        return jobs_mod.kind_of(rec)
    return _kind_from_note(entry.note or "")


def _kinds(layout: Layout, entries) -> list[str]:
    return [_entry_kind(layout, e) for e in entries]


def _delivery_identity(entries, kinds, idx: int) -> tuple[int, int]:
    """(attempt, seq) for the delivery entry at `idx`, counted from `entries` (every job this task has ever had,
    in dispatch order) and their `kinds`: an entry of another kind does not affect the count at all; among the
    delivery entries, a 'continuation after' note joins the open attempt as its next session and a plain
    dispatch or a revision opens the next one — within one kind the note is unambiguous, which is why the count
    reads it only after the kind is settled. This is the live-store reading of the mapping rule the
    attempt-identity design's migration (§8.2) will apply formally once the writer switch allocates these fields
    directly — nothing here persists a number, it is recomputed from the store on every call."""
    attempt = seq = 0
    for e, kind in zip(entries[:idx + 1], kinds[:idx + 1]):
        if kind != "delivery":
            continue
        if attempt and (e.note or "").startswith("continuation after"):
            seq += 1
        else:
            attempt += 1
            seq = 1
    return attempt, seq


def _last_delivery_index(kinds, idx: int) -> int | None:
    """The index of the most recent delivery entry before `idx` (skipping every other kind), or None."""
    for j in range(idx - 1, -1, -1):
        if kinds[j] == "delivery":
            return j
    return None


def _produced_delivery(layout: Layout, entries, kinds, delivery_idx: int, idx: int) -> bool:
    """Whether the delivery entry at `delivery_idx` ever passed verification, looking only at the entries
    between it and `idx` (exclusive): its own verification, or any review of this task — `tasks._evaluate` only
    ever moves a task to WAITING_FOR_REVIEW once verification has already said ok for the current digest, so a
    review entry is proof by itself. A task that declares no test commands is verified the same tick its worker
    claims COMPLETE (verify.verify() returns ok with nothing to track) and so gains no verification entry of its
    own; the review is then the only record in the store that such a delivery ever happened (§4.1 of the
    attempt-identity design). A verification is tied to this session by the verify job's own `verify_delivery`
    field, by its note when its record is missing."""
    session_id = entries[delivery_idx].id
    for e, kind in zip(entries[delivery_idx + 1:idx], kinds[delivery_idx + 1:idx]):
        if kind == "review":
            return True
        if kind == "verify":
            rec = jobs_mod.resolve(layout, e.id)
            if rec is not None and rec.get("verify_delivery") == session_id:
                return True
            if rec is None and (e.note or "") == f"verify delivery {session_id}":
                return True
    return False


def _new_shape_identity(entries, attempt_id: str) -> tuple[int, str, int, str | None] | None:
    """(attempt, kind, seq, parent) for `attempt_id`, when the task's own `attempts[]` already carries it in the
    allocator-written shape (a numbered attempt's own `jobs[]`, written by
    `tasks.allocate_job` before `prepare()` ever runs) — the direct read every new job gets, no reconstruction
    needed because the fields were never derived from a note or a suffix in the first place. `None` when
    `attempt_id` is not there (a legacy entry from before the writer switch, §6's "readers first, writers
    second": the old per-job-entry shape below is still what such an id resolves against)."""
    for a in entries:
        if a.n is None:
            continue
        for j in a.jobs or []:
            if j.get("id") == attempt_id:
                return a.n, j.get("kind"), j.get("seq"), j.get("parent")
    return None


def resolve_handoff(layout: Layout, task_id: str, attempt_id: str, kind: str = "delivery"):
    """(attempt, seq, parent) for `attempt_id`, then `handoff.select`'s answer for them. A job the allocator
    wrote (§2.7) is read straight off its own `attempts[]` entry (`_new_shape_identity`); only a legacy job
    dispatched before the writer switch falls through to the task's older, flat attempt history —
    `tasks.add_attempt` wrote that job's own entry before `prepare()` ran, so a fresh load still sees it. Review
    and diagnosis jobs never reach the delivery branch below; a diagnosis's parent is simply the entry
    immediately before its own. A kind that is neither delivery nor diagnosis is given nothing either way
    (`handoff.select` returns `None` for it regardless of attempt/seq/parent) — checked before the load below,
    not after, because a conversation's `task_id` names a chat thread, never a Task document on disk."""
    if kind not in ("delivery", "diagnosis"):
        return handoff.select(layout, task_id, None, kind, None, None)
    entries = tasks_mod.load(layout, task_id).attempts
    new_shape = _new_shape_identity(entries, attempt_id)
    if new_shape is not None:
        attempt, job_kind, seq, parent = new_shape
        return handoff.select(layout, task_id, attempt, job_kind or kind, seq, parent)
    entries = [e for e in entries if e.n is None]      # the old reconstruction only ever walked flat, per-job entries
    idx = next((i for i in range(len(entries) - 1, -1, -1) if entries[i].id == attempt_id), None)
    if idx is None:
        return handoff.select(layout, task_id, None, kind, None, None)
    if kind == "diagnosis":
        parent = entries[idx - 1].id if idx > 0 else None
        return handoff.select(layout, task_id, None, kind, None, parent)
    if kind != "delivery":
        return handoff.select(layout, task_id, None, kind, None, None)
    kinds = _kinds(layout, entries)
    kinds[idx] = kind         # this job's own kind comes from its brief: its record is written after prepare()
    attempt, seq = _delivery_identity(entries, kinds, idx)
    prior_idx = _last_delivery_index(kinds, idx)
    if seq > 1:
        return handoff.select(layout, task_id, attempt, kind, seq, entries[prior_idx].id if prior_idx is not None else None)
    parent = None
    if attempt > 1 and prior_idx is not None and not _produced_delivery(layout, entries, kinds, prior_idx, idx):
        parent = entries[prior_idx].id
    return handoff.select(layout, task_id, attempt, kind, seq, parent)
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
    """config/routing.yaml as a mapping; delegates to `routing_config.load` (R-28: one routing loader)."""
    return routing_config.load(layout)


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
        rk = job.get("result_kind", "delivery")
        kind = rk if rk in packets_mod.NON_DELIVERY_KINDS else "delivery"
        prompt_text += "\n\n" + handoff_instructions(run_dir, prior=resolve_handoff(layout, task.id, attempt_id, kind))
        if job.get("result_kind", "delivery") != "conversation":   # a one-shot chat turn has nothing to check between steps
            from .. import conversations as conversations_mod
            note = conversations_mod.owner_note_instructions(layout, run_dir)
            if note:
                prompt_text += "\n\n" + note
        if job.get("result_kind", "delivery") == "delivery":     # only work that can run long reports progress
            prompt_text += "\n\n" + progress_mod.instructions(run_dir)
        policy.assert_packet_clean(prompt_text, layout, f"{self.name} prompt {task.id}/{attempt_id}",
                                   job=job, task=task, lane=self.name)      # hosted lane: screens see the job too
        prompt_file.write_text(prompt_text, encoding="utf-8")
        tools = prof["tools"] if isinstance(prof["tools"], list) else DEFAULT_TOOLS.get(prof["tools"], DEFAULT_TOOLS["reader"])
        # This lane resolves its budget through the one shared path, so a
        # declared duration derives a cap here exactly as it does on the process lane -- the recorded loss was an
        # agent session killed at the policy cap -- and a declared timeout_s the ceiling lowers leaves a receipt.
        limits, _ = self._resolve_limits(layout, task, attempt_id, job, routing(layout))
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir), "root": str(layout.root),
                "role": role, "model": prof["model"], "effort": prof["effort"], "tools": tools,
                "write_scope": write_scope(layout, run_dir, job.get("write_scope")),
                "write_confined": bool(job.get("write_scope")),
                "session_id": str(uuid.uuid4()), "stdin_file": str(prompt_file),
                **timeouts.spec_fields(limits),
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
        env.update(job_identity_env(spec))
        env.update(progress_mod.env(spec))                    # scripts the session starts report progress (supervisor.progress)
        return env

    def agent_handle(self, layout: Layout) -> dict:
        """For supervisor.plugin.agent_lane: what a plugin needs to launch this agent itself, under this lane's
        environment rules (the executable, the configured model, a callable returning the scrubbed environment)."""
        def environment() -> dict:
            env = scrub_env(dict(os.environ), layout)
            env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
            return env
        return {"executable": executable(layout), "model": (routing(layout).get(self.engine) or {}).get("model"),
                "environment": environment}

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
                return Result(status="FAILED", error="max turns reached", raw=meta)
            if LIMIT_RE.search(diag):                     # limits arrive as an is_error result; a limit is not a failure
                return limit_result(layout, self.engine, text[-300:], raw=meta)
            if AUTH_FAILURE_RE.search(diag):
                return Result(status="FAILED", error="login_required: run `claude /login` on the workstation",
                              raw=meta)
            return Result(status="FAILED", error=f"CLI error: {text[:300]}", raw=meta)
        try:
            obj = structured_result(text, spec.get("result_kind", "delivery"))
        except ValueError as exc:
            obj = self.follow_up(spec, meta["session_id"], str(exc))
            if obj is None:
                return Result(status="FAILED", error="no structured result after follow-up", raw=meta)
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
        return Result(status=status, artifacts=list(obj.get("artifacts") or []), notes=notes, error=error, raw=obj)

    def follow_up(self, spec, session_id, error: str = ""):
        """One tool-less resume asking only for the JSON block. `error` is the contract's own description of what
        was wrong with the first answer (structured_result's ValueError), quoted so the model fixes that and keeps
        the rest — a bare "lacked a JSON block" made reviewers rewrite their verdict from scratch."""
        if not session_id:
            return None
        run_dir = Path(spec["run_dir"])
        out_file = run_dir / "followup_stdout.jsonl"
        if out_file.exists():                              # exactly one follow-up per attempt, crash-safe
            return self._parse_follow_up(spec, out_file)
        ask = run_dir / "followup_prompt.md"
        why = (f" The supervisor could not accept it: {error[:600]}." if error else "")
        ask.write_text(f"Your previous answer lacked a valid fenced JSON `{spec.get('result_kind', 'delivery')}` object "
                       f"(schema_version 1).{why} Reply with ONLY the corrected JSON block describing what you actually "
                       "completed — keep your verdict and findings, fix only what the error names.", encoding="utf-8")
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
