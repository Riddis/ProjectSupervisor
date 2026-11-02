"""T2-108 (release review 2.2.0 finding F2): a `fallback:` block written without `flag: same_vendor` must still
keep its model/effort on a role-owned DELIVERY, exactly as it already does on a review (`review.dispatch()`,
`review.py:127-134`) and on a login reroute (`_lane_login_failed()` -> `reroute()`, `loop.py:515-526`) - both
apply the fallback's overrides whatever the flag says and use the flag only for the note and the `same_vendor`
extra. `dispatch_delivery()` alone gated the overrides on the flag itself (`if not same_vendor or lane != name:
return self.dispatch(task, name)`, pre-fix `loop.py:163-164`), so a flag-less block dropped the overrides on a
delivery and ran the role's own model instead - for a Claude fallback of a Codex role, a model that cannot run.

This regression fails at ab1dc143ba3086ce844c1bade4473f20bb2289b0 (delivery model is 'gpt-6-astra': the
fallback's model was dropped) and at T2-107's head d5585dc (same gate, untouched by that task - see
docs/releases/2.2.0/F1-fallback-continuations.md "What this leaves alone"). It passes once dispatch_delivery()
applies non-empty overrides whenever lane == name, letting the flag decide only the note and the same_vendor
extra - same_vendor recorded False (not True, and not absent) on the flag-less delivery."""
import yaml

from supervisor import review, scheduler, tasks
from supervisor._vendor.contract import CapacityDeferred
from supervisor.loop import Supervisor
from tests.conftest import make_task

FALLBACK_NO_FLAG = {"adapter": "claude_cli", "model": "fable", "effort": "high"}


def write_routing(layout, roles):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({"roles": roles}), encoding="utf-8")


def deny_codex(monkeypatch):
    def deny(layout_, engine):
        if engine == "codex":
            raise CapacityDeferred(0.0, "codex out")
    monkeypatch.setattr("supervisor.capacity.require", deny)


def test_a_flagless_fallback_block_keeps_its_model_and_effort_on_a_role_owned_delivery(layout, monkeypatch):
    write_routing(layout, {"code_review": {"adapter": "codex_cli", "model": "gpt-6-astra", "effort": "high",
                                           "fallback": FALLBACK_NO_FLAG}})
    make_task(layout, "B", owner_role="code_review")
    deny_codex(monkeypatch)
    monkeypatch.setattr(Supervisor, "launch", lambda self, rec: rec)
    sup = Supervisor(layout, sleep_s=0)
    [(task, name)] = scheduler.plan(layout)
    rec = sup.dispatch_delivery(task, name)
    assert rec["adapter"] == "claude_cli"
    assert rec["spec"]["model"] == "fable", f"delivery model is {rec['spec']['model']!r}: the fallback's model was dropped"
    assert rec["spec"].get("effort") == "high"
    assert rec.get("same_vendor") is False, f"same_vendor is {rec.get('same_vendor')!r}, expected False (not True, not absent)"
    note = tasks.load(layout, "B").attempts[-1].note
    assert "same-vendor fallback" not in note, f"flag-less dispatch should not claim same-vendor: {note!r}"


def test_control_the_same_flagless_block_already_keeps_its_model_on_a_review(layout, monkeypatch):
    """Control: the same flag-less block, on review.dispatch(), already keeps the model and records same_vendor
    False - this is what dispatch_delivery() must now match (test_probe_2_2_0.py's own control, reproduced here
    so this file stands on its own)."""
    write_routing(layout, {"engineer": {"adapter": "claude_cli", "model": "sonnet"},
                           "code_review": {"adapter": "codex_cli", "model": "gpt-6-astra", "effort": "high",
                                           "fallback": FALLBACK_NO_FLAG}})
    make_task(layout, "RV", owner_role="engineer", reviewer=["code_review"], status="IN_PROGRESS")
    tasks.record_delivery(layout, "RV", "d" * 64, actor="loop", result=None, paths=[])
    tasks.set_status(layout, "RV", "WAITING_FOR_REVIEW")
    deny_codex(monkeypatch)
    monkeypatch.setattr(Supervisor, "launch", lambda self, rec: rec)
    sup = Supervisor(layout, sleep_s=0)
    rec = review.dispatch(sup, tasks.load(layout, "RV"), "code_review")
    assert rec["adapter"] == "claude_cli" and rec["spec"]["model"] == "fable" and rec.get("same_vendor") is False
