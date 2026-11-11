"""llm_endpoint: a Supervisor plugin package.

A generic OpenAI-compatible chat-completions lane for text-only work (D-013 R2): text in, text or
schema-validated JSON out. It knows nothing about any particular server - the binary, its argv, the health URL
and the model name are configuration - so the same lane serves any server that speaks the shape. The HTTP call
runs in a short-lived child process (`_worker.py`), same pid/create_time handle and result.json contract as
every other `process`-family adapter; the server itself is started and stopped by a separate watchdog
(`_server.py`) that outlives any one job. GPU-locked (`gpu = True`): one GPU-bound lane runs at a time
(`resources.gpu.single_job`), and, where the project configures `resources.gpu.lease`, the machine-wide lease
is held by the watchdog for as long as the server is up.

Import only `supervisor.plugin` and `supervisor.testing` (in tests); register only inside setup()."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from supervisor import plugin

from . import _server

job_spec = plugin.job_spec

DEFER_SECONDS = 30.0
CHARS_PER_TOKEN = 4                 # a deterministic, conservative estimate: the budget check never calls a model


def llm_endpoint_config(layout: plugin.Layout) -> dict:
    cfg = plugin.config(layout, "llm_endpoint")
    cfg.setdefault("base_url", "http://127.0.0.1:8080")
    cfg.setdefault("health_path", "/health")
    cfg.setdefault("start_timeout_s", 300)
    cfg.setdefault("idle_stop_s", 900)
    cfg.setdefault("timeout_s", 1800)
    cfg.setdefault("context_tokens", 8192)
    cfg.setdefault("max_tokens", 2048)
    return cfg


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def _preflight_devices(layout: plugin.Layout) -> list[tuple[str | None, int | None, int]]:
    """(name, index, floor) for every card the preflight must check: every entry of `resources.gpu.devices`
    when any is configured (section 1: "both cards, each against its own floor"), else the one shared
    `usable_vram_floor_mb` against the default card - the single-card behaviour every project without named
    devices already has."""
    gpu_cfg = plugin.resources(layout).get("gpu") or {}
    devices = gpu_cfg.get("devices") or {}
    default_floor = gpu_cfg.get("usable_vram_floor_mb", 0)
    if devices:
        return [(name, entry.get("index"), entry.get("usable_vram_floor_mb") or default_floor)
                for name, entry in devices.items()]
    return [(None, None, default_floor)]


class LlmEndpointAdapter(plugin.ProcessAdapter):
    name = "llm_endpoint"
    gpu = True

    def prepare(self, layout: plugin.Layout, task, attempt_id: str) -> dict:
        job = job_spec(task)
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        cfg = llm_endpoint_config(layout)
        prompt = job.get("prompt", "")
        if job.get("prompt_file"):
            prompt = (layout.root / job["prompt_file"]).read_text(encoding="utf-8")
        schema = job.get("schema")
        if job.get("schema_file"):
            schema = json.loads((layout.root / job["schema_file"]).read_text(encoding="utf-8"))
        max_tokens = int(job.get("max_tokens", cfg["max_tokens"]))
        context_tokens = int(job.get("context_tokens", cfg["context_tokens"]))
        budget = int(0.9 * context_tokens) - max_tokens
        estimate = _estimate_tokens(prompt)
        if estimate > budget:
            raise plugin.PluginError(f"prompt estimated at {estimate} tokens exceeds the budget "
                                     f"0.9 x {context_tokens} - {max_tokens} = {budget}")
        request = {"base_url": job.get("base_url", cfg["base_url"]), "model": job.get("model", cfg.get("model")),
                   "prompt": prompt, "schema": schema, "max_tokens": max_tokens,
                   "timeout_s": int(job.get("timeout_s", cfg["timeout_s"])), "run_root": str(layout.root),
                   "output_file": job.get("output_file"), "activity_file": str(layout.runtime / "llm_endpoint_activity")}
        plugin.atomic_json(run_dir / "request.json", request)
        worker = str(Path(__file__).with_name("_worker.py"))
        return {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir),
                "root": str(layout.root), "timeout_s": request["timeout_s"] + 30, "output_file": job.get("output_file"),
                "argv": [job.get("python", sys.executable), worker, str(run_dir / "request.json"), str(run_dir / "result.json")],
                "cfg": cfg, "devices": _preflight_devices(layout)}

    def submit(self, spec: dict) -> dict:
        for name, index, floor in spec.get("devices") or []:
            if not floor:
                continue
            free = plugin.gpu.free_mb(index)
            if free is not None and free < floor:
                label = name or (f"#{index}" if index is not None else None)
                prefix = f"GPU {label} free VRAM" if label else "GPU free VRAM"
                raise plugin.CapacityDeferred(time.time() + DEFER_SECONDS, f"{prefix} {free}MiB below floor {floor}MiB")
        _server.ensure_started(spec["cfg"])
        return super().submit(spec)


ADAPTER = LlmEndpointAdapter()


def setup(registry) -> None:
    registry.adapter(ADAPTER)
