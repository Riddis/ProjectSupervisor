"""Capacity floors that take effect (1.11.0, owner request 2026-10-27). routing.yaml has two floors per engine:
`capacity.<engine>.reserve_percent` (the 5-hour window) and `capacity.<engine>.weekly_gate_percent` (the 7-day
window). A floor changed in Settings, or by hand in the file, applies to the next dispatch of a running supervisor,
without a restart."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest
import yaml

from supervisor import capacity, settings, testing
from supervisor._vendor import usage_policy as up
from supervisor._vendor.contract import CapacityDeferred

ENGINE = "claude"
HOUR = 3600


def routing(layout, reserve=1, weekly=20, mtime_bump=True):
    doc = {"capacity": {ENGINE: {"reserve_percent": reserve, "weekly_gate_percent": weekly},
                        "codex": {"reserve_percent": 10, "weekly_gate_percent": 20}},
           "limits": {"max_agent_jobs": 2, "max_resource_jobs": 1}}
    path = layout.config / "routing.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.stat().st_mtime_ns if path.exists() else None
    path.write_text("# owner decision comments live here\n" + yaml.safe_dump(doc), encoding="utf-8")
    if mtime_bump and before is not None and path.stat().st_mtime_ns == before:
        os.utime(path, ns=(before + 1_000_000, before + 1_000_000))      # a coarse clock must not hide the edit


def observe(layout, seven_day_used=82.0, five_hour_used=10.0, reset_in=2 * 24 * HOUR):
    """A fresh measurement: 18 % of the 7-day window left, the 5-hour window nearly unused."""
    capacity.ensure(layout)
    now = time.time()
    return up.record(ENGINE, [{"name": "seven_day", "used_percent": seven_day_used, "reset_epoch": now + reset_in},
                              {"name": "five_hour", "used_percent": five_hour_used, "reset_epoch": now + HOUR}],
                     "test", now)


@pytest.fixture
def lay(layout):
    routing(layout)
    capacity.configure(layout)
    return layout


def test_a_floor_changed_in_settings_reaches_a_running_supervisor(lay):
    settings.update(lay, "capacity.claude.reserve_percent", "20")
    observe(lay, seven_day_used=10.0, five_hour_used=85.0)              # 15 % of the 5-hour window left
    sup = testing.drive(lay, iterations=1)                              # a supervisor in this process, running
    with pytest.raises(CapacityDeferred):
        capacity.require(lay, ENGINE)                                   # 15 % <= the 20 % reserve
    settings.update(lay, "capacity.claude.reserve_percent", "10")       # same process: no restart
    capacity.require(lay, ENGINE)                                       # 15 % > 10 %: dispatch goes ahead
    sup.once()


def test_a_floor_edited_by_hand_reaches_a_running_supervisor(lay):
    observe(lay)
    with pytest.raises(CapacityDeferred):
        capacity.require(lay, ENGINE)
    routing(lay, weekly=10)                                             # same file size: only the stamp changes
    capacity.require(lay, ENGINE)
    routing(lay, weekly=30)
    with pytest.raises(CapacityDeferred):
        capacity.require(lay, ENGINE)


def test_a_removed_floor_falls_back_to_the_default(lay):
    observe(lay, seven_day_used=95.0)                                   # 5 % left of the 7-day window
    with pytest.raises(CapacityDeferred):
        capacity.require(lay, ENGINE)
    (lay.config / "routing.yaml").write_text("limits: {max_agent_jobs: 2}\n", encoding="utf-8")
    capacity.require(lay, ENGINE)                                       # default 7-day floor: 0 (only when spent)


def test_settings_apply_the_floor_even_when_the_file_stamp_does_not_move(lay, monkeypatch):
    """A save within the filesystem's clock resolution keeps (mtime, size): Settings reconfigures directly."""
    monkeypatch.setattr(capacity, "_stamp", lambda layout: ("frozen",))
    capacity.configure(lay)
    settings.update(lay, "capacity.claude.reserve_percent", "20")
    observe(lay, seven_day_used=10.0, five_hour_used=85.0)
    with pytest.raises(CapacityDeferred):
        capacity.require(lay, ENGINE)
    settings.update(lay, "capacity.claude.reserve_percent", "10")
    capacity.require(lay, ENGINE)
