"""Human adapter: writes an inbox item, sets the task to WAITING_FOR_HUMAN, and waits — a human
gate never times out. `collect()` returns None (still waiting) until the owner answers in the Control Center or via
`supervisor inbox answer`; the answer then maps to a delivery status: ACCEPT or free text (the owner's own words are the
deliverable) → COMPLETE; REJECT/MODIFY/REDO → FAILED, with the full response (including any
argument, e.g. "MODIFY shorter summary") recorded as the failure note so a later attempt can act on the feedback —
`FAILED` tasks are re-dispatchable, unlike a silent drop."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .. import inbox as inbox_mod, tasks as tasks_mod
from .._vendor.contract import CapacityDeferred
from ..models.task import TaskStatus
from ..paths import Layout
from .base import Adapter, Result, register

ACCEPTED_KINDS = {"ACCEPT", "text"}
DEFER_SECONDS = 300.0


def job_spec(task) -> dict:
    extra = getattr(task, "model_extra", None) or {}
    return dict(extra.get("job") or {})


class HumanAdapter(Adapter):
    name = "human"

    def prepare(self, layout: Layout, task, attempt_id: str) -> dict:
        job = job_spec(task)
        summary, detail = inbox_mod.fit(str(job.get("summary", task.objective)))
        return {"adapter": "human", "task_id": task.id, "attempt_id": attempt_id, "root": str(layout.root),
                "for_role": job.get("for_role", "owner"), "kind": job.get("kind", "question"),
                "subject": job.get("subject", f"{task.id}: {task.title}"), "summary": summary, "detail": detail,
                "allowed_responses": job.get("allowed_responses"), "free_text": job.get("free_text", True)}

    def submit(self, spec: dict) -> dict:
        layout = Layout(Path(spec["root"]))
        try:
            item = inbox_mod.create(layout, for_role=spec["for_role"], subject=spec["subject"], summary=spec["summary"],
                                    kind=spec.get("kind", "question"), allowed_responses=spec.get("allowed_responses"),
                                    free_text=spec.get("free_text", True), from_="loop", task_id=spec["task_id"],
                                    attempt_id=spec["attempt_id"], detail=spec.get("detail"))
        except inbox_mod.InboxFull as exc:
            raise CapacityDeferred(time.time() + DEFER_SECONDS, str(exc)) from exc
        tasks_mod.set_status(layout, spec["task_id"], TaskStatus.WAITING_FOR_HUMAN, actor="loop",
                             note=f"awaiting {spec['for_role']}: inbox {item['id']}")
        return {"kind": "human", "item_id": item["id"]}

    def recover(self, spec: dict) -> Optional[dict]:
        layout = Layout(Path(spec["root"]))
        item = inbox_mod.find_by_attempt(layout, spec["attempt_id"])
        return {"kind": "human", "item_id": item["id"]} if item else None

    def alive(self, handle: dict) -> bool:
        return True   # a human gate is never "lost": it just waits

    def collect(self, spec: dict, handle: dict) -> Optional[Result]:
        layout = Layout(Path(spec["root"]))
        item = inbox_mod.load(layout, handle["item_id"])
        if item.get("status") != "answered":
            return None
        answer = item.get("answer") or {}
        status = "COMPLETE" if answer.get("kind") in ACCEPTED_KINDS else "FAILED"
        notes = answer.get("response", "")
        return Result(status=status, notes=notes, error=notes if status == "FAILED" else None, raw=item)


register(HumanAdapter())
