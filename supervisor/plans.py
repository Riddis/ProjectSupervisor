"""The recovery plan (R-10): a typed document a diagnosis may propose to
repair one stopped attempt, a closed six-operation vocabulary, and a deterministic validator that refuses whole on
any of 26 named codes. No applier lives here — `validate()` never writes to the store, never touches state/plans/,
and never mutates a task; step 2 of the order of work builds the journal and the apply path.

R0 (restated): no value a model produces ever becomes a program, an argument, a path outside a declared
root, or an operation the framework did not already know how to perform. `SetJobOp` is the one place this has to be
structural rather than a check (RF-04): the model declares exactly `op`, `task`, `template`, `params` and forbids
every other key, so there is no `argv`, `fn`, `cmd` or `shell` slot anywhere in the six operations for a value to
reach. Every other refusal is ordinary deterministic code.

R0 reaches past the operations' own fields into the *contract* the two writing operations carry, because a contract
has executable content of its own: a check's `command` is a string verification re-runs, its `cwd` is where that
runs, and a test root is what bounds the `cwd`. A template reference is not a command, but a check is one, so
neither `create_task` nor `patch_contract` may author either, or a test root (`_executable_content_refusals`,
RF-03). An allow-listed command *prefix* would not do: the arguments after it would still be the model's, and R0
names an argument beside a program.

A refusal never carries the value it refused. The refusal list is written to the store, rendered, and quoted into
the correction turn's brief, so every message here is built from field paths and counts and run through
`policy.redact` — never from the string representation of a validation error, which quotes the input. The two
value scans (RF-03, RF-21) therefore run on every operation whatever the shape check decided about it: an
operation refused for its shape is the one most likely to be carrying something that must not be logged.

Three refusal codes (RF-23, RF-24, RF-25) belong, by the plan model's own preamble, to the approval
lane and the inbox (steps 7-8 of the order of work) rather than to a plan document's own shape. Their rule is
already fully determined by this step's inputs, so the pure functions that decide them (`self_approval_refusal`,
`choice_not_offered_refusal`, `choice_stale_refusal`) live here, ahead of the machinery that will call them, so the
vocabulary of refusals has exactly one home.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import inbox as inbox_mod
from . import policy as policy_mod
from . import store
from . import tasks as tasks_mod
from .models.task import Check, Task, TaskStatus
from .paths import Layout

PLAN_SCHEMA = 1

OPERATIONS = ("create_task", "add_dependency", "remove_dependency", "patch_contract", "set_job", "resume")
OWNER_ONLY_EFFECTS = frozenset({"apply_plan", "cancel_plan", "cancel_task", "hold", "release_hold", "diagnose_now"})


# ----------------------------------------------------------------------- the six operations, typed, closed (§2.2)
#
# Every model forbids any key beyond the ones named: an operation is a selection from the vocabulary plus typed,
# bounded parameters, never an open document. `SetJobOp` is the one R0 binds structurally rather than by a check
# (RF-04) — it declares exactly `op`, `task`, `template`, `params`, so there is no `argv`, `fn`, `cmd` or `shell`
# key anywhere for a value to reach; `test_rf04_job_without_template_structural` constructs one to prove it.

class CreateTaskOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: str
    task: dict


class AddDependencyOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: str
    task: str
    depends_on: list[str]


class RemoveDependencyOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: str
    task: str
    depends_on: list[str]


class PatchContractOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: str
    task: str
    fields: dict


class SetJobOp(BaseModel):
    """The whole of what a plan may ever write towards running something: a template id and typed parameters.
    No other key — not `argv`, not `fn`, not `cmd`, not `shell` — is a field this model could ever hold."""
    model_config = ConfigDict(extra="forbid")
    op: str
    task: str
    template: str
    params: dict = Field(default_factory=dict)


class ResumeOp(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    op: str
    task: str
    as_: str = Field("ready", alias="as")


_OP_MODELS = {"create_task": CreateTaskOp, "add_dependency": AddDependencyOp, "remove_dependency": RemoveDependencyOp,
              "patch_contract": PatchContractOp, "set_job": SetJobOp, "resume": ResumeOp}

# What `patch_contract.fields` may name, before the monotonicity checks run (§2.4). Anything outside this set is
# not a field this operation may touch at all (RF-02) except the two named elsewhere: the eight authority fields
# (RF-15) and `acceptance` (RF-16, "replaces acceptance").
AUTHORITY_FIELDS = frozenset({"human_approval", "escalation", "cost_category", "owner_role", "phase", "priority",
                              "title", "objective"})
PATCH_ALLOWED_FIELDS = frozenset({"tests", "checks", "test_roots", "artifacts", "reviewer", "evidence",
                                  "owned_paths", "actions", "review_note"})
PATCH_ADD_ONLY_FIELDS = frozenset({"tests", "checks", "test_roots", "artifacts", "reviewer"})

# `checks` and `test_roots` stay *named* above — a `checks` key is how §1.3's `checks_to_tests` reaches the prose
# it moves to `tests`, and emptying one of them is a loosening RF-16 has to be able to see — but no content a plan
# proposes for either survives `_executable_content_refusals`. They are contract fields a human writes.
EXECUTABLE_CONTRACT_FIELDS = ("checks", "test_roots")

# What `create_task.task` may name: the four the created task must state, the four
# inherited from the subject and therefore forbidden here, and everything else a contract may optionally carry.
CREATE_REQUIRED_FIELDS = frozenset({"title", "objective", "owner_role", "cost_category"})
CREATE_FORBIDDEN_FIELDS = frozenset({"human_approval", "escalation", "phase", "priority"})
CREATE_OPTIONAL_FIELDS = frozenset({"id", "depends_on", "actions", "artifacts", "tests", "checks", "owned_paths",
                                    "reviewer", "review_note", "acceptance", "evidence", "test_roots",
                                    "approval_note", "needs"})
# `needs` is create_task-only: it is deliberately absent from PATCH_ALLOWED_FIELDS, so a plan
# has no operation that can write it onto a task it did not create -- the writer-authority rule
# holds by the ops vocabulary's own field lists, with no extra check needed.
CREATE_ALLOWED_FIELDS = CREATE_REQUIRED_FIELDS | CREATE_OPTIONAL_FIELDS

EXCLUSIVE = "."

# A string this long or longer is screened for shell metacharacters and path separators (RF-03); kept in one place
# so a future operation's field is covered automatically rather than by a per-operation allow-list of what to scan.
_SHELL_META = set(";&|`$><(){}\n\r\x00")
_PATH_SEP = set("/\\")

EVIDENCE_ORDER = {"review": 0, "auto": 1, "commands": 2}

DEFAULT_MAX_OPS = 8
DEFAULT_MAX_CREATES = 2
DEFAULT_MAX_RESUMES = 3

PLAN_STATUSES = ("proposed", "refused", "awaiting_approval", "applying", "applied", "stuck", "abandoned")
TERMINAL_PLAN_STATUSES = frozenset({"applied", "abandoned", "refused"})


class PlanError(RuntimeError):
    """A plan document cannot be found, or an operation (`apply`, `cancel_plan`) was asked of it out of turn."""


@dataclass(frozen=True)
class Refusal:
    code: str
    op_index: int | None
    field: str | None
    message: str
    rule: str

    def as_dict(self) -> dict:
        return {"code": self.code, "op_index": self.op_index, "field": self.field, "message": self.message,
                "rule": self.rule}


@dataclass
class PlanReport:
    """What `validate()` returns beside the refusal list: the normalisations applied, the computed scope, (only
    for a plan with no refusals) the graph diff — edges added/removed and contract fields before/after, per task
    — and `preflight`: one entry per `set_job` operation actually preflighted (`{op_index, ok, digest}`). An
    operation already refused on an earlier ground in the same function (RF-06, RF-07, RF-09, RF-11, RF-14)
    never reaches the preflight call, so it carries no entry; one that does is stored on the plan document
    regardless of whether the plan as a whole validates — the approver reading a `set_job` that passed needs the
    same transcript the refused-by-RF-08 reader gets."""
    normalised: list[dict] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    graph_diff: dict[str, Any] = field(default_factory=dict)
    preflight: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"normalised": self.normalised, "scope": self.scope, "graph_diff": self.graph_diff,
                "preflight": self.preflight}


def _rf(code: str, rule: str, op_index: int | None, field_: str | None, message: str) -> Refusal:
    return Refusal(code=code, op_index=op_index, field=field_, message=message, rule=rule)


def _safe(message: str, layout: Layout) -> str:
    """Any message built from a value rather than written here passes through this. A refusal is written to the
    store, rendered, and quoted into the correction turn's brief (§2.5), so a credential that reached a message
    would be published by the very path RF-21 exists to close."""
    return str(policy_mod.redact(message, layout))[:300]


def _shape_message(name: str, exc: ValidationError, layout: Layout) -> str:
    """A shape refusal built from the error locations alone: `str(ValidationError)` quotes every `input_value`,
    and the field is what a correction turn needs while the value is the thing that must not travel."""
    parts = []
    for e in exc.errors():
        loc = ".".join(str(x) for x in (e.get("loc") or ()))
        parts.append(f"{loc or '<operation>'} ({e.get('type')})")
    return _safe(f"{name} does not match its declared shape: " + "; ".join(dict.fromkeys(parts)), layout)


# ----------------------------------------------------------------------- scanning every string of every operation

def _iter_strings(value: Any, path: str):
    """Every string an operation carries, value and key alike: a credential- or command-shaped field *name* is as
    unscreened as a value in the same position would be, and RF-21/RF-03 bind "every string of every operation",
    not "every value"."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            child = f"{path}.{k}" if path else str(k)
            if isinstance(k, str):
                yield child, k
            yield from _iter_strings(v, child)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _iter_strings(v, f"{path}[{i}]")


def _path_param_fields(op: dict, templates: dict) -> set[str]:
    """Dotted paths, inside one `set_job` operation's own dict, that a declared `path`-typed template parameter
    may legitimately contain a path separator in (RF-03's one exemption). Empty for every other operation and for
    a `set_job` whose template is unknown (RF-05 refuses that on its own)."""
    if op.get("op") != "set_job":
        return set()
    tmpl = (templates.get(str(op.get("template"))) or {})
    params = tmpl.get("params") or {}
    out = set()
    for name, decl in params.items():
        if isinstance(decl, dict) and decl.get("type") == "path":
            out.add(f"params.{name}")
    return out


def _is_contract_content(field_path: str) -> bool:
    """A string that lives inside `create_task.task` or `patch_contract.fields` — a contract field, already
    legal to hold a path (`artifacts`, `owned_paths`) and already checked for that by
    `tasks.check_new`/`_apply_patch`. RF-03's path-separator clause (unlike its metacharacter clause, which still
    runs on this content too) is about a bare field growing a path it was never declared to carry — a task id, a
    template id, a dependency id, a `set_job` parameter — not about the contract's own path fields.

    This is a narrowing of the R0 tripwire and so is stated with it: without it a `create_task` declaring
    `artifacts: ["docs/x.md"]` would be refused for the separator in a field whose whole content is a path. The
    two contract fields where a separator would have bounded or aimed an *execution* — `test_roots` and a check's
    `cwd` — are not covered by the narrowing at all, because no plan may propose either
    (`_executable_content_refusals`). `test_rf03_contract_path_fields_may_hold_a_path` and
    `test_rf03_a_path_outside_contract_content_is_refused` pin each side of the line."""
    return field_path.startswith("task.") or field_path.startswith("fields.")


def _check_tuple(c: Any) -> tuple:
    """A check entry's whole content (command, cwd, timeout_s), whatever shape it arrives in — the task's own
    `Check` model, a plan's dict, or a bare command string — so a restatement is recognised by content and not by
    representation."""
    if isinstance(c, Check):
        return (c.command, c.cwd, c.timeout_s)
    if isinstance(c, dict):
        return (c.get("command"), c.get("cwd"), c.get("timeout_s"))
    return (c, None, None)


def _existing_contract(op: dict, tasks: dict[str, Task], created_tasks: dict[str, Task]) -> Task | None:
    tid = op.get("task")
    return tasks.get(tid) if tid in tasks else created_tasks.get(tid)


