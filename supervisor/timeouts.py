"""Activity-based time limits for subprocess jobs (1.7.0).

Before 1.7.0 every claude_cli / codex_cli / process job had one wall-clock deadline, 3600 s after launch: a long
agent session was killed mid-work at exactly one hour, and the kill spent a revision attempt.

Now a job is stopped on time only when it is
- **idle**: its output files have not grown and its process tree has used less than `idle_cpu_s` CPU seconds for
  `idle_s` seconds (a stalled API call, a hung tool); or
- **past its hard cap**: `hard_cap_s` after launch, plus whatever the owner added with **Extend** in the Control
  Center (`agent.extend`). A model session is asked to write its handoff `HANDOFF_WARN_S` before the cap.

A model-lane timeout is a continuation (a fresh session picks up the HANDOFF.md, `Supervisor.requeue`), not a
failed attempt.

Resolution, later wins: `DEFAULTS` -> routing.yaml `timeouts` -> `timeouts.by_cost.<cost_category>` -> the role's
`timeouts` -> the task's `job` (`timeout_s` = hard cap, `idle_timeout_s` = idle limit). `idle_s: 0` (or null)
switches the idle limit off. Non-model process jobs (verification, scripts) keep `job.timeout_s` (default 3600) as
their cap and get an idle limit only when the job sets `idle_timeout_s`.
"""
from __future__ import annotations

import time
from pathlib import Path

import psutil

from ._vendor.contract import atomic_json, read_json

DEFAULTS = {"idle_s": 1200, "hard_cap_s": 21600, "idle_cpu_s": 5.0}
PROCESS_CAP_S = 3600            # non-model process jobs: unchanged default cap
EXTEND_STEP_S = 3600            # one click on Extend
HANDOFF_WARN_S = 600            # ask a model session to write its handoff this long before the hard cap
ACTIVITY_FILE = "activity.json"
OUTPUT_FILES = ("stdout.jsonl", "stderr.txt")


def _num(value, default):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _merge(out: dict, block) -> None:
    if not isinstance(block, dict):
        return
    for key in ("idle_s", "hard_cap_s", "idle_cpu_s"):
        if key in block:
            out[key] = block[key]


def resolve(routing_cfg: dict, cost_category: str | None = None, role_prof: dict | None = None,
            job: dict | None = None) -> dict:
    """The limits for one model-lane job: {"idle_s": int | None, "hard_cap_s": int, "idle_cpu_s": float}."""
    out = dict(DEFAULTS)
    cfg = (routing_cfg or {}).get("timeouts") or {}
    _merge(out, cfg)
    if cost_category:
        _merge(out, (cfg.get("by_cost") or {}).get(str(cost_category)))
    _merge(out, (role_prof or {}).get("timeouts"))
    job = job or {}
    if job.get("timeout_s") is not None:
        out["hard_cap_s"] = job["timeout_s"]
    if "idle_timeout_s" in job:
        out["idle_s"] = job["idle_timeout_s"]
    idle = _num(out.get("idle_s"), 0)
    return {"idle_s": int(idle) if idle > 0 else None,
            "hard_cap_s": max(60, int(_num(out.get("hard_cap_s"), DEFAULTS["hard_cap_s"]))),
            "idle_cpu_s": max(0.5, _num(out.get("idle_cpu_s"), DEFAULTS["idle_cpu_s"]))}


def spec_fields(limits: dict) -> dict:
    """What a prepared spec carries: `timeout_s` stays the hard cap (the name every reader already uses)."""
    return {"timeout_s": limits["hard_cap_s"], "idle_s": limits["idle_s"], "idle_cpu_s": limits["idle_cpu_s"]}


def tree_cpu(pid: int | None, create_time: float | None) -> float | None:
    """CPU seconds (user + system) used by a live process and its descendants, or None when it cannot be read."""
    if not pid:
        return None
    try:
        p = psutil.Process(pid)
        if create_time is not None and abs(p.create_time() - float(create_time)) > 1.0:
            return None
        procs = [p] + p.children(recursive=True)
    except (psutil.Error, OSError):
        return None
    total = 0.0
    for proc in procs:
        try:
            t = proc.cpu_times()
            total += float(t.user) + float(t.system)
        except (psutil.Error, OSError):
            continue
    return total


