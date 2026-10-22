"""Restart-on-reload wrapper for the supervisor loop: `python -m supervisor.launcher` runs `supervisor run` in a
child and starts it again whenever it exits with EXIT_RELOAD (3: `loop.reload`, a framework update); any other
exit code ends the wrapper with that code. The same convention as `tools/run_supervisor.ps1`, in Python so the
Control Center's Start button (and the updater) can launch a loop that survives a reload on any platform."""
from __future__ import annotations

import subprocess
import sys
import time

from .paths import Layout
from .updates import RESTART_EXIT_CODE as EXIT_RELOAD     # == loop.EXIT_RELOAD; updates is the lighter import

RESTART_DELAY_S = 1.0


def run(args: list[str] | None = None, python: str | None = None, delay_s: float = RESTART_DELAY_S) -> int:
    """Run `supervisor run <args>` until it exits with a code other than EXIT_RELOAD; returns that code."""
    argv = [python or sys.executable, "-B", "-m", "supervisor", "run", *(args or [])]
    while True:
        code = subprocess.call(argv, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if code != EXIT_RELOAD:
            return code
        print("reload requested - restarting the supervisor on the current code", flush=True)
        time.sleep(delay_s)


def spawn(layout: Layout, args: list[str] | None = None) -> subprocess.Popen:
    """Start a detached wrapped loop for `layout` (output appended to state/runtime/supervisor_ui_launch.log)."""
    import os
    log_path = layout.runtime / "supervisor_ui_launch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    env = {**os.environ, "SUPERVISOR_ROOT": str(layout.root)}   # `supervisor run` otherwise targets the nearest project.yaml
    with open(log_path, "ab") as out:
        return subprocess.Popen([sys.executable, "-m", "supervisor.launcher", *(args or [])], cwd=str(layout.root),
                                stdout=out, stderr=out, stdin=subprocess.DEVNULL, creationflags=flags, env=env)


def main(argv: list[str] | None = None) -> None:
    sys.exit(run(list(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    main()
