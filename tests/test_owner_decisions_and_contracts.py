"""1.4.0: owner decisions reach the inbox and take effect, long inbox text is kept, contract fixes reach a live loop.

- A long message used to be cut at MAX_SUMMARY_CHARS with the background first, so an escalation lost its question
  and options; inbox.fit() keeps the head as the summary and the full text as `detail`.
- The owner route of a diagnosis wrote a `question` item whose REQUEUE was recorded and ignored, and a task BLOCKED
  "owner decision needed" reached no one; both are now `decision` items acted on by inbox.apply_decision.
- `task import --update-contracts` refused while a supervisor held the lock, so a corrected seed could never be
  applied by anyone working under the loop; it is now the `task.import` command.
- Artifact entries the contract floor can never satisfy (escaping paths, directories, `<id>`) are refused up front;
  prose- or placeholder-like spellings that are still legal file names are accepted with a warning (1.4.1)."""
import pytest
import yaml
from fastapi.testclient import TestClient

from supervisor import commands, diagnosis, inbox, render, tasks
from supervisor.locks import FileLock
from supervisor.loop import Supervisor
from supervisor.models.task import Task, TaskStatus
from supervisor.ui.app import create_app
from supervisor.ui.auth import CSRF_COOKIE
from tests.conftest import make_task

LONG = " ".join(f"word{n}" for n in range(400))          # ~3,000 characters


def test_fit_keeps_short_text_and_moves_long_text_to_detail():
    assert inbox.fit("short") == ("short", None)
    summary, detail = inbox.fit(LONG)
    assert len(summary) <= inbox.MAX_SUMMARY_CHARS and summary.endswith("…")
    assert detail == LONG
    assert not summary[:-1].endswith("word")              # cut on a word boundary, not mid-word


def test_create_stores_detail_and_both_views_show_it(layout):
    summary, detail = inbox.fit(LONG)
    item = inbox.create(layout, for_role="owner", subject="s", summary=summary, detail=detail)
    assert item["detail"] == LONG
    assert "word399" in render.render_inbox(layout).read_text(encoding="utf-8")
    client = TestClient(create_app(layout), base_url="http://127.0.0.1")
    client.cookies.set(CSRF_COOKIE, "c")
    page = client.get("/inbox").text
    assert "Show full message" in page and "word399" in page


def test_short_items_carry_no_detail(layout):
    item = inbox.create(layout, for_role="owner", subject="s", summary="short", detail="short")
    assert "detail" not in item


def failed_task(layout, tid="T"):
    make_task(layout, tid)
    tasks.set_status(layout, tid, "IN_PROGRESS")
    tasks.set_status(layout, tid, "FAILED", note="verification failed")


def notes(layout, tid="T"):
    return [n["note"] for n in yaml.safe_load((layout.tasks / f"{tid}.yaml").read_text(encoding="utf-8")).get("notes") or []]


def test_owner_route_leads_with_the_question_and_keeps_the_whole_diagnosis(layout):
    failed_task(layout)
    out = diagnosis.route_to_owner(layout, tasks.load(layout, "T"), {"diag_role": "manager"}, LONG,
                                   "Fix the artifact glob?", "needs the owner")
    item = inbox.load(layout, out["inbox"])
    assert item["kind"] == inbox.DECISION_KIND and item["resume"] == "task"
    assert item["summary"].startswith("Fix the artifact glob? REQUEUE =")
    assert "CANCEL" in item["summary"] and "DEFER" in item["summary"]
    assert "word399" in item["detail"]


def test_requeue_with_text_puts_a_failed_task_back_with_the_guidance(layout):
    failed_task(layout)
    out = diagnosis.route_to_owner(layout, tasks.load(layout, "T"), {}, "s", "q", "why")
    inbox.answer(layout, out["inbox"], "REQUEUE contract fixed, deliver the existing outputs")   # button + typed text
    assert tasks.load(layout, "T").status == TaskStatus.READY
    assert any("contract fixed" in n for n in notes(layout))
    assert inbox.load(layout, out["inbox"])["applied"]["resumed"] == "task"


