"""supervisor.leases: the machine-wide, re-entrant, project-held resource lease behind
`supervisor.plugin.resource_lease` (R4, docs/plugin_api.json). Each test builds two project Layouts pointed at the
same lease file to act as "two projects on one machine" the way the design's GPU-sharing problem is stated."""
from __future__ import annotations

import os
import time

from supervisor import leases, testing


def _routing(path, label, **extra):
    cfg = {"path": str(path), "label": label}
    cfg.update(extra)
    return {"resources": {"gpu": {"lease": cfg}}}


def _project(tmp_path, name, lease_path, label, **extra):
    return testing.project(tmp_path / name, routing=_routing(lease_path, label, **extra))


def test_no_lease_configured_is_a_no_op(tmp_path):
    lay = testing.project(tmp_path / "solo")
    lease = leases.resource_lease(lay, "gpu")
    assert lease.acquire() is True
    assert lease.release_requested() is False
    lease.release()                                    # never raises with nothing configured


def test_acquire_is_exclusive_across_projects(tmp_path):
    lease_file = tmp_path / "shared" / "gpu.lease"
    a = leases.resource_lease(_project(tmp_path, "a", lease_file, "project-a"), "gpu")
    b = leases.resource_lease(_project(tmp_path, "b", lease_file, "project-b"), "gpu")
    assert a.acquire(wait_s=0) is True
    assert b.acquire(wait_s=0) is False                 # a already holds it


def test_acquire_is_reentrant_within_one_project(tmp_path):
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay = _project(tmp_path, "a", lease_file, "project-a")
    first = leases.resource_lease(lay, "gpu")
    second = leases.resource_lease(lay, "gpu")           # a second process of the same project
    assert first.acquire(wait_s=0) is True
    assert second.acquire(wait_s=0) is True              # re-entrant: the loop's own job alongside its watchdog


def test_release_frees_it_for_another_project(tmp_path):
    lease_file = tmp_path / "shared" / "gpu.lease"
    a = leases.resource_lease(_project(tmp_path, "a", lease_file, "project-a"), "gpu")
    b = leases.resource_lease(_project(tmp_path, "b", lease_file, "project-b"), "gpu")
    assert a.acquire(wait_s=0) is True
    assert b.acquire(wait_s=0) is False
    a.release()
    assert b.acquire(wait_s=0) is True


def test_stale_entry_is_pruned(tmp_path):
    """An entry whose pid/create_time names a process that no longer exists (a crash) is pruned, not honoured."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay_b = _project(tmp_path, "b", lease_file, "project-b")
    lease_file.parent.mkdir(parents=True, exist_ok=True)
    import json
    lease_file.write_text(json.dumps({
        "entries": [{"holder": "deadbeefdeadbeef", "label": "ghost", "pid": 999999, "create_time": 12345.0,
                     "since": time.time(), "renewed": time.time()}],
        "release_requested_by": None, "release_requested_label": None, "release_requested_at": None,
    }), encoding="utf-8")
    b = leases.resource_lease(lay_b, "gpu")
    assert b.acquire(wait_s=0) is True


def test_renewal_older_than_stale_after_s_is_pruned(tmp_path):
    lease_file = tmp_path / "shared" / "gpu.lease"
    a_layout = _project(tmp_path, "a", lease_file, "project-a", stale_after_s=1)
    b_layout = _project(tmp_path, "b", lease_file, "project-b", stale_after_s=1)
    a = leases.resource_lease(a_layout, "gpu")
    b = leases.resource_lease(b_layout, "gpu")
    assert a.acquire(wait_s=0) is True
    time.sleep(1.2)                                      # a never renews: its entry goes stale
    assert b.acquire(wait_s=0) is True


def test_release_requested_is_recorded_and_seen_by_the_holder(tmp_path):
    lease_file = tmp_path / "shared" / "gpu.lease"
    a = leases.resource_lease(_project(tmp_path, "a", lease_file, "project-a"), "gpu")
    b = leases.resource_lease(_project(tmp_path, "b", lease_file, "project-b"), "gpu")
    assert a.acquire(wait_s=0) is True
    assert a.release_requested() is False
    assert b.acquire(wait_s=0) is False                  # b cannot take it; its wait is recorded
    assert a.release_requested() is True


def test_hand_over_timeout_gives_the_waiter_a_window_before_the_holder_takes_a_new_one(tmp_path):
    lease_file = tmp_path / "shared" / "gpu.lease"
    a_layout = _project(tmp_path, "a", lease_file, "project-a", hand_over_timeout_s=1)
    b_layout = _project(tmp_path, "b", lease_file, "project-b", hand_over_timeout_s=1)
    a = leases.resource_lease(a_layout, "gpu")
    b = leases.resource_lease(b_layout, "gpu")
    assert a.acquire(wait_s=0) is True
    assert b.acquire(wait_s=0) is False                  # records b's release_requested
    a.release()
    assert a.acquire(wait_s=0) is False                  # free now, but b's request is fresh: a yields the window
    time.sleep(1.2)                                      # the window passes with b never coming back for it
    assert a.acquire(wait_s=0) is True                   # a may proceed rather than starve forever


def test_acquire_blocks_up_to_wait_s_then_reports_unavailable(tmp_path):
    lease_file = tmp_path / "shared" / "gpu.lease"
    a = leases.resource_lease(_project(tmp_path, "a", lease_file, "project-a"), "gpu")
    b = leases.resource_lease(_project(tmp_path, "b", lease_file, "project-b"), "gpu")
    assert a.acquire(wait_s=0) is True
    started = time.monotonic()
    assert b.acquire(wait_s=0.3) is False
    assert time.monotonic() - started >= 0.25


def test_renew_keeps_the_entry_from_going_stale(tmp_path):
    lease_file = tmp_path / "shared" / "gpu.lease"
    a_layout = _project(tmp_path, "a", lease_file, "project-a", stale_after_s=1)
    b_layout = _project(tmp_path, "b", lease_file, "project-b", stale_after_s=1)
    a = leases.resource_lease(a_layout, "gpu")
    b = leases.resource_lease(b_layout, "gpu")
    assert a.acquire(wait_s=0) is True
    time.sleep(0.6)
    a.renew()
    time.sleep(0.6)                                       # 1.2s since acquire, but renewed at 0.6s
    assert b.acquire(wait_s=0) is False                    # still fresh: not pruned
