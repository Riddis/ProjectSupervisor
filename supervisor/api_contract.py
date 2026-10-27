"""Gate G2: the public extension API's recorded contract (docs/BOUNDARY.md).

`supervisor.ext.__all__` is the contract. Every name in it is recorded in docs/extension_api.json with its kind and
signature: parameters (name, kind, default, annotation) and return annotation for a function; constructor, public
bases, public methods, properties and fields for a class; public functions and classes for a module; type and value
for data. docs/EXTENSION_API.md is the human half and names every one of them.

    python -m supervisor.api_contract record     # write docs/extension_api.json from the code
    python -m supervisor.api_contract check      # G2

`check` fails when the code and docs/extension_api.json differ (record it again, in the same commit), when a public
name has no section of its own in docs/EXTENSION_API.md (a `### `name`` heading with text), and, once a release tag
carries a contract, when a difference from that tag breaks the version rule. Both halves of the contract are compared
with the tag's: the recorded signatures, where a removal or a change that breaks a caller needs a major version
(after a minor release of deprecation warnings, which the reviewer checks) and an addition or a compatible change at
least a minor version; and each name's documented section, where a change (types, data shapes, semantics, error
cases) cannot be proved compatible by a program, so the CHANGELOG's newest section classifies it in an "Extension API"
entry naming the name: marked "(editorial)" (any version), "(breaking)" (a major version) or neither (a compatible
change, at least a minor version). Every change needs that entry. The previous contract is read from the last `v*`
tag reachable from HEAD with `git show`, both docs/extension_api.json and docs/EXTENSION_API.md; a shallow clone
cannot supply it and fails instead of passing unchecked.
"""
from __future__ import annotations

import enum
import inspect
import json
import re
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = 1
CONTRACT_FILE = "docs/extension_api.json"
DOC_FILE = "docs/EXTENSION_API.md"
CHANGELOG_FILE = "CHANGELOG.md"
INTERNAL = ("supervisor",)                 # classes defined here are the framework's: their members are contract

_KINDS = {
    inspect.Parameter.POSITIONAL_ONLY: "positional_only",
    inspect.Parameter.POSITIONAL_OR_KEYWORD: "positional_or_keyword",
    inspect.Parameter.VAR_POSITIONAL: "var_positional",
    inspect.Parameter.KEYWORD_ONLY: "keyword_only",
    inspect.Parameter.VAR_KEYWORD: "var_keyword",
}
_POSITIONAL = ("positional_only", "positional_or_keyword")
_ADDRESS = re.compile(r" at 0x[0-9A-Fa-f]+")
_PATH_FLAVOUR = re.compile(r"\b(?:Pure)?(?:Windows|Posix)Path\(")


class ContractError(RuntimeError):
    """The contract cannot be recorded or compared: a name in __all__ that does not exist, no git history, ..."""


# ---------------------------------------------------------------- describing the code

def _text(value) -> str:
    """repr() without what differs between platforms (WindowsPath, PosixPath) and processes (object addresses)."""
    return _PATH_FLAVOUR.sub("Path(", _ADDRESS.sub("", repr(value)))


def _annotation(ann) -> str | None:
    if ann is inspect.Parameter.empty:
        return None
    return ann if isinstance(ann, str) else _text(inspect.formatannotation(ann))


def _signature(obj) -> dict | None:
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):                   # a builtin without a signature
        return None
    params = []
    for p in sig.parameters.values():
        entry = {"name": p.name, "kind": _KINDS[p.kind]}
        ann = _annotation(p.annotation)
        if ann is not None:
            entry["annotation"] = ann
        if p.default is not inspect.Parameter.empty:
            entry["default"] = _text(p.default)
        params.append(entry)
    out: dict = {"params": params}
    ret = _annotation(sig.return_annotation)
    if ret is not None:
        out["returns"] = ret
    return out


def _simple(value) -> bool:
    if value is None or isinstance(value, (bool, int, float, str, enum.Enum)):
        return True
    return isinstance(value, (tuple, frozenset)) and all(_simple(v) for v in value)


def _data(value) -> dict:
    out = {"kind": "data", "type": type(value).__name__}
    if _simple(value):
        out["value"] = _text(value)
    return out


