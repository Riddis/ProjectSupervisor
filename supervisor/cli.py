"""`supervisor` command line. All writes go through the library; the Control Center uses the same commands."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
import yaml

from . import __version__
from . import commands as commands_mod
from . import doctor as doctor_mod
from . import extensions as extensions_mod
from . import gates as gates_mod
from . import inbox as inbox_mod
from . import project as project_mod
from . import render, runtime
from . import tasks as tasks_mod
from .paths import Layout, NoProject, find_root

app = typer.Typer(help="Supervisor: run a project with AI agents under a deterministic supervisor and a Control Center.",
                  no_args_is_help=True, pretty_exceptions_enable=False)
task_app = typer.Typer(help="task store")
gate_app = typer.Typer(help="gates")
inbox_app = typer.Typer(help="owner inbox (machine/fallback path; normally answered in the Control Center)")
extension_app = typer.Typer(help="project extensions (extensions/*.py, listed in project.yaml)")
boundary_app = typer.Typer(help="framework boundary check (gate G1) - for framework contributors")
app.add_typer(task_app, name="task")
app.add_typer(gate_app, name="gate")
app.add_typer(inbox_app, name="inbox")
app.add_typer(extension_app, name="extension")
app.add_typer(boundary_app, name="boundary")


def _layout() -> Layout:
    root = find_root()
    if root is None:
        raise typer.BadParameter(str(NoProject("no project.yaml found here or above; run `supervisor init <dir>` "
                                               "or set SUPERVISOR_ROOT")))
    layout = Layout(root).ensure()
    extensions_mod.load(layout)
    return layout


def _echo_cmd(outcome: dict, task_id: str) -> None:
    """Print a command outcome; render projections when we applied it ourselves (a running loop renders on its own)."""
    if outcome.get("queued"):
        typer.echo(f"{task_id}: queued ({outcome['id']}) — the running supervisor applies it within one iteration")
        return
    if not outcome.get("ok", True):
        raise typer.BadParameter(f"{task_id}: {outcome.get('error')}")
    render.render_all(_layout())
    res = outcome.get("result") or {}
    typer.echo(f"{task_id}: {res.get('status', 'ok')}" + (f" ({res['skipped']})" if res.get("skipped") else ""))
    for w in res.get("warnings") or []:
        typer.echo(f"warning: {w}")


def _writer() -> Layout:
    """For commands that mutate state/ directly: refuse while a supervisor holds the lock (single-writer rule)."""
    from .locks import holder
    layout = _layout()
    h = holder(layout.lock)
    if h is not None:
        raise typer.BadParameter(f"a supervisor holds state/.lock ({h.get('owner')}); use `supervisor cmd <kind> <json>` "
                                 "or the Control Center so the running loop applies the change")
    return layout


# ------------------------------------------------------------------ project lifecycle

@app.command()
def version():
    """Installed framework version."""
    typer.echo(__version__)


@app.command()
def init(directory: str = typer.Argument(".", help="project folder to create or fill (must be empty or have no project.yaml)"),
         name: str = typer.Option("", help="project name (defaults to the folder name)"),
         force: bool = typer.Option(False, help="write template files even if the folder already has a project.yaml")):
    """Create a thin project from the framework's template: project.yaml, config/, agents/, state/, docs/, extensions/,
    start scripts. Nothing project-specific is decided here — the setup skill (or you) fills the template in."""
    from . import template, updates
    target = Path(directory).resolve()
    fw = updates.framework_dir()
    if target == fw or (fw in target.parents and (fw / "runs") not in (target, *target.parents)):   # runs/ is scratch
        raise typer.BadParameter(f"{target} is inside the framework clone ({fw}); projects live in their own folder, "
                                 "never in the framework repository")
    if (target / "project.yaml").exists() and not force:
        raise typer.BadParameter(f"{target} already has a project.yaml (use --force to re-stamp template files)")
    written = template.stamp(target, name=name or target.name, force=force)
    for p in written:
        typer.echo(f"wrote {p.relative_to(target)}")
    typer.echo(f"\nproject '{name or target.name}' created at {target}\n"
               "next: fill in project.yaml, config/routing.yaml, agents/ and state/tasks_seed.yaml (the setup skill does this), "
               "then `supervisor doctor` and `supervisor task import`.")


@app.command()
def migrate(dry_run: bool = typer.Option(False, help="report what would change without writing")):
    """Bring a project's state and config up to the installed framework version (idempotent)."""
    from . import migrations
    layout = _writer()
    try:
        notes = migrations.run(layout, dry_run=dry_run)
    except migrations.MigrationError as exc:
        raise typer.BadParameter(str(exc))
    for n in notes:
        typer.echo(n)
    if not notes:
        typer.echo("nothing to migrate")


