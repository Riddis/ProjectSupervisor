"""`project.yaml`: the one file that makes a directory a supervisor project. Read-mostly; the setup skill and
`supervisor init` write it, the framework reads it (name, phase labels, extensions, framework version)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from . import __version__, store, yaml_edit
from .paths import Layout

# `write` changes only what differs (supervisor.yaml_edit): project.yaml is born from the commented template, and
# the comments and the owner's layout are what most owners read, so an update (migration, `extension new`) keeps
# them.

DEFAULTS: dict[str, Any] = {
    "name": "Untitled project",
    "description": "",
    "framework_version": __version__,
    "repo": {"path": ".", "remote": None, "default_branch": "main"},
    "phases": {},            # {0: "Environment", 1: "Foundations", ...} — labels only; gating lives in state/gates.yaml
    "extensions": [],        # import paths relative to extensions/ (e.g. "my_adapter") or dotted module names
    "ui": {"port": 8790, "title": None},
    "owner": {"name": "owner"},
}


def path(layout: Layout) -> Path:
    return layout.project_file


def _stamp(p: Path) -> tuple:
    try:
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(p), 0, 0)


def _read_raw(p: Path) -> tuple[dict, str]:
    """(document as written, problem): the problem is a one-line note when the file is missing, malformed or
    not a mapping — the raw document is then empty. Callers that must not crash merge DEFAULTS over it; the
    doctor/migrate paths look at the problem."""
    try:
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as exc:
        return {}, f"{p} unreadable: {exc}"
    except yaml.YAMLError as exc:
        return {}, f"{p} is malformed YAML: {str(exc).splitlines()[0][:160]}"
    if loaded is None:
        return {}, ""
    if not isinstance(loaded, dict):
        return {}, f"{p} must be a YAML mapping, not {type(loaded).__name__}"
    return loaded, ""


@lru_cache(maxsize=16)
def _load_cached(stamp: tuple) -> dict:
    p = Path(stamp[0])
    doc = _read_raw(p)[0] if p.exists() else {}
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in doc.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def load(layout: Layout) -> dict:
    """The project document merged over DEFAULTS (cached on the file's mtime/size)."""
    return _load_cached(_stamp(path(layout)))


def exists(layout: Layout) -> bool:
    return path(layout).is_file()


def raw(layout: Layout) -> dict:
    """The document exactly as written (no DEFAULTS merged); {} when the file is missing or malformed."""
    return _read_raw(path(layout))[0] if exists(layout) else {}


def problem(layout: Layout) -> str | None:
    """Why project.yaml cannot be trusted, or None: malformed YAML, not a mapping, or no `framework_version`
    (so nothing says which framework version wrote the state — `supervisor migrate` records the installed one).
    A missing file is not a problem here (`doctor` reports it separately; DEFAULTS apply)."""
    if not exists(layout):
        return None
    doc, note = _read_raw(path(layout))
    if note:
        return note
    if not str(doc.get("framework_version") or ""):
        return "project.yaml has no framework_version (cannot tell which version wrote the state); run `supervisor migrate`"
    return None


def name(layout: Layout) -> str:
    return str(load(layout).get("name") or DEFAULTS["name"])


def phase_label(layout: Layout, phase: int, default: str = "Phase") -> str:
    phases = load(layout).get("phases") or {}
    label = phases.get(phase, phases.get(str(phase)))
    if isinstance(label, dict):
        label = label.get("title") or label.get("label")
    return str(label) if label else f"{default} {phase}"


def phase_labels(layout: Layout) -> dict[int, str]:
    out: dict[int, str] = {}
    for k, v in (load(layout).get("phases") or {}).items():
        try:
            n = int(k)
        except (TypeError, ValueError):
            continue
        out[n] = str(v.get("title") or v.get("label") or n) if isinstance(v, dict) else str(v)
    return out


def extensions(layout: Layout) -> list[str]:
    """Configured extension names. Entries are a bare name or a mapping with `name` (and `required`, see
    supervisor.extensions.specs); this returns the names only."""
    names = []
    for x in load(layout).get("extensions") or []:
        name = x.get("name") if isinstance(x, dict) else x
        if name:
            names.append(str(name))
    return names


def ui_port(layout: Layout) -> int:
    try:
        return int((load(layout).get("ui") or {}).get("port") or 8790)
    except (TypeError, ValueError):
        return 8790


def write(layout: Layout, doc: dict) -> Path:
    """Write project.yaml (full document) changing only what differs: the changed values' tokens when only existing
    values changed (migrate's framework_version), else a round trip that keeps comments, long lines, `null` and the
    file's indentation (supervisor.yaml_edit). Atomic, in the file's own line endings. Used by migrations and
    `supervisor extension new`; `supervisor init` stamps the template instead."""
    p = path(layout)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_bytes().decode("utf-8") if p.is_file() else ""
    store.rewrite_text(p, yaml_edit.edit(text, dict(doc)))
    _load_cached.cache_clear()
    return p


def version_mismatch(layout: Layout) -> str | None:
    """A one-line note when project.yaml cannot be trusted (see `problem`) or records a framework version other
    than the installed one, else None. The doctor's project check and the Control Center's Start use it."""
    note = problem(layout)
    if note:
        return note
    recorded = str(load(layout).get("framework_version") or "")
    if recorded and recorded != __version__:
        return f"project.yaml records framework {recorded}; installed {__version__} — run `supervisor migrate`"
    return None
