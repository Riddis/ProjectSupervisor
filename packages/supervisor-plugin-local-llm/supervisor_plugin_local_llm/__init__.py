"""local_llm: a Supervisor plugin package.

A vision/text model served by Ollama, asked for schema-valid JSON (`format: <json schema>` on `/api/generate`).
The HTTP call runs in a short-lived child process (`_worker.py`) so slow inference never blocks the supervisor
loop - same pid/create_time handle and result.json contract as every other `process`-family adapter. GPU-locked
(`gpu = True`): only one GPU-bound lane runs at a time (`resources.gpu.single_job`).

Ported from a project-local extension of the same shape (boundary plan phase 5, step 4: the model store's drive
is an optional `models_store_drive` key, never assumed to be a particular drive, and there is no default model -
a task's job or the `plugins.local_llm` section must name one). Import only `supervisor.plugin` and
`supervisor.testing` (in tests); register only inside setup()."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

from supervisor import plugin

DEFER_SECONDS = 30.0

job_spec = plugin.job_spec          # the framework's: a copy of the task's `job:` block


def local_llm_config(layout: plugin.Layout) -> dict:
    cfg = plugin.config(layout, "local_llm")
    cfg.setdefault("base_url", "http://127.0.0.1:11434")
    cfg.setdefault("timeout_s", 300)
    if cfg.get("models_dir") is None:        # Settings declares it unset; resolve it here, where layout.root is known
        cfg["models_dir"] = str(layout.root / "models" / "ollama")   # expected OLLAMA_MODELS; check_ollama (below)
    gpu_cfg = plugin.resources(layout)["gpu"]
    try:
        device = plugin.gpu_device(gpu_cfg, cfg.get("vram_device"))
    except plugin.PluginError as exc:
        raise plugin.PluginError(f"plugins.local_llm.vram_device: {exc}") from exc
    if device is not None:
        cfg["vram_device_index"] = device["index"]
        cfg["vram_device_name"] = device["name"]
    default_floor = device["vram_floor_mb"] if device and device["vram_floor_mb"] is not None \
        else gpu_cfg.get("usable_vram_floor_mb", 0)
    cfg.setdefault("vram_floor_mb", default_floor)
    return cfg


class LocalLlmAdapter(plugin.ProcessAdapter):
    name = "local_llm"
    gpu = True

    def prepare(self, layout: plugin.Layout, task, attempt_id: str) -> dict:
        job = job_spec(task)
        run_dir = layout.runs / task.id / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        cfg = local_llm_config(layout)
        schema = job.get("schema")
        if job.get("schema_file"):
            schema = json.loads((layout.root / job["schema_file"]).read_text(encoding="utf-8"))
        request = {"base_url": job.get("base_url", cfg["base_url"]), "model": job.get("model", cfg["model"]),
                   "prompt": job.get("prompt", ""), "schema": schema,
                   "images": [str(layout.root / p) for p in job.get("images", [])],
                   "options": job.get("options"), "timeout_s": int(job.get("timeout_s", cfg["timeout_s"]))}
        plugin.atomic_json(run_dir / "request.json", request)
        worker = str(Path(__file__).with_name("_worker.py"))
        spec = {"adapter": self.name, "task_id": task.id, "attempt_id": attempt_id, "run_dir": str(run_dir),
                "root": str(layout.root), "timeout_s": int(job.get("timeout_s", cfg["timeout_s"])) + 30,
                "argv": [job.get("python", sys.executable), worker, str(run_dir / "request.json"), str(run_dir / "result.json")],
                "vram_floor_mb": int(job.get("vram_mb", cfg["vram_floor_mb"]))}
        if "vram_device_index" in cfg:                 # only when a device is configured (1.0.0 drop-in otherwise)
            spec["vram_device_index"] = cfg["vram_device_index"]
            if cfg.get("vram_device_name") is not None:
                spec["vram_device_name"] = cfg["vram_device_name"]
        return spec

    def submit(self, spec: dict) -> dict:
        floor = spec.get("vram_floor_mb")
        if floor:
            index = spec.get("vram_device_index")
            free = plugin.gpu.free_mb(index)
            if free is not None and free < floor:
                label = spec.get("vram_device_name") or (f"#{index}" if index is not None else None)
                prefix = f"GPU {label} free VRAM" if label else "GPU free VRAM"
                raise plugin.CapacityDeferred(time.time() + DEFER_SECONDS, f"{prefix} {free}MiB below floor {floor}MiB")
        return super().submit(spec)


ADAPTER = LocalLlmAdapter()       # one instance; setup() registers it (importing this module registers nothing)


# ------------------------------------------------------------------ doctor check


def _model_aliases(model: str) -> set[str]:
    """The /api/tags names that are the configured model. The tag is part of the model: the adapter asks Ollama for
    `pixnote:4b` and gets a 404 when only `pixnote:8b` is pulled, so accepting any tag in the family reported a
    model that is not there. The only equivalence is Ollama's implicit `:latest` on an untagged pull."""
    base = model.strip()
    names = {base}
    if ":" not in base:
        names.add(f"{base}:latest")
    elif base.endswith(":latest"):
        names.add(base[: -len(":latest")])
    return names


