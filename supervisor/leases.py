"""Machine-wide resource leases: `resources.<name>.lease` in a project's `config/routing.yaml`
(`supervisor.plugin.resource_lease`, docs/PLUGIN_API.md). A re-entrant lock over a resource that two projects'
own loops might otherwise grab at once (two GPUs, one machine, each project running its own supervisor) — not a
second lock discipline per project, one mechanism in the core that every project opts into with a `lease:` block.

The lease is held per *project* (a stable hash of its root, never a name the framework writes), not per process:
several processes of the same project (the loop launching a job, that job's own long-lived server watchdog) hold
it re-entrantly, each with its own `{pid, create_time}` entry so a crash of one does not drop the others'. Absent
`lease:` configuration, every method below is a no-op that already has it — the behaviour of every project that
configures nothing.

File format (JSON, at `lease.path`, outside every repository):

    {"entries": [{"holder": "<hash>", "label": "<project label>", "pid": 123, "create_time": 456.0,
                  "since": 789.0, "renewed": 789.0}, ...],
     "release_requested_by": "<hash>" | null, "release_requested_label": "<label>" | null,
     "release_requested_at": <epoch> | null}
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from ._vendor.contract import atomic_json, read_json
from .locks import FileLock
from .paths import Layout

_POLL_S = 0.05


def _holder_id(layout: Layout) -> str:
    return hashlib.sha256(str(layout.root.resolve()).encode("utf-8")).hexdigest()[:16]


def _create_time(pid: int) -> float:
    import psutil
    return psutil.Process(pid).create_time()


def _pid_alive(pid, create_time) -> bool:
    import psutil
    if not isinstance(pid, int) or create_time is None:
        return False
    try:
        p = psutil.Process(pid)
        return abs(p.create_time() - float(create_time)) < 2.0 and p.is_running()
    except (psutil.Error, ValueError, OSError):
        return False


@dataclass(frozen=True)
class LeaseConfig:
    path: Path
    label: str
    stale_after_s: float = 120.0
    wait_s: float = 60.0
    hand_over_timeout_s: float = 900.0


def _lease_config(layout: Layout, name: str) -> LeaseConfig | None:
    """`resources.<name>.lease` read straight from `config/routing.yaml` (never through the plugin config facade:
    core callers — `supervisor.gate`, `supervisor.doctor` — are not a plugin's own code)."""
    cfg_path = layout.config / "routing.yaml"
    if not cfg_path.exists():
        return None
    doc = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    resources = doc.get("resources") if isinstance(doc, dict) else None
    section = (resources or {}).get(name) if isinstance(resources, dict) else None
    lease = (section or {}).get("lease") if isinstance(section, dict) else None
    if not lease:
        return None
    if not lease.get("path"):
        raise ValueError(f"resources.{name}.lease.path is required")
    return LeaseConfig(path=Path(lease["path"]), label=str(lease.get("label") or name),
                       stale_after_s=float(lease.get("stale_after_s", 120.0)),
                       wait_s=float(lease.get("wait_s", 60.0)),
                       hand_over_timeout_s=float(lease.get("hand_over_timeout_s", 900.0)))


def _lock_path(cfg: LeaseConfig) -> Path:
    return cfg.path.with_suffix(cfg.path.suffix + ".lock")


def _read(cfg: LeaseConfig) -> dict:
    try:
        doc = read_json(cfg.path)
    except (OSError, ValueError):
        return {"entries": [], "release_requested_by": None, "release_requested_label": None,
                "release_requested_at": None}
    doc.setdefault("entries", [])
    doc.setdefault("release_requested_by", None)
    doc.setdefault("release_requested_label", None)
    doc.setdefault("release_requested_at", None)
    return doc


def _live_entries(doc: dict, cfg: LeaseConfig, now: float) -> list[dict]:
    return [e for e in doc.get("entries", [])
            if now - float(e.get("renewed", e.get("since", 0))) <= cfg.stale_after_s
            and _pid_alive(e.get("pid"), e.get("create_time"))]


class ResourceLease:
    """One project's handle on a named machine-wide resource lease (`supervisor.plugin.resource_lease`).
    `acquire`/`renew`/`release` act for this process; several `ResourceLease` instances of one project (one per
    process) are independent entries of the same holder, so one releasing never drops another's hold."""

    def __init__(self, layout: Layout, name: str):
        self.name = name
        self._layout = layout
        self._cfg = _lease_config(layout, name)
        self._holder = _holder_id(layout)
        self._pid = os.getpid()
        self._create_time = _create_time(self._pid) if self._cfg else 0.0

    def acquire(self, wait_s: float | None = None) -> bool:
        """True once this process holds an entry (immediately when no lease is configured, when this project
        already holds it, or when it is free and no other project's fresher release request is still inside its
        hand-over window). Polls every 50 ms up to `wait_s` (the config's own `wait_s` by default; `0` tries once
        and returns immediately) before reporting unavailable."""
        if self._cfg is None:
            return True
        deadline = time.monotonic() + (self._cfg.wait_s if wait_s is None else wait_s)
        while True:
            if self._try_take():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_S)

    def _try_take(self) -> bool:
        cfg = self._cfg
        with FileLock(_lock_path(cfg), owner="resource-lease", timeout=5.0):
            now = time.time()
            doc = _read(cfg)
            live = _live_entries(doc, cfg, now)
            others = {e["holder"] for e in live if e["holder"] != self._holder}
            if others:
                self._record_waiting(cfg, doc, now)
                return False
            pending = doc.get("release_requested_by")
            if (pending and pending != self._holder
                    and now - float(doc.get("release_requested_at") or 0) < cfg.hand_over_timeout_s):
                return False                      # free, but another project's turn has not timed out yet
            mine = [e for e in live if e["holder"] == self._holder]
            mine.append({"holder": self._holder, "label": cfg.label, "pid": self._pid,
                         "create_time": self._create_time, "since": now, "renewed": now})
            doc["entries"] = mine
            if doc.get("release_requested_by") == self._holder:
                doc["release_requested_by"] = None
                doc["release_requested_label"] = None
                doc["release_requested_at"] = None
            atomic_json(cfg.path, doc)
            return True

    def _record_waiting(self, cfg: LeaseConfig, doc: dict, now: float) -> None:
        if doc.get("release_requested_by") != self._holder:
            doc["release_requested_by"] = self._holder
            doc["release_requested_label"] = cfg.label
            doc["release_requested_at"] = now
            atomic_json(cfg.path, doc)

    def renew(self) -> None:
        """Refresh this process's own entry so it does not go stale while the work it covers continues."""
        if self._cfg is None:
            return
        cfg = self._cfg
        with FileLock(_lock_path(cfg), owner="resource-lease", timeout=5.0):
            doc = _read(cfg)
            now = time.time()
            found = False
            for e in doc.get("entries", []):
                if e.get("holder") == self._holder and e.get("pid") == self._pid:
                    e["renewed"] = now
                    found = True
            if found:
                atomic_json(cfg.path, doc)

    def release(self) -> None:
        """Drop this process's own entry. Harmless when it holds none (never acquired, already released, or no
        lease configured)."""
        if self._cfg is None:
            return
        cfg = self._cfg
        with FileLock(_lock_path(cfg), owner="resource-lease", timeout=5.0):
            doc = _read(cfg)
            before = len(doc.get("entries", []))
            doc["entries"] = [e for e in doc.get("entries", [])
                              if not (e.get("holder") == self._holder and e.get("pid") == self._pid)]
            if len(doc["entries"]) != before:
                atomic_json(cfg.path, doc)

    def release_requested(self) -> bool:
        """True when another project is waiting for this one to let go: the holder's own code (a long-lived
        server watchdog) checks this to finish up and release early rather than wait for its idle timeout."""
        if self._cfg is None:
            return False
        doc = _read(self._cfg)
        pending = doc.get("release_requested_by")
        return bool(pending and pending != self._holder)


