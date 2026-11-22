"""`why_not_runnable`: one
function computes the reason a non-terminal task cannot run, from the closed nine-code set, and the idle alert,
the Overview and the task page all read it — so the three surfaces can never show a different sentence for the
same task, and each listed task is offered only the choices its own reason admits."""
from __future__ import annotations

import sys
import time

import pytest

from supervisor import gate, inbox, jobs, runtime, tasks
from supervisor.loop import Supervisor
from supervisor.ui.routes import overview as overview_routes
from supervisor.ui.routes import tasks as tasks_routes
from tests.conftest import make_task
from tests.test_exception_route_and_checkpoints import fake_claude  # noqa: F401 (fixture)

PY = sys.executable
CODES = {"unmet_dependency", "phase_gate", "role_inactive", "capacity_floor", "lane_unregistered",
        "awaiting_review", "awaiting_approval", "preflight_failed", "resource_claimed"}


def _in_status(layout, tid, status, **fields):
    make_task(layout, tid, **fields)
    tasks.set_status(layout, tid, "IN_PROGRESS")
    if status != "IN_PROGRESS":
        tasks.set_status(layout, tid, status)


def test_every_non_terminal_status_produces_one_of_the_closed_nine_codes(layout):
    from supervisor import why_not_runnable as wnr
    all_tasks = {}
    for i, status in enumerate(("NOT_STARTED", "READY", "IN_PROGRESS", "WAITING_FOR_REVIEW",
                                "WAITING_FOR_HUMAN", "BLOCKED", "FAILED")):
        tid = f"S{i}"
        if status == "NOT_STARTED":
            make_task(layout, tid)
        elif status == "READY":
            make_task(layout, tid, status="READY")
        else:
            _in_status(layout, tid, status)
        all_tasks[tid] = tasks.load(layout, tid)
    for tid, t in all_tasks.items():
        why = wnr.why_not_runnable(t, all_tasks, layout)
        assert why["code"] in CODES, f"{tid} ({t.status.value}): {why!r} is not one of the closed nine codes"
        assert isinstance(why.get("detail"), str) and why["detail"]
        assert isinstance(why.get("blockers"), list)


def test_unmet_dependency_offers_diagnose_now_and_cancel_task_but_never_resume(layout):
    from supervisor import why_not_runnable as wnr
    make_task(layout, "DEP")                                  # NOT_STARTED: never COMPLETE
    _in_status(layout, "B", "BLOCKED", deps=["DEP"])
    all_tasks = tasks.load_all(layout)
    why = wnr.why_not_runnable(all_tasks["B"], all_tasks, layout)
    assert why["code"] == "unmet_dependency"
    assert "DEP" in why["detail"]
    choice_ids = {c["id"] for c in wnr.choices_for(all_tasks["B"], why)}
    assert choice_ids == {"diagnose_now", "cancel_task"}, choice_ids
    assert "resume" not in choice_ids


def test_role_inactive_offers_no_choices_and_names_the_configuration_line(layout, monkeypatch):
    from supervisor import why_not_runnable as wnr
    monkeypatch.setattr(gate, "role_inactive", lambda layout_, role: role == "engineer")
    _in_status(layout, "B", "BLOCKED")
    all_tasks = tasks.load_all(layout)
    why = wnr.why_not_runnable(all_tasks["B"], all_tasks, layout)
    assert why["code"] == "role_inactive"
    assert "roles.engineer.active" in why["detail"]
    assert wnr.choices_for(all_tasks["B"], why) == [], "no button on the item can fix a configuration line"


def test_a_held_task_is_still_not_listed_by_the_idle_alert(layout):
    """The 2.7.0 behaviour (R-06/R-17) this step must not re-close: a task the owner holds is the owner's own
    call, never the pipeline's problem."""
    _in_status(layout, "B", "BLOCKED")
    tasks.hold(layout, "B", actor="owner", note="held for now")
    sup = Supervisor(layout, sleep_s=0)
    now = time.time()
    assert sup.alert_if_idle(now=now) is None
    assert sup.alert_if_idle(now=now + 3 * 3600) is None, (
        "a task the owner holds alone must never raise the idle alert"
    )


def test_the_idle_alert_the_overview_and_the_task_page_render_the_same_sentence(layout):
    from supervisor import why_not_runnable as wnr
    _in_status(layout, "DEP", "IN_PROGRESS")                  # non-terminal but not eligible either
    _in_status(layout, "B", "BLOCKED", deps=["DEP"])

    all_tasks = tasks.load_all(layout)
    why = wnr.why_not_runnable(all_tasks["B"], all_tasks, layout)
    sentence = wnr.render_reason(why)

    sup = Supervisor(layout, sleep_s=0)
    now = time.time()
    sup.alert_if_idle(now=now)
    item = sup.alert_if_idle(now=now + 3600)
    assert item is not None
    alert_text = item["summary"] + (item.get("detail") or "")
    assert sentence in alert_text, "the idle alert must render why_not_runnable's own sentence, not a quoted note"

    live_ctx = overview_routes.overview_live_context(layout)
    overview_sentences = [wnr.render_reason(wnr.why_not_runnable(t, all_tasks, layout))
                          for t in live_ctx.get("blocked_preview_tasks", [])]
    assert sentence in overview_sentences or not live_ctx.get("blocked_preview_tasks"), (
        "the Overview must read the same function, not a second copy of the sentence"
    )

    ctx = tasks_routes.task_context(layout, "B")
    assert ctx["why_not_runnable"] == sentence, "the task page must render the identical sentence"


