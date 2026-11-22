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
their cap and get an idle limit only when the job sets `idle_timeout_s`. A project that wants an upper bound on what
such a job may declare for itself sets one explicitly, per lane, in routing.yaml `timeouts.lane_caps` (`lane_cap()`,
read by the write-time preflight); nothing bounds it by default.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import psutil

from ._vendor.contract import atomic_json, read_json

DEFAULTS = {"idle_s": 1200, "hard_cap_s": 21600, "idle_cpu_s": 5.0}
PROCESS_CAP_S = 3600            # non-model process jobs: unchanged default cap
EXTEND_STEP_S = 3600            # one click on Extend, when a project configures no timeouts.extend_offers
MAX_HARD_CAP_S = 86400          # timeouts.max_hard_cap_s default (docs/plan/LONG_WORK.md section 4.2)
NEEDS_HEADROOM = 1.25           # timeouts.needs_headroom default: the derived-cap multiplier over a declared duration
EXTEND_DEDUPE_S = 10            # timeouts.extend_dedupe_s default: a second same-duration Extend inside this window is a no-op
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
    for key in ("idle_s", "hard_cap_s", "idle_cpu_s", "max_hard_cap_s"):
        if key in block:
            out[key] = block[key]


def needs_headroom(routing_cfg: dict) -> float:
    """`timeouts.needs_headroom` (docs/plan/LONG_WORK.md section 4.2): the multiplier a declared `duration_min`
    is derived into a budget through, default 1.25 -- project-wide, not merged per cost category or role."""
    cfg = (routing_cfg or {}).get("timeouts") or {}
    return max(1.0, _num(cfg.get("needs_headroom"), NEEDS_HEADROOM))


def ceiling_s(routing_cfg: dict, cost_category: str | None = None, role_prof: dict | None = None,
             capabilities: dict | None = None) -> int:
    """The one composed ceiling (docs/plan/LONG_WORK.md section 3.2/4.2, D-028 decision 4): the minimum of the
    resolved `timeouts.max_hard_cap_s` (the same per-cost, per-role `_merge` chain as every other timeout key,
    default `MAX_HARD_CAP_S`) and the lane's declared `max_duration_min * 60`, where the lane declares one. A
    lane declaring a *higher* number than the project's neither refuses nor grants: the minimum always picks the
    project's. Read by RF-28, the derived-cap clamp inside `resolve()`, RF-32 (`agent.extend`) and RF-08's
    `process`-row cap comparison (`preflight._timeout_checks`) -- four readers, one number."""
    out: dict = {"max_hard_cap_s": MAX_HARD_CAP_S}
    cfg = (routing_cfg or {}).get("timeouts") or {}
    _merge(out, cfg)
    if cost_category:
        _merge(out, (cfg.get("by_cost") or {}).get(str(cost_category)))
    _merge(out, (role_prof or {}).get("timeouts"))
    resolved = max(60, int(_num(out.get("max_hard_cap_s"), MAX_HARD_CAP_S)))
    lane_max_min = (capabilities or {}).get("max_duration_min")
    if lane_max_min is not None:
        try:
            resolved = min(resolved, max(60, int(lane_max_min) * 60))
        except (TypeError, ValueError):
            pass
    return resolved


def extend_offers(routing_cfg: dict) -> list[int]:
    """`timeouts.extend_offers` (docs/plan/LONG_WORK.md section 4.3): the durations in seconds the Control
    Center's Extend renders one control per -- `[EXTEND_STEP_S]` (today's single +1h) for a project that
    configures nothing or configures nothing usable."""
    cfg = (routing_cfg or {}).get("timeouts") or {}
    offers = cfg.get("extend_offers")
    out = []
    if isinstance(offers, list):
        for o in offers:
            if isinstance(o, int) and not isinstance(o, bool) and 0 < o <= 24 * 3600:
                out.append(o)
    return out or [EXTEND_STEP_S]


def extend_dedupe_s(routing_cfg: dict) -> float:
    """`timeouts.extend_dedupe_s` (docs/plan/LONG_WORK.md section 4.3), default `EXTEND_DEDUPE_S`: a second
    extension of the same duration on the same job inside this window is a no-op (`commands._agent_extend`)."""
    cfg = (routing_cfg or {}).get("timeouts") or {}
    return max(0.0, _num(cfg.get("extend_dedupe_s"), EXTEND_DEDUPE_S))


