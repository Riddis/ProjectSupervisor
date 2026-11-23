"""2.9.0 step 12: the documented result contract must be the one the code validates. `docs/TASK_CONTRACT.md`
describes a diagnosis `defer` as `new_task`/`wait_for` and says nothing of `needs`, `contract_rev` or the `job`
writer rule; `docs/PLUGIN_API.md`'s `job_spec` section still describes `job:` as an untyped mapping. This reads
each document next to the actual validator (`supervisor.plans.OPERATIONS`, `supervisor.tasks.CONTRACT_FIELDS`/
`JUDGED_FIELDS`, `supervisor.models.task.Needs`/`Job`) rather than asserting fixed prose, so it keeps holding once
either side changes again."""
from __future__ import annotations

import re
from pathlib import Path

from supervisor import plans, tasks
from supervisor.models.task import Job, Needs

REPO = Path(__file__).resolve().parents[1]
TASK_CONTRACT = (REPO / "docs/TASK_CONTRACT.md").read_text(encoding="utf-8")
PLUGIN_API = (REPO / "docs/PLUGIN_API.md").read_text(encoding="utf-8")

NEEDS_FIELDS = tuple(Needs.model_fields)
JOB_WRITABLE_FIELDS = tuple(k for k in Job.model_fields if k != "preflight")


def _contract_table_fields(doc: str) -> set[str]:
    """The field names of the `The **contract** fields` table: every backtick-quoted name in the first column of
    a `| `name`[, `name2`] | ... |` row between that heading and `The **runtime** fields` (a row may name more
    than one field, e.g. `title`, `objective` sharing one row)."""
    lines = doc.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("The **contract** fields"))
    end = next(i for i, line in enumerate(lines) if line.startswith("The **runtime** fields"))
    out: set[str] = set()
    for line in lines[start:end]:
        if not line.startswith("|"):
            continue
        first_col = line.split("|")[1]
        out.update(re.findall(r"`([a-zA-Z_]+)`", first_col))
    return out


def test_every_contract_field_is_documented():
    documented = _contract_table_fields(TASK_CONTRACT)
    missing = [f for f in tasks.CONTRACT_FIELDS if f not in documented]
    assert missing == [], f"undocumented contract field(s): {missing}"


def test_the_defer_names_the_plans_own_six_operations():
    missing = [op for op in plans.OPERATIONS if f"`{op}`" not in TASK_CONTRACT]
    assert missing == [], f"operation(s) not named in the defer's own description: {missing}"


def test_the_defer_no_longer_describes_new_task_or_wait_for():
    assert "new_task" not in TASK_CONTRACT
    assert "wait_for" not in TASK_CONTRACT


def test_contract_rev_and_its_judged_set_are_documented():
    assert "`contract_rev`" in TASK_CONTRACT
    missing = [f for f in sorted(tasks.JUDGED_FIELDS) if f"`{f}`" not in TASK_CONTRACT]
    assert missing == [], f"judged field(s) not named in the contract_rev note: {missing}"


def test_needs_is_documented_with_its_own_four_keys():
    assert "`needs`" in TASK_CONTRACT
    missing = [f for f in NEEDS_FIELDS if f not in TASK_CONTRACT]
    assert missing == [], f"needs.{{}} not named: {missing}"


def test_the_job_writer_rule_is_documented():
    assert "gates.is_owner_actor" in TASK_CONTRACT
    assert "job.template" in TASK_CONTRACT and "job.params" in TASK_CONTRACT
    assert "nothing else may write one at all" in TASK_CONTRACT


def test_job_specs_own_section_names_every_framework_known_key():
    missing = [f for f in JOB_WRITABLE_FIELDS if f"`{f}`" not in PLUGIN_API]
    assert missing == [], f"Job field(s) not named in job_spec's own section: {missing}"
