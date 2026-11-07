"""The configuration this plugin owns: `plugins.comfy` in a project's config/routing.yaml, validated by
this model before setup() runs. Read it with `supervisor.plugin.config(layout, "comfy")`, or call this package's
`comfy_config(layout)` for the same section with its defaults filled in."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Ffmpeg(BaseModel):
    """plugins.comfy.ffmpeg: where ffmpeg lives (`root`, with ffmpeg.exe under <root>/bin)."""
    model_config = ConfigDict(extra="allow")
    root: str | None = None


class Settings(BaseModel):
    """Declares the values with a default, the same ones comfy_config() fills in; other keys (`root`, the ComfyUI
    install, a `vram_floor_mb` override) pass through as written. `pinned_nodes` and `pins_file` name the
    installation's own pin manifest (empty/generic by default: no custom node is required unless configured).
    `vram_device` names the card this lane's preflight reads: a key of `resources.gpu.devices`, or a bare
    integer index, resolved through `supervisor.plugin.gpu_device`. Declared here rather than left to
    `extra="allow"` - an undeclared key is accepted and silently ignored, which would defeat the whole point of
    naming a device. Unset (the default) resolves to no device at all, which is every project that configures
    nothing and the behaviour every prior release has."""
    model_config = ConfigDict(extra="allow")
    base_url: str = "http://127.0.0.1:8188"
    timeout_s: int = 1800
    defer_seconds: float = 30.0
    ws_poll_budget_s: float = 1.5
    download_attempts: int = 3
    download_retry_s: float = 1.0
    pinned_nodes: list[str] = []
    pins_file: str = "comfy_pins.json"
    ffmpeg: Ffmpeg = Ffmpeg()
    vram_device: str | int | None = None
