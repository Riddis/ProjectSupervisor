"""The project's one reader of `config/routing.yaml` (R-28): every module that needs the file calls `load()` (or
an existing name that now delegates to it) instead of opening and parsing it itself. `load()` never raises --
{} when the file is absent, unreadable or not a mapping, so every caller applies its own defaults for whatever
`load()` does not have. `read_raw()` is the one exception: `doctor.check_routing` tells a missing file apart from
a malformed one for its own diagnostic message, which `load()`'s single return value for both cannot do."""
from __future__ import annotations

from pathlib import Path

import yaml

from .paths import Layout


def read_raw(path: Path) -> dict:
    """Parsed YAML at `path` as a mapping (empty document -> {}); raises OSError or yaml.YAMLError, unlike
    `load()`, so a caller that must distinguish "missing" from "malformed" can."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def load(layout: Layout | None) -> dict:
    """config/routing.yaml as a mapping; {} when `layout` is None or the file is absent, unreadable or malformed."""
    if layout is None:
        return {}
    cfg = layout.config / "routing.yaml"
    if not cfg.exists():
        return {}
    try:
        return read_raw(cfg)
    except (OSError, yaml.YAMLError):
        return {}