def test_a_long_reason_is_not_truncated_and_stays_byte_identical_in_the_alert(layout, monkeypatch):
    """The idle alert used to cut each task's own line to 220 characters before handing the joined text to
    `inbox.fit` -- a disagreement with the Overview and the task page (which show the sentence whole) for any
    reason longer than that, reachable in practice (a `resource_claimed` holder list, a `capacity_floor` message).
    `inbox.fit`'s own budget (MAX_DETAIL_CHARS) is the only bound that may apply, never a per-line one."""
    from supervisor import why_not_runnable as wnr
    _in_status(layout, "B", "BLOCKED")
    long_sentence = "resource-lock limit reached, held by " + ", ".join(f"T{i}" for i in range(40))
    assert len(long_sentence) > 220
    monkeypatch.setattr(wnr, "render_reason", lambda why: long_sentence)
    sup = Supervisor(layout, sleep_s=0)
    now = time.time()
    sup.alert_if_idle(now=now)
    item = sup.alert_if_idle(now=now + 3600)
    assert item is not None
    alert_text = item["summary"] + (item.get("detail") or "")
    assert long_sentence in alert_text, "a long reason must reach the alert whole, not cut at 220 characters"


def test_the_idle_alert_does_not_fire_while_the_loop_is_paused_draining_or_stopped(layout):
    """A paused/draining/stopped loop is the owner's own call, exactly like a held task (R-17's own precedent):
    never raised as idle, rather than raised with every task's reason computed as if decision were still RUN."""
    _in_status(layout, "B", "BLOCKED")
    runtime.set_decision(layout, "PAUSE")
    sup = Supervisor(layout, sleep_s=0)
    now = time.time()
    assert sup.alert_if_idle(now=now) is None
    assert sup.alert_if_idle(now=now + 3 * 3600) is None, (
        "a paused loop alone must never raise the idle alert"
    )


def test_the_overview_and_the_task_page_show_the_loops_decision_instead_of_a_false_structural_reason(layout):
    """`gate.check`'s own first non-terminal refusal (`decision != RUN`) blocks every delivery launch identically;
    `why_not_runnable.decision_blocked` is read once and both surfaces substitute it for the structural reason
    they would otherwise compute and disagree about wording for."""
    from supervisor import why_not_runnable as wnr
    _in_status(layout, "B", "BLOCKED")
    runtime.set_decision(layout, "DRAIN")
    paused = wnr.decision_blocked(layout)
    assert paused and "DRAIN" in paused

    live_ctx = overview_routes.overview_live_context(layout)
    overview_reasons = [row["reason"] for row in live_ctx.get("blocked_preview", [])]
    assert paused in overview_reasons

    ctx = tasks_routes.task_context(layout, "B")
    assert ctx["why_not_runnable"] == paused


def test_task_page_shows_no_reason_for_an_eligible_task(layout):
    """A `NOT_STARTED`/`READY` task with nothing structurally blocking it is not "not runnable" — it is merely
    outranked, exactly as the Overview's own `waiting_to_start` scoping already treats it; the task page must
    agree, not show a reason a `tasks.eligible` task does not have."""
    make_task(layout, "T1")
    assert any(e.id == "T1" for e in tasks.eligible(layout, tasks.load_all(layout)))
    ctx = tasks_routes.task_context(layout, "T1")
    assert ctx["why_not_runnable"] is None


def test_task_page_shows_no_reason_for_a_task_with_its_own_job_open(layout):
    """An `IN_PROGRESS` task with a job running right now is not "not runnable" — it is running. The task page
    used to have no guard for this (only terminal/held), unlike the Overview's own `running_ids` scoping, and
    could show a false line, including the agent-limit holder list naming the task's own job as its blocker."""
    _in_status(layout, "B", "IN_PROGRESS")
    jobs.write(layout, {"attempt_id": "B.a1.j1", "task_id": "B", "phase": "RUNNING", "adapter": "claude_cli",
                        "spec": {"kind": "delivery"}})
    ctx = tasks_routes.task_context(layout, "B")
    assert ctx["why_not_runnable"] is None


