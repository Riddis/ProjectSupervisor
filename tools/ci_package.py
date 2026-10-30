"""CI for the plugin packages kept in this repository (packages/<distribution>/, docs/PLUGIN_STANDARD.md,
"Packages in the framework repository"), from 2.0.0.

    python tools/ci_package.py matrix [--github-output <file>]
    python tools/ci_package.py run packages/<distribution> --framework <vX.Y.Z | HEAD> [--workdir <folder>]

`matrix` lists every folder under packages/ that holds a pyproject.toml, twice: against the oldest framework release
(a `vX.Y.Z` tag, never a package's `<identity>-vX.Y.Z`) in its manifest's `framework` range, and against HEAD. It
prints the GitHub Actions matrix as JSON, or writes `matrix=` and `count=` to `--github-output`.

`run` tests one package as a project installs it, with no source on the path:
1. the framework's wheel is built from that ref (`git archive`, never the working tree), and the package's wheel
   from a copy of its folder, both in the work folder;
2. a fresh virtual environment there gets the framework wheel with its `dev` and `ui` extras, then the package's;
3. tools/ci_installed_plugin.py proves it installed (found, loaded, G5, a task on its lane or python job), with
   the options the package's pyproject.toml gives under `[tool.supervisor.ci]`: `lane`, `python_job`, and a
   `config` table written as its `plugins.<identity>` section;
4. the package's `tests/` folder, copied to an empty folder, runs there against the installed wheel, its temporary
   files in the work folder (never the system's temporary folder, which a sandboxed runner may refuse).

Exits non-zero, naming the step, on any failure. The project words scan (`supervisor boundary words`) needs the
owner's local word list, so it is a release check (plugin-release), not a CI step."""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = "packages"
RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
SKIP = shutil.ignore_patterns("build", "dist", "*.egg-info", "__pycache__", ".pytest_cache", "runs")


def packages(root: Path | None = None) -> list[Path]:
    folder = (root or ROOT) / PACKAGES
    return sorted(d for d in folder.iterdir() if (d / "pyproject.toml").is_file()) if folder.is_dir() else []


def manifest(package: Path) -> dict:
    """The package's supervisor-plugin.toml (inside its Python package, not under tests/ or build/)."""
    found = [p for p in package.rglob("supervisor-plugin.toml")
             if not {"tests", "build", "dist"} & set(p.relative_to(package).parts)]
    if len(found) != 1:
        raise SystemExit(f"{package}: expected one supervisor-plugin.toml, found {len(found)}")
    return tomllib.loads(found[0].read_text(encoding="utf-8"))


def ci_options(package: Path) -> dict:
    doc = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))
    return ((doc.get("tool") or {}).get("supervisor") or {}).get("ci") or {}


def releases(root: Path | None = None) -> list[str]:
    """The framework's release tags, oldest first."""
    out = subprocess.run(["git", "-C", str(root or ROOT), "tag", "--list"], capture_output=True, text=True, check=True).stdout
    tags = [t for t in out.split() if RELEASE_TAG.match(t)]
    return sorted(tags, key=lambda t: tuple(int(n) for n in RELEASE_TAG.match(t).groups()))


def oldest_in_range(spec: str, tags: list[str]) -> str | None:
    from packaging.specifiers import SpecifierSet
    rng = SpecifierSet(spec)
    return next((t for t in tags if rng.contains(t[1:], prereleases=False)), None)


def matrix(root: Path | None = None) -> dict:
    include, tags = [], releases(root)
    for package in packages(root):
        oldest = oldest_in_range(manifest(package)["framework"], tags)
        if oldest:
            include.append({"package": package.name, "framework": oldest})
        else:
            print(f"note: {package.name}: no framework release in {manifest(package)['framework']} yet; HEAD only",
                  file=sys.stderr)
        include.append({"package": package.name, "framework": "HEAD"})
    return {"include": include}


