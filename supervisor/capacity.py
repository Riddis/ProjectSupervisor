"""Capacity facade over the vendored usage_policy: windows per provider under state/runtime, reserve from routing.yaml."""
from __future__ import annotations

import yaml

from ._vendor import usage_policy as up
from ._vendor.contract import CapacityDeferred
from .paths import Layout

_configured_for: str | None = None


def configure(layout: Layout) -> None:
    global _configured_for
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
    _configured_for = str(layout.runtime)


def ensure(layout: Layout) -> None:
    if _configured_for != str(layout.runtime):
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
