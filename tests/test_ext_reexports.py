"""supervisor.ext, the names re-exported as they are (phase 2 step 2): each is the framework's own object, and the
behaviour the dependency map says must survive holds when an extension reaches it through supervisor.ext: paths
resolved from the project root, atomic replace (also while a reader holds the file on Windows), `contained`
refusing a path outside the root, the GPU probe never raising, and a CapacityDeferred costing no attempt."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import threading
import time

import pytest

from supervisor import ext, jobs, tasks
from supervisor.adapters.base import REGISTRY
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task


def test_the_reexported_names_are_the_framework_objects():
    from supervisor import gpu, paths, store
    from supervisor._vendor import contract
    from supervisor.adapters import base, process
    from supervisor.doctor import Check
    assert ext.Layout is paths.Layout and ext.now_iso is store.now_iso and ext.gpu is gpu and ext.Check is Check
    assert (ext.atomic_json, ext.read_json, ext.atomic_write_text, ext.contained, ext.CapacityDeferred) == (
        contract.atomic_json, contract.read_json, contract.atomic_write_text, contract.contained,
        contract.CapacityDeferred)
    assert (ext.Adapter, ext.Result, ext.ProcessAdapter) == (base.Adapter, base.Result, process.ProcessAdapter)
    assert set(ext.__all__) == {"Adapter", "CapacityDeferred", "Check", "Layout", "ProcessAdapter", "Result",
                                "atomic_json", "atomic_write_text", "contained", "gpu", "now_iso", "read_json"}


def test_layout_resolves_every_path_from_the_project_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proj").mkdir()
    lay = ext.Layout("proj")                                   # a relative base is resolved, never kept relative
    assert lay.root == (tmp_path / "proj").resolve()
    assert (lay.state, lay.runs, lay.config, lay.tasks, lay.runtime) == (
        lay.root / "state", lay.root / "runs", lay.root / "config", lay.root / "state" / "tasks",
        lay.root / "state" / "runtime")


def test_an_atomic_write_replaces_the_whole_file_and_leaves_no_temporary(tmp_path):
    target = tmp_path / "out.txt"
    target.write_bytes(b"old\n")
    ext.atomic_write_text(target, "new\nlines\n", newline="\r\n")
    assert target.read_bytes() == b"new\r\nlines\r\n"
    ext.atomic_json(tmp_path / "data.json", {"a": [1, 2]})
    assert ext.read_json(tmp_path / "data.json") == {"a": [1, 2]}
    (tmp_path / "bom.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"b": 1}).encode())
    assert ext.read_json(tmp_path / "bom.json") == {"b": 1}   # a BOM from a Windows editor still reads
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bom.json", "data.json", "out.txt"]


def test_an_atomic_write_waits_out_a_reader_holding_the_file(tmp_path):
    """On Windows a replace is refused while another handle has the file open; the write retries briefly instead of
    failing (on POSIX the replace simply succeeds)."""
    target = tmp_path / "status.json"
    ext.atomic_json(target, {"v": 1})
    reader = open(target, encoding="utf-8")                  # noqa: SIM115 - held open across the write, closed by the timer
    release = threading.Timer(0.4, reader.close)
    release.start()
    try:
        started = time.monotonic()
        ext.atomic_json(target, {"v": 2})
    finally:
        release.join()
        reader.close()
    assert ext.read_json(target) == {"v": 2}
    assert time.monotonic() - started < 10


def test_a_failed_atomic_write_leaves_the_old_file_whole(tmp_path, monkeypatch):
    from supervisor._vendor import contract
    target = tmp_path / "keep.txt"
    target.write_bytes(b"old content\n")

    def refuse(*_a, **_k):
        raise OSError("disk went away")
    monkeypatch.setattr(contract.os, "replace", refuse)
    with pytest.raises(OSError, match="disk went away"):
        ext.atomic_write_text(target, "new content\n")
    monkeypatch.undo()
    assert target.read_bytes() == b"old content\n"
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]   # the temporary copy is gone too


def test_contained_refuses_a_path_outside_the_root(tmp_path):
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    (tmp_path / "proj2").mkdir()
    assert ext.contained(root / "docs" / "a.md", [root])
    assert ext.contained("docs/a.md", ["."], root=root)          # relative paths resolve against `root`
    assert not ext.contained(root / ".." / "proj2" / "a.md", [root])
    assert not ext.contained(tmp_path / "proj2" / "a.md", [root])   # a shared name prefix is not containment
    assert not ext.contained("../outside.md", ["."], root=root)


@pytest.mark.parametrize("behaviour", ["missing", "timeout", "error_exit", "garbage"])
def test_the_gpu_probe_never_raises(monkeypatch, behaviour):
    def run(*_a, **_k):
        if behaviour == "missing":
            raise FileNotFoundError("nvidia-smi")
        if behaviour == "timeout":
            raise subprocess.TimeoutExpired("nvidia-smi", 10)
        return subprocess.CompletedProcess([], 1 if behaviour == "error_exit" else 0,
                                           stdout="N/A\n" if behaviour == "garbage" else "", stderr="")
    monkeypatch.setattr(ext.gpu.subprocess, "run", run)
    assert (ext.gpu.free_mb(), ext.gpu.used_mb(), ext.gpu.total_mb()) == (None, None, None)
    assert ext.gpu.available() is False


def test_now_iso_is_utc_to_the_second():
    stamp = dt.datetime.fromisoformat(ext.now_iso())
    assert stamp.utcoffset() == dt.timedelta(0) and stamp.microsecond == 0


class DeferringLane(ext.Adapter):
    """A lane written against supervisor.ext only, whose submit() is always out of capacity."""
    name = "ext_deferring_lane"
    submits = 0

    def prepare(self, layout, task, attempt_id):
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir)}

    def submit(self, spec):
        type(self).submits += 1
        raise ext.CapacityDeferred(0.0, "out of capacity until the window resets")

    def alive(self, handle):
        return False

    def collect(self, spec, handle):
        return ext.Result(status="COMPLETE")


def test_a_capacity_deferral_costs_no_attempt(layout, monkeypatch):
    monkeypatch.setitem(REGISTRY, DeferringLane.name, DeferringLane())
    make_task(layout, "X")
    tasks._update(layout, "X", lambda d: d.update(job={"adapter": DeferringLane.name}))
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(5):                       # a counted attempt would fail the job after two launches
        sup.once()
    assert DeferringLane.submits >= 3
    recs = jobs.for_task(layout, "X")
    assert [r["attempt_id"] for r in recs] == ["X.a1"]
    assert recs[0]["phase"] == "PREPARED" and int(recs[0].get("launch_attempts", 0)) == 0
    assert "out of capacity" in recs[0]["deferred_reason"]
    assert tasks.load(layout, "X").status not in (TaskStatus.FAILED, TaskStatus.BLOCKED)
