"""The write-time preflight (RECOVERY_PLAN.md section 3.3, D-027 decision 9): a `process` or `python` job block
is checked the moment it is written -- by a seed import, a patch, `task.set_job`, or a plan -- never at
dispatch, so a wrong path, a wrong port, an unknown job function or an unparsable flag is refused once, with its
own code, instead of discovered by running it.

The result is stored on the task as `job.preflight: {ok, at, template, probe_digest, detail}` and `supervisor.gate`
refuses to launch a `process`/`python` job whose stored preflight is absent, failed, or was recorded against a
different job block (`detail['block_digest']`) than the one on disk. Nothing here ever raises: a template or a
job block too broken to check is a failed preflight (`detail['code'] == "preflight_error"`), never an exception
that would abort the write that carries it.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import subprocess
import time
from typing import Any

from . import envconfig
from . import policy as policy_mod
from . import templates as templates_mod
from ._vendor.contract import atomic_json, contained, read_json
from .paths import Layout
from .store import now_iso

PREFLIGHT_ADAPTERS = ("process", "python")
PROBE_CAP_S = 20                 # default preflight cap (RECOVERY_PLAN.md section 3.3)


def block_digest(job_like: dict) -> str:
    """Identifies a job block's own content, `preflight` excluded -- the same function run at write time (over
    the block about to be stored) and at launch time (over the block on disk), so a mismatch means the block
    changed since the stored preflight was recorded."""
    payload = {k: v for k, v in dict(job_like).items() if k != "preflight"}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _fail(code: str, message: str, *, template: str | None, digest: str, probe_digest: str | None = None) -> dict:
    return {"ok": False, "at": now_iso(), "template": template, "probe_digest": probe_digest,
            "detail": {"code": code, "message": message, "block_digest": digest}}


def _pass(*, template: str | None, digest: str, probe_digest: str | None = None) -> dict:
    return {"ok": True, "at": now_iso(), "template": template, "probe_digest": probe_digest,
            "detail": {"block_digest": digest}}


def _resolve_executable(executable: str, layout: Layout, allowed: list[str] | None) -> bool:
    if not executable:
        return False
    if allowed and templates_mod.basename(executable) not in [templates_mod.basename(a) for a in allowed]:
        root_path = (layout.root / executable)
        return root_path.exists() and contained(root_path, [layout.root], root=layout.root)
    if shutil.which(executable):
        return True
    root_path = layout.root / executable
    return root_path.exists()


def _substitute(argv: list[str], params: dict[str, Any]) -> list[str]:
    """One slot = one argv element, never concatenated (RECOVERY_PLAN.md section 3.2): an element that is
    exactly `{name}` becomes `params[name]`, stringified; anything else passes through unchanged."""
    out = []
    for element in argv:
        if element.startswith("{") and element.endswith("}") and len(element) > 2:
            name = element[1:-1]
            out.append(str(params.get(name, "")))
        else:
            out.append(element)
    return out


def _param_checks(params: dict[str, Any], declared: dict[str, dict], layout: Layout, digest: str,
                  template: str | None) -> dict | None:
    unknown = set(params) - set(declared)
    if unknown:
        return _fail("parameter_rejected", f"{', '.join(sorted(unknown))} not declared by {template!r}",
                     template=template, digest=digest)
    for name, value in params.items():
        slot = declared.get(name) or {}
        ptype = slot.get("type")
        if isinstance(value, str) and value.startswith("-") and not slot.get("allow_leading_dash"):
            return _fail("parameter_rejected", f"{name!r} begins with '-', not allowed for this slot",
                         template=template, digest=digest)
        if ptype == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                return _fail("parameter_type_invalid", f"{name!r} must be an int, got {value!r}",
                             template=template, digest=digest)
            if slot.get("min") is not None and value < slot["min"]:
                return _fail("parameter_type_invalid", f"{name!r} is below min {slot['min']}",
                             template=template, digest=digest)
            if slot.get("max") is not None and value > slot["max"]:
                return _fail("parameter_type_invalid", f"{name!r} is above max {slot['max']}",
                             template=template, digest=digest)
        elif ptype == "enum":
            if value not in (slot.get("values") or []):
                return _fail("parameter_type_invalid", f"{name!r} is not one of {slot.get('values')}",
                             template=template, digest=digest)
        elif ptype == "port":
            allowed = slot.get("allowed") or []
            if value not in allowed:
                return _fail("host_port_not_allowed", f"{name!r}={value!r} is not in {allowed}",
                             template=template, digest=digest)
        elif ptype == "path":
            failed = _path_checks(name, value, slot, layout, digest, template)
            if failed:
                return failed
    return None


def _path_checks(name: str, value: Any, slot: dict, layout: Layout, digest: str, template: str | None) -> dict | None:
    """One declared `path` slot: inside its roots, and there (RECOVERY_PLAN.md section 3.3's table) -- "every
    path parameter exists (or its parent does, for an output)". A slot marked `must_exist: false` names
    something a later job writes, so the file itself is not required; the directory it goes in is, which is the
    difference between a path nobody has written yet and a typo in the one they will."""
    roots = slot.get("roots") or ["."]
    if not isinstance(value, str) or not contained(value, roots, layout.root):
        return _fail("path_outside_roots", f"{name!r} does not resolve inside its declared roots",
                     template=template, digest=digest)
    target = layout.root / value
    if slot.get("must_exist", True):
        if not target.exists():
            return _fail("path_missing", f"{name!r} ({value}) does not exist", template=template, digest=digest)
    elif not target.parent.is_dir():
        return _fail("output_parent_missing", f"{name!r} ({value}): its directory does not exist",
                     template=template, digest=digest)
    return None


def _secret_scan(params: dict[str, Any], layout: Layout, digest: str, template: str | None) -> dict | None:
    """The table's own `all` row: no parameter of any job block, on either adapter, templated or literal, holds
    a secret-shaped value. Unlike every other parameter check this is not about a declared slot -- a credential
    written into the store is one whether a template declared the slot carrying it or not -- so it runs for a
    literal `argv` block too, which declares no slots at all (section 1.4)."""
    for name, value in params.items():
        if isinstance(value, str) and policy_mod.find_secrets(value, layout):
            return _fail("secret_shaped_value", f"{name!r} holds a secret-shaped value",
                         template=template, digest=digest)
    return None


def _run_probe(probe: list[str], layout: Layout, env: dict[str, str], cap_s: int) -> dict | None:
    """The probe, in a scratch directory of its own under the run tree -- removed again when it finishes, pass
    or fail: one directory per templated write, kept for the life of the project, is a leak nothing reads."""
    scratch = layout.runs / "_preflight" / str(int(time.time() * 1000))
    scratch.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in layout.root.iterdir()} if layout.root.exists() else set()
    try:
        try:
            proc = subprocess.run(probe, cwd=str(scratch), env=env, capture_output=True, timeout=cap_s,
                                  check=False)        # a non-zero exit is this function's own finding, not a raise
        except subprocess.TimeoutExpired:
            return {"code": "probe_failed", "message": f"probe timed out after {cap_s}s"}
        except OSError as exc:
            return {"code": "probe_failed", "message": f"probe could not start: {exc}"}
        if proc.returncode != 0:
            return {"code": "probe_failed", "message": f"probe exited {proc.returncode}"}
        after = {p.name for p in layout.root.iterdir()} if layout.root.exists() else set()
        if after - before:
            return {"code": "probe_escaped_run_dir",
                    "message": f"probe wrote {sorted(after - before)} outside the run directory"}
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)       # a probe's own leftovers go with it; the finding stands


def run(layout: Layout, job: dict, task=None) -> dict:
    """The preflight result for one job block, `{ok, at, template, probe_digest, detail}` -- never raises; a
    problem this function cannot itself classify is `detail['code'] == 'preflight_error'`, ok False.

    `task` is the task whose contract this block belongs to, when the caller has one: the declared-needs match of
    docs/plan/LONG_WORK.md section 3.3 reads the task's own `needs` block beside the block's template, and the
    two callers that write a block differ in what they have (`attach` the whole document, a plan's validator a
    stored task or none at all, for one it is creating). Without it only the block's own template can declare a
    need, which is exactly what a plan's created task has."""
    job = dict(job)
    digest = block_digest(job)
    template_id = job.get("template")
    adapter = job.get("adapter")
    try:
        return _run(layout, job, adapter, template_id, digest, task)
    except Exception as exc:                                     # noqa: BLE001 - a broken check is a failed preflight
        return _fail("preflight_error", f"{type(exc).__name__}: {exc}", template=template_id, digest=digest)


def _run(layout: Layout, job: dict, adapter: str | None, template_id: str | None, digest: str, task=None) -> dict:
    if adapter not in PREFLIGHT_ADAPTERS:
        return _pass(template=template_id, digest=digest)

    tmpl: dict = {}
    if template_id is not None:
        try:
            templates = templates_mod.load(layout)
        except templates_mod.TemplateError as exc:
            return _fail("preflight_error", f"job_templates.yaml: {exc}", template=template_id, digest=digest)
        tmpl = templates.get(str(template_id))
        if tmpl is None:
            return _fail("template_unknown", f"template {template_id!r} is not in config/job_templates.yaml",
                        template=template_id, digest=digest)

    params = dict(job.get("params") or {})
    failed = _secret_scan(params, layout, digest, template_id)
    if failed:
        return failed
    if tmpl:
        # Only a template's own declared slots are checked (RECOVERY_PLAN.md section 1.4), on either adapter: a
        # literal `argv`/`fn` job declares none, so nothing in its `params` is required to be declared, to exist
        # or to match a type -- for a literal python job the registered function's own signature
        # (`_run_python`) is the one check that binds them, and for a literal process job only `argv[0]` is
        # checked at all. Running these checks against a block with no template would refuse every parameter it
        # carries as undeclared, which is a refusal the design does not make.
        declared = tmpl.get("params") or {}
        failed = _param_checks(params, declared, layout, digest, template_id)
        if failed:
            return failed

    timeout_s = job.get("timeout_s")
    failed = _timeout_checks(layout, timeout_s, tmpl, adapter, digest, template_id, task, job)
    if failed:
        return failed

    failed = _needs_checks(layout, task, job, digest, template_id)
    if failed:
        return failed

    if adapter == "python":
        return _run_python(layout, job, tmpl, params, template_id, digest)
    return _run_process(layout, job, tmpl, params, template_id, digest)


def _needs_checks(layout: Layout, task, job: dict, digest: str, template: str | None) -> dict | None:
    """The declared-needs match, run at the write (docs/plan/LONG_WORK.md section 3.3's second and third rows,
    D-028 decision 2) through the one entry point every write-time caller shares, `needs.results_for`.

    The owner's `task.set_job` on a task that already exists has no row to refuse, so a decidable mismatch is
    stored here with the probe's and the gate holds the task as `preflight_failed`; a plan's `set_job` is refused
    with the plan it rode in on, because `plans._preflight_transcript_entry` refuses the operation on any failed
    preflight (RF-08) and stores nothing on the task. An import or a patch row is refused by `needs.write_check`
    in the `(errors, warnings)` pair those paths already return, which is the first row and not this one.

    Only the refusal tier is stored: a warning never refuses a write (D-010) and the path that reports warnings
    to their writer is that same import/patch pair. The code stored is the match's own
    (`needs_exceed_lane`/`needs_unsupported`/...), so the stored record names which declaration failed, while
    the task's stated reason stays `preflight_failed` -- one stored verdict, not a second beside it."""
    from . import needs as needs_mod
    refusal = next((r for r in needs_mod.results_for(layout, task, job) if r["tier"] == "refusal"), None)
    if refusal is None:
        return None
    return _fail(refusal["code"], refusal["message"], template=template, digest=digest)


def _timeout_checks(layout: Layout, timeout_s: Any, tmpl: dict, adapter: str | None, digest: str,
                    template: str | None, task=None, job: dict | None = None) -> dict | None:
    """`timeout_s` against the ceilings that exist for this block, and against what its template says the work
    needs. The ceiling is the lower of the template's own `timeout_s` and the lane's configured cap (routing.yaml
    `timeouts.lane_caps.<adapter>`) -- where neither is declared there is none, and a block (a literal,
    owner-authored one above all) may declare whatever deadline its work needs, exactly as before this release:
    a lane's *default* duration is not a ceiling, and turning one into a ceiling would refuse blocks that have
    been running correctly."""
    from . import gate, timeouts
    routing_cfg = gate.routing_doc(layout)
    lane_ceiling = timeouts.ceiling_for(layout, task, adapter, job)
    # RF-08's own "the lane's cap" (RECOVERY_PLAN.md section 3.3) is this one number, named by
    # docs/plan/LONG_WORK.md section 4.2/9 step 3: the check, its code, its message and its process-row scope
    # stay exactly here, with `timeouts.ceiling_s` added beside the template's own declared cap and the project's
    # explicit `timeouts.lane_caps.<adapter>` override as one more candidate the minimum is taken over. Composed
    # through `ceiling_inputs` like every other reader of it, so this row refuses against exactly the number the
    # resolver's clamp will enforce on the same job (D-028 decision 4).
    caps = [c for c in (tmpl.get("timeout_s"), timeouts.lane_cap(routing_cfg, adapter), lane_ceiling)
            if isinstance(c, int) and not isinstance(c, bool)]
    if timeout_s is not None and caps and timeout_s > min(caps):
        return _fail("timeout_too_high", f"timeout_s {timeout_s} is above the cap {min(caps)} declared for it",
                    template=template, digest=digest)

    needs = tmpl.get("needs") or {}
    duration_min = needs.get("duration_min")
    if duration_min is not None and timeout_s is not None and timeout_s < duration_min * 60:
        return _fail("duration_below_needs",
                    f"timeout_s {timeout_s} is below the template's needs.duration_min ({duration_min} min)",
                    template=template, digest=digest)
    return None


def _run_python(layout: Layout, job: dict, tmpl: dict, params: dict, template_id: str | None, digest: str) -> dict:
    from .adapters.python import JOBS
    fn_name = job.get("fn") or tmpl.get("fn")
    fn = JOBS.get(fn_name)
    if fn is None:
        return _fail("fn_unregistered", f"python job {fn_name!r} is not registered in JOBS",
                    template=template_id, digest=digest)
    try:
        inspect.signature(fn).bind(root=layout.root, run_dir=layout.runs, **params)
    except TypeError as exc:
        return _fail("params_signature_mismatch", f"params do not match {fn_name}'s signature: {exc}",
                    template=template_id, digest=digest)
    return _pass(template=template_id, digest=digest)


def _run_process(layout: Layout, job: dict, tmpl: dict, params: dict, template_id: str | None, digest: str) -> dict:
    if tmpl:
        argv = _substitute(tmpl.get("argv") or [], params)
    else:
        argv = list(job.get("argv") or [])          # literal, owner-authored: no declared slots (section 1.4)
    if not argv:
        return _fail("executable_not_allowed", "no argv to resolve", template=template_id, digest=digest)
    interpreters = tmpl.get("interpreters")
    if not _resolve_executable(argv[0], layout, interpreters):
        return _fail("executable_not_allowed", f"{argv[0]!r} does not resolve", template=template_id, digest=digest)

    stdin_file = job.get("stdin_file")
    if stdin_file and not (layout.root / stdin_file).exists():
        return _fail("stdin_missing", f"stdin_file {stdin_file!r} does not exist", template=template_id, digest=digest)

    env, probe_digest = envconfig.build(layout, "process")
    probe = tmpl.get("probe")
    if probe:
        failure = _run_probe(_substitute(probe, params), layout, env, PROBE_CAP_S)
        if failure:
            return _fail(failure["code"], failure["message"], template=template_id, digest=digest,
                        probe_digest=probe_digest)
    return _pass(template=template_id, digest=digest, probe_digest=probe_digest)


def attach(layout: Layout, doc: dict) -> None:
    """Mutates `doc['job']` in place, adding its own freshly-run preflight -- called from `tasks._write_job`
    (every writer-authority-checked job write) and from `tasks._update`'s own wrapper, which calls it for any
    lower-level mutation that leaves the job's content (`preflight` excluded) different from what it was, so
    the stored preflight is always run against the exact block it ends up recorded on, however it got there.
    Left untouched (`None`) for any adapter the gate's own rule does not bind (`PREFLIGHT_ADAPTERS`) -- a model
    lane's job carries no preflight to go stale, so there is nothing here for it to record.

    The block is canonicalised through the `Job` model (validate, then dump the same way `gate.preflight_reason`
    dumps the one it reads back at launch: `mode="json", exclude_none=True, exclude_unset=True`) before anything
    here digests or checks it -- the writer's own raw dict (an explicit null for a declared key, a numeric
    string a lax-mode parse would coerce) and the block the gate recomputes from `task.job` later must be the
    same bytes, or an unchanged block is held as stale forever (RECOVERY_PLAN.md section 3.3). A block too
    broken even to canonicalise is a failed preflight, same as one `run()` itself cannot classify -- never an
    exception that would abort the write that carries it."""
    job = doc.get("job")
    if not job or job.get("adapter") not in PREFLIGHT_ADAPTERS:
        return
    raw = {k: v for k, v in dict(job).items() if k != "preflight"}
    from .models.task import Job
    try:
        canonical = Job.model_validate(raw).model_dump(mode="json", exclude_none=True, exclude_unset=True)
    except Exception as exc:                                   # noqa: BLE001 - uncanonicalisable is a failed preflight
        canonical = dict(raw)
        canonical["preflight"] = _fail("preflight_error", f"{type(exc).__name__}: {exc}",
                                       template=raw.get("template"), digest=block_digest(raw))
        doc["job"] = canonical
        return
    canonical["preflight"] = run(layout, canonical, _task_of(doc, canonical))
    doc["job"] = canonical


def _task_of(doc: dict, canonical: dict):
    """The task this block is being written onto, for the needs match of section 3.3 -- built from the document
    the write is about to store, with the canonical block in place, so the declaration and the block are read as
    one. None when the document cannot be validated, which is a document the write itself is about to refuse."""
    from .models.task import Task
    try:
        return Task.model_validate({**doc, "job": {k: v for k, v in canonical.items() if k != "preflight"}})
    except Exception:                                   # noqa: BLE001 - nothing here may raise (see `attach`)
        return None


# ---------------------------------------------------------------- the backfill sweep (RECOVERY_PLAN.md 3.3, step 5)
#
# Every `process`/`python` job block written before this release predates the preflight and has none stored; the
# gate's refusal above would stop every one of them on upgrade day. Two bounded, automatic mechanisms close that
# window without a `supervisor migrate` step (a release needing one is a major version, and this is not): the
# loop's one-time sweep (`loop.Supervisor.preflight_backfill`, oldest task first, a bounded number per turn) and
# the gate's own run-on-absent check (`gate.preflight_reason`, one task, synchronously, on its first launch).
# Both call `tasks.backfill_job_preflight` for one task and both stop consulting this once `backfill_complete` is
# true -- written exactly once, the moment the sweep finds nothing left pending.

BACKFILL_BATCH = 50               # bounded per turn: enough to clear a few-hundred-task store in single-digit
                                   # turns, small enough that one turn never stalls on it
_BACKFILL_STATE_FILE = "preflight_backfill.json"


def _backfill_state_path(layout: Layout):
    return layout.runtime / _BACKFILL_STATE_FILE


def backfill_complete(layout: Layout) -> bool:
    """True once the sweep has recorded there is nothing left to backfill: after this, `gate.preflight_reason`'s
    run-on-absent window is closed and an absent preflight refuses outright, the same as a failed one always has.
    False for a project that has never run it -- including a fresh install with nothing to backfill at all, which
    reaches this exactly once, on its first turn (`loop.Supervisor.preflight_backfill`)."""
    p = _backfill_state_path(layout)
    if not p.exists():
        return False
    try:
        return bool(read_json(p).get("complete"))
    except (OSError, ValueError):
        return False


def mark_backfill_complete(layout: Layout, *, swept: int) -> None:
    """Written once, by the sweep alone, the turn it finds nothing left pending (never by the gate's own
    run-on-absent check, which backfills one task at a time and has no view of whether any other is still
    pending). `swept` is the one permanent record of the sweep's own cost, so the caller passes the running
    total across every turn (`backfill_progress` plus this turn's own count, via `record_backfill_progress`),
    never just the final turn's batch."""
    atomic_json(_backfill_state_path(layout), {"complete": True, "completed_at": now_iso(), "swept": swept})


def backfill_progress(layout: Layout) -> int:
    """How many tasks the sweep has written a preflight for so far, across every turn up to and including the
    last one recorded -- 0 for a project that has not started (or has not yet finished) its first turn's
    bookkeeping."""
    p = _backfill_state_path(layout)
    if not p.exists():
        return 0
    try:
        return int(read_json(p).get("swept") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def record_backfill_progress(layout: Layout, *, swept_this_turn: int) -> int:
    """Adds this turn's own count to the running total and persists it, `complete` still false -- so the total
    `mark_backfill_complete` eventually records is the sweep's own cost, not just its last batch. Returns the
    new total."""
    total = backfill_progress(layout) + swept_this_turn
    atomic_json(_backfill_state_path(layout), {"complete": False, "swept": total})
    return total


def pending_backfill(tasks: dict) -> list:
    """Every task whose stored job predates the preflight -- a `process`/`python` block with none recorded at
    all -- oldest first (`created`, then `id`, so a task written before that field existed sorts ahead of every
    task that has a date, which is the order the bounded sweep takes them in). A block that already carries one,
    passing, failed or stale, is not pending: absent is the only case this backfill ever fills. Not filtered by
    `task.status`: a `COMPLETE` task is swept the same as any other so its result is on record if anything ever
    reads it, and a `CANCELLED` one must be, since it can return to `NOT_STARTED` later and a sweep that had
    skipped it would leave it permanently refused once the mark closes the gate's run-on-absent window --
    `loop.preflight_backfill` is the one that decides which of these results is worth an owner item."""
    out = [t for t in tasks.values() if t.job is not None and t.job.adapter in PREFLIGHT_ADAPTERS
           and t.job.preflight is None]
    return sorted(out, key=lambda t: (t.created or "", t.id))


def report_backfill_failure(layout: Layout, task, pf) -> dict | None:
    """One deduped owner item for a job block the backfill could not pass -- `inbox.create`'s own `dedup_key`
    keeps it to exactly one, however many turns the sweep runs or however many times a launch is attempted,
    whichever of its two call sites (the sweep, the gate's run-on-absent check) meets the failure first. None
    when the inbox has no room this turn (InboxFull): the result is stored either way, so the refusal itself
    never depends on this succeeding."""
    from . import inbox as inbox_mod
    detail = pf.detail or {}
    summary, text = inbox_mod.fit(
        f"{task.id}'s job block cannot pass its preflight ({detail.get('code', 'unknown')}): "
        f"{detail.get('message', detail)}. It will not launch until the block is corrected.")
    try:
        return inbox_mod.create(layout, for_role="owner", subject=f"{task.id}: preflight failed", summary=summary,
                                 detail=text, kind="preflight_failed", allowed_responses=[], from_="loop",
                                 task_id=task.id, dedup_key=f"preflight_backfill:{task.id}")
    except inbox_mod.InboxFull:
        return None
