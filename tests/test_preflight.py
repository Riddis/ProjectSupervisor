"""supervisor/preflight.py and the gate's own refusal (RECOVERY_PLAN.md section 3.3, D-027 decision 9): a wrong
path, a wrong port, an unknown python `fn` and an unparsable flag are each refused at write time, with their own
code, under section 3.3's table; a literal `argv`/`fn` job declares no parameter slots at all; the probe runs
under the declared base environment a dispatched job would get; and a stale or failed preflight never launches."""
from __future__ import annotations

import sys

import yaml

from supervisor import envconfig, gate, preflight, tasks as tasks_mod
from supervisor.adapters.python import JOBS, job as register_job
from tests.conftest import make_task

PY = sys.executable


def _write_templates(layout, templates: dict):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text(yaml.safe_dump({"version": 1, "templates": templates}),
                                                       encoding="utf-8")


def _process_template(**extra):
    out = {"adapter": "process", "argv": [PY, "-c", "pass"], "may_be_set_by": ["owner"], "params": {}}
    out.update(extra)
    return out


# ----------------------------------------------------------------------- section 3.3's table, one code each

def test_wrong_path_refused(layout):
    _write_templates(layout, {"t": _process_template(
        argv=[PY, "-c", "pass", "{path}"], params={"path": {"type": "path", "roots": ["."], "must_exist": True}})})
    job = {"adapter": "process", "template": "t", "params": {"path": "nope/missing.txt"}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "path_missing"


def test_wrong_port_refused(layout):
    _write_templates(layout, {"t": _process_template(
        argv=[PY, "-c", "pass", "{port}"], params={"port": {"type": "port", "allowed": [8080]}})})
    job = {"adapter": "process", "template": "t", "params": {"port": 9999}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "host_port_not_allowed"


def test_unknown_python_fn_refused(layout):
    _write_templates(layout, {"t": {"adapter": "python", "fn": "ghost_fn_does_not_exist", "may_be_set_by": ["owner"],
                                     "params": {}}})
    job = {"adapter": "python", "template": "t", "params": {}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "fn_unregistered"


def test_unparsable_flag_refused(layout):
    _write_templates(layout, {"t": _process_template(
        argv=[PY, "-c", "pass", "{flag}"], params={"flag": {"type": "int"}})})
    job = {"adapter": "process", "template": "t", "params": {"flag": "not-an-int"}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "parameter_type_invalid"


def test_params_signature_mismatch_refused(layout):
    @register_job("preflight_test_needs_mode")
    def _needs_mode(root, run_dir, mode):
        return {"status": "COMPLETE"}
    try:
        _write_templates(layout, {"t": {"adapter": "python", "fn": "preflight_test_needs_mode",
                                         "may_be_set_by": ["owner"], "params": {}}})
        job = {"adapter": "python", "template": "t", "params": {"wrong_key": 1}}
        out = preflight.run(layout, job)
        assert out["ok"] is False
        assert out["detail"]["code"] == "params_signature_mismatch"
    finally:
        JOBS.pop("preflight_test_needs_mode", None)


def test_executable_not_resolved_refused(layout):
    _write_templates(layout, {"t": _process_template(argv=["definitely-not-a-real-binary-xyz123"])})
    job = {"adapter": "process", "template": "t", "params": {}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "executable_not_allowed"


def test_timeout_too_high_refused(layout):
    _write_templates(layout, {"t": _process_template(timeout_s=10)})
    job = {"adapter": "process", "template": "t", "params": {}, "timeout_s": 100}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "timeout_too_high"


def test_duration_below_needs_refused(layout):
    """RECOVERY_PLAN.md section 7: the one R-20 check this release owns."""
    _write_templates(layout, {"t": _process_template(needs={"duration_min": 5})})
    job = {"adapter": "process", "template": "t", "params": {}, "timeout_s": 60}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "duration_below_needs"


def test_secret_shaped_value_refused(layout):
    _write_templates(layout, {"t": _process_template(
        argv=[PY, "-c", "pass", "{key}"], params={"key": {"type": "str"}})})
    job = {"adapter": "process", "template": "t", "params": {"key": "sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa"}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "secret_shaped_value"


def test_template_unknown_refused(layout):
    job = {"adapter": "process", "template": "ghost", "params": {}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "template_unknown"


# ----------------------------------------------------------------------- the literal-argv rule (section 1.4)

def test_literal_argv_declares_no_slots_and_preflights_clean(layout):
    """A job block carrying a literal `argv` declares no parameter slots: no element of it is treated as a
    required input path, whether or not the path it names will ever exist."""
    job = {"adapter": "process", "argv": [PY, "-c", "pass", "will/never/exist.txt"]}
    out = preflight.run(layout, job)
    assert out["ok"] is True


def test_a_later_tasks_output_path_preflights_clean(layout):
    """Only a template's declared path parameters are checked for existence, and an output path an argument
    names is not required to exist."""
    _write_templates(layout, {"t": _process_template(
        argv=[PY, "-c", "pass", "{out}"], params={"out": {"type": "path", "roots": ["."], "must_exist": False}})})
    job = {"adapter": "process", "template": "t", "params": {"out": "reports/not-written-yet.md"}}
    out = preflight.run(layout, job)
    assert out["ok"] is True


# ----------------------------------------------------------------------- the probe's own environment

def test_probe_runs_under_the_jobs_own_declared_environment_digest(layout):
    _write_templates(layout, {"t": _process_template(probe=[PY, "-c", "pass"])})
    job = {"adapter": "process", "template": "t", "params": {}}
    out = preflight.run(layout, job)
    assert out["ok"] is True
    _, expected_digest = envconfig.build(layout, "process")
    assert out["probe_digest"] == expected_digest


def test_a_failing_probe_is_refused(layout):
    _write_templates(layout, {"t": _process_template(probe=[PY, "-c", "import sys; sys.exit(1)"])})
    job = {"adapter": "process", "template": "t", "params": {}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "probe_failed"


# ----------------------------------------------------------------------- never raises

def test_a_broken_template_file_fails_the_preflight_rather_than_raising(layout):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "job_templates.yaml").write_text("not: [valid", encoding="utf-8")
    job = {"adapter": "process", "template": "t", "params": {}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "preflight_error"


# ----------------------------------------------------------------------- the gate's own refusal (invariant 7)

def test_gate_refuses_a_process_job_with_no_preflight(layout):
    make_task(layout, "T1", job={"adapter": "process", "argv": [PY, "-c", "pass"], "preflight": None})
    t = tasks_mod.load(layout, "T1")
    reason = gate.check(layout, t, "process", "delivery", running=[])
    assert reason and "preflight" in reason


def test_gate_refuses_a_process_job_with_a_failed_preflight(layout):
    failed = {"ok": False, "at": "2026-01-01T00:00:00Z", "template": None, "probe_digest": None,
              "detail": {"code": "path_missing", "message": "nope", "block_digest": "x"}}
    make_task(layout, "T1", job={"adapter": "process", "argv": [PY, "-c", "pass"], "preflight": failed})
    t = tasks_mod.load(layout, "T1")
    reason = gate.check(layout, t, "process", "delivery", running=[])
    assert reason and "preflight failed" in reason


def test_gate_refuses_a_process_job_whose_block_changed_since_its_preflight(layout):
    make_task(layout, "T1", job={"adapter": "process", "argv": [PY, "-c", "pass"], "timeout_s": 60})
    tasks_mod._update(layout, "T1", lambda d: d["job"].update(timeout_s=999))   # drifts without re-preflighting
    t = tasks_mod.load(layout, "T1")
    reason = gate.check(layout, t, "process", "delivery", running=[])
    assert reason and "stale" in reason


def test_gate_allows_a_process_job_with_a_passing_preflight(layout):
    make_task(layout, "T1", job={"adapter": "process", "argv": [PY, "-c", "pass"]}, owned_paths=["."])
    t = tasks_mod.load(layout, "T1")
    assert t.job.preflight is not None and t.job.preflight.ok is True
    reason = gate.check(layout, t, "process", "delivery", running=[])
    assert reason is None


def test_gate_is_silent_for_adapters_the_rule_does_not_bind(layout):
    make_task(layout, "T1", job={"adapter": "claude_cli"}, owned_paths=["."])
    t = tasks_mod.load(layout, "T1")
    assert t.job.preflight is None                    # never attached: claude_cli is not a preflight adapter
    reason = gate.check(layout, t, "claude_cli", "delivery", running=[])
    assert reason is None
