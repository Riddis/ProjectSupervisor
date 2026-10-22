"""Rendered projections of the state store (never parsed back): TASKS, STATE, ISSUES, DECISIONS, HUMAN_INBOX,
OWNER_TASKS, plus any projection an extension registers. Deterministic given the store (timestamps come from the data, plus one render stamp)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from . import extensions as extensions_mod
from . import project as project_mod
from . import gates as gates_mod
from . import store, tasks as tasks_mod
from ._vendor.contract import atomic_write_text
from .models.task import TaskStatus
from .paths import Layout


def _write(path: Path, lines: list[str]):
    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")


def _front_matter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    return yaml.safe_load(m.group(1)) or {} if m else {}


def render_tasks(layout: Layout) -> Path:
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
        lines += [f"## Phase {phase} — {project_mod.phase_label(layout, phase)} ({len(rows)} tasks)", "",
                  "| ID | Title | Owner | Depends on | Reviewer | Human | Cost | Status |", "|---|---|---|---|---|---|---|---|"]
        for t in rows:
            flag = "⚑ " if t.id in elig else ""
            forced = " (forced)" if t.forced else ""
            lines.append(f"| {t.id} | {t.title} | {t.owner_role} | {', '.join(t.depends_on) or '—'} | "
                         f"{', '.join(t.reviewer) or '—'} | {t.human_approval.value} | {t.cost_category.value} | {flag}{t.status.value}{forced} |")
        lines.append("")
        for t in rows:
            lines += [f"### {t.id} — {t.title}", "", f"**Objective.** {t.objective}", "",
                      "**Actions.** " + "; ".join(t.actions), "", "**Artifacts.** " + ", ".join(t.artifacts), "",
                      "**Tests.** " + ("; ".join(t.tests) or "—"), "", f"**Acceptance.** {t.acceptance}", "",
                      f"**Escalation.** {t.escalation}", ""]
            if t.history:
                lines += ["**History.** " + "; ".join(f"{h.get('at', '')[:16]} {h.get('by')}: {h.get('from')} → {h.get('to')}"
                                                      + (" (FORCED: " + str(h.get('note')) + ")" if h.get('forced') else "") for h in t.history), ""]
            if t.attempts:
                lines += ["**Attempts.** " + "; ".join(f"{a.id} {a.resource}{' ' + a.model if a.model else ''} → {a.outcome or '…'}" for a in t.attempts), ""]
            if t.result:
                lines += [f"**Result.** {t.result}", ""]
    out = layout.root / "TASKS.md"
    _write(out, lines)
    return out


def render_issues(layout: Layout) -> Path:
    rows = [store.read(p) for p in sorted(layout.issues.glob("*.yaml"))]
    lines = ["# ISSUES (rendered — records live in state/issues/)", "", "| ID | Status | Severity | Title | Owner | Resolution |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r.get('id')} | {r.get('status')} | {r.get('severity')} | {r.get('title')} | {r.get('owner_role')} | {r.get('resolution_task') or '—'} |")
    out = layout.root / "ISSUES.md"
    _write(out, lines)
    return out


def render_decisions(layout: Layout) -> Path:
    lines = ["# DECISIONS (rendered index — records live in state/decisions/)", "", "| ID | Date | Title | Status | Decided by |", "|---|---|---|---|---|"]
    for p in sorted(layout.decisions.glob("*.md")):
        fm = _front_matter(p.read_text(encoding="utf-8"))
        lines.append(f"| {fm.get('id', p.stem)} | {fm.get('date', '')} | {fm.get('title', '')} | {fm.get('status', '')} | {fm.get('decided_by', '')} |")
    out = layout.root / "DECISIONS.md"
    _write(out, lines)
    return out


def render_inbox(layout: Layout) -> Path:
    items = [store.read(p) for p in sorted(layout.inbox.glob("*.yaml"))]
    open_items = [i for i in items if i.get("status") == "open"]
    lines = ["# HUMAN INBOX (rendered — items live in state/inbox/; answer in the Control Center or `supervisor inbox answer`)", ""]
    if not open_items:
        lines.append("No open items.")
    for i in open_items:
        lines += [f"## {i.get('id')} — {i.get('subject')}  ({i.get('kind', 'question')}, for {i.get('for', 'owner')})", "",
                  i.get("summary", ""), "", "Allowed responses: " + " / ".join(i.get("allowed_responses", [])), ""]
    archived = [i for i in items if i.get("status") != "open"]
    if archived:
        lines += ["", "## Archive", ""] + [f"- {i.get('id')} — {i.get('subject')} — {i.get('status')} {i.get('answered_at') or ''}" for i in archived]
    out = layout.root / "HUMAN_INBOX.md"
    _write(out, lines)
    return out


def render_owner_tasks(layout: Layout) -> Path:
    data = yaml.safe_load(layout.owner_tasks.read_text(encoding="utf-8")) if layout.owner_tasks.exists() else {}
    rows = data.get("owner_tasks", []) if data else []
    lines = ["# OWNER TASKS (rendered — records live in state/owner_tasks.yaml; also shown in the Control Center)", "",
             "| ID | Title | Status | Priority | Blocks |", "|---|---|---|---|---|"]
    lines += [f"| {t['id']} | {t['title']} | {t['status']} | {t.get('priority', '')} | {', '.join(t.get('blocks', [])) or '—'} |" for t in rows]
    lines.append("")
    for t in rows:
        lines += [f"## {t['id']} — {t['title']}", "", str(t.get("guidance", "")).strip(), ""]
    out = layout.root / "OWNER_TASKS.md"
    _write(out, lines)
    return out


def _tail_log(layout: Layout, n=10) -> list[dict]:
    if not layout.log.exists():
        return []
    rows = []
    for line in layout.log.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def render_state(layout: Layout, runtime: dict | None = None, capacity_lines: list[str] | None = None) -> Path:
    tasks = tasks_mod.load_all(layout)
    gate_data = gates_mod.load(layout)
    elig = tasks_mod.eligible(layout, tasks)
    in_flight = [t for t in tasks.values() if t.status in (TaskStatus.IN_PROGRESS, TaskStatus.WAITING_FOR_REVIEW)]
    waiting = [t for t in tasks.values() if t.status == TaskStatus.WAITING_FOR_HUMAN]
    blocked = [t for t in tasks.values() if t.status in (TaskStatus.BLOCKED, TaskStatus.FAILED)]
    done = sum(1 for t in tasks.values() if t.status == TaskStatus.COMPLETE)
    phases_done = sorted({g.get("phase", -1) for g in (gate_data.get("gates") or {}).values() if g.get("status") == "PASSED"})
    current_phase = min((t.phase for t in tasks.values() if t.status != TaskStatus.COMPLETE), default=0)
    open_inbox = [store.read(p) for p in sorted(layout.inbox.glob("*.yaml"))]
    open_inbox = [i for i in open_inbox if i.get("status") == "open"]
    runtime = runtime or {}
    last = _tail_log(layout, 1)
    stamp = last[0].get("ts") if last else max((t.updated or "" for t in tasks.values()), default="")
    lines = [f"# STATE — {project_mod.name(layout)} (rendered — do not edit) — store last changed {stamp}", "",
             "## Where we are", "",
             f"- **Phase:** {current_phase} ({project_mod.phase_label(layout, current_phase)}) · gates passed: {', '.join(f'G{p}' for p in phases_done) or 'none'}",
             f"- **Tasks:** {done}/{len(tasks)} complete · in flight: {', '.join(t.id for t in in_flight) or 'none'} · "
             f"waiting for owner: {', '.join(t.id for t in waiting) or 'none'} · blocked/failed: {', '.join(t.id for t in blocked) or 'none'}",
             f"- **Eligible next:** {', '.join(t.id for t in elig) or 'none'}",
             f"- **Supervisor:** {runtime.get('status', 'not running')}" + (f" (pid {runtime.get('pid')}, heartbeat {runtime.get('heartbeat')})" if runtime.get('pid') else ""),
             f"- **Owner inbox:** {len(open_inbox)} open item(s)" + (": " + ", ".join(i.get('id') for i in open_inbox) if open_inbox else ""),
             f"- **Decisions:** {len(list(layout.decisions.glob('*.md')))} (DECISIONS.md) · **Issues open:** "
             f"{sum(1 for p in layout.issues.glob('*.yaml') if store.read(p).get('status') == 'open')} (ISSUES.md)", ""]
    if capacity_lines:
        lines += ["## Capacity", ""] + [f"- {l}" for l in capacity_lines] + [""]
    lines += ["## Last events", ""] + [f"- {e.get('ts')} {e.get('actor')} {e.get('event')} {e.get('task', '')} {e.get('to', '') or ''}"
                                        + (" FORCED" if e.get('forced') else "") + f" {e.get('note', '') or ''}".rstrip()
                                        for e in _tail_log(layout)] + [""]
    nxt = elig[0] if elig else None
    lines += ["## Next steps for a resuming session", "",
              f"1. {'Work ' + nxt.id + ' — ' + nxt.title if nxt else 'No eligible task: check waiting/blocked items and the owner inbox.'}",
              "2. Owner items: see HUMAN_INBOX.md and OWNER_TASKS.md.",
              "3. After any task: `supervisor task set-status` / delivery records, then `supervisor render`, then commit."]
    out = layout.root / "STATE.md"
    _write(out, lines)
    return out


def render_all(layout: Layout, **kw) -> list[Path]:
    out = [render_tasks(layout), render_issues(layout), render_decisions(layout), render_inbox(layout),
           render_owner_tasks(layout), render_state(layout, **kw)]
    for fn in extensions_mod.load(layout).projections:
        try:
            out.append(fn(layout))
        except Exception as exc:                            # noqa: BLE001 - a projection must never stop the loop
            tasks_mod.log(layout, "projection_error", "render", projection=getattr(fn, "__name__", "?"), error=str(exc)[:200])
    return out
