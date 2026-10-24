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


@pytest.fixture(autouse=True)
def _no_real_capacity_rpc(request, monkeypatch):
    """The suite never asks the workstation's real Codex CLI for its usage windows: Supervisor.poll_capacity() used to
    reach the installed, authenticated CLI, and a nearly spent real window (93% used) gated the fake jobs of
    test_codex_cli and failed them. The RPC is reported as unavailable (the same path as a machine without Codex);
    tests that exercise polling patch `poll_codex` themselves, and `@pytest.mark.capacity_integration` opts a test
    back into the real call."""
    if request.node.get_closest_marker("capacity_integration"):
        return
    from supervisor._vendor import usage_policy as up

    def unavailable(*_a, **_k):
        raise RuntimeError("capacity RPC disabled in the test suite (mark the test capacity_integration to allow it)")
    monkeypatch.setattr(up, "poll_codex", unavailable)


# ---- no real model client, ever (independent review of 1.6.0, finding 2)

_MODEL_CLIENTS = {"claude", "codex"}
_HARMLESS_FLAGS = {"--version", "-v", "--help", "-h"}


def _model_client_argv(args, kwargs):
    argv = args[0] if args else kwargs.get("args")
    if isinstance(argv, (str, bytes, os.PathLike)):
        argv = [argv]
    try:
        argv = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in (argv or [])]
    except TypeError:
        return None
    if not argv:
        return None
    head = pathlib.PurePath(argv[0].replace("\\", "/")).stem.lower()
    scripted = any("claude-code" in a.replace("\\", "/").lower() and a.lower().endswith("cli.js") for a in argv[:3])
    if (head in _MODEL_CLIENTS or scripted) and not (set(argv[1:]) & _HARMLESS_FLAGS):
        return argv
    return None


@pytest.fixture(autouse=True)
def _no_real_model_client(request, monkeypatch):
    """Every worker, reviewer and diagnosis lane in the suite must be a fake. A test that reaches the installed Claude
    or Codex CLI (a real, possibly billable model session) fails: the launch is refused on the spot and the test is
    failed at teardown with the command it tried. Version/help probes stay allowed. Opt out only with
    `@pytest.mark.model_integration` (capacity_integration does NOT authorize model jobs)."""
    if request.node.get_closest_marker("model_integration"):
        yield
        return
    import subprocess
    attempts = []
    real_popen, real_run = subprocess.Popen, subprocess.run

    class GuardedPopen(real_popen):
        def __init__(self, *args, **kwargs):
            argv = _model_client_argv(args, kwargs)
            if argv:
                attempts.append(argv[:6])
                raise OSError("test suite refused to launch a real model client: " + " ".join(argv[:4]))
            super().__init__(*args, **kwargs)

    def guarded_run(*args, **kwargs):
        argv = _model_client_argv(args, kwargs)
        if argv:
            attempts.append(argv[:6])
            raise OSError("test suite refused to launch a real model client: " + " ".join(argv[:4]))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    monkeypatch.setattr(subprocess, "run", guarded_run)
    yield
    if attempts:
        pytest.fail(f"test tried to launch a real model client {len(attempts)}x: {attempts[0]}", pytrace=False)