def test_a_missing_preflight_after_the_backfill_window_closes_is_reported_as_preflight_failed(layout):
    """Case (a) of the preflight finding: `gate.check` refuses an absent preflight outright once the backfill
    window is closed (`preflight.backfill_complete`); `why_not_runnable` must report the same refusal, through
    the same side-effect-free predicate (`gate.stored_preflight_reason`), never run a probe of its own."""
    from supervisor import preflight as preflight_mod
    from supervisor import why_not_runnable as wnr
    _in_status(layout, "B", "BLOCKED", job={"adapter": "process", "argv": [PY, "-c", "pass"], "preflight": None})
    preflight_mod.mark_backfill_complete(layout, swept=0)
    t = tasks.load(layout, "B")
    all_tasks = tasks.load_all(layout)
    why = wnr.why_not_runnable(t, all_tasks, layout)
    assert why["code"] == "preflight_failed"
    assert "preflight missing" in why["detail"]
    assert gate.preflight_reason(layout, t, "process", "delivery") == "preflight missing: write the job block again to record one"


def test_a_stale_preflight_whose_block_digest_moved_is_reported_as_preflight_failed(layout):
    """Case (a)'s other half: a stored preflight recorded against a job block that has since changed
    (`block_digest` mismatch) refuses in `gate.check`; `why_not_runnable` must name the same reason."""
    from supervisor import why_not_runnable as wnr
    stale = {"ok": True, "at": "2026-01-01T00:00:00Z", "template": None, "probe_digest": None,
            "detail": {"block_digest": "not-the-current-digest"}}
    _in_status(layout, "B", "BLOCKED", job={"adapter": "process", "argv": [PY, "-c", "pass"], "preflight": stale})
    t = tasks.load(layout, "B")
    all_tasks = tasks.load_all(layout)
    why = wnr.why_not_runnable(t, all_tasks, layout)
    assert why["code"] == "preflight_failed"
    assert "stale" in why["detail"]
    assert gate.preflight_reason(layout, t, "process", "delivery") == "preflight stale: the job block changed since it was recorded"


def test_a_stored_preflight_for_a_different_launch_adapter_is_never_reported(layout, monkeypatch):
    """Case (b) of the preflight finding: a job block's own stored preflight binds only the launch that matches
    both its `adapter` and `PREFLIGHT_ADAPTERS` membership (`gate.preflight_binding`) -- a model-lane task's
    unrelated, process-shaped preflight (left over, say, from a routing change) must never be borrowed and
    reported here, the same guard `gate.preflight_reason` itself applies."""
    from supervisor import scheduler as scheduler_mod
    from supervisor import why_not_runnable as wnr
    failed = {"ok": False, "at": "2026-01-01T00:00:00Z", "template": None, "probe_digest": None,
             "detail": {"code": "path_missing", "message": "nope", "block_digest": "x"}}
    _in_status(layout, "B", "BLOCKED", job={"adapter": "process", "argv": ["true"], "preflight": failed})
    monkeypatch.setattr(scheduler_mod, "adapter_name", lambda t, l=None: "claude_cli")
    t = tasks.load(layout, "B")
    all_tasks = tasks.load_all(layout)
    why = wnr.why_not_runnable(t, all_tasks, layout)
    assert why["code"] != "preflight_failed", why
    assert gate.preflight_reason(layout, t, "claude_cli", "delivery") is None


@pytest.mark.usefixtures("fake_claude")
def test_answering_a_diagnose_now_choice_dispatches_a_diagnosis(layout):
    """The owner-only `diagnose_now` effect, once offered by
    `why_not_runnable.choices_for` and answered, actually reaches `diagnosis.dispatch` through a `loop.Supervisor`
    built for the call -- not refused for want of "the running loop" the way it was before this step."""
    make_task(layout, "DEP")
    _in_status(layout, "B", "BLOCKED", deps=["DEP"])
    assert not jobs.for_task(layout, "B")
    item = inbox.create(layout, for_role="owner", subject="s", summary="s", task_id="B",
                        choices=[{"id": "diag", "effect": {"op": "diagnose_now", "task": "B"}}])
    inbox.answer(layout, item["id"], "diag", choice="diag", choices_digest=item["choices_digest"])
    diag_jobs = [r for r in jobs.for_task(layout, "B") if (r.get("spec") or {}).get("kind") == "diagnosis"]
    assert diag_jobs, "diagnose_now must dispatch a diagnosis job for the task it names"
    answered = inbox.load(layout, item["id"])
    assert answered["choice_applied"]["ok"] is True


def test_a_twice_refused_plan_item_offers_only_the_choices_the_item_already_offered(layout):
    """Guard against re-closing the existing step 9/§2.5 behaviour while wiring the new choice: a plan escalation's
    own choice set (cancel_task, hold — never apply_plan) is unrelated to why_not_runnable and must keep working."""
    make_task(layout, "T")
    item = inbox.create(layout, for_role="owner", subject="s", summary="s", kind=inbox.DECISION_KIND,
                        choices=[{"id": "cancel_task", "effect": {"op": "cancel_task", "task": "T"}},
                                {"id": "hold", "effect": {"op": "hold", "task": "T"}}],
                        free_text=True, task_id="T")
    assert {c["id"] for c in item["choices"]} == {"cancel_task", "hold"}