@app.command()
def status():
    """Render STATE.md and print the summary."""
    layout = _layout()
    out = render.render_state(layout)
    typer.echo(out.read_text(encoding="utf-8"))


@app.command("render")
def render_cmd():
    """Render every markdown projection from the store."""
    for p in render.render_all(_layout()):
        typer.echo(f"rendered {p.name}")


@app.command()
def doctor():
    """Health checks: project.yaml, Python, state store, routing/role cards, git, agent CLIs, extensions."""
    checks = doctor_mod.run(_layout())
    for c in checks:
        typer.echo(f"{'OK  ' if c.ok else 'FAIL'} {c.name}: {c.detail}")
    raise typer.Exit(0 if all(c.ok for c in checks) else 1)


@app.command()
def check():
    """Validate the task store (fields, enums, dependencies, cycles)."""
    layout = _layout()
    tasks = tasks_mod.load_all(layout)
    errors = tasks_mod.validate_graph(tasks)
    for e in errors:
        typer.echo(f"ERROR {e}")
    typer.echo(f"{len(tasks)} tasks; eligible: {', '.join(t.id for t in tasks_mod.eligible(layout, tasks)) or 'none'}")
    raise typer.Exit(1 if errors else 0)


# ------------------------------------------------------------------ tasks

@task_app.command("show")
def task_show(task_id: str):
    typer.echo(json.dumps(tasks_mod.load(_layout(), task_id).model_dump(mode="json"), indent=2, ensure_ascii=False))


@task_app.command("set-status")
def task_set_status(task_id: str, status: str, note: str = "", actor: str = "cli", force: bool = False):
    """Manual transition. COMPLETE is not allowed here; use deliver/verify/review/approve (advance). `--force` needs
    --actor owner and a --note, is persisted on the task and rendered as (forced)."""
    _echo_cmd(commands_mod.submit_or_apply(_layout(), "task.set_status", {"task_id": task_id, "status": status, "note": note,
                                                                       "force": force}, actor), task_id)


@task_app.command("import")
def task_import(seed: str = "state/tasks_seed.yaml", update_contracts: str = "",
                dry_run: bool = typer.Option(False, "--dry-run", help="validate and report what would change; "
                                             "writes nothing, works while a supervisor runs")):
    """Create tasks from the seed that do not exist yet; refresh only the named ids' contract fields. Idempotent.
    Goes through the command queue, so it also works while a supervisor is running (it applies it next iteration)."""
    ids = [s for s in update_contracts.split(",") if s]
    if dry_run:
        layout = _layout()
        try:
            res = tasks_mod.import_seed(layout, commands_mod.seed_path(layout, seed), ids, dry_run=True)
        except (ValueError, tasks_mod.TaskError) as exc:
            raise typer.BadParameter(str(exc))
        typer.echo(f"dry run: would create {len(res['created'])} ({', '.join(res['created']) or '-'}), "
                   f"update {len(res['updated'])} ({', '.join(res['updated']) or '-'}); nothing written")
        for w in res.get("warnings") or []:
            typer.echo(f"warning: {w}")
        return
    r = commands_mod.submit_or_apply(_layout(), "task.import", {"seed": seed, "update_contracts": ids}, "owner")
    if r.get("queued"):
        typer.echo(f"queued ({r['id']}) — the running supervisor applies it within one iteration")
        return
    if not r.get("ok", True):
        raise typer.BadParameter(str(r.get("error")))
    render.render_all(_layout())
    res = r.get("result") or {}
    typer.echo(f"created {len(res.get('created', []))}, updated {len(res.get('updated', []))}")
    for w in res.get("warnings") or []:
        typer.echo(f"warning: {w}")


