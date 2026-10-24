import json
from pathlib import Path

import pytest
import yaml

from supervisor import loop as loop_mod
from supervisor import scheduler, tasks
from supervisor import verify as verify_mod
from supervisor.adapters import REGISTRY, claude_api
from supervisor.loop import Supervisor
from tests.conftest import make_task

DELIVERY_JSON = {"kind": "delivery", "schema_version": 1, "status": "COMPLETE", "artifacts": [],
                 "tests": [], "notes": "done", "decisions": []}


def api_task(layout, tid="CA"):
    make_task(layout, tid)
    def fn(doc):
        doc["cost_category"] = "claude"
        doc["job"] = {"adapter": "claude_api", "role": "engineer"}
        doc["artifacts"] = []
        doc["tests"] = []
    tasks._update(layout, tid, fn)


def text_task(layout, tid, job=None, artifacts=(), tests=()):
    """A task routed to claude_cli by cost_category alone (no explicit job.adapter) — the shape the unattended
    switch acts on. Defaults to the only shape it may switch: a contract that needs no files and no commands.
    The switch is opt-in per project (capacity.unattended_prefers_api_credits), so a routing.yaml saying so is
    written unless the test already wrote one."""
    if not (layout.config / "routing.yaml").exists():
        write_routing(layout)
    make_task(layout, tid)
    def fn(doc):
        doc["cost_category"] = "claude"
        doc["artifacts"] = list(artifacts)
        doc["tests"] = list(tests)
        doc["job"] = dict(job or {"api_credits": True})
    tasks._update(layout, tid, fn)


def plain_claude_task(layout, tid):
    """text_task without the routing.yaml side effect: an opted-in, tool-free task on the cost-category default
    lane, for tests that write their own routing.yaml (or deliberately none)."""
    make_task(layout, tid)
    def fn(doc):
        doc["cost_category"] = "claude"
        doc["artifacts"] = []
        doc["tests"] = []
        doc["job"] = {"api_credits": True}
    tasks._update(layout, tid, fn)


def write_routing(layout, **claude_api_cfg):
    layout.config.mkdir(parents=True, exist_ok=True)
    doc = {"version": 1, "capacity": {"unattended_prefers_api_credits": True}, "claude_api": claude_api_cfg}
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def write_key(layout, key="sk-ant-test-key"):
    layout.secrets.mkdir(parents=True, exist_ok=True)
    (layout.secrets / ".env").write_text(f"ANTHROPIC_API_KEY={key}\n", encoding="utf-8")


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or json.dumps(payload or {})

    def json(self):
        return self._payload


def messages_body(text, input_tokens=100, output_tokens=50):
    return {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


@pytest.fixture
def ad():
    return REGISTRY["claude_api"]


def test_probe_returns_json(layout, ad, monkeypatch):
    write_key(layout)
    api_task(layout)
    text = "All done.\n```json\n" + json.dumps(DELIVERY_JSON) + "\n```\n"
    monkeypatch.setattr(ad, "_post", lambda *a, **kw: FakeResponse(messages_body(text)))
    spec = ad.prepare(layout, tasks.load(layout, "CA"), "CA.a1")
    handle = ad.submit(spec)
    result = ad.collect(spec, handle)
    assert result.status == "COMPLETE"
    envelope = json.loads((Path(spec["run_dir"]) / "result.json").read_text(encoding="utf-8"))
    assert envelope["result"]["kind"] == "delivery" and envelope["result"]["schema_version"] == 1  # parsed JSON, not text
    assert result.raw == DELIVERY_JSON        # Result.raw is the structured object itself, as claude_cli returns it


def test_probe_rejects_a_response_without_structured_json(layout, ad, monkeypatch):
    write_key(layout)
    api_task(layout)
    monkeypatch.setattr(ad, "_post", lambda *a, **kw: FakeResponse(messages_body("I did some things, no JSON here.")))
    spec = ad.prepare(layout, tasks.load(layout, "CA"), "CA.a1")
    handle = ad.submit(spec)
    result = ad.collect(spec, handle)
    assert result.status == "FAILED" and "structured result" in result.error


def test_ledger_entry_marks_api_credits(layout, ad, monkeypatch):
    write_key(layout)
    api_task(layout)
    text = "```json\n" + json.dumps(DELIVERY_JSON) + "\n```\n"
    monkeypatch.setattr(ad, "_post", lambda *a, **kw: FakeResponse(messages_body(text, 1000, 500)))
    spec = ad.prepare(layout, tasks.load(layout, "CA"), "CA.a1")
    ad.submit(spec)
    lines = claude_api.ledger_path(layout).read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines]
    assert any(e["category"] == "api_credits" and e["task_id"] == "CA" and e["input_tokens"] == 1000
              and e["output_tokens"] == 500 for e in entries)


