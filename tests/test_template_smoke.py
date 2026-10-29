"""Gate G3's template smoke test (docs/BOUNDARY.md): a project made by `supervisor init` passes doctor's project
checks and carries one task through the real loop to COMPLETE on a model-free lane (the `process` adapter running
a small Python child), with nothing project-specific in between."""
import sys
import time

import yaml
from typer.testing import CliRunner

from supervisor import plugins, tasks
from supervisor.cli import app
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from supervisor.paths import Layout

CHILD = ("import json, os, pathlib; a = os.environ['SUPERVISOR_ATTEMPT']; t = a.rsplit('.', 1)[0]; "
         "pathlib.Path('hello.txt').write_text('hi', encoding='utf-8'); "
         "pathlib.Path('runs', t, a, 'result.json').write_text(json.dumps("
         "{'status': 'COMPLETE', 'artifacts': ['hello.txt'], 'notes': 'smoke'}), encoding='utf-8')")


def test_init_doctor_and_one_task_to_complete(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_ROOT", raising=False)
    plugins.reset()
    runner = CliRunner()
    target = tmp_path / "smoke"
    r = runner.invoke(app, ["init", str(target), "--name", "Smoke"])
    assert r.exit_code == 0, r.output
    monkeypatch.chdir(target)

    r = runner.invoke(app, ["doctor"])
    lines = {ln.split()[1].rstrip(":"): ln.split()[0] for ln in r.output.splitlines() if len(ln.split()) > 1}
    assert all(lines.get(name) == "OK" for name in ("project", "state", "routing")), r.output

    (target / "state" / "tasks_seed.yaml").write_text(yaml.safe_dump({"tasks": [{
        "id": "S-01", "phase": 0, "title": "Smoke", "objective": "write hello.txt", "owner_role": "engineer",
        "actions": ["run the child"], "artifacts": ["hello.txt"], "acceptance": "hello.txt exists",
        "cost_category": "deterministic", "job": {"adapter": "process", "argv": [sys.executable, "-c", CHILD]}}]}),
        encoding="utf-8")
    r = runner.invoke(app, ["task", "import"])
    assert r.exit_code == 0 and "created 1" in r.output, r.output

    layout = Layout(target)
    monkeypatch.setenv("SUPERVISOR_ROOT", str(target))
    sup = Supervisor(layout, sleep_s=0)
    deadline = time.time() + 60
    while time.time() < deadline and tasks.load(layout, "S-01").status != TaskStatus.COMPLETE:
        sup.once()
        time.sleep(0.2)
    task = tasks.load(layout, "S-01")
    assert task.status == TaskStatus.COMPLETE, task.history
    assert (target / "hello.txt").read_text(encoding="utf-8") == "hi"
