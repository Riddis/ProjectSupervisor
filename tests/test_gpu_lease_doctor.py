"""`supervisor doctor`'s check of resources.gpu.lease (R4: "a doctor check for the lease file: present, writable,
who holds it, whether any entry is stale")."""
from __future__ import annotations

import json

from supervisor import doctor, testing


def _by_name(checks, name):
    return next(c for c in checks if c.name == name)


def test_no_lease_configured_is_ok_and_says_so(tmp_path):
    lay = testing.project(tmp_path / "solo")
    check = _by_name(doctor.run(lay), "gpu_lease")
    assert check.ok and "no lease configured" in check.detail


def test_reports_who_holds_it(tmp_path):
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay = testing.project(tmp_path / "a", routing={"resources": {"gpu": {"lease": {
        "path": str(lease_file), "label": "project-a"}}}})
    from supervisor import leases
    assert leases.resource_lease(lay, "gpu").acquire(wait_s=0) is True
    check = _by_name(doctor.run(lay), "gpu_lease")
    assert check.ok and "project-a" in check.detail


def test_reports_a_stale_entry_as_pruned(tmp_path):
    lease_file = tmp_path / "shared" / "gpu.lease"
    lay = testing.project(tmp_path / "a", routing={"resources": {"gpu": {"lease": {
        "path": str(lease_file), "label": "project-a"}}}})
    lease_file.parent.mkdir(parents=True, exist_ok=True)
    lease_file.write_text(json.dumps({
        "entries": [{"holder": "deadbeefdeadbeef", "label": "ghost", "pid": 999999, "create_time": 1.0,
                     "since": 1.0, "renewed": 1.0}],
        "release_requested_by": None, "release_requested_label": None, "release_requested_at": None}),
        encoding="utf-8")
    check = _by_name(doctor.run(lay), "gpu_lease")
    assert check.ok and "stale" in check.detail and "ghost" not in check.detail