def test_adapter_disabled_when_key_missing(layout, ad):
    api_task(layout)                        # no secrets/.env written
    assert claude_api.api_key(layout) is None
    assert not claude_api.available(layout)
    spec = ad.prepare(layout, tasks.load(layout, "CA"), "CA.a1")
    handle = ad.submit(spec)                 # must not raise
    result = ad.collect(spec, handle)
    assert result.status == "FAILED"
    assert "ANTHROPIC_API_KEY" in result.error


def test_disabled_call_never_reaches_the_network(layout, ad, monkeypatch):
    api_task(layout)
    called = []
    monkeypatch.setattr(ad, "_post", lambda *a, **kw: called.append(1) or FakeResponse(messages_body("x")))
    spec = ad.prepare(layout, tasks.load(layout, "CA"), "CA.a1")
    ad.submit(spec)
    assert not called


def test_crash_between_submit_and_collect_never_resubmits(layout, ad, monkeypatch):
    write_key(layout)
    api_task(layout)
    calls = []
    def fake_post(*a, **kw):
        calls.append(1)
        return FakeResponse(messages_body("```json\n" + json.dumps(DELIVERY_JSON) + "\n```\n"))
    monkeypatch.setattr(ad, "_post", fake_post)
    spec = ad.prepare(layout, tasks.load(layout, "CA"), "CA.a1")
    ad.submit(spec)
    assert len(calls) == 1
    recovered = ad.recover(spec)             # a fresh loop after a crash finds the finished result
    assert recovered is not None
    ad.submit(spec)                          # re-entering submit() (e.g. a retried launch()) must not re-spend the call
    assert len(calls) == 1


def test_interrupted_before_the_ledger_append_never_calls_the_api_again(layout, ad, monkeypatch):
    """Killed after the response was persisted but before it was billed: the resumed submit()
    must finish from response.json, not POST a second time."""
    write_key(layout)
    api_task(layout)
    calls = []
    def fake_post(*a, **kw):
        calls.append(1)
        return FakeResponse(messages_body("```json\n" + json.dumps(DELIVERY_JSON) + "\n```\n"))
    monkeypatch.setattr(ad, "_post", fake_post)
    spec = ad.prepare(layout, tasks.load(layout, "CA"), "CA.a1")

    crash, real_record = {"on": True}, claude_api.record_usage
    def maybe_boom(*a, **kw):
        if crash["on"]:
            raise RuntimeError("killed before the ledger append")
        return real_record(*a, **kw)
    monkeypatch.setattr(claude_api, "record_usage", maybe_boom)
    with pytest.raises(RuntimeError):
        ad.submit(spec)
    run_dir = Path(spec["run_dir"])
    assert (run_dir / "response.json").exists() and not (run_dir / "result.json").exists()
    assert not claude_api.ledger_path(layout).exists()

    crash["on"] = False                                  # fresh process after the crash, same run dir
    handle = ad.submit(spec)
    assert len(calls) == 1                               # the paid call was NOT repeated
    assert ad.collect(spec, handle).status == "COMPLETE"
    assert len(claude_api.entries(layout)) == 1


def test_interrupted_between_the_ledger_append_and_the_result_bills_once(layout, ad, monkeypatch):
    """Killed after the ledger append but before result.json: resuming must neither POST again nor bill twice."""
    write_key(layout)
    api_task(layout)
    calls = []
    def fake_post(*a, **kw):
        calls.append(1)
        return FakeResponse(messages_body("```json\n" + json.dumps(DELIVERY_JSON) + "\n```\n"))
    monkeypatch.setattr(ad, "_post", fake_post)
    spec = ad.prepare(layout, tasks.load(layout, "CA"), "CA.a1")

    crash, real_atomic_json = {"on": True}, claude_api.atomic_json
    def die_on_result(path, data):
        if crash["on"] and Path(path).name == "result.json":
            raise RuntimeError("killed before the result was written")
        return real_atomic_json(path, data)
    monkeypatch.setattr(claude_api, "atomic_json", die_on_result)
    with pytest.raises(RuntimeError):
        ad.submit(spec)
    assert len(claude_api.entries(layout)) == 1          # billed once, result not yet written
    assert not (Path(spec["run_dir"]) / "result.json").exists()
    assert ad.recover(spec) is None                      # nothing for the loop to collect yet

    crash["on"] = False
    handle = ad.submit(spec)
    assert len(calls) == 1
    assert ad.collect(spec, handle).status == "COMPLETE"
    entries = claude_api.entries(layout)
    assert len(entries) == 1 and entries[0]["response_id"] == "msg_1"   # idempotent per response, not appended twice


