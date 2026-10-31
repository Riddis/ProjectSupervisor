"""The configuration this plugin owns: `plugins.review_rounds` in a project's config/routing.yaml, validated by
this model before setup() runs. Read it with `supervisor.plugin.config(layout, "review_rounds")`."""
from __future__ import annotations

from pydantic import BaseModel


class Settings(BaseModel):
    """Empty until the review grid's own constants move in (boundary plan phase 5, step 5)."""
