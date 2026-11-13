"""supervisor.leases: the machine-wide, re-entrant, project-held resource lease behind
`supervisor.plugin.resource_lease` (R4, docs/plugin_api.json). Each test builds two project Layouts pointed at the
same lease file to act as "two projects on one machine" the way the design's GPU-sharing problem is stated."""
from __future__ import annotations

import json
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


def test_release_requested_expires_after_the_hand_over_window_so_a_is_not_asked_to_yield_forever(tmp_path):
    """F8. After hand_over_timeout_s, b's never coming back (its loop stopped, its task cancelled) must not
    leave a asked to yield forever, and the doctor must not keep naming a waiter that no longer exists."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    a_layout = _project(tmp_path, "a", lease_file, "project-a", hand_over_timeout_s=0.5)
    b_layout = _project(tmp_path, "b", lease_file, "project-b", hand_over_timeout_s=0.5)
    a = leases.resource_lease(a_layout, "gpu")
    b = leases.resource_lease(b_layout, "gpu")
    assert a.acquire(wait_s=0) is True
    assert b.acquire(wait_s=0) is False                  # records b's release_requested
    a.release()
    time.sleep(0.7)                                      # the window passes; b never comes back for it
    assert a.acquire(wait_s=0) is True                    # window over: a may take it again
    assert a.release_requested() is False                 # expired: nobody is waiting any more
    assert leases.status(b_layout, "gpu")["waiting"] is None   # the doctor names no waiter either
    a.release()


def test_release_requested_still_true_inside_the_hand_over_window(tmp_path):
    """The companion case: the fix cannot be "always False" — a live request inside the window must still
    read True, and the doctor must still name the waiter, so the watchdog still stops on it."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    a_layout = _project(tmp_path, "a", lease_file, "project-a", hand_over_timeout_s=60)
    b_layout = _project(tmp_path, "b", lease_file, "project-b", hand_over_timeout_s=60)
    a = leases.resource_lease(a_layout, "gpu")
    b = leases.resource_lease(b_layout, "gpu")
    assert a.acquire(wait_s=0) is True
    assert b.acquire(wait_s=0) is False                  # records b's release_requested
    assert a.release_requested() is True                  # still inside the window: a is still asked to yield
    assert leases.status(b_layout, "gpu")["waiting"] == "project-b"
    a.release()


def test_a_waiter_still_asking_every_pass_is_not_read_as_expired_once_the_window_is_older_than_its_first_ask(tmp_path):
    """F20 (docs/reviews/RELEASE_REVIEW_local-lane_round2.md). F8's expiry read only release_requested_at, set
    once on b's first refused pass and never refreshed (F2's own idempotence). A holder whose job outlives
    hand_over_timeout_s therefore read "nobody is waiting" while b was still asking on every pass. b's second
    ask, after the window from its *first* ask has already passed, must keep a asked to yield."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    a_layout = _project(tmp_path, "a", lease_file, "project-a", hand_over_timeout_s=0.5)
    b_layout = _project(tmp_path, "b", lease_file, "project-b", hand_over_timeout_s=0.5)
    a = leases.resource_lease(a_layout, "gpu")
    b = leases.resource_lease(b_layout, "gpu")
    assert a.acquire(wait_s=0) is True
    assert b.acquire(wait_s=0) is False                  # b's first ask, recorded
    time.sleep(0.7)                                       # older than the window since the first ask
    assert b.acquire(wait_s=0) is False                  # b asks again: still refused, still waiting
    assert a.release_requested() is True                  # not expired: b never stopped asking
    assert leases.status(b_layout, "gpu")["waiting"] == "project-b"
    a.release()


def test_a_waiter_that_has_stopped_asking_still_expires_after_the_window(tmp_path):
    """The companion the fix must keep holding, so it cannot become "never expires": a waiter whose loop is
    gone — it asked once and never came back — must still read as expired once hand_over_timeout_s has
    passed, exactly as F8 made it."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    a_layout = _project(tmp_path, "a", lease_file, "project-a", hand_over_timeout_s=0.5)
    b_layout = _project(tmp_path, "b", lease_file, "project-b", hand_over_timeout_s=0.5)
    a = leases.resource_lease(a_layout, "gpu")
    b = leases.resource_lease(b_layout, "gpu")
    assert a.acquire(wait_s=0) is True
    assert b.acquire(wait_s=0) is False                  # b's one and only ask
    time.sleep(0.7)                                       # b never asks again: its loop is gone
    assert a.release_requested() is False                 # expired: nobody is waiting any more
    assert leases.status(b_layout, "gpu")["waiting"] is None
    a.release()
    assert a.acquire(wait_s=0) is True                    # the requester's own path aside, a plain take proceeds