def _is_internal(obj, internal: tuple[str, ...]) -> bool:
    mod = getattr(obj, "__module__", None) or ""
    return any(mod == p or mod.startswith(p + ".") for p in internal)


def _member(value, internal, public) -> dict:
    if isinstance(value, staticmethod):
        return {"kind": "staticmethod", **(_signature(value.__func__) or {})}
    if isinstance(value, classmethod):
        return {"kind": "classmethod", **(_signature(value.__func__) or {})}
    if isinstance(value, property) or type(value).__name__ == "cached_property":
        return {"kind": "property"}
    if inspect.isfunction(value):
        return {"kind": "method", **(_signature(value) or {})}
    if inspect.isclass(value):
        return _class(value, internal, public)
    return _data(value)


def _class(cls, internal, public) -> dict:
    bases = []
    for base in cls.__bases__:
        if base is object:
            continue
        if id(base) in public:
            bases.append(public[id(base)])
        elif base.__module__ == "builtins":
            bases.append(base.__qualname__)
        elif not _is_internal(base, internal):        # an internal base that is not public is not part of the contract
            bases.append(f"{base.__module__}.{base.__qualname__}")
    members: dict[str, dict] = {}
    for klass in cls.__mro__:                         # the first definition along the MRO is the one callers get
        if not _is_internal(klass, internal):
            continue
        for name, ann in vars(klass).get("__annotations__", {}).items():
            if not name.startswith("_") and name not in members:
                members[name] = {"kind": "field", "annotation": _annotation(ann)}
        for name, value in vars(klass).items():
            if not name.startswith("_") and name not in members:
                members[name] = _member(value, internal, public)
    out: dict = {"kind": "class", "bases": bases}
    init = None if getattr(cls, "_is_protocol", False) else _signature(cls)   # a Protocol is never constructed
    if init is not None:
        init.pop("returns", None)                     # a constructor returns the instance, whatever __init__ says
        out["init"] = init
    out["members"] = dict(sorted(members.items()))
    return out


def _module(mod, internal, public) -> dict:
    names = getattr(mod, "__all__", None)
    if names is None:
        names = [n for n, v in vars(mod).items() if not n.startswith("_") and not isinstance(v, types.ModuleType)
                 and getattr(v, "__module__", None) == mod.__name__]
    return {"kind": "module", "members": {n: _entry(getattr(mod, n), internal, public) for n in sorted(names)}}


def _entry(obj, internal, public) -> dict:
    if isinstance(obj, types.ModuleType):
        return _module(obj, internal, public)
    if inspect.isclass(obj):
        return _class(obj, internal, public)
    if inspect.isroutine(obj):
        return {"kind": "function", **(_signature(obj) or {})}
    return _data(obj)


def describe(module, internal: tuple[str, ...] = INTERNAL) -> dict:
    """The contract of `module`: every name in its `__all__`, described as the module docstring says."""
    names = getattr(module, "__all__", None)
    if names is None:
        raise ContractError(f"{module.__name__} has no __all__: the contract is what it exports")
    if len(set(names)) != len(names):
        raise ContractError(f"{module.__name__}.__all__ names something twice")
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        raise ContractError(f"{module.__name__}.__all__ names {', '.join(missing)}, missing from the module")
    public = {id(getattr(module, n)): n for n in names}
    return {"format": FORMAT, "module": module.__name__,
            "names": {n: _entry(getattr(module, n), internal, public) for n in sorted(names)}}


# ---------------------------------------------------------------- comparing two contracts

@dataclass
class Diff:
    breaking: list[tuple[str, str]] = field(default_factory=list)   # (dotted name, what changed)
    changes: list[tuple[str, str]] = field(default_factory=list)    # additions and compatible changes


