"""`project.yaml`: the one file that makes a directory a supervisor project. Read-mostly; the setup skill and
`supervisor init` write it, the framework reads it (name, phase labels, extensions, framework version)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .paths import Layout

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


@lru_cache(maxsize=16)
def _load_cached(stamp: tuple) -> dict:
    p = Path(stamp[0])
    doc: dict = {}
    if p.exists():
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
            doc = loaded if isinstance(loaded, dict) else {}
        except yaml.YAMLError:
            doc = {}
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
    return [str(x) for x in (load(layout).get("extensions") or []) if x]


def ui_port(layout: Layout) -> int:
    try:
        return int((load(layout).get("ui") or {}).get("port") or 8790)
    except (TypeError, ValueError):
        return 8790


def write(layout: Layout, doc: dict) -> Path:
    """Write project.yaml (full document). Used by `supervisor init` and migrations only."""
    p = path(layout)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _load_cached.cache_clear()
    return p


def version_mismatch(layout: Layout) -> str | None:
    """A one-line note when the installed framework differs from the version recorded in project.yaml, else None."""
    recorded = str(load(layout).get("framework_version") or "")
    if recorded and recorded != __version__:
        return f"project.yaml records framework {recorded}; installed {__version__} — run `supervisor migrate`"
    return None
