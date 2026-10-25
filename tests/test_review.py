import os
import sys
import time
from pathlib import Path

import pytest

from supervisor import escalation, jobs as jobs_mod, review, tasks
from supervisor._vendor.contract import CapacityDeferred
from supervisor.adapters import REGISTRY, python as pyad
from supervisor.adapters.base import Result
from supervisor.loop import Supervisor
from supervisor.models.task import TaskStatus
from tests.conftest import make_task

FAKE_REVIEW = str(Path(__file__).resolve().parent / "fixtures" / "fake_review.py")


class FakeReviewClaude(REGISTRY["claude_cli"].__class__):
    name = "claude_cli"

    def command(self, spec):
        argv = super().command(spec)
        return [sys.executable, FAKE_REVIEW, "--fake-role", spec.get("role", "")] + argv[1:]


@pyad.job("write_artifact")
def _write_artifact(root, run_dir, name="out.txt", content="x", **_):
    (root / name).write_text(content)
    return {"status": "COMPLETE", "artifacts": [name], "notes": "wrote " + name}


def review_task(layout, tid, reviewer=("code_review",)):
    make_task(layout, tid, reviewer=reviewer)
    def fn(doc):
        doc["job"] = {"adapter": "python", "fn": "write_artifact", "params": {"name": f"{tid}.txt"}}
        doc["artifacts"] = [f"{tid}.txt"]
    tasks._update(layout, tid, fn)


@pytest.fixture(autouse=True)
def fake_claude(monkeypatch):
    monkeypatch.setitem(REGISTRY, "claude_cli", FakeReviewClaude())
    yield
    for k in [k for k in os.environ if k.startswith("FAKE_REVIEW_VERDICT")]:
        os.environ.pop(k, None)


def run_until(layout, predicate, timeout=20):
    sup = Supervisor(layout, sleep_s=0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        sup.once()
        if predicate():
            return sup
        time.sleep(0.1)
    raise AssertionError("condition never became true")


def test_pending_skips_roles_with_a_live_review_or_in_flight_job(layout):
    review_task(layout, "T")
    tasks.set_status(layout, "T", "IN_PROGRESS")
    tasks.record_delivery(layout, "T", "d" * 64, actor="test", paths=["T.txt"])
    tasks.record_verification(layout, "T", "d" * 64, True, detail="ok")
    tasks.advance(layout, "T")   # -> WAITING_FOR_REVIEW (no review yet)
    assert review.pending(layout) == [(tasks.load(layout, "T"), "code_review")]
    tasks.record_review(layout, "T", "d" * 64, "code_review", "ACCEPT", vendor="claude")
    assert review.pending(layout) == []


def test_lane_for_falls_back_same_vendor_on_capacity_deferral(layout, monkeypatch):
    import yaml
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({"roles": {"code_review": {
        "adapter": "codex_cli", "fallback": {"adapter": "claude_cli", "model": "opus", "flag": "same_vendor"}}}}))

    def deny(layout_, engine):
        if engine == "codex":
            raise CapacityDeferred(0.0, "out")
    monkeypatch.setattr("supervisor.capacity.require", deny)
    adapter_name, overrides, same_vendor = review.lane_for(layout, "code_review")
    assert adapter_name == "claude_cli" and overrides.get("model") == "opus" and same_vendor is True


def test_full_cycle_implement_verify_review_complete(layout):
    review_task(layout, "T")
    os.environ["FAKE_REVIEW_VERDICT"] = "ACCEPT"
    run_until(layout, lambda: tasks.load(layout, "T").status == TaskStatus.COMPLETE)
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.COMPLETE
    assert len(t.delivery.reviews) == 1 and t.delivery.reviews[0].verdict.value == "ACCEPT"
    assert t.delivery.reviews[0].vendor == "claude"