def _incompatible(old: list[dict], new: list[dict]) -> list[str]:
    """What a call that was valid against `old` parameters can no longer do against `new` ones."""
    out = []
    old_pos = [p for p in old if p["kind"] in _POSITIONAL]
    new_pos = [p for p in new if p["kind"] in _POSITIONAL]
    by_name = {p["name"]: p for p in new if p["kind"] in ("positional_or_keyword", "keyword_only")}
    var_pos = any(p["kind"] == "var_positional" for p in new)
    var_kw = any(p["kind"] == "var_keyword" for p in new)
    old_var_pos = any(p["kind"] == "var_positional" for p in old)
    old_var_kw = any(p["kind"] == "var_keyword" for p in old)
    old_keywords = {p["name"] for p in old if p["kind"] in ("positional_or_keyword", "keyword_only")}
    for i, p in enumerate(old_pos):
        q = new_pos[i] if i < len(new_pos) else None
        if q is None:
            if not var_pos or (p["kind"] == "positional_or_keyword" and not var_kw):
                out.append(f"parameter {p['name']} removed")
            continue
        if p["kind"] == "positional_or_keyword" and (q["kind"] != p["kind"] or q["name"] != p["name"]):
            out.append(f"parameter {p['name']} is no longer accepted at position {i + 1} and by name")
            continue
        if "default" in p and "default" not in q:
            out.append(f"parameter {p['name']} lost its default")
    for p in old:
        if p["kind"] != "keyword_only":
            continue
        q = by_name.get(p["name"])
        if q is None:
            if not var_kw:
                out.append(f"keyword parameter {p['name']} removed")
        elif "default" in p and "default" not in q:
            out.append(f"parameter {p['name']} lost its default")
    for kind, label in (("var_positional", "*args"), ("var_keyword", "**kwargs")):
        if any(p["kind"] == kind for p in old) and not any(p["kind"] == kind for p in new):
            out.append(f"{label} removed")
    # A name only counts as an old parameter when callers could bind it; `*args` / `**kwargs` collect, they impose
    # nothing (review 1.11.0 F3: `f(*args)` -> `f(args, *rest)` adds a requirement).
    old_names = {p["name"] for p in old if p["kind"] not in ("var_positional", "var_keyword")}
    for i, q in enumerate(new_pos):
        if i >= len(old_pos) and "default" not in q and q["name"] not in old_names:
            out.append(f"new required parameter {q['name']}")
        # A keyword the old **kwargs collected now names a parameter that can also be filled by position: a call
        # passing both (`f(1, a=2)` against `f(a, /, **kwargs)`) now fails with "multiple values".
        if (q["kind"] == "positional_or_keyword" and old_var_kw and q["name"] not in old_keywords
                and (i < len(old_pos) or old_var_pos)):
            out.append(f"parameter {q['name']} can now be given by name, so a keyword of that name that **kwargs "
                       f"collected collides with a positional argument")
    for q in new:
        if q["kind"] == "keyword_only" and "default" not in q and q["name"] not in old_names:
            out.append(f"new required keyword parameter {q['name']}")
    return out


def _params_text(params: list[dict]) -> str:
    return "(" + ", ".join(p["name"] + (f"={p['default']}" if "default" in p else "") for p in params) + ")"


def _compare_signature(o: dict, n: dict, path: str, diff: Diff) -> None:
    old_p, new_p = o.get("params", []), n.get("params", [])
    problems = _incompatible(old_p, new_p)
    diff.breaking += [(path, p) for p in problems]
    if not problems and old_p != new_p:
        diff.changes.append((path, f"parameters {_params_text(old_p)} -> {_params_text(new_p)}"))
    if o.get("returns") != n.get("returns"):
        diff.changes.append((path, f"returns {n.get('returns')} (was {o.get('returns')})"))


def _compare_names(old: dict, new: dict, prefix: str, diff: Diff) -> None:
    for name in sorted(old.keys() | new.keys()):
        path = prefix + name
        if name not in new:
            diff.breaking.append((path, "removed"))
        elif name not in old:
            diff.changes.append((path, "added"))
        else:
            _compare_entry(old[name], new[name], path, diff)