def _executable_content_refusals(op_index: int, op: dict, tasks: dict[str, Task],
                                 created_tasks: dict[str, Task]) -> list[Refusal]:
    """RF-03 for the contract's own executable content: a check (a command verification re-runs, with its own
    `cwd`) or a test root (the directory set a declared command may `cwd` into). §1.3's `checks_to_tests` has
    already moved every prose entry out of `checks` by the time this runs, so what is left in it resolves to
    something executable — which is exactly what R0 says a model's output never becomes.

    What counts as *authored* is a diff against the contract the task already carries (its own on the store, or
    the one this same plan is creating): an entry identical to one already there is a restatement, never refused;
    an entry the plan adds is authored, and refused whole regardless of how many of its siblings are old. A
    `create_task` has no existing contract to restate, so every entry it proposes is authored. The refusal names
    the field and how many entries it added, never an entry."""
    if op.get("op") == "create_task":
        content, prefix, existing = op.get("task"), "task", None
    elif op.get("op") == "patch_contract":
        content, prefix, existing = op.get("fields"), "fields", _existing_contract(op, tasks, created_tasks)
    else:
        return []
    if not isinstance(content, dict):
        return []
    out = []
    for key in EXECUTABLE_CONTRACT_FIELDS:
        if key not in content:
            continue
        entries = content[key]
        entries = entries if isinstance(entries, list) else [entries]
        if not entries:
            continue                       # an empty list sets nothing executable; RF-16 judges what it removes
        if key == "checks":
            old = {_check_tuple(c) for c in (existing.checks if existing is not None else [])}
            authored = [e for e in entries if _check_tuple(e) not in old]
        else:
            old = set(existing.test_roots) if existing is not None else set()
            authored = [e for e in entries if e not in old]
        if not authored:
            continue                       # every entry restates one the task's own contract already carries
        what = "check command" if key == "checks" else "test root"
        out.append(_rf("RF-03", "I1 §1.2", op_index, f"{prefix}.{key}",
                       f"{prefix}.{key} proposes {len(authored)} new {what}(s); a command the supervisor runs, "
                       f"the directory it runs in and the roots that bound it are human-written contract data"))
    return out


def _command_shaped_refusals(op_index: int, op: dict, templates: dict, layout: Layout) -> list[Refusal]:
    """`field_path` walks every key and value an operation carries (`_iter_strings`), so it is itself built from
    data a plan controls — a credential-shaped *key* makes `field_path` the thing that must not travel, exactly
    as much as a value would. Both the message and the refusal's own `field` go through `_safe`, never the raw
    path, for that reason."""
    allowed_path_fields = _path_param_fields(op, templates)
    out = []
    for field_path, s in _iter_strings(op, ""):
        if field_path == "op":
            continue
        safe_field = _safe(field_path, layout)
        if any(c in s for c in _SHELL_META):
            out.append(_rf("RF-03", "I1 §1.2", op_index, safe_field,
                           _safe(f"{field_path!r} contains a shell metacharacter or control character", layout)))
            continue
        if (any(c in s for c in _PATH_SEP) and field_path not in allowed_path_fields
                and not _is_contract_content(field_path)):
            out.append(_rf("RF-03", "I1 §1.2", op_index, safe_field,
                           _safe(f"{field_path!r} contains a path separator outside a parameter declared as a "
                                 f"path", layout)))
    return out


def _secret_shaped_refusals(op_index: int, op: dict, layout: Layout) -> list[Refusal]:
    out = []
    for field_path, s in _iter_strings(op, ""):
        if field_path == "op":
            continue
        if policy_mod.find_secrets(s, layout):
            out.append(_rf("RF-21", "I1 §1.2", op_index, _safe(field_path, layout),
                           _safe(f"{field_path!r} holds a secret-shaped value", layout)))
    return out


# ----------------------------------------------------------------------- normalisation (§1.3), closed

def _resolves(first_token: str) -> bool:
    if not first_token:
        return False
    if Path(first_token).is_file():
        return True
    return shutil.which(first_token) is not None


def _normalise_fields(fields: dict, op_index: int, normalised: list[dict],
                      existing_tests: list[str] | None = None,
                      existing_checks: list[Check] | None = None) -> dict:
    """`checks_to_tests`: a `checks` entry whose first token resolves to nothing executable moves to `tests`.
    `existing_tests` (the task's current contract, for `patch_contract` only) is the base the moved entries join
    when the operation's own `fields` does not already state `tests` — so the move itself never trips RF-16 by
    discarding what the contract already carries.

    `existing_checks` is the same correction for the field the move empties: a plan correcting prose it put in
    `checks` did not ask to remove the real command already there, so a check the task already carries and this
    operation does not otherwise restate is merged back into what the move leaves behind — the move itself is
    never read as the one correction turn §2.5 grants being spent on an entry nobody touched."""
    checks = fields.get("checks")
    if not checks:
        return fields
    kept, moved = [], []
    for c in checks:
        command = c.get("command") if isinstance(c, dict) else c
        first = str(command or "").split()[0] if str(command or "").strip() else ""
        (moved if not _resolves(first) else kept).append(c)
    if not moved:
        return fields
    seen = {_check_tuple(c) for c in kept}
    for ec in existing_checks or []:
        if _check_tuple(ec) not in seen:
            kept.append(ec.model_dump(mode="json"))
            seen.add(_check_tuple(ec))
    fields = dict(fields)
    fields["checks"] = kept
    base_tests = fields["tests"] if "tests" in fields else (existing_tests or [])
    fields["tests"] = list(base_tests or []) + [
        (m.get("command") if isinstance(m, dict) else m) for m in moved]
    normalised.append({"code": "checks_to_tests", "op_index": op_index,
                       "detail": f"moved {len(moved)} checks entry(ies) with no resolvable command to tests"})
    return fields


def _normalise_dependency_op(op: dict, op_index: int, tasks: dict[str, Task], normalised: list[dict]) -> dict:
    """`dedupe_dependencies`: a dependency already present, or listed twice. `drop_complete_waits`: a dependency
    naming a task already COMPLETE is dropped — the caller (`_graph_refusals`) is responsible for RF-17 when
    dropping would empty the release, which is checked AFTER normalisation, not here.

    "Already present" is seeded from the target's own `depends_on` for `add_dependency` only (re-adding an edge
    that is already there would be the duplicate): seeding `remove_dependency` the same way would dedupe away
    the one id a removal exists to name — every id worth removing is, by definition, already present. A
    `remove_dependency` therefore only dedupes a repeated id *within its own list* (incidental fix, step 8: the
    four-category fixed-order fixture this module's own tests exercise had never removed a real edge before)."""
    deps = list(op.get("depends_on") or [])
    target = tasks.get(op.get("task"))
    existing = set(target.depends_on) if target is not None else set()
    seed = existing if op.get("op") == "add_dependency" else set()
    out, seen, dropped_dupe, dropped_complete = [], set(seed), [], []
    for d in deps:
        if d in seen:
            dropped_dupe.append(d)
            continue
        seen.add(d)
        t = tasks.get(d)
        if op.get("op") == "add_dependency" and t is not None and t.status == TaskStatus.COMPLETE:
            dropped_complete.append(d)
            continue
        out.append(d)
    if dropped_dupe:
        normalised.append({"code": "dedupe_dependencies", "op_index": op_index,
                           "detail": f"dropped already-present or duplicated id(s): {', '.join(dropped_dupe)}"})
    if dropped_complete:
        normalised.append({"code": "drop_complete_waits", "op_index": op_index,
                           "detail": f"dropped already-COMPLETE id(s): {', '.join(dropped_complete)}"})
    op = dict(op)
    op["depends_on"] = out
    return op


def normalise(doc: dict, tasks: dict[str, Task]) -> tuple[dict, list[dict]]:
    """(the normalised document, the normalisations applied) — the closed list of §1.3, reported and never counted
    as a refusal. Called once, before any refusal is computed, so every refusal check sees the normalised form."""
    normalised: list[dict] = []
    ops = list(doc.get("operations") or [])
    out_ops = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            out_ops.append(op)
            continue
        op = dict(op)
        if op.get("op") in ("add_dependency", "remove_dependency"):
            op = _normalise_dependency_op(op, i, tasks, normalised)
        if op.get("op") == "patch_contract" and isinstance(op.get("fields"), dict):
            target = tasks.get(op.get("task"))
            op["fields"] = _normalise_fields(op["fields"], i, normalised,
                                             existing_tests=list(target.tests) if target is not None else None,
                                             existing_checks=list(target.checks) if target is not None else None)
        if op.get("op") == "create_task" and isinstance(op.get("task"), dict):
            op["task"] = _normalise_fields(op["task"], i, normalised)
        out_ops.append(op)
    doc = dict(doc)
    doc["operations"] = out_ops
    return doc, normalised


# ----------------------------------------------------------------------- templates (a minimal, read-only lookup;
# the loader, the probe and `needs` matching are step 4's own work — this reads only what RF-05/06/07/08/09 need)

def _load_templates(layout: Layout) -> dict:
    p = layout.config / "job_templates.yaml"
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    templates = data.get("templates")
    return templates if isinstance(templates, dict) else {}


# ----------------------------------------------------------------------- the scope rule (§2.3) — from the store

def compute_scope(doc: dict, tasks: dict[str, Task]) -> dict[str, set[str]]:
    """Two scopes, computed from the store and never from the plan: `narrow` (subject + created — what
    `patch_contract` and `set_job` may touch) and `dependency` (narrow + the subject's direct neighbours — what
    `add_dependency`/`remove_dependency` may touch)."""
    subject_id = (doc.get("subject") or {}).get("task")
    created = {op.get("task", {}).get("id") for op in (doc.get("operations") or [])
               if isinstance(op, dict) and op.get("op") == "create_task" and isinstance(op.get("task"), dict)}
    created.discard(None)
    narrow = {subject_id} | created
    neighbours = set()
    if subject_id in tasks:
        neighbours |= set(tasks[subject_id].depends_on)
        neighbours |= {t.id for t in tasks.values() if subject_id in t.depends_on}
    return {"narrow": narrow, "dependency": narrow | neighbours}


# ----------------------------------------------------------------------- per-operation refusals

def _check_create_task(i: int, op: dict, layout: Layout, tasks: dict[str, Task], subject: Task | None) -> list[Refusal]:
    task_doc = op.get("task")
    if not isinstance(task_doc, dict):
        return [_rf("RF-02", "I3 §1.2", i, "task", "create_task.task must be an object (a task contract)")]
    keys = set(task_doc)
    out = []
    forbidden = keys & CREATE_FORBIDDEN_FIELDS
    missing = CREATE_REQUIRED_FIELDS - keys
    if forbidden or missing:
        why = []
        if forbidden:
            why.append(f"sets {', '.join(sorted(forbidden))} (inherited from the subject)")
        if missing:
            why.append(f"is missing {', '.join(sorted(missing))}")
        out.append(_rf("RF-15", "I2 §1.2", i, "task", "create_task.task " + "; ".join(why)))
    unknown = keys - CREATE_ALLOWED_FIELDS - CREATE_FORBIDDEN_FIELDS
    if unknown:
        out.append(_rf("RF-02", "I3 §1.2", i, "task",
                       _safe(f"create_task.task carries {', '.join(sorted(unknown))}, not a contract field",
                             layout)))
    if out:
        return out
    tid = task_doc.get("id")
    if not tid or (tid in tasks):
        return [_rf("RF-02", "I3 §1.2", i, "task.id", f"create_task names id {tid!r}, already a task or empty")]
    combined = dict(tasks)
    inherited = {"phase": subject.phase, "human_approval": subject.human_approval, "escalation": subject.escalation,
                "priority": subject.priority} if subject is not None else {"phase": 0}
    full_doc = {**inherited, **task_doc}
    try:
        _, errors, warnings = tasks_mod.check_new(layout, full_doc, combined)
    except ValidationError as exc:
        return [_rf("RF-02", "I3 §1.2", i, "task", _shape_message("create_task.task", exc, layout))]
    except Exception as exc:                                  # anything else check_new raises
        return [_rf("RF-02", "I3 §1.2", i, "task",
                    _safe(f"create_task.task is not a valid task contract: {type(exc).__name__}", layout))]
    for e in errors:
        code = "RF-17" if "cycle" in e else "RF-02"
        out.append(_rf(code, "I3 §1.2" if code == "RF-02" else "I2 §1.2", i, "task", _safe(e, layout)))
    if not out:
        # RF-31's `may_be_claimed_by` half: a plan may name
        # only a resource the project opened to plans. Checked on the **effective** need (no job block exists on
        # a create_task, so this is the task's own `needs.resource` alone) so the permission stays the project's
        # line of data, never something this write grants itself.
        from . import needs as needs_mod
        refusal = needs_mod.plan_claim_refusal(layout, needs_mod.effective(full_doc.get("needs") or {}, {}))
        if refusal:
            out.append(_rf("RF-31", "I2 §1.2", i, "task.needs.resource", _safe(refusal, layout)))
    if not out and warnings:
        task_obj = Task.model_validate({**full_doc, "status": "NOT_STARTED", "attempts": [], "result": None,
                                        "paths": []})
        refusals = tasks_mod.contract_warnings([task_obj], layout)
        for w in refusals:
            out.append(_rf("RF-18", "I2 §1.2", i, "task", _safe(w, layout)))
    return out