def test_revise_verdict_dispatches_an_automatic_revision(layout):
    review_task(layout, "T")
    os.environ["FAKE_REVIEW_VERDICT"] = "REVISE"
    run_until(layout, lambda: len(jobs_mod.for_task(layout, "T")) >= 3, timeout=30)
    # a1 delivery, a2 review (REVISE), a3 the automatic revision re-dispatch
    recs = jobs_mod.for_task(layout, "T")
    assert [r.get("spec", {}).get("kind", "delivery") for r in recs[:2]] == ["delivery", "review"]
    assert any(r.get("spec", {}).get("kind", "delivery") == "delivery" and r["attempt_id"] != recs[0]["attempt_id"] for r in recs)


def test_frozen_input_change_invalidates_an_in_flight_review(layout):
    review_task(layout, "T")
    os.environ["FAKE_REVIEW_VERDICT"] = "ACCEPT"
    sup = Supervisor(layout, sleep_s=0)
    sup.once()                                                     # delivery a1 dispatched + settled synchronously
    assert tasks.load(layout, "T").status == TaskStatus.WAITING_FOR_REVIEW
    sup.dispatch_reviews()                                          # review a2 in flight, frozen on digest D1
    rec = jobs_mod.for_task(layout, "T")[-1]
    stale_digest = rec["review_digest"]
    # a brand-new delivery lands before the review is collected (digest moves to D2)
    tasks.record_delivery(layout, "T", "f" * 64, actor="test", paths=["T.txt"])
    assert tasks.load(layout, "T").delivery.digest == "f" * 64
    deadline = time.time() + 20
    while time.time() < deadline and jobs_mod.read(layout, rec["attempt_id"])["phase"] != "DONE":
        sup.once(); time.sleep(0.1)
    t = tasks.load(layout, "T")
    assert not any(r.digest == stale_digest for r in t.delivery.reviews)   # discarded, never recorded


def test_redispatch_after_invalidation_refreezes_drifted_bytes_under_the_stale_digest(layout):
    """a redispatch after invalidation freezes whatever is on disk *now* as the new
    baseline. If that baseline never matched the delivery digest in the first place (bytes drifted before the
    redispatch, not during it), the old check — comparing the baseline only against itself — could never catch
    it; a verdict on stale bytes would be recorded under the original digest."""
    review_task(layout, "T")
    os.environ["FAKE_REVIEW_VERDICT"] = "ACCEPT"
    sup = Supervisor(layout, sleep_s=0)
    sup.once()                                                     # delivery a1, digest D1
    digest = tasks.load(layout, "T").delivery.digest
    sup.dispatch_reviews()                                          # review a2 in flight, frozen on the real bytes
    a2 = jobs_mod.for_task(layout, "T")[-1]["attempt_id"]
    (layout.root / "T.txt").write_text("drifted while a2 was in flight, no new delivery")
    deadline = time.time() + 20
    while time.time() < deadline and jobs_mod.read(layout, a2)["phase"] != "DONE":
        sup.once(); time.sleep(0.1)
    assert jobs_mod.read(layout, a2).get("invalidated") is True
    assert tasks.load(layout, "T").delivery.reviews == []

    deadline = time.time() + 20                                     # a3: redispatched, freezes the DRIFTED bytes
    while time.time() < deadline and len(jobs_mod.for_task(layout, "T")) < 3:
        sup.once(); time.sleep(0.1)
    a3 = jobs_mod.for_task(layout, "T")[-1]["attempt_id"]
    assert jobs_mod.read(layout, a3)["review_digest"] == digest    # still the original, now-stale digest
    deadline = time.time() + 20
    while time.time() < deadline and jobs_mod.read(layout, a3)["phase"] != "DONE":
        sup.once(); time.sleep(0.1)
    t = tasks.load(layout, "T")
    assert t.delivery.reviews == []                                 # a3's own baseline never matched D1 either
    assert jobs_mod.read(layout, a3).get("invalidated") is True
    assert t.status == TaskStatus.BLOCKED                           # MAX_INVALIDATIONS (2) reached


