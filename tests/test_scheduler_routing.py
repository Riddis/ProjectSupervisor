"""Delivery routing: the owner role's configured lane wins over cost_category, a dormant role is never dispatched,
and `--unattended` only spends API credits when routing.yaml says it may."""
import yaml

from supervisor import scheduler, tasks
from supervisor.adapters import REGISTRY
from tests.conftest import make_task
from tests.test_claude_api import plain_claude_task, write_key


def write_routing(layout, doc: dict) -> None:
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def picks(layout, **kw) -> dict:
    return {t.id: name for t, name in scheduler.plan(layout, **kw)}


def skip_reasons(layout, task_id: str) -> list[str]:
    log = layout.state / "log.jsonl"
    if not log.exists():
        return []
    import json
    return [e["reason"] for e in (json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip())
            if e.get("event") == "schedule_skip" and e.get("task") == task_id]


# ------------------------------------------------------------------ finding 1: role adapter and active flag

def test_role_adapter_wins_over_cost_category(layout):
    """The default `cost_category: claude` must not pull an engineer configured for Codex back onto Claude."""
    assert "codex_cli" in REGISTRY
    write_routing(layout, {"roles": {"engineer": {"adapter": "codex_cli", "active": True}}})
    make_task(layout, "R1")                                   # owner_role engineer, cost_category claude
    assert scheduler.adapter_name(tasks.load(layout, "R1"), layout) == "codex_cli"
    assert picks(layout)["R1"] == "codex_cli"


def test_job_adapter_still_overrides_the_role_lane(layout):
    write_routing(layout, {"roles": {"engineer": {"adapter": "codex_cli", "active": True}}})
    make_task(layout, "R2")
    tasks._update(layout, "R2", lambda d: d.update(job={"adapter": "python", "fn": "x"}))
    assert scheduler.adapter_name(tasks.load(layout, "R2"), layout) == "python"


def test_non_model_cost_lane_is_kept_whoever_owns_the_task(layout):
    """A deterministic/local/human task owned by an agent role is not agent work: its cost lane stands."""
    write_routing(layout, {"roles": {"engineer": {"adapter": "codex_cli", "active": True}},
                           "cost_lanes": {"local": "python"}})
    make_task(layout, "R2b")
    for category, lane in (("deterministic", "python"), ("local", "python"), ("human", "human")):
        tasks._update(layout, "R2b", lambda d: d.update(cost_category=category))
        assert scheduler.adapter_name(tasks.load(layout, "R2b"), layout) == lane
    for category in ("claude", "mixed", "paid"):
        tasks._update(layout, "R2b", lambda d: d.update(cost_category=category))
        assert scheduler.adapter_name(tasks.load(layout, "R2b"), layout) == "codex_cli"


def test_unconfigured_role_falls_back_to_cost_lane(layout):
    write_routing(layout, {"roles": {"senior_pm": {"adapter": "codex_cli", "active": True}}})
    make_task(layout, "R3")                                   # engineer is not in roles
    assert scheduler.adapter_name(tasks.load(layout, "R3"), layout) == "claude_cli"


def test_inactive_role_is_parked_not_dispatched_on_another_lane(layout):
    write_routing(layout, {"roles": {"engineer": {"adapter": "codex_cli", "active": False}}})
    make_task(layout, "R4")
    assert "R4" not in picks(layout)
    reasons = skip_reasons(layout, "R4")
    assert reasons and "inactive" in reasons[0] and "engineer" in reasons[0]
    assert "R4" not in picks(layout)                          # still parked on the next tick
    assert len(skip_reasons(layout, "R4")) == 1               # ...and the reason is logged once, not every tick
    assert tasks.load(layout, "R4").status == "NOT_STARTED"   # parked, not failed or consumed


def test_inactive_role_with_explicit_job_adapter_is_still_parked(layout):
    write_routing(layout, {"roles": {"engineer": {"adapter": "claude_cli", "active": False}}})
    make_task(layout, "R5")
    tasks._update(layout, "R5", lambda d: d.update(job={"adapter": "python", "fn": "x"}))
    assert "R5" not in picks(layout)


def test_role_without_active_key_is_dispatched(layout):
    write_routing(layout, {"roles": {"engineer": {"adapter": "claude_cli"}}})
    make_task(layout, "R6")
    assert picks(layout)["R6"] == "claude_cli"


# ------------------------------------------------------------------ finding 2: unattended_prefers_api_credits

def test_unattended_with_credits_but_preference_off_never_picks_claude_api(layout):
    write_key(layout)
    write_routing(layout, {"capacity": {"unattended_prefers_api_credits": False}})
    plain_claude_task(layout, "U1")
    assert picks(layout, unattended=True)["U1"] == "claude_cli"