def _check_dependency_op(i: int, op: dict, tasks: dict[str, Task], scope: dict[str, set[str]]) -> list[Refusal]:
    """§2.3: scope bounds which task an `add_dependency`/`remove_dependency` may *edit* (`op.task`) — a direct
    neighbour, the subject or a task this plan created. A `depends_on` target is not itself scope-checked: adding
    or removing an edge never touches the target task's own contract or state, and a brand-new edge's far end is,
    by definition, not yet a neighbour."""
    out = []
    tid = op.get("task")
    deps = op.get("depends_on")
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        return [_rf("RF-02", "I3 §1.2", i, "depends_on", f"{op.get('op')}.depends_on must be a list of task ids")]
    if tid not in scope["dependency"]:
        out.append(_rf("RF-13", "I2 §1.2", i, "task", f"{op.get('op')} names {tid!r}, outside the plan's scope"))
    t = tasks.get(tid)
    if t is not None and t.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
        out.append(_rf("RF-14", "I2 §1.2", i, "task", f"{op.get('op')} edits {tid!r}, which is {t.status.value}"))
    return out


def _check_patch_contract(i: int, op: dict, layout: Layout, tasks: dict[str, Task],
                          scope: dict[str, set[str]], created_tasks: dict[str, Task]) -> list[Refusal]:
    """A patch to a task this same plan creates is checked against the contract the plan is creating it with —
    monotonicity, the RF-02 shape check and RF-18 all see the task as it would exist once applied, not as `None`
    (a created task parked forever on an unconfigured role is exactly what RF-18 refuses on `create_task` itself;
    the patch path offers the same stall through a second operation)."""
    out = []
    tid = op.get("task")
    fields = op.get("fields")
    if not isinstance(fields, dict):
        return [_rf("RF-02", "I3 §1.2", i, "fields", "patch_contract.fields must be an object")]
    if tid not in scope["narrow"]:
        out.append(_rf("RF-13", "I2 §1.2", i, "task", f"patch_contract names {tid!r}, outside the plan's scope"))
    t = tasks.get(tid)
    if t is None:
        t = created_tasks.get(tid)
    if t is not None and t.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
        out.append(_rf("RF-14", "I2 §1.2", i, "task", f"patch_contract edits {tid!r}, which is {t.status.value}"))
    for key in fields:
        if key in AUTHORITY_FIELDS:
            out.append(_rf("RF-15", "I2 §1.2", i, f"fields.{key}", f"patch_contract may not touch {key!r}"))
        elif key == "acceptance":
            out.append(_rf("RF-16", "I2 §1.2", i, "fields.acceptance", "patch_contract may not revise acceptance"))
        elif key not in PATCH_ALLOWED_FIELDS:
            out.append(_rf("RF-02", "I3 §1.2", i, f"fields.{key}",
                           _safe(f"patch_contract.fields may not set {key!r} (through the dependency ops only, "
                                 f"or not at all)", layout)))
    if out or t is None:
        return out
    out += _monotonicity_refusals(i, t, fields)
    if out:
        return out
    out += _patched_contract_refusals(i, t, fields, layout)
    return out


def _patched_contract_refusals(i: int, t: Task, fields: dict, layout: Layout) -> list[Refusal]:
    """RF-18 on the patch path, not only on a created task. `patch_contract` may add a `reviewer`, and a role
    routing does not configure parks the patched task exactly as it would park a new one — the reason RF-18
    exists. Only what this patch *adds* is refused: a task already carrying a warning (its own role was
    deconfigured, a check a human wrote is outside the allow-list) is not made unpatchable by it.

    The patched contract is built by validating it, which is also how `tasks.patch_task` would see it (§2.2), so a
    patch whose value is of the wrong type is RF-02 here rather than a failure at apply time."""
    try:
        patched = Task.model_validate({**t.model_dump(mode="json"), **fields})
    except ValidationError as exc:
        return [_rf("RF-02", "I3 §1.2", i, "fields", _shape_message("patch_contract.fields", exc, layout))]
    before = set(tasks_mod.contract_warnings([t], layout))
    return [_rf("RF-18", "I2 §1.2", i, "fields", _safe(w, layout))
            for w in tasks_mod.contract_warnings([patched], layout) if w not in before]


def _monotonicity_refusals(i: int, t: Task, fields: dict) -> list[Refusal]:
    out = []
    for key in PATCH_ADD_ONLY_FIELDS:
        if key not in fields:
            continue
        if key == "checks":
            old = {c.command for c in t.checks}
            new = {(c.get("command") if isinstance(c, dict) else c) for c in (fields[key] or [])}
        else:
            old = set(getattr(t, key) or [])
            new = set(fields[key] or [])
        if not old <= new:
            out.append(_rf("RF-16", "I2 §1.2", i, f"fields.{key}",
                           f"patch_contract removes or replaces an existing {key} entry ({sorted(old - new)})"))
    if "evidence" in fields:
        old_v = EVIDENCE_ORDER.get(t.evidence.value if hasattr(t.evidence, "value") else t.evidence)
        new_val = EVIDENCE_ORDER.get(str(fields["evidence"]))
        if old_v is None or new_val is None or new_val < old_v:
            out.append(_rf("RF-16", "I2 §1.2", i, "fields.evidence", "patch_contract loosens evidence"))
    if "owned_paths" in fields:
        new_paths = fields["owned_paths"] or []
        old_paths = t.owned_paths or []
        if EXCLUSIVE in new_paths and EXCLUSIVE not in old_paths:
            out.append(_rf("RF-16", "I2 §1.2", i, "fields.owned_paths", "patch_contract widens owned_paths to exclusive"))
        elif old_paths and EXCLUSIVE not in old_paths:
            from ._vendor.contract import contained
            for p in new_paths:
                if not contained(p, old_paths):
                    out.append(_rf("RF-16", "I2 §1.2", i, "fields.owned_paths",
                                   f"patch_contract widens owned_paths beyond {old_paths} ({p!r})"))
                    break
    return out


def _set_job_block(existing: dict, tmpl: dict, template_id: Any, params: dict) -> dict:
    """The one block `set_job` ever writes — the existing job's own keys (a parameter diff's inherited
    `timeout_s`, `stdin_file`, ...; nothing at all for a task this plan created, its first job), `adapter`
    carried over or else taken from the template, `template`/`params` replaced by this operation's own, and
    `preflight` dropped (`tasks_mod._update` writes a fresh one once the block lands). `_check_set_job`'s own
    preflight simulation and `_effect_set_job`'s actual write both build this block the same way, so the
    transcript recorded on the plan is always the transcript for what the applier is actually about to run."""
    existing = dict(existing)
    existing.pop("preflight", None)
    return {**existing, "adapter": existing.get("adapter") or tmpl.get("adapter"),
            "template": template_id, "params": dict(params)}


