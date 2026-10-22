import json, os, sys, time
import pytest

from supervisor import jobs, tasks
from supervisor.adapters import REGISTRY
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

FAKE = str(__import__("pathlib").Path(__file__).resolve().parent / "fixtures" / "fake_codex.py")


class FakeCodex(REGISTRY["codex_cli"].__class__):
    name = "codex_cli"

    def command(self, spec):
        return [sys.executable, FAKE] + super().command(spec)[1:]


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setitem(REGISTRY, "codex_cli", FakeCodex())
    yield
    os.environ.pop("FAKE_CODEX_MODE", None)


def rtask(layout, mode="ok"):
    make_task(layout, "R")
    def fn(doc):
        doc["cost_category"] = "codex"
        (layout.root / "img").mkdir(exist_ok=True); (layout.root / "img" / "a.png").write_bytes(b"\x89PNG")
        doc["job"] = {"adapter": "codex_cli", "role": "code_review", "result_kind": "review", "executable": sys.executable,
                      "images": ["img/a.png"], "timeout_s": 60}
        doc["artifacts"] = []
        doc["tests"] = []
    tasks._update(layout, "R", fn)
    os.environ["FAKE_CODEX_MODE"] = mode


def run(layout):
    sup = Supervisor(layout, sleep_s=0)
    for _ in range(50):
        sup.once()
        recs = jobs.for_task(layout, "R")
        if recs and (recs[-1]["phase"] in ("DONE", "FAILED") or float(recs[-1].get("deferred_until") or 0) > time.time()):
            return
        time.sleep(0.2)
    raise AssertionError("never finished")


def test_command_shape_and_env(layout):
    rtask(layout)
    ad = REGISTRY["codex_cli"]
    spec = ad.prepare(layout, tasks.load(layout, "R"), "R.a1")
    argv = ad.command(spec)
    assert argv[2:7] == ["-a", "never", "exec", "--ephemeral", "-s"] and "--json" in argv and "--output-last-message" in argv
    assert "-m" not in argv                                            # no model unless routing/job names one
    assert argv[-3:] == ["-i", "img/a.png", "-"] and spec["sandbox"] == "read-only"
    env = ad.env(spec)
    assert "OPENAI_API_KEY" not in env and env["HOME"]


def test_ok_review_parsed(layout):
    rtask(layout, "ok")
    run(layout)
    rd = layout.runs / "R" / "R.a1"
    obj = json.loads((rd / "result.json").read_text())
    meta = json.loads((rd / "result_meta.json").read_text())
    assert obj["verdict"] == "ACCEPT" and meta["session_id"] == "thr_123" and meta["usage"]["input_tokens"] == 120
    assert tasks.load(layout, "R").status == TaskStatus.COMPLETE


def test_limit_records_capacity_and_requeues(layout, monkeypatch):
    from supervisor.adapters import codex_cli
    monkeypatch.setattr(codex_cli, "poll_capacity", lambda layout, max_age_s=0: None)   # RPC unavailable → synthetic window
    rtask(layout, "limit")
    run(layout)
    from supervisor import capacity
    from supervisor._vendor.contract import CapacityDeferred
    with pytest.raises(CapacityDeferred):
        capacity.require(layout, "codex")
    recs = jobs.for_task(layout, "R")
    assert recs[0]["result"] == "DEFERRED" and recs[1]["phase"] == "PREPARED" and tasks.load(layout, "R").status == TaskStatus.IN_PROGRESS


def test_poll_capacity_populates_usage_file(layout, monkeypatch):
    from supervisor.adapters import codex_cli
    from supervisor._vendor import usage_policy as up
    calls = []
    def fake_poll(exe, env=None, timeout=10, client_name=""):
        calls.append(env)
        return up.record("codex", [{"name": "primary", "used_percent": 95, "reset_epoch": time.time() + 900}], "fake-rpc")
    monkeypatch.setattr(up, "poll_codex", fake_poll)
    obs = codex_cli.poll_capacity(layout)
    assert obs["windows"][0]["remaining_percent"] == 5 and (layout.runtime / "usage_codex.json").exists()
    assert "OPENAI_API_KEY" not in (calls[0] or {})
    codex_cli.poll_capacity(layout, max_age_s=600)
    assert len(calls) == 1                                                     # fresh observation: no second RPC
    Supervisor(layout, sleep_s=0).once()                                        # loop polls on a timer, never crashes
    assert len(calls) == 1


def test_revise_verdict_is_not_complete(layout):
    rtask(layout, "revise")
    run(layout)
    assert jobs.for_task(layout, "R")[-1]["result"] == "PARTIAL" and tasks.load(layout, "R").status != TaskStatus.COMPLETE


@pytest.mark.parametrize("mode,needle", [("auth", "login_required"), ("fail", "turn failed"), ("nojson", "no structured")])
def test_failure_modes(layout, mode, needle):
    rtask(layout, mode)
    run(layout)
    t = tasks.load(layout, "R")
    assert t.status == TaskStatus.FAILED and needle in t.delivery.verification.detail


def test_poll_failure_backs_off(layout, monkeypatch):
    from supervisor.adapters import codex_cli
    from supervisor._vendor import usage_policy as up
    calls = []
    def boom(*a, **k):
        calls.append(1); raise RuntimeError("Usage RPC timed out")
    monkeypatch.setattr(up, "poll_codex", boom)
    codex_cli._last_failed_poll.clear()
    assert codex_cli.poll_capacity(layout, max_age_s=600) is None
    assert codex_cli.poll_capacity(layout, max_age_s=600) is None
    assert len(calls) == 1                                   # second call within the window spawns nothing
    assert codex_cli.poll_capacity(layout) is None and len(calls) == 2   # unconditional (post-limit) poll still runs


def test_nonfatal_error_event_before_completion(layout):
    from supervisor.adapters import codex_cli
    import json as J
    ad = codex_cli.CodexCliAdapter()
    rtask(layout)
    spec = ad.prepare(layout, tasks.load(layout, "R"), "R.a1")
    rd = __import__("pathlib").Path(spec["run_dir"])
    rd.joinpath("stdout.jsonl").write_text("\n".join(J.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "t"}, {"type": "error", "message": "stream disconnected; retrying"},
        {"type": "turn.completed", "usage": {}}]) + "\n")
    rd.joinpath("result.txt").write_text('```json\n{"kind":"review","schema_version":1,"verdict":"ACCEPT","findings":[]}\n```')
    assert ad.parse_result(spec, {}).status == "COMPLETE"