@task_app.command("migrate-ownership")
def task_migrate_ownership():
    """Give every live writing-lane task without owned_paths explicit exclusive ownership (['.'])."""
    r = tasks_mod.migrate_ownership(_writer(), actor="cli")
    render.render_all(_layout())
    typer.echo(f"migrated {len(r['migrated'])}: {', '.join(r['migrated']) or 'nothing to do'}")


@task_app.command("create")
def task_create(file: str):
    """Add one task from a YAML file (same fields as the seed) through the command queue."""
    doc = yaml.safe_load(Path(file).read_text(encoding="utf-8"))
    _echo_cmd(commands_mod.submit_or_apply(_layout(), "task.create", {"task": doc}, "owner"), doc.get("id", "?"))


@task_app.command("set-job")
def task_set_job(task_id: str, job_json: str, cost_category: str = ""):
    """Replace the task's job block, e.g. '{"adapter":"python","fn":"my_job"}'."""
    _echo_cmd(commands_mod.submit_or_apply(_layout(), "task.set_job", {"task_id": task_id, "job": json.loads(job_json),
                                                                       "cost_category": cost_category or None}, "owner"), task_id)


@task_app.command("digest")
def task_digest(task_id: str, paths: str = ""):
    """sha256 digest of the task's artifact paths (its `paths` field, or --paths) — the identity of a delivery."""
    from ._vendor.contract import digest, manifest
    layout = _layout()
    t = tasks_mod.load(layout, task_id)
    ps = [p for p in paths.split(",") if p] or t.paths
    if not ps:
        raise typer.BadParameter("no paths recorded for this task; pass --paths")
    typer.echo(digest(manifest([layout.root / p for p in ps], root=layout.root)))


@task_app.command("deliver")
def task_deliver(task_id: str, digest: str, result: str = "", paths: str = "", actor: str = "cli"):
    _echo_cmd(commands_mod.submit_or_apply(_layout(), "task.deliver", {"task_id": task_id, "digest": digest, "result": result or None,
                                                                    "paths": [p for p in paths.split(",") if p]}, actor), task_id)


@task_app.command("verify")
def task_verify(task_id: str, digest: str, ok: bool = True, report: str = "", detail: str = ""):
    _echo_cmd(commands_mod.submit_or_apply(_layout(), "task.verify", {"task_id": task_id, "digest": digest, "ok": ok,
                                                                   "report": report or None, "detail": detail}, "verify"), task_id)


@task_app.command("review")
def task_review(task_id: str, digest: str, role: str, verdict: str, vendor: str = "", report: str = ""):
    from . import tasks as tasks_mod
    layout = _layout()
    sha = tasks_mod.report_file_sha256(layout, report) if report else None     # the report as submitted, not as applied
    _echo_cmd(commands_mod.submit_or_apply(layout, "task.review", {"task_id": task_id, "digest": digest, "role": role,
                                                                   "verdict": verdict, "vendor": vendor or None,
                                                                   "report": report or None, "report_sha256": sha},
                                           "review"), task_id)


@task_app.command("approve")
def task_approve(task_id: str, digest: str, response: str = "ACCEPT", by: str = "owner", note: str = ""):
    _echo_cmd(commands_mod.submit_or_apply(_layout(), "task.approve", {"task_id": task_id, "digest": digest, "response": response,
                                                                    "note": note}, by), task_id)


@task_app.command("advance")
def task_advance(task_id: str):
    _echo_cmd(commands_mod.submit_or_apply(_layout(), "task.advance", {"task_id": task_id}, "owner"), task_id)


@task_app.command("note")
def task_note(task_id: str, note: str, actor: str = "owner"):
    _echo_cmd(commands_mod.submit_or_apply(_layout(), "task.note", {"task_id": task_id, "note": note}, actor), task_id)


# ------------------------------------------------------------------ inbox, gates, extensions

@inbox_app.command("list")
def inbox_list(for_role: str = ""):
    """Open items (all roles by default)."""
    items = inbox_mod.open_items(_layout(), for_role or None)
    if not items:
        typer.echo("no open items")
        return
    for i in items:
        typer.echo(f"{i['id']} [{i.get('for')}] {i.get('subject')} - allowed: "
                   f"{', '.join(i.get('allowed_responses', [])) or 'free text'}")


