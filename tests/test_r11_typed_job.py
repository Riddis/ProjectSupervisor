"""`job` becomes a declared, typed contract field, carried
by import, refresh and patch under a rule on who is writing (section 3.4) rather than on the field. Before this
change `job` lived only in `model_extra`, so a misspelled key was silently ignored and a seed could not carry one
at all (`tasks.py`'s old `job_not_applied` report)."""
import pytest
import yaml
from pydantic import ValidationError

from supervisor import commands, plugins, store, tasks, testing
from tests.conftest import make_task


def _row(tid, **fields):
    base = dict(phase=0, title="t", objective="o", owner_role="engineer", artifacts=["x"],
               cost_category="deterministic")
    base.update(fields)
    base["id"] = tid
    return base


@pytest.fixture(autouse=True)
def _clean():
    testing.reset()
    yield
    testing.reset()


# ---------------------------------------------------------------- a seed may carry a job (import_seed, create)

def test_a_seed_carrying_a_job_creates_a_runnable_task_with_no_job_not_applied(layout):
    seed = {"tasks": [_row("NEW", job={"adapter": "process", "argv": ["x"]})]}
    p = layout.root / "seed.yaml"
    p.write_text(yaml.safe_dump(seed), encoding="utf-8")

    r = tasks.import_seed(layout, p, actor="owner")

    assert r["created"] == ["NEW"]
    assert "job_not_applied" not in r
    task = tasks.load(layout, "NEW")
    assert task.job is not None and task.job.adapter == "process" and task.job.argv == ["x"]


# ---------------------------------------------------------------- a refresh carries a job change (the owner's)

def test_a_refresh_carries_a_job_change(layout):
    make_task(layout, "A", cost_category="deterministic")
    seed = {"tasks": [_row("A", job={"adapter": "process", "argv": ["y"]})]}
    p = layout.root / "seed.yaml"
    p.write_text(yaml.safe_dump(seed), encoding="utf-8")

    r = tasks.import_seed(layout, p, update_ids=["A"], actor="owner")

    assert r["updated"] == ["A"]
    assert "job_not_applied" not in r
    doc = store.read(layout.tasks / "A.yaml")
    # `preflight` is attached by the same write; "y" is not an executable, so it
    # fails, which does not block the write -- only naming the field it was writing, never its value.
    assert {k: v for k, v in doc.get("job", {}).items() if k != "preflight"} == {"adapter": "process", "argv": ["y"]}


def test_a_refresh_omitting_job_leaves_an_earlier_one_alone(layout):
    make_task(layout, "A", job={"adapter": "process", "argv": ["keep"]}, cost_category="deterministic")
    seed = {"tasks": [_row("A", title="t2")]}
    p = layout.root / "seed.yaml"
    p.write_text(yaml.safe_dump(seed), encoding="utf-8")

    tasks.import_seed(layout, p, update_ids=["A"])

    doc = store.read(layout.tasks / "A.yaml")
    assert {k: v for k, v in doc.get("job", {}).items() if k != "preflight"} == {"adapter": "process", "argv": ["keep"]}


# ---------------------------------------------------------------- a task.patch carries a job change too

def test_a_patch_carries_a_job_change(layout):
    make_task(layout, "A", cost_category="deterministic")

    tasks.patch_task(layout, "A", {"job": {"adapter": "process", "argv": ["z"]}}, actor="owner")

    assert tasks.load(layout, "A").job.argv == ["z"]


# ---------------------------------------------------------------- the writer-authority rule (section 3.4)

def test_a_writer_that_is_neither_owner_nor_plan_writes_no_job_at_all_via_patch(layout):
    make_task(layout, "A", cost_category="deterministic")

    with pytest.raises(tasks.TaskError, match="may not write a job"):
        tasks.patch_task(layout, "A", {"job": {"adapter": "process", "argv": ["z"]}}, actor="worker")

    assert tasks.load(layout, "A").job is None


def test_a_writer_that_is_neither_owner_nor_plan_writes_no_job_at_all_via_import(layout):
    seed = {"tasks": [_row("NEW", job={"adapter": "process", "argv": ["z"]})]}
    p = layout.root / "seed.yaml"
    p.write_text(yaml.safe_dump(seed), encoding="utf-8")

    with pytest.raises(tasks.TaskError, match="may not write a job"):
        tasks.import_seed(layout, p, actor="worker")

    assert not (layout.tasks / "NEW.yaml").exists()