def test_two_reviewers_revising_the_same_digest_cost_one_revision_not_two(layout):
    """two required reviewers both REVISE the same digest. Escalating per-reviewer
    (instead of once per digest round) used to charge two revisions for one failed round, so the real second
    (senior-model) attempt the budget was meant to buy never ran."""
    review_task(layout, "T", reviewer=("chief_architect", "code_review"))
    os.environ["FAKE_REVIEW_VERDICT"] = "REVISE"
    sup = Supervisor(layout, sleep_s=0)
    sup.once()                                                     # a1 delivery
    sup.dispatch_reviews()                                          # a2 + a3, both required roles, in flight together
    recs = jobs_mod.for_task(layout, "T")
    assert len(recs) == 3
    assert {r.get("review_role") for r in recs[1:]} == {"chief_architect", "code_review"}
    deadline = time.time() + 20
    while time.time() < deadline and any(jobs_mod.read(layout, r["attempt_id"])["phase"] != "DONE" for r in recs[1:]):
        sup.once(); time.sleep(0.1)
    deadline = time.time() + 20                                     # the automatic revision, once dispatched
    while time.time() < deadline and len(jobs_mod.for_task(layout, "T")) < 4:
        sup.once(); time.sleep(0.1)
    all_recs = jobs_mod.for_task(layout, "T")
    deliveries = [r for r in all_recs if (r.get("spec") or {}).get("kind", "delivery") == "delivery"]
    assert len(deliveries) == 2                                     # a1 + exactly one revision, not two
    assert escalation._load(layout, "T")["revisions"] == 1
    assert tasks.load(layout, "T").status != TaskStatus.BLOCKED     # budget not exhausted by a single round


def test_accept_with_a_blocking_finding_is_rejected_not_recorded(layout):
    """a schema-correct but self-contradictory ACCEPT (carrying a blocking finding) must
    never be recorded as a verdict — contract.review_result()'s cross-field rule existed but neither adapter
    enforced it on a reviewer's own answer."""
    review_task(layout, "T")
    os.environ["FAKE_REVIEW_VERDICT"] = "ACCEPT_INVALID"
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    sup.dispatch_reviews()
    rec = jobs_mod.for_task(layout, "T")[-1]
    deadline = time.time() + 20
    while time.time() < deadline and jobs_mod.read(layout, rec["attempt_id"])["phase"] != "DONE":
        sup.once(); time.sleep(0.1)
    t = tasks.load(layout, "T")
    assert t.delivery.reviews == []                                 # never recorded, despite the model's "ACCEPT"
    assert jobs_mod.read(layout, rec["attempt_id"])["result"] == "FAILED"


@pyad.job("missing_artifact")
def _missing_artifact(root, run_dir, **_):
    return {"status": "COMPLETE", "artifacts": ["missing.txt"], "notes": "claims complete; never wrote it"}


def test_mixed_round_with_accept_landing_last_still_escalates_once(layout):
    """ingest() used to return before the round-completion check whenever the
    verdict it had just ingested was ACCEPT, so a round whose last-landing verdict is ACCEPT (after an earlier
    required reviewer said REVISE) silently dropped the revision: no record_failure, no revision brief, no issue,
    no task note — the task sat IN_PROGRESS with nothing reported and nothing left to re-queue it."""
    review_task(layout, "T", reviewer=("code_review", "chief_architect"))
    os.environ["FAKE_REVIEW_VERDICT_CODE_REVIEW"] = "REVISE"
    os.environ["FAKE_REVIEW_VERDICT_CHIEF_ARCHITECT"] = "ACCEPT"
    sup = Supervisor(layout, sleep_s=0)
    sup.once()                                                     # a1 delivery
    sup.dispatch_reviews()                                          # a2 code_review, a3 chief_architect, together
    recs = jobs_mod.for_task(layout, "T")
    assert len(recs) == 3
    deadline = time.time() + 20
    while time.time() < deadline and any(jobs_mod.read(layout, r["attempt_id"])["phase"] != "DONE" for r in recs[1:]):
        sup.once(); time.sleep(0.1)
    deadline = time.time() + 20                                     # the automatic revision, once dispatched
    while time.time() < deadline and len(jobs_mod.for_task(layout, "T")) < 4:
        sup.once(); time.sleep(0.1)
    all_recs = jobs_mod.for_task(layout, "T")
    deliveries = [r for r in all_recs if (r.get("spec") or {}).get("kind", "delivery") == "delivery"]
    assert len(deliveries) == 2                                     # a1 + exactly one revision, not zero
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.IN_PROGRESS                       # not stranded with a stale WAITING_FOR_REVIEW


