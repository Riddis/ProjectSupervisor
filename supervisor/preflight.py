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
import subprocess
import time
from pathlib import Path
from typing import Any

from . import envconfig
from . import policy as policy_mod
from . import templates as templates_mod
from ._vendor.contract import contained
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
    import shutil
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
            roots = slot.get("roots") or ["."]
            if not isinstance(value, str) or not contained(value, roots, layout.root):
                return _fail("path_missing", f"{name!r} does not resolve inside its declared roots",
                             template=template, digest=digest)
            if slot.get("must_exist", True) and not (layout.root / value).exists():
                return _fail("path_missing", f"{name!r} ({value}) does not exist", template=template, digest=digest)
        for value_text in ([value] if isinstance(value, str) else []):
            if policy_mod.find_secrets(value_text, layout):
                return _fail("secret_shaped_value", f"{name!r} holds a secret-shaped value",
                             template=template, digest=digest)
    return None


def _run_probe(probe: list[str], layout: Layout, env: dict[str, str], cap_s: int) -> dict | None:
    scratch = layout.runs / "_preflight" / str(int(time.time() * 1000))
    scratch.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in layout.root.iterdir()} if layout.root.exists() else set()
    try:
        proc = subprocess.run(probe, cwd=str(scratch), env=env, capture_output=True, timeout=cap_s)
    except subprocess.TimeoutExpired:
        return {"code": "probe_failed", "message": f"probe timed out after {cap_s}s"}
    except OSError as exc:
        return {"code": "probe_failed", "message": f"probe could not start: {exc}"}
    if proc.returncode != 0:
        return {"code": "probe_failed", "message": f"probe exited {proc.returncode}"}
    after = {p.name for p in layout.root.iterdir()} if layout.root.exists() else set()
    if after - before:
        return {"code": "probe_escaped_run_dir", "message": f"probe wrote {sorted(after - before)} outside the run directory"}
    return None


def run(layout: Layout, job: dict) -> dict:
    """The preflight result for one job block, `{ok, at, template, probe_digest, detail}` -- never raises; a
    problem this function cannot itself classify is `detail['code'] == 'preflight_error'`, ok False."""
    job = dict(job)
    digest = block_digest(job)
    template_id = job.get("template")
    adapter = job.get("adapter")
    try:
        return _run(layout, job, adapter, template_id, digest)
    except Exception as exc:                                     # noqa: BLE001 - a broken check is a failed preflight
        return _fail("preflight_error", f"{type(exc).__name__}: {exc}", template=template_id, digest=digest)


def _run(layout: Layout, job: dict, adapter: str | None, template_id: str | None, digest: str) -> dict:
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
    if tmpl and adapter == "process":
        # Only a template's own declared slots are checked at all (RECOVERY_PLAN.md section 1.4): a literal
        # `argv` job declares none, so nothing in its `params` is required to exist or match a type here. A
        # python job's `params` go straight to its registered function, never through a declared slot -- its
        # own signature (`_run_python`) is the one check that binds them, templated or literal alike.
        declared = tmpl.get("params") or {}
        failed = _param_checks(params, declared, layout, digest, template_id)
        if failed:
            return failed

    timeout_s = job.get("timeout_s")
    cap = tmpl.get("timeout_s")
    from . import timeouts
    lane_cap = cap if cap is not None else (timeouts.PROCESS_CAP_S if adapter == "process" else None)
    if timeout_s is not None and lane_cap is not None and timeout_s > lane_cap:
        return _fail("timeout_too_high", f"timeout_s {timeout_s} is above the lane's cap {lane_cap}",
                    template=template_id, digest=digest)

    needs = tmpl.get("needs") or {}
    duration_min = needs.get("duration_min")
    if duration_min is not None and timeout_s is not None and timeout_s < duration_min * 60:
        return _fail("duration_below_needs",
                    f"timeout_s {timeout_s} is below the template's needs.duration_min ({duration_min} min)",
                    template=template_id, digest=digest)

    if adapter == "python":
        return _run_python(layout, job, tmpl, params, template_id, digest)
    return _run_process(layout, job, tmpl, params, template_id, digest)


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
    canonical["preflight"] = run(layout, canonical)
    doc["job"] = canonical