def test_a_plan_writer_may_set_only_a_template_and_params(layout):
    make_task(layout, "A", job={"adapter": "process", "argv": ["keep"]}, cost_category="deterministic")

    tasks.patch_task(layout, "A", {"job": {"template": "t1", "params": {"n": 1}}}, actor="plan")
    assert tasks.load(layout, "A").job.template == "t1"

    with pytest.raises(tasks.TaskError, match="template and job.params only"):
        tasks.patch_task(layout, "A", {"job": {"template": "t1", "params": {}, "argv": ["sneaky"]}}, actor="plan")


def test_the_owner_may_write_a_literal_argv_the_plan_may_not(layout):
    make_task(layout, "A", cost_category="deterministic")
    tasks.patch_task(layout, "A", {"job": {"adapter": "process", "argv": ["ok"]}}, actor="owner")
    assert tasks.load(layout, "A").job.argv == ["ok"]


def test_the_control_centers_owner_ui_identity_may_write_a_job(layout):
    """The owner is `gates.is_owner_actor` (the CLI's `owner`, the Control Center's `owner-ui`, any
    `owner-<channel>`) — not a second, narrower copy of that rule."""
    make_task(layout, "A", cost_category="deterministic")
    tasks.patch_task(layout, "A", {"job": {"adapter": "process", "argv": ["ok"]}}, actor="owner-ui")
    assert tasks.load(layout, "A").job.argv == ["ok"]


def test_a_caller_naming_no_actor_at_all_may_not_write_a_job(layout):
    """Authority is asserted, never inherited: a seed row or a patch that does not name an actor must not draw
    default-parameter identity (the queued command applier's own `cmd.get("actor", "cli")` fallback included)."""
    make_task(layout, "A", cost_category="deterministic")
    with pytest.raises(tasks.TaskError, match="may not write a job"):
        tasks.patch_task(layout, "A", {"job": {"adapter": "process", "argv": ["z"]}})
    assert tasks.load(layout, "A").job is None


def test_task_set_job_refuses_a_non_owner_actor(layout):
    """`task.set_job` always carries a literal `argv`; the one write path a plan must never reach must refuse it exactly like a patch or a seed row would."""
    make_task(layout, "A", cost_category="deterministic")
    with pytest.raises(tasks.TaskError, match="may not write a job"):
        commands.apply_job(layout, "A", {"adapter": "process", "argv": ["sneaky"]}, "worker")
    assert tasks.load(layout, "A").job is None


def test_task_set_job_refuses_a_plan_a_literal_argv_too(layout):
    """The one actor the owner-only rule forbids an `argv` for: a plan reaching `task.set_job` (a queued command's
    `actor`, a plugin's `submit_command(..., actor=...)`) must not slip a literal `argv` through where a patch
    would have refused anything beyond `template`/`params`."""
    make_task(layout, "A", cost_category="deterministic")
    with pytest.raises(tasks.TaskError):
        commands.apply_job(layout, "A", {"adapter": "process", "argv": ["sneaky"]}, "plan")
    assert tasks.load(layout, "A").job is None


def test_task_set_job_allows_the_control_centers_owner_ui(layout):
    make_task(layout, "A", cost_category="deterministic")
    t, warnings = commands.apply_job(layout, "A", {"adapter": "process", "argv": ["ok"]}, "owner-ui")
    assert t.job.argv == ["ok"]
    assert warnings == []


# ---------------------------------------------------------------- every framework-known key is typed

def test_a_wrong_type_on_a_framework_known_key_is_refused_at_the_write(layout):
    make_task(layout, "A", cost_category="deterministic")

    with pytest.raises(ValidationError, match="timeout_s"):
        tasks.patch_task(layout, "A", {"job": {"adapter": "process", "timeout_s": "soon"}}, actor="owner")


# ---------------------------------------------------------------- per-adapter schema: refuse vs warn (section 3.2)

SCHEMA_PLUGIN = '''
from pydantic import BaseModel

class WidgetParams(BaseModel):
    workflow: str = ""

def setup(registry):
    registry.job_schema("widget", WidgetParams)
'''


def test_a_misspelled_key_is_refused_for_an_adapter_that_declares_a_schema(tmp_path):
    lay = plugins.load(testing.project(tmp_path, modules={"schema": SCHEMA_PLUGIN})).layout
    make_task(lay, "A", cost_category="deterministic")

    with pytest.raises(tasks.TaskError, match="widget"):
        tasks.patch_task(lay, "A", {"job": {"adapter": "widget", "workflow": "a", "worfklow_typo": "b"}},
                         actor="owner")


