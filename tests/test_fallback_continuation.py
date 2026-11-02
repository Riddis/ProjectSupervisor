"""T2-107 (D-012 F-B, release review 2.2.0 finding F1): every continuation of a fallen-back attempt must keep
the fallback's resolved model/effort and its same-vendor flag, on all three paths that rebuild one -
`Supervisor.requeue()`'s delivery branch, `Supervisor.repair_continuations()`'s torn-write recovery of the same,
and a fallback review requeued mid-flight (`review.rebuild()`). Each regression fails at
ab1dc143ba3086ce844c1bade4473f20bb2289b0 (continuation keeps the ROLE PROFILE's model, no same_vendor, no
"same-vendor fallback" in the note) and passes once the fix reads the fallback's own keys back off the record
the way `_reroute_rebuild()` already does for a reroute."""
import time

import yaml

from supervisor import jobs, review, scheduler, tasks
from supervisor._vendor.contract import CapacityDeferred
from supervisor.adapters import Result
from supervisor.loop import Supervisor
from tests.conftest import make_task

FALLBACK = {"adapter": "claude_cli", "model": "fable", "effort": "high", "flag": "same_vendor"}
WANT = {"same_vendor": True, "model": "fable", "effort": "high"}


def write_routing(layout, roles):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({"roles": roles}), encoding="utf-8")


def deny_codex(monkeypatch):
    def deny(layout_, engine):
        if engine == "codex":
            raise CapacityDeferred(0.0, "codex out")
    monkeypatch.setattr("supervisor.capacity.require", deny)


def fallen_back_delivery(layout, monkeypatch, tid="A"):
    write_routing(layout, {"code_review": {"adapter": "codex_cli", "model": "gpt-6-astra", "effort": "high",
                                           "fallback": FALLBACK}})
    make_task(layout, tid, owner_role="code_review")
    deny_codex(monkeypatch)
    monkeypatch.setattr(Supervisor, "launch", lambda self, rec: rec)
    sup = Supervisor(layout, sleep_s=0)
    [(task, name)] = scheduler.plan(layout)
    return sup, sup.dispatch_delivery(task, name)


def lost(rec):
    got = {"same_vendor": rec.get("same_vendor"), "model": rec["spec"].get("model"), "effort": rec["spec"].get("effort")}
    return {k: v for k, v in got.items() if v != WANT[k]}


def test_a_requeued_continuation_of_a_fallen_back_delivery_keeps_the_fallback_s_keys_and_note(layout, monkeypatch):
    """requeue()'s delivery branch: a usage limit mid-run queues a continuation that must still carry the
    fallback's model/effort and flag, and its attempt note must say so."""
    sup, rec = fallen_back_delivery(layout, monkeypatch)
    assert not lost(rec)
    tasks.set_status(layout, "A", "IN_PROGRESS")
    running = jobs.set_phase(layout, rec["attempt_id"], "RUNNING", handle={})
    sup.requeue(running, Result(status="DEFERRED", error="usage limit reported by the CLI: 5-hour window",
                                reset_epoch=time.time() + 60, raw={"measured_window": True}), "capacity limit")
    recs = jobs.for_task(layout, "A")
    assert len(recs) == 2 and recs[0].get("continued") is True
    cont = recs[1]
    assert cont["phase"] == "PREPARED" and cont["adapter"] == "claude_cli"
    assert not lost(cont), f"continuation lost the fallback's keys/flag: {lost(cont)} (expected {WANT})"
    note = tasks.load(layout, "A").attempts[-1].note
    assert "same-vendor fallback" in note, f"attempt note does not say same-vendor fallback: {note!r}"


def test_a_torn_continuation_repair_keeps_the_fallback_s_keys_and_note(layout, monkeypatch):
    """repair_continuations(): a crash after closing the fallen-back attempt but before queuing its continuation
    must recover the SAME fallback keys/flag/note a live requeue() would have carried."""
    sup, rec = fallen_back_delivery(layout, monkeypatch)
    tasks.set_status(layout, "A", "IN_PROGRESS")
    jobs.set_phase(layout, rec["attempt_id"], "DONE", result="DEFERRED", finished="2026-11-02T00:00:00+00:00",
                   error="usage limit")
    fresh = Supervisor(layout, sleep_s=0)
    fresh.repair_continuations()
    recs = jobs.for_task(layout, "A")
    assert len(recs) == 2 and recs[1]["phase"] == "PREPARED" and recs[1]["adapter"] == "claude_cli"
    assert not lost(recs[1]), f"repaired continuation lost the fallback's keys/flag: {lost(recs[1])}"
    note = tasks.load(layout, "A").attempts[-1].note
    assert "same-vendor fallback" in note, f"repaired attempt note does not say same-vendor fallback: {note!r}"


def test_a_rebuilt_fallback_review_keeps_the_fallback_s_keys_and_note(layout, monkeypatch):
    """review.rebuild() (reached from requeue()'s review branch on a usage limit mid-review) must keep the
    fallback's model/effort too - the flag already survives (loop.py:216); the note must say so as well."""
    write_routing(layout, {"engineer": {"adapter": "claude_cli", "model": "sonnet"},
                           "code_review": {"adapter": "codex_cli", "model": "gpt-6-astra", "effort": "high",
                                           "fallback": FALLBACK}})
    make_task(layout, "RV2", owner_role="engineer", reviewer=["code_review"], status="IN_PROGRESS")
    tasks.record_delivery(layout, "RV2", "e" * 64, actor="loop", result=None, paths=[])
    tasks.set_status(layout, "RV2", "WAITING_FOR_REVIEW")
    deny_codex(monkeypatch)
    monkeypatch.setattr(Supervisor, "launch", lambda self, rec: rec)
    sup = Supervisor(layout, sleep_s=0)
    rec = review.dispatch(sup, tasks.load(layout, "RV2"), "code_review")
    assert rec["adapter"] == "claude_cli" and rec["spec"]["model"] == "fable" and rec.get("same_vendor") is True
    running = jobs.set_phase(layout, rec["attempt_id"], "RUNNING", handle={})
    sup.requeue(running, Result(status="DEFERRED", error="usage limit reported by the CLI", reset_epoch=time.time() + 60,
                                raw={"measured_window": True}), "capacity limit")
    cont = jobs.for_task(layout, "RV2")[1]
    assert cont["adapter"] == "claude_cli" and cont.get("same_vendor") is True and cont["spec"]["kind"] == "review"
    assert cont["spec"]["model"] == "fable", f"review continuation model is {cont['spec']['model']!r}, not the fallback's"
    assert cont["spec"].get("effort") == "high"
    note = tasks.load(layout, "RV2").attempts[-1].note
    assert "same-vendor fallback" in note, f"review continuation note does not say same-vendor fallback: {note!r}"