def _compare_entry(o: dict, n: dict, path: str, diff: Diff) -> None:
    kind = o.get("kind")
    if kind != n.get("kind"):
        diff.breaking.append((path, f"was a {kind}, now a {n.get('kind')}"))
        return
    if kind in ("function", "method", "staticmethod", "classmethod"):
        _compare_signature(o, n, path, diff)
    elif kind == "class":
        if "init" in o and "init" not in n:
            diff.breaking.append((path, "constructor signature no longer known"))
        elif "init" in o or "init" in n:
            _compare_signature(o.get("init") or {}, n["init"], path, diff)
        for base in o.get("bases", []):
            if base not in n.get("bases", []):
                diff.breaking.append((path, f"no longer a subclass of {base}"))
        for base in n.get("bases", []):
            if base not in o.get("bases", []):
                diff.changes.append((path, f"now also a subclass of {base}"))
        _compare_names(o.get("members", {}), n.get("members", {}), path + ".", diff)
    elif kind == "module":
        _compare_names(o.get("members", {}), n.get("members", {}), path + ".", diff)
    elif kind == "data":
        if o.get("type") != n.get("type"):
            diff.breaking.append((path, f"type {o.get('type')} -> {n.get('type')}"))
        elif o.get("value") != n.get("value"):
            diff.changes.append((path, f"value {o.get('value')} -> {n.get('value')}"))
    elif kind == "field" and o.get("annotation") != n.get("annotation"):
        diff.changes.append((path, f"annotation {o.get('annotation')} -> {n.get('annotation')}"))


def compare(old: dict, new: dict) -> Diff:
    """How `new` differs from `old`, split into what breaks a caller of `old` and what does not."""
    diff = Diff()
    _compare_names(old.get("names", {}), new.get("names", {}), "", diff)
    return diff


# ---------------------------------------------------------------- the check

@dataclass
class Result:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _version(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in text.lstrip("v").split("."))
    except ValueError:
        raise ContractError(f"not a release version: {text!r}") from None


def _mentions(text: str, name: str) -> bool:
    return re.search("`" + re.escape(name) + r"(?![A-Za-z0-9_])", text) is not None


def _newest_section(changelog: str) -> str:
    """The CHANGELOG's first `## ` section (`## Unreleased`, or the version being released)."""
    m = re.search(r"^## .*?(?=^## |\Z)", changelog, re.MULTILINE | re.DOTALL)
    return m.group(0) if m else ""


def _sections(doc: str) -> dict[str, str]:
    """Each documented name's section of docs/EXTENSION_API.md, whitespace collapsed: the text under a `### `name``
    heading, up to the next heading of level 1 to 3."""
    out: dict[str, str] = {}
    name, lines = None, []
    for line in doc.splitlines() + ["## end"]:
        heading = re.match(r"^#{1,3}\s", line)
        if heading:
            if name is not None:
                out[name] = " ".join(" ".join(lines).split())
            m = re.match(r"^###\s+`([^`]+)`\s*$", line)
            name, lines = (m.group(1) if m else None), []
        elif name is not None:
            lines.append(line)
    return out


def _entries(section: str) -> list[str]:
    """The CHANGELOG section's top-level bullets, each with its continuation and nested lines."""
    out: list[str] = []
    for line in section.splitlines():
        if line.startswith("- "):
            out.append(line)
        elif out and line.strip():
            out[-1] += "\n" + line
    return out


def _classification(section: str, name: str) -> str | None:
    """How the newest CHANGELOG section classifies a documented change to `name`: "editorial", "breaking",
    "compatible", or None when no "Extension API" entry names it."""
    found = [e for e in _entries(section) if "Extension API" in e and _mentions(e, name)]
    if not found:
        return None
    if any("(breaking)" in e for e in found):
        return "breaking"
    if all("(editorial)" in e for e in found):
        return "editorial"
    return "compatible"


def _listed(items: list[tuple[str, str]], limit: int = 6) -> str:
    shown = "; ".join(f"{n}: {w}" for n, w in items[:limit])
    return shown + (f"; and {len(items) - limit} more" if len(items) > limit else "")


