"""`supervisor doctor`: read-only health checks of a project and the tools its lanes need. Never installs or starts
anything; a FAIL says what to do. Extensions add their own checks through `registry.doctor_check`."""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

import yaml

from . import __version__, extensions, project
from .paths import Layout


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False        # ok, but needs attention: shown as WARN, never fails doctor


def _version_of(exe: str | None, args=("--version",)) -> str | None:
    if not exe:
        return None
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or out.stderr or "").strip().splitlines()
    return text[0][:80] if text else "ok"


def check_project(layout: Layout) -> Check:
    if not project.exists(layout):
        return Check("project", False, f"{layout.project_file} missing; run `supervisor init {layout.root}`")
    doc = project.load(layout)
    note = project.version_mismatch(layout)
    if note:
        return Check("project", False, f"{note}; root {layout.root}")
    # the root is part of the detail: a rehearsal on a copy must be able to show which tree doctor examined
    return Check("project", True, f"{doc.get('name')} at {layout.root} (framework {__version__})")


def check_framework(layout: Layout) -> Check:
    """Which framework code is running. An editable install tracks its checkout's working tree, so a project may only
    run from a checkout that is exactly at a tag and clean: a dirty or untagged checkout means the running code is
    something no review accepted and no CHANGELOG names, and every rehearsal or cutover note that records "framework
    commit X" would be wrong. An installed package (no git checkout) reports its version only."""
    from . import updates
    rt = updates.runtime_checkout()
    if not rt["is_git"]:
        return Check("framework", True, f"{rt['version']} (installed package, {rt['path']})")
    where = f"{rt['version']} @ {rt['commit'] or '?'}" + (f" ({rt['tag']})" if rt["tag"] else "") + f", {rt['path']}"
    problems = []
    if rt.get("status_error"):
        problems.append(f"its state could not be verified ({rt['status_error']})")
    elif rt["dirty"]:
        untracked = rt.get("untracked") or []
        problems.append("the checkout has uncommitted changes"
                        + (f" including untracked files ({', '.join(untracked[:5])}{'…' if len(untracked) > 5 else ''})"
                           if untracked else ""))
    if not rt["tag"]:
        problems.append("HEAD is not at a tag")
    if problems:
        return Check("framework", False, where + " — " + "; ".join(problems)
                     + ". Run the project from a tagged, clean checkout (git checkout <tag>), never a working tree being edited")
    return Check("framework", True, where)


def check_python(layout: Layout) -> Check:
    v = sys.version.split()[0]
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 11):
        return Check("python", False, f"python {v}: 3.11+ required")
    return Check("python", True, f"python {v} ({sys.executable})")


def check_state(layout: Layout) -> Check:
    from . import tasks as tasks_mod
    if not layout.state.is_dir():
        return Check("state", False, "state/ missing; run `supervisor init`")
    try:
        tasks = tasks_mod.load_all(layout)
        errors = tasks_mod.validate_graph(tasks)
    except Exception as exc:                                # noqa: BLE001 - report, never crash doctor
        return Check("state", False, f"task store unreadable: {exc}"[:300])
    if errors:
        return Check("state", False, "; ".join(errors)[:300])
    return Check("state", True, f"{len(tasks)} task(s), graph valid")


def check_jobs(layout: Layout) -> Check:
    """No task's newest job record is a reroute whose successor was never queued (a crash between closing the
    refused record and dispatching the new one). The loop repairs that on its next iteration; a framework older
    than 1.10.0 cannot, so this must be OK before a downgrade (CHANGELOG 1.10.0, Rollback)."""
    from . import jobs as jobs_mod
    try:
        task_ids = {r.get("task_id") for r in jobs_mod.all_records(layout) if r.get("result") == "REROUTED"}
        pending = []
        for task_id in sorted(t for t in task_ids if t):
            recs = jobs_mod.for_task(layout, task_id)
            last = recs[-1] if recs else {}
            if last.get("result") == "REROUTED" and not last.get("continued"):
                pending.append(last.get("attempt_id", task_id))
    except (OSError, ValueError) as exc:
        return Check("jobs", False, f"job records unreadable: {exc}"[:300])
    if pending:
        return Check("jobs", False, "reroute not yet re-queued: " + ", ".join(pending)[:200]
                     + "; run the loop once (a drain will do) so it queues them")
    return Check("jobs", True, "no interrupted reroute")


