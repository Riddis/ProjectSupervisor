"""The configuration this plugin owns: `plugins.comfy` in a project's config/routing.yaml, validated by
this model before setup() runs. Read it with `supervisor.plugin.config(layout, "comfy")`."""
from __future__ import annotations

from pydantic import BaseModel


class Settings(BaseModel):
    """Empty until the comfy lane's own constants move in (boundary plan phase 5, step 3)."""