def resolve(routing_cfg: dict, cost_category: str | None = None, role_prof: dict | None = None,
            job: dict | None = None, *, needs: dict | None = None, lane: str = "model",
            capabilities: dict | None = None) -> dict:
    """The limits for one job: {"idle_s": int | None, "hard_cap_s": int, "idle_cpu_s": float,
    "clamped_from_s": int?}. `lane="model"` is every pre-2.9.0 call: `DEFAULTS` -> routing `timeouts` ->
    `by_cost.<cost_category>` -> the role's `timeouts` -> a derived cap from a declared `needs.duration_min`
    (docs/plan/LONG_WORK.md section 4.2) -> the job block, each clamped to `ceiling_s()`.
    `lane="plain"` (a non-model `process`/`python` job, `ProcessAdapter.prepare`) starts from `PROCESS_CAP_S` with
    no idle default and skips every routing/role merge above -- policy a process job has never read -- then
    applies the same derived cap, job block and clamp. `needs`/`capabilities` default to `None`/`{}`, which
    derives nothing and clamps to the project's own `max_hard_cap_s` alone: the pre-2.9.0 behaviour on both
    lanes. `clamped_from_s` is present only when the final clamp lowered a *declared* `job.timeout_s` -- the
    receipt a silent clamp would owe the owner (D-028 decision 4)."""
    if lane == "plain":
        out = {"hard_cap_s": PROCESS_CAP_S, "idle_s": None, "idle_cpu_s": DEFAULTS["idle_cpu_s"]}
    else:
        out = dict(DEFAULTS)
        cfg = (routing_cfg or {}).get("timeouts") or {}
        _merge(out, cfg)
        if cost_category:
            _merge(out, (cfg.get("by_cost") or {}).get(str(cost_category)))
        _merge(out, (role_prof or {}).get("timeouts"))
    job = job or {}
    declared_timeout = job.get("timeout_s")
    duration_min = (needs or {}).get("duration_min")
    if duration_min is not None and declared_timeout is None:
        derived = math.ceil(duration_min * 60 * needs_headroom(routing_cfg))
        out["hard_cap_s"] = derived
    ceiling = ceiling_s(routing_cfg, cost_category, role_prof, capabilities)
    out["hard_cap_s"] = min(out["hard_cap_s"], ceiling)      # derive and clamp
    if declared_timeout is not None:
        out["hard_cap_s"] = declared_timeout                 # an explicit timeout_s still wins (D-028 decision 4)
    if "idle_timeout_s" in job:
        out["idle_s"] = job["idle_timeout_s"]
    clamped_from_s = None
    if out["hard_cap_s"] > ceiling:
        if declared_timeout is not None and declared_timeout > ceiling:
            clamped_from_s = declared_timeout
        out["hard_cap_s"] = ceiling                           # clamp once more: the one place a model-lane
                                                                # timeout_s above the ceiling is actually enforced
    idle = _num(out.get("idle_s"), 0)
    result = {"idle_s": int(idle) if idle > 0 else None,
              "hard_cap_s": max(60, int(_num(out.get("hard_cap_s"), DEFAULTS["hard_cap_s"]))),
              "idle_cpu_s": max(0.5, _num(out.get("idle_cpu_s"), DEFAULTS["idle_cpu_s"]))}
    if clamped_from_s is not None:
        result["clamped_from_s"] = clamped_from_s
    return result


def lane_cap(routing_cfg: dict, adapter: str | None) -> int | None:
    """The declared ceiling on a non-model job's own `timeout_s` for one adapter lane: routing.yaml
    `timeouts.lane_caps.<adapter>`, and None unless a project declares one.

    `PROCESS_CAP_S` is deliberately not that ceiling: it is the duration a process job that declares no
    `timeout_s` *gets* (`adapters.process`), and a block asking for longer was never illegal -- `extend()`
    exists to push a deadline out further still. A write-time refusal here can therefore only come from a cap
    the project itself set, never from the lane's own default (RECOVERY_PLAN.md section 3.3's "timeout_s <= the
    lane's cap")."""
    caps = ((routing_cfg or {}).get("timeouts") or {}).get("lane_caps")
    value = caps.get(str(adapter)) if isinstance(caps, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


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