def _output_size(run_dir: Path) -> int:
    size = 0
    for name in OUTPUT_FILES:
        try:
            size += (run_dir / name).stat().st_size
        except OSError:
            continue
    return size


def _epoch(iso) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(iso)).timestamp() if iso else None
    except (TypeError, ValueError):
        return None


def last_active(run_dir: Path, handle: dict, idle_cpu_s: float, now: float | None = None) -> float:
    """When the job last showed activity: its output grew, or its process tree used `idle_cpu_s` CPU seconds since
    the previous activity. Persisted in `<run_dir>/activity.json`, so a restarted loop keeps measuring from where
    the last one left off. CPU is measured against a baseline, not per sample: slow, steady work (a download, a
    render driven from a child process) adds up and counts; a hung process at ~0 % never reaches the threshold.

    The first sample opens the observation window at `now`, not at launch: one sample proves nothing about idleness
    (growth and CPU work are differences between two samples). Dating it back to launch killed a busy session that
    predates activity tracking (an upgrade adopting a job already running longer than the idle limit) or whose first
    collection came late — on its very first look, while its output was still growing."""
    now = time.time() if now is None else now
    run_dir = Path(run_dir)
    path = run_dir / ACTIVITY_FILE
    size = _output_size(run_dir)
    cpu = tree_cpu(handle.get("pid"), handle.get("create_time"))
    try:
        st = read_json(path)
    except (OSError, ValueError):
        st = None
    if not isinstance(st, dict) or "last_active" not in st:
        st = {"size": size, "cpu_base": cpu, "last_active": now, "first_seen": now}
        atomic_json(path, st)
        return float(st["last_active"])
    active = size != st.get("size")
    base = st.get("cpu_base")
    if cpu is not None:
        if base is None or cpu < float(base):          # first reading, or a child exited and took its CPU time along
            st["cpu_base"] = cpu
            if not active:
                atomic_json(path, st)
        elif cpu - float(base) >= idle_cpu_s:
            active = True
    if active:
        st = {"size": size, "cpu_base": cpu, "last_active": now}
        atomic_json(path, st)
    return float(st["last_active"])


def read_activity(run_dir) -> float | None:
    """The last recorded activity time (Control Center display); None when nothing was measured yet."""
    try:
        return float(read_json(Path(run_dir) / ACTIVITY_FILE).get("last_active"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def effective_limits(adapter_name: str | None, spec: dict, handle: dict) -> tuple[float, int | None, float]:
    """The one authoritative (hard-cap deadline, idle limit, idle CPU threshold) of a job: what its adapter's
    `limits` enforces. Extend, the saved status, the handoff request and the Control Center all read this instead of
    the raw `handle["deadline"]` — for a job prepared before activity limits the two differ (claude_cli.model_limits
    moves it onto the current policy), and acting on the raw value extended an hour that had long passed."""
    from .adapters import REGISTRY
    spec, handle = spec or {}, handle or {}
    ad = REGISTRY.get(adapter_name or "")
    if ad is not None and callable(getattr(ad, "limits", None)):
        try:
            return ad.limits(spec, handle)
        except Exception:                                # a display/extension path must never fail on a bad config
            pass
    deadline = handle.get("deadline")
    return (float("inf") if deadline is None else float(deadline), spec.get("idle_s"),
            float(spec.get("idle_cpu_s") or DEFAULTS["idle_cpu_s"]))


def extend(handle: dict, seconds: int, command_id: str | None, actor: str, at: str,
           base: float | None = None) -> tuple[dict, bool]:
    """A copy of `handle` with its hard cap pushed back by `seconds` from `base` (the effective deadline,
    `effective_limits`; the raw handle deadline when not given). The result is written as the handle's deadline,
    so a job moved onto the current policy carries its real cap from then on. Idempotent per command id."""
    exts = list(handle.get("extensions") or [])
    if command_id and any(e.get("command_id") == command_id for e in exts):
        return handle, False
    out = dict(handle)
    start = float(handle.get("deadline") or time.time())
    if base is not None and base != float("inf"):
        start = max(start, float(base))
    out["deadline"] = start + int(seconds)
    out["extensions"] = exts + [{"command_id": command_id, "seconds": int(seconds), "by": actor, "at": at}]
    return out, True