def test_first_listed_reviewer_revising_does_not_strand_the_round(layout):
    """_evaluate used to short-circuit on the first-listed role's
    non-ACCEPT verdict and move the task straight to IN_PROGRESS the instant that role reported, even though a
    later-listed required role's review had not been dispatched yet — pending() only dispatches for
    WAITING_FOR_REVIEW tasks, so the round could never complete at all."""
    review_task(layout, "T", reviewer=("code_review", "chief_architect"))
    os.environ["FAKE_REVIEW_VERDICT"] = "REVISE"
    sup = Supervisor(layout, sleep_s=0)
    sup.once()                                                     # a1 delivery
    assert tasks.load(layout, "T").status == TaskStatus.WAITING_FOR_REVIEW
    review.dispatch(sup, tasks.load(layout, "T"), "code_review")    # dispatch only the first-listed role
    a2 = jobs_mod.for_task(layout, "T")[-1]["attempt_id"]
    deadline = time.time() + 20                                     # settle only: never call dispatch_reviews()
    while time.time() < deadline and jobs_mod.read(layout, a2)["phase"] != "DONE":   # here, or chief_architect
        sup.settle(); time.sleep(0.1)                               # would get auto-dispatched too (masking the bug)
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.WAITING_FOR_REVIEW                # still waiting on chief_architect, not stranded
    assert review.pending(layout) == [(t, "chief_architect")]


def test_blocked_review_redispatches_the_role_instead_of_recording_a_verdict(layout):
    """BLOCKED is an interruption, not a judgement on the work (continuity
    protocol). Recording it as a ReviewRecord is a one-way door — pending() never re-dispatches a role with a
    live review for the digest — so the required verdict could never be obtained again by automation."""
    review_task(layout, "T")
    os.environ["FAKE_REVIEW_VERDICT"] = "BLOCKED"
    sup = Supervisor(layout, sleep_s=0)
    sup.once()                                                     # a1 delivery
    # dispatch + settle only, never sup.once()/dispatch_reviews() here: once() would re-dispatch code_review the
    # instant a2 settles, leaving it permanently "in flight" and masking what pending() reports in between.
    review.dispatch(sup, tasks.load(layout, "T"), "code_review")    # a2
    a2 = jobs_mod.for_task(layout, "T")[-1]["attempt_id"]
    deadline = time.time() + 20
    while time.time() < deadline and jobs_mod.read(layout, a2)["phase"] != "DONE":
        sup.settle(); time.sleep(0.1)
    t = tasks.load(layout, "T")
    assert t.delivery.reviews == []                                 # never recorded as a verdict
    assert t.status == TaskStatus.WAITING_FOR_REVIEW                # not permanently blocked after one interruption
    assert review.pending(layout) == [(t, "code_review")]           # re-dispatchable

    for _ in range(review.MAX_INVALIDATIONS - 1):                   # exhaust the remaining budget: repeated BLOCKED
        review.dispatch(sup, tasks.load(layout, "T"), "code_review")
        attempt = jobs_mod.for_task(layout, "T")[-1]["attempt_id"]
        deadline = time.time() + 20
        while time.time() < deadline and jobs_mod.read(layout, attempt)["phase"] != "DONE":
            sup.settle(); time.sleep(0.1)
    assert tasks.load(layout, "T").status == TaskStatus.BLOCKED     # cap reached: now it is left for the owner