def check(current: dict, recorded: dict | None, baseline: dict | None, *, version: str,
          baseline_version: str | None, changelog: str, api_doc: str | None, baseline_doc: str | None = None) -> Result:
    """G2 over already-loaded inputs: `current` from the code, `recorded` from docs/extension_api.json, `api_doc`
    docs/EXTENSION_API.md, `baseline` and `baseline_doc` the same two from the last release tag (None when it has
    none), `version` the framework version of this commit."""
    r = Result()
    if recorded is None:
        r.problems.append(f"{CONTRACT_FILE} is missing: run `python -m supervisor.api_contract record`")
    elif recorded != current:
        d = compare(recorded, current)
        what = _listed(d.breaking + d.changes) or "its format or module differs"
        r.problems.append(f"{CONTRACT_FILE} does not describe the code ({what}): run `python -m supervisor.api_contract "
                          f"record` and commit the file with the change")
    names = sorted(current.get("names", {}))
    sections = _sections(api_doc or "")
    if names and api_doc is None:
        r.problems.append(f"{DOC_FILE} is missing: it documents every public name")
    elif names:
        undocumented = [n for n in names if not sections.get(n)]
        if undocumented:
            r.problems.append(f"{DOC_FILE} has no section of its own (a `### `name`` heading with text) for "
                              f"{', '.join(undocumented)}")
    if baseline is None:
        since = f" ({baseline_version} has none)" if baseline_version else ""
        r.notes.append(f"no previous contract to compare with{since}: this one is the first")
        return r
    d = compare(baseline, current)
    cur, base = _version(version), _version(baseline_version or "0")
    section = _newest_section(changelog)
    documented = _documented_changes(r, baseline, current, baseline_doc, sections, section, baseline_version,
                                     version, cur, base)
    if d.breaking:
        if cur[0] <= base[0]:
            r.problems += [f"breaks callers of {baseline_version} ({n}: {w}): a major version is needed, this is {version}"
                           for n, w in d.breaking]
        else:
            r.notes.append(f"breaking changes in major version {version} ({_listed(d.breaking)}): the reviewer checks "
                           f"that each was deprecated with warnings for one minor release first")
    if not (d.breaking or d.changes):
        if not documented:
            r.notes.append(f"unchanged since {baseline_version}")
        return r
    if cur == base:
        r.notes.append(f"the extension API changed since {baseline_version}: the release commit raises at least the "
                       f"minor version")
    elif cur[:2] == base[:2]:
        r.problems.append(f"the extension API changed since {baseline_version}, so {version} cannot be a patch "
                          f"release: raise at least the minor version")
    if "Extension API" not in section:
        r.problems.append(f"{CHANGELOG_FILE}: the newest section has no \"Extension API\" line for the changes since "
                          f"{baseline_version} ({_listed(d.breaking + d.changes)})")
    else:
        unnamed = sorted({n for n, _w in d.breaking + d.changes if not _mentions(section, n)})
        if unnamed:
            r.problems.append(f"{CHANGELOG_FILE}: the newest section does not name {', '.join(unnamed)}, changed in "
                              f"the extension API since {baseline_version}")
    return r


def _documented_changes(r: Result, baseline: dict, current: dict, baseline_doc: str | None, sections: dict,
                        changelog_section: str, baseline_version: str | None, version: str, cur: tuple,
                        base: tuple) -> list[str]:
    """Compare each name's documented section with the tag's (review 1.11.0 F5) and hold each change to its
    CHANGELOG classification. Returns the names whose documented contract changed."""
    if baseline_doc is None:
        r.problems.append(f"{baseline_version} carries {CONTRACT_FILE} but no {DOC_FILE}: the documented half of its "
                          f"contract cannot be compared")
        return []
    before = _sections(baseline_doc)
    changed = [n for n in sorted(set(baseline.get("names", {})) & set(current.get("names", {})))
               if before.get(n) != sections.get(n)]
    for name in changed:
        kind = _classification(changelog_section, name)
        if kind is None:
            r.problems.append(f"the documented contract of `{name}` changed since {baseline_version} ({DOC_FILE}): "
                              f"name it in an \"Extension API\" entry of the {CHANGELOG_FILE}'s newest section, marked "
                              f"(editorial), (breaking) or neither for a compatible change")
            continue
        r.notes.append(f"documented contract of `{name}` changed since {baseline_version}, classified {kind}: the "
                       f"reviewer checks the classification")
        if kind == "breaking" and cur[0] <= base[0]:
            r.problems.append(f"the documented contract of `{name}` changed in a way the CHANGELOG calls breaking: a "
                              f"major version is needed, this is {version}")
        elif kind == "compatible" and cur != base and cur[:2] == base[:2]:
            r.problems.append(f"the documented contract of `{name}` changed since {baseline_version}, so {version} "
                              f"cannot be a patch release: raise at least the minor version, or mark the entry "
                              f"(editorial) if only the wording changed")
    return changed


