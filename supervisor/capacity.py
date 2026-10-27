"""Capacity facade over the vendored usage_policy: windows per provider under state/runtime, reserve from routing.yaml."""
from __future__ import annotations

from datetime import datetime, timezone

import yaml

from ._vendor import usage_policy as up
from ._vendor.contract import CapacityDeferred
from .paths import Layout

_configured_for: tuple | None = None       # (runtime dir, routing.yaml stamp) the floors were last read for


def _stamp(layout: Layout) -> tuple:
    """routing.yaml as last written: (modification time in ns, size), or () when absent or unreadable."""
    try:
        st = (layout.config / "routing.yaml").stat()
    except OSError:
        return ()
    return (st.st_mtime_ns, st.st_size)


def configure(layout: Layout) -> None:
    """Read the floors from routing.yaml (`capacity.<engine>.reserve_percent`, the 5-hour window, and
    `weekly_gate_percent`, the 7-day window) into the usage policy. Absent ones take the defaults again."""
    global _configured_for
    stamp = _stamp(layout)
    reserve = {"claude": 10, "codex": 10}
    weekly = {"claude": 0, "codex": 0}
    cfg = layout.config / "routing.yaml"
    if cfg.exists():
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        cap = data.get("capacity") or {}
        for eng in reserve:
            if isinstance(cap.get(eng), dict):
                if "reserve_percent" in cap[eng]:
                    reserve[eng] = int(cap[eng]["reserve_percent"])
                if "weekly_gate_percent" in cap[eng]:
                    weekly[eng] = int(cap[eng]["weekly_gate_percent"])
    up.configure(layout.runtime, reserve, weekly)
    _configured_for = (str(layout.runtime), stamp)


def ensure(layout: Layout) -> None:
    """Configure for this project, and again whenever routing.yaml changed since (Settings or a hand edit): a
    floor the owner changes applies to the next dispatch of a running supervisor, not after a restart. Before
    1.11.0 the floors were read once per process."""
    if _configured_for != (str(layout.runtime), _stamp(layout)):
        configure(layout)


def _held_because(engine: str, window: dict) -> str:
    reset = datetime.fromtimestamp(window["reset_epoch"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    remaining = f"{window.get('remaining_percent', 0):g}% remaining"
    if window.get("floor_setting"):
        return (f"{window['label']} window at {remaining}, at or below its floor {window['floor']:g}% "
                f"(routing.yaml {window['floor_setting']}); resets {reset}")
    return f"{window['label']} window exhausted ({remaining}); resets {reset}"


def require(layout: Layout, engine: str) -> None:
    """Raise CapacityDeferred if the engine's measured windows gate dispatch now. The message names each window
    that holds dispatch, its remaining share, the floor and the routing.yaml setting that holds it, and when the
    window resets: what the owner needs to decide whether to change the floor or wait."""
    ensure(layout)
    until = up.reserve_until(engine)
    if until is not None:
        held = [w for w in windows(layout).get(engine, {}).get("windows", []) if w.get("gating")]
        why = "; ".join(_held_because(engine, w) for w in held) or "a measured window is at its floor"
        raise CapacityDeferred(until, f"{engine} capacity held: {why}")


def ingest_claude_stream(layout: Layout, path, offset=0) -> int:
    ensure(layout)
    return up.ingest_claude_stream(path, offset)


def lines(layout: Layout) -> list[str]:
    ensure(layout)
    return up.capacity_lines()


def windows(layout: Layout) -> dict:
    """Per engine, the measured windows; each says whether it holds dispatch (`gating`), its `floor` and the
    routing.yaml key that sets the floor (`floor_setting`, e.g. capacity.<engine>.weekly_gate_percent, or None)."""
    ensure(layout)
    out = up.capacity_windows()
    for engine, data in out.items():
        for w in data.get("windows", []):
            key = w.pop("floor_key", None)
            w["floor_setting"] = f"capacity.{engine}.{key}" if key else None
    return out


def owner_reset(layout: Layout, engine: str, message: str = "") -> list:
    ensure(layout)
    return up.owner_reset(engine, message)
