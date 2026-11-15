"""An architect diagnosis `revise` fixes a wrong task contract without the owner.

Before, a diagnosis could requeue with guidance, defer, escalate or ask the owner, and none of those changes a task's
contract: guidance does nothing for a deterministic job, so a process task whose own argv was wrong always became an
owner inbox item, even when the diagnosis named the exact change. Now the manager sends a contract fix to the
architect with `guidance` (what is wrong and why), and the architect returns a structured patch (`changes`) and a
`reason`. The supervisor checks the revised contract the way `task create` does, writes it through
commands.apply_job / tasks.revise_contract, notes old value, new value and reason, grants a fresh retry budget once
and sets the task READY. Refused, invalid or weakening patches, a second revise for the same failure and a revise
from the manager go to the owner."""
import json
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from supervisor import diagnosis, escalation, inbox, jobs, store, tasks
from supervisor.adapters.base import Result
from supervisor.cli import app
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task
from tests.test_diagnosis_defer_release import diag_rec, events
from tests.test_exception_route_and_checkpoints import diag_records, fake_claude, run_until  # noqa: F401

# A stand-in tool with a guard: on the port another process already uses it refuses (and writes nothing), on any
# other port it writes its artifact. The port is argv element 3.
TOOL = ("import json, os, pathlib, sys; port = sys.argv[1]; ok = port != '8765'; "
        "ok and pathlib.Path('T.txt').write_text('ran'); "
        "pathlib.Path(os.environ['SUPERVISOR_RUN_DIR'], 'result.json').write_text(json.dumps("
        "{'status': 'COMPLETE', 'artifacts': ['T.txt'] if ok else [], "
        "'notes': 'ran on port ' + port if ok else 'refused: port ' + port + ' is already in use'}))")
ARGV = [sys.executable, "-c", TOOL, "8765"]
FIX = [{"path": "job.argv.3", "value": "18766"}]
REASON = "argv[3] names the port the project's own UI already uses; the tool's guard refuses every run on it"


def process_task(layout, tid="T", status=None, **fields):
    make_task(layout, "W", status="COMPLETE")
    doc = {"job": {"adapter": "process", "argv": list(ARGV), "timeout_s": 60}, "artifacts": [f"{tid}.txt"],
           "checks": [{"command": "python -m unittest"}], "depends_on": ["W"], "acceptance": "the tool writes T.txt",
           "cost_category": "deterministic", **fields}
    make_task(layout, tid, priority=5, **doc)
    if status != "NOT_STARTED":
        tasks.set_status(layout, tid, "IN_PROGRESS")
        tasks.set_status(layout, tid, "BLOCKED", actor="escalation", note="retries stopped")


def revise(layout, rec=None, changes=FIX, reason=REASON, **obj):
    rec = rec or diag_rec(layout, level="architect", role="chief_architect")
    raw = {"kind": "diagnosis", "schema_version": 1, "action": obj.pop("action", "revise"),
           "summary": "the job's own argv is wrong", "changes": changes, "reason": reason, **obj}
    return diagnosis.ingest(Supervisor(layout, sleep_s=0), rec, Result(status="COMPLETE", raw=raw))


def owner_text(layout):
    items = inbox.open_items(layout, "owner")
    assert len(items) == 1
    return items[0]["summary"] + (items[0].get("detail") or "")


def job_of(layout, tid="T"):
    return (tasks.load(layout, tid).model_extra or {}).get("job")


def test_a_revise_fixes_the_argv_notes_it_and_releases_the_task(layout):
    process_task(layout)
    out = revise(layout)
    assert out["action"] == "revise" and out["changes"] == [{"path": "job.argv.3", "old": "8765", "new": "18766"}]
    assert job_of(layout)["argv"] == ARGV[:3] + ["18766"] and job_of(layout)["timeout_s"] == 60
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.READY
    note = [n for n in t.model_extra["notes"] if n["by"] == "architect"]
    assert len(note) == 1 and '"8765" → "18766"' in note[0]["note"] and REASON in note[0]["note"]
    rev = t.model_extra["revisions"]
    assert len(rev) == 1 and rev[0]["by"] == "architect" and rev[0]["role"] == "chief_architect"
    assert rev[0]["reason"] == REASON and rev[0]["changes"][0]["old"] == "8765"
    assert escalation.current_grant(layout, "T") == 1
    assert inbox.open_items(layout, "owner") == []
    assert jobs.read(layout, "T.a9")["diag_action"] == "revise"
    assert {"task_job_set", "task_contract_revised", "diagnosis_revise"} <= set(events(layout))

    # a replay of the same result (crash before the job record closed) writes and grants nothing twice
    again = revise(layout, rec=jobs.read(layout, "T.a9"))
    assert again["action"] == "revise"
    t = tasks.load(layout, "T")
    assert len(t.model_extra["revisions"]) == 1 and escalation.current_grant(layout, "T") == 1
    assert len([n for n in t.model_extra["notes"] if n["by"] == "architect"]) == 1
    assert events(layout).count("task_job_set") == 1