def test_an_escalation_question_from_an_older_version_is_acted_on_too(layout):
    failed_task(layout)
    item = inbox.create(layout, for_role="owner", subject="Decision needed", summary="s", kind="question",
                        allowed_responses=["REQUEUE", "CANCEL", "DEFER"], from_="escalation", task_id="T")
    inbox.answer(layout, item["id"], "CANCEL")
    assert tasks.load(layout, "T").status == TaskStatus.CANCELLED


def test_an_ordinary_agent_question_is_not_a_decision(layout):
    failed_task(layout)
    item = inbox.create(layout, for_role="owner", subject="q", summary="s", kind="question", from_="engineer",
                        task_id="T", allowed_responses=["REQUEUE", "CANCEL", "DEFER"])
    inbox.answer(layout, item["id"], "REQUEUE")
    assert tasks.load(layout, "T").status == TaskStatus.FAILED


def test_a_task_blocked_for_the_owner_reaches_the_inbox_once_and_requeue_resumes_it(layout):
    make_task(layout, "B")
    tasks.set_status(layout, "B", "IN_PROGRESS")
    tasks.set_status(layout, "B", "BLOCKED", note="B.a3: capacity handoff after 4 continuations — owner decision needed")
    sup = Supervisor(layout, sleep_s=0)
    sup.request_owner_decisions()
    sup.request_owner_decisions()
    items = [i for i in inbox.all_items(layout) if i.get("task_id") == "B"]
    assert len(items) == 1 and items[0]["kind"] == inbox.DECISION_KIND and items[0]["resume"] == "delivery"
    assert items[0]["summary"].startswith("REQUEUE = resume the work")
    inbox.answer(layout, items[0]["id"], "REQUEUE")
    assert tasks.load(layout, "B").status == TaskStatus.READY


def write_seed(layout, artifacts):
    doc = yaml.safe_load((layout.tasks / "T.yaml").read_text(encoding="utf-8"))
    seed = {k: v for k, v in doc.items() if k in tasks.CONTRACT_FIELDS or k in ("id", "status")}
    seed.update(artifacts=artifacts, owned_paths=["."])
    extra = dict(seed, id="NEW", status="NOT_STARTED")
    (layout.state / "tasks_seed.yaml").write_text(yaml.safe_dump({"tasks": [seed, extra]}), encoding="utf-8")


def test_task_import_reaches_a_live_supervisor_through_the_queue(layout):
    failed_task(layout)
    write_seed(layout, ["out/sheet*.png"])
    with FileLock(layout.lock, owner="supervisor"):          # a running loop holds the lock
        r = commands.submit_or_apply(layout, "task.import", {"update_contracts": ["T"], "create": False}, "owner")
        assert r.get("queued")
        out = commands.apply_all(layout)                     # what the loop does on its next iteration
    assert out[0]["ok"], out[0].get("error")
    assert out[0]["result"] == {"created": [], "updated": ["T"]}
    assert tasks.load(layout, "T").artifacts == ["out/sheet*.png"]
    assert not (layout.tasks / "NEW.yaml").exists()          # create=False touches only the named contract


