"""Validates plugins-catalog/index.yaml (docs/releases/2.5.0/CONTRIBUTOR_RULING.md section 2): the framework's
plugin catalog, one list, both homes (a folder of this repository's own packages/<distribution>/, or anyone else's
https:// repository). Deterministic only: fetches nothing, proves no author's ownership, never imports or runs a
package's code. The maintainers' own G5 re-run on the exact artifact (the catalog-review skill) is the human half
that answers "is this safe to list"; this is the mechanical half that answers "is this well-formed".

    python tools/catalog_check.py [plugins-catalog/index.yaml] [--root <framework checkout>]

Exit 0 and "OK" when every entry is well-formed; exit 1 and one line per refusal, each naming the entry's identity
(or "index" for a file-level problem) and the field: "<identity>: <field>: <reason>". `tests/test_catalog.py` calls
`check()` directly over scaffolded fixtures and over the real index, the way `tests/test_ci_package.py` loads
`tools/ci_package.py` - one mechanism, two front doors, no new CI job and no new required context."""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "plugins-catalog" / "index.yaml"

_IDENTITY = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HOME_PACKAGE = re.compile(r"^packages/([A-Za-z0-9][A-Za-z0-9._-]*)$")
_HOME_URL = re.compile(r"^https://[^/\s]+/\S+$")
_FOREIGN_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
_SDIST_VERSION = re.compile(r"\d+(\.\d+)*((a|b|rc|\.post|\.dev)\d+)*")

FILE_KEYS = {"version", "plugins"}
ENTRY_KEYS = {"identity", "distribution", "summary", "home", "version", "framework", "licence", "tag", "artifact",
              "g5"}
STRING_FIELDS = ["identity", "distribution", "summary", "home", "version", "framework", "licence", "tag"]
ARTIFACT_KEYS = {"name", "sha256"}
G5_KEYS = {"date", "framework"}


