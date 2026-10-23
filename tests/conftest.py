import pathlib, shutil, sys
sys.dont_write_bytecode = True   # bytecode from the Linux dev VM must never be reused on Windows (and vice versa)
import os, tempfile
os.environ.setdefault("SUPERVISOR_UPDATE_CHECK", "0")   # no origin fetches (network, git subprocesses) from the suite
_TMP = pathlib.Path(__file__).resolve().parents[1] / "runs" / "tmp"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMP", str(_TMP)); os.environ.setdefault("TEMP", str(_TMP)); os.environ.setdefault("TMPDIR", str(_TMP))
tempfile.tempdir = str(_TMP)          # the workstation's %TEMP% is access-denied; everything temporary lives in runs/tmp
# Scratch projects live under runs/ inside the framework clone. Git must not discover the clone from there: a fresh
# project is "not a git repo" to doctor and friends, whatever directory the suite happens to run in.
_RUNS = str(pathlib.Path(__file__).resolve().parents[1] / "runs")
os.environ["GIT_CEILING_DIRECTORIES"] = _RUNS + (os.pathsep + os.environ["GIT_CEILING_DIRECTORIES"]
                                                 if os.environ.get("GIT_CEILING_DIRECTORIES") else "")
import pytest
import yaml

from supervisor.paths import Layout
from supervisor import store

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "supervisor" / "template"       # the project template: config/, agents/, state/ live here, not at the repo root


def make_task(layout, tid, phase=0, deps=(), reviewer=(), human="none", status="NOT_STARTED"):
    doc = {"id": tid, "phase": phase, "title": tid, "objective": "o", "owner_role": "engineer",
           "depends_on": list(deps), "actions": ["a"], "artifacts": ["x"], "tests": ["t"], "reviewer": list(reviewer),
           "acceptance": "ok", "escalation": "none", "human_approval": human, "cost_category": "claude", "status": status}
    return store.write_new(layout.tasks / f"{tid}.yaml", doc)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_ROOT", str(tmp_path))
    lay = Layout(tmp_path).ensure()
    (lay.state / "gates.yaml").write_text(yaml.safe_dump({
        "phases": {0: {"requires": []}, 1: {"requires": ["G0"]}, 9: {"requires": ["G6"]}},
        "gates": {"G0": {"status": "OPEN", "phase": 0}, "G6": {"status": "OPEN", "phase": 6}}}), encoding="utf-8")
    (lay.state / "owner_tasks.yaml").write_text("owner_tasks: []\n", encoding="utf-8")
    return lay
