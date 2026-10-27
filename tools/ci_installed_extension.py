"""Gate G5's installed-package proof (CI job "G5 reference extension", phase 3 gate).

    python tools/ci_installed_extension.py <identity> [--lane <adapter>]

Run in an environment where the extension `<identity>` is installed as a package (a built wheel, or `pip install -e`)
and the framework is installed, with the extension's source checkout out of the way. In a fresh temporary project
listing `<identity>`, it:
1. finds the extension through its entry point, as an installed package (never a project-local copy);
2. loads it with no error;
3. passes `supervisor extension check <identity>` (gate G5), run as a separate process;
4. with `--lane`, drives a task on that lane through the loop to COMPLETE (the fake-lane task of the phase 3 gate).

Exits non-zero, naming the step, on any failure."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("identity")
    parser.add_argument("--lane", default=None, help="an adapter the extension registers, to complete a task on")
    args = parser.parse_args()

    from supervisor import discovery, extensions, tasks, testing
    from supervisor.models.task import TaskStatus

    root = Path(tempfile.mkdtemp(prefix="g5-installed-"))
    layout = testing.project(root, extensions=[args.identity],
                             routing={"extensions": {args.identity: {}}, "resources": {"gpu": {}}})
    found = discovery.find(layout, args.identity)
    print(f"1 discovery: source={found.source} module={found.module} location={found.location}")
    if found.source != "installed" or found.manifest is None or found.problems:
        print(f"FAIL: not found as an installed package with a manifest: {found}")
        return 1
    reg = extensions.load(layout)
    print(f"2 load: loaded={reg.loaded} errors={reg.errors}")
    if reg.loaded != [args.identity] or reg.errors:
        return 1
    env = {**os.environ, "SUPERVISOR_ROOT": str(root), "PYTHONIOENCODING": "utf-8"}
    check = subprocess.run([sys.executable, "-m", "supervisor", "extension", "check", args.identity], cwd=str(root),
                           env=env, capture_output=True, text=True, check=False)
    print("3 supervisor extension check:\n" + check.stdout + check.stderr)
    if check.returncode != 0:
        return 1
    if args.lane:
        testing.make_task(layout, "G5", job={"adapter": args.lane, "output": "out/g5.txt"}, artifacts=["out/g5.txt"])
        testing.drive(layout, iterations=40, until=lambda: tasks.load(layout, "G5").status == TaskStatus.COMPLETE)
        status = tasks.load(layout, "G5").status
        print(f"4 task on {args.lane}: {status}")
        if status != TaskStatus.COMPLETE:
            return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
