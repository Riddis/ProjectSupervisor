import json, os, sys, time
import pytest

from supervisor import capacity, jobs, tasks
from supervisor._vendor.contract import CapacityDeferred
from supervisor.adapters import REGISTRY
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

FAKE = str(__import__("pathlib").Path(__file__).resolve().parent / "fixtures" / "fake_claude.py")


def ctask(layout, tid="C", mode="ok", reviewer=()):
    make_task(layout, tid, reviewer=reviewer)
    def fn(doc):
        doc["cost_category"] = "claude"
        doc["job"] = {"adapter": "claude_cli", "role": "engineer", "executable": sys.executable, "timeout_s": 60}
        doc["artifacts"] = ["out/fake.txt"]
        doc["tests"] = []                     # no declared tests → no test evidence required (verify.py)
    tasks._update(layout, tid, fn)
    os.environ["FAKE_CLAUDE_MODE"] = mode


class FakeClaude(REGISTRY["claude_cli"].__class__):
    name = "claude_cli"

    def command(self, spec):
        argv = super().command(spec)
        return [sys.executable, FAKE] + argv[1:]


@pytest.fixture(autouse=True)
def fake_adapter(monkeypatch):
    monkeypatch.setitem(REGISTRY, "claude_cli", FakeClaude())
    yield
    os.environ.pop("FAKE_CLAUDE_MODE", None)


