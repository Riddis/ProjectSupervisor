"""Capacity facade over the vendored usage_policy: windows per provider under state/runtime, reserve from routing.yaml."""
from __future__ import annotations

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


def require(layout: Layout, engine: str) -> None:
    """Raise CapacityDeferred if the engine's measured windows gate dispatch now."""
    ensure(layout)
    until = up.reserve_until(engine)
    if until is not None:
        raise CapacityDeferred(until, f"{engine} capacity reserve reached; resets at {until:.0f}")


def ingest_claude_stream(layout: Layout, path, offset=0) -> int:
    ensure(layout)
    return up.ingest_claude_stream(path, offset)


def lines(layout: Layout) -> list[str]:
    ensure(layout)
    return up.capacity_lines()


def windows(layout: Layout) -> dict:
    ensure(layout)
    return up.capacity_windows()


def owner_reset(layout: Layout, engine: str, message: str = "") -> list:
    ensure(layout)
    return up.owner_reset(engine, message)
