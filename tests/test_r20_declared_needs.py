"""R-20 (D-028, docs/plan/LONG_WORK.md): declared task needs matched against the lane at import, a job-declared
time budget enforced instead of the global cap, and an Extend that takes a duration bounded by the same ceiling,
deduped against a click burst. Answers three recorded failures: an 8h agent session killed at the policy cap
with no field to say the work was long; two reshaped tasks that ran three sessions on a lane that could never
finish them; seventeen Extend clicks on one job, ten of them inside two seconds."""
from __future__ import annotations

import json
import sys
import time

import pytest
import yaml
from pydantic import ValidationError

from supervisor import (commands, gate, jobs, needs as needs_mod, plans, scheduler,
                        tasks as tasks_mod, timeouts, why_not_runnable)
from supervisor.adapters import REGISTRY
from supervisor.adapters.process import ProcessAdapter
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_plans import base_plan

PY = sys.executable


def _write_routing(layout, doc):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def _write_templates(layout, templates: dict):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text(yaml.safe_dump({"version": 1, "templates": templates}),
                                                       encoding="utf-8")


def _task_doc(tid, **extra):
    doc = {"id": tid, "phase": 0, "title": tid, "objective": "o", "owner_role": "engineer",
           "artifacts": ["x"], "cost_category": "deterministic"}
    doc.update(extra)
    return doc


