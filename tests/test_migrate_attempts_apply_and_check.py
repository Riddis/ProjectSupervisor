"""The attempt-identity migration's apply and check (ATTEMPT_MODEL.md section 8, step 7 of "Order of work"):
apply writes a reviewed `mapping.json`'s identity fields onto the store it was derived from, never derives one
of its own, and `check` re-reads the store afterwards and asserts section 8.5's seven claims against it.

Builds on the dry run's own fixture (`tests/test_migrate_attempts_dry_run.py`) and adds two tasks of its own: one
whose delivery names a run directory that exists on disk at dry-run time, and one whose named run directory does
not -- the two halves of check assertion 2."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from supervisor import jobs, migrations, store
from supervisor import tasks as tasks_mod
from supervisor.cli import app

from tests.conftest import make_task
from tests.test_migrate_attempts_dry_run import EXPECTED, T, _attempt, build_fixture

runner = CliRunner()


def _job(layout, job_id, task_id, kind, created, run_dir=None, **fields):
    spec = {"kind": kind}
    if run_dir is not None:
        spec["run_dir"] = str(run_dir)
    rec = {"attempt_id": job_id, "task_id": task_id, "adapter": fields.pop("adapter", "claude_cli"),
          "phase": fields.pop("phase", "DONE"), "spec": spec, "handle": None, "created": created}
    rec.update(fields)
    return jobs.write(layout, rec)


def build_apply_fixture(layout, tmp_path) -> dict:
    """The dry-run fixture (section 8.2's shapes) plus `T-RUNDIR` (a run directory that exists at dry-run time,
    holding a file never to be touched) and `T-ABSENT` (a run directory that never existed) -- check assertion
    2's two halves."""
    build_fixture(layout)

    run_dir = tmp_path / "run_dirs" / "T-RUNDIR.a1"
    run_dir.mkdir(parents=True)
    (run_dir / "HANDOFF.md").write_text("a run directory's own file, never to be renamed or rewritten",
                                        encoding="utf-8")
    missing_dir = tmp_path / "run_dirs" / "T-ABSENT.a1"

    make_task(layout, "T-RUNDIR", attempts=[
        _attempt("T-RUNDIR.a1", "dispatch T-RUNDIR.a1", at=T.format(1)),
        _attempt("T-RUNDIR.a2", "verify delivery T-RUNDIR.a1 T-RUNDIR.a2", at=T.format(2)),
        _attempt("T-RUNDIR.a3", "review T-RUNDIR.a3", at=T.format(3)),
    ])
    _job(layout, "T-RUNDIR.a1", "T-RUNDIR", "delivery", T.format(1), run_dir=run_dir, phase="DONE")
    _job(layout, "T-RUNDIR.a2", "T-RUNDIR", "verify", T.format(2), adapter="process",
        verify_delivery="T-RUNDIR.a1", verify_digest="7" * 64)
    _job(layout, "T-RUNDIR.a3", "T-RUNDIR", "review", T.format(3), review_digest="7" * 64)

    make_task(layout, "T-ABSENT", attempts=[_attempt("T-ABSENT.a1", "dispatch T-ABSENT.a1", at=T.format(1))])
    _job(layout, "T-ABSENT.a1", "T-ABSENT", "delivery", T.format(1), run_dir=missing_dir, phase="DONE")

    return {"run_dir": run_dir, "missing_dir": missing_dir}


EXTRA_EXPECTED = {
    "T-RUNDIR.a1": (1, "delivery", 1), "T-RUNDIR.a2": (1, "verify", 1), "T-RUNDIR.a3": (1, "review", 1),
    "T-ABSENT.a1": (1, "delivery", 1),
}
ALL_EXPECTED = {**EXPECTED, **EXTRA_EXPECTED}