def _registry_locations():
    """Windows only: the registry hives that hold persisted environment variables. Imported lazily so the module
    loads on the Linux test VM, where doctor simply reports the variable as unset."""
    try:
        import winreg
    except ImportError:
        return []
    return [(winreg.HKEY_CURRENT_USER, r"Environment", "User"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", "Machine")]


def _registry_env(name: str) -> tuple[str | None, str | None]:
    """(value, scope) of an environment variable from the User or Machine registry (checked in that order), or
    (None, None) if neither has it. A thin wrapper so tests can monkeypatch registry access directly instead of
    touching the real Windows registry."""
    for hive, subkey, scope in _registry_locations():
        import winreg                       # present whenever _registry_locations() is non-empty
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, name)
            if value:
                return str(value), scope
        except OSError:
            continue
    return None, None


def _resolve_models_dir(layout: plugin.Layout) -> tuple[str | None, str]:
    """Where Ollama is actually configured to store models, and how we know: `OLLAMA_MODELS` in doctor's own
    process environment first (the common case - a shell that set it before launching doctor), else the User/Machine
    registry (the server may have been started as a service, or before the variable existed, so doctor's own
    process env is not authoritative either; the registry is the next-best persisted source). Never silently falls
    back to the configured default here - an unset variable must read as unset, not as "looks fine"."""
    env_val = os.environ.get("OLLAMA_MODELS")
    if env_val:
        return env_val, "process environment"
    reg_val, scope = _registry_env("OLLAMA_MODELS")
    if reg_val:
        return reg_val, f"{scope} registry"
    return None, "unset"


def _manifest_paths(store: Path, alias: str) -> list[Path]:
    """Both manifest layouts Ollama has used (`manifests/registry.ollama.ai/...` and the newer
    `manifests-v2/ollama.com/...`), for one model:tag alias."""
    name, _, tag = alias.partition(":")
    tag = tag or "latest"
    return [store / "manifests" / "registry.ollama.ai" / "library" / name / tag,
            store / "manifests-v2" / "ollama.com" / "library" / name / tag]


def check_ollama(layout: plugin.Layout) -> plugin.Check:
    import requests

    try:
        cfg = local_llm_config(layout)
    except plugin.PluginError as exc:
        return plugin.Check("ollama", False, str(exc))
    wanted = cfg.get("model")
    if not wanted:
        return plugin.Check("ollama", False, "no model configured: set plugins.local_llm.model, or name one in "
                                              "the task's job")
    wanted = str(wanted)
    try:
        resp = requests.get(cfg["base_url"].rstrip("/") + "/api/tags", timeout=5.0)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return plugin.Check("ollama", False, f"{cfg['base_url']} unreachable ({exc}); start `ollama serve`")
    models = {m.get("name") for m in (resp.json() or {}).get("models") or [] if isinstance(m.get("name"), str)}
    if not _model_aliases(wanted) & models:
        return plugin.Check("ollama", False, f"model {wanted} not pulled (have: {', '.join(sorted(models)) or 'none'}); "
                                              f"run `ollama pull {wanted}`")
    models_dir, source = _resolve_models_dir(layout)
    expected = cfg.get("models_dir")
    if not models_dir:
        return plugin.Check("ollama", False, f"OLLAMA_MODELS is not set anywhere ({source}); expected {expected}")
    store = Path(models_dir)
    drive = cfg.get("models_store_drive")
    if drive:
        want_drive = drive.rstrip(":").upper() + ":"
        if store.drive.upper() != want_drive:
            return plugin.Check("ollama", False, f"OLLAMA_MODELS={models_dir} ({source}) is not on {want_drive} "
                                                  f"(expected {expected})")
    if not any(p.exists() for alias in _model_aliases(wanted) for p in _manifest_paths(store, alias)):
        return plugin.Check("ollama", False, f"model manifest for {wanted} not found under {store} (checked both manifest "
                                              f"layouts: manifests/registry.ollama.ai and manifests-v2/ollama.com); the "
                                              "server may be serving from a different store than it reports")
    where = f"store on {drive} at {store}" if drive else f"store at {store}"
    return plugin.Check("ollama", True, f"{cfg['base_url']} healthy, {wanted} present, {where} ({source})")


def setup(registry) -> None:
    """The Ollama lane (holds the GPU slot) and its doctor check."""
    registry.adapter(ADAPTER)
    registry.doctor_check(check_ollama)