def run_until_done(layout, tid, timeout=20):
    sup = Supervisor(layout, sleep_s=0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        sup.once()
        recs = jobs.for_task(layout, tid)
        if recs and (recs[-1]["phase"] in ("DONE", "FAILED") or float(recs[-1].get("deferred_until") or 0) > time.time()):
            return sup                                              # finished, or parked on a capacity deferral
        time.sleep(0.2)
    raise AssertionError("job never finished")


def test_write_scope_emits_both_a_relative_and_an_absolute_rule(layout):
    """a bare cwd-relative gitignore pattern never matched the absolute path Write/Edit
    actually receive, so the confined review lane's own HANDOFF.md write was silently denied."""
    from supervisor.adapters.claude_cli import write_scope
    run_dir = layout.runs / "T" / "T.a1"
    rules = write_scope(layout, run_dir, "run_dir")
    assert rules == ["runs/T/T.a1/**", "//" + run_dir.resolve().as_posix() + "/**"]
    assert write_scope(layout, run_dir, None) is None
    assert write_scope(layout, run_dir, "custom/path/**") == ["custom/path/**"]


def test_review_lane_command_allows_both_write_scope_forms(layout):
    make_task(layout, "R", reviewer=())
    def fn(doc):
        doc["job"] = {"adapter": "claude_cli", "role": "code_review", "write_scope": "run_dir", "tools": "reviewer"}
    tasks._update(layout, "R", fn)
    ad = REGISTRY["claude_cli"]
    spec = ad.prepare(layout, tasks.load(layout, "R"), "R.a1")
    assert isinstance(spec["write_scope"], list)             # the persisted spec carries the list form
    argv = ad.command(spec)
    allow = argv[argv.index("--allowedTools") + 1:]
    assert any(a.startswith("Write(runs/R/R.a1") for a in allow)
    assert any(a.startswith("Write(//") for a in allow)


def test_command_tolerates_a_stale_string_write_scope_from_a_pre_fix_job_record(layout):
    """a job record PREPARED before the list-form fix landed (and relaunched after it, e.g. a
    crash-recovery continuation) still carries write_scope as a single string on disk. command() used to iterate
    it character by character, silently stripping all write capability from the recovered attempt."""
    make_task(layout, "R2", reviewer=())
    def fn(doc):
        doc["job"] = {"adapter": "claude_cli", "role": "code_review", "write_scope": "run_dir", "tools": "reviewer"}
    tasks._update(layout, "R2", fn)
    ad = REGISTRY["claude_cli"]
    spec = ad.prepare(layout, tasks.load(layout, "R2"), "R2.a1")
    spec["write_scope"] = spec["write_scope"][0]              # simulate the pre-fix persisted shape
    argv = ad.command(spec)
    allow = argv[argv.index("--allowedTools") + 1:]
    write_rules = [a for a in allow if a.startswith("Write(")]
    assert write_rules == [f"Write({spec['write_scope']})"]  # exactly one rule for the whole scope, not one per char


def test_command_shape(layout):
    ctask(layout)
    ad = REGISTRY["claude_cli"]
    spec = ad.prepare(layout, tasks.load(layout, "C"), "C.a1")
    argv = ad.command(spec)
    assert argv[2:5] == ["-p", "--model", "sonnet"] and "--output-format" in argv and "stream-json" in argv
    assert "--permission-mode" in argv and "dontAsk" in argv and "--session-id" in argv
    assert "--allowedTools" in argv and any(a.startswith("Bash(python") for a in argv) and "Bash(git push *)" in argv
    assert "Edit" in argv[argv.index("--allowedTools"):] and "--permission-prompts" not in argv
    env = ad.env(spec)
    assert "CLAUDECODE" not in env and "ANTHROPIC_API_KEY" not in env and env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert (layout.runs / "C" / "C.a1" / "prompt.md").read_text(encoding="utf-8").count("HANDOFF_REQUEST.json") == 1


def test_ok_run_parses_result_and_capacity(layout):
    ctask(layout, mode="ok")
    run_until_done(layout, "C")
    t = tasks.load(layout, "C")
    assert t.status == TaskStatus.COMPLETE and t.paths == ["out/fake.txt"]
    run_dir = layout.runs / "C" / "C.a1"
    meta = json.loads((run_dir / "result_meta.json").read_text())
    assert meta["session_id"] and meta["model_usage"]["claude-sonnet-5"]["inputTokens"] == 100
    w = capacity.windows(layout)["claude"]["windows"]
    assert {x["name"]: x["remaining_percent"] for x in w} == {"five_hour": 58.0, "seven_day": 90.0}


def test_missing_json_triggers_one_followup(layout):
    ctask(layout, mode="nojson")
    run_until_done(layout, "C")
    run_dir = layout.runs / "C" / "C.a1"
    assert (run_dir / "followup_stdout.jsonl").exists()
    assert tasks.load(layout, "C").status == TaskStatus.COMPLETE


def test_auth_failure_is_reported(layout):
    ctask(layout, mode="auth")
    run_until_done(layout, "C")
    t = tasks.load(layout, "C")
    assert t.status == TaskStatus.FAILED and "login_required" in t.delivery.verification.detail


def test_limit_requeues_instead_of_failing(layout):
    ctask(layout, mode="limit")
    run_until_done(layout, "C")
    t = tasks.load(layout, "C")
    assert t.status == TaskStatus.IN_PROGRESS and t.delivery.digest is None   # a limit is not a failed delivery
    recs = jobs.for_task(layout, "C")
    assert [r["phase"] for r in recs] == ["DONE", "PREPARED"] and recs[0]["result"] == "DEFERRED"
    assert float(recs[1]["deferred_until"]) > time.time() + 60             # continuation waits for the window
    with pytest.raises(CapacityDeferred):
        capacity.require(layout, "claude")
    w = capacity.windows(layout)["claude"]["windows"]
    assert any(x["name"] == "five_hour" for x in w)                        # flat rate_limit_info shape ingested


def test_review_verdict_and_login_prose(layout):
    ctask(layout, mode="review")
    def fn(doc): doc["job"]["result_kind"] = "review"
    tasks._update(layout, "C", fn)
    run_until_done(layout, "C")
    t = tasks.load(layout, "C")
    rec = jobs.for_task(layout, "C")[-1]
    assert rec["result"] == "PARTIAL" and t.status != TaskStatus.COMPLETE      # REVISE never completes; /login prose ignored
    assert "login" not in (t.delivery.verification.detail or "")


def test_permission_denials_are_visible_not_fatal(layout):
    ctask(layout, mode="denied")
    run_until_done(layout, "C")
    t = tasks.load(layout, "C")
    assert t.status == TaskStatus.COMPLETE and "denial" in (t.result or "")
    assert json.loads((layout.runs / "C" / "C.a1" / "result.json").read_text())["permission_denials"]


def test_registry_has_both_cli_adapters():
    import supervisor.adapters as a
    assert {"claude_cli", "codex_cli"} <= set(a.REGISTRY)


def test_env_scrubbed(layout, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x"); monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "y"); monkeypatch.setenv("CLAUDECODE", "1")
    ctask(layout)
    env = REGISTRY["claude_cli"].env(REGISTRY["claude_cli"].prepare(layout, tasks.load(layout, "C"), "C.a1"))
    assert not ({"OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE", "ANTHROPIC_API_KEY"} & set(env))


def test_gated_engine_is_not_dispatched_until_reset(layout):
    ctask(layout, mode="limit")
    run_until_done(layout, "C")
    ctask(layout, tid="D", mode="ok")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    assert jobs.for_task(layout, "D") == []                     # gated: nothing dispatched, no attempt consumed
    capacity.owner_reset(layout, "claude", "test reset")        # releases the parked continuation too
    os.environ["FAKE_CLAUDE_MODE"] = "ok"
    run_until_done(layout, "D")
    run_until_done(layout, "C")
    assert tasks.load(layout, "C").status == TaskStatus.COMPLETE   # continuation attempt a2 completed the task
    assert tasks.load(layout, "D").status == TaskStatus.COMPLETE


def test_continuations_are_capped(layout):
    ctask(layout, mode="limit")
    import supervisor.jobs as J
    for _ in range(Supervisor.MAX_CONTINUATIONS + 1):
        run_until_done(layout, "C")
        for r in jobs.for_task(layout, "C"):
            if r["phase"] == "PREPARED":
                J.set_phase(layout, r["attempt_id"], "PREPARED", deferred_until=0)
        capacity.owner_reset(layout, "claude", "again")
    assert tasks.load(layout, "C").status == TaskStatus.BLOCKED
    assert len(jobs.for_task(layout, "C")) == Supervisor.MAX_CONTINUATIONS + 1


def test_crashed_requeue_is_repaired(layout):
    ctask(layout, mode="limit")
    run_until_done(layout, "C")
    import supervisor.jobs as J
    recs = jobs.for_task(layout, "C")
    (layout.jobs / f"{recs[1]['attempt_id']}.json").unlink()      # simulate: continuation never written
    J.set_phase(layout, recs[0]["attempt_id"], "DONE", continued=False)
    Supervisor(layout, sleep_s=0).reconcile()
    recs = jobs.for_task(layout, "C")
    assert len(recs) == 2 and recs[1]["phase"] == "PREPARED" and recs[0]["continued"] is True


def test_max_turns_is_failure_not_limit(layout, tmp_path):
    from supervisor.adapters import claude_cli
    ad = REGISTRY["claude_cli"]
    ctask(layout)
    spec = ad.prepare(layout, tasks.load(layout, "C"), "C.a1")
    rd = __import__("pathlib").Path(spec["run_dir"])
    (rd / "stdout.jsonl").write_text(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                                                 "result": "Reached max turns; limit resets in 2h", "session_id": "s"}) + "\n")
    r = ad.parse_result(spec, {})
    assert r.status == "FAILED" and "max turns" in r.error
