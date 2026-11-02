"""D-012 F-B Change 2: a lane whose login fails counts as unavailable, not failed, and hands over to the role's
configured fallback - the same way a role's own tasks already fall back on capacity (Change 1)."""
import os
import sys
import time

import pytest
import yaml

from supervisor import capacity, inbox, jobs, tasks
from supervisor._vendor.contract import CapacityDeferred
from supervisor.adapters import REGISTRY, Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

FAKE_CODEX = str(__import__("pathlib").Path(__file__).resolve().parent / "fixtures" / "fake_codex.py")
FAKE_CLAUDE = str(__import__("pathlib").Path(__file__).resolve().parent / "fixtures" / "fake_claude.py")


class FakeCodex(REGISTRY["codex_cli"].__class__):
    name = "codex_cli"

    def command(self, spec):
        return [sys.executable, FAKE_CODEX] + super().command(spec)[1:]


class FakeClaude(REGISTRY["claude_cli"].__class__):
    name = "claude_cli"

    def command(self, spec):
        argv = super().command(spec)
        return [sys.executable, FAKE_CLAUDE] + argv[1:]


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setitem(REGISTRY, "codex_cli", FakeCodex())
    monkeypatch.setitem(REGISTRY, "claude_cli", FakeClaude())
    os.environ["FAKE_CODEX_MODE"] = "auth"
    os.environ["FAKE_CLAUDE_MODE"] = "ok"
    yield
    os.environ.pop("FAKE_CODEX_MODE", None)
    os.environ.pop("FAKE_CLAUDE_MODE", None)


def write_routing(layout, fallback: dict | None) -> None:
    layout.config.mkdir(parents=True, exist_ok=True)
    roles = {"code_review": {"adapter": "codex_cli"}}
    if fallback is not None:
        roles["code_review"]["fallback"] = fallback
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({"roles": roles}), encoding="utf-8")


def own_task(layout, tid="A"):
    make_task(layout, tid, owner_role="code_review")
    def fn(doc):
        doc["job"] = {"adapter": "codex_cli", "role": "code_review", "executable": sys.executable, "timeout_s": 60}
        doc["artifacts"] = ["out/fake.txt"]
        doc["tests"] = []
    tasks._update(layout, tid, fn)


def run_until(layout, tid, predicate, timeout=20):
    sup = Supervisor(layout, sleep_s=0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        sup.once()
        if predicate():
            return sup
        time.sleep(0.1)
    raise AssertionError("condition never became true")


def test_a_login_failure_hands_the_role_owned_task_over_to_the_fallback_with_no_attempt_spent(layout):
    write_routing(layout, {"adapter": "claude_cli", "model": "fable", "effort": "high", "flag": "same_vendor"})
    own_task(layout, "A")
    run_until(layout, "A", lambda: tasks.load(layout, "A").status == TaskStatus.COMPLETE)
    recs = jobs.for_task(layout, "A")
    assert recs[0]["result"] == "REROUTED" and recs[0]["adapter"] == "codex_cli"
    assert recs[1]["adapter"] == "claude_cli" and recs[1].get("same_vendor") is True
    assert recs[1]["spec"]["model"] == "fable" and recs[1]["spec"]["effort"] == "high"
    t = tasks.load(layout, "A")
    assert t.status == TaskStatus.COMPLETE
    with pytest.raises(CapacityDeferred):
        capacity.require(layout, "codex")                    # the primary lane is still marked unavailable
    assert not list(layout.issues.glob("I-*.yaml"))            # no attempt spent: nothing escalated
    items = inbox.open_items(layout, "owner")                  # but the owner still hears the credential needs renewing
    assert len(items) == 1 and items[0].get("login_engine") == "codex"
    assert "codex" in (items[0]["subject"] + items[0]["summary"]).lower()


def test_a_login_failure_waits_token_free_with_no_fallback_configured(layout):
    write_routing(layout, None)
    own_task(layout, "B")
    run_until(layout, "B", lambda: len(jobs.for_task(layout, "B")) >= 2)
    recs = jobs.for_task(layout, "B")
    assert recs[0]["result"] == "DEFERRED" and recs[1]["phase"] == "PREPARED"
    assert float(recs[1].get("deferred_until") or 0) > time.time() + 60
    assert tasks.load(layout, "B").status == TaskStatus.IN_PROGRESS
    assert not list(layout.issues.glob("I-*.yaml"))
    items = inbox.open_items(layout, "owner")                  # no fallback either: the owner still hears about it
    assert len(items) == 1 and items[0].get("login_engine") == "codex"
    assert "codex" in (items[0]["subject"] + items[0]["summary"]).lower()


def test_two_consecutive_login_failures_on_the_same_engine_raise_exactly_one_deduped_owner_item(layout):
    """mark_login_failed() moves `until` forward on every call; fingerprinting the owner item on that value would
    stack one item per failure instead of one per engine per login window."""
    write_routing(layout, None)
    own_task(layout, "D")
    sup = Supervisor(layout, sleep_s=0)
    for i in (1, 2):
        rec = jobs.prepare(layout, "D", f"D.a{i}", "codex_cli", {"kind": "delivery"})
        sup._lane_login_failed(rec, Result(status="FAILED", error="login_required: run `codex login` on the workstation"),
                               "delivery")
    items = inbox.open_items(layout, "owner")
    assert len(items) == 1
    assert items[0].get("login_engine") == "codex"
    text = (items[0]["subject"] + " " + items[0]["summary"]).lower()
    assert "codex" in text and ("login" in text or "credential" in text)
    assert not list(layout.issues.glob("I-*.yaml"))            # still no attempt spent


def test_a_diagnosis_job_whose_lane_reports_login_required_reroutes_with_its_own_kind_and_extras_intact(layout):
    """reroute() used to lose a diagnosis's kind, prompt and diag_* extras: home_lane() kept this path
    unreachable for anything but a delivery until _lane_login_failed newly reached it for a diagnosis job too.
    Rerouting it through the plain (delivery-shaped) branch turned a login failure on a diagnosis lane into an
    extra plain delivery attempt of the task, and the diagnosis decision never happened."""
    write_routing(layout, {"adapter": "claude_cli", "model": "fable", "effort": "high", "flag": "same_vendor"})
    own_task(layout, "E")
    sup = Supervisor(layout, sleep_s=0)
    rec = jobs.write(layout, {"attempt_id": "E.a1", "task_id": "E", "adapter": "codex_cli", "phase": "RUNNING",
                             "spec": {"kind": "diagnosis"}, "handle": {}, "diag_role": "code_review",
                             "diag_level": "manager", "diag_signature": "sig1", "diag_source": "worker_failed",
                             "diag_detail": "it broke", "diag_issue": None, "diag_grant": 0})
    sup._lane_login_failed(rec, Result(status="FAILED", error="login_required: run `codex login` on the workstation"),
                           "diagnosis")
    recs = jobs.for_task(layout, "E")
    assert recs[0]["result"] == "REROUTED" and recs[0]["adapter"] == "codex_cli"
    new = recs[1]
    assert new["adapter"] == "claude_cli" and new.get("rerouted_from") == "E.a1"
    assert (new.get("spec") or {}).get("kind") == "diagnosis"          # not a fresh "delivery" attempt of the task
    assert new.get("diag_role") == "code_review" and new.get("diag_level") == "manager"
    assert new.get("diag_signature") == "sig1" and new.get("diag_source") == "worker_failed"
    assert new.get("diag_detail") == "it broke" and new.get("diag_grant") == 0
    assert all((r.get("spec") or {}).get("kind", "delivery") != "delivery" for r in recs)