def test_blocked_review_budget_is_not_double_charged_by_a_crash_replay(layout):
    """loop.ingest() calls review.ingest() (which may set blocked_review=True on the
    attempt) and only marks that same attempt DONE *after* review.ingest() returns (supervisor/loop.py ingest). A
    supervisor crash in that window, replayed against the same persisted record, must not count the one real
    interruption as two against MAX_INVALIDATIONS — or the role is never actually redispatched."""
    review_task(layout, "T")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()                                                     # a1 delivery -> WAITING_FOR_REVIEW
    digest = tasks.load(layout, "T").delivery.digest
    attempt_id = "T.a2"
    jobs_mod.prepare(layout, "T", attempt_id, "claude_cli", {"kind": "review"},
                     review_role="code_review", review_digest=digest)
    jobs_mod.set_phase(layout, attempt_id, "RUNNING")               # in flight, same as a real review job
    rec = jobs_mod.read(layout, attempt_id)
    result = Result(status="BLOCKED", raw={"verdict": "BLOCKED", "summary": "cut off mid-review"})

    review.ingest(sup, rec, result)                                 # first ingestion; crash before loop marks DONE
    assert tasks.load(layout, "T").status == TaskStatus.WAITING_FOR_REVIEW
    rec = jobs_mod.read(layout, attempt_id)                          # the persisted record a replay would reload
    assert rec.get("blocked_review") is True and rec["phase"] != "DONE"   # not advanced to DONE by review.ingest itself

    review.ingest(sup, rec, result)                                 # replay: same attempt, same result, after "crash"
    t = tasks.load(layout, "T")
    assert t.status == TaskStatus.WAITING_FOR_REVIEW                # one real interruption, not two: budget intact
    jobs_mod.set_phase(layout, attempt_id, "DONE", result="BLOCKED")  # loop.ingest() finally closes the attempt out
    assert review.pending(layout) == [(t, "code_review")]           # still redispatchable, not stranded BLOCKED


def test_verification_failure_dispatches_a_revision_despite_its_own_attempt_still_being_open(layout):
    """loop.ingest() calls escalation.record_failure()
    before marking the failing delivery DONE, so the failing attempt is still 'open' by jobs.open_records() at
    that moment. dispatch_revision's own-attempt exclusion must still let the revision through end to end
    through Supervisor.ingest, not just through the FakeSupervisor unit tests in test_escalation.py."""
    make_task(layout, "T")
    def fn(doc):
        doc["job"] = {"adapter": "python", "fn": "missing_artifact"}
        doc["tests"] = []
    tasks._update(layout, "T", fn)
    sup = Supervisor(layout, sleep_s=0)
    deadline = time.time() + 20
    while time.time() < deadline and len(jobs_mod.for_task(layout, "T")) < 2:
        sup.once(); time.sleep(0.1)
    t = tasks.load(layout, "T")
    # the revision really was dispatched (not silently skipped as "attempt already open"): the task moved back
    # to IN_PROGRESS for the new attempt instead of sitting FAILED with nothing queued
    assert "revision after verification failure" in t.attempts[-1].note
    assert t.status == TaskStatus.IN_PROGRESS


# ------------------------------------------------------------------ review 1.10.0 round 4: one input for verdict and report

def _review_job(layout, sup, attempt_id, role, digest):
    run_dir = layout.runs / "T" / attempt_id
    run_dir.mkdir(parents=True, exist_ok=True)
    jobs_mod.prepare(layout, "T", attempt_id, "claude_cli", {"kind": "review", "run_dir": str(run_dir)},
                     review_role=role, review_digest=digest)
    jobs_mod.set_phase(layout, attempt_id, "RUNNING")
    return jobs_mod.read(layout, attempt_id), run_dir


def test_the_recorded_report_is_the_jobs_own_result_not_a_later_file(layout):
    """Review 1.10.0 round 4: result.json replaced between the job's end and ingestion. The verdict and the report
    that speaks for it both come from the collected result; the replacement is never recorded as the rationale."""
    import json
    review_task(layout, "T")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    digest = tasks.load(layout, "T").delivery.digest
    rec, run_dir = _review_job(layout, sup, "T.a2", "code_review", digest)
    original = {"verdict": "ACCEPT", "summary": "ORIGINAL summary", "findings": []}
    (run_dir / "result.json").write_text(json.dumps(original), encoding="utf-8")
    (run_dir / "result.json").write_text(json.dumps({"verdict": "REJECT", "summary": "WINDOW FORGED"}), encoding="utf-8")
    review.ingest(sup, rec, Result(status="COMPLETE", raw=original))
    [r] = tasks.load(layout, "T").delivery.reviews
    assert r.verdict.value == "ACCEPT" and r.report.endswith("review_report.json")
    doc, state = tasks.review_report(layout, r)
    assert state == "ok" and doc["summary"] == "ORIGINAL summary"
    (layout.root / r.report).write_text(json.dumps({"verdict": "ACCEPT", "summary": "LATER FORGED"}), encoding="utf-8")
    assert tasks.review_report(layout, r) == (None, "changed")