@inbox_app.command("answer")
def inbox_answer(item_id: str, response: str, note: str = "", by: str = "owner"):
    """Record an answer (one of the item's allowed responses, or free text) and render HUMAN_INBOX.md."""
    item = inbox_mod.answer(_writer(), item_id, response, note, by)
    render.render_all(_layout())
    typer.echo(f"{item_id}: {item.get('status')}")


@gate_app.command("show")
def gate_show():
    typer.echo(json.dumps(gates_mod.load(_layout()), indent=2))


@gate_app.command("set")
def gate_set(gate_id: str, status: str, evidence: str = "", actor: str = "cli"):
    """Record a gate status. An owner gate (owner_ack_required) needs --actor owner; an evidence gate needs --evidence."""
    try:
        gates_mod.set_status(_writer(), gate_id, status, [e for e in evidence.split(",") if e], actor)
    except gates_mod.GateError as exc:
        raise typer.BadParameter(str(exc))
    render.render_all(_layout())
    typer.echo(f"{gate_id}: {status}")


@gate_app.command("ack")
def gate_ack(gate_id: str, note: str = "", actor: str = "owner"):
    try:
        gates_mod.ack(_writer(), gate_id, actor, note)
    except gates_mod.GateError as exc:
        raise typer.BadParameter(str(exc))
    typer.echo(f"{gate_id}: acknowledged by {actor}")


@boundary_app.command("check")
def boundary_check(root: str = typer.Option("", help="framework checkout (default: the one running)"),
                   as_json: bool = typer.Option(False, "--json", help="machine-readable report")):
    """G1: no project or extension import, no project-specific literal, no vendor name outside the listed files in
    supervisor/ (docs/BOUNDARY.md). Exit 0 clean, 1 violations, 2 when it cannot run. Needs no project."""
    from . import boundary
    raise typer.Exit(boundary.run(Path(root) if root else None, as_json=as_json, echo=typer.echo))


@extension_app.command("list")
def extension_list():
    reg = extensions_mod.load(_layout())
    for name in reg.loaded:
        typer.echo(f"loaded {name}")
    for err in reg.errors:
        typer.echo(f"ERROR {err}")
    if not reg.loaded and not reg.errors:
        typer.echo("no extensions configured (project.yaml `extensions:`)")


@extension_app.command("new")
def extension_new(name: str):
    """Scaffold extensions/<name>.py and list it in project.yaml."""
    layout = _layout()
    p = extensions_mod.scaffold_module(layout, name)
    doc = dict(project_mod.load(layout))
    exts = list(doc.get("extensions") or [])
    if name not in exts:
        exts.append(name)
        doc["extensions"] = exts
        project_mod.write(layout, doc)
    typer.echo(f"{p} ready; registered in project.yaml")


# ------------------------------------------------------------------ loop and control

@app.command()
def update(check_only: bool = typer.Option(False, "--check", help="report only; change nothing"),
           yes: bool = typer.Option(False, "--yes", "-y", help="apply without asking")):
    """Check the framework clone against origin; on yes: pull, reinstall, migrate, doctor, reload the loop."""
    from . import updates
    layout = _layout()
    status = updates.check(layout, fetch=True, max_age_s=0)
    if check_only or not status.available:
        for line in updates.describe(status):
            typer.echo(line)
        raise typer.Exit(0 if status.ok else 1)
    if yes:
        for line in updates.describe(status):
            typer.echo(line)
        result = updates.apply(layout, status)
        for step in result.steps:
            typer.echo("  " + step)
        if not result.ok:
            raise typer.BadParameter(result.error)
    else:
        result = updates.prompt_and_apply(layout, status, echo=typer.echo)
    if result and result.ok and result.restart_ui:
        typer.echo("restart any running Control Center so it imports the new code")