def _log(layout):
    return [json.loads(line) for line in layout.log.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- the vocabulary (RF-27) and a template conflict (RF-33)

def test_rf27_unknown_needs_key_refused_at_create(layout):
    with pytest.raises(ValidationError):
        tasks_mod.create(layout, _task_doc("T", needs={"bogus_key": True}), actor="owner")


def test_rf27_duration_min_must_be_a_positive_int(layout):
    with pytest.raises(ValidationError):
        tasks_mod.create(layout, _task_doc("T", needs={"duration_min": -5}), actor="owner")


def test_rf27_network_must_be_a_bool(layout):
    with pytest.raises(ValidationError):
        tasks_mod.create(layout, _task_doc("T", needs={"network": "yes"}), actor="owner")


def test_rf33_a_task_and_its_templates_own_resource_conflict_is_refused(layout):
    _write_templates(layout, {"t": {"adapter": "process", "argv": [PY, "-c", "pass"], "may_be_set_by": ["owner"],
                                     "params": {}, "needs": {"resource": "gpu_a"}}})
    with pytest.raises(tasks_mod.TaskError, match="differ"):
        tasks_mod.create(layout, _task_doc("T", job={"adapter": "process", "template": "t", "params": {}},
                                           needs={"resource": "gpu_b"}), actor="owner")


# ---------------------------------------------------------------- RF-28: reported at import, not after three sessions

def test_rf28_a_duration_no_lane_can_honour_is_refused_at_the_write(layout):
    _write_routing(layout, {"timeouts": {"max_hard_cap_s": 3600}})
    with pytest.raises(tasks_mod.TaskError, match="needs.duration_min"):
        tasks_mod.create(layout, _task_doc("T", job={"adapter": "process", "argv": [PY, "-c", "pass"]},
                                           needs={"duration_min": 120}), actor="owner")  # 120 min = 7200s > 3600s


def test_rf28_matches_needs_match_directly(layout):
    _write_routing(layout, {"timeouts": {"max_hard_cap_s": 60}})
    results = needs_mod.match(layout, None, "process", {"duration_min": 2})
    assert [r["code"] for r in results if r["tier"] == "refusal"] == ["needs_exceed_lane"]


# ---------------------------------------------------------------- RF-29: a declaration something contradicts

def test_rf29_resumable_against_a_model_lane_that_declares_durable_false(layout):
    results = needs_mod.match(layout, make_task_like(layout), "claude_cli", {"resumable": True})
    assert any(r["code"] == "needs_unsupported" and r["tier"] == "refusal" for r in results)


def make_task_like(layout):
    """A bare Task with no job block -- needs.match only reads task.job.template, so None also suffices, but
    exercising the real object keeps this test honest about the type the function actually takes."""
    make_task(layout, "RF29", status="BLOCKED")
    return tasks_mod.load(layout, "RF29")


def test_rf29_resumable_against_a_process_template_that_does_not_declare_it(layout):
    _write_templates(layout, {"t": {"adapter": "process", "argv": [PY, "-c", "pass"], "may_be_set_by": ["owner"],
                                     "params": {}}})   # no resumable: true
    with pytest.raises(tasks_mod.TaskError, match="resumable"):
        tasks_mod.create(layout, _task_doc("T", job={"adapter": "process", "template": "t", "params": {}},
                                           needs={"resumable": True}), actor="owner")


def test_rf29_network_against_claude_api(layout):
    results = needs_mod.match(layout, None, "claude_api", {"network": True})
    assert any(r["code"] == "needs_unsupported" and r["tier"] == "refusal" and r["need"] == "network"
              for r in results)


# ---------------------------------------------------------------- the three warnings (section 2.3), never a refusal

def test_needs_lane_unknown_is_a_warning_not_a_refusal(layout):
    from supervisor import testing
    with testing.lane(testing.FakeLane("mystery_lane")):
        out = tasks_mod.create(layout, _task_doc("T", job={"adapter": "mystery_lane"},
                                                 needs={"duration_min": 10}), actor="owner")
    assert out.get("warnings") and "mystery_lane" in out["warnings"][0]


_WIDGET_CAPABILITIES_PLUGIN = '''
def setup(registry):
    registry.capabilities("widget_lane", {"durable": False, "network": True, "max_duration_min": 10})
'''


def test_a_plugin_declared_lane_is_matched_not_warned_unknown(tmp_path):
    """section 3.2/9 step 2: a lane with its own `registry.capabilities` declaration is matched against it, not
    left as the `needs_lane_unknown` warning a capability-less lane draws."""
    from supervisor import capabilities as capabilities_mod, plugins, testing
    lay = plugins.load(testing.project(tmp_path, modules={"widget": _WIDGET_CAPABILITIES_PLUGIN})).layout
    assert capabilities_mod.for_lane(lay, "widget_lane") == {"durable": False, "network": True,
                                                              "max_duration_min": 10}
    with testing.lane(testing.FakeLane("widget_lane")):
        out = tasks_mod.create(lay, _task_doc("T", job={"adapter": "widget_lane"},
                                              needs={"duration_min": 5}), actor="owner")
        assert out.get("warnings", []) == []     # matched against the declared ceiling, not warned unknown
        with pytest.raises(tasks_mod.TaskError, match="needs.duration_min"):
            tasks_mod.create(lay, _task_doc("T2", job={"adapter": "widget_lane"},
                                           needs={"duration_min": 20}), actor="owner")   # 20 min > the declared 10


def test_needs_unverifiable_resumable_with_no_template_at_all(layout):
    out = tasks_mod.create(layout, _task_doc("T", job={"adapter": "process", "argv": [PY, "-c", "pass"]},
                                             needs={"resumable": True}), actor="owner")
    assert out.get("warnings") and any("resumable" in w for w in out["warnings"])


def test_needs_cleared_is_a_warning_when_a_patch_explicitly_empties_it(layout):
    make_task(layout, "T", cost_category="deterministic", needs={"duration_min": 30})
    out = tasks_mod.patch_task(layout, "T", {"needs": {}}, actor="owner")
    assert any("needs_cleared" in w for w in out.get("warnings", []))


# ---------------------------------------------------------------- a task carrying no declaration: unchanged

def test_needs_is_sticky_across_a_refresh_that_omits_it(layout, tmp_path):
    make_task(layout, "T", cost_category="deterministic", needs={"duration_min": 30})
    seed = {"tasks": [_task_doc("T", title="T2")]}  # no "needs" key at all
    seed_path = tmp_path / "seed.yaml"
    seed_path.write_text(yaml.safe_dump(seed), encoding="utf-8")
    tasks_mod.import_seed(layout, seed_path, update_ids=["T"], actor="owner")
    assert tasks_mod.load(layout, "T").needs.duration_min == 30


def test_a_task_with_no_needs_block_behaves_exactly_as_before(layout):
    make_task(layout, "P", cost_category="deterministic", job={"adapter": "process", "argv": [PY, "-c", "pass"]})
    task = tasks_mod.load(layout, "P")
    assert task.needs is None
    spec = REGISTRY["process"].prepare(layout, task, "P.a1")
    assert spec["timeout_s"] == timeouts.PROCESS_CAP_S and spec["idle_s"] is None and spec["resumable"] is False


def test_a_declaration_describes_the_delivery_alone(layout):
    make_task(layout, "T", needs={"duration_min": 30})
    task = tasks_mod.load(layout, "T")
    assert needs_mod.needs_for(layout, task, "delivery") == {"duration_min": 30}
    assert needs_mod.needs_for(layout, task, "review") is None
    assert needs_mod.needs_for(layout, task, "verify") is None


# ---------------------------------------------------------------- the budget: its own duration, not the global cap

def test_a_process_jobs_budget_is_its_own_declared_duration_not_the_global_cap(layout):
    make_task(layout, "P", cost_category="deterministic", job={"adapter": "process", "argv": [PY, "-c", "pass"]},
              needs={"duration_min": 10})
    spec = REGISTRY["process"].prepare(layout, tasks_mod.load(layout, "P"), "P.a1")
    assert spec["timeout_s"] == 750                           # 10 * 60 * 1.25 -- neither 3600 nor 21600
    assert spec["timeout_s"] != timeouts.PROCESS_CAP_S
    assert spec["timeout_s"] != timeouts.DEFAULTS["hard_cap_s"]


def test_an_explicit_timeout_s_above_the_ceiling_is_clamped_and_logged(layout):
    _write_routing(layout, {"timeouts": {"max_hard_cap_s": 120}})
    make_task(layout, "P", cost_category="deterministic",
              job={"adapter": "process", "argv": [PY, "-c", "pass"], "timeout_s": 3600})
    spec = REGISTRY["process"].prepare(layout, tasks_mod.load(layout, "P"), "P.a1")
    assert spec["timeout_s"] == 120
    log = _log(layout)
    assert any(e["event"] == "timeout_clamped" and e["declared_s"] == 3600 and e["ceiling_s"] == 120 for e in log)


# ---------------------------------------------------------------- needs_mismatch: a lane change after the write

def test_needs_mismatch_stalls_as_its_own_reason_not_a_failed_attempt(layout):
    make_task(layout, "T", cost_category="deterministic", status="READY",
              job={"adapter": "process", "argv": [PY, "-c", "pass"]}, needs={"duration_min": 30})
    task = tasks_mod.load(layout, "T")
    assert gate.needs_mismatch_reason(layout, task, "process", "delivery") is None     # fine today

    _write_routing(layout, {"timeouts": {"max_hard_cap_s": 60}})                       # routing moves after the write
    task = tasks_mod.load(layout, "T")
    reason = gate.needs_mismatch_reason(layout, task, "process", "delivery")
    assert reason is not None and reason.startswith("needs mismatch on process: needs_exceed_lane")

    why = why_not_runnable.why_not_runnable(task, {"T": task}, layout)
    assert why["code"] == "needs_mismatch"
    choices = why_not_runnable.choices_for(task, why)
    assert {c["id"] for c in choices} == {"cancel_task", "hold"}                       # never resume

    chosen = scheduler.plan(layout, {"T": task})
    assert chosen == []
    assert any(e["event"] == "schedule_skip" and "needs mismatch" in e.get("reason", "") for e in _log(layout))


# ---------------------------------------------------------------- Extend takes a duration, and a window dedupes it

def _running_job(layout, deadline, adapter="claude_cli"):
    from supervisor._vendor.contract import atomic_json
    run_dir = layout.runs / "T" / "T.a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    make_task(layout, "T")
    handle = {"kind": "process", "pid": 999999, "create_time": 0, "started": "2026-10-24T08:00:00+00:00",
              "deadline": deadline}
    jobs.prepare(layout, "T", "T.a1", adapter, {"run_dir": str(run_dir), "idle_s": 1200, "timeout_s": 21600})
    jobs.set_phase(layout, "T.a1", "RUNNING", handle=handle)
    atomic_json(run_dir / "status.json", {"status": "RUNNING", **handle})
    return run_dir


def test_extend_takes_a_duration(layout):
    deadline = time.time() + 100
    _running_job(layout, deadline)
    out = commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1", "_id": "c1", "seconds": 1800}, "owner-ui")
    assert out["extended_s"] == 1800
    assert jobs.read(layout, "T.a1")["handle"]["deadline"] == pytest.approx(deadline + 1800)


def test_a_click_burst_inside_the_dedupe_window_adds_one_unit_not_several(layout):
    deadline = time.time() + 100
    _running_job(layout, deadline)
    out1 = commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1", "_id": "c1", "seconds": 1800}, "owner-ui")
    for i in range(9):                                        # ten clicks in total, each its own command id
        out_n = commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1", "_id": f"c{i + 2}", "seconds": 1800},
                                                   "owner-ui")
        assert out_n.get("skipped") == "already extended" and out_n["deadline"] == out1["deadline"]
    handle = jobs.read(layout, "T.a1")["handle"]
    assert handle["deadline"] == pytest.approx(deadline + 1800)      # exactly one unit landed
    assert len(handle["extensions"]) == 1
    assert any(e["event"] == "job_extend_deduped" for e in _log(layout))