def test_a_misspelled_key_is_only_warned_for_an_adapter_with_no_schema(layout):
    make_task(layout, "A", cost_category="deterministic")

    r = tasks.patch_task(layout, "A", {"job": {"adapter": "process", "argvv_typo": ["x"]}}, actor="owner")

    assert any("argvv_typo" in w for w in r.get("warnings", []))
    assert tasks.load(layout, "A").job.model_extra.get("argvv_typo") == ["x"]


def test_an_installed_plugin_with_no_schema_keeps_working_unchanged(tmp_path):
    """A plugin lane that predates registry.job_schema (no call to it at all) is only ever warned, never
    refused, for a key it has always carried."""
    no_schema_plugin = '''
from supervisor.plugin import Adapter, Result

class WidgetLane(Adapter):
    name = "widget2"
    def prepare(self, layout, task, attempt_id):
        return {}
    def submit(self, spec):
        return {}
    def alive(self, handle):
        return False
    def collect(self, spec, handle):
        return Result(status="COMPLETE")

def setup(registry):
    registry.adapter(WidgetLane())
'''
    lay = plugins.load(testing.project(tmp_path, modules={"widget2": no_schema_plugin})).layout
    make_task(lay, "A", cost_category="deterministic")

    r = tasks.patch_task(lay, "A", {"job": {"adapter": "widget2", "workflow": "a"}}, actor="owner")

    assert tasks.load(lay, "A").job.adapter == "widget2"
    assert any("widget2" in w for w in r.get("warnings", []))


# -------------------------------------------------- the built-in lanes declare their own schema too (section 3.2)

def test_a_built_in_lanes_own_documented_keys_draw_no_warning(layout):
    """claude_cli's documented keys (docs/TASK_CONTRACT.md) are typed by its own job_schema (one of the built-in
    lanes besides `process`), so a routine job using them is never told to check for a misspelled key."""
    make_task(layout, "A", cost_category="deterministic", owned_paths=["."])

    r = tasks.patch_task(layout, "A", {"job": {"adapter": "claude_cli", "role": "code_review",
                                               "write_scope": "run_dir"}}, actor="owner")

    assert r.get("warnings", []) == []


def test_a_misspelled_key_is_refused_for_a_built_in_lane_with_a_schema(layout):
    make_task(layout, "A", cost_category="deterministic", owned_paths=["."])

    with pytest.raises(tasks.TaskError, match="prompt_fiel"):
        tasks.patch_task(layout, "A", {"job": {"adapter": "claude_cli", "prompt_fiel": "x.md"}}, actor="owner")


@pytest.mark.parametrize("adapter", ["claude_cli", "claude_api", "codex_cli"])
def test_the_review_lanes_own_confinement_keys_draw_no_refusal_on_any_review_lane(layout, adapter):
    """The framework composes its review lane (`tools`, `sandbox`, `write_scope`) into the job of whichever lane
    reviews, and documents all three as data a role may override. An owner writing that shape by hand must
    therefore not be refused by the lane's own schema for a key the framework itself puts there — whichever of
    them that lane actually reads (a per-path write mode, a sandbox mode, or neither)."""
    make_task(layout, "A", cost_category="deterministic", owned_paths=["."])

    r = tasks.patch_task(layout, "A", {"job": {"adapter": adapter, "tools": "reviewer",
                                               "sandbox": "read-only", "write_scope": "run_dir"}}, actor="owner")

    assert r.get("warnings", []) == []
    assert tasks.load(layout, "A").job.model_extra.get("sandbox") == "read-only"


def test_the_human_lanes_own_documented_keys_draw_no_warning(layout):
    make_task(layout, "A", cost_category="deterministic")

    r = tasks.patch_task(layout, "A", {"job": {"adapter": "human", "subject": "s", "summary": "s",
                                               "allowed_responses": ["ACCEPT"]}}, actor="owner")

    assert r.get("warnings", []) == []


# ------------------------------------------- the create path is a job write path too (section 3.4, decision 7)