def test_a_revise_may_add_verification_and_widen_what_the_task_owns(layout):
    process_task(layout)
    out = revise(layout, changes=FIX + [{"path": "checks.1", "value": {"command": "ruff check"}},
                                        {"path": "acceptance", "value": "the tool writes T.txt on a free port"},
                                        {"path": "owned_paths", "value": ["out"]}])
    assert out["action"] == "revise"
    t = tasks.load(layout, "T")
    assert [c.command for c in t.checks] == ["python -m unittest", "ruff check"]
    assert t.acceptance == "the tool writes T.txt on a free port" and t.owned_paths == ["out"]


def test_paths_address_the_contract_as_the_brief_shows_it(layout):
    """A check stored as a bare string is {"command": ...} in the brief's stored contract, and in the patch too."""
    process_task(layout, checks=["python -m unittest"])
    out = revise(layout, changes=FIX + [{"path": "checks.1", "value": {"command": "ruff check"}}])
    assert out["action"] == "revise"
    assert [c.command for c in tasks.load(layout, "T").checks] == ["python -m unittest", "ruff check"]
    plan, why = diagnosis.revision_plan(layout, tasks.load(layout, "T"),
                                        diag_rec(layout, attempt="T.a20", level="architect", role="chief_architect",
                                                 diag_signature="other"),
                                        {"changes": [{"path": "checks.0.command", "value": "pytest"}], "reason": REASON})
    assert plan is None and "drops or alters checks" in why       # reached the entry, then judged it a loosening


@pytest.mark.parametrize("changes, reason, why", [
    ([{"path": "job.argv.9", "value": "x"}], REASON, "indexes"),
    ([{"path": "job.params.port", "value": "1"}], REASON, "does not have"),
    ([{"path": "job.argv.3"}], REASON, "exactly {path, value}"),
    ([{"path": "job.argv", "value": [1, 2]}], REASON, "list of strings"),
    ([{"path": "job", "value": "run it"}], REASON, "job must stay an object"),
    ([], REASON, "names no changes"),
    ("job.argv.3=18766", REASON, "names no changes"),
    (FIX, "", "gives no reason"),
    ([{"path": "job.argv.3", "value": "8765"}], REASON, "changes nothing"),
    ([{"path": "checks.1", "value": {"command": "rm -rf build"}}], REASON, "allowed_test_commands"),
    ([{"path": "artifacts.1", "value": "/abs/out.txt"}], REASON, "relative to the project root"),
    ([{"path": "depends_on.1", "value": "NOPE"}], REASON, "unknown dependency"),
    ([{"path": "checks.1", "value": {"command": "pytest", "bogus": 1}}], REASON, "not a valid task contract"),
    ([{"path": "job.adapter", "value": "claude_cli"}], REASON, "job.adapter decides which lane"),
    ([{"path": "job.model", "value": "a-bigger-model"}], REASON, "job.model decides which lane"),
    ([{"path": f"x{n}", "value": 1} for n in range(11)], REASON, "at most 10"),
])
def test_an_invalid_or_refused_patch_goes_to_the_owner(layout, changes, reason, why):
    process_task(layout)
    before = store.read(tasks.path_for(layout, "T"))
    out = revise(layout, changes=changes, reason=reason)
    assert out["action"] == "owner" and out["inbox"]
    items = inbox.open_items(layout, "owner")
    assert len(items) == 1 and items[0]["kind"] == inbox.DECISION_KIND and items[0]["task_id"] == "T"
    text = items[0]["summary"] + (items[0].get("detail") or "")
    assert why in text and "set-job" in text
    after = store.read(tasks.path_for(layout, "T"))
    assert job_of(layout) == before["job"] and "revisions" not in after
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED
    assert jobs.read(layout, "T.a9")["diag_action"] == "owner"
    assert "diagnosis_revise_refused" in events(layout) and "task_job_set" not in events(layout)