def _preflight_transcript_entry(i: int, layout: Layout, block: dict, task=None) -> tuple[Refusal | None, dict]:
    """Runs the real preflight (`supervisor.preflight.run`, the same check a write-time job block gets) against
    `block` — the exact block this operation would write, built by `_set_job_block` — never the weaker
    standalone checks this validator used to run on its own: a template whose declared probe fails, whose argv
    does not resolve, or whose inherited `timeout_s`/`stdin_file` no longer holds is otherwise invisible until
    the applier's own write triggers it — a stall, not a named refusal (RF-08). `task` is the stored task this
    operation writes onto, or None for one the plan creates in the same document: the preflight's declared-needs
    match reads the task's own `needs` beside the block's template, so a block whose effective need the lane
    contradicts is refused with the plan and stores nothing on any
    task. The transcript is never stored
    whole (it can carry a probe's own stdout); only its digest travels, onto `PlanReport.preflight` and from
    there onto the plan document itself, so the record a plan's own row promises survives a refusal
    as well as a pass."""
    from . import preflight as preflight_mod
    pf = preflight_mod.run(layout, block, task)
    digest = hashlib.sha256(json.dumps(pf, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    entry = {"op_index": i, "ok": bool(pf.get("ok")), "digest": digest}
    if pf.get("ok"):
        return None, entry
    detail = pf.get("detail") or {}
    refusal = _rf("RF-08", "I1 §1.2", i, "template",
                 _safe(f"set_job's preflight failed ({detail.get('code', 'unknown')}): {detail.get('message', '')}",
                       layout))
    return refusal, entry


def _check_set_job(i: int, op: dict, layout: Layout, tasks: dict[str, Task], scope: dict[str, set[str]],
                   created: set[str], templates: dict) -> tuple[list[Refusal], dict | None]:
    tid = op.get("task")
    template_id = op.get("template")
    params = op.get("params")
    out = []
    if not isinstance(params, dict):
        return [_rf("RF-02", "I3 §1.2", i, "params", "set_job.params must be an object")], None
    if tid not in scope["narrow"]:
        return ([_rf("RF-12", "I2 §1.2", i, "task",
                     f"set_job names {tid!r}, neither the subject nor created by this plan")], None)
    t = tasks.get(tid)
    if t is not None and t.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
        out.append(_rf("RF-14", "I2 §1.2", i, "task", f"set_job edits {tid!r}, which is {t.status.value}"))
    tmpl = templates.get(str(template_id))
    if tmpl is None:
        out.append(_rf("RF-05", "I1 §1.2", i, "template", f"template {template_id!r} is not in config/job_templates.yaml"))
        return out, None
    may = tmpl.get("may_be_set_by") or []
    if "plan" not in may:
        out.append(_rf("RF-09", "I2 §1.2", i, "template", f"template {template_id!r} is not may_be_set_by: [plan]"))
    existing_job: dict | None = None
    if tid not in created and t is not None:
        existing_job = t.job.model_dump(exclude_none=True) if t.job is not None else None
        if not isinstance(existing_job, dict) or not existing_job.get("adapter"):
            out.append(_rf("RF-11", "I2 §1.2", i, "task", f"{tid!r} has no existing job; set_job may not give it a first one"))
        elif not existing_job.get("template") or existing_job.get("argv") or existing_job.get("fn"):
            # A job with no `template` is a literal, owner-authored block (a bare argv or fn, never a plan's
            # own to replace): the two RF-11 branches below both guard on the existing value, so a job that
            # carries none would trip neither, and the applier would write the new template/params beside the
            # stale literal it never removes — a third shape the frontier does not admit.
            out.append(_rf("RF-11", "I2 §1.2", i, "template",
                           f"{tid!r}'s existing job carries no template (a literal, owner-authored block); "
                           "set_job may not replace it"))
        else:
            if tmpl.get("adapter") and existing_job["adapter"] != tmpl["adapter"]:
                out.append(_rf("RF-11", "I2 §1.2", i, "template", f"set_job would change {tid!r}'s adapter"))
            existing_template = existing_job.get("template")
            if existing_template != template_id:
                out.append(_rf("RF-11", "I2 §1.2", i, "template",
                               f"set_job would change {tid!r}'s template from {existing_template!r}"))
    decl = tmpl.get("params") or {}
    unknown = set(params) - set(decl)
    if unknown:
        out.append(_rf("RF-06", "I1 §1.2", i, "params",
                       _safe(f"set_job.params names {', '.join(sorted(unknown))}, not declared by {template_id!r}",
                             layout)))
    for name, value in params.items():
        slot = decl.get(name)
        if not isinstance(slot, dict):
            continue
        out += _param_refusals(i, name, value, slot, layout)
    preflight_entry = None
    if not out:
        # RF-31's `may_be_claimed_by` half: checked on the
        # **effective** need -- the task's own `needs.resource` (when `t` is stored; a task this same plan is
        # also creating has none visible here, the existing gap `needs.results_for(layout, None, ...)` already
        # has below) beside *this* template's own, so a plan cannot launder an owner-only claim onto itself by
        # selecting a template that carries it, even though the plan itself writes no resource name.
        from . import needs as needs_mod
        template_needs = tmpl.get("needs") if isinstance(tmpl.get("needs"), dict) else {}
        task_needs = t.needs.model_dump(exclude_none=True) if (t is not None and t.needs) else {}
        refusal = needs_mod.plan_claim_refusal(layout, needs_mod.effective(task_needs, template_needs))
        if refusal:
            out.append(_rf("RF-31", "I2 §1.2", i, "template.needs.resource", _safe(refusal, layout)))
    if not out:
        # Every earlier check in this function passed, so this is the one shape worth the cost of actually
        # running the preflight: an operation already refused on another ground (RF-06, RF-07, RF-09, RF-11,
        # RF-14) would only have that same underlying problem reported twice, under two codes, for one cause.
        # The block simulated here is `_set_job_block`'s own merge, the same one `_effect_set_job` writes, so
        # whatever the existing job inherited (a parameter diff) or the template declares (a created task's
        # first job) is exactly what gets preflighted.
        block = _set_job_block(existing_job or {}, tmpl, template_id, params)
        refusal, preflight_entry = _preflight_transcript_entry(i, layout, block, t)
        if refusal is not None:
            out.append(refusal)
    return out, preflight_entry


def _param_refusals(op_index: int, name: str, value: Any, slot: dict, layout: Layout) -> list[Refusal]:
    out = []
    field_path = f"params.{name}"
    ptype = slot.get("type")
    if isinstance(value, str) and value.startswith("-") and not slot.get("allow_leading_dash"):
        out.append(_rf("RF-06", "I1 §1.2", op_index, field_path, f"{field_path!r} begins with '-', not allowed for this slot"))
        return out
    if ptype == "enum" and value not in (slot.get("values") or []):
        out.append(_rf("RF-06", "I1 §1.2", op_index, field_path, f"{field_path!r} is not one of {slot.get('values')}"))
    elif ptype == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            out.append(_rf("RF-06", "I1 §1.2", op_index, field_path, f"{field_path!r} must be an int"))
        else:
            if slot.get("min") is not None and value < slot["min"]:
                out.append(_rf("RF-06", "I1 §1.2", op_index, field_path, f"{field_path!r} is below min {slot['min']}"))
            if slot.get("max") is not None and value > slot["max"]:
                out.append(_rf("RF-06", "I1 §1.2", op_index, field_path, f"{field_path!r} is above max {slot['max']}"))
    elif ptype == "path":
        from ._vendor.contract import contained
        roots = slot.get("roots") or []
        if not isinstance(value, str) or not roots or not contained(value, roots, layout.root):
            out.append(_rf("RF-07", "I1 §1.2", op_index, field_path, f"{field_path!r} does not resolve inside its declared roots"))
        elif slot.get("must_exist") and not (layout.root / value).exists():
            out.append(_rf("RF-08", "I1 §1.2", op_index, field_path, f"{field_path!r} does not exist"))
    pattern = slot.get("pattern")
    if pattern and isinstance(value, str):
        import re
        if not re.fullmatch(pattern, value):
            out.append(_rf("RF-06", "I1 §1.2", op_index, field_path, f"{field_path!r} does not match the declared pattern"))
    return out


def _check_resume(i: int, op: dict, subject_id: str) -> list[Refusal]:
    out = []
    if op.get("task") != subject_id:
        out.append(_rf("RF-13", "I2 §1.2", i, "task", "resume must name the subject"))
    as_ = op.get("as", "ready")
    if as_ not in ("ready", "blocked_on_dependencies"):
        out.append(_rf("RF-02", "I3 §1.2", i, "as", f"resume.as {as_!r} is not ready or blocked_on_dependencies"))
    return out


def _build_created_tasks(doc: dict, tasks: dict[str, Task]) -> dict[str, Task]:
    """Every `create_task` operation's task, built once as the `Task` it would be once applied — the same object
    a patch to it (RF-18, monotonicity, RF-02) and the dependency graph (`_graph_refusals`) both need, built once
    so the two never disagree about what a created task's contract holds. An operation whose own `task` does not
    validate contributes nothing here; `_check_create_task` is what refuses it."""
    subject_id = (doc.get("subject") or {}).get("task")
    subject = tasks.get(subject_id)
    inherited = {"phase": subject.phase, "human_approval": subject.human_approval, "escalation": subject.escalation,
                "priority": subject.priority} if subject is not None else {"phase": 0}
    created: dict[str, Task] = {}
    for op in doc.get("operations") or []:
        if not isinstance(op, dict):
            continue
        if op.get("op") == "create_task" and isinstance(op.get("task"), dict) and op["task"].get("id"):
            try:
                created[op["task"]["id"]] = Task.model_validate({**inherited, **op["task"], "status": "NOT_STARTED",
                                                                  "attempts": [], "result": None, "paths": []})
            except Exception:
                continue
    return created


def _graph_refusals(doc: dict, tasks: dict[str, Task], created_tasks: dict[str, Task]) -> list[Refusal]:
    """Simulate every add_dependency/remove_dependency on a copy of the graph and check for a cycle, or a subject
    left waiting on nothing (or only on tasks already COMPLETE) when `resume.as == blocked_on_dependencies`."""
    out = []
    graph = dict(tasks)
    graph.update(created_tasks)
    subject_id = (doc.get("subject") or {}).get("task")
    for op in doc.get("operations") or []:
        if not isinstance(op, dict) or op.get("op") not in ("add_dependency", "remove_dependency"):
            continue
        tid = op.get("task")
        if tid not in graph:
            continue
        deps = list(graph[tid].depends_on)
        for d in op.get("depends_on") or []:
            if op["op"] == "add_dependency" and d not in deps:
                deps.append(d)
            elif op["op"] == "remove_dependency" and d in deps:
                deps.remove(d)
        graph[tid] = graph[tid].model_copy(update={"depends_on": deps})
    for e in tasks_mod.validate_graph(graph):
        if "cycle" in e:
            out.append(_rf("RF-17", "I3 §1.2", None, "operations", e))
    resume_ops = [op for op in doc.get("operations") or [] if isinstance(op, dict) and op.get("op") == "resume"]
    if resume_ops and resume_ops[-1].get("as") == "blocked_on_dependencies" and subject_id in graph:
        waits = graph[subject_id].depends_on
        live = [d for d in waits if d not in graph or graph[d].status != TaskStatus.COMPLETE]
        if not live:
            out.append(_rf("RF-17", "I3 §1.2", None, "operations",
                           "resume.as is blocked_on_dependencies but the subject would wait on nothing unfinished"))
    return out


# ----------------------------------------------------------------------- the envelope and the entry point

def _envelope_refusals(doc: Any) -> list[Refusal]:
    if not isinstance(doc, dict):
        return [_rf("RF-02", "I3 §1.2", None, None, "a plan must be a document (a mapping)")]
    out = []
    if not isinstance(doc.get("id"), str) or not doc.get("id", "").strip():
        out.append(_rf("RF-02", "I3 §1.2", None, "id", "a plan must carry a non-empty id"))
    subject = doc.get("subject")
    if not isinstance(subject, dict) or not isinstance(subject.get("task"), str) or not subject.get("task"):
        out.append(_rf("RF-02", "I3 §1.2", None, "subject", "a plan must carry subject.task"))
    if not isinstance(doc.get("operations"), list):
        out.append(_rf("RF-02", "I3 §1.2", None, "operations", "operations must be a list"))
    if doc.get("plan_schema") != PLAN_SCHEMA:
        out.append(_rf("RF-01", "I2 §1.2", None, "plan_schema", f"plan_schema {doc.get('plan_schema')!r} is not one this release knows"))
    return out


def validate(doc: dict, layout: Layout, tasks: dict[str, Task] | None = None) -> tuple[PlanReport, list[dict]]:
    """(report, refusals) for `doc` (a plan document, already loaded from YAML). `tasks` defaults to the live
    store (`tasks_mod.load_all`); passing a snapshot lets a caller validate against the graph as it will be after
    an earlier step of the same turn, exactly as `tasks.check_new` already allows. Never raises on a malformed
    document — malformed is RF-02, not an exception — and never writes anything, anywhere: there is no applier in
    this module. A plan tripping any refusal is reported refused whole: `refusals` is non-empty and nothing in
    `report.graph_diff` should be read as describing an applied change."""
    tasks = tasks_mod.load_all(layout) if tasks is None else tasks
    refusals = _envelope_refusals(doc)
    if refusals:
        return PlanReport(), [r.as_dict() for r in refusals]

    normalised_doc, normalised = normalise(doc, tasks)
    ops = normalised_doc.get("operations") or []
    templates = _load_templates(layout)
    subject_id = normalised_doc["subject"]["task"]
    scope = compute_scope(normalised_doc, tasks)
    # scope["narrow"] carries every create_task id named, whether or not that op itself is a valid create (an id
    # already in the store is refused on its own ground, RF-02's own check); excluding what the store already
    # knows keeps set_job's RF-11 identity block closed on its own, not only as a side effect of that refusal.
    created = (scope["narrow"] - {subject_id}) - set(tasks)
    created_tasks = _build_created_tasks(normalised_doc, tasks)

    out: list[Refusal] = []
    preflight_entries: list[dict] = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            out.append(_rf("RF-02", "I3 §1.2", i, "op", "every operation must be an object naming op"))
            continue
        name = op["op"]
        if name in OWNER_ONLY_EFFECTS:
            out.append(_rf("RF-26", "I2 §1.2", i, "op", f"{name!r} is an owner-only effect, never a plan operation"))
            continue
        if name not in OPERATIONS:
            out.append(_rf("RF-01", "I2 §1.2", i, "op", f"{name!r} is outside the closed vocabulary"))
            continue
        # The value scans first, and whatever the shape check then decides: RF-03 runs on every string of every
        # operation (§1.2), which an operation skipped for its shape would not be, and an operation carrying a
        # credential must reach RF-21 before anything builds a message out of it.
        scanned = (_command_shaped_refusals(i, op, templates, layout) + _secret_shaped_refusals(i, op, layout)
                   + _executable_content_refusals(i, op, tasks, created_tasks))
        out += scanned
        try:
            _OP_MODELS[name].model_validate(op)
        except ValidationError as exc:
            code = "RF-04" if name == "set_job" else "RF-02"
            rule = "I1 §1.2" if code == "RF-04" else "I3 §1.2"
            out.append(_rf(code, rule, i, None, _shape_message(name, exc, layout)))
            continue
        if scanned:
            continue        # refused whole either way; its own checks would only read a value already refused
        if name == "create_task":
            out += _check_create_task(i, op, layout, tasks, tasks.get(subject_id))
        elif name in ("add_dependency", "remove_dependency"):
            out += _check_dependency_op(i, op, tasks, scope)
        elif name == "patch_contract":
            out += _check_patch_contract(i, op, layout, tasks, scope, created_tasks)
        elif name == "set_job":
            job_refusals, pf_entry = _check_set_job(i, op, layout, tasks, scope, created, templates)
            out += job_refusals
            if pf_entry is not None:
                preflight_entries.append(pf_entry)
        elif name == "resume":
            out += _check_resume(i, op, subject_id)

    # The plan-level refusals run on every document, including one carrying an owner-only effect: §2.5 grants the
    # proposer exactly one correction turn on this list, and a list truncated by the first refusal found spends it.
    resume_ops = [op for op in ops if isinstance(op, dict) and op.get("op") == "resume"]
    if len(resume_ops) != 1:
        out.append(_rf("RF-02", "I3 §1.2", None, "operations", "a plan must carry exactly one resume operation"))
    set_job_ops = [op for op in ops if isinstance(op, dict) and op.get("op") == "set_job"]
    creates = [op for op in ops if isinstance(op, dict) and op.get("op") == "create_task"]
    cfg = _plan_limits(layout)
    if len(ops) > cfg["max_ops"]:
        out.append(_rf("RF-10", "I2 §1.2", None, "operations", f"{len(ops)} operations exceeds plan.max_ops ({cfg['max_ops']})"))
    if len(creates) > cfg["max_creates"]:
        out.append(_rf("RF-10", "I2 §1.2", None, "operations", f"{len(creates)} creates exceeds plan.max_creates ({cfg['max_creates']})"))
    if len(set_job_ops) > 1:
        out.append(_rf("RF-10", "I2 §1.2", None, "operations", "more than one set_job in a plan"))
    out += _graph_refusals(normalised_doc, tasks, created_tasks)
    out += _subject_refusals(normalised_doc, layout, tasks)

    refusal_dicts = [r.as_dict() for r in out]
    report = PlanReport(normalised=normalised, scope=sorted(scope["dependency"]), preflight=preflight_entries)
    if not refusal_dicts:
        report.graph_diff = _graph_diff(normalised_doc, tasks)
    return report, refusal_dicts


def _plan_limits(layout: Layout) -> dict:
    from . import gate as gate_mod
    doc = gate_mod.routing_doc(layout).get("plan") or {}
    return {"max_ops": int(doc.get("max_ops") or DEFAULT_MAX_OPS),
            "max_creates": int(doc.get("max_creates") or DEFAULT_MAX_CREATES),
            "max_resumes": int(doc.get("max_resumes") or DEFAULT_MAX_RESUMES)}


def _subject_refusals(doc: dict, layout: Layout, tasks: dict[str, Task]) -> list[Refusal]:
    from . import jobs as jobs_mod
    out = []
    subject = doc["subject"]
    subject_id = subject["task"]
    t = tasks.get(subject_id)
    if t is None:
        return [_rf("RF-19", "I3 §1.2", None, "subject", f"subject {subject_id!r} no longer exists")]
    if t.held:
        out.append(_rf("RF-22", "I2 §1.2", None, "subject", f"subject {subject_id!r} is held by the owner"))
    if t.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
        out.append(_rf("RF-20", "I3 §1.2", None, "subject", f"subject {subject_id!r} is {t.status.value}"))
    elif any(r.get("phase") in jobs_mod.OPEN_PHASES for r in jobs_mod.for_task(layout, subject_id)):
        out.append(_rf("RF-20", "I3 §1.2", None, "subject", f"subject {subject_id!r} has an open job"))
    expected = doc.get("expected_rev") or {}
    if subject_id not in expected:
        # A plan that declares no rev for its own subject got no staleness check at all. §2.1 annotates
        # `expected_rev` "every task the plan reads or writes", and the subject is read and written by every plan.
        out.append(_rf("RF-19", "I3 §1.2", None, "expected_rev",
                       f"expected_rev does not name the subject {subject_id!r}, so nothing checks it is unchanged"))
    for tid, rev in expected.items():
        if tid not in tasks or tasks[tid].rev != rev:
            out.append(_rf("RF-19", "I3 §1.2", None, "expected_rev",
                           f"expected_rev names {tid!r} at rev {rev}, the store disagrees"))
    current = tasks_mod.current_attempt(layout, subject_id)
    if current is not None and subject.get("attempt") is not None and current != subject["attempt"]:
        out.append(_rf("RF-19", "I3 §1.2", None, "subject.attempt",
                       f"subject.attempt {subject['attempt']} is no longer the newest ({current})"))
    return out


def _plain(value: Any) -> Any:
    """`before`, straight off the `Task` model, is whatever pydantic actually built — a list of `Check` model
    objects for `checks`, an `Evidence` enum member for `evidence` — not the plain data `yaml.safe_dump` (what
    `store.dump` runs on the whole plan document) can write. The same conversion `_normalise_fields` already
    applies to `existing_checks`, generalised to every value a patch's `before` can hold: every member of
    PATCH_ALLOWED_FIELDS is a plain string, a list of plain strings, a list of `Check`, or an `Evidence` member —
    checked against the `Task` model directly rather than assumed — so a list is walked, a `BaseModel` is
    `model_dump(mode="json")`, an `Enum` member is its `.value`, and anything else (already plain) passes through."""
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    return value


def _graph_diff(doc: dict, tasks: dict[str, Task]) -> dict:
    added, removed, patched, created = [], [], [], []
    for op in doc.get("operations") or []:
        if not isinstance(op, dict):
            continue
        if op.get("op") == "add_dependency":
            for d in op.get("depends_on") or []:
                added.append([op.get("task"), d])
        elif op.get("op") == "remove_dependency":
            for d in op.get("depends_on") or []:
                removed.append([op.get("task"), d])
        elif op.get("op") == "patch_contract":
            t = tasks.get(op.get("task"))
            before = {k: _plain(getattr(t, k)) for k in (op.get("fields") or {})} if t else {}
            patched.append({"task": op.get("task"), "before": before, "after": op.get("fields")})
        elif op.get("op") == "create_task":
            created.append((op.get("task") or {}).get("id"))
    return {"edges_added": added, "edges_removed": removed, "contract_patches": patched, "created": created}


# ----------------------------------------------------------------------- RF-23, RF-24, RF-25 — pure, ahead of
# the approval lane (step 8) and the inbox's typed choices (step 7), which will call these once they exist.

def self_approval_refusal(proposer_role: str, approver_role: str) -> dict | None:
    """RF-23 `self_approval`: the approver resolved for a plan is never the role that proposed it."""
    if proposer_role == approver_role:
        return _rf("RF-23", "I2 §1.2", None, "approver", f"{approver_role!r} proposed this plan and may not approve it").as_dict()
    return None


def choice_not_offered_refusal(choice_ids: list[str], answered: str) -> dict | None:
    """RF-24 `choice_not_offered`: an inbox answer naming a choice the item does not carry."""
    if answered not in choice_ids:
        return _rf("RF-24", "I3 §1.2", None, "choice", f"{answered!r} is not one of this item's choices {choice_ids}").as_dict()
    return None


def choice_stale_refusal(shown_digest: str, answered_digest: str) -> dict | None:
    """RF-25 `choice_stale`: an answer whose choices_digest no longer matches the item as shown."""
    if shown_digest != answered_digest:
        return _rf("RF-25", "I3 §1.2", None, "choices_digest", "this item changed, reload").as_dict()
    return None


def choices_digest(choices: list[dict]) -> str:
    return hashlib.sha256(yaml.safe_dump(choices, sort_keys=True).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------- a choice's label and detail, rendered
# from its own effect ("the label is rendered *from* the effect") — the one
# function `inbox.create()` calls for every choice it is offered, so a stored label can never promise more than
# its own effect performs. Covers the closed, twelve-effect vocabulary (OPERATIONS, OWNER_ONLY_EFFECTS) only;
# `None` for anything else, which `inbox.create()` turns into a refusal rather than a blank button.

def _op_phrase(op: dict) -> str | None:
    name = op.get("op")
    if name == "create_task":
        tid = (op.get("task") if isinstance(op.get("task"), str) else (op.get("task") or {}).get("id")) or "?"
        return f"create {tid}"
    if name == "add_dependency":
        return f"make {op.get('task', '?')} depend on {', '.join(op.get('depends_on') or []) or '?'}"
    if name == "remove_dependency":
        return f"remove {op.get('task', '?')}'s dependency on {', '.join(op.get('depends_on') or []) or '?'}"
    if name == "patch_contract":
        fields = ", ".join(sorted((op.get("fields") or {}).keys())) or "?"
        return f"update {op.get('task', '?')}'s contract ({fields})"
    if name == "set_job":
        return f"set {op.get('task', '?')}'s job to {op.get('template', '?')}"
    if name == "resume":
        as_ = op.get("as", "ready")
        if as_ == "ready":
            return f"resume {op.get('task', '?')}"
        if as_ == "blocked_on_dependencies":
            return f"unblock {op.get('task', '?')} once its dependencies finish"
        return f"resume {op.get('task', '?')} ({as_})"
    if name == "apply_plan":
        return f"apply plan {op.get('plan', '?')}"
    if name == "cancel_plan":
        return f"cancel plan {op.get('plan', '?')}"
    if name == "cancel_task":
        return f"cancel {op.get('task', '?')}"
    if name == "hold":
        return f"hold {op.get('task', '?')}"
    if name == "release_hold":
        return f"release the hold on {op.get('task', '?')}"
    if name == "diagnose_now":
        return f"run diagnosis on {op.get('task', '?')} now"
    return None


def render_choice(effect: Any) -> tuple[str | None, str | None]:
    """(label, detail) for a choice's `effect` — one operation, an owner-only effect on its own, or a list of
    plan operations applied as one transaction — derived entirely from the operation(s) it names. A single
    operation's phrase, capitalised, is the label; two or more are joined "; then " for the label and numbered
    for the detail. `(None, None)` when `effect` is not a non-empty operation or list of them, or names an
    operation outside the closed vocabulary — never a guess at what an unknown operation might mean."""
    if isinstance(effect, dict):
        ops = [effect]
    elif isinstance(effect, list) and effect:
        ops = list(effect)
    else:
        return None, None
    if any(not isinstance(op, dict) or not isinstance(op.get("op"), str) for op in ops):
        return None, None
    phrases = [_op_phrase(op) for op in ops]
    if any(p is None for p in phrases):
        return None, None
    sentences = [p[0].upper() + p[1:] for p in phrases]
    if len(sentences) == 1:
        return sentences[0], None
    return "; then ".join(sentences), "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sentences))


# ----------------------------------------------------------------------- the authority classes and the approval
# lane: validation proves a plan is
# permitted, never that it is the right one, so a second, cheap reader sits between a validated plan and the
# applier for everything but routine repair. Three classes, the mapping held in `config/routing.yaml` under
# `plan.authority` so a project may tighten it without a release (never loosen: `_tightened` only ever raises an
# operation's class, never lowers one the vocabulary itself fixed).

CLASS_A, CLASS_B, CLASS_OWNER = "A", "B", "owner"
_CLASS_RANK = {CLASS_A: 0, CLASS_B: 1, CLASS_OWNER: 2}
# The four dependency/contract/release operations are routine repair (A); create_task is shaping work (B) unless
# the lane it names is metered, which raises it to the owner class (§2.4); set_job is a parameter diff on an
# existing task (A, RF-11 having already refused anything else) or takes the class of the create_task it
# accompanies on a task this same plan creates (§2.7: "set_job ... takes the class of the create_task it
# accompanies on a created one"). An operation outside this table is not this release's to classify.
_BASE_OPERATION_CLASS = {"add_dependency": CLASS_A, "remove_dependency": CLASS_A, "patch_contract": CLASS_A,
                         "resume": CLASS_A, "create_task": CLASS_B, "set_job": CLASS_A}
# "Metered", restated from §2.4: paid (API credits) and human (the owner's own hours) always are; anything else a
# project lists under `limits.resource_cost_categories` (the project's single resource slot) is too.
BUILTIN_METERED_CATEGORIES = frozenset({"paid", "human"})


def metered(layout: Layout | None, cost_category: Any) -> bool:
    cat = str(getattr(cost_category, "value", cost_category) or "")
    if cat in BUILTIN_METERED_CATEGORIES:
        return True
    if layout is None:
        return False
    from . import gate as gate_mod
    return cat in gate_mod.limits(layout)["resource_cost_categories"]


def _configured_class(layout: Layout | None, op_name: str) -> str | None:
    if layout is None:
        return None
    from . import gate as gate_mod
    cfg = (gate_mod.routing_doc(layout).get("plan") or {}).get("authority") or {}
    cls = cfg.get(op_name) if isinstance(cfg, dict) else None
    return cls if cls in _CLASS_RANK else None


def _tightened(layout: Layout | None, op_name: str, cls: str) -> str:
    configured = _configured_class(layout, op_name)
    return configured if configured is not None and _CLASS_RANK[configured] > _CLASS_RANK[cls] else cls


def _plan_duration_limit_min(layout: Layout | None, task_doc: dict) -> int | None:
    """`plan.max_duration_min`, or its unconfigured default
    -- the created task's own lane policy cap in minutes, from `timeouts.resolve` called with no `needs` and no
    `job` block, so the limit is the policy cap the chain yields and never the plan's own declared duration
    (passing the declaration would make the limit equal it, so it could never bind). `None` when `layout` is not
    given: no project to resolve a lane's policy against."""
    if layout is None:
        return None
    from . import gate as gate_mod
    from . import scheduler as scheduler_mod
    from . import timeouts as timeouts_mod
    from . import capabilities as capabilities_mod
    routing = gate_mod.routing_doc(layout)
    configured = (routing.get("plan") or {}).get("max_duration_min")
    if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
        return configured
    adapter = scheduler_mod.default_adapter_for(layout, task_doc.get("cost_category"))
    lane_shape = "plain" if adapter in ("process", "python") else "model"
    caps = capabilities_mod.for_lane(layout, adapter)
    return timeouts_mod.resolve(routing, lane=lane_shape, capabilities=caps)["hard_cap_s"] // 60


def authority(plan: dict, tasks: dict[str, Task], layout: Layout | None = None) -> str:
    """A | B | owner: the highest class of `plan`'s own operations (§2.7). Total over the closed, six-operation
    vocabulary — an operation this table does not name (a seventh operation, or one of the owner-only effects
    `validate()` already refuses as RF-26) fires the assertion rather than falling through to a default class, so
    nothing a `validate()` bug let through reaches the applier unclassified. `tasks` is accepted for the same
    shape every other reader of a plan takes, though no operation's class depends on the store today; `layout`,
    when given, resolves a project's metered lanes and any configured tightening — omitted, every create_task is
    judged against the built-in metered categories alone."""
    ops = [op for op in (plan.get("operations") or []) if isinstance(op, dict)]
    assert ops, "plans.authority: a plan with no operations cannot be classified"
    created_class: dict[str, str] = {}
    for op in ops:
        if op.get("op") == "create_task" and isinstance(op.get("task"), dict):
            tid = op["task"].get("id")
            cls = CLASS_OWNER if metered(layout, op["task"].get("cost_category")) else CLASS_B
            duration = (op["task"].get("needs") or {}).get("duration_min")
            if isinstance(duration, int) and not isinstance(duration, bool):
                limit = _plan_duration_limit_min(layout, op["task"])
                if limit is not None and duration > limit:
                    # Not a refusal -- raised to the owner authority class, exactly as a
                    # created task on a metered lane already is.
                    cls = CLASS_OWNER
            if tid:
                created_class[tid] = _tightened(layout, "create_task", cls)
    classes = []
    for op in ops:
        name = op.get("op")
        assert name in _BASE_OPERATION_CLASS, f"plans.authority cannot classify operation {name!r}"
        if name == "create_task":
            task = op.get("task")
            tid = task.get("id") if isinstance(task, dict) else None
            assert tid in created_class, \
                f"plans.authority cannot classify a create_task op with no id to classify ({task!r})"
            classes.append(created_class[tid])
        elif name == "set_job":
            classes.append(_tightened(layout, "set_job", created_class.get(op.get("task"), CLASS_A)))
        else:
            classes.append(_tightened(layout, name, _BASE_OPERATION_CLASS[name]))
    return max(classes, key=lambda c: _CLASS_RANK[c])


def approver_role(layout: Layout, proposer_role: str | None) -> tuple[str | None, str | None]:
    """(role, None) for class B's one approval session: the first role in `config/routing.yaml` `plan.approver`
    (an ordered list) that is not `proposer_role` (RF-23), that the project actually configures under `roles:`
    when it configures that section at all, and whose lane is not configured inactive. (None, reason) when no
    candidate qualifies — the caller raises the plan to the owner with that reason named (§2.7), never silently.

    A role absent from `roles:` is unavailable here exactly as tasks.py's own contract_warnings treats it ("the
    task would wait for it forever"): a project that configures `roles:` at all and lists a `plan.approver` the
    section does not name must escalate to the owner, never mint a job for a role nothing will ever route to. A
    project that configures no `roles:` section at all skips this check, same as that other reader does."""
    from . import gate as gate_mod
    candidates = [str(r) for r in ((gate_mod.routing_doc(layout).get("plan") or {}).get("approver") or [])]
    roles_cfg = gate_mod.routing_doc(layout).get("roles")
    roles_cfg = roles_cfg if isinstance(roles_cfg, dict) else {}
    tried = []
    for role in candidates:
        refusal = self_approval_refusal(proposer_role, role) if proposer_role else None
        if refusal is not None:
            tried.append(f"{role} ({refusal['code']})")
            continue
        if roles_cfg and role not in roles_cfg:
            tried.append(f"{role} (not configured in routing.yaml roles:)")
            continue
        if gate_mod.role_inactive(layout, role):
            tried.append(f"{role} (inactive)")
            continue
        return role, None
    reason = f"no configured approver available ({'; '.join(tried)})" if tried else \
        "no configured approver available (plan.approver is empty)"
    return None, reason


# ======================================================================= the journal and the applier:
# the store holds one document per task and no multi-file transaction, so
# the plan is its own journal instead — written before the first mutation, applied in the applier's own fixed
# order (never the plan's), each operation individually idempotent, its index appended to `done` before the next
# one starts. `resume` is always last, so a crash at any point leaves the subject exactly where it already was:
# stopped. WHO may call apply() on a given plan is `authority()` above (§2.7, step 8): the loop calls it directly
# for class A, holds it for a `plan_review` verdict or an owner's own `apply_plan` choice otherwise; this module
# only guarantees that whoever does, the result is ordered, idempotent and visibly stuck rather than half-applied.
#
# Status machine: proposed -> refused | awaiting_approval -> applying -> applied | stuck -> abandoned. `refused`
# and `applied` are reached from validation alone; `abandoned` is reached from `stuck` alone, and only by
# cancel_plan — the one terminal state that records a deliberate stop instead of a finish.

def plans_dir(layout: Layout) -> Path:
    return layout.state / "plans"


def path_for(layout: Layout, plan_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(plan_id))
    return plans_dir(layout) / f"{safe}.yaml"


def load_plan(layout: Layout, plan_id: str) -> dict | None:
    p = path_for(layout, plan_id)
    return store.read(p) if p.exists() else None


def all_plans(layout: Layout) -> list[dict]:
    d = plans_dir(layout)
    return [store.read(p) for p in sorted(d.glob("*.yaml"))] if d.exists() else []


def _history(doc: dict, by: str, to: str) -> list[dict]:
    hist = list(doc.get("history") or [])
    hist.append({"at": store.now_iso(), "by": by, "from": doc.get("status"), "to": to})
    return hist


def propose(layout: Layout, doc: dict, by: str = "diagnosis", proposer_role: str | None = None) -> dict:
    """Write the plan a proposing job's result names, exactly once for its whole life: a
    repeat call with the same id — a replayed ingest — returns the plan already on disk, unchanged, never a
    second validation and never a second write. What is stored as `operations` is the normalised form (§1.3): the
    applier later executes exactly what was checked, never the raw proposal. The correction turn of §2.5 (a
    `revisions[]` entry replacing a refused `operations`) is `revise()`'s own job; this function always writes a
    fresh plan once and leaves `revisions` empty.

    `proposer_role` is stored on the plan (§2.7): the role `approver_role()` must never resolve to for this plan
    (RF-23), carried here rather than re-derived later so a plan's own approval lane always answers "never the
    proposer" from one recorded fact instead of whoever happens to call `open_plan_review()`."""
    plan_id = str(doc.get("id") or "").strip()
    if not plan_id:
        raise PlanError("a plan must carry a non-empty id")
    existing = load_plan(layout, plan_id)
    if existing is not None:
        return existing
    tasks_now = tasks_mod.load_all(layout)
    normalised_doc, _ = normalise(doc, tasks_now)
    report, refusals = validate(doc, layout, tasks=tasks_now)
    base = {"plan_schema": doc.get("plan_schema"), "id": plan_id, "subject": doc.get("subject"),
            "reason": doc.get("reason"), "expected_rev": doc.get("expected_rev"),
            "operations": normalised_doc.get("operations"), "revisions": [], "proposer_role": proposer_role,
            # Written whether or not the plan validates: a `set_job` refused for a failed preflight (RF-08) is
            # refused *because* this ran, and the approver of a plan that does validate needs the same transcript
            # for the one that passed — neither reader gets it from a run directory.
            "report": report.as_dict()}
    to = "refused" if refusals else "awaiting_approval"
    base["history"] = [{"at": store.now_iso(), "by": by, "from": None, "to": to}]
    if refusals:
        base["status"], base["refusals"] = "refused", refusals
    else:
        base.update({"status": "awaiting_approval", "refusals": [], "done": [], "resume_count": 0})
    store.write_new(path_for(layout, plan_id), base)
    tasks_mod.log(layout, "plan_refused" if refusals else "plan_proposed", by, plan=plan_id,
                  **({"refusals": [r["code"] for r in refusals]} if refusals else {"scope": report.scope}))
    return load_plan(layout, plan_id)


def revise(layout: Layout, plan_id: str, doc: dict, by: str) -> dict:
    """The correction turn: a plan already `refused` gets
    exactly one more proposal from the SAME job, under the same id. The refused `operations`, its own refusal
    list and its normalisations are moved into a `revisions[]` entry before the corrected form overwrites them
    — "both plans and both refusal lists" stays one document, never a sibling file. `doc` is a fresh raw
    proposal, the same shape `propose()` takes; how many times this may be called for one plan is the caller's
    own rule (`diagnosis.py` dispatches at most one correction session), not this function's — it corrects
    whatever `refused` plan it is given, however many times it is asked to."""
    existing = load_plan(layout, plan_id)
    if existing is None:
        raise PlanError(f"no such plan {plan_id!r}")
    if existing.get("status") != "refused":
        raise PlanError(f"plan {plan_id!r} is {existing.get('status')!r}, not refused; nothing to revise")
    tasks_now = tasks_mod.load_all(layout)
    normalised_doc, _ = normalise(doc, tasks_now)
    report, refusals = validate(doc, layout, tasks=tasks_now)
    revision_entry = {"at": store.now_iso(), "session": by, "operations": existing.get("operations"),
                      "refusals": existing.get("refusals"),
                      "normalised": (existing.get("report") or {}).get("normalised")}

    def fn(d):
        d["revisions"] = list(d.get("revisions") or []) + [revision_entry]
        d["operations"] = normalised_doc.get("operations")
        d["reason"] = doc.get("reason")
        d["expected_rev"] = doc.get("expected_rev")
        d["report"] = report.as_dict()
        to = "refused" if refusals else "awaiting_approval"
        d["history"] = _history(d, by, to)
        d["status"] = to
        if refusals:
            d["refusals"] = refusals
        else:
            d.update({"refusals": [], "done": [], "resume_count": 0})
        return d
    doc_out = store.update(path_for(layout, plan_id), fn)
    tasks_mod.log(layout, "plan_refused" if refusals else "plan_revised", by, plan=plan_id,
                  **({"refusals": [r["code"] for r in refusals]} if refusals else {"scope": report.scope}))
    return doc_out


# ----------------------------------------------------------------------- the applier's own fixed order (§2.6 rule 2)

_EXEC_CATEGORY = {"create_task": 0, "add_dependency": 1, "remove_dependency": 1, "patch_contract": 2,
                  "set_job": 3, "resume": 4}


def _execution_order(ops: list[dict]) -> list[int]:
    """The original index of every operation, sorted by the applier's own category — never the plan's listed
    order — and stable within a category, so two operations of the same kind keep the order the plan gave them."""
    return sorted(range(len(ops)), key=lambda i: (_EXEC_CATEGORY.get(ops[i].get("op"), 99), i))


# ----------------------------------------------------------------------- one operation's effect, each idempotent
# by construction (reusing the store's own idempotent writers): a create whose id exists is a no-op
# (tasks.create), a dependency edit is a set operation (tasks.patch_task), a contract patch is an assignment
# (tasks.patch_task), set_job writes the same block again, resume is a status transition that is already a
# no-op from the target status (tasks.set_status's _NoChange).

def _effect_create_task(layout: Layout, op: dict, plan_doc: dict) -> None:
    subject_id = (plan_doc.get("subject") or {}).get("task")
    try:
        # model_dump(mode="json"): plain strings, never the enum members Task.phase/human_approval/... are typed
        # as — this document is about to be written through to YAML (tasks.create -> store.write_new), unlike
        # validate()'s own _check_create_task/_build_created_tasks, which only ever hand it to pydantic.
        subject = tasks_mod.load(layout, subject_id).model_dump(mode="json")
        inherited = {k: subject[k] for k in ("phase", "human_approval", "escalation", "priority")}
    except tasks_mod.TaskError:
        inherited = {"phase": 0}
    full_doc = {**inherited, **(op.get("task") or {})}
    tasks_mod.create(layout, full_doc, actor="plan")


def _effect_add_dependency(layout: Layout, op: dict, plan_doc: dict) -> None:
    tasks_mod.patch_task(layout, op["task"], {"add_dependency": list(op.get("depends_on") or [])}, actor="plan")


def _effect_remove_dependency(layout: Layout, op: dict, plan_doc: dict) -> None:
    tasks_mod.patch_task(layout, op["task"], {"remove_dependency": list(op.get("depends_on") or [])}, actor="plan")


def _effect_patch_contract(layout: Layout, op: dict, plan_doc: dict) -> None:
    tasks_mod.patch_task(layout, op["task"], dict(op.get("fields") or {}), actor="plan")


def _effect_set_job(layout: Layout, op: dict, plan_doc: dict) -> None:
    """The one place a plan's `set_job` becomes a stored job block: a template id and typed params only — RF-04
    already refuses anything else at validate() time, so there is nothing else here to write. The adapter is the
    one the task's existing job already names (a parameter diff, RF-11), or — on a task this plan created — the
    template's own declared adapter (its first job). Built by `_set_job_block`, the same merge `_check_set_job`
    simulated at validate() time, so the preflight transcript recorded on the plan is the transcript for this
    exact block. This does not go through `tasks._write_job`: that chokepoint judges a
    writer-supplied value, and `adapter` here is never one — it is carried over from the existing job or the
    template, not chosen by this op — so a plan's own writer-authority bound is RF-04 itself, already enforced
    before this effect ever runs."""
    templates = _load_templates(layout)
    tmpl = templates.get(str(op.get("template"))) or {}

    def fn(doc):
        doc["job"] = _set_job_block(doc.get("job") or {}, tmpl, op.get("template"), op.get("params") or {})
        # `tasks_mod._update` preflights the changed block itself.
    tasks_mod._update(layout, op["task"], fn)


def _release_note(plan_id: str) -> str:
    """The release's own signature on the subject: the note `_effect_resume` writes, and the one `_already_released`
    looks for. One string, so a retry can never disagree with what the first attempt recorded."""
    return f"released by plan {plan_id}"


def _already_released(subject: Task, plan_id: str, target: TaskStatus) -> bool:
    """Proof, not a guess, that *this* plan's release already landed on an earlier call (F9): the subject's own
    history entry, written by `_effect_resume` with `by: plan` and this plan's note. A status check cannot carry
    that proof in either direction — a subject that is FAILED is a release this plan still owes (FAILED -> READY
    is an allowed transition, and the repair lane's subjects arrive FAILED as often as BLOCKED), while a subject
    some other actor moved off BLOCKED while `resume` was still pending was never released by this plan at all."""
    note = _release_note(plan_id)
    return any(h.get("by") == "plan" and h.get("note") == note and h.get("to") == target.value
               for h in (subject.history or []))


def _effect_resume(layout: Layout, op: dict, plan_doc: dict) -> None:
    """The release itself, always the applier's last operation (§2.6 rule 2) — re-reads the subject fresh rather
    than trusting `plan_doc`, for two reasons a single validate() at promotion cannot cover (F2, F9): a Hold the
    owner places after promotion must still stop the release, and must never be cleared by this call — `resume`'s
    own `actor="plan"` is outside `tasks.AUTOMATIC_ACTORS`, which is exactly what would otherwise erase it
    (tasks.set_status) on the way to BLOCKED -> READY; and a retry that lands after this same effect already
    succeeded (the crash window between the effect and `done`) must not force the subject backwards into an
    illegal transition. Anything short of that proof is a release this plan still owes: the transition is made,
    and if the store refuses it the plan stalls visibly rather than reporting a release it never performed."""
    subject_id = (plan_doc.get("subject") or {}).get("task")
    plan_id = str(plan_doc.get("id"))
    target = TaskStatus.BLOCKED if op.get("as", "ready") == "blocked_on_dependencies" else TaskStatus.READY
    subject = tasks_mod.load(layout, subject_id)
    if subject.held:
        raise PlanError(f"subject {subject_id!r} is held by the owner; resume refused")
    if _already_released(subject, plan_id, target):
        return
    tasks_mod.set_status(layout, subject_id, target, actor="plan", note=_release_note(plan_id))


_EFFECTS = {"create_task": _effect_create_task, "add_dependency": _effect_add_dependency,
           "remove_dependency": _effect_remove_dependency, "patch_contract": _effect_patch_contract,
           "set_job": _effect_set_job, "resume": _effect_resume}


# ----------------------------------------------------------------------- the applier itself

def _log_quiet(layout: Layout, event: str, **fields) -> None:
    """Every log line inside `apply()`'s own call tree goes through here: `tasks.log` does its own file I/O on a
    lock that can time out, and each of these lines is written *after* the status write it records has already
    landed. A lost line costs the trail, never the plan — and nothing inside this call tree may raise, or one bad
    plan takes the loop's whole turn with it (the residual on F1). `propose()`'s own line is deliberately not
    routed here: it has no status write to protect and its caller should see the failure."""
    try:
        tasks_mod.log(layout, event, "applier", **fields)
    except Exception:                                  # noqa: BLE001 - a record, never a reason to raise
        pass


def _fail_refused(layout: Layout, plan_id: str, refusals: list[dict], report: dict | None = None) -> dict:
    def fn(d):
        d["history"] = _history(d, "applier", "refused")
        d["status"] = "refused"
        d["refusals"] = refusals
        if report is not None:
            d["report"] = report               # re-validation at promotion: the same preflight transcript rule
        return d
    doc = store.update(path_for(layout, plan_id), fn)
    _log_quiet(layout, "plan_refused", plan=plan_id, refusals=[r["code"] for r in refusals])
    return doc


def _promote_to_applying(layout: Layout, plan_id: str, order: list[int], report: dict) -> dict:
    def fn(d):
        d["history"] = _history(d, "applier", "applying")
        d["status"] = "applying"
        d["done"] = []
        d["order"] = order
        d["resume_count"] = 0
        d["report"] = report
        return d
    doc = store.update(path_for(layout, plan_id), fn)
    _log_quiet(layout, "plan_applying", plan=plan_id)
    return doc


def _append_done(layout: Layout, plan_id: str, idx: int) -> dict:
    def fn(d):
        done = list(d.get("done") or [])
        if idx not in done:
            done.append(idx)
        d["done"] = done
        return d
    return store.update(path_for(layout, plan_id), fn)


def _finish_applied(layout: Layout, plan_id: str) -> dict:
    def fn(d):
        d["history"] = _history(d, "applier", "applied")
        d["status"] = "applied"
        return d
    doc = store.update(path_for(layout, plan_id), fn)
    _log_quiet(layout, "plan_applied", plan=plan_id)
    return doc


def _try_open_stuck_item(layout: Layout, plan_id: str, doc: dict) -> dict:
    """Best-effort: a `stuck` plan with no `owner_item` yet gets exactly one the moment the owner's inbox has
    room. `inbox.create` raises `InboxFull` whenever the owner's three-item cap (inbox.MAX_OPEN_PER_ROLE) is
    already spent — a routine condition, not a crash (F1) — so this never raises: the plan stays `stuck` without
    an item, and `resume_plans` retries this same call on a later turn until it succeeds. The dedup_key makes a
    retry (or a second call that races this one) return the same item rather than open a second."""
    if doc.get("owner_item"):
        return doc
    ops = doc.get("operations") or []
    completed = [f"{ops[i].get('op')}#{i}" for i in sorted(doc.get("done") or []) if i < len(ops)]
    err = doc.get("last_error") or {}
    summary, detail = inbox_mod.fit(
        f"Plan {plan_id} is stuck after {doc.get('resume_count')} attempt(s). Completed: "
        f"{', '.join(completed) or 'none'}. Failed at operation {err.get('op_index')} ({err.get('op')}): "
        f"{err.get('message')}")
    subject_id = (doc.get("subject") or {}).get("task")
    try:
        item = inbox_mod.create(layout, for_role="owner", subject=f"Plan {plan_id} is stuck", summary=summary,
                                detail=detail, kind=inbox_mod.DECISION_KIND, allowed_responses=[], free_text=True,
                                from_="applier", task_id=subject_id, dedup_key=f"plan:{plan_id}")
    except inbox_mod.InboxFull:
        _log_quiet(layout, "inbox_full", reason="plan_stuck", plan=plan_id)
        return doc
    except Exception:                                  # noqa: BLE001 - a lock timeout here is not this plan's
        _log_quiet(layout, "plan_apply_stall_unrecorded", plan=plan_id)
        return doc

    def fn(d):
        d["owner_item"] = item["id"]
        return d
    try:
        doc = store.update(path_for(layout, plan_id), fn)
    except Exception:                                  # noqa: BLE001 - the item exists (dedup_key) either way
        _log_quiet(layout, "plan_apply_stall_unrecorded", plan=plan_id)
        return doc
    _log_quiet(layout, "plan_stuck_item_opened", plan=plan_id, owner_item=item["id"],
               op_index=err.get("op_index"))
    return doc


def _mark_stuck(layout: Layout, plan_id: str, doc: dict) -> dict:
    """`status: stuck` is written on its own, before an owner item is even attempted (F1): the owner item is a
    courtesy the inbox's own cap may defer, never a precondition for the plan itself being visibly stopped."""
    if doc.get("status") != "stuck":
        def fn(d):
            d["history"] = _history(d, "applier", "stuck")
            d["status"] = "stuck"
            return d
        try:
            doc = store.update(path_for(layout, plan_id), fn)
        except Exception:                              # noqa: BLE001 - never let a lock timeout out of apply()
            _log_quiet(layout, "plan_apply_stall_unrecorded", plan=plan_id)
            return load_plan(layout, plan_id) or doc
        _log_quiet(layout, "plan_stuck", plan=plan_id,
                   op_index=(doc.get("last_error") or {}).get("op_index"))
    return _try_open_stuck_item(layout, plan_id, doc)


def _record_stall(layout: Layout, plan_id: str, idx: int | None, op: dict | None, exc: Exception) -> dict:
    """Record one operation's failure against the plan and, past `plan.max_resumes`, mark it `stuck` — never
    letting a second failure (a full inbox, a lock timeout) escape this call and take the loop's own turn down
    with it (F1). `idx`/`op` are `None` for a failure outside any one operation (promotion's own re-validation,
    a write the applier makes between operations) rather than inside an effect."""
    op_name = (op or {}).get("op")
    message = _safe((f"operation {idx} ({op_name}) failed: {exc}" if idx is not None else f"apply() failed: {exc}"),
                    layout)

    def fn(d):
        d["resume_count"] = int(d.get("resume_count") or 0) + 1
        d["last_error"] = {"op_index": idx, "op": op_name, "message": message}
        return d
    try:
        doc = store.update(path_for(layout, plan_id), fn)
    except Exception:                                  # noqa: BLE001 - left for the next turn to retry
        _log_quiet(layout, "plan_apply_stall_unrecorded", plan=plan_id)
        return load_plan(layout, plan_id) or {}
    limits = _plan_limits(layout)
    if doc["resume_count"] >= limits["max_resumes"]:
        return _mark_stuck(layout, plan_id, doc)
    _log_quiet(layout, "plan_apply_stalled", plan=plan_id, op_index=idx, resume_count=doc["resume_count"])
    return doc


def apply(layout: Layout, plan_id: str) -> dict:
    """Progress one plan as far as it can go in one call: `awaiting_approval` is re-validated fresh against the
    current store before the journal is written (a stale `expected_rev`, or any other refusal, is RF-19/etc.
    here, before the first mutation — §2.6 rule 1); `applying` resumes from `done`; `stuck` is retried the same
    way (the one effect `apply_plan` would have, §2.7). `applied`, `abandoned` and `refused` are terminal and this
    is a no-op on them — calling apply() twice, or replaying the proposing job's ingest, changes nothing the
    second time. An operation's own failure (a store write that raises) is never let out of this call: it is
    recorded (`resume_count`, `last_error`) and, past `plan.max_resumes`, turned into exactly one owner item
    (`stuck`) — never re-raised, so one bad plan can never take an entire turn down with it."""
    doc = load_plan(layout, plan_id)
    if doc is None:
        raise PlanError(f"no such plan {plan_id!r}")
    status = doc.get("status")
    if status in TERMINAL_PLAN_STATUSES:
        return doc
    if status not in ("awaiting_approval", "applying", "stuck"):
        raise PlanError(f"plan {plan_id!r} is {status!r}, not ready to apply")
    try:
        return _apply_pending(layout, plan_id, doc, status)
    except Exception as exc:                          # noqa: BLE001 - a write between operations (promotion,
        # done, applied) failed rather than an operation's own effect: recorded the same defensive way, so a
        # lock timeout here can never take the loop's own turn down with it either (F1).
        return _record_stall(layout, plan_id, None, None, exc)


def _apply_pending(layout: Layout, plan_id: str, doc: dict, status: str) -> dict:
    order = _execution_order(doc.get("operations") or [])
    if status == "awaiting_approval":
        tasks_now = tasks_mod.load_all(layout)
        probe = {"plan_schema": doc.get("plan_schema"), "id": plan_id, "subject": doc.get("subject"),
                "reason": doc.get("reason"), "expected_rev": doc.get("expected_rev"),
                "operations": doc.get("operations")}
        report, refusals = validate(probe, layout, tasks=tasks_now)
        if refusals:
            return _fail_refused(layout, plan_id, refusals, report.as_dict())
        doc = _promote_to_applying(layout, plan_id, order, report.as_dict())
    elif status == "stuck":
        doc = _try_open_stuck_item(layout, plan_id, doc)       # retry a deferred item before trying an effect

    if list(doc.get("order") or []) != order:
        # The stored order is re-derived from the operations and cross-checked on every call, never computed once
        # at promotion and then trusted (F4): a truncated one would reach `applied` with operations never
        # attempted, and a reordered one would put the release before the work it releases. Neither is reachable
        # from any input today — only the applier writes this document — so a disagreement means the document is
        # not what the applier wrote, and the plan stops visibly instead of executing it.
        raise PlanError(f"plan {plan_id!r}: the stored operation order is not the one the applier computes from "
                        f"these operations; refusing to apply it")
    ops = doc.get("operations") or []
    done = set(doc.get("done") or [])
    for idx in order:
        if idx in done:
            continue
        op = ops[idx]
        try:
            _EFFECTS[op["op"]](layout, op, doc)
            doc = _append_done(layout, plan_id, idx)
        except Exception as exc:                      # noqa: BLE001 - any op (or persisting it) may fail;
            # recorded against this same op_index whether the effect itself raised or only the journal write
            # after it did (F3's own crash window is the effect succeeding and this write failing) - never
            # silent either way.
            return _record_stall(layout, plan_id, idx, op, exc)
        done.add(idx)
    return _finish_applied(layout, plan_id)


def cancel_plan(layout: Layout, plan_id: str, by: str = "owner") -> dict:
    """The owner's one terminal exit from `stuck` (§2.6 rule 6, §4.1): what is in `done` stays applied, the rest
    is never attempted, and nothing is undone. Refused off any status but `stuck` — there is nothing to abandon
    on a plan that already finished, was refused, or has not started."""
    doc = load_plan(layout, plan_id)
    if doc is None:
        raise PlanError(f"no such plan {plan_id!r}")
    if doc.get("status") != "stuck":
        raise PlanError(f"plan {plan_id!r} is {doc.get('status')!r}, not stuck; cancel_plan only abandons a "
                        f"stuck plan")

    def fn(d):
        d["history"] = _history(d, by, "abandoned")
        d["status"] = "abandoned"
        return d
    doc = store.update(path_for(layout, plan_id), fn)
    tasks_mod.log(layout, "plan_abandoned", by, plan=plan_id, done=sorted(doc.get("done") or []))
    return doc


# ----------------------------------------------------------------------- routing a freshly validated plan by its
# own authority class (§2.7): class A is applied on the spot (below, in `resume_plans`); the owner class opens
# exactly one inbox item carrying the `apply_plan` choice and waits for the owner. Class B is meant to open one
# `plan_review` job and wait for its typed verdict (`open_plan_review`/`apply_plan_review`, built and tested
# below) — but nothing in this tree submits that job to an adapter yet (no packet builder, no loop ingest hook,
# no `agents/schemas/plan_review.json`; P1-3, docs/releases/2.9.0/P1-3-R1-class-b-approval-lane.md), so minting
# the id and stopping there leaves the plan `awaiting_approval` forever with no job behind it and nothing in the
# inbox. Until that dispatch is built, `resume_plans()` routes class B through this same owner path instead of
# through `open_plan_review` — not a second, dead-end lane. Each of the two entry points here is idempotent on
# the field it writes (`owner_item`), so calling either again once that field is set is a no-op — the sweep that
# drives them may run every turn without ever opening a second item.

def _open_owner_choice(layout: Layout, plan_id: str, doc: dict, *, reason: str | None = None) -> dict:
    """Exactly one inbox item carrying the owner-only `apply_plan` choice (§2.7's owner class, §4.1's typed
    choices) — a typed button, never prose. Reached by a plan whose own class is `owner` (a metered
    `create_task`); by a class B plan for which `approver_role()` found nobody available (§2.7: "if none is,
    the plan goes to the owner with the reason named"); by every other class B plan, until the `plan_review`
    dispatch exists (P1-3, above); and by a plan found `awaiting_approval` with a `plan_review` naming a job
    that is not live (`_plan_review_job_live`, below) — a store written by an earlier version, or a job lost."""
    if doc.get("owner_item"):
        return doc
    effect = {"op": "apply_plan", "plan": plan_id}
    label, _ = render_choice(effect)
    assert label is not None, "apply_plan is in OPERATIONS/OWNER_ONLY_EFFECTS; render_choice must name it"
    why = f" ({reason})" if reason else ""
    summary, detail = inbox_mod.fit(f"Plan {plan_id} needs the owner's approval{why}. {doc.get('reason') or ''}")
    subject_id = (doc.get("subject") or {}).get("task")
    try:
        item = inbox_mod.create(layout, for_role="owner", subject=f"Approve plan {plan_id}", summary=summary,
                                detail=detail, kind=inbox_mod.DECISION_KIND, choices=[{"id": "apply_plan",
                                "effect": effect}], free_text=True, from_="applier", task_id=subject_id,
                                dedup_key=f"plan-approval:{plan_id}")
    except inbox_mod.InboxFull:
        _log_quiet(layout, "inbox_full", reason="plan_owner_approval", plan=plan_id)
        return doc

    def fn(d):
        d["owner_item"] = item["id"]
        return d
    try:
        doc = store.update(path_for(layout, plan_id), fn)
    except Exception:                                  # noqa: BLE001 - the item exists (dedup_key) either way
        _log_quiet(layout, "plan_apply_stall_unrecorded", plan=plan_id)
        return doc
    _log_quiet(layout, "plan_owner_item_opened", plan=plan_id, owner_item=item["id"])
    return doc


def _open_owner_question(layout: Layout, plan_id: str, doc: dict, role: str, question: str) -> dict:
    """The plan's other owner path: a `plan_review` verdict of `OWNER` is a question, never an
    effect — distinct from `_open_owner_choice`'s typed `apply_plan` button. Free text on the item it opens is an
    ordinary comment (§4.1); the plan itself is left `awaiting_approval`, exactly where the review found it."""
    if doc.get("owner_item"):
        return doc
    summary, detail = inbox_mod.fit(question or f"{role} has a question about plan {plan_id}")
    subject_id = (doc.get("subject") or {}).get("task")
    try:
        item = inbox_mod.create(layout, for_role="owner", subject=f"{role}'s question on plan {plan_id}",
                                summary=summary, detail=detail, kind=inbox_mod.DECISION_KIND,
                                allowed_responses=[], free_text=True, from_=role, task_id=subject_id,
                                dedup_key=f"plan-review-question:{plan_id}")
    except inbox_mod.InboxFull:
        _log_quiet(layout, "inbox_full", reason="plan_review_owner", plan=plan_id)
        return doc

    def fn(d):
        d["owner_item"] = item["id"]
        d["plan_review"] = {**(d.get("plan_review") or {}), "verdict": "OWNER"}
        return d
    doc = store.update(path_for(layout, plan_id), fn)
    _log_quiet(layout, "plan_review_owner_question", plan=plan_id, role=role, owner_item=item["id"])
    return doc


def open_plan_review(layout: Layout, plan_id: str, *, by: str = "loop") -> dict:
    """Class B's one approval session (§2.7): resolve `plan.approver` (never the proposer, RF-23) and mint
    exactly one `plan_review` job id against the plan's subject task — a new job kind, additive to the existing
    id derivation (`tasks.JobId`/`tasks.allocate_job`). Attempt-scoped when the subject names an attempt (RF-19
    already compares it with current_attempt), task-scoped like a diagnosis raised before any delivery exists
    only when it does not. No approver available escalates to the owner instead, with the reason named, never
    silently dropped. Idempotent: a plan that already carries `plan_review` or `owner_item` is returned
    unchanged, so a repeated call from the sweep never opens a second job."""
    doc = load_plan(layout, plan_id)
    if doc is None:
        raise PlanError(f"no such plan {plan_id!r}")
    if doc.get("plan_review") or doc.get("owner_item"):
        return doc
    subject = doc.get("subject") or {}
    subject_id = subject.get("task")
    role, reason = approver_role(layout, doc.get("proposer_role"))
    if role is None:
        return _open_owner_choice(layout, plan_id, doc, reason=reason)
    pre_rev = tasks_mod.load(layout, subject_id).rev
    job_id = tasks_mod.allocate_job(layout, subject_id, "plan_review", attempt=subject.get("attempt"),
                                    note=f"plan_review of {plan_id}").id
    # allocate_job's own write bumps the subject's rev (its job counter lives there) — a mutation this function
    # makes, not one the store owner made, so it must never read back as RF-19 staleness when the review's own
    # APPROVE later promotes this plan. Absorb only that own delta: refresh expected_rev to the post-allocation
    # rev only when the plan's own recorded value still equals the rev read just before the allocation. If it
    # does not, something else moved the subject in this same window, and expected_rev is left exactly as stale
    # as it truthfully is, so apply()'s own re-validation refuses with RF-19 rather than being silenced here.
    new_rev = tasks_mod.load(layout, subject_id).rev

    def fn(d):
        d["plan_review"] = {"job": job_id, "role": role}
        expected = dict(d.get("expected_rev") or {})
        if expected.get(subject_id) == pre_rev:
            expected[subject_id] = new_rev
        d["expected_rev"] = expected
        return d
    doc = store.update(path_for(layout, plan_id), fn)
    tasks_mod.log(layout, "plan_review_opened", by, plan=plan_id, job=job_id, role=role)
    return doc


def apply_plan_review(layout: Layout, plan_id: str, verdict: str, role: str, *,
                      findings: list[dict] | None = None, question: str | None = None,
                      by: str | None = None) -> dict:
    """The `plan_review` job's typed result (§2.7), applied: `APPROVE` runs the same applier an owner's
    `apply_plan` choice uses; `REVISE` records refusal-shaped `findings` and returns the plan to `refused`,
    exactly as a validator's own refusal would; `OWNER` opens one free-text item carrying the approver's
    `question` (`_open_owner_question`), never an effect. Refuses (RF-23) a verdict from any role but the one
    `open_plan_review` actually sent this plan to — self-approval under a borrowed role name is the same refusal
    as self-approval under the proposer's own."""
    doc = load_plan(layout, plan_id)
    if doc is None:
        raise PlanError(f"no such plan {plan_id!r}")
    review = doc.get("plan_review") or {}
    if review.get("role") != role:
        raise PlanError(f"plan {plan_id!r} was not sent to {role!r} for review")
    refusal = self_approval_refusal(doc.get("proposer_role"), role) if doc.get("proposer_role") else None
    if refusal is not None:
        raise PlanError(refusal["message"])
    by = by or f"plan_review:{role}"
    if verdict == "APPROVE":
        tasks_mod.log(layout, "plan_review_approved", by, plan=plan_id, role=role)
        return apply(layout, plan_id)
    if verdict == "REVISE":
        refusals = list(findings or [])

        def fn(d):
            d["history"] = _history(d, by, "refused")
            d["status"] = "refused"
            d["refusals"] = refusals
            d["plan_review"] = {**review, "verdict": "REVISE"}
            return d
        doc = store.update(path_for(layout, plan_id), fn)
        tasks_mod.log(layout, "plan_refused", by, plan=plan_id, refusals=[r.get("code") for r in refusals])
        return doc
    if verdict == "OWNER":
        return _open_owner_question(layout, plan_id, doc, role, question or "")
    raise PlanError(f"plan_review verdict {verdict!r} is not APPROVE, REVISE or OWNER")


def _refuse_held_subject(layout: Layout, plan_id: str, subject_id: str) -> dict:
    """RF-22, re-checked immediately before routing opens any approval record — a hold placed after validate()
    and before this turn must refuse here exactly as validate() itself would, never reach a human (an approver's
    `plan_review` job, an owner's `apply_plan` button) as a choice apply()'s own re-validation would refuse
    anyway (only a choice the underlying command would accept is ever offered)."""
    refusal = _rf("RF-22", "I2 §1.2", None, "subject", f"subject {subject_id!r} is held by the owner").as_dict()

    def fn(d):
        d["history"] = _history(d, "loop", "refused")
        d["status"] = "refused"
        d["refusals"] = [refusal]
        return d
    doc = store.update(path_for(layout, plan_id), fn)
    tasks_mod.log(layout, "plan_refused", "loop", plan=plan_id, refusals=["RF-22"])
    return doc


def _plan_review_job_live(layout: Layout, doc: dict) -> bool:
    """Whether the job id a plan's own `plan_review` names actually has a record behind it
    (`jobs.prepare` was called for it — an adapter dispatch happened or is about to). `open_plan_review` only
    ever mints the id (P1-3): nothing in this tree submits it, so this is `False` for every `plan_review` this
    release's own routing writes. It exists for the shape the id-minting still has to answer honestly — a plan
    document carrying `plan_review` with no job behind it at all, whether from a store an earlier version wrote
    or from a job lost after it was prepared — so `resume_plans` can notice and escalate rather than leaving the
    plan `awaiting_approval` forever with nothing watching it."""
    from . import jobs as jobs_mod
    review = doc.get("plan_review") or {}
    job_id = review.get("job")
    subject_id = (doc.get("subject") or {}).get("task")
    if not job_id or not subject_id:
        return False
    return any(r.get("attempt_id") == job_id for r in jobs_mod.for_task(layout, subject_id))


def resume_plans(layout: Layout) -> list[str]:
    """The loop's own turn (§2.6 rule 5, extended by §2.7/step 8): route every plan still `awaiting_approval` and
    not yet routed, then continue every plan still `applying` from its own journal. Class A applies on the spot.
    Class B and the owner class each reach the owner's one typed `apply_plan` choice (`_open_owner_choice`) — the
    `plan_review` dispatch class B is designed for (`open_plan_review`/`apply_plan_review`) does not exist yet
    (P1-3, docs/releases/2.9.0/P1-3-R1-class-b-approval-lane.md), so routing a class B plan there and stopping
    would leave it `awaiting_approval` with no job behind it and nothing in the inbox; routing it to the owner
    instead gives every validated plan a human-actionable end, exactly as the owner class always has. A plan
    already found `awaiting_approval` with a `plan_review` naming a job that turns out not to be live —
    `_plan_review_job_live` above — is noticed here too and escalated to the owner the same way, exactly once,
    however many sweeps run (the `owner_item` field it writes is the same idempotence guard every other entry
    point here already uses). A `stuck` plan's operations are left alone — only the owner's
    apply_plan/cancel_plan choice decides what happens to them next, never an automatic retry that would keep
    reopening the question the owner item already asked — but a `stuck` plan still missing its owner item (F1:
    the inbox was full when it first tried) gets one more attempt to open it, independent of and never re-running
    any operation. One plan's own exception (a lock timeout `apply()` itself did not already contain) is logged
    and never stops the sweep through the rest."""
    acted = []
    tasks_now = None
    for doc in all_plans(layout):
        plan_id = doc.get("id")
        try:
            status = doc.get("status")
            if (status == "awaiting_approval" and doc.get("plan_review") and not doc.get("owner_item")
                   and not _plan_review_job_live(layout, doc)):
                _open_owner_choice(layout, plan_id, doc,
                                  reason="no plan_review job is live behind this approval record")
                acted.append(plan_id)
            elif status == "awaiting_approval" and not doc.get("plan_review") and not doc.get("owner_item"):
                tasks_now = tasks_mod.load_all(layout) if tasks_now is None else tasks_now
                subject_id = (doc.get("subject") or {}).get("task")
                subject_task = tasks_now.get(subject_id)
                if subject_task is not None and subject_task.held:
                    _refuse_held_subject(layout, plan_id, subject_id)
                    acted.append(plan_id)
                    continue
                cls = authority(doc, tasks_now, layout)
                if cls == CLASS_A:
                    apply(layout, plan_id)
                elif cls == CLASS_B:
                    _open_owner_choice(layout, plan_id, doc,
                                      reason="class B: the plan_review dispatch is not built yet")
                else:
                    _open_owner_choice(layout, plan_id, doc)
                acted.append(plan_id)
            elif status == "applying":
                apply(layout, plan_id)
                acted.append(plan_id)
            elif status == "stuck" and not doc.get("owner_item"):
                _try_open_stuck_item(layout, plan_id, doc)
        except Exception:                              # noqa: BLE001 - one plan's own failure, never the sweep's
            _log_quiet(layout, "plan_resume_error", plan=plan_id)
            continue
    return acted
