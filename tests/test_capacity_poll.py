"""The loop's Codex capacity poll runs only when something could send work to the Codex lane (PHASE4 F2 of the
Studio migration: a rehearsal copy whose config named no Codex lane still reached the installed CLI every iteration,
because codex_cli is always registered)."""
import yaml

from supervisor import jobs, runtime, scheduler
from supervisor.adapters import codex_cli
from supervisor.loop import Supervisor
from tests.conftest import make_task


def routing(layout, doc):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def polls(layout, monkeypatch):
    calls = []
    monkeypatch.setattr(codex_cli, "poll_capacity", lambda lay, max_age_s=0: calls.append(lay))
    runtime.set_decision(layout, "PAUSE", "test")                # the poll runs under PAUSE too; nothing dispatches
    Supervisor(layout, sleep_s=0).once()
    return len(calls)


def test_no_codex_lane_anywhere_means_no_poll(layout, monkeypatch):
    routing(layout, {"roles": {"engineer": {"adapter": "claude_cli"}}})
    make_task(layout, "T")                                       # cost_category claude, role on claude_cli
    assert not scheduler.lane_in_use(layout, "codex_cli")
    assert polls(layout, monkeypatch) == 0


def test_an_active_role_on_codex_polls_and_a_dormant_one_does_not(layout, monkeypatch):
    routing(layout, {"roles": {"code_review": {"adapter": "codex_cli"}}})
    assert polls(layout, monkeypatch) == 1
    routing(layout, {"roles": {"code_review": {"adapter": "codex_cli", "active": False}}})
    assert polls(layout, monkeypatch) == 0


def test_a_codex_fallback_or_cost_lane_override_counts(layout):
    routing(layout, {"roles": {"code_review": {"adapter": "claude_cli", "fallback": {"adapter": "codex_cli"}}}})
    assert scheduler.lane_in_use(layout, "codex_cli")
    routing(layout, {"cost_lanes": {"mixed": "codex_cli"}})
    assert scheduler.lane_in_use(layout, "codex_cli")


def test_a_live_codex_task_counts_and_a_finished_one_does_not(layout):
    make_task(layout, "C")
    doc = yaml.safe_load((layout.tasks / "C.yaml").read_text(encoding="utf-8"))
    doc["cost_category"] = "codex"
    (layout.tasks / "C.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert scheduler.lane_in_use(layout, "codex_cli")
    doc["status"] = "COMPLETE"
    (layout.tasks / "C.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert not scheduler.lane_in_use(layout, "codex_cli")


def test_an_open_job_record_on_codex_counts_whatever_the_config_says(layout):
    routing(layout, {"roles": {}})
    jobs.prepare(layout, "X", "X.a1", "codex_cli", {"kind": "delivery"})
    assert scheduler.lane_in_use(layout, "codex_cli")
    jobs.set_phase(layout, "X.a1", "DONE")
    assert not scheduler.lane_in_use(layout, "codex_cli")