def test_unattended_with_no_preference_configured_defaults_to_off(layout):
    write_key(layout)                                         # no routing.yaml at all
    plain_claude_task(layout, "U2")
    assert picks(layout, unattended=True)["U2"] == "claude_cli"


def test_unattended_with_preference_on_and_credits_picks_claude_api(layout):
    write_key(layout)
    write_routing(layout, {"capacity": {"unattended_prefers_api_credits": True}})
    plain_claude_task(layout, "U3")
    assert picks(layout, unattended=True)["U3"] == "claude_api"


def test_unattended_with_preference_on_but_no_credits_stays_on_claude_cli(layout):
    write_routing(layout, {"capacity": {"unattended_prefers_api_credits": True}})
    plain_claude_task(layout, "U4")                           # no key: credits_remaining() is False
    assert picks(layout, unattended=True)["U4"] == "claude_cli"


def test_interactive_ignores_the_preference(layout):
    write_key(layout)
    write_routing(layout, {"capacity": {"unattended_prefers_api_credits": True}})
    plain_claude_task(layout, "U5")
    assert picks(layout, unattended=False)["U5"] == "claude_cli"


def test_preference_must_be_a_real_boolean_true(layout):
    """A string like "yes" or a 1 is not consent to spend: only `true` switches lanes."""
    write_key(layout)
    write_routing(layout, {"capacity": {"unattended_prefers_api_credits": "yes"}})
    plain_claude_task(layout, "U6")
    assert picks(layout, unattended=True)["U6"] == "claude_cli"


# ------------------------------------------------------------------ re-review: inactivity applies at every launch path

def test_a_record_queued_before_the_role_was_disabled_never_launches(layout):
    """The scheduler's skip is not the only guard: a PREPARED record (queued while the role was active) is held by
    the launch gate once routing says active: false, and launches again when the owner re-enables the role."""
    from supervisor import jobs as jobs_mod
    from supervisor.loop import Supervisor
    make_task(layout, "R9")
    tasks._update(layout, "R9", lambda d: d.update(job={"adapter": "python", "fn": "x"}))
    sup = Supervisor(layout, sleep_s=0)
    write_routing(layout, {"roles": {"engineer": {"active": False}}})
    rec = sup.dispatch(tasks.load(layout, "R9"), "python")               # a direct dispatch bypasses the scheduler...
    assert rec["phase"] == "PREPARED" and "inactive" in (rec.get("held") or "") and "engineer" in rec["held"]
    rec = sup.launch(jobs_mod.read(layout, rec["attempt_id"]))           # ...and the launch gate still holds it
    assert rec["phase"] == "PREPARED" and "inactive" in rec["held"]
    assert jobs_mod.read(layout, rec["attempt_id"]).get("handle") is None
    write_routing(layout, {"roles": {"engineer": {"active": True}}})
    rec = sup.launch(jobs_mod.read(layout, rec["attempt_id"]))
    assert rec["phase"] in ("SUBMITTED", "RUNNING", "DONE") and not rec.get("held")


def test_reviews_for_an_inactive_reviewer_role_are_not_dispatched(layout):
    from supervisor import review
    make_task(layout, "R10", reviewer=("code_review",))
    tasks.set_status(layout, "R10", "IN_PROGRESS")
    tasks.record_delivery(layout, "R10", "d" * 64, actor="test", paths=["R10.txt"])
    tasks.record_verification(layout, "R10", "d" * 64, True, detail="ok")
    tasks.advance(layout, "R10")                                           # -> WAITING_FOR_REVIEW
    assert [role for _t, role in review.pending(layout)] == ["code_review"]
    write_routing(layout, {"roles": {"code_review": {"adapter": "claude_cli", "active": False}}})
    assert review.pending(layout) == []
    assert tasks.load(layout, "R10").status == "WAITING_FOR_REVIEW"       # waits; nothing launched on another lane
    write_routing(layout, {"roles": {"code_review": {"adapter": "claude_cli"}}})
    assert [role for _t, role in review.pending(layout)] == ["code_review"]


def test_gate_refuses_a_review_record_whose_reviewer_role_is_inactive(layout):
    from supervisor import gate
    make_task(layout, "R11", reviewer=("code_review",))
    write_routing(layout, {"roles": {"code_review": {"active": False}}})
    task = tasks.load(layout, "R11")
    assert gate.check(layout, task, "claude_cli", "review", [], role="code_review") == gate.inactive_reason("code_review")
    assert gate.check(layout, task, "claude_cli", "review", [], role="code_review_2") is None
    assert gate.check(layout, task, "python", "verify", []) is None       # a verify job runs for no agent role
