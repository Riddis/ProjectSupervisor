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


def llm_endpoint_config(layout: plugin.Layout) -> dict:
    cfg = plugin.config(layout, "llm_endpoint")
    cfg.setdefault("base_url", "http://127.0.0.1:8080")
    cfg.setdefault("health_path", "/health")
    cfg.setdefault("start_timeout_s", 300)
    cfg.setdefault("idle_stop_s", 900)
    cfg.setdefault("timeout_s", 1800)
    cfg.setdefault("context_tokens", 8192)
    cfg.setdefault("max_tokens", 2048)
    cfg.setdefault("chars_per_token", 3)
    return cfg


def _estimate_tokens(text: str, chars_per_token: int) -> int:
    return len(text) // chars_per_token


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
        chars_per_token = int(cfg["chars_per_token"])
        budget = int(0.9 * context_tokens) - max_tokens
        estimate = _estimate_tokens(prompt, chars_per_token)
        if estimate > budget:
            raise plugin.PluginError(f"prompt estimated at {estimate} tokens ({chars_per_token} chars/token) "
                                     f"exceeds the budget 0.9 x {context_tokens} - {max_tokens} = {budget}")
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
        cfg = spec["cfg"]
        # The floor says whether a *start* will work (D-013 section 4); a resident server already
        # holds exactly the VRAM it guards, so it is only asked before a start is attempted - never
        # against a server that is already up and answering (F3, RELEASE_REVIEW_local-lane.md).
        if not _server._health_ok(cfg["base_url"], cfg.get("health_path", "/health")):
            for name, index, floor in spec.get("devices") or []:
                if not floor:
                    continue
                free = plugin.gpu.free_mb(index)
                if free is not None and free < floor:
                    label = name or (f"#{index}" if index is not None else None)
                    prefix = f"GPU {label} free VRAM" if label else "GPU free VRAM"
                    raise plugin.CapacityDeferred(time.time() + DEFER_SECONDS,
                                                  f"{prefix} {free}MiB below floor {floor}MiB")
        if not _server.ensure_started(cfg, spec["root"]):
            raise plugin.CapacityDeferred(time.time() + DEFER_SECONDS,
                                          f"{cfg['base_url']} not ready yet (cold start)")
        return super().submit(spec)


ADAPTER = LlmEndpointAdapter()


# ------------------------------------------------------------------ doctor check


def _models_present(base_url: str, model: str) -> bool | None:
    """True/False once `/v1/models` answers, None when it cannot be asked (reachable does not imply this
    endpoint, or any endpoint, serves that route)."""
    import requests
    try:
        resp = requests.get(base_url.rstrip("/") + "/v1/models", timeout=5.0)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    ids = {m.get("id") for m in (resp.json() or {}).get("data") or [] if isinstance(m, dict)}
    return model in ids


def check_llm_endpoint(layout: plugin.Layout) -> plugin.Check:
    cfg = llm_endpoint_config(layout)
    reachable = _server._health_ok(cfg["base_url"], cfg.get("health_path", "/health"))
    if not reachable:
        if cfg.get("argv"):
            return plugin.Check("llm_endpoint", True, f"{cfg['base_url']} not reachable now; configured to start "
                                "on demand (llm_endpoint.start, or a job that needs it)", warn=True)
        return plugin.Check("llm_endpoint", False, f"{cfg['base_url']} unreachable and plugins.llm_endpoint.argv "
                            "is not configured: nothing can start it")
    model = cfg.get("model")
    if model:
        present = _models_present(cfg["base_url"], model)
        if present is False:
            return plugin.Check("llm_endpoint", False, f"{cfg['base_url']} reachable, but {model} is not in "
                                "its /v1/models")
    try:
        state = plugin.read_json(_server.state_path(layout.root))
    except (OSError, ValueError):
        state = {}
    pid = state.get("pid")
    if pid is not None:
        if not _server._process_alive(pid, state.get("create_time")):
            return plugin.Check("llm_endpoint", False, f"{cfg['base_url']} reachable, but the state file names "
                                f"pid {pid}, which is not running: the watchdog may have crashed without cleaning up")
        if not _server._process_alive(state.get("watchdog_pid"), state.get("watchdog_create_time")):
            # F5: the state names a dead watchdog while its server is alive - nothing renews its lease, nothing
            # idle-stops it, and reporting it "healthy" is exactly how it goes unnoticed.
            return plugin.Check("llm_endpoint", False, f"{cfg['base_url']} reachable and its pid {pid} is "
                                "running, but the state names a dead watchdog while its server is alive: "
                                "nothing will stop or renew the lease for it; llm_endpoint.stop will kill it")
    return plugin.Check("llm_endpoint", True, f"{cfg['base_url']} healthy" + (f", {model} present" if model else ""))


# ------------------------------------------------------------------ owner commands


def _start_command(layout: plugin.Layout, payload: dict, actor: str) -> dict:
    cfg = llm_endpoint_config(layout)
    ready = _server.ensure_started(cfg, str(layout.root))
    return {"status": "ok", "ready": ready}


def _stop_command(layout: plugin.Layout, payload: dict, actor: str) -> dict:
    # F5: a stop request written for a dead watchdog has no reader. When the state names one, this command
    # kills the orphaned server directly instead of touching a file nobody is left to notice.
    orphan_pid = _server.dead_watchdog_with_live_server(str(layout.root))
    if orphan_pid is not None:
        _server._kill_process(orphan_pid)
        return {"status": "ok", "killed_orphan_pid": orphan_pid}
    _server.request_stop(str(layout.root))
    return {"status": "ok"}


def setup(registry) -> None:
    registry.adapter(ADAPTER)
    registry.doctor_check(check_llm_endpoint)
    registry.command("llm_endpoint.start", _start_command)
    registry.command("llm_endpoint.stop", _stop_command)
