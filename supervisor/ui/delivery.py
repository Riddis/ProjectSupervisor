"""What the owner is asked to approve, shown in full: the files a delivery digest covers, the verification and the
live reviews of that digest (verdict, summary, findings), and any declared artifact the digest does not cover.
Shared by the inbox's approval cards and the task page. Every path comes from the task record, never from the
browser; a file is opened through `/tasks/<id>/files/<n>`, an index into the task's own `paths`."""
from __future__ import annotations

import fnmatch
import html
import json

from .._vendor.contract import contained
from ..models.task import Task
from ..paths import Layout
from . import thumbs

TEXT_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".json", ".jsonl", ".toml", ".py", ".csv", ".ini", ".cfg", ".log",
                 ".ps1", ".sh", ".html", ".css", ".js"}
TEXT_LIMIT = 200_000          # a whole decision record or config file; beyond this, open the file itself
MAX_THUMBS = 24               # a delivery of hundreds of images shows the first ones as thumbnails, the rest as links
REVIEW_TEXT_LIMIT = 4000


def resolve(layout: Layout, rel: str):
    """The delivered file `rel` (a path the task record holds) on disk, or None when it is gone or escapes the
    project root."""
    try:
        path = (layout.root / str(rel)).resolve()
    except (OSError, ValueError):
        return None
    if not contained(path, [layout.root], root=layout.root) or not path.is_file():
        return None
    return path


def file_view(layout: Layout, task_id: str, index: int, rel: str, thumb: bool = True) -> dict:
    view = {"ref": rel, "href": f"/tasks/{task_id}/files/{index}", "kind": "link"}
    path = resolve(layout, rel)
    if path is None:
        return {**view, "kind": "missing"}
    if thumbs.is_image(path):
        key = thumbs.ensure(layout, path) if thumb else None
        return {**view, "kind": "image", "thumb_key": key} if key else view
    if path.suffix.lower() in TEXT_SUFFIXES:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {**view, "kind": "missing"}
        truncated = len(text) > TEXT_LIMIT
        return {**view, "kind": "text", "html": html.escape(text[:TEXT_LIMIT]), "truncated": truncated,
                "lines": text.count("\n") + 1}
    return view


def uncovered(task: Task) -> list[str]:
    """Declared artifacts (`task.artifacts`, names or globs) that match none of the files the digest covers."""
    paths = list(task.paths)
    return [a for a in task.artifacts
            if not any(p == a or fnmatch.fnmatchcase(p, a) for p in paths)]


def _review_report(layout: Layout, ref: str | None) -> dict:
    path = resolve(layout, ref) if ref else None
    if path is None:
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    findings = [{"category": str(f.get("category") or ""), "blocking": bool(f.get("blocking")),
                 "message": str(f.get("message") or "")[:REVIEW_TEXT_LIMIT]}
                for f in (doc.get("findings") or []) if isinstance(f, dict)]
    return {"summary": str(doc.get("summary") or "")[:REVIEW_TEXT_LIMIT], "findings": findings}


def view(layout: Layout, task: Task, digest: str | None = None) -> dict:
    """The delivery behind `digest` (default: the task's current one). `current` is False when the task has moved on
    to another digest since — the files on disk then belong to the newer delivery, so none are shown."""
    d = task.delivery
    digest = digest or d.digest
    current = bool(digest) and digest == d.digest
    out = {"digest": digest, "current": current, "files": [], "uncovered": [], "verification": None, "reviews": []}
    if not current:
        return out
    shown_images = 0
    for i, rel in enumerate(task.paths):
        fv = file_view(layout, task.id, i, rel, thumb=shown_images < MAX_THUMBS)
        if fv["kind"] == "image":
            shown_images += 1
        out["files"].append(fv)                    # images past MAX_THUMBS are links, not thumbnails
    out["uncovered"] = uncovered(task)
    if d.verification and d.verification.digest == digest:
        out["verification"] = {"ok": d.verification.ok, "detail": d.verification.detail}
    for r in d.reviews:
        if r.digest == digest and not r.superseded:
            out["reviews"].append({"role": r.role, "verdict": r.verdict.value, **_review_report(layout, r.report)})
    return out
