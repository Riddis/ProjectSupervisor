"""The server watchdog (D-013 section 1.2): starts the configured `argv` as its own child, polls the configured
health URL until ready, holds the machine-wide GPU lease for as long as it runs, and stops - killing the child,
then releasing the lease - on idle (`idle_stop_s`), on a release request from another project, when the child
dies on its own, or when told to (the owner command `llm_endpoint.stop`, a later commit). Runtime-agnostic: it
knows nothing about any particular server binary, only a configured argv and a configured health URL.

Two halves, deliberately separate:

- `ensure_started()` runs in the *loop's* process (the adapter's `submit()`): a cheap health check, and - only
  when unreachable - spawning the watchdog in the background. It never waits out a cold load; a job finding the
  endpoint still unreachable is the adapter's own `CapacityDeferred`, costing no attempt.
- `run()` is the watchdog's own loop, meant for its own detached process (`python -m
  supervisor_plugin_llm_endpoint._server <root> <config file>`, written by `ensure_started()`): acquire, start,
  wait for health, then watch until something says stop.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import requests

from supervisor import plugin

SPAWN_GUARD_S = 30.0         # ensure_started() will not spawn a second watchdog within this long of the last spawn
POLL_S = 2.0


def state_path(root) -> Path:
    return Path(root) / "state" / "runtime" / "llm_endpoint_server.json"


def activity_path(root) -> Path:
    return Path(root) / "state" / "runtime" / "llm_endpoint_activity"


def _write_state(root, data: dict) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def touch_activity(root) -> None:
    path = activity_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def is_idle(root, idle_stop_s: float, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        age = now - activity_path(root).stat().st_mtime
    except OSError:
        return True                                   # no activity ever recorded: idle, not kept alive by its absence
    return age > idle_stop_s


def _health_ok(base_url: str, health_path: str, timeout: float = 5.0) -> bool:
    try:
        resp = requests.get(base_url.rstrip("/") + health_path, timeout=timeout)
        return resp.status_code < 500
    except requests.RequestException:
        return False


def _spawned_recently(root) -> bool:
    try:
        doc = json.loads(state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return time.time() - float(doc.get("spawned", 0)) < SPAWN_GUARD_S


def ensure_started(cfg: dict, root: str) -> bool:
    """True once the endpoint answers healthy. Never waits out a cold load: when it is not reachable yet, this
    starts the watchdog in the background (unless one was already started inside `SPAWN_GUARD_S`) and returns
    False immediately - the adapter's own `CapacityDeferred` is what makes that cost no attempt."""
    if _health_ok(cfg["base_url"], cfg.get("health_path", "/health")):
        return True
    if not cfg.get("argv"):
        raise RuntimeError(f"{cfg['base_url']} is unreachable and plugins.llm_endpoint.argv is not configured: "
                           "nothing to start")
    if _spawned_recently(root):
        return False
    _write_state(root, {"spawned": time.time(), "pid": None})
    config_path = state_path(root).with_suffix(".config.json")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([sys.executable, "-m", "supervisor_plugin_llm_endpoint._server", str(root), str(config_path)],
                     creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return False


def run(root: str, cfg: dict, *, poll_s: float = POLL_S) -> str:
    """The watchdog's own loop. Returns why it stopped: `start_timeout`, `child_exited`, `idle` or
    `release_requested`. Blocking; meant to be the whole of the watchdog process's work."""
    layout = plugin.Layout(root)
    lease = plugin.resource_lease(layout, "gpu")
    lease.acquire(wait_s=cfg.get("wait_s", 60))
    touch_activity(root)
    proc = subprocess.Popen(cfg["argv"])
    try:
        deadline = time.time() + cfg.get("start_timeout_s", 300)
        health_path = cfg.get("health_path", "/health")
        while not _health_ok(cfg["base_url"], health_path):
            if proc.poll() is not None:
                return "child_exited"
            if time.time() > deadline:
                return "start_timeout"
            time.sleep(poll_s)
        _write_state(root, {"pid": proc.pid, "base_url": cfg["base_url"], "started": time.time()})
        idle_stop_s = cfg.get("idle_stop_s", 900)
        while True:
            time.sleep(poll_s)
            lease.renew()
            if proc.poll() is not None:
                return "child_exited"
            if is_idle(root, idle_stop_s):
                return "idle"
            if lease.release_requested():
                return "release_requested"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        lease.release()
        _write_state(root, {"pid": None, "stopped": time.time()})


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root, config_path = argv[0], argv[1]
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    run(root, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