def _launch_update_check(layout: Layout, interactive: bool) -> None:
    """At launch: list what origin has that this clone does not; on a terminal, offer to update (fail-soft)."""
    from . import updates
    try:
        status = updates.check(layout, fetch=True)
    except Exception as exc:                                # noqa: BLE001 - never block a launch on the check
        typer.echo(f"update check skipped: {exc}")
        return
    if not status.available and not status.ok:
        typer.echo(updates.describe(status)[0])
        return
    if not status.available:
        return
    if not interactive:
        for line in updates.describe(status):
            typer.echo(line)
        typer.echo("run `supervisor update` (or use the Control Center banner) to apply")
        return
    result = updates.prompt_and_apply(layout, status, echo=typer.echo)
    if result and result.ok and result.restart_ui:
        typer.echo("updated: restarting on the new code (exit 3; a manual start needs to be run again)")
        raise typer.Exit(updates.RESTART_EXIT_CODE)


@app.command()
def run(sleep: float = 5.0, once: bool = False, max_iterations: int = 0, unattended: bool = False,
        no_update_check: bool = typer.Option(False, help="skip the origin check at launch")):
    """Run the supervisor loop (holds state/.lock). Exit code 3 = reload requested."""
    from .loop import Supervisor
    layout = _layout()
    if not no_update_check and not once and not max_iterations:
        _launch_update_check(layout, interactive=False)     # the loop never blocks on a prompt: the UI asks
    sup = Supervisor(layout, sleep_s=sleep, unattended=unattended)
    code = sup.run(max_iterations=1 if once else (max_iterations or None), reset_decision=True)
    raise typer.Exit(code)


def _control(kind: str, actor: str):
    r = commands_mod.submit_or_apply(_layout(), kind, {}, actor)
    typer.echo(json.dumps(r))


@app.command()
def pause(actor: str = "owner"):
    """Pause dispatch at the next safe point (running jobs finish)."""
    _control("loop.pause", actor)


@app.command()
def resume(actor: str = "owner"):
    _control("loop.resume", actor)


@app.command()
def stop(actor: str = "owner"):
    _control("loop.stop", actor)


@app.command()
def reload(actor: str = "owner"):
    _control("loop.reload", actor)


@app.command()
def drain(shutdown: bool = False, actor: str = "owner"):
    """Launch nothing more; let running jobs finish and be verified, then stop (--shutdown: then power the PC off)."""
    typer.echo(json.dumps(commands_mod.submit_or_apply(_layout(), "loop.drain", {"shutdown": shutdown}, actor)))


@app.command()
def cmd(kind: str, payload: str = "{}", actor: str = "owner"):
    """Submit any command (JSON payload) — applied by the running supervisor, or directly if none runs."""
    typer.echo(json.dumps(commands_mod.submit_or_apply(_layout(), kind, json.loads(payload), actor)))


@app.command()
def supervisor():
    """Show runtime state."""
    typer.echo(json.dumps(runtime.read(_layout()), indent=2))


@app.command()
def ui(host: str = "127.0.0.1", port: int = 0,
       no_update_check: bool = typer.Option(False, help="skip the origin check at launch")):
    """Start the Control Center (FastAPI + Uvicorn), loopback-only by default; port from project.yaml unless given.
    At launch, lists framework updates available on origin and offers to apply them (exit 3 = restart on new code)."""
    import sys
    from .ui.app import run as run_ui
    layout = _layout()
    if not no_update_check:
        _launch_update_check(layout, interactive=sys.stdin.isatty())
    run_ui(layout, host=host, port=port or project_mod.ui_port(layout))


def main():
    app()


if __name__ == "__main__":
    main()


# Extension command groups are attached at import time so `supervisor <name>` works from any project directory.
def _attach_extension_clis() -> None:
    root = find_root()
    if root is None:
        return
    try:
        reg = extensions_mod.load(Layout(root))
    except Exception as exc:                                # noqa: BLE001 - a broken extension must not break the CLI
        print(f"warning: extensions not loaded, their command groups are missing from this CLI: {exc}", file=sys.stderr)
        return
    for err in reg.errors:                                  # a failed extension's `supervisor <name>` group is absent: say so
        print(f"warning: extension not loaded ({err}); its commands are missing from this CLI", file=sys.stderr)
    for ext_app, name in reg.cli_apps:
        try:
            app.add_typer(ext_app, name=name)
        except Exception as exc:                            # noqa: BLE001
            print(f"warning: command group {name!r} could not be attached: {exc}", file=sys.stderr)


_attach_extension_clis()