def test_repeated_asks_do_not_restart_the_release_requested_at_window_start(tmp_path):
    """F2's idempotence must survive F20's fix: the waiter's latest ask is tracked in its own field, so
    release_requested_at — the window's own start — is written once and never pushed forward by a later ask."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    a_layout = _project(tmp_path, "a", lease_file, "project-a", hand_over_timeout_s=60)
    b_layout = _project(tmp_path, "b", lease_file, "project-b", hand_over_timeout_s=60)
    a = leases.resource_lease(a_layout, "gpu")
    b = leases.resource_lease(b_layout, "gpu")
    assert a.acquire(wait_s=0) is True
    assert b.acquire(wait_s=0) is False
    first = json.loads(lease_file.read_text(encoding="utf-8"))["release_requested_at"]
    time.sleep(0.2)
    assert b.acquire(wait_s=0) is False                  # a second ask, still refused
    second = json.loads(lease_file.read_text(encoding="utf-8"))["release_requested_at"]
    assert second == first                                # the window's start never moves
    a.release()


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
    assert a.renew() is True
    time.sleep(0.6)                                       # 1.2s since acquire, but renewed at 0.6s
    assert b.acquire(wait_s=0) is False                    # still fresh: not pruned


def test_renew_reports_whether_it_refreshed_anything(tmp_path):
    """F19: the loop's own re-take (loop.py:_renew_gpu_lease_if_active) decides whether to call leases._reclaim()
    on what renew() reports, so renew() must say True only when it actually found and touched this process's own
    (holder, pid) entry. False both when no lease is configured (a no-op that already has it) and when this
    process holds no matching entry at all -- never acquired, already released, or (F19's own case) an entry on
    file whose pid names a different, exited process, which is what a restarted loop's first renew() finds."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay = _project(tmp_path, "a", lease_file, "project-a")
    unconfigured = leases.resource_lease(testing.project(tmp_path / "solo"), "gpu")
    assert unconfigured.renew() is False                   # no lease configured at all

    a = leases.resource_lease(lay, "gpu")
    assert a.renew() is False                               # never acquired: no entry to refresh
    assert a.acquire(wait_s=0) is True
    assert a.renew() is True                                 # now holds a real (holder, pid) entry
    a.release()
    assert a.renew() is False                                # released: the entry is gone again

    assert a.acquire(wait_s=0) is True
    doc = json.loads(lease_file.read_text(encoding="utf-8"))
    for e in doc["entries"]:
        e["pid"] = e["pid"] + 1                             # this process's own entry, but under another pid
    lease_file.write_text(json.dumps(doc), encoding="utf-8")
    assert a.renew() is False                                # (holder, pid) no longer matches: nothing refreshed


def test_reclaim_is_a_no_op_with_no_lease_configured(tmp_path):
    lay = testing.project(tmp_path / "solo")
    assert leases._reclaim(lay, "gpu") is True


def test_reclaim_re_establishes_this_projects_own_entry_after_a_pid_mismatch(tmp_path):
    """F19's own shape: the entry on file names a pid that is no longer this process's (what renew() finding
    nothing looks like after a restart). _reclaim() succeeds re-entrantly as long as the cards are still this
    project's, writing a fresh entry under this process's real (pid, create_time). Internal to the core (not
    part of the plugin-facing ResourceLease handle, the same precedent as _record_waiting_from_peek), so it is
    called as a module function, never as a method of the `a` handle below."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay = _project(tmp_path, "a", lease_file, "project-a")
    a = leases.resource_lease(lay, "gpu")
    assert a.acquire(wait_s=0) is True
    doc = json.loads(lease_file.read_text(encoding="utf-8"))
    for e in doc["entries"]:
        e["pid"], e["create_time"] = 999999, 1.0            # an exited process's identity, as after a restart
    lease_file.write_text(json.dumps(doc), encoding="utf-8")
    assert leases.status(lay, "gpu")["live"] == []           # pruned: the rewritten entry is not alive

    assert leases._reclaim(lay, "gpu") is True
    after = json.loads(lease_file.read_text(encoding="utf-8"))["entries"]
    assert len(after) == 1 and after[0]["pid"] == os.getpid()   # re-established under this process's own pid


def test_reclaim_refuses_when_another_project_already_holds_it_live(tmp_path):
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay_a = _project(tmp_path, "a", lease_file, "project-a")
    c = leases.resource_lease(_project(tmp_path, "c", lease_file, "project-c"), "gpu")
    assert c.acquire(wait_s=0) is True                        # c already started on top
    assert leases._reclaim(lay_a, "gpu") is False              # the cards are not a's any more


def test_reclaim_succeeds_and_leaves_another_projects_pending_request_untouched(tmp_path):
    """Unlike acquire()/_try_take(), a standing hand-over request from another project never refuses _reclaim()
    -- that request is about this project yielding the job it is already holding, not a free slot up for
    grabs -- and _reclaim() has no code path that writes release_requested_by/_at, so a request recorded by a
    peek that happened while this project's entry was still live is left exactly as it was."""
    lease_file = tmp_path / "shared" / "gpu.lease"
    a_layout = _project(tmp_path, "a", lease_file, "project-a", hand_over_timeout_s=60)
    b_layout = _project(tmp_path, "b", lease_file, "project-b", hand_over_timeout_s=60)
    a = leases.resource_lease(a_layout, "gpu")
    b = leases.resource_lease(b_layout, "gpu")
    assert a.acquire(wait_s=0) is True
    assert b.acquire(wait_s=0) is False                        # records b's release_requested while a is live
    before = json.loads(lease_file.read_text(encoding="utf-8"))
    assert before["release_requested_by"] is not None

    doc = json.loads(lease_file.read_text(encoding="utf-8"))
    for e in doc["entries"]:
        e["pid"], e["create_time"] = 999999, 1.0              # a's entry goes stale, as after a restart
    lease_file.write_text(json.dumps(doc), encoding="utf-8")

    assert leases._reclaim(a_layout, "gpu") is True             # still succeeds: the cards are still a's
    after = json.loads(lease_file.read_text(encoding="utf-8"))
    assert after["release_requested_by"] == before["release_requested_by"]
    assert after["release_requested_label"] == before["release_requested_label"]
    assert after["release_requested_at"] == before["release_requested_at"]
