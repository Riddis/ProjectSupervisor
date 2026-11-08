"""2.4.0 G4: a run-directory file declared as an artifact is dropped (like a volatile store file), never a
containment failure, and a contract artifact under `runs/` or `state/` (outside `state/decisions/`) is refused at
import — no file there can ever satisfy a delivery."""
from pathlib import Path

from supervisor import tasks
from supervisor.adapters import python as pyad
from supervisor.loop import Supervisor
from supervisor.models.task import Task, TaskStatus
from tests.conftest import make_task


@pyad.job("g4_write_widget_and_handoff")
def _write_widget_and_handoff(root, run_dir, **_):
    (root / "widget.txt").write_text("ok")
    handoff = Path(run_dir) / "HANDOFF.md"
    handoff.write_text("notes")
    handoff_rel = handoff.relative_to(root).as_posix()
    return {"status": "COMPLETE", "artifacts": ["widget.txt", handoff_rel]}


def test_run_directory_artifacts_are_dropped_not_failed(layout):
    make_task(layout, "T")
    def fn(doc):
        doc["job"] = {"adapter": "python", "fn": "g4_write_widget_and_handoff"}
        doc["artifacts"] = ["widget.txt"]
    tasks._update(layout, "T", fn)
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(3):
        sup.once()
    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.COMPLETE
    assert task.paths == ["widget.txt"]
    dropped = [e for e in layout.log.read_text(encoding="utf-8").splitlines() if '"artifacts_dropped"' in e]
    assert dropped and "HANDOFF.md" in dropped[0]


def _minimal_task_doc(artifact: str) -> dict:
    return {"id": "X", "phase": 0, "title": "t", "objective": "o", "owner_role": "engineer", "actions": ["a"],
            "artifacts": [artifact], "tests": ["t"], "acceptance": "ok", "cost_category": "deterministic"}


def test_a_contract_artifact_under_runs_is_refused_at_import(layout):
    task = Task.model_validate(_minimal_task_doc("runs/X/X.a1/out.txt"))
    errors, _ = tasks.artifact_problems([task], root=layout.root)
    assert any("runs/" in e for e in errors)


def test_a_contract_artifact_under_state_outside_authored_is_refused_at_import(layout):
    task = Task.model_validate(_minimal_task_doc("state/tasks/X.yaml"))
    errors, _ = tasks.artifact_problems([task], root=layout.root)
    assert any("state/" in e for e in errors)


def test_a_contract_artifact_under_state_decisions_is_allowed(layout):
    (layout.decisions).mkdir(parents=True, exist_ok=True)
    (layout.decisions / "D-900-widget.md").write_text("x", encoding="utf-8")
    task = Task.model_validate(_minimal_task_doc("state/decisions/D-900-widget.md"))
    errors, _ = tasks.artifact_problems([task], root=layout.root)
    assert errors == []


def test_brief_tells_the_worker_run_directory_files_are_dropped_not_artifacts(layout):
    from supervisor import packets
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    brief = packets.delivery_brief(layout, task)
    assert "never list them in `artifacts`" in brief and "runs/" in brief
