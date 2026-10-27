"""Validated edits to config/routing.yaml (Control Center Settings panel). Only a short allow-list of numeric fields is
editable from the UI — routing.yaml is hand-authored with owner-decision comments that a plain
yaml.safe_dump round-trip would destroy, so this uses ruamel.yaml's round-trip mode, which preserves comments and
layout, and writes back only the one field that changed."""
from __future__ import annotations

from typing import Any

from ruamel.yaml import YAML

from .locks import FileLock
from .paths import Layout

_yaml = YAML()
_yaml.preserve_quotes = True

# field path -> (type, min, max): the only knobs the Control Center may change.
EDITABLE: dict[tuple[str, ...], tuple[type, float, float]] = {
    ("capacity", "claude", "reserve_percent"): (int, 0, 100),
    ("capacity", "codex", "reserve_percent"): (int, 0, 100),
    ("limits", "max_agent_jobs"): (int, 1, 10),
    ("limits", "max_resource_jobs"): (int, 1, 4),
}


class SettingsError(ValueError):
    pass


def path(layout: Layout):
    return layout.config / "routing.yaml"


def load(layout: Layout) -> Any:
    with path(layout).open("r", encoding="utf-8") as f:
        return _yaml.load(f)


def _get(doc, keys):
    cur = doc
    for k in keys:
        cur = cur[k]
    return cur


def view(layout: Layout) -> dict:
    doc = load(layout)
    out = {}
    for keys in EDITABLE:
        try:
            out[".".join(keys)] = _get(doc, keys)
        except (KeyError, TypeError):
            out[".".join(keys)] = None
    return out


def update(layout: Layout, field: str, value: str, actor: str = "owner-ui") -> dict:
    keys = tuple(field.split("."))
    spec = EDITABLE.get(keys)
    if spec is None:
        raise SettingsError(f"{field} is not an editable setting")
    typ, lo, hi = spec
    try:
        typed = typ(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{field}: {value!r} is not a valid {typ.__name__}") from exc
    if not (lo <= typed <= hi):
        raise SettingsError(f"{field} must be between {lo} and {hi}")
    with FileLock(layout.state / ".routing.lock", owner="settings", timeout=5.0):
        doc = load(layout)
        cur = doc
        for k in keys[:-1]:
            cur = cur[k]
        before = cur.get(keys[-1])
        cur[keys[-1]] = typed
        with path(layout).open("w", encoding="utf-8") as f:
            _yaml.dump(doc, f)
    if keys[0] == "capacity":
        from . import capacity
        capacity.configure(layout)                     # this process applies the new floor now, whatever the stamp says
    from . import tasks as tasks_mod
    tasks_mod.log(layout, "settings_update", actor, field=field, before=before, after=typed)
    return {"field": field, "before": before, "after": typed}