@pytest.mark.parametrize("changes, field", [
    ([{"path": "checks", "value": []}], "checks"),
    ([{"path": "checks.0", "value": {"command": "python -m unittest", "timeout_s": 99999}}], "checks"),
    ([{"path": "artifacts.0", "value": "other.txt"}], "artifacts"),
    ([{"path": "tests", "value": []}], "tests"),
    ([{"path": "acceptance", "value": "it runs"}], "acceptance"),
    ([{"path": "depends_on", "value": []}], "depends_on"),
])
def test_a_patch_that_loosens_verification_goes_to_the_owner(layout, changes, field):
    process_task(layout)
    out = revise(layout, changes=FIX + changes)
    assert out["action"] == "owner"
    text = owner_text(layout)
    assert f"drops or alters {field}" in text and "loosening verification is the owner's call" in text
    assert job_of(layout)["argv"] == ARGV                  # nothing of the patch was applied, the argv fix included


@pytest.mark.parametrize("path, value", [
    ("owner_role", "chief_architect"), ("reviewer", ["qa"]), ("human_approval", "required"), ("phase", 1),
    ("cost_category", "claude"), ("objective", "something else"), ("title", "renamed"), ("priority", 99),
])
def test_owner_fields_are_never_revised(layout, path, value):
    process_task(layout)
    out = revise(layout, changes=FIX + [{"path": path, "value": value}])
    assert out["action"] == "owner"
    assert f"{path} is the owner's to change" in owner_text(layout)
    assert job_of(layout)["argv"] == ARGV


def test_a_second_revise_for_the_same_failure_goes_to_the_owner(layout):
    """The first revision did not fix it: the same signature reached an architect revise again."""
    process_task(layout)
    assert revise(layout)["action"] == "revise"
    jobs.set_phase(layout, "T.a9", "DONE")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.set_status(layout, "T", "BLOCKED", actor="escalation", note="failed again")
    rec = diag_rec(layout, attempt="T.a12", level="architect", role="chief_architect", diag_grant=1)
    out = revise(layout, rec=rec, changes=[{"path": "job.argv.3", "value": "18767"}])
    assert out["action"] == "owner"
    assert "already revised once for this failure" in owner_text(layout)
    assert job_of(layout)["argv"][3] == "18766" and len(tasks.load(layout, "T").model_extra["revisions"]) == 1


def test_a_different_failure_gets_its_own_revise_up_to_the_budget(layout):
    process_task(layout)
    for n, sig in ((1, "s1"), (2, "s2")):
        diag_rec(layout, attempt=f"T.a{n}", level="architect", role="chief_architect", diag_signature=sig,
                 phase="DONE", diag_action="revise", diag_revision={"changes": [], "fields": {}, "job": None})
    out = revise(layout)
    assert out["action"] == "owner" and "budget of 2 revisions spent" in owner_text(layout)
    jobs.set_phase(layout, "T.a2", "DONE", diag_revision=None)
    assert revise(layout, rec=diag_rec(layout, attempt="T.a13", level="architect", role="chief_architect"))["action"] == "revise"


def test_revise_at_the_manager_level_is_rejected_to_the_owner(layout):
    process_task(layout)
    out = revise(layout, rec=diag_rec(layout))                     # level manager, role senior_pm
    assert out["action"] == "owner"
    text = owner_text(layout)
    assert "'revise' not allowed at manager level" in text and "job.argv.3 = \"18766\"" in text
    assert job_of(layout)["argv"] == ARGV
    assert jobs.read(layout, "T.a9")["diag_action"] == "owner" and "diagnosis_failed" in events(layout)


def test_the_routing_switch_turns_revise_off(layout):
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text("escalation:\n  revise: false\n", encoding="utf-8")
    process_task(layout)
    out = revise(layout)
    assert out["action"] == "owner" and "escalation.revise: false" in owner_text(layout)
    assert job_of(layout)["argv"] == ARGV
    # and the briefs say so: the manager names the set_job a wrong command needs, the architect has no revise
    manager = diagnosis.brief(layout, tasks.load(layout, "T"), "senior_pm", "manager", "verification", "x", None)
    architect = diagnosis.brief(layout, tasks.load(layout, "T"), "chief_architect", "architect", "verification", "x", None)
    assert "task.set_job" in manager and "the architect can" not in manager
    assert "requeue, defer or owner" in architect and '"changes"' not in architect


def test_a_held_task_declines_the_revise(layout):
    process_task(layout)
    tasks.hold(layout, "T", "owner", "looking at it myself")
    out = revise(layout)
    assert out["action"] == "held"
    assert job_of(layout)["argv"] == ARGV and tasks.load(layout, "T").status == TaskStatus.BLOCKED
    assert jobs.read(layout, "T.a9")["diag_action"] == "held"


def test_the_result_contracts_send_a_contract_fix_to_the_architect_who_may_revise():
    manager, architect = diagnosis.RESULT_CONTRACT, diagnosis.ARCHITECT_RESULT_CONTRACT
    assert "requeue|defer|architect|owner" in manager and "revise" not in manager.split("```")[1]
    assert "change to this task's own contract" in manager and "`guidance`" in manager
    assert "requeue|defer|revise|owner" in architect and '"changes":[{"path"' in architect
    assert "job.argv.3" in architect and "loosens verification" in architect