def test_declared_tests_reach_verification_through_the_supervisor(layout, ad, monkeypatch):
    """The adapter→supervisor boundary: loop.ingest reads the delivery's `tests` off
    Result.raw, so a delivery declaring test commands must arrive at verification with them."""
    write_key(layout)
    api_task(layout)
    declared = [{"command": "python -m pytest tests/test_claude_api.py", "exit_code": 0}]
    text = "```json\n" + json.dumps({**DELIVERY_JSON, "tests": declared}) + "\n```\n"
    monkeypatch.setattr(ad, "_post", lambda *a, **kw: FakeResponse(messages_body(text)))
    seen = {}
    def fake_verify(lay, task, files, declared_tests=None, *a, **kw):
        seen["tests"] = declared_tests
        return verify_mod.Outcome(True, "ok")
    monkeypatch.setattr(loop_mod.verify_mod, "verify", fake_verify)
    sup = Supervisor(layout, sleep_s=0)
    rec = sup.dispatch(tasks.load(layout, "CA"), "claude_api")
    result = ad.collect(rec["spec"], rec["handle"])
    sup.ingest(rec, result)
    assert seen["tests"] == declared


def test_unattended_switch_picks_claude_api_for_an_opted_in_text_task(layout):
    write_key(layout)
    text_task(layout, "CB")                  # job.api_credits, nothing to write, nothing to run
    plan = scheduler.plan(layout, unattended=True)
    assert {t.id: name for t, name in plan}["CB"] == "claude_api"


def test_unattended_switch_picks_claude_api_for_a_configured_eligible_role(layout):
    write_key(layout)
    write_routing(layout, eligible_roles=["research_strategist"])
    text_task(layout, "CE", job={"role": "research_strategist"})
    plan = scheduler.plan(layout, unattended=True)
    assert {t.id: name for t, name in plan}["CE"] == "claude_api"


def test_unattended_switch_keeps_engineering_tasks_on_claude_cli(layout):
    """A tool-less lane cannot read inputs, write artifacts or run tests: the switch must not take work it cannot
    finish."""
    write_key(layout)
    text_task(layout, "CH", artifacts=["out/thing.txt"], tests=["pytest tests/x.py"], job={"role": "engineer"})
    plan = scheduler.plan(layout, unattended=True)
    assert {t.id: name for t, name in plan}["CH"] == "claude_cli"


def test_a_contract_needing_files_or_commands_is_never_eligible(layout):
    """Per-shape eligibility (a plan admits only one exclusive writer at a time, so this checks the predicate
    the scheduler calls). An opt-in flag does not override a contract the lane cannot satisfy."""
    write_key(layout)
    text_task(layout, "CF", artifacts=["out/thing.txt"])                     # must write a file
    text_task(layout, "CG", tests=["pytest tests/x.py"])                     # must run a command
    text_task(layout, "CI")
    def add_check(doc):
        doc["checks"] = ["python -m pytest tests/x.py"]                      # an executable contract check
    tasks._update(layout, "CI", add_check)
    for tid in ("CF", "CG", "CI"):
        t = tasks.load(layout, tid)
        assert not claude_api.unattended_eligible(layout, t, scheduler.job_spec(t)), tid
    text_task(layout, "CL")                                                  # the same opt-in, nothing to produce
    t = tasks.load(layout, "CL")
    assert claude_api.unattended_eligible(layout, t, scheduler.job_spec(t))


def test_unattended_switch_respects_an_explicit_adapter_choice(layout):
    write_key(layout)
    text_task(layout, "CJ", job={"adapter": "claude_cli", "api_credits": True})
    plan = scheduler.plan(layout, unattended=True)
    assert {t.id: name for t, name in plan}["CJ"] == "claude_cli"


def test_unattended_switch_stays_on_claude_cli_without_a_key(layout):
    text_task(layout, "CD")                  # no secrets/.env: credits_remaining() is False
    plan = scheduler.plan(layout, unattended=True)
    assert {t.id: name for t, name in plan}["CD"] == "claude_cli"


def test_unattended_switch_stays_on_claude_cli_without_an_opt_in(layout):
    write_key(layout)
    text_task(layout, "CK", job={"role": "engineer"})   # eligible_roles is empty by default: no automatic switch
    plan = scheduler.plan(layout, unattended=True)
    assert {t.id: name for t, name in plan}["CK"] == "claude_cli"


def test_interactive_runs_are_unaffected_by_a_linked_key(layout):
    write_key(layout)
    text_task(layout, "CC")
    plan = scheduler.plan(layout, unattended=False)
    assert {t.id: name for t, name in plan}["CC"] == "claude_cli"