def test_extend_refuses_past_the_lane_ceiling_rf32(layout):
    deadline = time.time() + 100
    _running_job(layout, deadline)
    _write_routing(layout, {"timeouts": {"max_hard_cap_s": 200}})
    with pytest.raises(ValueError, match="RF-32"):
        commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1", "_id": "c1", "seconds": 3600}, "owner-ui")
    assert jobs.read(layout, "T.a1")["handle"]["deadline"] == pytest.approx(deadline)   # refused, not clamped


def test_extend_offers_default_to_a_single_hour(layout):
    assert timeouts.extend_offers(gate.routing_doc(layout)) == [timeouts.EXTEND_STEP_S]
    _write_routing(layout, {"timeouts": {"extend_offers": [1800, 14400]}})
    assert timeouts.extend_offers(gate.routing_doc(layout)) == [1800, 14400]


# ---------------------------------------------------------------- the Control Center: one control per offer (section 4.3)

def test_agent_page_renders_one_control_per_declared_offer(layout):
    """A project configuring nothing sees exactly one control, today's +1h; one configuring `extend_offers`
    sees one control per declared duration."""
    from supervisor import testing
    _running_job(layout, time.time() + 21600)
    client = testing.ui_client(layout)
    page = client.get("/agents/T.a1").text
    assert page.count('action="/agents/T.a1/extend"') == 1
    assert "Extend +1h" in page

    _write_routing(layout, {"timeouts": {"extend_offers": [1800, 14400]}})
    page = client.get("/agents/T.a1").text
    assert page.count('action="/agents/T.a1/extend"') == 2
    assert "Extend +30m" in page and "Extend +4h" in page


