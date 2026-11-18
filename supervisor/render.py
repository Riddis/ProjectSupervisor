"""Rendered projections of the state store (never parsed back): TASKS, STATE, ISSUES, DECISIONS, HUMAN_INBOX,
OWNER_TASKS, plus any projection a plugin registers. Deterministic given the store (timestamps come from the data, plus one render stamp)."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path

import yaml

from . import plugins as plugins_mod
from . import project as project_mod
from . import gates as gates_mod
from . import store, tasks as tasks_mod
from .models.task import TaskStatus
from .paths import Layout
from .ui import logtail as logtail_mod


def _write(path: Path, lines: list[str]):
    """Replace `path` with `lines`, in its own line endings — unless that is already its content (R-22): a
    projection rebuilt from an unchanged store produces the unchanged text, and `store.rewrite_text` is the one
    primitive the framework already has for "write only when it differs" (migrations use it for the same
    reason). Every render still reads and builds fresh; only the write itself is skipped."""
    store.rewrite_text(path, "\n".join(lines).rstrip() + "\n")     # the file's own line endings (1.12.1)


_held = threading.local()        # render-lock paths this thread holds: the lock is reentrant within a thread


@contextmanager
def render_lock(layout: Layout):
    """`state/.render.lock`, held for one projection's read and write. Reentrant within a thread, so a plugin
    projection that itself calls a built-in render_* function does not wait on its own lock; across threads and
    processes it orders renders. Waits up to 30 s, then raises LockHeld (render_all logs that view and goes on)."""
    from .locks import FileLock
    held = _held.__dict__.setdefault("paths", set())
    key = str(layout.render_lock)
    if key in held:
        yield
        return
    with FileLock(layout.render_lock, owner="render", timeout=30.0):
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)


def _render_write(layout: Layout, path: Path, build) -> Path:
    """Write one projection under `state/.render.lock`: `build()` (the store read plus the view's computation) and
    the write happen inside the same held lock, so two renderers racing each other (the loop and an owner command,
    say) are strictly ordered rather than merely atomic-replacing in whatever order they finish. Whichever call
    enters the lock second cannot start its own read until the first one has both read and written, so its snapshot
    is never older than the file it is about to replace; before 1.10.0 a slower render holding an older snapshot
    could land after a faster, fresher one. Not the same lock as `state/.lock`: the loop holds that one for its
    whole run and calls render_all() from inside it, so reusing it here would make that call reject itself."""
    with render_lock(layout):
        lines = build()
        _write(path, lines)
    return path


def _front_matter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    return yaml.safe_load(m.group(1)) or {} if m else {}


def render_tasks(layout: Layout) -> Path:
    def build() -> list[str]:
        tasks = tasks_mod.load_all(layout)
        elig = {t.id for t in tasks_mod.eligible(layout, tasks)}
        stamp = max((t.updated or "" for t in tasks.values()), default="")
        lines = ["# TASKS (rendered from state/tasks/*.yaml — do not edit)", "",
                 f"Store last updated {stamp}. {len(tasks)} tasks. Eligible now: " + (", ".join(sorted(elig)) or "none"), ""]
        by_phase: dict[int, list] = {}
        for t in tasks.values():
            by_phase.setdefault(t.phase, []).append(t)
        for phase in sorted(by_phase):
            rows = sorted(by_phase[phase], key=lambda x: x.id)
            lines.extend([f"## Phase {phase} — {project_mod.phase_label(layout, phase)} ({len(rows)} tasks)", "",
                      "| ID | Title | Owner | Depends on | Reviewer | Human | Cost | Status |", "|---|---|---|---|---|---|---|---|"])
            for t in rows:
                flag = "⚑ " if t.id in elig else ""
                forced = " (forced)" if t.forced else ""
                lines.append(f"| {t.id} | {t.title} | {t.owner_role} | {', '.join(t.depends_on) or '—'} | "
                             f"{', '.join(t.reviewer) or '—'} | {t.human_approval.value} | {t.cost_category.value} | {flag}{t.status.value}{forced} |")
            lines.append("")
            for t in rows:
                lines.extend([f"### {t.id} — {t.title}", "", f"**Objective.** {t.objective}", "",
                          "**Actions.** " + "; ".join(t.actions), "", "**Artifacts.** " + ", ".join(t.artifacts), "",
                          "**Tests.** " + ("; ".join(t.tests) or "—"), "", f"**Acceptance.** {t.acceptance}", "",
                          f"**Escalation.** {t.escalation}", ""])
                if t.history:
                    lines.extend(["**History.** " + "; ".join(f"{h.get('at', '')[:16]} {h.get('by')}: {h.get('from')} → {h.get('to')}"
                                                          + (" (FORCED: " + str(h.get('note')) + ")" if h.get('forced') else "") for h in t.history), ""])
                if t.attempts:
                    lines.extend(["**Attempts.** " + "; ".join(f"{a.id} {a.resource}{' ' + a.model if a.model else ''} → {a.outcome or '…'}" for a in t.attempts), ""])
                if t.result:
                    lines.extend([f"**Result.** {t.result}", ""])
        return lines
    return _render_write(layout, layout.root / "TASKS.md", build)


def issue_counts(layout: Layout) -> dict[str, int]:
    """Open, recovered and resolved counted separately (R-21): a recovered issue — its task completed, but nobody
    has made the explicit resolved decision yet — is neither open work nor a closed matter."""
    rows = [store.read(p) for p in layout.issues.glob("*.yaml")]
    return {s: sum(1 for r in rows if r.get("status") == s) for s in ("open", "recovered", "resolved")}


def render_issues(layout: Layout) -> Path:
    def build() -> list[str]:
        rows = [store.read(p) for p in sorted(layout.issues.glob("*.yaml"))]
        counts = issue_counts(layout)
        lines = ["# ISSUES (rendered — records live in state/issues/)", "",
                 f"{counts['open']} open · {counts['recovered']} recovered · {counts['resolved']} resolved", "",
                 "| ID | Status | Severity | Title | Owner | Resolution | Resolved by | Resolving task |",
                 "|---|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r.get('id')} | {r.get('status')} | {r.get('severity')} | {r.get('title')} | "
                         f"{r.get('owner_role')} | {r.get('resolution') or r.get('recovered_attempt') or r.get('resolution_task') or '—'} | "
                         f"{r.get('resolved_by') or '—'} | {r.get('resolved_by_task') or '—'} |")
        return lines
    return _render_write(layout, layout.root / "ISSUES.md", build)


def render_decisions(layout: Layout) -> Path:
    def build() -> list[str]:
        lines = ["# DECISIONS (rendered index — records live in state/decisions/)", "", "| ID | Date | Title | Status | Decided by |", "|---|---|---|---|---|"]
        for p in sorted(layout.decisions.glob("*.md")):
            fm = _front_matter(p.read_text(encoding="utf-8"))
            lines.append(f"| {fm.get('id', p.stem)} | {fm.get('date', '')} | {fm.get('title', '')} | {fm.get('status', '')} | {fm.get('decided_by', '')} |")
        return lines
    return _render_write(layout, layout.root / "DECISIONS.md", build)


def render_inbox(layout: Layout) -> Path:
    def build() -> list[str]:
        items = [store.read(p) for p in sorted(layout.inbox.glob("*.yaml"))]
        open_items = [i for i in items if i.get("status") == "open"]
        lines = ["# HUMAN INBOX (rendered — items live in state/inbox/; answer in the Control Center or `supervisor inbox answer`)", ""]
        if not open_items:
            lines.append("No open items.")
        for i in open_items:
            lines.extend([f"## {i.get('id')} — {i.get('subject')}  ({i.get('kind', 'question')}, for {i.get('for', 'owner')})", "",
                      i.get("detail") or i.get("summary", ""), "",   # detail = the full text the summary was cut from
                      "Allowed responses: " + " / ".join(i.get("allowed_responses", [])), ""])
        archived = [i for i in items if i.get("status") != "open"]
        if archived:
            lines.extend(["", "## Archive", ""] + [f"- {i.get('id')} — {i.get('subject')} — {i.get('status')} {i.get('answered_at') or ''}" for i in archived])
        return lines
    return _render_write(layout, layout.root / "HUMAN_INBOX.md", build)


def render_owner_tasks(layout: Layout) -> Path:
    def build() -> list[str]:
        data = yaml.safe_load(layout.owner_tasks.read_text(encoding="utf-8")) if layout.owner_tasks.exists() else {}
        rows = data.get("owner_tasks", []) if data else []
        lines = ["# OWNER TASKS (rendered — records live in state/owner_tasks.yaml; also shown in the Control Center)", "",
                 "| ID | Title | Status | Priority | Blocks |", "|---|---|---|---|---|"]
        lines.extend([f"| {t['id']} | {t['title']} | {t['status']} | {t.get('priority', '')} | {', '.join(t.get('blocks', [])) or '—'} |" for t in rows])
        lines.append("")
        for t in rows:
            lines.extend([f"## {t['id']} — {t['title']}", "", str(t.get("guidance", "")).strip(), ""])
        return lines
    return _render_write(layout, layout.root / "OWNER_TASKS.md", build)


def _tail_log(layout: Layout, n=10) -> list[dict]:
    """The last `n` events, oldest first within that window — STATE.md's own convention, kept distinct from
    `ui.logtail.tail()`'s newest-first. Seeks from the end instead of reading the whole file (R-23): shares
    `logtail_mod`'s byte-offset tail rather than a second implementation of it."""
    if not layout.log.exists():
        return []
    rows = []
    for line in logtail_mod._tail_lines(layout.log, n):
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def phase_and_gates_passed(layout: Layout, tasks: dict) -> tuple[int, list[str]]:
    """(current phase, gate ids passed) exactly as `state_lines` renders them into STATE.md's "Phase:" line. Shared
    so anything that must agree with STATE.md (a project's resume or orientation check, say) uses this formula
    instead of a second derivation that can silently drift from it."""
    gate_data = gates_mod.load(layout)
    phases_done = sorted({g.get("phase", -1) for g in (gate_data.get("gates") or {}).values() if g.get("status") == "PASSED"})
    return tasks_mod.current_phase(tasks), [f"G{p}" for p in phases_done]


def state_lines(layout: Layout, runtime: dict | None = None) -> list[str]:
    """The lines `render_state` writes to STATE.md, computed but not written: `supervisor status` prints exactly
    this (with the live runtime record) without the write — see `render_state` and `cli.status`."""
    tasks = tasks_mod.load_all(layout)
    elig = tasks_mod.eligible(layout, tasks)
    in_flight = [t for t in tasks.values() if t.status in (TaskStatus.IN_PROGRESS, TaskStatus.WAITING_FOR_REVIEW)]
    waiting = [t for t in tasks.values() if t.status == TaskStatus.WAITING_FOR_HUMAN]
    blocked = [t for t in tasks.values() if t.status in (TaskStatus.BLOCKED, TaskStatus.FAILED)]
    done = sum(1 for t in tasks.values() if t.status == TaskStatus.COMPLETE)
    cancelled = len(tasks) - len(tasks_mod.active(tasks))
    cancelled_note = f" ({cancelled} cancelled)" if cancelled else ""
    current_phase, gates_passed = phase_and_gates_passed(layout, tasks)
    open_inbox = [store.read(p) for p in sorted(layout.inbox.glob("*.yaml"))]
    open_inbox = [i for i in open_inbox if i.get("status") == "open"]
    issues = issue_counts(layout)
    runtime = runtime or {}
    last = _tail_log(layout, 1)
    stamp = last[0].get("ts") if last else max((t.updated or "" for t in tasks.values()), default="")
    lines = [f"# STATE — {project_mod.name(layout)} (rendered — do not edit) — store last changed {stamp}", "",
             "## Where we are", "",
             f"- **Phase:** {current_phase} ({project_mod.phase_label(layout, current_phase)}) · gates passed: {', '.join(gates_passed) or 'none'}",
             f"- **Tasks:** {done}/{len(tasks_mod.active(tasks))} complete{cancelled_note} · in flight: {', '.join(t.id for t in in_flight) or 'none'} · "
             f"waiting for owner: {', '.join(t.id for t in waiting) or 'none'} · blocked/failed: {', '.join(t.id for t in blocked) or 'none'}",
             f"- **Eligible next:** {', '.join(t.id for t in elig) or 'none'}",
             f"- **Supervisor:** {runtime.get('status', 'not running')}" + (f" (pid {runtime.get('pid')}, heartbeat {runtime.get('heartbeat')})" if runtime.get('pid') else ""),
             f"- **Owner inbox:** {len(open_inbox)} open item(s)" + (": " + ", ".join(i.get('id') for i in open_inbox) if open_inbox else ""),
             f"- **Decisions:** {len(list(layout.decisions.glob('*.md')))} (DECISIONS.md) · **Issues:** "
             f"{issues['open']} open, {issues['recovered']} recovered, {issues['resolved']} resolved (ISSUES.md)", ""]
    lines += ["## Last events", ""] + [f"- {e.get('ts')} {e.get('actor')} {e.get('event')} {e.get('task', '')} {e.get('to', '') or ''}"
                                        + (" FORCED" if e.get('forced') else "") + f" {e.get('note', '') or ''}".rstrip()
                                        for e in _tail_log(layout)] + [""]
    nxt = elig[0] if elig else None
    lines += ["## Next steps for a resuming session", "",
              f"1. {'Work ' + nxt.id + ' — ' + nxt.title if nxt else 'No eligible task: check waiting/blocked items and the owner inbox.'}",
              "2. Owner items: see HUMAN_INBOX.md and OWNER_TASKS.md.",
              "3. After any task: `supervisor task set-status` / delivery records, then `supervisor render`, then commit."]
    return lines


def render_state(layout: Layout, runtime: dict | None = None) -> Path:
    return _render_write(layout, layout.root / "STATE.md",
                         lambda: state_lines(layout, runtime=runtime))


# Last error per (project root, view), so a view that keeps failing is logged once, not on every loop iteration.
_last_error: dict[tuple[str, str], str] = {}


def _guarded(layout: Layout, fn, *args, **kw) -> Path | None:
    """Render one view; a failure skips it instead of failing the loop iteration. The rendered views are read-only
    projections, so skipping one only leaves its previous file in place, while an exception here used to count as a
    loop error: an agent mid-way through writing a YAML record the view reads made five iterations in a row fail
    and the supervisor stopped itself BLOCKED. The error is logged once per distinct message (`render_error`) and
    the recovery once (`render_recovered`)."""
    key = (str(layout.root), fn.__name__)
    try:
        out = fn(layout, *args, **kw)
    except Exception as exc:                                # noqa: BLE001 - a view must never stop the loop
        msg = f"{type(exc).__name__}: {exc}"[:300]
        if _last_error.get(key) != msg:
            _last_error[key] = msg
            tasks_mod.log(layout, "render_error", "render", view=fn.__name__, error=msg)
        return None
    if _last_error.pop(key, None) is not None:
        tasks_mod.log(layout, "render_recovered", "render", view=fn.__name__)
    return out


def _stat_entry(root: Path, p: Path) -> str | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return f"{p.relative_to(root).as_posix()}\0{st.st_mtime_ns}\0{st.st_size}"


def _core_inputs_fingerprint(layout: Layout) -> str:
    """Fingerprint of exactly what the five store-backed projections read (R-22): the task store (by identity —
    see below), the issue, decision and inbox directories (stat-only: path, mtime, size, no parse), the
    owner-tasks and gates documents, the event log and project.yaml. Checked against every one of those five
    builders' own reads, so "dirty" here means what it says: nothing any of them touches has moved. render_state
    is not gated by this (it also depends on the caller's own `runtime` dict, which this walk cannot see) — it
    always rebuilds, and `_write`'s own content compare is what keeps an unchanged STATE.md from being rewritten.

    The task store's own share of this is `(id, rev, updated)` per task from `tasks_mod.load_all(layout)`, never
    a second `layout.tasks.glob()` — inside a turn that call is free (the turn's own snapshot), and a second real
    scan here would be exactly the R-09 regression this project's own `test_r09_turn_snapshot.py` measures: one
    task-directory scan per idle turn, not two. Deliberately excludes state/runtime/jobs/ (R-23): no projection
    reads a job record, so an archive that grows with finished work must never make this walk — or the turn —
    cost more."""
    entries: list[str] = [f"{t.id}\0{t.rev}\0{t.updated or ''}" for t in tasks_mod.load_all(layout).values()]
    for d, pattern in ((layout.issues, "*.yaml"), (layout.decisions, "*.md"), (layout.inbox, "*.yaml")):
        try:
            paths = sorted(d.glob(pattern))
        except OSError:
            paths = []
        entries.extend(e for p in paths if (e := _stat_entry(layout.root, p)))
    entries.extend(e for p in (layout.owner_tasks, layout.gates, layout.log, layout.project_file)
                   if (e := _stat_entry(layout.root, p)))
    entries.sort()
    h = hashlib.sha1(usedforsecurity=False)
    for e in entries:
        h.update(e.encode("utf-8", "surrogateescape"))
        h.update(b"\n")
    return h.hexdigest()[:16]


_LAST_CORE_FP: dict[str, str] = {}
_LAST_CORE_FP_LOCK = threading.Lock()


def render_all(layout: Layout, *, force: bool = False, **kw) -> list[Path]:
    """Every projection. The five store-backed ones (plus STATE.md's own build) are skipped outright — not
    rebuilt, not opened for a write — while `_core_inputs_fingerprint` has not moved since the last call for this
    project (R-22): an idle turn costs one stat walk, not nine YAML directory listings and six writes. `force`
    (the `supervisor render` CLI command) always does the full pass, so an explicit "render everything" never
    reports that nothing happened. A plugin projection is never gated by this — only a built-in one; it has no
    external inputs this walk can see."""
    key = str(layout.root)
    fp = _core_inputs_fingerprint(layout)
    with _LAST_CORE_FP_LOCK:
        skip = not force and _LAST_CORE_FP.get(key) == fp
    if skip:
        out = []
    else:
        built = [_guarded(layout, fn) for fn in (render_tasks, render_issues, render_decisions, render_inbox, render_owner_tasks)]
        built.append(_guarded(layout, render_state, **kw))
        out = [p for p in built if p is not None]
        if all(p is not None for p in built):
            # Only remember this fingerprint as "done" when every one of them actually rendered: a view that
            # failed (R-02 — a half-written YAML record, say) must be retried every call until it recovers, not
            # cached as settled the moment one of its six siblings happened to succeed next to it.
            with _LAST_CORE_FP_LOCK:
                _LAST_CORE_FP[key] = fp
    for fn in plugins_mod.load(layout).projections:
        try:
            with render_lock(layout):                       # a registered projection reads and writes under it too
                out.append(fn(layout))
        except Exception as exc:                            # noqa: BLE001 - a projection must never stop the loop
            tasks_mod.log(layout, "projection_error", "render", projection=getattr(fn, "__name__", "?"), error=str(exc)[:200])
    return out
