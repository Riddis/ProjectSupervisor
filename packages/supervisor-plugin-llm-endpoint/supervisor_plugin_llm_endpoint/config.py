"""The configuration this plugin owns: `plugins.llm_endpoint` in a project's `config/routing.yaml`, validated by
this model before `setup()` runs. Read it with `supervisor.plugin.config(layout, "llm_endpoint")`.

Every key is a neutral default or none at all (D-013 design section 6): no model, no weight path, no server
argv assumes a project. `base_url` defaults to `llama.cpp`'s own documented loopback port (the tool's choice,
not a project's); a project that runs a different endpoint, or more than one profile, overrides it entirely.
`argv` has no default: the server command line (binary path, model path, tensor split, `--n-cpu-moe`, context
length, ...) is a project's own measured values (T5-02), never guessed here. Unlike the framework's single-card
GPU-locked lanes, this one has no `vram_device` selector: the design's whole point is a model split by layer
across every configured card, so the adapter's preflight (`resources.gpu.devices`) checks every one of them
against its own floor, not a single chosen one."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")
    base_url: str = "http://127.0.0.1:8080"
    model: str | None = None
    argv: list[str] | None = None
    health_path: str = "/health"
    start_timeout_s: int = 300
    idle_stop_s: int = 900
    timeout_s: int = 1800
    context_tokens: int = 8192
    max_tokens: int = 2048