def test_extend_from_the_control_center_states_the_resulting_deadline(layout):
    """Every answer states the resulting deadline (section 4.3), including the dedupe window's no-op."""
    import urllib.parse

    from supervisor import testing
    deadline = time.time() + 100
    _running_job(layout, deadline)
    client = testing.ui_client(layout)

    def flash_of(response) -> str:
        assert response.status_code == 303
        return urllib.parse.unquote(response.headers["location"].split("flash=", 1)[1])

    first = client.post("/agents/T.a1/extend", data={"csrf_token": testing.CSRF, "seconds": 1800},
                        follow_redirects=False)
    assert "applied" in flash_of(first) and "hard cap" in flash_of(first)

    again = client.post("/agents/T.a1/extend", data={"csrf_token": testing.CSRF, "seconds": 1800},
                        follow_redirects=False)
    assert "hard cap" in flash_of(again)                     # the dedupe window's no-op states it too


# ---------------------------------------------------------------- a resumable process job's cap-stop is a continuation

_SLEEP_ARGV = [PY, "-c", "import time; time.sleep(30)"]


def test_a_resumable_process_cap_stop_is_recorded_as_a_continuation_not_a_failure(layout):
    _write_templates(layout, {"t": {"adapter": "process", "argv": _SLEEP_ARGV, "may_be_set_by": ["owner"],
                                     "params": {}, "resumable": True}})
    # `argv` is carried alongside `template` here only so this fixture's own job actually launches the template's
    # command (ProcessAdapter.prepare reads job.argv, not the template's -- the template's own argv is what the
    # preflight checks, section 1.4); the `template` reference is what satisfies `needs.resumable` (section 3.2).
    make_task(layout, "T", cost_category="deterministic",
              job={"adapter": "process", "template": "t", "params": {}, "argv": _SLEEP_ARGV},
              needs={"resumable": True})
    task = tasks_mod.load(layout, "T")
    ad = REGISTRY["process"]
    spec = ad.prepare(layout, task, "T.a1")
    assert spec["resumable"] is True
    handle = ad.submit(spec)
    handle["deadline"] = time.time() - 1
    try:
        res = ad.collect(spec, handle)
        assert res.status == "FAILED" and res.raw == {"timeout": "cap", "resumable": True}
    finally:
        ad.terminate(handle)


