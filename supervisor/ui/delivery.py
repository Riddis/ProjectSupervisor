"""What the owner is asked to approve, shown in full: the files a delivery digest covers, the verification and the
live reviews of that digest (verdict, summary, findings), and any declared artifact the digest does not cover.
Shared by the inbox's approval cards and the task page. Every path comes from the task record, never from the
browser; a file is opened through `/tasks/<id>/files/<n>?digest=<digest>`, an index into the task's own `paths`
bound to the delivery it was shown for.

What is shown is what was frozen: the delivered paths are hashed again (`frozen()`) and must still produce the
digest, and every file's bytes are read once, hashed and used only when they match their manifest entry
(`read_frozen()`), so a file edited after the delivery was recorded is never displayed under its verification and
reviews. A review's summary and findings come from its report file only while that still hashes to the
`report_sha256` recorded with the review (tasks.record_review): a replaced report never speaks for the reviewer."""
from __future__ import annotations

import fnmatch
import hashlib
import html
import json
from pathlib import Path

from .. import tasks as tasks_mod
from .._vendor.contract import digest as digest_of, manifest
from ..models.task import Task
from ..paths import Layout
from . import thumbs

TEXT_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".json", ".jsonl", ".toml", ".py", ".csv", ".ini", ".cfg", ".log",
                 ".ps1", ".sh", ".html", ".css", ".js"}
TEXT_LIMIT = 200_000          # a whole decision record or config file; beyond this, open the file itself
MAX_THUMBS = 24               # a delivery of hundreds of images shows the first ones as thumbnails, the rest as links
REVIEW_TEXT_LIMIT = 4000
MAX_FILE_BYTES = 64 * 1024 * 1024   # a larger delivered file is listed, not read into memory to be verified and served


def frozen(layout: Layout, task: Task, digest: str) -> dict | None:
    """The manifest of the task's delivered paths as they are now, when it still hashes to `digest`; None when any
    of them changed, vanished, became a link or cannot be read (contract.manifest refuses links)."""
    if not digest or not task.paths:
        return None
    try:
        entries = manifest([layout.root / p for p in task.paths], root=layout.root)
    except (OSError, ValueError):
        return None
    return entries if digest_of(entries) == digest else None


def read_frozen(layout: Layout, rel: str, entries: dict) -> bytes | None:
    """The bytes of delivered file `rel` exactly as frozen: read once, hashed, and returned only when they match its
    manifest entry, so what is shown or served is what was hashed (no gap between a check and a later read). The
    path is checked for links before anything is opened."""
    entry = entries.get(Path(str(rel)).as_posix())
    if not isinstance(entry, dict) or entry.get("kind") != "file" or int(entry.get("size") or 0) > MAX_FILE_BYTES:
        return None
    return read_verified(layout, rel, entry.get("sha256"), MAX_FILE_BYTES)


def read_verified(layout: Layout, rel: str, sha256: str | None, limit: int) -> bytes | None:
    """The bytes of `rel`, read once through a link-free path (tasks.read_linkfree), only when they hash to `sha256`."""
    data = tasks_mod.read_linkfree(layout, rel, limit) if sha256 else None
    return data if data is not None and hashlib.sha256(data).hexdigest() == sha256 else None


def file_view(layout: Layout, task_id: str, index: int, rel: str, entries: dict, digest: str,
              thumb: bool = True) -> dict:
    view = {"ref": rel, "href": f"/tasks/{task_id}/files/{index}?digest={digest}", "kind": "link"}
    data = read_frozen(layout, rel, entries)
    if data is None:
        return {**view, "kind": "missing"}
    suffix = Path(str(rel)).suffix.lower()
    if suffix in thumbs.IMAGE_SUFFIXES:
        key = thumbs.ensure_bytes(layout, data) if thumb else None
        return {**view, "kind": "image", "thumb_key": key} if key else view
    if suffix in TEXT_SUFFIXES:
        text = data.decode("utf-8", errors="replace")
        truncated = len(text) > TEXT_LIMIT
        return {**view, "kind": "text", "html": html.escape(text[:TEXT_LIMIT]), "truncated": truncated,
                "lines": text.count("\n") + 1}
    return view


def uncovered(task: Task) -> list[str]:
    """Declared artifacts (`task.artifacts`, names or globs) that match none of the files the digest covers."""
    paths = list(task.paths)
    return [a for a in task.artifacts
            if not any(p == a or fnmatch.fnmatchcase(p, a) for p in paths)]


def _review_report(layout: Layout, ref: str | None, sha256: str | None) -> dict:
    """The reviewer's summary and findings, from the report file exactly as it was when the review was recorded
    (`ReviewRecord.report_sha256`). `report_state` says why they are missing: `unverified` (no hash was recorded,
    e.g. a review recorded before 1.10.0) or `changed` (the file no longer hashes to it, is gone, or is a link)."""
    if not ref:
        return {}
    if not sha256:
        return {"report_state": "unverified"}
    data = read_verified(layout, ref, sha256, tasks_mod.MAX_REPORT_BYTES)
    if data is None:
        return {"report_state": "changed"}
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {"report_state": "changed"}
    if not isinstance(doc, dict):
        return {}
    findings = [{"category": str(f.get("category") or ""), "blocking": bool(f.get("blocking")),
                 "message": str(f.get("message") or "")[:REVIEW_TEXT_LIMIT]}
                for f in (doc.get("findings") or []) if isinstance(f, dict)]
    return {"summary": str(doc.get("summary") or "")[:REVIEW_TEXT_LIMIT], "findings": findings}


def view(layout: Layout, task: Task, digest: str | None = None) -> dict:
    """The delivery behind `digest` (default: the task's current one). `current` is False when the task has moved on
    to another digest since — the files on disk then belong to the newer delivery, so none are shown. `intact` is
    False when the task still records this digest but its delivered files no longer hash to it (edited, removed or
    replaced by a link after the delivery was recorded): nothing is shown either, only that warning."""
    d = task.delivery
    digest = digest or d.digest
    current = bool(digest) and digest == d.digest
    out = {"digest": digest, "current": current, "intact": False, "files": [], "uncovered": [],
           "verification": None, "reviews": []}
    if not current:
        return out
    entries = frozen(layout, task, digest)
    if entries is None:
        return out
    out["intact"] = True
    shown_images = 0
    for i, rel in enumerate(task.paths):
        fv = file_view(layout, task.id, i, rel, entries, digest, thumb=shown_images < MAX_THUMBS)
        if fv["kind"] == "image":
            shown_images += 1
        out["files"].append(fv)                    # images past MAX_THUMBS are links, not thumbnails
    out["uncovered"] = uncovered(task)
    if d.verification and d.verification.digest == digest:
        out["verification"] = {"ok": d.verification.ok, "detail": d.verification.detail}
    for r in d.reviews:
        if r.digest == digest and not r.superseded:
            out["reviews"].append({"role": r.role, "verdict": r.verdict.value, **_review_report(layout, r.report, r.report_sha256)})
    return out
