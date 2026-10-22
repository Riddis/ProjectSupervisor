"""Workstation shutdown at the end of a drain. The supervisor asks the OS to power off with a grace
period, records that it did so on the runtime document (`shutdown_issued`), and exits; the Control Center can still
cancel within the grace period (`shutdown /a` on Windows, `shutdown -c` elsewhere) because the owner may walk back
in. The command runner is a module attribute so tests (and the Linux dev VM) never power anything off."""
from __future__ import annotations

import os
import subprocess
import sys
import time

from . import runtime, tasks as tasks_mod
from .paths import Layout

GRACE_S = 60
MESSAGE = "Supervisor: running work finished; shutting down as requested from the Control Center"


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()[-300:]
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)[:300]


run_command = _run           # monkeypatched in tests


def shutdown_argv(grace_s: int = GRACE_S) -> list[str]:
    if sys.platform.startswith("win") or os.name == "nt":
        return ["shutdown", "/s", "/t", str(grace_s), "/c", MESSAGE]
    minutes = max(1, -(-grace_s // 60))
    return ["shutdown", "-h", f"+{minutes}", MESSAGE]


def cancel_argv() -> list[str]:
    if sys.platform.startswith("win") or os.name == "nt":
        return ["shutdown", "/a"]
    return ["shutdown", "-c"]


def schedule(layout: Layout, actor: str = "loop", grace_s: int = GRACE_S) -> dict:
    """Ask the OS to power off after `grace_s`; durable record first so the Control Center can show and cancel it."""
    runtime.write(layout, shutdown_issued=True, shutdown_at=time.time() + grace_s, shutdown_by=actor,
                  shutdown_on_drain=False)
    code, output = run_command(shutdown_argv(grace_s))
    ok = code == 0
    if not ok:
        runtime.write(layout, shutdown_issued=False, shutdown_error=output or f"exit {code}")
    tasks_mod.log(layout, "system_shutdown", actor, ok=ok, grace_s=grace_s, output=output)
    return {"ok": ok, "grace_s": grace_s, "output": output}


def cancel(layout: Layout, actor: str = "owner") -> dict:
    """Abort a scheduled power-off (no-op when none is pending)."""
    doc = runtime.read(layout)
    if not doc.get("shutdown_issued"):
        return {"ok": True, "cancelled": False}
    code, output = run_command(cancel_argv())
    runtime.write(layout, shutdown_issued=False, shutdown_cancelled_by=actor)
    tasks_mod.log(layout, "system_shutdown_cancelled", actor, ok=code == 0, output=output)
    return {"ok": code == 0, "cancelled": True, "output": output}


def pending(layout: Layout) -> float | None:
    """Seconds until the scheduled power-off, or None when none is pending (or it has passed)."""
    doc = runtime.read(layout)
    if not doc.get("shutdown_issued"):
        return None
    remaining = float(doc.get("shutdown_at", 0)) - time.time()
    return remaining if remaining > 0 else None