class _FastCapProcess(ProcessAdapter):
    name = "process"

    def prepare(self, layout, task, attempt_id):
        spec = super().prepare(layout, task, attempt_id)
        spec["timeout_s"] = 1
        return spec


def test_the_loop_continues_a_resumable_process_job_past_its_cap_instead_of_failing_the_attempt(layout, monkeypatch):
    _write_templates(layout, {"t": {"adapter": "process", "argv": _SLEEP_ARGV, "may_be_set_by": ["owner"],
                                     "params": {}, "resumable": True}})
    monkeypatch.setitem(REGISTRY, "process", _FastCapProcess())
    make_task(layout, "T", cost_category="deterministic", status="READY",
              job={"adapter": "process", "template": "t", "params": {}, "argv": _SLEEP_ARGV},
              needs={"resumable": True})
    from supervisor.loop import Supervisor
    sup = Supervisor(layout, sleep_s=0)
    end = time.time() + 20
    while time.time() < end and not any(r.get("continued") for r in jobs.for_task(layout, "T")):
        sup.once()
        time.sleep(0.2)
    recs = jobs.for_task(layout, "T")
    first = next(r for r in recs if r.get("seq") == 1)
    assert first["phase"] == "DONE" and first.get("continued") and "hard cap" in (first.get("error") or "")
    assert tasks_mod.load(layout, "T").status == TaskStatus.IN_PROGRESS      # not FAILED, no revision spent
    for r in jobs.open_records(layout):
        if r.get("handle"):
            REGISTRY["process"].terminate(r["handle"])


# ---------------------------------------------------------------- the unattended credit preference is skipped, not refused

def test_claude_api_unattended_eligible_skips_a_declaration_it_cannot_meet(layout):
    from supervisor.adapters import claude_api
    _write_routing(layout, {"claude_api": {"eligible_roles": ["engineer"], "timeout_s": 600}})
    make_task(layout, "Long", owner_role="engineer", artifacts=[], tests=[], checks=[],
              needs={"duration_min": 100})
    assert claude_api.unattended_eligible(layout, tasks_mod.load(layout, "Long"), {}) is False

    make_task(layout, "Short", owner_role="engineer", artifacts=[], tests=[], checks=[])
    assert claude_api.unattended_eligible(layout, tasks_mod.load(layout, "Short"), {}) is True


# ---------------------------------------------------------------- a plan's own declared duration and writer rule

def test_plan_authority_raises_a_long_declared_duration_to_the_owner_class(layout):
    op = {"op": "create_task", "task": {"id": "T2", "title": "T2", "objective": "o", "owner_role": "engineer",
                                        "cost_category": "claude", "owned_paths": ["x"],
                                        "needs": {"duration_min": 100000}}}
    doc = {"operations": [op]}
    assert plans.authority(doc, {}, layout=layout) == plans.CLASS_OWNER


def test_plan_authority_a_modest_declared_duration_stays_class_b(layout):
    op = {"op": "create_task", "task": {"id": "T2", "title": "T2", "objective": "o", "owner_role": "engineer",
                                        "cost_category": "claude", "owned_paths": ["x"],
                                        "needs": {"duration_min": 60}}}
    doc = {"operations": [op]}
    assert plans.authority(doc, {}, layout=layout) == plans.CLASS_B


def test_rf31_a_plan_may_not_write_needs_onto_a_task_it_did_not_create(layout):
    make_task(layout, "T1", status="BLOCKED")
    doc = base_plan(ops=[{"op": "patch_contract", "task": "T1", "fields": {"needs": {"duration_min": 5}}}])
    _, refusals = plans.validate(doc, layout)
    assert refusals
    assert any("needs" in (r.get("field") or "") for r in refusals)


# ---------------------------------------------------------------- the budget reaches every lane that resolves one
#
# The loss this release exists to close was recorded on an agent lane: a session with no job block of its own,
# killed at the policy cap after eight hours of work. Section 9 step 4 and section 10 invariant 4 therefore name
# three lanes, not one -- `process`, `claude_cli` and `codex_cli`, the three that call the resolver after this
# release -- and D-028 decision 11 says every `prepare` passes `needs.needs_for(layout, task, kind)`.

