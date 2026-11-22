"""`supervisor doctor`: read-only health checks of a project and the tools its lanes need. Never installs or starts
anything; a FAIL says what to do. Plugins add their own checks through `registry.doctor_check`."""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

import yaml

from . import __version__, plugins, project, routing_config
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
    detail = f"{doc.get('name')} at {layout.root} (framework {__version__})"
    removed = project.removed_settings(layout)
    if removed:
        return Check("project", True, detail + " — " + "; ".join(removed), warn=True)
    return Check("project", True, detail)


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
        problems.append("HEAD is not at a tag (a framework release, vX.Y.Z)")
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
        doc = routing_config.read_raw(cfg)
    except (OSError, yaml.YAMLError) as exc:
        return Check("routing", False, f"config/routing.yaml malformed: {exc}"[:200])
    roles = doc.get("roles") or {}
    missing = [r for r, p in roles.items() if (p or {}).get("card") and not (layout.agents / p["card"]).exists()]
    if missing:
        return Check("routing", False, "role cards missing: " + ", ".join(missing))
    plan_cfg = doc.get("plan") or {}
    approvers = [str(r) for r in (plan_cfg.get("approver") or [])] if isinstance(plan_cfg, dict) else []
    unconfigured = [r for r in approvers if roles and r not in roles]
    warnings = []
    if unconfigured:
        warnings.append(f"plan.approver names {', '.join(unconfigured)}, absent from roles: — a plan with no "
                        "other approver reaches the owner instead of that role")
    from . import gate as gate_mod
    warnings += gate_mod.removed_routing_settings(layout)
    if warnings:
        return Check("routing", True, f"{len(roles)} role(s) configured; " + "; ".join(warnings), warn=True)
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
        doc = routing_config.load(layout)
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


def check_plugins(layout: Layout) -> Check:
    reg = plugins.load(layout)
    if reg.blocking:
        return Check("plugins", False, ("LOOP BLOCKED until fixed (or `required: false` in project.yaml): "
                                           + "; ".join(reg.errors))[:300])
    if reg.errors:
        return Check("plugins", False, ("optional plugin(s) not loaded: " + "; ".join(reg.errors))[:300])
    return Check("plugins", True, ", ".join(reg.loaded) or "none configured")


def check_plugin_manifests(layout: Layout) -> Check:
    """Gate G6, from the manifests alone (no plugin is loaded, so this holds when loading fails).

    FAIL: a manifest discovery refuses, or a framework range that excludes this framework.

    WARN:
    - a plugin without a manifest: it loads in 1.12.x, and a later major release requires one;
    - configuration still in the old top-level sections, which `supervisor migrate` moves to `plugins.<identity>`
      and `resources.gpu`."""
    from . import discovery
    from .plugin._config import _routing
    specs = plugins.specs(layout)
    if not specs:
        return Check("plugin_manifests", True, "none configured")
    try:
        doc = _routing(layout)
    except Exception:                                       # noqa: BLE001 - check_routing reports an unreadable file
        doc = {}
    owned = doc.get("plugins") if isinstance(doc.get("plugins"), dict) else {}
    fails, legacy, unmoved, listed = [], [], [], []
    for name, _required in specs:
        found = discovery.find(layout, name)
        m = found.manifest
        if found.problems:
            fails.append(f"{name}: {found.problems[0]}")
        elif m is None:
            legacy.append(name)
        elif m.unsupported_reason():
            fails.append(f"{name}: {m.unsupported_reason()}")
        else:
            listed.append(f"{name} {m.version} ({m.framework})")
            if m.config is not None and m.identity not in owned:
                unmoved += [s for s in m.config.legacy_sections if s in doc]
    shared = doc.get("resources") if isinstance(doc.get("resources"), dict) else {}
    if "gpu" in doc and "gpu" not in shared:
        unmoved.append("gpu")
    if fails:
        return Check("plugin_manifests", False, "; ".join(fails)[:300])
    warns = []
    if legacy:
        warns.append(f"without a manifest (supervisor-plugin.toml; a later major release requires one): "
                     f"{', '.join(legacy)}")
    if unmoved:
        warns.append(f"configuration in the old routing.yaml sections {', '.join(unmoved)}: run `supervisor migrate`")
    if warns:
        return Check("plugin_manifests", True, "; ".join(warns + ([", ".join(listed)] if listed else []))[:300],
                     warn=True)
    return Check("plugin_manifests", True, ", ".join(listed))


def check_plugin_imports(layout: Layout) -> Check:
    """Gate G6, deprecation window: plugins import supervisor.plugin only (docs/PLUGIN_API.md). An import of a
    framework internal is a WARN naming file and line; a later major release makes it a FAIL."""
    found = plugins.load(layout).internal_imports
    if not found:
        return Check("plugin_imports", True, "plugins import supervisor.plugin only")
    shown = "; ".join(f"{f.file}:{f.line} {f.module}" for f in found[:8])
    more = f" (+{len(found) - 8} more)" if len(found) > 8 else ""
    return Check("plugin_imports", True, f"{len(found)} import(s) of framework internals, deprecated since 1.11.0 "
                 f"(import supervisor.plugin; a later major release refuses them): {shown}{more}", warn=True)


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


