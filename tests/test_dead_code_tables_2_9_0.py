"""The framework review's three remaining dead-code tables (FRAMEWORK_REVIEW_2026-11-14.md, "Dead code: what can
be removed"): Dependencies, Used only by tests, and Dead data fields. Each item gets one case here, naming the
verdict the release record states -- a production reader wired in, the surface removed with its test rewritten,
or (Dependencies, `find_secrets`, `redact`, `Result.digest`) confirmation that an earlier step already closed it.
"""
from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from supervisor import plugins, policy, project, tasks
from supervisor.adapters.base import Result
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

ROOT = Path(__file__).resolve().parents[1]


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _calls_in(relpath: str, function_name: str) -> set[str]:
    tree = ast.parse((ROOT / relpath).read_text(encoding="utf-8"), filename=relpath)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function_name)
    return {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def _attrs_in(relpath: str, function_name: str) -> set[str]:
    tree = ast.parse((ROOT / relpath).read_text(encoding="utf-8"), filename=relpath)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function_name)
    return {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}


# ------------------------------------------------------------------ Dependencies

def test_no_extra_declares_the_three_never_imported_distributions():
    doc = _toml(ROOT / "pyproject.toml")
    every = " ".join(list(doc["project"]["dependencies"]) +
                     [d for extra in doc["project"]["optional-dependencies"].values() for d in extra])
    for name in ("portalocker", "python-ulid", "sse-starlette", "sse_starlette"):
        assert name not in every


def test_boundary_toml_and_its_test_map_name_none_of_the_three():
    boundary = (ROOT / "boundary.toml").read_text(encoding="utf-8")
    test_boundary = (ROOT / "tests" / "test_boundary.py").read_text(encoding="utf-8")
    for name in ("portalocker", "ulid", "sse_starlette", "python-ulid", "sse-starlette"):
        assert name not in boundary
        assert name not in test_boundary


def test_jsonschema_and_pillow_sit_in_the_extra_that_imports_them():
    doc = _toml(ROOT / "pyproject.toml")
    core = doc["project"]["dependencies"]
    extras = doc["project"]["optional-dependencies"]
    assert not any(d.startswith("jsonschema") for d in core)
    assert not any(d.lower().startswith("pillow") for d in core)
    assert any(d.startswith("jsonschema") for d in extras["dev"])
    assert any(d.lower().startswith("pillow") for d in extras["ui"])


def test_llm_endpoint_plugin_declares_the_psutil_it_imports():
    doc = _toml(ROOT / "packages" / "supervisor-plugin-llm-endpoint" / "pyproject.toml")
    assert any(d.startswith("psutil") for d in doc["project"]["dependencies"])


# ------------------------------------------------------------------ Used only by tests

def test_find_secrets_and_redact_are_already_production_used():
    """The table was written against an earlier head; `plans.py` (RF-21) and `preflight.py` now call both, so
    these two items are stale as of this release and need no further action."""
    plans_src = (ROOT / "supervisor" / "plans.py").read_text(encoding="utf-8")
    preflight_src = (ROOT / "supervisor" / "preflight.py").read_text(encoding="utf-8")
    assert "policy_mod.redact(" in plans_src
    assert "policy_mod.find_secrets(" in plans_src
    assert "policy_mod.find_secrets(" in preflight_src


def test_secret_values_backs_find_secrets_instead_of_the_private_scan():
    assert "secret_values" in _calls_in("supervisor/policy.py", "find_secrets")


def test_placeholder_backs_the_redaction_context_builder():
    assert "placeholder" in _calls_in("supervisor/policy.py", "_redaction")


def test_discovery_found_legacy_backs_the_unmanifested_branch():
    assert "legacy" in _attrs_in("supervisor/plugins.py", "_load_into")


def test_completion_check_is_gone_and_evaluate_is_the_one_reader(layout):
    assert not hasattr(tasks, "completion_check")
    make_task(layout, "A", reviewer=["code_review"], status="IN_PROGRESS")
    tasks.record_delivery(layout, "A", "d1")
    tasks.record_verification(layout, "A", "d1", ok=True)
    tasks.record_review(layout, "A", "d1", "code_review", "ACCEPT")
    assert tasks._evaluate(tasks.load(layout, "A"))[0] == TaskStatus.COMPLETE


def test_artifact_errors_is_gone_and_artifact_problems_is_the_one_reader():
    assert not hasattr(tasks, "artifact_errors")
    assert tasks.artifact_problems([]) == ([], [])


def test_project_plugins_is_gone_and_plugins_specs_is_the_one_reader():
    assert not hasattr(project, "plugins")
    assert hasattr(plugins, "specs")


def test_recorded_projections_is_gone():
    assert not hasattr(plugins, "_recorded_projections")


# ------------------------------------------------------------------ Dead data fields: Result.model/.session_id/.usage

def test_result_fields_stay_declared_but_no_built_in_adapter_populates_them():
    assert {"model", "session_id", "usage"} <= set(Result.__dataclass_fields__)
    for relpath in ("supervisor/adapters/claude_cli.py", "supervisor/adapters/codex_cli.py",
                    "supervisor/adapters/claude_api.py"):
        tree = ast.parse((ROOT / relpath).read_text(encoding="utf-8"), filename=relpath)
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "Result":
                kw = {k.arg for k in call.keywords}
                assert not kw & {"model", "session_id", "usage"}, f"{relpath} still sets {kw}"


def test_result_digest_stays_public_and_documented_reserved():
    assert "digest" in Result.__dataclass_fields__
    doc = (ROOT / "docs" / "PLUGIN_API.md").read_text(encoding="utf-8")
    assert "`digest: str \\| None` | **reserved, unset" in doc