@pytest.mark.parametrize("lane", ["claude_cli", "codex_cli"])
def test_a_model_lane_delivery_is_budgeted_by_its_own_declared_duration(layout, lane):
    make_task(layout, "Long", job={"adapter": lane}, needs={"duration_min": 480})
    spec = REGISTRY[lane].prepare(layout, tasks_mod.load(layout, "Long"), "Long.a1")
    assert spec["timeout_s"] == 36000                            # 480 * 60 * 1.25
    assert spec["timeout_s"] != timeouts.DEFAULTS["hard_cap_s"]  # not the policy cap that killed the recorded run


@pytest.mark.parametrize("lane, extra", [("process", {"argv": [PY, "-c", "pass"]}),
                                          ("claude_cli", {}), ("codex_cli", {})])
def test_every_prepare_that_resolves_limits_scopes_the_declaration_to_the_delivery(layout, monkeypatch, lane, extra):
    """What each `prepare` passes the resolver, asserted on the `needs=` itself and not on the resolved number
    alone (section 9 step 4): a verify view's explicit `timeout_s` would mask a wrong kind."""
    from supervisor import capabilities as capabilities_mod
    calls = []
    real = timeouts.resolve

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(timeouts, "resolve", spy)
    for kind, expected in (("delivery", {"duration_min": 600}), ("review", None), ("verify", None),
                           ("conversation", None)):
        tid = f"{lane}_{kind}"
        make_task(layout, tid, job={"adapter": lane, "result_kind": kind, **extra}, needs={"duration_min": 600})
        REGISTRY[lane].prepare(layout, tasks_mod.load(layout, tid), f"{tid}.a1")
        assert calls, f"{lane}.prepare resolved no limits at all"
        assert calls[-1].get("needs") == expected, f"{lane}/{kind} passed {calls[-1].get('needs')!r}"
        assert calls[-1].get("capabilities") == capabilities_mod.for_lane(layout, lane)


def test_a_model_lane_timeout_s_above_the_ceiling_is_clamped_with_a_receipt(layout):
    """D-028 decision 4: RF-08's cap row is the preflight's `process` row alone, so the resolver's last clamp is
    the only enforcement on a model lane -- and it may not lower a declared number silently."""
    _write_routing(layout, {"timeouts": {"max_hard_cap_s": 120}})
    make_task(layout, "M", job={"adapter": "claude_cli", "timeout_s": 3600})
    spec = REGISTRY["claude_cli"].prepare(layout, tasks_mod.load(layout, "M"), "M.a1")
    assert spec["timeout_s"] == 120
    events = _log(layout) if layout.log.exists() else []
    assert any(e["event"] == "timeout_clamped" and e["declared_s"] == 3600 and e["ceiling_s"] == 120
               for e in events), events


# ---------------------------------------------------------------- one composed ceiling, read the same way by all four

def test_rf28_refuses_against_the_ceiling_the_resolver_will_clamp_to(layout):
    """`ceiling_s` is one function, but it is one *number* only if every reader composes it from the same
    arguments (D-028 decision 4, section 3.2's signature). A per-cost-category `max_hard_cap_s` that the clamp
    reads and the write-time refusal does not is the recorded cap-kill re-entering through configuration: the
    declaration is admitted at the write and cut at launch, with declared work left."""
    _write_routing(layout, {"timeouts": {"max_hard_cap_s": 86400, "by_cost": {"claude": {"max_hard_cap_s": 1800}}},
                            "roles": {"engineer": {"adapter": "claude_cli", "model": "sonnet"}}})
    with pytest.raises(tasks_mod.TaskError, match="needs.duration_min"):
        tasks_mod.create(layout, _task_doc("Over", cost_category="claude", owned_paths=["work"],
                                           job={"adapter": "claude_cli"},
                                           needs={"duration_min": 31}), actor="owner")      # 1860 s > 1800 s
    tasks_mod.create(layout, _task_doc("Exact", cost_category="claude", owned_paths=["work"],
                                       job={"adapter": "claude_cli"},
                                       needs={"duration_min": 30}), actor="owner")          # exactly 1800 s
    spec = REGISTRY["claude_cli"].prepare(layout, tasks_mod.load(layout, "Exact"), "Exact.a1")
    assert spec["timeout_s"] == 1800                      # the same number the write was judged against


