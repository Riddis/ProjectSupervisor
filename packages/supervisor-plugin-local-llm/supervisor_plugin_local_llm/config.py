"""The configuration this plugin owns: `plugins.local_llm` in a project's config/routing.yaml, validated by
this model before setup() runs. Read it with `supervisor.plugin.config(layout, "local_llm")`."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Settings(BaseModel):
    """Declares the values with a default, the same ones local_llm_config() fills in; a `vram_floor_mb` override
    still passes through unwritten here. `model` has no default: this package drives more than one model family,
    so a task's job or this section must name one, and `check_ollama` says so clearly when neither does.
    `models_store_drive` is unset by default: the drive rule is a project's own choice, not this package's.
    `models_dir` is unset here too (`None`): the schema has no project root to resolve it against, so
    local_llm_config() fills in the actual default, `<project root>/models/ollama`, only once this validates -
    root-relative, no project information, and only ever quoted back in doctor's "expected ..." text.
    `vram_device` names the card this lane's preflight reads: a key of `resources.gpu.devices`, or a bare
    integer index, resolved through `supervisor.plugin.gpu_device`. Declared here rather than left to
    `extra="allow"` - an undeclared key is accepted and silently ignored, which would defeat the whole point of
    naming a device. Unset (the default) resolves to no device at all, which is every project that configures
    nothing and the behaviour every prior release has."""
    model_config = ConfigDict(extra="allow")
    base_url: str = "http://127.0.0.1:11434"
    model: str | None = None
    timeout_s: int = 300
    models_store_drive: str | None = None
    models_dir: str | None = None
    vram_device: str | int | None = None
