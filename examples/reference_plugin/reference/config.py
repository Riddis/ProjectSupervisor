"""The reference plugin's configuration: `plugins.reference` in a project's config/routing.yaml, validated by this
model before setup() runs (docs/PLUGIN_STANDARD.md, Configuration)."""
from __future__ import annotations

from pydantic import BaseModel


class Settings(BaseModel):
    paused: bool = False                 # true: its lane defers every job (no attempt spent)
    forbidden_word: str | None = None    # its packet screen refuses a brief containing this word
    agent_lane: str | None = None        # an agent lane whose executable the doctor check reports (nothing launched)