def check_routing(layout: Layout) -> Check:
    cfg = layout.config / "routing.yaml"
    if not cfg.exists():
        return Check("routing", False, "config/routing.yaml missing")
    try:
        doc = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return Check("routing", False, f"config/routing.yaml malformed: {exc}"[:200])
    roles = doc.get("roles") or {}
    missing = [r for r, p in roles.items() if (p or {}).get("card") and not (layout.agents / p["card"]).exists()]
    if missing:
        return Check("routing", False, "role cards missing: " + ", ".join(missing))
    return Check("routing", True, f"{len(roles)} role(s) configured")


def check_git(layout: Layout) -> Check:
    exe = shutil.which("git")
    if not exe:
        return Check("git", False, "git not on PATH")
    try:
        out = subprocess.run([exe, "-C", str(layout.root), "rev-parse", "--is-inside-work-tree"], capture_output=True,
                             text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("git", False, f"git failed: {exc}")
    if out.returncode != 0:
        return Check("git", False, f"{layout.root} is not a git repository (checkpoints need one): `git init`")
    return Check("git", True, _version_of(exe) or "ok")


def _lane_check(name: str, exe_names: tuple[str, ...], login_hint: str):
    def fn(layout: Layout) -> Check:
        from .adapters import REGISTRY
        if name not in REGISTRY:
            return Check(name, True, "adapter not registered in this process")
        cfg = layout.config / "routing.yaml"
        used = False
        if cfg.exists():
            doc = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            used = any((p or {}).get("adapter") == name for p in (doc.get("roles") or {}).values())
        exe = None
        if name == "claude_cli":
            from .adapters.claude_cli import executable
            exe = executable(layout)
            exe = exe if shutil.which(exe) or (exe and __import__("pathlib").Path(exe).exists()) else None
        else:
            exe = next((shutil.which(n) for n in exe_names if shutil.which(n)), None)
        if not exe:
            return Check(name, not used, ("no role uses it; " if not used else "") + f"executable not found ({', '.join(exe_names)}); {login_hint}")
        return Check(name, True, f"{exe} — {_version_of(exe) or 'present'}")
    fn.__name__ = f"check_{name}"
    return fn


check_claude_cli = _lane_check("claude_cli", ("claude", "claude.exe"), "install Claude Code and run `claude /login`")
check_codex_cli = _lane_check("codex_cli", ("codex", "codex.exe"), "install the Codex CLI and sign in")


def check_extensions(layout: Layout) -> Check:
    reg = extensions.load(layout)
    if reg.blocking:
        return Check("extensions", False, ("LOOP BLOCKED until fixed (or `required: false` in project.yaml): "
                                           + "; ".join(reg.errors))[:300])
    if reg.errors:
        return Check("extensions", False, ("optional extension(s) not loaded: " + "; ".join(reg.errors))[:300])
    return Check("extensions", True, ", ".join(reg.loaded) or "none configured")


def check_extension_imports(layout: Layout) -> Check:
    """Gate G6, deprecation window: extensions import supervisor.ext only (docs/EXTENSION_API.md). An import of a
    framework internal is a WARN naming file and line; a later major release makes it a FAIL."""
    found = extensions.load(layout).internal_imports
    if not found:
        return Check("extension_imports", True, "extensions import supervisor.ext only")
    shown = "; ".join(f"{f.file}:{f.line} {f.module}" for f in found[:8])
    more = f" (+{len(found) - 8} more)" if len(found) > 8 else ""
    return Check("extension_imports", True, f"{len(found)} import(s) of framework internals, deprecated since 1.11.0 "
                 f"(import supervisor.ext; a later major release refuses them): {shown}{more}", warn=True)


def check_policy(layout: Layout) -> Check:
    """config/policy.yaml entries the floor rejected (supervisor.policy.config_problems): each one is a value the
    owner wrote that is NOT in effect."""
    from . import policy
    try:
        problems = policy.config_problems(layout)
    except Exception as exc:                                # noqa: BLE001 - report, never crash doctor
        return Check("policy", False, f"policy unreadable: {exc}"[:300])
    if problems:
        return Check("policy", False, "; ".join(problems)[:300])
    return Check("policy", True, f"{policy.CONFIG_NAME} ok (built-in floor" + (" + project values)" if (layout.config / policy.CONFIG_NAME).exists() else " only)"))


CHECKS = [check_project, check_framework, check_python, check_state, check_jobs, check_routing, check_policy,
          check_git, check_claude_cli, check_codex_cli, check_extensions, check_extension_imports]


def run(layout: Layout) -> list[Check]:
    out = [fn(layout) for fn in CHECKS]
    for fn in extensions.load(layout).doctor_checks:
        try:
            out.append(fn(layout))
        except Exception as exc:                            # noqa: BLE001
            out.append(Check(getattr(fn, "__name__", "extension"), False, f"check crashed: {exc}"[:200]))
    return out
