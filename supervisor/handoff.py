"""Which prior HANDOFF.md (if any) a new job should be pointed at — one rule per job kind (R-15; the
attempt-identity design, §4.1). The old selector picked the newest HANDOFF.md by modification time across every
other run directory of the task, whatever its kind, which is how a reviewer ended up reading the producer's own
account of the work it was judging. `_vendor/handoff.py` (the vendored continuity-protocol text) is unrelated and
untouched: it already omits the "read this first" line when it is given no path."""
from __future__ import annotations

from .paths import Layout


def _handoff_of(layout: Layout, task_id: str, job_id: str | None) -> str | None:
    if not job_id:
        return None
    p = layout.runs / task_id / job_id / "HANDOFF.md"
    return str(p) if p.exists() else None


def select(layout: Layout, task_id: str, attempt: int | None, kind: str, seq: int | None, parent: str | None) -> str | None:
    """`attempt` and `seq` number the delivery attempt and the session inside it; `parent` is the job this one's
    cause names (the session being continued, the failure a diagnosis explains). The caller decides what to pass
    as `parent` for a revision (session 1 of an attempt after the first): only the previous attempt's last
    session, and only when that attempt produced no delivery at all — in every other case it passes `None`, so
    this function need not re-derive that from the store itself.

    - a review (or anything that is neither a delivery nor a diagnosis) is given nothing, ever;
    - a diagnosis is given its parent's handoff, if the parent wrote one;
    - a delivery session after the first in its attempt (`seq > 1`: a capacity or time-limit continuation) is
      given its own predecessor's handoff, and nothing else;
    - a delivery session 1 of an attempt after the first is given nothing, unless the caller has already decided
      otherwise by passing `parent`.
    """
    if kind == "diagnosis":
        return _handoff_of(layout, task_id, parent)
    if kind == "delivery" and seq and seq > 1:
        return _handoff_of(layout, task_id, parent)
    if kind == "delivery" and (not seq or seq == 1) and attempt and attempt > 1:
        return _handoff_of(layout, task_id, parent)
    return None
