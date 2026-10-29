"""supervisor.testing (phase 2 step 6): the explicitly unstable helpers an extension's tests use - a temporary project,
an in-process fake lane, a loop driver, a Control Center test client and the import side-effect probe - promoted from
this suite's own fixtures, which now use them too."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from supervisor import extensions, jobs, tasks, testing
from supervisor.ext import InboxError, submit_command
from supervisor.models.task import TaskStatus

EXT = '''
from supervisor.ext import Check

def setup(registry):
    registry.doctor_check(lambda layout: Check("probe", True, "ok"))
'''


def test_a_temporary_project_has_the_state_a_loop_needs(tmp_path):
    lay = testing.project(tmp_path / "p")
    assert (lay.state / "gates.yaml").is_file() and (lay.state / "owner_tasks.yaml").is_file()
    assert lay.tasks.is_dir() and lay.runs.is_dir() and not lay.project_file.exists()


def test_a_temporary_project_with_extensions_config_and_files(tmp_path):
    lay = testing.project(tmp_path / "p", name="Demo", modules={"probe": EXT}, routing={"probe": {"a": 1}},
                          policy={"channels": {}}, files={"docs/a.md": "hello"})
    doc = yaml.safe_load(lay.project_file.read_text(encoding="utf-8"))
    assert doc["name"] == "Demo" and doc["extensions"] == ["probe"]
    assert yaml.safe_load((lay.config / "routing.yaml").read_text(encoding="utf-8")) == {"probe": {"a": 1}}
    assert (lay.config / "policy.yaml").is_file() and (lay.root / "docs" / "a.md").read_text(encoding="utf-8") == "hello"
    reg = extensions.load(lay)
    assert reg.loaded == ["probe"] and reg.errors == []
    testing.reset()


def test_add_extensions_keeps_the_rest_of_project_yaml(tmp_path):
    lay = testing.project(tmp_path / "p", name="Demo")
    testing.add_extensions(lay, {"probe": EXT})
    doc = yaml.safe_load(lay.project_file.read_text(encoding="utf-8"))
    assert doc["name"] == "Demo" and doc["extensions"] == ["probe"]
    assert extensions.load(lay).loaded == ["probe"]
    testing.reset()


def test_a_fake_lane_completes_a_task_through_the_loop(tmp_path):
    lay = testing.project(tmp_path / "p")
    with testing.lane(testing.FakeLane("fake_done", artifacts=["out/a.txt"])) as fake:
        testing.make_task(lay, "A", job={"adapter": "fake_done"}, artifacts=["out/a.txt"])
        testing.drive(lay, until=lambda: tasks.load(lay, "A").status == TaskStatus.COMPLETE)
    assert tasks.load(lay, "A").status == TaskStatus.COMPLETE
    assert fake.submitted == ["A.a1"] and (lay.root / "out" / "a.txt").is_file()
    from supervisor.adapters import REGISTRY
    assert "fake_done" not in REGISTRY                         # the lane is gone after the block


def test_a_fake_lane_can_defer_and_fail(tmp_path):
    lay = testing.project(tmp_path / "p")
    with testing.lane(testing.FakeLane("fake_defer", defer=2)) as fake:
        testing.make_task(lay, "D", job={"adapter": "fake_defer"}, artifacts=["d.txt"])
        testing.drive(lay, iterations=2)
        assert fake.deferred == 2 and fake.submitted == []
        assert jobs.for_task(lay, "D")[0]["phase"] == "PREPARED"
    with testing.lane(testing.FakeLane("fake_fail", status="FAILED", error="boom")):
        testing.make_task(lay, "F", job={"adapter": "fake_fail"}, artifacts=["f.txt"])
        testing.drive(lay, iterations=3)
        assert tasks.load(lay, "F").status == TaskStatus.FAILED


def test_the_ui_client_carries_the_csrf_cookie(tmp_path):
    lay = testing.project(tmp_path / "p", name="Demo")
    client = testing.ui_client(lay)
    r = client.get("/")
    assert r.status_code == 200 and f'value="{testing.CSRF}"' in r.text


def test_the_ui_client_is_imported_only_when_used():
    import subprocess
    from pathlib import Path
    code = "import sys, supervisor.testing; print('fastapi' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parents[1]),
                         capture_output=True, text=True, encoding="utf-8", check=False)
    assert out.returncode == 0 and out.stdout.strip() == "False", out.stderr


def test_the_import_probe_is_offered(tmp_path):
    lay = testing.project(tmp_path / "p", modules={"probe": EXT})
    assert testing.import_side_effects(lay, "probe") == {"adapters": [], "python_jobs": [], "commands": []}
    testing.reset()


# ---------------------------------------------------------------- read helpers (1.14.0)
# An extension package's tests may use only supervisor.ext and supervisor.testing (G5). Porting the first packages'
# tests showed what they read: the stored task a lane is handed, the job records, an open job record planted the
# way the loop leaves one, one settle pass, the scheduler's next picks, the inbox items of a claimed kind and the
# owner's generic answer, and the state lock a running loop holds. The tests below use nothing else.

LANE_EXT = '''
from pathlib import Path

from supervisor.ext import Adapter, Result


class Slow(Adapter):
    """Finishes when its run folder holds done.txt; the job takes the resource slot."""
    name = "slow_lane"
    gpu = True

    def prepare(self, layout, task, attempt_id):
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return {"run_dir": str(run_dir), "label": (task.job or {}).get("label", "")}

    def submit(self, spec):
        return {"kind": "slow", "started": True}

    def alive(self, handle):
        return True

    def collect(self, spec, handle):
        if (Path(spec["run_dir"]) / "done.txt").is_file():
            return Result(status="COMPLETE", artifacts=[], notes="done")
        return None


def setup(registry):
    registry.adapter(Slow())
    registry.inbox_kind("tally", "Tally", answerable=False)
'''


def lane_project(tmp_path):
    lay = testing.project(tmp_path / "p", modules={"slow": LANE_EXT},
                          routing={"limits": {"max_resource_jobs": 1}})
    testing.load(lay)
    return lay


def test_a_package_test_reads_its_registered_lane_and_the_task_it_is_handed(tmp_path):
    lay = lane_project(tmp_path)
    ad = testing.registered_lane("slow_lane")
    assert ad.name == "slow_lane" and ad.gpu
    testing.make_task(lay, "A", job={"adapter": "slow_lane", "label": "first"}, cost_category="local")
    task = testing.task(lay, "A")
    assert task.status == "NOT_STARTED" and task.job["label"] == "first"
    assert ad.prepare(lay, task, "A.a1")["label"] == "first"
    with pytest.raises(KeyError):
        testing.registered_lane("no_such_lane")
    testing.reset()


def test_a_planted_open_job_record_holds_the_slot_until_a_settle_pass_collects_it(tmp_path):
    lay = lane_project(tmp_path)
    ad = testing.registered_lane("slow_lane")
    for tid in ("A", "B"):
        testing.make_task(lay, tid, job={"adapter": "slow_lane"}, artifacts=[])
    spec = ad.prepare(lay, testing.task(lay, "A"), "A.a1")
    rec = testing.running_job(lay, "A", "slow_lane", spec, {"kind": "slow", "started": True}, gpu=True)
    assert rec["attempt_id"] == "A.a1" and rec["phase"] == "RUNNING"
    assert testing.plan(lay) == []                                  # the resource slot is taken
    testing.settle(lay)
    assert testing.job(lay, "A.a1")["phase"] == "RUNNING"            # still running: nothing collected
    (Path(spec["run_dir"]) / "done.txt").write_text("ok\n", encoding="utf-8")
    testing.settle(lay)
    assert testing.job(lay, "A.a1")["phase"] == "DONE" and testing.job(lay, "A.a1")["result"] == "COMPLETE"
    assert testing.task(lay, "A").status == "COMPLETE"
    assert [r["attempt_id"] for r in testing.jobs(lay, "A")] == ["A.a1"]
    assert [r["attempt_id"] for r in testing.jobs(lay)] == ["A.a1"]
    assert testing.plan(lay) == [("B", "slow_lane")]                # the slot is free again
    testing.reset()


def test_a_package_test_reads_and_answers_inbox_items(tmp_path):
    lay = lane_project(tmp_path)
    item = testing.make_inbox_item(lay, kind="tally", subject="count these", summary="three rows",
                                   allowed_responses=["SUBMIT"], rows=[{"id": "r1"}, {"id": "r2"}])
    stored = testing.inbox_item(lay, item["id"])
    assert stored["kind"] == "tally" and stored["status"] == "open" and [r["id"] for r in stored["rows"]] == ["r1", "r2"]
    question = testing.make_inbox_item(lay, subject="q", summary="a question")
    assert testing.inbox_item(lay, question["id"])["kind"] == "question"
    with pytest.raises(InboxError):                                  # the generic path may not close a claimed kind
        testing.answer(lay, item["id"], "SUBMIT")
    assert testing.inbox_item(lay, item["id"])["status"] == "open"
    assert testing.answer(lay, question["id"], "ok", note="fine")["status"] == "answered"
    with pytest.raises(InboxError):
        testing.inbox_item(lay, "no-such-item")
    testing.reset()


def test_while_the_loop_lock_is_held_an_owner_command_queues(tmp_path):
    lay = lane_project(tmp_path)
    question = testing.make_inbox_item(lay, subject="q", summary="a question", allowed_responses=["YES"])
    with testing.loop_lock(lay):
        out = submit_command(lay, "inbox.answer", {"item_id": question["id"], "response": "YES"})
        assert out.get("queued") and testing.inbox_item(lay, question["id"])["status"] == "open"
    testing.drive(lay, iterations=1)
    assert testing.inbox_item(lay, question["id"])["status"] == "answered"
    testing.reset()
