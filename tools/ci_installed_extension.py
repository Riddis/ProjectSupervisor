"""Gate G5's installed-package proof (CI job "G5 reference extension", phase 3 gate; each package's job, 1.14.0).

    python tools/ci_installed_extension.py <identity> [--lane <adapter>] [--python-job <name>] [--config <file>]
                                                      [--workdir <folder>]

Run in an environment where the extension `<identity>` is installed as a package (a built wheel, or `pip install -e`)
and the framework is installed, with the extension's source checkout out of the way. In a fresh temporary project
listing `<identity>`, it:
1. finds the extension through its entry point, as an installed package (never a project-local copy);
2. loads it with no error;
3. passes `supervisor extension check <identity>` (gate G5), run as a separate process;
4. with `--lane`, drives a task on that lane through the loop to COMPLETE (the fake-lane task of the phase 3 gate);
5. with `--python-job`, drives a task on that python job to COMPLETE (a package without a lane).

`--config <file>` is a YAML mapping written as the extension's section (`extensions.<identity>` in
config/routing.yaml), for a package whose config has a required field; without it the section is empty. The
temporary project is a new `g5-installed-*` folder under `--workdir` (default: the current folder), never the
system's temporary folder; it is removed when every step passes and kept, and named, when one fails.

Exits non-zero, naming the step, on any failure."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("identity")
    parser.add_argument("--lane", default=None, help="an adapter the extension registers, to complete a task on")
    parser.add_argument("--python-job", default=None, help="a python job the extension registers, to complete a task on")
    parser.add_argument("--config", default=None, help="a YAML file: the extension's config section")
    parser.add_argument("--workdir", default=None, help="where the temporary project goes (default: the current folder)")
    args = parser.parse_args()

    import yaml

    section = {}
    if args.config:
        section = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
        if not isinstance(section, dict):
            print(f"FAIL: {args.config} is not a YAML mapping")
            return 2
    workdir = Path(args.workdir).resolve() if args.workdir else Path.cwd()
    workdir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="g5-installed-", dir=workdir))
    print(f"project: {root}")
    code = run(root, args.identity, section, args.lane, args.python_job)
    if code == 0:
        shutil.rmtree(root, ignore_errors=True)
    else:
        print(f"kept for inspection: {root}")
    return code


def run(root: Path, identity: str, section: dict, lane: str | None, python_job: str | None) -> int:
    from supervisor import discovery, extensions, tasks, testing
    from supervisor.models.task import TaskStatus

    layout = testing.project(root, extensions=[identity],
                             routing={"extensions": {identity: section}, "resources": {"gpu": {}}})
    found = discovery.find(layout, identity)
    print(f"1 discovery: source={found.source} module={found.module} location={found.location}")
    if found.source != "installed" or found.manifest is None or found.problems:
        print(f"FAIL: not found as an installed package with a manifest: {found}")
        return 1
    reg = extensions.load(layout)
    print(f"2 load: loaded={reg.loaded} errors={reg.errors}")
    if reg.loaded != [identity] or reg.errors:
        return 1
    env = {**os.environ, "SUPERVISOR_ROOT": str(root), "PYTHONIOENCODING": "utf-8"}
    check = subprocess.run([sys.executable, "-m", "supervisor", "extension", "check", identity], cwd=str(root),
                           env=env, capture_output=True, text=True, check=False)
    print("3 supervisor extension check:\n" + check.stdout + check.stderr)
    if check.returncode != 0:
        return 1
    if lane:
        testing.make_task(layout, "G5", job={"adapter": lane, "output": "out/g5.txt"}, artifacts=["out/g5.txt"])
        testing.drive(layout, iterations=40, until=lambda: tasks.load(layout, "G5").status == TaskStatus.COMPLETE)
        status = tasks.load(layout, "G5").status
        print(f"4 task on {lane}: {status}")
        if status != TaskStatus.COMPLETE:
            return 1
    if python_job:
        if python_job not in reg.python_jobs:
            print(f"5 python job {python_job}: FAIL, the extension registers no such python job "
                  f"(it registers {sorted(reg.python_jobs)})")
            return 1
        testing.make_task(layout, "G5PY", job={"adapter": "python", "fn": python_job}, artifacts=[])
        testing.drive(layout, iterations=40, until=lambda: tasks.load(layout, "G5PY").status == TaskStatus.COMPLETE)
        status = tasks.load(layout, "G5PY").status
        print(f"5 python job {python_job}: {status.value}")
        if status != TaskStatus.COMPLETE:
            return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
