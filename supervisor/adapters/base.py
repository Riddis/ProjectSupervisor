from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Result:
    status: str                      # COMPLETE | PARTIAL | BLOCKED | FAILED | DEFERRED (capacity: re-queue, no attempt consumed)
    digest: Optional[str] = None     # artifact digest the adapter computed (loop re-verifies)
    artifacts: list[str] = field(default_factory=list)
    notes: str = ""
    model: Optional[str] = None
    session_id: Optional[str] = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    reset_epoch: Optional[float] = None   # DEFERRED only: earliest re-dispatch time


class Adapter:
    name = "base"
    gpu = False                      # holds the project's single "heavy local resource" lock while running (a GPU, a build
                                     # farm, a device): at most limits.max_resource_jobs such jobs run at once

    def prepare(self, layout, task, attempt_id: str) -> dict:
        raise NotImplementedError

    def submit(self, spec: dict) -> dict:
        raise NotImplementedError

    def alive(self, handle: dict) -> bool:
        raise NotImplementedError

    def collect(self, spec: dict, handle: dict) -> Optional[Result]:
        raise NotImplementedError

    def recover(self, spec: dict) -> Optional[dict]:
        """After a crash between submit() and the SUBMITTED record: the handle of work already launched for this
        spec (from the run dir), or None if nothing was launched. Reconcile calls this before ever re-submitting."""
        return None

    def terminate(self, handle: dict) -> None:
        pass

    def lane_ready(self, layout) -> bool:
        """For a job this lane deferred with LaneUnavailable: whether the lane could serve it now, before the saved
        deferral time (the owner topped up a balance, raised a budget, ran `lane.reset`). Checked by the loop on
        each launch of such a record; it must be cheap and side-effect free. Default: wait for the time."""
        return False

    def reset_lane(self, layout) -> bool:
        """The owner command `lane.reset {"lane": <name>}`: forget the lane-level unavailability this adapter
        recorded. Returns whether there was any; the default keeps none."""
        return False


REGISTRY: dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    REGISTRY[adapter.name] = adapter
    return adapter


def get(name: str) -> Adapter:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"unknown adapter {name!r}; known: {sorted(REGISTRY)}") from exc