def test_rf08_refuses_a_process_timeout_s_above_the_composed_ceiling_and_admits_one_exactly_at_it(layout):
    """Section 7.1's own named test for the one line this release lands inside the other design's check: RF-08's
    `process`-row cap comparison reads `timeouts.ceiling_s`, composed for the lane and the role the job would run
    on. The check, its code, its message and its home stay where RECOVERY_PLAN.md section 3.3 put them."""
    _write_routing(layout, {"timeouts": {"max_hard_cap_s": 86400,
                                          "by_cost": {"deterministic": {"max_hard_cap_s": 1800}}}})
    tasks_mod.create(layout, _task_doc("Over", job={"adapter": "process", "argv": [PY, "-c", "pass"],
                                                     "timeout_s": 1801}), actor="owner")
    pf = tasks_mod.load(layout, "Over").job.preflight
    assert pf.ok is False and pf.detail["code"] == "timeout_too_high"
    tasks_mod.create(layout, _task_doc("Exact", job={"adapter": "process", "argv": [PY, "-c", "pass"],
                                                      "timeout_s": 1800}), actor="owner")
    assert tasks_mod.load(layout, "Exact").job.preflight.ok is True


def test_rf32_refuses_against_the_ceiling_the_role_resolves(layout):
    """The fourth reader (`agent.extend`) composes the ceiling the same way: a role profile's own
    `max_hard_cap_s` bounds an extension exactly as it bounds the budget."""
    deadline = time.time() + 100
    _running_job(layout, deadline)
    _write_routing(layout, {"roles": {"engineer": {"adapter": "claude_cli", "timeouts": {"max_hard_cap_s": 200}}}})
    with pytest.raises(ValueError, match="RF-32"):
        commands.HANDLERS["agent.extend"](layout, {"attempt_id": "T.a1", "_id": "c1", "seconds": 3600}, "owner-ui")
    assert jobs.read(layout, "T.a1")["handle"]["deadline"] == pytest.approx(deadline)   # refused, not clamped


# ---------------------------------------------------------------- the match runs in the preflight too (section 3.3)

_LONG_TEMPLATE = {"adapter": "process", "argv": [PY, "-c", "pass"], "params": {},
                  "needs": {"duration_min": 120}}


def test_the_owners_set_job_stores_a_needs_refusal_where_the_gate_reads_it(layout):
    """Section 3.3's second row: the owner's `set_job` on a task that already exists has no row to refuse, so the
    refusal is stored in the same `job.preflight` record and the task stalls as `preflight_failed` -- not held
    later by the gate under a different code, with different admitted choices."""
    _write_routing(layout, {"timeouts": {"max_hard_cap_s": 3600}})
    _write_templates(layout, {"long": {**_LONG_TEMPLATE, "may_be_set_by": ["owner"]}})
    make_task(layout, "T", cost_category="deterministic", status="READY")
    t, _ = commands.apply_job(layout, "T", {"adapter": "process", "template": "long", "params": {}}, "owner")
    assert t.job.preflight.ok is False
    assert t.job.preflight.detail["code"] == "needs_exceed_lane"
    reason = gate.preflight_reason(layout, tasks_mod.load(layout, "T"), "process", "delivery")
    assert reason is not None and "needs_exceed_lane" in reason


def test_a_plans_set_job_is_refused_with_its_plan_and_stores_nothing_on_the_task(layout):
    """Section 3.3's third row: a plan's `set_job` must pass its own preflight (RF-08), so a needs refusal
    raised inside it refuses the operation with the plan it rode in on, and no live task is left holding a
    refusal it did not have before."""
    _write_templates(layout, {"long": {**_LONG_TEMPLATE, "may_be_set_by": ["plan"]}})
    make_task(layout, "T1", cost_category="deterministic", status="BLOCKED",
              job={"adapter": "process", "template": "long", "params": {}})
    _write_routing(layout, {"timeouts": {"max_hard_cap_s": 3600}})     # the ceiling moves under the declaration

    on_a_live_task = base_plan(ops=[{"op": "set_job", "task": "T1", "template": "long", "params": {}}])
    _, refusals = plans.validate(on_a_live_task, layout)
    assert any(r["code"] == "RF-08" and "needs_exceed_lane" in json.dumps(r) for r in refusals), refusals
    stored = tasks_mod.load(layout, "T1")
    assert stored.job.preflight.ok is True                 # untouched by the refused plan, as it was before it

    on_a_task_the_plan_creates = base_plan(ops=[
        {"op": "create_task", "task": {"id": "T2", "title": "t", "objective": "o", "owner_role": "engineer",
                                        "cost_category": "deterministic"}},
        {"op": "set_job", "task": "T2", "template": "long", "params": {}}])
    _, refusals = plans.validate(on_a_task_the_plan_creates, layout)
    assert any(r["code"] == "RF-08" and "needs_exceed_lane" in json.dumps(r) for r in refusals), refusals