def _ci_package():
    """tools/ci_package.py, loaded the same way its own test suite loads it: `releases()`, `oldest_in_range()` and
    `manifest()` are reused here rather than reimplemented (the ruling's own words)."""
    spec = importlib.util.spec_from_file_location("ci_package_for_catalog_check", Path(__file__).resolve().parent /
                                                   "ci_package.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _distribution(name: str) -> str:
    """A distribution name as packaging compares them: case and runs of -, _ and . do not matter."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _blank(value) -> bool:
    return not isinstance(value, str) or value.strip() == "" or value != value.strip()


def _problem(label: str, field: str, reason: str) -> str:
    return f"{label}: {field}: {reason}"


def _wheel_parts(name: str) -> tuple[str, str] | None:
    if not name.endswith(".whl"):
        return None
    parts = name[:-4].split("-")
    return (parts[0], parts[1]) if len(parts) in (5, 6) else None


def _sdist_parts(name: str) -> tuple[str, str] | None:
    if not name.endswith(".tar.gz") or "-" not in name:
        return None
    dist, version = name[:-7].rsplit("-", 1)
    return (dist, version) if _SDIST_VERSION.fullmatch(version) else None


def _date(value) -> datetime.date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _home_kind(home: str) -> tuple[str, str] | None:
    match = _HOME_PACKAGE.fullmatch(home)
    if match:
        return "packages", f"packages/{match.group(1)}"
    return ("url", home) if _HOME_URL.fullmatch(home) else None


def _git_tags(root: Path) -> set[str]:
    out = subprocess.run(["git", "-C", str(root), "tag", "--list"], capture_output=True, text=True, check=False)
    return set(out.stdout.split()) if out.returncode == 0 else set()


def _tag_date(root: Path, tag: str) -> datetime.date | None:
    out = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%ad", "--date=short", tag],
                         capture_output=True, text=True, check=False)
    return _date(out.stdout.strip()) if out.returncode == 0 else None


def _package_licence(folder: Path) -> str | None:
    """The package's own declared licence, read from its pyproject.toml (`[project] license`), a string or the
    classic `{text = "..."}` table. None when the package declares none, which is not itself a refusal: the
    catalog records a licence and never approves one (CONTRIBUTOR_RULING.md 2.2)."""
    doc = tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))
    licence = (doc.get("project") or {}).get("license")
    if isinstance(licence, str):
        return licence
    return licence.get("text") if isinstance(licence, dict) else None


def load(path: Path) -> dict:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"index: file: {exc}") from exc
    return doc if isinstance(doc, dict) else {}


def _check_entry(label: str, entry, root: Path, ci_package) -> list[str]:
    if not isinstance(entry, dict):
        return [_problem(label, "entry", "must be a mapping")]
    problems = []
    unknown = sorted(set(entry) - ENTRY_KEYS)
    if unknown:
        problems.append(_problem(label, "entry", f"unknown key(s): {unknown}"))
    for key in STRING_FIELDS:
        if key not in entry or _blank(entry.get(key)):
            problems.append(_problem(label, key, "missing, empty or not trimmed"))
    for key, sub in (("artifact", ARTIFACT_KEYS), ("g5", G5_KEYS)):
        value = entry.get(key)
        if not isinstance(value, dict):
            problems.append(_problem(label, key, "missing or not a mapping"))
            continue
        extra = sorted(set(value) - sub)
        if extra:
            problems.append(_problem(label, key, f"unknown key(s): {extra}"))
        for inner in sub:
            if inner not in value or _blank(value.get(inner)):
                problems.append(_problem(label, f"{key}.{inner}", "missing, empty or not trimmed"))
    if problems:
        # the basics are not in place: nothing past this point can be checked without guessing
        return problems

    identity = entry["identity"]
    if not _IDENTITY.fullmatch(identity):
        problems.append(_problem(label, "identity", "must match ^[a-z][a-z0-9_]*$"))

    try:
        Version(entry["version"])
    except InvalidVersion:
        problems.append(_problem(label, "version", "not a PEP 440 version"))

    tags = ci_package.releases(root)
    if ci_package.oldest_in_range(entry["framework"], tags) is None:
        problems.append(_problem(label, "framework", "no released framework version satisfies this range"))

    g5_framework = entry["g5"]["framework"]
    if g5_framework not in tags:
        problems.append(_problem(label, "g5.framework", "not a released framework tag of this repository"))

    g5_date = _date(entry["g5"]["date"])
    if g5_date is None:
        problems.append(_problem(label, "g5.date", "not an ISO date"))
    elif g5_date > datetime.date.today():
        problems.append(_problem(label, "g5.date", "lies in the future"))

    sha = entry["artifact"]["sha256"]
    if not _SHA256.fullmatch(sha):
        problems.append(_problem(label, "artifact.sha256", "not 64 lowercase hex characters"))

    name = entry["artifact"]["name"]
    parts = _wheel_parts(name) or _sdist_parts(name)
    if parts is None:
        problems.append(_problem(label, "artifact.name", "not a wheel or sdist file name"))
    else:
        dist, version = parts
        if _distribution(dist) != _distribution(entry["distribution"]):
            problems.append(_problem(label, "artifact.name", "distribution does not match the entry's distribution"))
        if version != entry["version"]:
            problems.append(_problem(label, "artifact.name", "version does not match the entry's version"))

    summary = entry["summary"]
    if len(summary) > 120 or "\n" in summary or re.search(r"https?://", summary):
        problems.append(_problem(label, "summary", "empty, longer than 120 characters, multi-line, or carries a URL"))

    kind = _home_kind(entry["home"])
    if kind is None:
        problems.append(_problem(label, "home",
                                 "neither packages/<folder> nor an https:// URL with a host and a path"))
    elif kind[0] == "packages":
        problems += _check_packages_home(label, entry, kind[1], root, g5_date, ci_package)
    else:
        if not _FOREIGN_TAG.fullmatch(entry["tag"] or ""):
            problems.append(_problem(label, "tag", "must be vX.Y.Z for a foreign home"))
        # the g5 date is attested, not checkable, for a foreign home (CONTRIBUTOR_RULING.md 2.5.8)

    return problems


def _check_packages_home(label: str, entry: dict, rel: str, root: Path, g5_date, ci_package) -> list[str]:
    problems = []
    identity, version = entry["identity"], entry["version"]
    expected_tag = f"{identity}-v{version}"
    if entry["tag"] != expected_tag:
        problems.append(_problem(label, "tag", f"must be {expected_tag!r} for a packages/ home"))

    folder = root / rel
    if not folder.is_dir() or not (folder / "pyproject.toml").is_file():
        problems.append(_problem(label, "home", f"{rel}: no such folder, or it holds no pyproject.toml"))
        return problems

    name = ((tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8")).get("project") or {})
            .get("name"))
    if _distribution(str(name or "")) != _distribution(entry["distribution"]):
        problems.append(_problem(label, "home", "pyproject.toml [project].name does not match distribution"))

    try:
        manifest = ci_package.manifest(folder)
    except SystemExit:
        problems.append(_problem(label, "home", "no supervisor-plugin.toml found under the package folder"))
    else:
        if manifest.get("identity") != identity:
            problems.append(_problem(label, "home", "the shipped manifest's identity does not match the entry's"))
        if manifest.get("version") != version:
            problems.append(_problem(label, "home", "the shipped manifest's version does not match the entry's"))
        if manifest.get("framework") != entry["framework"]:
            problems.append(_problem(label, "home", "the shipped manifest's framework range does not match the "
                                                     "entry's"))

    licence = _package_licence(folder)
    if licence is not None and licence != entry["licence"]:
        problems.append(_problem(label, "licence", "does not match the package's own declared licence"))

    if entry["tag"] not in _git_tags(root):
        problems.append(_problem(label, "tag", "does not exist in this repository"))
    elif g5_date is not None:
        tag_date = _tag_date(root, entry["tag"])
        if tag_date is not None and g5_date < tag_date:
            problems.append(_problem(label, "g5.date", "precedes its own tag's date"))

    return problems


def check(path: Path, root: Path | None = None) -> list[str]:
    """Every refusal CONTRIBUTOR_RULING.md 2.5 names, for the index at `path`, read against the framework checkout
    `root` (default: this repository)."""
    root = Path(root) if root else ROOT
    doc = load(Path(path))
    problems = []
    unknown = sorted(set(doc) - FILE_KEYS)
    if unknown:
        problems.append(_problem("index", "file", f"unknown key(s): {unknown}"))
    if doc.get("version") != 1:
        problems.append(_problem("index", "version", f"must be 1, found {doc.get('version')!r}"))
    plugins = doc.get("plugins")
    if not isinstance(plugins, list):
        problems.append(_problem("index", "plugins", "must be a list"))
        return problems

    ci_package = _ci_package()
    identities = []
    for i, entry in enumerate(plugins):
        identity = entry.get("identity") if isinstance(entry, dict) else None
        label = identity if isinstance(identity, str) and identity else f"plugins[{i}]"
        problems += _check_entry(label, entry, root, ci_package)
        if isinstance(identity, str):
            identities.append(identity)

    if identities != sorted(identities):
        problems.append(_problem("index", "plugins", "entries are not sorted by identity"))
    dupes = sorted({x for x in identities if identities.count(x) > 1})
    if dupes:
        problems.append(_problem("index", "identity", f"duplicate identity: {dupes}"))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python tools/catalog_check.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("index", nargs="?", default=str(INDEX))
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args(argv)
    problems = check(Path(args.index), Path(args.root))
    for problem in problems:
        print(problem)
    print("OK" if not problems else f"FAIL: {len(problems)} refusal(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