def test_task_import_refuses_a_seed_outside_the_project(layout, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "seed.yaml"
    outside.write_text("tasks: []\n", encoding="utf-8")
    r = commands.submit_or_apply(layout, "task.import", {"seed": str(outside)}, "owner")
    assert not r["ok"] and "inside the project" in r["error"]


def contract(*artifacts):
    return Task.model_validate({"id": "C", "phase": 0, "title": "c", "objective": "o", "owner_role": "engineer",
                                "actions": ["a"], "artifacts": list(artifacts), "tests": ["t"], "acceptance": "ok",
                                "escalation": "none", "human_approval": "none", "cost_category": "claude"})


@pytest.mark.parametrize("bad", ["bench/runs/<id>/summary.json", "sims/cycle_N/", "/abs/file.txt", "../outside.txt",
                                 "C:/x.txt", "dir/", " padded.txt", ""])
def test_unsatisfiable_artifact_entries_are_named(bad):
    errors = tasks.artifact_errors([contract(bad)])
    assert len(errors) == 1 and repr(bad) in errors[0]


@pytest.mark.parametrize("odd", ["out contact sheets", "tasks T10-xx", "experiments/EXP-1xx.yaml",
                                 "strategies/S1..S6.yaml", "cands/round_N/*", "Final Report.md", "out/xx.json",
                                 "out/round_N_results.txt", "cfg/{name}.yaml"])
def test_prose_or_placeholder_spellings_are_warnings_not_refusals(odd):
    """1.4.0 refused these outright, but each is also a legal file name (a project may well deliver "Final
    Report.md"): the contract is accepted and the caller warned."""
    errs, warns = tasks.artifact_problems([contract(odd)])
    assert errs == [] and len(warns) == 1 and repr(odd) in warns[0]


def test_an_odd_name_that_exists_on_disk_raises_no_warning(layout):
    (layout.root / "out").mkdir()
    for name in ("Final Report.md", "out/xx.json", "out/round_N_results.txt"):
        (layout.root / name).write_text("real", encoding="utf-8")
        assert tasks.artifact_problems([contract(name)], root=layout.root) == ([], [])


@pytest.mark.parametrize("good", ["README.md", "experiments/EXP-1[0-9][0-9].yaml", "out/run/contact_sheet*.png",
                                  "bench/runs/*/summary.json", "strategies/S[1-6].yaml", "workflows/flow_v1.json",
                                  "state/tasks/T10-0[2-9].yaml"])
def test_real_paths_and_globs_pass(good):
    assert tasks.artifact_errors([contract(good)]) == []


def test_an_existing_directory_is_refused_when_the_root_is_known(layout):
    (layout.root / "experiments").mkdir(exist_ok=True)
    assert tasks.artifact_errors([contract("experiments")], root=layout.root)
    assert tasks.artifact_errors([contract("experiments")]) == []


def test_import_and_create_accept_an_odd_name_with_a_warning(layout):
    failed_task(layout)
    write_seed(layout, ["Final Report.md"])
    r = tasks.import_seed(layout, layout.state / "tasks_seed.yaml", ["T"], create=False)
    assert r["updated"] == ["T"] and "Final Report.md" in r["warnings"][0]
    assert tasks.load(layout, "T").artifacts == ["Final Report.md"]
    doc = contract("out/round_N_results.txt").model_dump(mode="json")
    doc.update(id="NEW2", owned_paths=["."])
    r = tasks.create(layout, doc)
    assert r["id"] == "NEW2" and "round_N_results.txt" in r["warnings"][0]


def test_import_and_create_still_refuse_an_unsatisfiable_artifact(layout):
    failed_task(layout)
    write_seed(layout, ["runs/<id>/summary.json"])
    with pytest.raises(tasks.TaskError, match="summary.json"):
        tasks.import_seed(layout, layout.state / "tasks_seed.yaml", ["T"], create=False)
    doc = contract("out/").model_dump(mode="json")
    doc.update(id="NEW2", owned_paths=["."])
    with pytest.raises(tasks.TaskError, match="directory"):
        tasks.create(layout, doc)


def test_odd_names_verify_against_real_files(layout):
    """The reviewer's case: these names verify fine, so refusing them at create time was the bug."""
    from supervisor import verify
    (layout.root / "out").mkdir()
    doc = contract("Final Report.md", "out/xx.json", "out/round_N_results.txt").model_dump(mode="json")
    doc.update(id="ODD", owned_paths=["."])
    assert tasks.create(layout, doc)["id"] == "ODD"
    task = tasks.load(layout, "ODD")
    before = verify.snapshot(layout, task)
    for name in task.artifacts:
        (layout.root / name).write_text("real", encoding="utf-8")
    outcome = verify.verify(layout, task, list(task.artifacts), before=before)
    assert outcome.ok, outcome.detail


# ------------------------------------------------------------------ 1.4.1: REQUEUE text reaches the next packet

def test_requeue_text_reaches_the_next_delivery_packet(layout):
    """The owner's REQUEUE text used to be stored only as a note, which no packet reads: the item was applied, the task
    went READY, and the next worker never saw the correction. It must be in the brief the worker actually gets."""
    from supervisor import packets
    from supervisor.adapters import REGISTRY
    failed_task(layout)
    out = diagnosis.route_to_owner(layout, tasks.load(layout, "T"), {}, "s", "q", "why")
    inbox.answer(layout, out["inbox"], "REQUEUE use cached output from approved experiment ZEBRA")
    task = tasks.load(layout, "T")
    assert task.status == TaskStatus.READY
    assert "experiment ZEBRA" in packets.delivery_brief(layout, task)
    spec = REGISTRY["claude_cli"].prepare(layout, task, "T.a9")                 # the prompt file the agent reads
    assert "experiment ZEBRA" in open(spec["stdin_file"], encoding="utf-8").read()
    inbox.apply_decision(layout, {**inbox.load(layout, out["inbox"]), "applied": None})   # a replay adds nothing
    assert len(tasks.load(layout, "T").model_extra["guidance"]) == 1


def test_requeue_text_reaches_a_job_with_its_own_prompt(layout):
    from supervisor.adapters import REGISTRY
    failed_task(layout)
    tasks._update(layout, "T", lambda d: d.update(job={"adapter": "claude_cli", "prompt": "# custom brief"}))
    out = diagnosis.route_to_owner(layout, tasks.load(layout, "T"), {}, "s", "q", "why")
    inbox.answer(layout, out["inbox"], "REQUEUE write the summary in French")
    spec = REGISTRY["claude_cli"].prepare(layout, tasks.load(layout, "T"), "T.a9")
    text = open(spec["stdin_file"], encoding="utf-8").read()
    assert text.startswith("# custom brief") and "write the summary in French" in text
    spec = REGISTRY["codex_cli"].prepare(layout, tasks.load(layout, "T"), "T.a10")
    assert "write the summary in French" in open(spec["stdin_file"], encoding="utf-8").read()


def test_review_requeue_text_reaches_the_review_of_that_digest_only(layout):
    from supervisor import packets
    make_task(layout, "R")
    tasks._update(layout, "R", lambda d: d.update(delivery={"digest": "d1"}))
    tasks.set_status(layout, "R", "IN_PROGRESS")
    tasks.set_status(layout, "R", "BLOCKED", note="review of digest d1 failed 3 times; owner decision needed")
    Supervisor(layout, sleep_s=0).request_owner_decisions()
    item = next(i for i in inbox.all_items(layout) if i.get("task_id") == "R")
    assert item["resume"] == "review"
    inbox.answer(layout, item["id"], "REQUEUE the flaky benchmark is known; judge the report only")
    task = tasks.load(layout, "R")
    assert task.status == TaskStatus.WAITING_FOR_REVIEW
    assert "judge the report only" in packets.review_brief(layout, task, "d1", ["x"], "reviewer")
    assert "judge the report only" not in packets.review_brief(layout, task, "d2", ["x"], "reviewer")
    assert "judge the report only" not in packets.delivery_brief(layout, task)


# ------------------------------------------------------------------ 1.4.1: REQUEUE resets the escalation budget

def test_owner_requeue_restores_the_revision_budget(layout, monkeypatch):
    """An exhausted escalation record (revisions spent, signatures kept) survived the owner's REQUEUE, so the next
    failure escalated at once instead of getting the normal revision budget."""
    from supervisor import escalation
    failed_task(layout)
    state = escalation._load(layout, "T")
    state.update(revisions=3, signatures=["a", "b", "c"])
    escalation._save(layout, "T", state)
    out = diagnosis.route_to_owner(layout, tasks.load(layout, "T"), {}, "s", "q", "why")
    inbox.answer(layout, out["inbox"], "REQUEUE")
    state = escalation._load(layout, "T")
    assert state["revisions"] == 0 and state["signatures"] == [] and state["resets"] == 1
    monkeypatch.setattr(escalation, "_apply", lambda sup, task, source, detail, decision: decision)   # no dispatch
    tasks.set_status(layout, "T", "IN_PROGRESS")
    sup = Supervisor(layout, sleep_s=0)
    assert escalation.record_failure(sup, "T", "verify", "c missing", event="T.a5")["action"] == "retry"
    assert escalation.record_failure(sup, "T", "verify", "d missing", event="T.a6")["action"] == "retry"
    assert escalation.record_failure(sup, "T", "verify", "e missing", event="T.a7")["action"] == "escalate"
    item = {**inbox.load(layout, out["inbox"]), "applied": None}
    tasks._update(layout, "T", lambda d: d.update(status="FAILED"))
    inbox.apply_decision(layout, item)                                        # a replay of the same decision
    assert escalation._load(layout, "T")["resets"] == 1                       # never a second grant