def test_the_architect_brief_carries_the_managers_guidance_and_the_stored_job(layout):
    process_task(layout)
    handoff = {"role": "senior_pm", "summary": "every run is refused by the tool's guard",
               "guidance": "argv[3] is a port already in use; it needs a free one"}
    text = diagnosis.brief(layout, tasks.load(layout, "T"), "chief_architect", "architect", "verification", "x",
                           "I-001", handoff)
    assert "## Hand-off from the senior_pm diagnosis" in text
    assert handoff["summary"] in text and handoff["guidance"] in text
    stored = json.loads(text.split("## Stored contract\n```json\n")[1].split("\n```")[0])
    assert stored["job"]["argv"][3] == "8765" and stored["checks"] == [{"command": "python -m unittest", "timeout_s": None}]
    assert diagnosis.ARCHITECT_RESULT_CONTRACT in text


def test_check_warns_about_a_revised_contract_the_seed_does_not_have(layout, monkeypatch):
    """The framework never edits the seed; `supervisor check` names the drift and the revision behind it, and a
    re-import of the task says it replaces the revision."""
    process_task(layout)
    seed_doc = {k: v for k, v in store.read(tasks.path_for(layout, "T")).items()
                if k in tasks.CONTRACT_FIELDS or k in ("id", "job")}
    w_doc = {k: v for k, v in store.read(tasks.path_for(layout, "W")).items() if k in tasks.CONTRACT_FIELDS or k == "id"}
    seed = layout.root / "state" / "tasks_seed.yaml"
    seed.write_text(yaml.safe_dump({"tasks": [w_doc, seed_doc]}, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(layout.root)
    clean = CliRunner().invoke(app, ["check"])
    assert clean.exit_code == 0 and "differ from" not in clean.output

    revise(layout, changes=FIX + [{"path": "checks.1", "value": {"command": "ruff check"}}])
    out = CliRunner().invoke(app, ["check"])
    assert out.exit_code == 0
    line = [ln for ln in out.output.splitlines() if "differ from" in ln]
    assert len(line) == 1 and line[0].startswith("WARN T: the store's checks, job differ from state/tasks_seed.yaml")
    assert "job.argv.3, checks.1 revised by the chief_architect diagnosis" in line[0]
    assert "would put the seed's checks back" in line[0]

    dry = tasks.import_seed(layout, seed, ["T"], dry_run=True)
    assert any("replaces a contract a diagnosis revised" in w for w in dry["warnings"])


@pytest.mark.usefixtures("fake_claude")
def test_manager_hands_a_contract_fix_to_the_architect_whose_revise_lets_the_rerun_pass(layout, monkeypatch):
    """End to end: a process job's argv names a port its own guard refuses, so every run fails verification and
    the retries stop. The manager (tests/fixtures/fake_diagnosis.py) hands it to the architect with guidance; the
    architect revises argv[3]; the task runs again with the fixed argv and completes. The owner hears nothing."""
    guidance = "argv[3] names port 8765, which another process already uses; the tool refuses it. Use a free port."
    monkeypatch.setenv("FAKE_DIAGNOSIS_ACTION_SENIOR_PM", "architect")
    monkeypatch.setenv("FAKE_DIAGNOSIS_ACTION_CHIEF_ARCHITECT", "revise")
    monkeypatch.setenv("FAKE_DIAGNOSIS_GUIDANCE", guidance)
    monkeypatch.setenv("FAKE_DIAGNOSIS_CHANGES", json.dumps(FIX))
    process_task(layout, status="NOT_STARTED", checks=[])     # no project tests to run here
    run_until(layout, lambda: tasks.load(layout, "T").status == TaskStatus.COMPLETE, timeout=60)

    recs = diag_records(layout, "T")
    assert [(r["diag_level"], r["diag_action"]) for r in recs] == [("manager", "architect"), ("architect", "revise")]
    assert recs[1]["diag_handoff"]["guidance"] == guidance
    prompt = (Path(recs[1]["spec"]["run_dir"]) / "prompt.md").read_text(encoding="utf-8")
    assert "## Hand-off from the senior_pm diagnosis" in prompt and guidance in prompt
    assert '"8765"' in prompt                                   # the stored job the patch addresses
    assert job_of(layout)["argv"][3] == "18766" and (layout.root / "T.txt").read_text() == "ran"
    assert inbox.open_items(layout, "owner") == []
    assert store.read(layout.issues / "I-001.yaml")["diagnosis"][-1]["action"] == "revise"