@pytest.mark.parametrize("mode", ["edited", "link", "unhashed"])
def test_escalation_never_quotes_an_unverifiable_earlier_report(layout, monkeypatch, mode):
    """Review 1.10.0 round 4: reviewer A's report changes while B works; when B's verdict completes the round the
    revision brief quotes B verbatim and says A's rationale is unavailable — never the substituted text."""
    import json
    review_task(layout, "T", reviewer=("code_review", "senior_pm"))
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    digest = tasks.load(layout, "T").delivery.digest
    rec_a, _ = _review_job(layout, sup, "T.a2", "code_review", digest)
    a = {"verdict": "REVISE", "summary": "s", "findings": [{"category": "correctness", "blocking": True,
                                                             "message": "A REAL finding", "evidence": "x:1"}]}
    review.ingest(sup, rec_a, Result(status="COMPLETE", raw=a))
    jobs_mod.set_phase(layout, "T.a2", "DONE", result="COMPLETE")
    [ra] = tasks.load(layout, "T").delivery.reviews
    report = layout.root / ra.report
    forged = json.dumps({"verdict": "REVISE", "findings": [{"category": "x", "message": "FORGED finding",
                                                            "evidence": "y"}]})
    if mode == "edited":
        report.write_text(forged, encoding="utf-8")
    elif mode == "link":
        (layout.root / "forged.json").write_text(forged, encoding="utf-8")
        report.unlink()
        try:
            os.symlink(layout.root / "forged.json", report)
        except (OSError, AttributeError):
            pytest.skip("creating symlinks needs privileges here")
    else:
        report.write_text(forged, encoding="utf-8")
        def drop(doc):
            for r in doc["delivery"]["reviews"]:
                r.pop("report_sha256", None)
        tasks._update(layout, "T", drop)
    seen = []
    monkeypatch.setattr(escalation, "record_failure", lambda sup_, tid, kind, text, **kw: seen.append(text))
    rec_b, _ = _review_job(layout, sup, "T.a3", "senior_pm", digest)
    b = {"verdict": "REVISE", "summary": "s", "findings": [{"category": "quality", "blocking": True,
                                                             "message": "B REAL finding", "evidence": "z:2"}]}
    review.ingest(sup, rec_b, Result(status="COMPLETE", raw=b))
    [text] = seen
    assert "B REAL finding" in text and "FORGED" not in text and "A REAL finding" not in text
    assert "code_review (REVISE): verdict REVISE; rationale unavailable" in text


def test_a_queued_cli_review_is_bound_to_the_report_as_submitted(layout):
    """Review 1.10.0 round 4: the CLI hashes the report when the command is submitted; a replacement before the
    loop applies it shows as changed, not as the reviewer's text."""
    import json
    from supervisor import commands
    review_task(layout, "T")
    sup = Supervisor(layout, sleep_s=0)
    sup.once()
    digest = tasks.load(layout, "T").delivery.digest
    (layout.root / "manual.json").write_text(json.dumps({"verdict": "ACCEPT", "summary": "AS SUBMITTED"}),
                                             encoding="utf-8")
    sha = tasks.report_file_sha256(layout, "manual.json")
    commands.submit(layout, "task.review", {"task_id": "T", "digest": digest, "role": "code_review",
                                            "verdict": "ACCEPT", "report": "manual.json", "report_sha256": sha},
                    actor="review")
    (layout.root / "manual.json").write_text(json.dumps({"verdict": "ACCEPT", "summary": "SWAPPED"}), encoding="utf-8")
    commands.apply_all(layout)
    [r] = tasks.load(layout, "T").delivery.reviews
    assert r.report_sha256 == sha and tasks.review_report(layout, r) == (None, "changed")
