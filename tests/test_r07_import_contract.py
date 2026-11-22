"""R-07: seed import must validate the graph it will actually write, apply current contract rules only to rows
being created or changed, carry every contract field (not just the ones CONTRACT_FIELDS happened to list), offer a
patch for one field without resending the whole contract, and write all its rows or none."""
import yaml
import pytest
from pydantic import ValidationError

from supervisor import store, tasks
from tests.conftest import make_task


def _row(tid, **fields):
    base = dict(phase=0, title="t", objective="o", owner_role="engineer", artifacts=["x"],
               cost_category="deterministic")
    base.update(fields)
    base["id"] = tid
    return base


def test_seed_with_a_finished_row_that_fails_a_current_rule_still_creates_the_new_row(layout):
    # OLD's artifact was legal when it was written; a rule added since then refuses it on every re-validation.
    make_task(layout, "OLD", status="COMPLETE", artifacts=["state/tasks_seed.yaml"], cost_category="deterministic")
    seed = {"tasks": [_row("OLD", artifacts=["state/tasks_seed.yaml"], status="COMPLETE"), _row("NEW")]}
    p = layout.state / "seed.yaml"
    p.write_text(yaml.safe_dump(seed), encoding="utf-8")

    r = tasks.import_seed(layout, p)

    assert r["created"] == ["NEW"]
    assert r["untouched"] == ["OLD"]
    assert (layout.tasks / "NEW.yaml").exists()


def test_seed_passing_only_because_of_a_row_it_does_not_write_is_refused(layout):
    make_task(layout, "A", cost_category="deterministic")
    make_task(layout, "B", deps=["A"], cost_category="deterministic")
    # The seed's own version of B (no deps) would make A -> B legal; B is not in update_ids, so B's real
    # contract (depends_on A) stays on disk, and A -> B -> A is a cycle nobody who reads the seed can see.
    seed = {"tasks": [_row("A", depends_on=["B"]), _row("B", depends_on=[])]}
    p = layout.root / "seed.yaml"
    p.write_text(yaml.safe_dump(seed), encoding="utf-8")

    with pytest.raises(tasks.TaskError, match="cycle"):
        tasks.import_seed(layout, p, update_ids=["A"])

    assert tasks.load(layout, "A").depends_on == []
    assert tasks.load(layout, "B").depends_on == ["A"]


def test_refresh_stores_evidence_test_roots_and_the_seeds_own_job(layout):
    # An owner's refresh (asserted explicitly — gates.is_owner_actor, never a bare default)
    # carries a job change like any other contract field — no separate task.set_job, no job_not_applied.
    make_task(layout, "A", tests=["prose"], cost_category="deterministic")
    seed = {"tasks": [_row("A", title="t2", evidence="review", test_roots=["sub"],
                           job={"adapter": "process", "argv": ["x"]})]}
    p = layout.root / "seed.yaml"
    p.write_text(yaml.safe_dump(seed), encoding="utf-8")

    r = tasks.import_seed(layout, p, update_ids=["A"], actor="owner")

    doc = store.read(layout.tasks / "A.yaml")
    assert r["updated"] == ["A"]
    assert doc["title"] == "t2"
    assert doc.get("evidence") == "review"
    assert doc.get("test_roots") == ["sub"]
    # `preflight` is attached by the same write; "x" is not an executable, so it
    # fails, which does not block the write -- only naming the field it was writing, never its value.
    assert {k: v for k, v in doc.get("job", {}).items() if k != "preflight"} == {"adapter": "process", "argv": ["x"]}
    assert "job_not_applied" not in r


def test_refresh_omitting_evidence_and_test_roots_keeps_them(layout):
    # R-07(d): a refresh entry's omitted fields must not erase what an EARLIER refresh (or the store) already
    # set. A seed row touching only `title` must not reset `evidence`/`test_roots` to their defaults, the same
    # way it already leaves `owned_paths`/`checks` alone.
    make_task(layout, "A", tests=["prose"], evidence="review", test_roots=["sub"], cost_category="deterministic")
    seed = {"tasks": [_row("A", title="t2")]}
    p = layout.root / "seed.yaml"
    p.write_text(yaml.safe_dump(seed), encoding="utf-8")

    r = tasks.import_seed(layout, p, update_ids=["A"])

    doc = store.read(layout.tasks / "A.yaml")
    assert r["updated"] == ["A"]
    assert doc["title"] == "t2"
    assert doc.get("evidence") == "review"
    assert doc.get("test_roots") == ["sub"]


def test_patch_can_add_one_dependency_without_resending_the_contract(layout):
    make_task(layout, "A", cost_category="deterministic")
    make_task(layout, "B", cost_category="deterministic")

    tasks.patch_task(layout, "B", {"add_dependency": "A"})
    assert tasks.load(layout, "B").depends_on == ["A"]

    tasks.patch_task(layout, "B", {"remove_dependency": "A"})
    assert tasks.load(layout, "B").depends_on == []


def test_import_third_row_invalid_writes_nothing(layout):
    seed = {"tasks": [_row("S1"), _row("S2"), {"id": "S3", "phase": 0, "title": "t"}]}  # S3 has no objective/owner_role
    p = layout.root / "seed.yaml"
    p.write_text(yaml.safe_dump(seed), encoding="utf-8")

    with pytest.raises((tasks.TaskError, ValidationError)):
        tasks.import_seed(layout, p)

    assert not list(layout.tasks.glob("*.yaml"))
