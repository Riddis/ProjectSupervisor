"""R-11 / RECOVERY_PLAN.md section 3, D-027 decision 7: `job` becomes a declared, typed contract field, carried
by import, refresh and patch under a rule on who is writing (section 3.4) rather than on the field. Before this
change `job` lived only in `model_extra`, so a misspelled key was silently ignored and a seed could not carry one
at all (`tasks.py`'s old `job_not_applied` report)."""
import pytest
import yaml
from pydantic import ValidationError

from supervisor import plugins, store, tasks, testing
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

    r = tasks.import_seed(layout, p)

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

    r = tasks.import_seed(layout, p, update_ids=["A"])

    assert r["updated"] == ["A"]
    assert "job_not_applied" not in r
    doc = store.read(layout.tasks / "A.yaml")
    assert doc.get("job") == {"adapter": "process", "argv": ["y"]}


def test_a_refresh_omitting_job_leaves_an_earlier_one_alone(layout):
    make_task(layout, "A", job={"adapter": "process", "argv": ["keep"]}, cost_category="deterministic")
    seed = {"tasks": [_row("A", title="t2")]}
    p = layout.root / "seed.yaml"
    p.write_text(yaml.safe_dump(seed), encoding="utf-8")

    tasks.import_seed(layout, p, update_ids=["A"])

    doc = store.read(layout.tasks / "A.yaml")
    assert doc.get("job") == {"adapter": "process", "argv": ["keep"]}


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
