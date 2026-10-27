"""Validated edits to config/routing.yaml (Control Center Settings panel). Only a short allow-list of numeric fields is
editable from the UI. routing.yaml is hand-authored with owner-decision comments, so an edit changes only the value's
own token (supervisor.yaml_edit) and the file is replaced atomically in its own line-ending style; before 1.11.0 the
ruamel round trip reflowed long lines and rewrote every line ending in place."""
from __future__ import annotations

import copy
from typing import Any

import yaml
from ruamel.yaml import YAML

from . import store, yaml_edit
from .locks import FileLock
from .paths import Layout

_yaml = YAML()
_yaml.preserve_quotes = True

# field path -> (type, min, max): the only knobs the Control Center may change.
# Each engine has two capacity floors, one per measured window: dispatch waits while the window's remaining share is
# at or below its floor. Both are editable (before 1.11.0 only the 5-hour one was, so a held 7-day window looked
# like a floor that ignored the owner).
EDITABLE: dict[tuple[str, ...], tuple[type, float, float]] = {
    ("capacity", "claude", "reserve_percent"): (int, 0, 100),
    ("capacity", "claude", "weekly_gate_percent"): (int, 0, 100),
    ("capacity", "codex", "reserve_percent"): (int, 0, 100),
    ("capacity", "codex", "weekly_gate_percent"): (int, 0, 100),
    ("limits", "max_agent_jobs"): (int, 1, 10),
    ("limits", "max_resource_jobs"): (int, 1, 4),
}
# What the Settings page calls each field.
LABELS: dict[str, str] = {
    "capacity.claude.reserve_percent": "Claude: 5-hour reserve (% remaining)",
    "capacity.claude.weekly_gate_percent": "Claude: 7-day floor (% remaining)",
    "capacity.codex.reserve_percent": "Codex: 5-hour reserve (% remaining)",
    "capacity.codex.weekly_gate_percent": "Codex: 7-day floor (% remaining)",
    "limits.max_agent_jobs": "Agent jobs at once",
    "limits.max_resource_jobs": "Heavy-resource jobs at once",
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
        text = path(layout).read_bytes().decode("utf-8")
        doc = yaml.safe_load(text) or {}
        if not isinstance(doc, dict):
            raise SettingsError("config/routing.yaml is not a mapping")
        new = copy.deepcopy(doc)
        cur = new
        for k in keys[:-1]:
            if cur.get(k) is None:
                cur[k] = {}
            if not isinstance(cur[k], dict):
                raise SettingsError(f"config/routing.yaml: {k} is not a mapping, so {field} cannot be set")
            cur = cur[k]
        before = cur.get(keys[-1])
        cur[keys[-1]] = typed
        try:
            rewritten = yaml_edit.edit(text, new)
        except yaml_edit.EditRefused as exc:
            raise SettingsError(f"config/routing.yaml: {field} was not saved: {exc}") from exc
        store.rewrite_text(path(layout), rewritten)
    if keys[0] == "capacity":
        from . import capacity
        capacity.configure(layout)                     # this process applies the new floor now, whatever the stamp says
    from . import tasks as tasks_mod
    tasks_mod.log(layout, "settings_update", actor, field=field, before=before, after=typed)
    return {"field": field, "before": before, "after": typed}