def check_gpu_lease(layout: Layout) -> Check:
    """`resources.gpu.lease` (R4): unconfigured is fine (every project before this release, and every one that
    does not opt in); configured, who holds it now and whether any entry needed pruning."""
    from . import leases
    try:
        info = leases.status(layout, "gpu")
    except Exception as exc:                               # noqa: BLE001 - report, never crash doctor
        return Check("gpu_lease", False, f"resources.gpu.lease unreadable: {exc}"[:300])
    if not info["configured"]:
        return Check("gpu_lease", True, "no lease configured (resources.gpu.lease unset): this project's own "
                     "VRAM preflight is the only guard against another project's GPU work")
    holders = ", ".join(f"{e['label']} (pid {e['pid']})" for e in info["live"]) or "free"
    detail = f"{info['path']}: {holders}"
    if info["stale"]:
        detail += f"; {info['stale']} stale entr{'y' if info['stale'] == 1 else 'ies'} pruned on read"
    if info["waiting"]:
        detail += f"; {info['waiting']} is waiting for it"
    return Check("gpu_lease", True, detail[:300])


def check_resource_claims(layout: Layout) -> Check:
    """Every `resources.<name>` this project declares (docs/plan/LONG_WORK.md section 5): its current holder, an
    open job record carrying `claim: <name>` (§5.2), and its waiter — the same reason `why_not_runnable` would
    report, read here without a task to ask it for. Unconfigured (no `resources:` section, or none with claim
    fields) is fine: every project before this release, and every one that opens no name to a plan. Also names a
    template a plan may select (`may_be_set_by: [plan]`) whose own declared resource is owner-only (§6) — the
    misconfiguration a plan's own refusal (RF-31) cannot see the cause of without this."""
    from . import jobs as jobs_mod
    from . import needs as needs_mod
    from . import tasks as tasks_mod
    from . import templates as templates_mod
    from .models.task import TERMINAL
    declared = needs_mod.resources_declared(layout)
    if not declared:
        return Check("resource_claims", True, "no resources declared (config/routing.yaml resources:)")
    holders: dict[str, str] = {}
    open_task_ids: set[str] = set()
    for r in jobs_mod.open_records(layout):
        open_task_ids.add(r["task_id"])
        claim = r.get("claim")
        if claim and claim not in holders:
            holders[claim] = r["task_id"]
    # The waiter: a non-terminal task with no open record of its own (so it is not itself a holder) whose
    # effective need claims the name -- the same read `why_not_runnable` makes against a holder's open record,
    # here with no task to ask, so a diagnosis only a task page can otherwise show is visible without one.
    waiters: dict[str, str] = {}
    for tid, t in sorted(tasks_mod.load_all(layout).items()):
        if t.status in TERMINAL or tid in open_task_ids:
            continue
        claim = needs_mod.claim_for(layout, t, "delivery")
        if claim and claim not in waiters:
            waiters[claim] = tid
    parts = []
    for name in sorted(declared):
        status = f"held by {holders[name]}" if name in holders else "free"
        if name in waiters:
            status += f", waited on by {waiters[name]}"
        parts.append(f"{name}: {status}")
    try:
        tmpls = templates_mod.load(layout)
    except templates_mod.TemplateError:
        tmpls = {}
    for tid, tmpl in sorted((tmpls or {}).items()):
        if "plan" not in (tmpl.get("may_be_set_by") or []):
            continue
        resource = (tmpl.get("needs") or {}).get("resource")
        if resource and resource in declared and "plan" not in needs_mod.resource_claimants(layout, resource):
            parts.append(f"{tid!r} is may_be_set_by: [plan] but claims {resource!r}, "
                        f"which is not may_be_claimed_by: plan")
    return Check("resource_claims", True, "; ".join(parts)[:300])


CHECKS = [check_project, check_framework, check_python, check_state, check_jobs, check_routing, check_policy,
          check_git, check_claude_cli, check_codex_cli, check_gpu_lease, check_resource_claims, check_plugins,
          check_plugin_manifests, check_plugin_imports]


def run(layout: Layout) -> list[Check]:
    """The framework's checks, then each plugin's. A check registered by a plugin with a manifest must report
    a name its manifest declares in `provides.doctor_checks` (names are known only when a check runs); any other
    name is a FAIL."""
    out = [fn(layout) for fn in CHECKS]
    reg = plugins.load(layout)
    for i, fn in enumerate(reg.doctor_checks):
        try:
            check = fn(layout)
        except Exception as exc:                            # noqa: BLE001
            out.append(Check(getattr(fn, "__name__", "plugin"), False, f"check crashed: {exc}"[:200]))
            continue
        owner = reg.doctor_check_owners[i] if i < len(reg.doctor_check_owners) else None
        m = reg.manifests.get(owner) if owner else None
        if m is not None and isinstance(check, Check) and check.name not in m.provides.get("doctor_checks", ()):
            check = Check(check.name, False, f"plugin {owner!r} reported the check {check.name!r}, which its "
                          f"manifest does not declare in provides.doctor_checks")
        out.append(check)
    return out