def status(layout: Layout, name: str) -> dict:
    """A read-only snapshot for `supervisor doctor`: `{"configured": False}`, or `{"configured": True, "path",
    "live": [{"label", "pid"}, ...], "stale": <pruned count>, "waiting": <label or None>}`."""
    cfg = _lease_config(layout, name)
    if cfg is None:
        return {"configured": False}
    doc = _read(cfg)
    live = _live_entries(doc, cfg, time.time())
    waiting = doc.get("release_requested_label") if doc.get("release_requested_by") else None
    return {"configured": True, "path": str(cfg.path),
            "live": [{"label": e["label"], "pid": e["pid"]} for e in live],
            "stale": len(doc.get("entries", [])) - len(live), "waiting": waiting}


def holder_label(layout: Layout, name: str) -> str | None:
    """The label of whichever project currently holds `resources.<name>.lease` live, when it is not this one;
    `None` when it is free, held by this project, or no lease is configured. A read-only peek for
    `supervisor.gate`'s launch check — the loop takes the lease for real only right before `submit()`."""
    cfg = _lease_config(layout, name)
    if cfg is None:
        return None
    me = _holder_id(layout)
    others = [e for e in _live_entries(_read(cfg), cfg, time.time()) if e["holder"] != me]
    return others[0]["label"] if others else None


def _record_waiting_from_peek(layout: Layout, name: str) -> None:
    """Record this project as waiting on `resources.<name>.lease`, for a caller that only ever peeks
    (`holder_label`, from `supervisor.gate.lease_blocked`) and so never reaches `ResourceLease._try_take()` — the
    only place this was previously written. Internal to the core (not part of the plugin-facing lease handle);
    a no-op when unconfigured, when nobody else holds it live right now, or when this project's own request is
    already on file, so repeated calls while the same hand-over window is open never push `release_requested_at`
    forward."""
    cfg = _lease_config(layout, name)
    if cfg is None:
        return
    holder = _holder_id(layout)
    with FileLock(_lock_path(cfg), owner="resource-lease", timeout=5.0):
        now = time.time()
        doc = _read(cfg)
        live = _live_entries(doc, cfg, now)
        if not any(e["holder"] != holder for e in live):
            return
        if doc.get("release_requested_by") != holder:
            doc["release_requested_by"] = holder
            doc["release_requested_label"] = cfg.label
            doc["release_requested_at"] = now
            atomic_json(cfg.path, doc)


def resource_lease(layout: Layout, name: str) -> ResourceLease:
    """The project's handle on the machine-wide lease `resources.<name>.lease` names (docs/PLUGIN_API.md). Reads
    configuration fresh each call; the returned handle acts for this process from here on."""
    return ResourceLease(layout, name)