def _step(n: int, what: str, cmd: list[str], **kw) -> None:
    print(f"{n} {what}: {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, check=False, **kw)
    if proc.returncode != 0:
        raise SystemExit(f"FAIL at step {n} ({what}): exit {proc.returncode}")


def run(package: Path, framework: str, workdir: Path) -> int:
    package = package.resolve()
    if not (package / "pyproject.toml").is_file():
        raise SystemExit(f"{package}: not a package folder (no pyproject.toml)")
    identity = manifest(package)["identity"]
    work = workdir.resolve() / f"{package.name}-{framework}"
    if work.exists():
        shutil.rmtree(work)
    wheels = work / "wheels"
    wheels.mkdir(parents=True)

    fw_src = work / "framework-src"
    fw_src.mkdir()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", framework], capture_output=True,
                             check=False)
    if archive.returncode != 0:
        raise SystemExit(f"FAIL at step 1: git archive {framework}: {archive.stderr.decode(errors='replace')}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as t:
        t.extractall(fw_src, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))   # 3.11.4+
    _step(1, f"the framework's wheel at {framework}",
          [sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(wheels), str(fw_src)])
    pkg_src = work / "package-src"
    shutil.copytree(package, pkg_src, ignore=SKIP)
    _step(1, "the package's wheel", [sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(wheels),
                                     str(pkg_src)])
    shutil.rmtree(fw_src)
    shutil.rmtree(pkg_src)
    fw_wheel = next(wheels.glob("supervisor_framework-*.whl"))
    pkg_wheel = next(w for w in wheels.glob("*.whl") if w != fw_wheel)
    print(f"  wheels: {fw_wheel.name}, {pkg_wheel.name}")

    env_dir = work / "env"
    _step(2, "a fresh environment", [sys.executable, "-m", "venv", str(env_dir)])
    py = env_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    _step(2, "the framework, installed", [str(py), "-m", "pip", "install", "-q", f"{fw_wheel}[dev,ui]"])
    _step(2, "the package, installed", [str(py), "-m", "pip", "install", "-q", str(pkg_wheel)])

    opts = ci_options(package)
    out = work / "run"
    out.mkdir()
    tool = out / "ci_installed_plugin.py"
    shutil.copy2(ROOT / "tools" / "ci_installed_plugin.py", tool)
    cmd = [str(py), str(tool), identity, "--workdir", str(out)]
    if opts.get("lane"):
        cmd += ["--lane", str(opts["lane"])]
    if opts.get("python_job"):
        cmd += ["--python-job", str(opts["python_job"])]
    if opts.get("config"):
        cfg = out / "config.json"                       # JSON is YAML: the tool reads it with yaml.safe_load
        cfg.write_text(json.dumps(opts["config"]), encoding="utf-8")
        cmd += ["--config", str(cfg)]
    _step(3, "installed: found, loaded, G5, a task", cmd, cwd=str(out), env=_env())

    tests = work / "suite" / "tests"
    shutil.copytree(package / "tests", tests, ignore=SKIP)
    _step(4, "the package's suite on the installed wheel",
          [str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--basetemp={work / 'suite' / 'tmp'}", "tests"],
          cwd=str(tests.parent), env=_env())
    print(f"OK: {package.name} on framework {framework}")
    return 0


def _env() -> dict:
    import os
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "SUPERVISOR_ROOT")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python tools/ci_package.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    m = sub.add_parser("matrix")
    m.add_argument("--github-output", default=None)
    r = sub.add_parser("run")
    r.add_argument("package")
    r.add_argument("--framework", required=True, help="a framework release tag (vX.Y.Z) or HEAD")
    r.add_argument("--workdir", default="package-ci")
    args = ap.parse_args(argv)
    if args.command == "matrix":
        doc = matrix()
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as f:
                f.write(f"matrix={json.dumps(doc)}\ncount={len(doc['include'])}\n")
        print(json.dumps(doc, indent=2))
        return 0
    return run(Path(args.package), args.framework, Path(args.workdir))


if __name__ == "__main__":
    sys.exit(main())
