"""`supervisor.ext.policy`: an extension's policy floor and the framework's policy diagnostics (docs/EXTENSION_API.md,
Policy). Everything returned is a copy: the framework's cached policy is never handed out."""
from __future__ import annotations

import copy

from .. import policy as _policy
from ..paths import Layout
from ..policy import PolicyViolation

__all__ = ["PolicyViolation", "config_problems", "load", "merge_floor", "secret_scan_problems", "section"]


def merge_floor(floor: dict, configured: dict | None) -> tuple[dict, list[str]]:
    """The framework's merge rule, for an extension's own floor: `configured` (from config/policy.yaml) over `floor`,
    section by section. A scalar from the config wins, a list is unioned with the floor's, and an entry whose kind
    differs from the floor entry it would replace (a string for a flag, null for a list) is dropped and reported.
    Returns `(effective, rejected entries as text)`; neither input is changed."""
    return _policy._merge(copy.deepcopy(floor), copy.deepcopy(configured or {}))


def load(layout: Layout) -> dict:
    """The effective config/policy.yaml: the framework's floor merged with the file; sections the framework has no
    floor for (an extension's) as written. A copy."""
    return copy.deepcopy(_policy.load(layout))


def section(layout: Layout, name: str) -> dict:
    """One section of `load()`, or {} when absent or not a mapping. A copy."""
    value = _policy.load(layout).get(name)
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def config_problems(layout: Layout) -> list[str]:
    """The config/policy.yaml entries the framework rejected (unparseable file, wrong kind for its floor): each is a
    value the owner wrote that is not in effect."""
    return list(_policy.config_problems(layout))


def secret_scan_problems(layout: Layout) -> list[str]:
    """Why the credential store could not be screened completely; non-empty means every packet is refused. Names
    files, never their contents."""
    return list(_policy.secret_scan_problems(layout))
