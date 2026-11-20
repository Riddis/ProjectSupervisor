"""2.7.0 round 2, P3-R2-3, and ATTEMPT_MODEL.md section 7 / step 4 ("the plugin API, additively"): the six
documented plugin surfaces the identity change touches must change additively -- a plugin built against the
previous release keeps running, and `submit_command()` gains the one thing P3-R2-3 found missing: a job can tag
its own command, same as every CLI verb has been able to since R-19/P2-1.

`supervisor.testing` carries no compatibility promise of its own (G2 does not record it), but this project's own
convention holds it to the same additive discipline the design asks for, so `job`/`jobs`/`running_job` are proved
here too, each against the exact wording of ATTEMPT_MODEL.md section 7's table."""
from __future__ import annotations

import inspect

from supervisor import jobs, testing
from supervisor.adapters import REGISTRY
from supervisor.plugin import submit_command
from supervisor.verify import JOB_CONTROL_ENV_VARS, declared_test_env

from tests.conftest import make_task


# ---------------------------------------------------------------- plugin.job resolves both id shapes

def test_job_misses_cleanly_instead_of_raising(layout):
    assert testing.job(layout, "NOPE.a1") is None


def test_job_reads_a_record_under_either_id_shape(layout):
    jobs.write(layout, {"attempt_id": "A.a1", "task_id": "A", "adapter": "process", "phase": "DONE", "spec": {}})
    jobs.write(layout, {"attempt_id": "A.a1.s1", "task_id": "A", "adapter": "process", "phase": "DONE", "spec": {}})
    assert testing.job(layout, "A.a1")["attempt_id"] == "A.a1"
    assert testing.job(layout, "A.a1.s1")["attempt_id"] == "A.a1.s1"


# ---------------------------------------------------------------- plugin.jobs orders by (attempt, kind, seq)

def test_jobs_with_no_task_id_orders_by_attempt_field_not_by_filename(layout):
    # "B.a9" sorts after "A.a1" by filename, but its attempt field (1) is the earlier one.
    jobs.write(layout, {"attempt_id": "B.a9", "task_id": "B", "adapter": "process", "phase": "DONE", "spec": {},
                        "attempt": 1, "kind": "delivery", "seq": 1})
    jobs.write(layout, {"attempt_id": "A.a1", "task_id": "A", "adapter": "process", "phase": "DONE", "spec": {},
                        "attempt": 2, "kind": "delivery", "seq": 1})
    ordered = [r["attempt_id"] for r in testing.jobs(layout)]
    assert ordered.index("B.a9") < ordered.index("A.a1")


def test_jobs_with_a_task_id_still_orders_oldest_attempt_first(layout):
    jobs.write(layout, {"attempt_id": "C.a9", "task_id": "C", "adapter": "process", "phase": "DONE", "spec": {},
                        "attempt": 2, "kind": "delivery", "seq": 1})
    jobs.write(layout, {"attempt_id": "C.a1", "task_id": "C", "adapter": "process", "phase": "DONE", "spec": {},
                        "attempt": 1, "kind": "delivery", "seq": 1})
    assert [r["attempt_id"] for r in testing.jobs(layout, "C")] == ["C.a1", "C.a9"]


# ---------------------------------------------------------------- plugin.running_job gains kind=/attempt=/seq=

def test_running_job_names_kind_attempt_and_seq_as_its_own_parameters():
    params = inspect.signature(testing.running_job).parameters
    assert {"kind", "attempt", "seq"} <= params.keys()
    assert params["kind"].default is None and params["attempt"].default is None and params["seq"].default is None


def test_running_job_with_the_new_fields_omitted_reproduces_todays_record_shape(layout):
    make_task(layout, "D", job={"adapter": "process"}, artifacts=[])
    rec = testing.running_job(layout, "D", "process", {"run_dir": str(layout.runs / "D" / "D.a1")}, {"pid": 0})
    assert "kind" not in rec and "attempt" not in rec and "seq" not in rec


