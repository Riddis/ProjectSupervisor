"""The configuration this plugin owns: `plugins.local_llm` in a project's config/routing.yaml, validated by
this model before setup() runs. Read it with `supervisor.plugin.config(layout, "local_llm")`."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Settings(BaseModel):
    """Declares the values with a default, the same ones local_llm_config() fills in; other keys
    (`models_dir`, a `vram_floor_mb` override) pass through as written."""
    model_config = ConfigDict(extra="allow")
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3-vl:4b"
    timeout_s: int = 300
