import json
from pathlib import Path

import pytest

from supervisor import scheduler, tasks
from supervisor.adapters import REGISTRY, claude_api
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


def plain_claude_task(layout, tid):
    """A task routed to claude_cli by cost_category alone (no explicit job.adapter) — the shape the unattended switch acts on."""
    make_task(layout, tid)
    def fn(doc):
        doc["cost_category"] = "claude"
    tasks._update(layout, tid, fn)


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
    raw = json.loads((Path(spec["run_dir"]) / "result.json").read_text(encoding="utf-8"))
    assert raw["raw"]["kind"] == "delivery" and raw["raw"]["schema_version"] == 1   # a real parsed JSON object, not just text


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


def test_unattended_switch_picks_claude_api_when_credits_available(layout):
    write_key(layout)
    plain_claude_task(layout, "CB")
    plan = scheduler.plan(layout, unattended=True)
    assert {t.id: name for t, name in plan}["CB"] == "claude_api"


def test_unattended_switch_stays_on_claude_cli_without_a_key(layout):
    plain_claude_task(layout, "CD")          # no secrets/.env: credits_remaining() is False
    plan = scheduler.plan(layout, unattended=True)
    assert {t.id: name for t, name in plan}["CD"] == "claude_cli"


def test_interactive_runs_are_unaffected_by_a_linked_key(layout):
    write_key(layout)
    plain_claude_task(layout, "CC")
    plan = scheduler.plan(layout, unattended=False)
    assert {t.id: name for t, name in plan}["CC"] == "claude_cli"