def test_running_job_can_plant_the_attempt_identity_fields(layout):
    make_task(layout, "E", job={"adapter": "process"}, artifacts=[])
    rec = testing.running_job(layout, "E", "process", {"run_dir": str(layout.runs / "E" / "E.a1.s1")}, {"pid": 0},
                              kind="delivery", attempt=1, seq=1)
    assert rec["kind"] == "delivery" and rec["attempt"] == 1 and rec["seq"] == 1
    assert testing.job(layout, rec["attempt_id"])["seq"] == 1


# ---------------------------------------------------------------- submit_command(job_id=...)

def test_submit_command_names_job_id_as_its_own_parameter():
    assert "job_id" in inspect.signature(submit_command).parameters


def test_submit_command_job_id_tags_the_command_as_that_jobs(layout):
    make_task(layout, "Z")
    out = submit_command(layout, "task.note", {"task_id": "Z", "note": "hi"}, "owner", job_id="Z.a1.s1")
    assert out["submitted_by"] == {"kind": "job", "id": "Z.a1.s1"}


def test_submit_command_default_stays_interactive_and_the_library_reads_no_environment(layout, monkeypatch):
    make_task(layout, "Z")
    monkeypatch.setenv("SUPERVISOR_ATTEMPT", "Z.a1.s1")           # a job's own leaked variable must not be inferred
    out = submit_command(layout, "task.note", {"task_id": "Z", "note": "hi"}, "owner-ui")
    assert out["submitted_by"]["kind"] == "interactive"


# ---------------------------------------------------------------- SUPERVISOR_JOB / SUPERVISOR_ATTEMPT_NO

def test_process_lane_env_carries_the_job_and_attempt_number_markers():
    # `attempt` is written onto the spec by `loop.dispatch`, from the id the allocator minted — never parsed
    # back out of `attempt_id` (ATTEMPT_MODEL.md §6: the writer switch deletes that parse).
    env = REGISTRY["process"].env({"attempt_id": "F.a3.s1", "attempt": 3})
    assert env["SUPERVISOR_ATTEMPT"] == "F.a3.s1"
    assert env["SUPERVISOR_JOB"] == "F.a3.s1"
    assert env["SUPERVISOR_ATTEMPT_NO"] == "3"


def test_codex_cli_env_carries_the_same_markers():
    env = REGISTRY["codex_cli"].env({"attempt_id": "F.a5.s1", "attempt": 5})
    assert env["SUPERVISOR_JOB"] == "F.a5.s1" and env["SUPERVISOR_ATTEMPT_NO"] == "5"


def test_claude_cli_env_carries_the_same_markers():
    env = REGISTRY["claude_cli"].env({"attempt_id": "F.a7.s1", "attempt": 7})
    assert env["SUPERVISOR_JOB"] == "F.a7.s1" and env["SUPERVISOR_ATTEMPT_NO"] == "7"


def test_a_declared_command_sees_none_of_the_two_new_markers_either(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_JOB", "F.a3")
    monkeypatch.setenv("SUPERVISOR_ATTEMPT_NO", "3")
    assert "SUPERVISOR_JOB" not in declared_test_env()
    assert "SUPERVISOR_ATTEMPT_NO" not in declared_test_env()
    assert {"SUPERVISOR_JOB", "SUPERVISOR_ATTEMPT_NO"} <= set(JOB_CONTROL_ENV_VARS)


# ---------------------------------------------------------------- a plugin built against 2.7.0 still runs unchanged

def test_a_plugin_built_against_the_previous_release_still_runs_unchanged(layout):
    make_task(layout, "G", job={"adapter": "process"}, artifacts=[])
    ad = REGISTRY["process"]
    task = testing.task(layout, "G")
    spec = ad.prepare(layout, task, "G.a1")                              # positional attempt_id, as 2.7.0 called it
    rec = testing.running_job(layout, "G", "process", spec, {"pid": 0}, gpu=False)   # no new kwargs at all
    assert rec["attempt_id"] == "G.a1" and rec["phase"] == "RUNNING"
    assert testing.job(layout, "G.a1")["phase"] == "RUNNING"
    out = submit_command(layout, "task.note", {"task_id": "G", "note": "hi"}, "owner-ui")   # no job_id at all
    assert out["submitted_by"]["kind"] == "interactive"