# ---------------------------------------------------------------- the five framework lanes' stated capability values

_CAPABILITIES_AS_DESIGNED = {
    "claude_cli": {"durable": False, "network": True},
    "codex_cli": {"durable": False, "network": True},
    "claude_api": {"durable": False, "network": False, "max_duration_min": 10},
    "process": {"network": True},
    "python": {"network": True},
}


def test_the_five_framework_lanes_answer_exactly_the_declared_table(layout):
    """Section 9 step 2: asserted over the table, so a changed value is a changed test."""
    from supervisor import capabilities as capabilities_mod
    _write_routing(layout, {"claude_api": {"timeout_s": 600}})
    assert set(capabilities_mod.FRAMEWORK_LANES) == set(_CAPABILITIES_AS_DESIGNED)
    for lane, declared in _CAPABILITIES_AS_DESIGNED.items():
        assert capabilities_mod.for_lane(layout, lane) == declared, lane


def test_claude_apis_one_configured_entry_follows_its_own_timeout(layout):
    """The one entry in the table that is not a constant (section 3.2, section 4.2): this lane's whole budget is
    one HTTP call's socket timeout, so a matcher that could not see it would refuse nothing on it."""
    from supervisor import capabilities as capabilities_mod
    from supervisor.adapters import claude_api
    _write_routing(layout, {"claude_api": {"timeout_s": 1800}})
    assert capabilities_mod.for_lane(layout, "claude_api")["max_duration_min"] == 30
    _write_routing(layout, {})
    assert (capabilities_mod.for_lane(layout, "claude_api")["max_duration_min"]
            == claude_api.DEFAULT_TIMEOUT_S // 60)


# ---------------------------------------------------------------- what the gate does and does not re-run

def test_the_gate_does_not_re_run_the_preflights_probe(layout, monkeypatch):
    """Section 3.4: the gate re-runs the match and only the match. The match is two dict lookups; the probe is a
    subprocess, and running it at dispatch is what RECOVERY_PLAN.md section 3.3 refused on cost and lateness."""
    from supervisor import preflight as preflight_mod
    _write_templates(layout, {"t": {"adapter": "process", "argv": [PY, "-c", "pass"], "may_be_set_by": ["owner"],
                                     "params": {}, "probe": [PY, "-c", "pass"]}})
    make_task(layout, "T", cost_category="deterministic", status="READY",
              job={"adapter": "process", "template": "t", "params": {}, "argv": [PY, "-c", "pass"]})
    task = tasks_mod.load(layout, "T")
    assert task.job.preflight.ok is True                      # recorded once, at the write
    probes = []
    monkeypatch.setattr(preflight_mod, "_run_probe", lambda *a, **k: probes.append(a))
    assert gate.preflight_reason(layout, task, "process", "delivery") is None
    assert gate.needs_mismatch_reason(layout, task, "process", "delivery") is None
    assert probes == []


def test_a_declaration_never_matches_or_budgets_the_jobs_it_does_not_describe(layout):
    """Invariant 11, on the pairing that would otherwise stall forever: a resumable `process` delivery whose
    review runs on a model lane (every model lane declares `durable: false`). The review is matched against
    nothing, so its gate check refuses nothing and the task carries no `needs_mismatch`."""
    from supervisor import packets
    _write_templates(layout, {"t": {"adapter": "process", "argv": [PY, "-c", "pass"], "may_be_set_by": ["owner"],
                                     "params": {}, "resumable": True}})
    make_task(layout, "T", cost_category="deterministic", status="READY",
              job={"adapter": "process", "template": "t", "params": {}, "argv": [PY, "-c", "pass"]},
              needs={"resumable": True, "duration_min": 600})
    task = tasks_mod.load(layout, "T")
    assert gate.needs_mismatch_reason(layout, task, "process", "delivery") is None
    for kind in ("review", "verify", "conversation"):
        assert needs_mod.needs_for(layout, task, kind) is None
        assert gate.needs_mismatch_reason(layout, task, "claude_cli", kind) is None
    assert gate.needs_mismatch_reason(layout, None, "claude_cli", "conversation") is None   # a chat turn: no task
    assert "verify" not in packets.NON_DELIVERY_KINDS         # it governs prompt guidance, never a budget