# ---------------------------------------------------------------- git and the command line

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                          check=False)


def contract_at(root: Path, tag: str) -> dict | None:
    """The contract a tag carries, or None when the tag predates it."""
    if _git(root, "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}").returncode != 0:
        raise ContractError(f"no such tag or commit: {tag}")
    shown = _git(root, "show", f"{tag}:{CONTRACT_FILE}")
    if shown.returncode != 0:
        return None
    try:
        return json.loads(shown.stdout)
    except ValueError as exc:
        raise ContractError(f"{tag}:{CONTRACT_FILE} is not JSON ({exc})") from None


def document_at(root: Path, tag: str) -> str | None:
    """The docs/EXTENSION_API.md a tag carries, or None when it has none."""
    shown = _git(root, "show", f"{tag}:{DOC_FILE}")
    return shown.stdout if shown.returncode == 0 else None


def previous_contract(root: Path) -> tuple[str | None, dict | None]:
    """(last release tag reachable from HEAD, its contract or None). A shallow clone raises: without the history and
    the tags it would look like a first release and pass unchecked."""
    shallow = _git(root, "rev-parse", "--is-shallow-repository")
    if shallow.returncode != 0:
        raise ContractError(f"{root} is not a git checkout: the previous release's contract cannot be read "
                            f"(pass --against none to skip that comparison)")
    if shallow.stdout.strip() == "true":
        raise ContractError("this is a shallow clone: fetch the full history and tags to read the previous release's "
                            "contract (actions/checkout: fetch-depth: 0)")
    described = _git(root, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", "HEAD")
    if described.returncode != 0:
        return None, None                                 # no release yet
    tag = described.stdout.strip()
    return tag, contract_at(root, tag)


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def run(command: str, against: str = "auto", echo=print) -> int:
    """CLI body. Exit 0 OK, 1 the check failed, 2 it could not run."""
    # Imported here, statically (G1 allows dynamic loading only in the extension loader): `record` and `check` need
    # the framework importable, the rest of this module does not.
    import supervisor
    from supervisor import ext as module
    try:
        current = describe(module)
    except ContractError as exc:
        echo(f"extension API contract: {exc}")
        return 2
    root = Path(module.__file__).resolve().parents[2]
    target = root / CONTRACT_FILE
    if command == "record":
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        echo(f"extension API contract: recorded {len(current['names'])} name(s) in {CONTRACT_FILE}")
        return 0
    try:
        if against == "none":
            tag, baseline = None, None
        elif against == "auto":
            tag, baseline = previous_contract(root)
        else:
            tag, baseline = against, contract_at(root, against)
        text = _read(target)
        baseline_doc = document_at(root, tag) if tag and baseline is not None else None
        result = check(current, json.loads(text) if text is not None else None, baseline,
                       version=supervisor.__version__, baseline_version=tag.lstrip("v") if tag else None,
                       changelog=_read(root / CHANGELOG_FILE) or "", api_doc=_read(root / DOC_FILE),
                       baseline_doc=baseline_doc)
    except (ContractError, ValueError) as exc:
        echo(f"extension API contract: {exc}")
        return 2
    for p in result.problems:
        echo(p)
    for n in result.notes:
        echo(f"note: {n}")
    since = ("no comparison" if against == "none" else "no release tag" if tag is None
             else f"compared with {tag}" if baseline is not None else f"{tag} carries no contract")
    status = "OK" if result.ok else f"{len(result.problems)} problem(s)"
    echo(f"extension API contract: {status} ({len(current['names'])} name(s); {since})")
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m supervisor.api_contract", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["record", "check"])
    ap.add_argument("--against", default="auto", metavar="TAG",
                    help="the release to compare with: a tag, 'auto' (the last v* tag before HEAD, the default) or "
                         "'none' (skip the comparison, e.g. in a shallow clone)")
    args = ap.parse_args(argv)
    return run(args.command, against=args.against)


if __name__ == "__main__":
    sys.exit(main())
