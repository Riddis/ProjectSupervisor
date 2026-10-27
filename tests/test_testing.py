"""supervisor.testing (phase 2 step 6): the explicitly unstable helpers an extension's tests use - a temporary project,
an in-process fake lane, a loop driver, a Control Center test client and the import side-effect probe - promoted from
this suite's own fixtures, which now use them too."""
from __future__ import annotations

import sys

import yaml

from supervisor import extensions, jobs, tasks, testing
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
