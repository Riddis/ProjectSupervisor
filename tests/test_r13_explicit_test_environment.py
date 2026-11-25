"""The part a later release left: the verifier's
environment is still built from this process's whole `os.environ` (only six named job-control variables dropped,
`verify.py` before this change), the allow-list's normaliser lives only in `verify.permitted` with nothing shared
to the worker's own brief, interpreter/cwd/PATH are recorded per command but never as one pinned triple the brief
also states, and a document-only task's `evidence` still defaults to `auto` at import. Invented task ids and
artifact paths throughout."""
from __future__ import annotations

import sys

import yaml

from supervisor import packets, tasks, verify
from tests.conftest import make_task


def _row(tid, **fields):
    base = dict(phase=0, title="t", objective="o", owner_role="engineer", artifacts=["x"], tests=["t"],
               cost_category="deterministic")
    base.update(fields)
    base["id"] = tid
    return base


def _seed(layout, *rows):
    p = layout.state / "seed.yaml"
    p.write_text(yaml.safe_dump({"tasks": list(rows)}), encoding="utf-8")
    return p


# ---------------------------------------------------------------- one normaliser, shared

def test_shared_normaliser_used_by_both_the_allow_list_and_the_brief():
    assert packets.normalise_command is verify.normalise_command
    bare, module_form, venv_form = "pytest", "python -m pytest", f"{sys.executable} -m pytest"
    assert verify.normalise_command(bare) == verify.normalise_command(module_form) == verify.normalise_command(venv_form)


def test_uppercase_and_dotted_interpreter_spellings_are_decided_the_same_way_by_both():
    """`permitted()` is the allow-list's own decision; a caller with no access to it (the brief, or anything else
    that wants to know whether a spelling would be admitted) can reproduce the identical verdict from nothing but
    the public, shared `normalise_command` — proved here for the two spellings the 2.7.0 review named (P3-2)."""
    allowed = ["pytest"]
    cases = {"PYTHON -M PYTEST tests": True, "python3.11 -m pytest tests": False}
    normalised_allowed = [verify.normalise_command(a) for a in allowed if verify.normalise_command(a)]
    for command, admitted in cases.items():
        decided_by_the_allow_list = verify.permitted(command, allowed)
        decided_by_the_brief = any(verify.normalise_command(command)[:len(w)] == w for w in normalised_allowed)
        assert decided_by_the_allow_list is admitted
        assert decided_by_the_brief == decided_by_the_allow_list


# ---------------------------------------------------------------- the derived environment

def test_declared_env_drops_every_supervisor_prefixed_name_not_just_the_named_six(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_BRAND_NEW_MARKER_NOBODY_NAMED_YET", "x")
    env = verify.declared_test_env()
    assert not any(k.startswith("SUPERVISOR_") for k in env)


def test_declared_env_derives_from_the_base_allow_list_not_the_whole_process_environment(monkeypatch):
    monkeypatch.setenv("SOME_VENDOR_TOOL_TOKEN_NOT_ON_THE_BASE_LIST", "secret-ish")
    env = verify.declared_test_env()
    assert "SOME_VENDOR_TOOL_TOKEN_NOT_ON_THE_BASE_LIST" not in env
    assert "PATH" in env


def test_pythondontwritebytecode_is_set():
    assert verify.declared_test_env()["PYTHONDONTWRITEBYTECODE"] == "1"


def test_interpreter_cwd_and_path_in_verification_json_equal_the_briefs_own_statement(layout):
    make_task(layout, "T", cost_category="claude")
    task = tasks.load(layout, "T")
    env = verify.declared_environment(layout)
    outcome = verify.commands_outcome(True, [], [], False, environment=env)
    assert outcome.report["environment"] == env
    brief = packets.delivery_brief(layout, task)
    assert env["interpreter"] in brief
    assert env["cwd"] in brief
    assert env["path"] in brief


# ---------------------------------------------------------------- basetemp cleanup (reaffirmed against this module's
# own call shape, which now threads `layout` through)

def test_basetemp_is_removed_after_a_passing_run_and_kept_after_a_failing_one(layout):
    (layout.root / "test_ok.py").write_text("def test_ok(tmp_path):\n    assert tmp_path.exists()\n", encoding="utf-8")
    ok_cmd = f"{sys.executable} -m pytest -q -p no:cacheprovider test_ok.py"
    ok, _ = verify.run_declared_tests(layout.root, [{"command": ok_cmd}], allowed=["python -m pytest"], layout=layout)
    assert ok is True
    assert list((layout.root / "runs" / "pytest_tmp").iterdir()) == []

    (layout.root / "test_bad.py").write_text("def test_no(tmp_path):\n    assert False\n", encoding="utf-8")
    bad_cmd = f"{sys.executable} -m pytest -q -p no:cacheprovider test_bad.py"
    ok2, results = verify.run_declared_tests(layout.root, [{"command": bad_cmd}], allowed=["python -m pytest"],
                                             layout=layout)
    assert ok2 is False
    kept = list((layout.root / "runs" / "pytest_tmp").iterdir())
    assert len(kept) == 1
    assert results[0].get("basetemp")


# ---------------------------------------------------------------- evidence settable everywhere, defaulted at import

def test_evidence_settable_by_create_patch_and_a_seed_refresh(layout):
    tasks.create(layout, _row("E1", evidence="commands"))
    assert tasks.load(layout, "E1").evidence == "commands"

    tasks.patch_task(layout, "E1", {"evidence": "review"})
    assert tasks.load(layout, "E1").evidence == "review"

    tasks.import_seed(layout, _seed(layout, _row("E1", evidence="commands")), update_ids=["E1"])
    assert tasks.load(layout, "E1").evidence == "commands"


def test_a_document_only_task_defaults_evidence_to_review_at_import(layout):
    row = _row("DOC1", artifacts=["docs/releases/note.md"], tests=["the note explains the change"])
    row.pop("evidence", None)
    tasks.import_seed(layout, _seed(layout, row))
    assert tasks.load(layout, "DOC1").evidence == "review"


def test_a_code_artifact_task_keeps_the_auto_default_at_import(layout):
    row = _row("CODE1", artifacts=["widget.py"], tests=["t"])
    row.pop("evidence", None)
    tasks.import_seed(layout, _seed(layout, row))
    assert tasks.load(layout, "CODE1").evidence == "auto"


def test_a_document_only_task_created_directly_also_defaults_to_review(layout):
    doc = _row("DOC2", artifacts=["docs/plan/NOTE.md"])
    doc.pop("evidence", None)
    tasks.create(layout, doc)
    assert tasks.load(layout, "DOC2").evidence == "review"
