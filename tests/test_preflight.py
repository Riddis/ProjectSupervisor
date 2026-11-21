"""supervisor/preflight.py and the gate's own refusal (RECOVERY_PLAN.md section 3.3, D-027 decision 9): a wrong
path, a wrong port, an unknown python `fn` and an unparsable flag are each refused at write time, with their own
code, under section 3.3's table; a literal `argv`/`fn` job declares no parameter slots at all; the probe runs
under the declared base environment a dispatched job would get; and a stale or failed preflight never launches."""
from __future__ import annotations

import sys

import yaml

from supervisor import envconfig, gate, preflight, store, tasks as tasks_mod, timeouts
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
    """The registered callable's own signature, not the template's slots: the template below declares
    `wrong_key` (so the declared-slot checks pass it) and the function does not take it."""
    @register_job("preflight_test_needs_mode")
    def _needs_mode(root, run_dir, mode):
        return {"status": "COMPLETE"}
    try:
        _write_templates(layout, {"t": {"adapter": "python", "fn": "preflight_test_needs_mode",
                                         "may_be_set_by": ["owner"],
                                         "params": {"wrong_key": {"type": "int"}}}})
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
    names is not required to exist -- its own directory is (section 3.3's table: "every path parameter exists,
    or its parent does, for an output"), which is what tells a file nobody has written yet from a typo."""
    (layout.root / "reports").mkdir(parents=True, exist_ok=True)
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
    """Outside the backfill's own run-on-absent window (RECOVERY_PLAN.md section 3.3, step 5,
    tests/test_preflight_backfill.py): once the sweep's completion mark is written, an absent preflight is
    refused outright, with no second chance to pass it at the gate."""
    make_task(layout, "T1", job={"adapter": "process", "argv": [PY, "-c", "pass"], "preflight": None})
    preflight.mark_backfill_complete(layout, swept=0)
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
    """Every path this release gives a job block -- `tasks._write_job`'s callers and `tasks._update`'s own
    generic mutation -- re-preflights a block it actually changes; staleness is what is left for a write that
    bypasses both (a store write below either, as only a direct YAML edit or an older release's row could)."""
    make_task(layout, "T1", job={"adapter": "process", "argv": [PY, "-c", "pass"], "timeout_s": 60})
    path = tasks_mod.path_for(layout, "T1")
    store.update(path, lambda d: d["job"].update(timeout_s=999), lock_path=layout.state / ".tasks.lock")
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


# ----------------------------------------------------------------------- the rule binds the launched block, not the record

def test_gate_does_not_demand_a_preflight_for_a_non_delivery_job_on_a_preflight_adapter(layout):
    """A verify (or review, or diagnosis) job never runs the task's own stored `job` block -- each builds its own
    spec and launches on whatever adapter that needs, `process` among them. A task whose own delivery is a model
    lane's never gets a preflight attached (that adapter is not bound by the rule at all); the record's own
    adapter for THIS launch being `process` must not borrow that task's unrelated, preflight-less job block and
    refuse it as 'preflight missing'."""
    make_task(layout, "T1", job={"adapter": "claude_cli", "model": "fable"})
    t = tasks_mod.load(layout, "T1")
    assert t.job.preflight is None
    for kind in ("verify", "review", "diagnosis"):
        assert gate.check(layout, t, "process", kind, running=[]) is None


def test_gate_still_refuses_a_delivery_whose_own_block_has_no_preflight(layout):
    """The rule above narrows the check, it does not remove it: a `delivery` launch on `process`/`python` is
    still refused without one, once the backfill's own run-on-absent window is closed (step 5,
    tests/test_preflight_backfill.py)."""
    make_task(layout, "T1", job={"adapter": "process", "argv": [PY, "-c", "pass"], "preflight": None})
    preflight.mark_backfill_complete(layout, swept=0)
    t = tasks_mod.load(layout, "T1")
    reason = gate.check(layout, t, "process", "delivery", running=[])
    assert reason and "preflight" in reason


# ----------------------------------------------------------------------- write-time and launch-time agree on one shape

def test_gate_does_not_see_an_explicit_null_as_a_change(layout):
    """`timeout_s: null`, written explicitly, is the same block as one that omits the key: the digest stored at
    write time and the one recomputed at launch must agree, not disagree because one side keeps a declared key's
    explicit null and the other (`exclude_none`) drops it."""
    make_task(layout, "T1", job={"adapter": "process", "argv": [PY, "-c", "pass"], "timeout_s": None})
    t = tasks_mod.load(layout, "T1")
    assert t.job.preflight is not None and t.job.preflight.ok is True
    reason = gate.check(layout, t, "process", "delivery", running=[])
    assert reason is None


def test_gate_does_not_see_a_value_pydantic_coerces_as_a_change(layout):
    """`timeout_s: "600"` (a string a lax-mode parse coerces to the int 600) is the same block before and after
    that coercion: the write-time digest must be taken over the coerced form too, or an unchanged block is held
    as 'stale' forever and rewriting it reproduces the same mismatch."""
    make_task(layout, "T1", job={"adapter": "process", "argv": [PY, "-c", "pass"], "timeout_s": "600"})
    t = tasks_mod.load(layout, "T1")
    assert t.job.timeout_s == 600
    assert t.job.preflight is not None and t.job.preflight.ok is True
    reason = gate.check(layout, t, "process", "delivery", running=[])
    assert reason is None


# ----------------------------------------------------------------------- section 3.3's table, the rows the first round skipped

def test_undeclared_parameter_refused(layout):
    _write_templates(layout, {"t": _process_template(params={})})
    job = {"adapter": "process", "template": "t", "params": {"ghost": "x"}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "parameter_rejected"


def test_leading_dash_parameter_refused_unless_the_slot_allows_it(layout):
    _write_templates(layout, {"t": _process_template(
        argv=[PY, "-c", "pass", "{flag}"], params={"flag": {"type": "str"}})})
    job = {"adapter": "process", "template": "t", "params": {"flag": "-x"}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "parameter_rejected"


def test_leading_dash_parameter_allowed_when_the_slot_says_so(layout):
    _write_templates(layout, {"t": _process_template(
        argv=[PY, "-c", "pass", "{flag}"], params={"flag": {"type": "str", "allow_leading_dash": True}})})
    job = {"adapter": "process", "template": "t", "params": {"flag": "-x"}}
    out = preflight.run(layout, job)
    assert out["ok"] is True


def test_stdin_missing_refused(layout):
    _write_templates(layout, {"t": _process_template()})
    job = {"adapter": "process", "template": "t", "params": {}, "stdin_file": "nope/missing_stdin.txt"}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "stdin_missing"


def test_probe_escaped_run_dir_refused(layout):
    """The probe's own working directory is a scratch directory under the run tree; a probe that writes a new
    top-level entry into the project root itself -- anywhere outside that scratch directory -- is refused, never
    silently allowed through because the probe's own exit code was 0."""
    marker = f"escaped_probe_marker_{id(layout)}.txt"
    code = f"import pathlib; pathlib.Path({str(layout.root)!r}, {marker!r}).write_text('x')"
    _write_templates(layout, {"t": _process_template(probe=[PY, "-c", code])})
    job = {"adapter": "process", "template": "t", "params": {}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "probe_escaped_run_dir"


# ------------------------------------------- the secret scan binds every block, not only a templated process one

SECRET_SHAPED = "sk-ant-" + "a" * 24          # shaped like a key, matched by policy.find_secrets, never a real one


def test_secret_shaped_value_refused_in_a_literal_process_block(layout):
    """The table's own `all` row (RECOVERY_PLAN.md section 3.3: "no secret-shaped value") binds a block with no
    template at all: a literal, owner-authored `argv` declares no slots, but its `params` are still scanned."""
    job = {"adapter": "process", "argv": [PY, "-c", "pass"], "params": {"key": SECRET_SHAPED}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "secret_shaped_value"


def test_secret_shaped_value_refused_in_a_python_block(layout):
    """And on the other adapter, templated or not: a python job's params go to its registered function rather
    than to argv, which is no reason for a credential written into the store to pass the scan."""
    @register_job("preflight_test_takes_token")
    def _takes_token(root, run_dir, token):
        return {"status": "COMPLETE"}
    try:
        job = {"adapter": "python", "fn": "preflight_test_takes_token", "params": {"token": SECRET_SHAPED}}
        out = preflight.run(layout, job)
        assert out["ok"] is False
        assert out["detail"]["code"] == "secret_shaped_value"
    finally:
        JOBS.pop("preflight_test_takes_token", None)


# ------------------------------------------- a templated block's declared slots are checked on either adapter

def test_a_templated_python_jobs_undeclared_parameter_is_refused(layout):
    """A template declares the slots its job may carry, whichever adapter it names: a param the template does
    not declare is refused even though the registered function's own signature would have bound it."""
    @register_job("preflight_test_takes_mode")
    def _takes_mode(root, run_dir, mode=None):
        return {"status": "COMPLETE"}
    try:
        _write_templates(layout, {"t": {"adapter": "python", "fn": "preflight_test_takes_mode",
                                        "may_be_set_by": ["owner"], "params": {}}})
        job = {"adapter": "python", "template": "t", "params": {"mode": "fast"}}
        out = preflight.run(layout, job)
        assert out["ok"] is False
        assert out["detail"]["code"] == "parameter_rejected"
    finally:
        JOBS.pop("preflight_test_takes_mode", None)


def test_a_templated_python_jobs_parameter_type_is_checked(layout):
    @register_job("preflight_test_takes_count")
    def _takes_count(root, run_dir, count):
        return {"status": "COMPLETE"}
    try:
        _write_templates(layout, {"t": {"adapter": "python", "fn": "preflight_test_takes_count",
                                        "may_be_set_by": ["owner"],
                                        "params": {"count": {"type": "int", "min": 1, "max": 8}}}})
        job = {"adapter": "python", "template": "t", "params": {"count": "lots"}}
        out = preflight.run(layout, job)
        assert out["ok"] is False
        assert out["detail"]["code"] == "parameter_type_invalid"
    finally:
        JOBS.pop("preflight_test_takes_count", None)


def test_a_literal_block_declares_no_slots_on_the_python_adapter_either(layout):
    """The literal rule is the same on both adapters: with no template there are no declared slots, so a param
    is neither required to be declared nor type-checked -- only the function's own signature binds it."""
    @register_job("preflight_test_takes_anything")
    def _takes_anything(root, run_dir, **params):
        return {"status": "COMPLETE"}
    try:
        job = {"adapter": "python", "fn": "preflight_test_takes_anything",
               "params": {"whatever": "x", "count": "not-an-int"}}
        assert preflight.run(layout, job)["ok"] is True
    finally:
        JOBS.pop("preflight_test_takes_anything", None)


# ------------------------------------------- the lane's cap is configuration, never the lane's own default

def _write_routing(layout, doc: dict):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_a_literal_process_block_may_declare_a_timeout_above_the_lane_default(layout):
    """`timeouts.PROCESS_CAP_S` is the duration a process block that declares none *gets*, not a ceiling
    anything ever refused (`timeouts.extend` exists to push a deadline out further still). A block asking for
    longer than the default is not a failure class this preflight invented a refusal for."""
    job = {"adapter": "process", "argv": [PY, "-c", "pass"], "timeout_s": timeouts.PROCESS_CAP_S + 1800}
    assert preflight.run(layout, job)["ok"] is True


def test_a_configured_lane_cap_refuses_a_block_above_it(layout):
    """A ceiling exists only where the project declared one: routing.yaml `timeouts.lane_caps.<adapter>`."""
    _write_routing(layout, {"timeouts": {"lane_caps": {"process": 600}}})
    job = {"adapter": "process", "argv": [PY, "-c", "pass"], "timeout_s": 1200}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "timeout_too_high"


def test_a_configured_lane_cap_binds_the_python_lane_too(layout):
    _write_routing(layout, {"timeouts": {"lane_caps": {"python": 600}}})
    job = {"adapter": "python", "fn": "noop", "timeout_s": 1200}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "timeout_too_high"


def test_the_lower_of_the_template_cap_and_the_configured_lane_cap_binds(layout):
    _write_routing(layout, {"timeouts": {"lane_caps": {"process": 600}}})
    _write_templates(layout, {"t": _process_template(timeout_s=1200)})
    job = {"adapter": "process", "template": "t", "params": {}, "timeout_s": 900}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "timeout_too_high"


# ------------------------------------------- an output path's own directory, and one code per refusal class

def test_an_output_paths_parent_must_exist(layout):
    """"every path parameter exists (or its parent does, for an output)" (section 3.3's table): a slot marked
    `must_exist: false` still names a directory something is about to write into, so a typo in it is the very
    failure class this preflight exists to catch rather than discover at run time."""
    _write_templates(layout, {"t": _process_template(
        argv=[PY, "-c", "pass", "{out}"], params={"out": {"type": "path", "roots": ["."], "must_exist": False}})})
    job = {"adapter": "process", "template": "t", "params": {"out": "repotrs/typo/out.md"}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "output_parent_missing"


def test_a_path_outside_its_declared_roots_has_its_own_code(layout):
    """Two refusal classes, two codes: a path that does not resolve inside the slot's declared roots is not the
    same finding as one that resolves there and does not exist."""
    (layout.root / "inside").mkdir(parents=True, exist_ok=True)
    _write_templates(layout, {"t": _process_template(
        argv=[PY, "-c", "pass", "{path}"],
        params={"path": {"type": "path", "roots": ["inside"], "must_exist": False}})})
    job = {"adapter": "process", "template": "t", "params": {"path": "elsewhere/x.txt"}}
    out = preflight.run(layout, job)
    assert out["ok"] is False
    assert out["detail"]["code"] == "path_outside_roots"


# ------------------------------------------- the probe leaves nothing behind

def test_the_probe_leaves_no_scratch_directory_behind(layout):
    """The probe's scratch directory is removed when it finishes, pass or fail: one per templated write, kept
    for the life of the project, is a leak nothing ever reads."""
    _write_templates(layout, {"t": _process_template(probe=[PY, "-c", "pass"])})
    assert preflight.run(layout, {"adapter": "process", "template": "t", "params": {}})["ok"] is True
    scratch = layout.runs / "_preflight"
    assert not scratch.exists() or not any(scratch.iterdir())
