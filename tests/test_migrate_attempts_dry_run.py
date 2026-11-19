"""The attempt-identity migration's dry run (ATTEMPT_MODEL.md section 8, step 6 of "Order of work"): derives
`mapping.json` and `report.json` from the fields a store carries today, never writes to the store itself, takes an
`overrides.yaml` as an input, and can be compared against an earlier run.

The fixture store below holds one task per shape section 8.2 names: a continuation (`T-CONT`), a reroute
(`T-REROUTE`), a revision (`T-CONT` and `T-AMBIG`), a re-verification of unchanged bytes (`T-REVERIFY`), an
ambiguous digest (`T-AMBIG`), a review record carrying a continuation note (`T-REVCONT`), a diagnosis before any
delivery (`T-DIAGFIRST`), a stall event (`T-STALL`) and a lost record (`T-LOST`, a review whose linking verify
record never existed). `T-DIAG` is an ordinary diagnosis of a failed delivery, kept alongside them as a sanity
check. Every job record is written directly (as `tests/test_attempt_readers_prefer_fields.py` already does for the
same store), so the fixture is the shape a real, un-migrated store carries, not a replay of the dispatch path."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from supervisor import jobs, migrations
from supervisor.cli import app

from tests.conftest import make_task
from tests._cli_help_width import clean, invoke_help

runner = CliRunner()


def _job(layout, job_id, task_id, kind, created, **fields):
    rec = {"attempt_id": job_id, "task_id": task_id, "adapter": fields.pop("adapter", "claude_cli"),
          "phase": fields.pop("phase", "DONE"), "spec": {"kind": kind}, "handle": None, "created": created}
    rec.update(fields)
    return jobs.write(layout, rec)


def _attempt(job_id, note, resource="claude_cli", at=""):
    return {"id": job_id, "resource": resource, "at": at, "note": note, "outcome": ""}


def _escalate(layout, task_id, events):
    p = layout.runtime / "escalation" / f"{task_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"revisions": 0, "signatures": [], "events": events}), encoding="utf-8")


T = "2026-01-{:02d}T00:00:00+00:00"


def build_fixture(layout):
    # T-CONT: a continuation, a revision, and the imprecise reroute recorded as a plain dispatch (flagged).
    make_task(layout, "T-CONT", attempts=[
        _attempt("T-CONT.a1", "dispatch T-CONT.a1", at=T.format(1)),
        _attempt("T-CONT.a2", "revision after worker_failed failure T-CONT.a2", at=T.format(2)),
        _attempt("T-CONT.a3", "continuation after capacity limit T-CONT.a3", at=T.format(3)),
        _attempt("T-CONT.a4", "dispatch T-CONT.a4", at=T.format(4)),
        _attempt("T-CONT.a5", "verify delivery T-CONT.a4 T-CONT.a5", at=T.format(5)),
    ], history=[{"at": T.format(3) + "01", "by": "owner", "from": "FAILED", "to": "READY",
                "note": "rerouted to codex_cli after capacity limit"}])
    _job(layout, "T-CONT.a1", "T-CONT", "delivery", T.format(1), adapter="claude_cli", phase="FAILED")
    _job(layout, "T-CONT.a2", "T-CONT", "delivery", T.format(2), adapter="claude_cli", phase="DEFERRED")
    _job(layout, "T-CONT.a3", "T-CONT", "delivery", T.format(3), adapter="claude_cli", phase="FAILED",
        deferred_reason="continuation after capacity limit")
    _job(layout, "T-CONT.a4", "T-CONT", "delivery", T.format(4), adapter="codex_cli", phase="DONE")
    _job(layout, "T-CONT.a5", "T-CONT", "verify", T.format(5), adapter="process", verify_delivery="T-CONT.a4",
        verify_digest="d" * 64)

    # T-REROUTE: a structured reroute (`rerouted_from` set) -- no new attempt, next session.
    make_task(layout, "T-REROUTE", attempts=[
        _attempt("T-REROUTE.a1", "dispatch:engineer T-REROUTE.a1", at=T.format(1)),
        _attempt("T-REROUTE.a2", "rerouted from claude_cli T-REROUTE.a2", at=T.format(2)),
        _attempt("T-REROUTE.a3", "verify delivery T-REROUTE.a2 T-REROUTE.a3", at=T.format(3)),
    ])
    _job(layout, "T-REROUTE.a1", "T-REROUTE", "delivery", T.format(1), adapter="claude_cli", phase="FAILED")
    _job(layout, "T-REROUTE.a2", "T-REROUTE", "delivery", T.format(2), adapter="codex_cli", phase="DONE",
        rerouted_from="T-REROUTE.a1")
    _job(layout, "T-REROUTE.a3", "T-REROUTE", "verify", T.format(3), adapter="process",
        verify_delivery="T-REROUTE.a2", verify_digest="e" * 64)

    # T-REVERIFY: a re-verification of unchanged bytes -- the same digest, the same delivery, twice.
    make_task(layout, "T-REVERIFY", attempts=[
        _attempt("T-REVERIFY.a1", "dispatch T-REVERIFY.a1", at=T.format(1)),
        _attempt("T-REVERIFY.a2", "verify delivery T-REVERIFY.a1 T-REVERIFY.a2", at=T.format(2)),
        _attempt("T-REVERIFY.a3", "verify delivery T-REVERIFY.a1 T-REVERIFY.a3", at=T.format(3)),
    ])
    _job(layout, "T-REVERIFY.a1", "T-REVERIFY", "delivery", T.format(1), adapter="claude_cli", phase="DONE")
    _job(layout, "T-REVERIFY.a2", "T-REVERIFY", "verify", T.format(2), adapter="process",
        verify_delivery="T-REVERIFY.a1", verify_digest="f" * 64)
    _job(layout, "T-REVERIFY.a3", "T-REVERIFY", "verify", T.format(3), adapter="process",
        verify_delivery="T-REVERIFY.a1", verify_digest="f" * 64)

    # T-AMBIG: two different deliveries produce the same bytes; both get verified; the review's digest is ambiguous.
    make_task(layout, "T-AMBIG", attempts=[
        _attempt("T-AMBIG.a1", "dispatch T-AMBIG.a1", at=T.format(1)),
        _attempt("T-AMBIG.a2", "verify delivery T-AMBIG.a1 T-AMBIG.a2", at=T.format(2)),
        _attempt("T-AMBIG.a3", "revision after worker_failed failure T-AMBIG.a3", at=T.format(3)),
        _attempt("T-AMBIG.a4", "verify delivery T-AMBIG.a3 T-AMBIG.a4", at=T.format(4)),
        _attempt("T-AMBIG.a5", "review T-AMBIG.a5", at=T.format(5)),
    ])
    _job(layout, "T-AMBIG.a1", "T-AMBIG", "delivery", T.format(1), adapter="claude_cli", phase="DONE")
    _job(layout, "T-AMBIG.a2", "T-AMBIG", "verify", T.format(2), adapter="process",
        verify_delivery="T-AMBIG.a1", verify_digest="a" * 64)
    _job(layout, "T-AMBIG.a3", "T-AMBIG", "delivery", T.format(3), adapter="claude_cli", phase="DONE")
    _job(layout, "T-AMBIG.a4", "T-AMBIG", "verify", T.format(4), adapter="process",
        verify_delivery="T-AMBIG.a3", verify_digest="a" * 64)
    _job(layout, "T-AMBIG.a5", "T-AMBIG", "review", T.format(5), adapter="claude_cli", review_digest="a" * 64)

    # T-LOST: a review whose linking verify record was lost -- placed by the nearest-preceding-delivery fallback.
    make_task(layout, "T-LOST", attempts=[
        _attempt("T-LOST.a1", "dispatch T-LOST.a1", at=T.format(1)),
        _attempt("T-LOST.a2", "review T-LOST.a2", at=T.format(2)),
    ])
    _job(layout, "T-LOST.a1", "T-LOST", "delivery", T.format(1), adapter="claude_cli", phase="DONE")
    _job(layout, "T-LOST.a2", "T-LOST", "review", T.format(2), adapter="claude_cli", review_digest="b" * 64)

    # T-REVCONT: a review re-dispatched after a capacity limit -- a further round of the same attempt, not a new one.
    make_task(layout, "T-REVCONT", attempts=[
        _attempt("T-REVCONT.a1", "dispatch T-REVCONT.a1", at=T.format(1)),
        _attempt("T-REVCONT.a2", "verify delivery T-REVCONT.a1 T-REVCONT.a2", at=T.format(2)),
        _attempt("T-REVCONT.a3", "review T-REVCONT.a3", at=T.format(3)),
        _attempt("T-REVCONT.a4", "continuation after capacity limit T-REVCONT.a4", at=T.format(4)),
    ])
    _job(layout, "T-REVCONT.a1", "T-REVCONT", "delivery", T.format(1), adapter="claude_cli", phase="DONE")
    _job(layout, "T-REVCONT.a2", "T-REVCONT", "verify", T.format(2), adapter="process",
        verify_delivery="T-REVCONT.a1", verify_digest="c" * 64)
    _job(layout, "T-REVCONT.a3", "T-REVCONT", "review", T.format(3), adapter="claude_cli", review_digest="c" * 64)
    _job(layout, "T-REVCONT.a4", "T-REVCONT", "review", T.format(4), adapter="claude_cli", review_digest="c" * 64)

    # T-DIAGFIRST: a stall on a task never dispatched -- task-scoped, no attempt to attach to.
    make_task(layout, "T-DIAGFIRST", attempts=[_attempt("T-DIAGFIRST.a1", "diagnosis:senior_pm T-DIAGFIRST.a1",
                                                        at="2026-02-01T00:05:00+00:00")])
    _escalate(layout, "T-DIAGFIRST",
             {"stall:2026-02-01T00:00:00+00:00": {"signature": "SIGFIRST", "at": "2026-02-01T00:00:00+00:00"}})
    _job(layout, "T-DIAGFIRST.a1", "T-DIAGFIRST", "diagnosis", "2026-02-01T00:05:00+00:00", adapter="claude_cli",
        diag_signature="SIGFIRST", diag_source="stalled")

    # T-STALL: a stall with a delivery already open -- attaches to the newest attempt open at that time.
    make_task(layout, "T-STALL", attempts=[
        _attempt("T-STALL.a1", "dispatch T-STALL.a1", at="2026-02-10T23:00:00+00:00"),
        _attempt("T-STALL.a2", "diagnosis:senior_pm T-STALL.a2", at="2026-02-11T00:10:00+00:00"),
    ])
    _job(layout, "T-STALL.a1", "T-STALL", "delivery", "2026-02-10T23:00:00+00:00", adapter="human", phase="DONE")
    _escalate(layout, "T-STALL",
             {"stall:2026-02-11T00:00:00+00:00": {"signature": "SIGSTALL", "at": "2026-02-11T00:00:00+00:00"}})
    _job(layout, "T-STALL.a2", "T-STALL", "diagnosis", "2026-02-11T00:10:00+00:00", adapter="claude_cli",
        diag_signature="SIGSTALL", diag_source="stalled")

    # T-DIAG: an ordinary diagnosis of a failed delivery, keyed by the failing job's own id.
    make_task(layout, "T-DIAG", attempts=[
        _attempt("T-DIAG.a1", "dispatch T-DIAG.a1", at="2026-02-20T00:00:00+00:00"),
        _attempt("T-DIAG.a2", "diagnosis:senior_pm T-DIAG.a2", at="2026-02-20T00:02:00+00:00"),
    ])
    _job(layout, "T-DIAG.a1", "T-DIAG", "delivery", "2026-02-20T00:00:00+00:00", adapter="claude_cli", phase="FAILED")
    _escalate(layout, "T-DIAG", {"T-DIAG.a1": {"signature": "SIGDIAG", "at": "2026-02-20T00:01:00+00:00"}})
    _job(layout, "T-DIAG.a2", "T-DIAG", "diagnosis", "2026-02-20T00:02:00+00:00", adapter="claude_cli",
        diag_signature="SIGDIAG", diag_source="worker_failed")


EXPECTED = {
    "T-CONT.a1": (1, "delivery", 1),
    "T-CONT.a2": (2, "delivery", 1),
    "T-CONT.a3": (2, "delivery", 2),
    "T-CONT.a4": (3, "delivery", 1),
    "T-CONT.a5": (3, "verify", 1),
    "T-REROUTE.a1": (1, "delivery", 1),
    "T-REROUTE.a2": (1, "delivery", 2),
    "T-REROUTE.a3": (1, "verify", 1),
    "T-REVERIFY.a1": (1, "delivery", 1),
    "T-REVERIFY.a2": (1, "verify", 1),
    "T-REVERIFY.a3": (1, "verify", 2),
    "T-AMBIG.a1": (1, "delivery", 1),
    "T-AMBIG.a2": (1, "verify", 1),
    "T-AMBIG.a3": (2, "delivery", 1),
    "T-AMBIG.a4": (2, "verify", 1),
    "T-AMBIG.a5": (2, "review", 1),            # the ambiguous digest resolves to the LATER delivery (attempt 2)
    "T-LOST.a1": (1, "delivery", 1),
    "T-LOST.a2": (1, "review", 1),              # placed by the nearest-preceding-delivery fallback
    "T-REVCONT.a1": (1, "delivery", 1),
    "T-REVCONT.a2": (1, "verify", 1),
    "T-REVCONT.a3": (1, "review", 1),
    "T-REVCONT.a4": (1, "review", 2),           # a further round of attempt 1, not a new attempt
    "T-DIAGFIRST.a1": (None, "diagnosis", 1),   # task-scoped
    "T-STALL.a1": (1, "delivery", 1),
    "T-STALL.a2": (1, "diagnosis", 1),
    "T-DIAG.a1": (1, "delivery", 1),
    "T-DIAG.a2": (1, "diagnosis", 1),
}


def test_the_dry_run_places_every_shape_with_zero_unplaced(layout, tmp_path):
    build_fixture(layout)
    out = tmp_path / "out1"
    result = migrations.dry_run_attempts(layout, out)
    mapping = json.loads((out / "mapping.json").read_text(encoding="utf-8"))

    assert result["totals"]["unplaced"] == 0
    assert set(mapping) == set(EXPECTED)
    for job_id, (attempt, kind, seq) in EXPECTED.items():
        entry = mapping[job_id]
        assert (entry["attempt"], entry["kind"], entry["seq"]) == (attempt, kind, seq), (job_id, entry)

    flagged = {f["job"] for f in json.loads((out / "report.json").read_text(encoding="utf-8"))["flags"]}
    assert flagged == {"T-CONT.a4"}


def test_a_second_run_is_byte_identical(layout, tmp_path):
    build_fixture(layout)
    out1, out2 = tmp_path / "out1", tmp_path / "out2"
    migrations.dry_run_attempts(layout, out1)
    migrations.dry_run_attempts(layout, out2)
    for name in ("mapping.json", "report.json", "summary.md"):
        assert (out1 / name).read_bytes() == (out2 / name).read_bytes(), name


def test_an_override_changes_one_cause_and_renumbers_later_attempts(layout, tmp_path):
    build_fixture(layout)
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(yaml.safe_dump({"T-CONT.a4": "reroute"}), encoding="utf-8")
    out = tmp_path / "out"
    migrations.dry_run_attempts(layout, out, overrides_path=overrides)
    mapping = json.loads((out / "mapping.json").read_text(encoding="utf-8"))

    assert mapping["T-CONT.a4"]["cause"] == "reroute"
    assert (mapping["T-CONT.a4"]["attempt"], mapping["T-CONT.a4"]["seq"]) == (2, 3)
    assert mapping["T-CONT.a5"]["attempt"] == 2                 # the verify follows its delivery's new attempt

    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["flags"] == []                                # the override resolves the flag, not merely masks it
    assert report["overrides"]["sha256"] == __import__("hashlib").sha256(overrides.read_bytes()).hexdigest()


def test_compare_reports_unchanged_and_added_rows(layout, tmp_path):
    build_fixture(layout)
    baseline_dir = tmp_path / "baseline"
    migrations.dry_run_attempts(layout, baseline_dir)

    out = tmp_path / "second"
    result = migrations.dry_run_attempts(layout, out, compare_path=baseline_dir / "mapping.json")
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["compare"]["changed"] == []
    assert report["compare"]["added"] == []
    assert report["compare"]["unchanged"] == len(EXPECTED)
    assert result["compare"]["unchanged"] == len(EXPECTED)

    # a record dispatched since the baseline: reported as added, never as changed
    _job(layout, "T-DIAG.a3", "T-DIAG", "verify", "2026-02-20T00:03:00+00:00", adapter="process",
        verify_delivery="T-DIAG.a1", verify_digest="9" * 64)
    out2 = tmp_path / "third"
    migrations.dry_run_attempts(layout, out2, compare_path=baseline_dir / "mapping.json")
    report2 = json.loads((out2 / "report.json").read_text(encoding="utf-8"))
    assert report2["compare"]["added"] == ["T-DIAG.a3"]
    assert report2["compare"]["changed"] == []


def test_compare_against_a_changed_row_is_an_error(layout, tmp_path):
    build_fixture(layout)
    baseline_dir = tmp_path / "baseline"
    migrations.dry_run_attempts(layout, baseline_dir)

    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(yaml.safe_dump({"T-CONT.a4": "reroute"}), encoding="utf-8")
    with pytest.raises(migrations.MigrationError, match="T-CONT.a4"):
        migrations.dry_run_attempts(layout, tmp_path / "changed", overrides_path=overrides,
                                    compare_path=baseline_dir / "mapping.json")


def test_it_writes_only_inside_out_and_reads_escalation_files_read_only(layout, tmp_path):
    build_fixture(layout)
    esc = layout.runtime / "escalation" / "T-STALL.json"
    before = esc.read_bytes()
    before_mtime = esc.stat().st_mtime_ns

    before_manifest = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in layout.root.rglob("*")
                       if p.is_file() and "out" not in p.parts}
    out = tmp_path / "out"
    migrations.dry_run_attempts(layout, out)
    after_manifest = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in layout.root.rglob("*")
                      if p.is_file() and "out" not in p.parts}
    assert before_manifest == after_manifest, "the dry run wrote inside the project store"

    assert esc.read_bytes() == before
    assert esc.stat().st_mtime_ns == before_mtime

    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    import hashlib
    assert report["escalation_digests"]["T-STALL"] == hashlib.sha256(before).hexdigest()
    assert set(out.iterdir()) == {out / "mapping.json", out / "report.json", out / "summary.md"}


def test_report_and_summary_name_the_resolved_package_file_and_version(layout, tmp_path, monkeypatch):
    build_fixture(layout)
    import supervisor
    monkeypatch.setattr(supervisor, "__version__", "99.0.0-test")
    out = tmp_path / "out"
    migrations.dry_run_attempts(layout, out)
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    summary = (out / "summary.md").read_text(encoding="utf-8")

    assert report["package"]["version"] == "99.0.0-test"
    assert report["package"]["file"] == supervisor.__file__
    assert summary.splitlines()[0] == f"supervisor 99.0.0-test ({supervisor.__file__})"


def test_cli_help_shows_the_four_options_and_the_bare_command_still_works(layout, monkeypatch, tmp_path):
    from supervisor import project as project_mod
    project_mod.write(layout, {"name": "p"})
    monkeypatch.setenv("SUPERVISOR_ROOT", str(layout.root))
    result = invoke_help(runner, app, "migrate", "attempts")
    assert result.exit_code == 0, result.output
    for opt in ("--dry-run", "--out", "--overrides", "--compare"):
        assert opt in clean(result.output), result.output

    bare = runner.invoke(app, ["migrate", "--dry-run"])
    assert bare.exit_code == 0, bare.output

    build_fixture(layout)
    out = tmp_path / "cli-out"
    ran = runner.invoke(app, ["migrate", "attempts", "--dry-run", "--out", str(out)])
    assert ran.exit_code == 0, ran.output
    assert (out / "mapping.json").exists()


@pytest.mark.parametrize("ambient_columns", ["40", None])
def test_cli_help_options_are_present_at_a_narrow_rendered_width_too(layout, monkeypatch, ambient_columns):
    """A narrower rendered width (CI's own no-TTY default among them) truncates typer/rich's Options panel's
    longer option names with an ellipsis before this test's own `in result.output` check ever runs; COLUMNS=40
    reproduces the same truncation on a workstation whose own wide terminal otherwise hides it."""
    from supervisor import project as project_mod
    project_mod.write(layout, {"name": "p"})
    monkeypatch.setenv("SUPERVISOR_ROOT", str(layout.root))
    if ambient_columns is None:
        monkeypatch.delenv("COLUMNS", raising=False)
    else:
        monkeypatch.setenv("COLUMNS", ambient_columns)
    result = invoke_help(runner, app, "migrate", "attempts")
    assert result.exit_code == 0, result.output
    for opt in ("--dry-run", "--out", "--overrides", "--compare"):
        assert opt in clean(result.output), result.output


# ---- dispatch order on an un-migrated store (section 8.2) ----
#
# `jobs.for_task` groups its result by kind, then by id string, for a record carrying none of `jobs.sort_key`'s
# fields (no id-suffix fallback since the writers switch, C-3/T7-08) -- which is exactly the shape of a record the
# migration exists to place. These four cases are the ones no fixture above builds: ten or more records of one
# kind, and a review that falls to the preceding-delivery rule after a later delivery has already been walked.

def _fielded_job(layout, job_id, task_id, spec_kind, created, attempt, kind, seq, **fields):
    rec = {"attempt_id": job_id, "task_id": task_id, "adapter": fields.pop("adapter", "claude_cli"),
          "phase": fields.pop("phase", "DONE"), "spec": {"kind": spec_kind}, "handle": None, "created": created,
          "attempt": attempt, "kind": kind, "seq": seq}
    rec.update(fields)
    return jobs.write(layout, rec)


def test_a_task_with_more_than_nine_bare_deliveries_is_numbered_in_dispatch_order(layout, tmp_path):
    make_task(layout, "T-TEN", attempts=[
        _attempt("T-TEN.a1", "dispatch T-TEN.a1", at=T.format(1)),
        *[_attempt(f"T-TEN.a{i}", f"continuation after capacity limit T-TEN.a{i}", at=T.format(i))
          for i in range(2, 10)],
        _attempt("T-TEN.a10", "revision after worker_failed failure T-TEN.a10", at=T.format(10)),
        _attempt("T-TEN.a11", "continuation after capacity limit T-TEN.a11", at=T.format(11)),
        _attempt("T-TEN.a12", "continuation after capacity limit T-TEN.a12", at=T.format(12)),
    ])
    _job(layout, "T-TEN.a1", "T-TEN", "delivery", T.format(1), adapter="claude_cli", phase="DONE")
    for i in range(2, 10):
        _job(layout, f"T-TEN.a{i}", "T-TEN", "delivery", T.format(i), adapter="claude_cli", phase="FAILED",
            deferred_reason="continuation after capacity limit")
    _job(layout, "T-TEN.a10", "T-TEN", "delivery", T.format(10), adapter="claude_cli", phase="DONE")
    _job(layout, "T-TEN.a11", "T-TEN", "delivery", T.format(11), adapter="claude_cli", phase="FAILED",
        deferred_reason="continuation after capacity limit")
    _job(layout, "T-TEN.a12", "T-TEN", "delivery", T.format(12), adapter="claude_cli", phase="DONE")

    out = tmp_path / "out"
    migrations.dry_run_attempts(layout, out)
    mapping = json.loads((out / "mapping.json").read_text(encoding="utf-8"))

    expected = {"T-TEN.a1": (1, "delivery", 1), **{f"T-TEN.a{i}": (1, "delivery", i) for i in range(2, 10)},
               "T-TEN.a10": (2, "delivery", 1), "T-TEN.a11": (2, "delivery", 2), "T-TEN.a12": (2, "delivery", 3)}
    for job_id, (attempt, kind, seq) in expected.items():
        entry = mapping[job_id]
        assert (entry["attempt"], entry["kind"], entry["seq"]) == (attempt, kind, seq), (job_id, entry)


def test_a_first_round_review_with_no_verify_record_is_parented_on_its_own_preceding_delivery(layout, tmp_path):
    make_task(layout, "T-NOVERIFY2", attempts=[
        _attempt("T-NOVERIFY2.a1", "dispatch T-NOVERIFY2.a1", at=T.format(1)),
        _attempt("T-NOVERIFY2.a2", "review T-NOVERIFY2.a2", at=T.format(2)),
        _attempt("T-NOVERIFY2.a3", "revision after worker_failed failure T-NOVERIFY2.a3", at=T.format(3)),
        _attempt("T-NOVERIFY2.a4", "review T-NOVERIFY2.a4", at=T.format(4)),
    ])
    _job(layout, "T-NOVERIFY2.a1", "T-NOVERIFY2", "delivery", T.format(1), adapter="claude_cli", phase="DONE")
    _job(layout, "T-NOVERIFY2.a2", "T-NOVERIFY2", "review", T.format(2), adapter="claude_cli",
        review_digest="1" * 64)
    _job(layout, "T-NOVERIFY2.a3", "T-NOVERIFY2", "delivery", T.format(3), adapter="claude_cli", phase="DONE")
    _job(layout, "T-NOVERIFY2.a4", "T-NOVERIFY2", "review", T.format(4), adapter="claude_cli",
        review_digest="2" * 64)

    out = tmp_path / "out"
    migrations.dry_run_attempts(layout, out)
    mapping = json.loads((out / "mapping.json").read_text(encoding="utf-8"))

    assert (mapping["T-NOVERIFY2.a2"]["attempt"], mapping["T-NOVERIFY2.a2"]["parent"]) == (1, "T-NOVERIFY2.a1")
    assert (mapping["T-NOVERIFY2.a4"]["attempt"], mapping["T-NOVERIFY2.a4"]["parent"]) == (2, "T-NOVERIFY2.a3")


def test_a_mixed_kind_task_interleaving_delivery_and_review_walks_in_dispatch_order_not_grouped_by_kind(
        layout, tmp_path):
    make_task(layout, "T-INTERLEAVE", attempts=[
        _attempt("T-INTERLEAVE.a1", "dispatch T-INTERLEAVE.a1", at=T.format(1)),
        _attempt("T-INTERLEAVE.a2", "review T-INTERLEAVE.a2", at=T.format(2)),
        _attempt("T-INTERLEAVE.a3", "dispatch T-INTERLEAVE.a3", at=T.format(3)),
        _attempt("T-INTERLEAVE.a4", "review T-INTERLEAVE.a4", at=T.format(4)),
        _attempt("T-INTERLEAVE.a5", "dispatch T-INTERLEAVE.a5", at=T.format(5)),
        _attempt("T-INTERLEAVE.a6", "review T-INTERLEAVE.a6", at=T.format(6)),
    ])
    for i, kind in zip(range(1, 7), ("delivery", "review", "delivery", "review", "delivery", "review")):
        job_id = f"T-INTERLEAVE.a{i}"
        if kind == "delivery":
            _job(layout, job_id, "T-INTERLEAVE", "delivery", T.format(i), adapter="claude_cli", phase="DONE")
        else:
            _job(layout, job_id, "T-INTERLEAVE", "review", T.format(i), adapter="claude_cli",
                review_digest=str(i) * 64)

    out = tmp_path / "out"
    migrations.dry_run_attempts(layout, out)
    mapping = json.loads((out / "mapping.json").read_text(encoding="utf-8"))

    # each delivery opens its own attempt (parent None, the first session); each review is parented on the
    # delivery that immediately precedes it in dispatch order, not on the task's last delivery.
    for job_id, attempt in (("T-INTERLEAVE.a1", 1), ("T-INTERLEAVE.a3", 2), ("T-INTERLEAVE.a5", 3)):
        entry = mapping[job_id]
        assert (entry["attempt"], entry["kind"]) == (attempt, "delivery"), (job_id, entry)
    for job_id, (attempt, parent) in (("T-INTERLEAVE.a2", (1, "T-INTERLEAVE.a1")),
                                      ("T-INTERLEAVE.a4", (2, "T-INTERLEAVE.a3")),
                                      ("T-INTERLEAVE.a6", (3, "T-INTERLEAVE.a5"))):
        entry = mapping[job_id]
        assert (entry["attempt"], entry["kind"], entry["parent"]) == (attempt, "review", parent), (job_id, entry)


def test_a_fully_fielded_store_is_still_walked_in_field_order(layout, tmp_path):
    """The control: a record already carrying `attempt`/`kind`/`seq` (migration-stamped or allocator-written)
    must be walked in the same field order `jobs.sort_key` already gives it, unchanged by this fix -- the ids
    are deliberately out of suffix order (`.a20`, `.a5`, `.a1`) so a fix that sorted by suffix regardless of
    fields would misplace this task too."""
    make_task(layout, "T-FIELDCTRL", attempts=[
        _attempt("T-FIELDCTRL.a20", "dispatch T-FIELDCTRL.a20", at=T.format(1)),
        _attempt("T-FIELDCTRL.a5", "verify delivery T-FIELDCTRL.a20 T-FIELDCTRL.a5", at=T.format(2)),
        _attempt("T-FIELDCTRL.a1", "revision after worker_failed failure T-FIELDCTRL.a1", at=T.format(3)),
    ])
    _fielded_job(layout, "T-FIELDCTRL.a20", "T-FIELDCTRL", "delivery", T.format(1), 1, "delivery", 1)
    _fielded_job(layout, "T-FIELDCTRL.a5", "T-FIELDCTRL", "verify", T.format(2), 1, "verify", 1,
                adapter="process", verify_delivery="T-FIELDCTRL.a20", verify_digest="3" * 64)
    _fielded_job(layout, "T-FIELDCTRL.a1", "T-FIELDCTRL", "delivery", T.format(3), 2, "delivery", 1)

    out = tmp_path / "out"
    migrations.dry_run_attempts(layout, out)
    mapping = json.loads((out / "mapping.json").read_text(encoding="utf-8"))

    assert (mapping["T-FIELDCTRL.a20"]["attempt"], mapping["T-FIELDCTRL.a20"]["seq"]) == (1, 1)
    assert (mapping["T-FIELDCTRL.a5"]["attempt"], mapping["T-FIELDCTRL.a5"]["seq"]) == (1, 1)
    assert (mapping["T-FIELDCTRL.a1"]["attempt"], mapping["T-FIELDCTRL.a1"]["seq"]) == (2, 1)
