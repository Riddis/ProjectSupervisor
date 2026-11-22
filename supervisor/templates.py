"""`config/job_templates.yaml`: project data, never shipped by the framework.
A template is the executable line a plan may only *reference* (`template`, a plan's own
writer) — never author — so this loader is the one place that line's own shape is checked, before
`supervisor.preflight` ever runs a job block against it.

A malformed file loads no template at all: `load()` raises `TemplateError` naming every problem at once, never a
partial set a caller could silently run a job against half-checked.
"""
from __future__ import annotations

import ntpath
import posixpath

import yaml

from .paths import Layout

# A template whose argv is `["sh", "-c", "{script}"]` defeats everything above.
# The loader refuses a template whose argv[0] basename is one of these unless the template is owner-only
# (`may_be_set_by: [owner]`) -- a check, not a guarantee of what the executable itself then does.
SHELLS = ("sh", "bash", "cmd", "powershell", "pwsh", "zsh")
PARAM_TYPES = ("path", "enum", "int", "str", "port")


class TemplateError(ValueError):
    pass


def basename(argv0: str) -> str:
    """The executable name an argv[0] resolves to, lowered and stripped of a Windows `.exe` suffix, so a full
    path to an interpreter and its bare name are recognised as the same one."""
    name = posixpath.basename(str(argv0))
    name = ntpath.basename(name)
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name.lower()


def is_shell(argv0: str) -> bool:
    return basename(argv0) in SHELLS


def _validate_one(tid: str, tmpl) -> list[str]:
    if not isinstance(tmpl, dict):
        return [f"template {tid!r} is not a mapping"]
    errors: list[str] = []
    adapter = tmpl.get("adapter")
    if not isinstance(adapter, str) or not adapter:
        errors.append(f"template {tid!r}: adapter must be a non-empty string")

    may = tmpl.get("may_be_set_by", [])
    if not isinstance(may, list) or any(v not in ("owner", "plan") for v in may):
        errors.append(f"template {tid!r}: may_be_set_by must be a list of 'owner'/'plan'")

    params = tmpl.get("params", {})
    if not isinstance(params, dict):
        errors.append(f"template {tid!r}: params must be a mapping")
        params = {}
    for slot, decl in params.items():
        if not isinstance(decl, dict):
            errors.append(f"template {tid!r}: param {slot!r} must declare a mapping")
            continue
        ptype = decl.get("type")
        if ptype is not None and ptype not in PARAM_TYPES:
            errors.append(f"template {tid!r}: param {slot!r} has an undeclared type {ptype!r}")

    if adapter == "process":
        argv = tmpl.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            errors.append(f"template {tid!r}: a process template needs a non-empty argv of strings")
        elif is_shell(argv[0]) and list(may) != ["owner"]:
            errors.append(f"template {tid!r}: argv[0] ({argv[0]!r}) is a shell; only an owner-only template "
                          "(may_be_set_by: [owner]) may name one")
        probe = tmpl.get("probe")
        if probe is not None and (not isinstance(probe, list) or not all(isinstance(a, str) for a in probe)):
            errors.append(f"template {tid!r}: probe must be a list of strings")
    elif adapter == "python":
        if not isinstance(tmpl.get("fn"), str) or not tmpl["fn"]:
            errors.append(f"template {tid!r}: a python template needs a string fn")

    timeout_s = tmpl.get("timeout_s")
    if timeout_s is not None and (not isinstance(timeout_s, int) or isinstance(timeout_s, bool) or timeout_s <= 0):
        errors.append(f"template {tid!r}: timeout_s must be a positive int")

    needs = tmpl.get("needs")
    if needs is not None and not isinstance(needs, dict):
        errors.append(f"template {tid!r}: needs must be a mapping")

    resumable = tmpl.get("resumable")
    if resumable is not None and not isinstance(resumable, bool):
        errors.append(f"template {tid!r}: resumable must be a bool")
    return errors


def validate(data) -> list[str]:
    """Every problem in a `job_templates.yaml` document, collected at once (never a partial report)."""
    if not isinstance(data, dict):
        return ["job_templates.yaml must be a mapping"]
    if data.get("version") != 1:
        return [f"job_templates.yaml: version must be 1, got {data.get('version')!r}"]
    templates = data.get("templates")
    if not isinstance(templates, dict) or not templates:
        return ["job_templates.yaml: templates must be a non-empty mapping"]
    errors: list[str] = []
    for tid, tmpl in templates.items():
        errors += _validate_one(tid, tmpl)
    return errors


def load(layout: Layout) -> dict:
    """The validated `templates` mapping of `config/job_templates.yaml`; `{}` when the project declares none at
    all (a fresh project, or one with no process/python work of its own). Raises `TemplateError` naming every
    problem at once for a file that exists but does not validate -- never a partial, half-checked load."""
    path = layout.config / "job_templates.yaml"
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TemplateError(f"job_templates.yaml is not valid YAML: {exc}") from exc
    errors = validate(data)
    if errors:
        raise TemplateError("; ".join(errors))
    return data["templates"]
