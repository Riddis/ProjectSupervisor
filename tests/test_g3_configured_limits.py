"""2.4.0 G3: the three test-evidence limits (per-command timeout, overall budget, command count) become
configuration with today's constants as defaults; the inline verification path (as_job: false) clamps the two time
limits to the built-in ceiling because it runs inside the loop, holding state/.lock."""
import yaml

from supervisor import packets, tasks, verify
from supervisor.loop import Supervisor
from tests.conftest import make_task


def test_limits_default_to_the_built_in_constants_when_unconfigured(layout):
    assert verify.limits(layout) == {"per_command_timeout_s": verify.TEST_TIMEOUT_S,
                                     "budget_s": verify.TEST_BUDGET_S, "max_commands": verify.MAX_TEST_COMMANDS}


def test_limits_are_read_from_configured_routing(layout):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(
        {"verification": {"per_command_timeout_s": 5, "budget_s": 20, "max_commands": 3}}))
    assert verify.limits(layout) == {"per_command_timeout_s": 5, "budget_s": 20, "max_commands": 3}


def test_inline_path_clamps_a_larger_configured_limit_to_the_built_in_ceiling(layout, monkeypatch):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(
        {"verification": {"per_command_timeout_s": 999999, "budget_s": 999999}}))
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    captured = {}

    def fake(root, declared, allowed=None, tag="verify", roots=(), timeout_s=None, budget_s=None, max_commands=None):
        captured.update(timeout_s=timeout_s, budget_s=budget_s)
        return True, []
    monkeypatch.setattr(verify, "run_declared_tests", fake)

    verify.verify(layout, task, [], declared_tests=[{"command": "pytest -q", "exit_code": 0}], require_tests=False,
                  run_commands=True)

    assert captured["timeout_s"] == verify.TEST_TIMEOUT_S
    assert captured["budget_s"] == verify.TEST_BUDGET_S


def test_the_tracked_verify_job_honours_the_configured_limits_without_clamping(layout, monkeypatch):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(
        {"verification": {"per_command_timeout_s": 5, "budget_s": 20}}))
    from supervisor import verify_job
    captured = {}

    def fake(root, declared, allowed=None, tag="verify", roots=(), timeout_s=None, budget_s=None, max_commands=None):
        captured.update(timeout_s=timeout_s, budget_s=budget_s)
        return True, []
    monkeypatch.setattr(verify, "run_declared_tests", fake)
    import json
    spec_path = layout.root / "verify_input.json"
    spec_path.write_text(json.dumps({"root": str(layout.root), "task_id": "T", "digest": "d", "commands": [], "fixed": []}))

    verify_job.run(spec_path)

    assert captured["timeout_s"] == 5 and captured["budget_s"] == 20


def test_dispatch_verify_computes_the_process_timeout_from_the_configured_budget(layout, monkeypatch):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({"verification": {"budget_s": 30}}))
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    sup = Supervisor(layout, sleep_s=0)
    captured = {}

    def fake_dispatch(view, name, **kw):
        captured["timeout_s"] = (view.model_extra or {}).get("job", {}).get("timeout_s")
        return {"attempt_id": "T.verify1"}
    monkeypatch.setattr(sup, "dispatch", fake_dispatch)
    run_dir = layout.runs / "T" / "T.a1"
    run_dir.mkdir(parents=True, exist_ok=True)

    sup.dispatch_verify(task, {"spec": {"run_dir": str(run_dir)}, "attempt_id": "T.a1"}, "d" * 8, [], [])

    assert captured["timeout_s"] == 30 + 120


def test_brief_states_the_configured_limits(layout):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump(
        {"verification": {"per_command_timeout_s": 42, "budget_s": 777, "max_commands": 4}}))
    make_task(layout, "T")
    task = tasks.load(layout, "T")
    text = packets.result_contract_delivery(layout, task)
    assert "42 s" in text and "777 s" in text and "**4**" in text