def _write_expect(mapping_dir: Path) -> None:
    """The committed expectation file (check assertion 6): three named tasks -- a straightforward one
    (`T-RUNDIR`), a continuation that folds a lane change into a flagged attempt (`T-CONT`), and a review round
    re-dispatched after a capacity limit, read as a further round rather than a new attempt (`T-REVCONT`)."""
    expect = {"T-RUNDIR.a1": [1, "delivery", 1], "T-CONT.a1": [1, "delivery", 1], "T-REVCONT.a3": [1, "review", 1]}
    (mapping_dir / "expect.json").write_text(json.dumps(expect, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _dry_run(layout, mapping_dir) -> Path:
    migrations.dry_run_attempts(layout, mapping_dir)
    return mapping_dir / "mapping.json"


# ------------------------------------------------------------------ apply


def test_apply_refuses_without_a_mapping_file(layout, tmp_path):
    with pytest.raises(migrations.MigrationError, match="mapping"):
        migrations.apply_attempts(layout, tmp_path / "no-such-mapping.json", tmp_path / "apply")


def test_apply_writes_exactly_the_mapping_fields_and_stamps_each_record(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_path = _dry_run(layout, tmp_path / "mapping")
    mapping_sha256 = hashlib.sha256(mapping_path.read_bytes()).hexdigest()

    report = migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    assert sorted(report["written"]) == sorted(ALL_EXPECTED)
    assert report["already_migrated"] == []
    assert report["backup"]

    for job_id, (attempt, kind, seq) in ALL_EXPECTED.items():
        rec = jobs.resolve(layout, job_id)
        assert rec.get("attempt") == attempt
        assert rec.get("kind") == kind
        assert rec.get("seq") == seq
        assert rec["migrated"]["attempts"]["mapping_sha256"] == mapping_sha256


def test_apply_is_a_no_op_the_second_time_and_verifies_rather_than_rewrites(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_path = _dry_run(layout, tmp_path / "mapping")
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply1")

    before_jobs = {p.name: p.read_bytes() for p in layout.jobs.glob("*.json")}
    before_tasks = {p.name: p.read_bytes() for p in layout.tasks.glob("*.yaml")}

    report = migrations.apply_attempts(layout, mapping_path, tmp_path / "apply2")
    assert report["written"] == []
    assert sorted(report["already_migrated"]) == sorted(ALL_EXPECTED)
    assert report["backup"] is None

    after_jobs = {p.name: p.read_bytes() for p in layout.jobs.glob("*.json")}
    after_tasks = {p.name: p.read_bytes() for p in layout.tasks.glob("*.yaml")}
    assert after_jobs == before_jobs, "a no-op apply rewrote a job record"
    assert after_tasks == before_tasks, "a no-op apply rewrote a task document"


def test_an_interrupted_apply_resumes_and_completes(layout, tmp_path, monkeypatch):
    build_apply_fixture(layout, tmp_path)
    mapping_path = _dry_run(layout, tmp_path / "mapping")

    real_write = jobs.write
    calls = {"n": 0}

    def flaky_write(layout_, record):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated interruption")
        return real_write(layout_, record)

    monkeypatch.setattr(migrations.jobs_mod, "write", flaky_write)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    monkeypatch.setattr(migrations.jobs_mod, "write", real_write)

    resumed = migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    assert len(resumed["already_migrated"]) == 2             # the two writes that landed before the interruption
    assert sorted(resumed["written"]) == sorted(set(ALL_EXPECTED) - set(resumed["already_migrated"]))

    final = migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    assert final["written"] == []
    assert sorted(final["already_migrated"]) == sorted(ALL_EXPECTED)

    for job_id, (attempt, kind, seq) in ALL_EXPECTED.items():
        rec = jobs.resolve(layout, job_id)
        assert (rec.get("attempt"), rec.get("kind"), rec.get("seq")) == (attempt, kind, seq)


def test_apply_renames_nothing_and_leaves_run_directory_contents_untouched(layout, tmp_path):
    paths = build_apply_fixture(layout, tmp_path)
    mapping_path = _dry_run(layout, tmp_path / "mapping")

    before_job_names = sorted(p.name for p in layout.jobs.glob("*.json"))
    before_task_names = sorted(p.name for p in layout.tasks.glob("*.yaml"))
    handoff = paths["run_dir"] / "HANDOFF.md"
    before_handoff = handoff.read_bytes()

    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")

    assert sorted(p.name for p in layout.jobs.glob("*.json")) == before_job_names
    assert sorted(p.name for p in layout.tasks.glob("*.yaml")) == before_task_names
    assert handoff.read_bytes() == before_handoff
    assert paths["run_dir"].exists()


def test_a_record_already_stamped_whose_mapping_disagrees_is_an_error_not_an_overwrite(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_path = _dry_run(layout, tmp_path / "mapping")
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")

    doc = json.loads(mapping_path.read_text(encoding="utf-8"))
    doc["T-CONT.a4"]["cause"] = "reroute"               # as if a reviewer re-ran the dry run with an override
    mapping_path.write_text(json.dumps(doc), encoding="utf-8")

    before = jobs.resolve(layout, "T-CONT.a4")
    with pytest.raises(migrations.MigrationError, match="T-CONT.a4"):
        migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    assert jobs.resolve(layout, "T-CONT.a4") == before


def test_apply_takes_a_backup_before_its_first_write_and_not_again_when_nothing_changed(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_path = _dry_run(layout, tmp_path / "mapping")

    before_jobs = {p.name: p.read_bytes() for p in layout.jobs.glob("*.json")}
    before_tasks = {p.name: p.read_bytes() for p in layout.tasks.glob("*.yaml")}

    report = migrations.apply_attempts(layout, mapping_path, tmp_path / "apply1")
    backup = Path(report["backup"])
    assert backup.is_dir()
    for name, data in before_jobs.items():
        assert (backup / "jobs" / name).read_bytes() == data
    for name, data in before_tasks.items():
        assert (backup / "tasks" / name).read_bytes() == data

    migrations_root = layout.runtime / "migrations"
    count_after_first = len(list(migrations_root.iterdir()))
    report2 = migrations.apply_attempts(layout, mapping_path, tmp_path / "apply2")
    assert report2["backup"] is None
    assert len(list(migrations_root.iterdir())) == count_after_first


def test_apply_never_touches_escalation_files(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_path = _dry_run(layout, tmp_path / "mapping")

    esc_dir = layout.runtime / "escalation"
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in esc_dir.glob("*.json")}
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in esc_dir.glob("*.json")}
    assert after == before


def test_apply_sets_task_counters_to_the_observed_maxima(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_path = _dry_run(layout, tmp_path / "mapping")
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")

    cont = tasks_mod.load(layout, "T-CONT")
    assert cont.counters["attempt"] == 3
    assert cont.counters["jobs"][1] == {"delivery": 1}
    assert cont.counters["jobs"][2] == {"delivery": 2}
    assert cont.counters["jobs"][3] == {"delivery": 1, "verify": 1}

    diagfirst = tasks_mod.load(layout, "T-DIAGFIRST")
    assert diagfirst.counters["task_jobs"] == {"diagnosis": 1}


def test_apply_refuses_while_the_lock_is_held(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_path = _dry_run(layout, tmp_path / "mapping")
    from supervisor.locks import FileLock
    with FileLock(layout.lock, owner="test-loop"):
        with pytest.raises(migrations.MigrationError, match="lock"):
            migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")


def test_apply_refuses_while_the_runtime_status_is_running(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_path = _dry_run(layout, tmp_path / "mapping")
    from supervisor import runtime as runtime_mod
    runtime_mod.write(layout, status="RUNNING")
    with pytest.raises(migrations.MigrationError, match="RUNNING"):
        migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")


# ------------------------------------------------------------------ check


def test_check_passes_on_a_cleanly_applied_store(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_dir = tmp_path / "mapping"
    mapping_path = _dry_run(layout, mapping_dir)
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    _write_expect(mapping_dir)

    report = migrations.check_attempts(layout, mapping_path, tmp_path / "check")
    assert report["ok"] is True
    assert report["failures"] == {}


def test_check_fails_assertion_1_when_a_job_record_is_gone(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_dir = tmp_path / "mapping"
    mapping_path = _dry_run(layout, mapping_dir)
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    _write_expect(mapping_dir)

    jobs.path(layout, "T-LOST.a2").unlink()
    with pytest.raises(migrations.MigrationError, match="1_job_resolves"):
        migrations.check_attempts(layout, mapping_path, tmp_path / "check")


def test_check_assertion_2_absent_before_is_not_a_failure_but_deleted_after_is(layout, tmp_path):
    paths = build_apply_fixture(layout, tmp_path)
    mapping_dir = tmp_path / "mapping"
    mapping_path = _dry_run(layout, mapping_dir)
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    _write_expect(mapping_dir)

    # T-ABSENT's run_dir was already missing at dry-run time: still missing now, and that is not a failure.
    report = migrations.check_attempts(layout, mapping_path, tmp_path / "check1")
    assert report["ok"] is True

    # T-RUNDIR's run_dir existed at dry-run time; delete it now and the same check must fail.
    shutil.rmtree(paths["run_dir"])
    with pytest.raises(migrations.MigrationError, match="2_run_dir_baseline"):
        migrations.check_attempts(layout, mapping_path, tmp_path / "check2")


def test_check_fails_assertion_3_when_counters_are_tampered(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_dir = tmp_path / "mapping"
    mapping_path = _dry_run(layout, mapping_dir)
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    _write_expect(mapping_dir)

    def fn(doc):
        doc["counters"] = {}
        return doc
    store.update(tasks_mod.path_for(layout, "T-CONT"), fn, lock_path=layout.state / ".tasks.lock")

    with pytest.raises(migrations.MigrationError, match="3_counters"):
        migrations.check_attempts(layout, mapping_path, tmp_path / "check")


def test_check_fails_assertion_4_when_the_dry_runs_own_counts_are_tampered(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_dir = tmp_path / "mapping"
    mapping_path = _dry_run(layout, mapping_dir)
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    _write_expect(mapping_dir)

    report_path = mapping_dir / "report.json"
    doc = json.loads(report_path.read_text(encoding="utf-8"))
    doc["by_kind"]["delivery"] += 1
    report_path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(migrations.MigrationError, match="4_counts"):
        migrations.check_attempts(layout, mapping_path, tmp_path / "check")


def test_check_fails_assertion_5_when_an_escalation_file_changes(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_dir = tmp_path / "mapping"
    mapping_path = _dry_run(layout, mapping_dir)
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    _write_expect(mapping_dir)

    esc = layout.runtime / "escalation" / "T-DIAG.json"
    esc.write_bytes(esc.read_bytes() + b" ")
    with pytest.raises(migrations.MigrationError, match="5_escalation_digests"):
        migrations.check_attempts(layout, mapping_path, tmp_path / "check")


def test_check_fails_assertion_6_without_a_committed_expectation_file(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_dir = tmp_path / "mapping"
    mapping_path = _dry_run(layout, mapping_dir)
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")

    with pytest.raises(migrations.MigrationError, match="6_expectation_file"):
        migrations.check_attempts(layout, mapping_path, tmp_path / "check")


def test_check_fails_assertion_6_when_the_expectation_file_disagrees(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_dir = tmp_path / "mapping"
    mapping_path = _dry_run(layout, mapping_dir)
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    _write_expect(mapping_dir)

    expect_path = mapping_dir / "expect.json"
    doc = json.loads(expect_path.read_text(encoding="utf-8"))
    doc["T-CONT.a1"][0] = 999                           # as if a mapping rule changed and nobody re-checked it
    expect_path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(migrations.MigrationError, match="6_expectation_file"):
        migrations.check_attempts(layout, mapping_path, tmp_path / "check")


def test_check_fails_assertion_7_when_the_mapping_loses_integrity(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_dir = tmp_path / "mapping"
    mapping_path = _dry_run(layout, mapping_dir)
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    _write_expect(mapping_dir)

    doc = json.loads(mapping_path.read_text(encoding="utf-8"))
    doc["T-RUNDIR.a1"]["kind"] = "verify"               # attempt 1 of T-RUNDIR now has no delivery session
    mapping_path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(migrations.MigrationError, match="7_integrity"):
        migrations.check_attempts(layout, mapping_path, tmp_path / "check")


def test_check_refuses_without_the_dry_runs_report(layout, tmp_path):
    build_apply_fixture(layout, tmp_path)
    mapping_dir = tmp_path / "mapping"
    mapping_path = _dry_run(layout, mapping_dir)
    migrations.apply_attempts(layout, mapping_path, tmp_path / "apply")
    (mapping_dir / "report.json").unlink()

    with pytest.raises(migrations.MigrationError, match="report.json"):
        migrations.check_attempts(layout, mapping_path, tmp_path / "check")


# ------------------------------------------------------------------ CLI


def test_cli_help_shows_apply_and_check_and_an_end_to_end_run_passes(layout, monkeypatch, tmp_path):
    from supervisor import project as project_mod
    project_mod.write(layout, {"name": "p"})
    monkeypatch.setenv("SUPERVISOR_ROOT", str(layout.root))

    result = runner.invoke(app, ["migrate", "attempts", "--help"])
    assert result.exit_code == 0, result.output
    for opt in ("--dry-run", "--apply", "--check", "--out", "--mapping", "--overrides", "--compare"):
        assert opt in result.output, result.output

    build_apply_fixture(layout, tmp_path)
    mapping_dir = tmp_path / "mapping"
    dry = runner.invoke(app, ["migrate", "attempts", "--dry-run", "--out", str(mapping_dir)])
    assert dry.exit_code == 0, dry.output
    _write_expect(mapping_dir)

    no_mapping = runner.invoke(app, ["migrate", "attempts", "--apply", "--out", str(tmp_path / "apply")])
    assert no_mapping.exit_code != 0
    assert "mapping" in no_mapping.output.lower()

    apply_out = tmp_path / "apply"
    ap = runner.invoke(app, ["migrate", "attempts", "--apply", "--mapping", str(mapping_dir / "mapping.json"),
                             "--out", str(apply_out)])
    assert ap.exit_code == 0, ap.output
    assert (apply_out / "apply_report.json").exists()

    check_out = tmp_path / "check"
    ck = runner.invoke(app, ["migrate", "attempts", "--check", "--mapping", str(mapping_dir / "mapping.json"),
                             "--out", str(check_out)])
    assert ck.exit_code == 0, ck.output
    assert (check_out / "check_report.json").exists()

    both = runner.invoke(app, ["migrate", "attempts", "--dry-run", "--apply", "--out", str(apply_out)])
    assert both.exit_code != 0