def test_a_non_owner_create_may_not_carry_a_literal_argv(layout):
    """A created row's job is the writer's own literal choice exactly like a refresh's or a patch's, so the same
    rule decides it: the create path must not be the one way around the authority check."""
    with pytest.raises(tasks.TaskError, match="may not write a job"):
        tasks.create(layout, _row("NEW", job={"adapter": "process", "argv": ["sneaky"]}), actor="worker")

    assert not (layout.tasks / "NEW.yaml").exists()


def test_a_create_naming_no_actor_at_all_may_not_carry_a_job(layout):
    """Authority is asserted, never inherited from a default parameter value."""
    with pytest.raises(tasks.TaskError, match="may not write a job"):
        tasks.create(layout, _row("NEW", job={"adapter": "process", "argv": ["z"]}))

    assert not (layout.tasks / "NEW.yaml").exists()


def test_a_plan_creating_a_task_may_not_carry_a_literal_argv(layout):
    """The one actor decision 7 forbids an `argv`: a plan creating a task may name a template and typed params
    only, whichever write path it reaches."""
    with pytest.raises(tasks.TaskError, match="template and job.params only"):
        tasks.create(layout, _row("NEW", job={"adapter": "process", "argv": ["sneaky"]}), actor="plan")

    assert not (layout.tasks / "NEW.yaml").exists()


def test_the_owners_create_may_carry_a_literal_argv(layout):
    r = tasks.create(layout, _row("NEW", job={"adapter": "process", "argv": ["ok"]}), actor="owner")

    assert r["id"] == "NEW"
    assert tasks.load(layout, "NEW").job.argv == ["ok"]


def test_a_queued_task_create_from_a_non_owner_carries_no_job(layout):
    """A queued command envelope names its own actor (a plugin's `submit_command(..., actor=...)`, a hand-written
    file): applying it must not create a task whose job the loop would then run."""
    commands.submit(layout, "task.create", {"task": _row("NEW", job={"adapter": "process", "argv": ["sneaky"]})},
                    actor="worker")

    out = commands.apply_all(layout)

    assert out[0]["ok"] is False and "may not write a job" in out[0]["error"]
    assert not (layout.tasks / "NEW.yaml").exists()


def test_a_queued_task_create_from_the_owner_carries_its_job(layout):
    commands.submit(layout, "task.create", {"task": _row("NEW", job={"adapter": "process", "argv": ["ok"]})},
                    actor="owner")

    out = commands.apply_all(layout)

    assert out[0]["ok"] is True, out[0].get("error")
    assert tasks.load(layout, "NEW").job.argv == ["ok"]


def test_a_create_carrying_an_empty_job_is_no_write_and_needs_no_authority(layout):
    """A document built from a whole task names every declared field, `job` among them: saying a new row runs
    nothing is not a job write, and there is nothing on a new row to clear."""
    r = tasks.create(layout, _row("NEW", job=None))

    assert r["id"] == "NEW"
    assert tasks.load(layout, "NEW").job is None


def test_a_misspelled_key_on_a_created_row_is_refused_for_an_adapter_with_a_schema(tmp_path):
    lay = plugins.load(testing.project(tmp_path, modules={"schema": SCHEMA_PLUGIN})).layout

    with pytest.raises(tasks.TaskError, match="worfklow_typo"):
        tasks.create(lay, _row("NEW", job={"adapter": "widget", "workflow": "a", "worfklow_typo": "b"}),
                     actor="owner")

    assert not (lay.tasks / "NEW.yaml").exists()


def test_a_misspelled_key_on_a_created_row_is_warned_for_an_adapter_with_no_schema(layout):
    r = tasks.create(layout, _row("NEW", job={"adapter": "process", "argvv_typo": ["x"]}), actor="owner")

    assert any("argvv_typo" in w for w in r.get("warnings", []))
    assert tasks.load(layout, "NEW").job.model_extra.get("argvv_typo") == ["x"]


# -------------------------------------------------- job is a declared field, not model_extra (plugin API, 3.2)

def test_a_job_is_no_longer_reachable_through_model_extra(layout):
    """docs/FRAMEWORK_VS_PLUGIN.md's own adapter example reads a job through `job_spec(task)`, never
    `task.model_extra["job"]`: `job` is a declared field now, so `model_extra` never carries it."""
    from supervisor.plugin import job_spec

    make_task(layout, "A", job={"adapter": "process", "argv": ["x"]}, cost_category="deterministic")

    task = tasks.load(layout, "A")

    assert "job" not in (task.model_extra or {})
    assert job_spec(task)["argv"] == ["x"]
