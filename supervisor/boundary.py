"""Framework boundary check (gate G1 of docs/BOUNDARY.md): narrow, testable rules over the `supervisor/` package, so
"is this generic?" is answered by code and not by memory.

  G1a  no static import of a project or extension module: a module may import the standard library, `supervisor`
       itself and the third-party packages the framework declares; dynamic loading is allowed only in the files
       listed as loaders (the extension loader is the one sanctioned path to project code);
  G1b  no project-specific literals: drive-letter or home-directory paths, non-loopback hosts in URLs or IP
       literals, e-mail addresses and the user names the check is given;
  G1c  no model or vendor names outside the explicit list of files that legitimately carry them (the agent
       adapters, the capacity and usage code, the CLI doctor checks, the template config, the docs).

Every exception is an explicit entry in `boundary.toml` at the framework root, so a new one is a reviewed change.
A scan for registered projects' domain words also runs: it is reported as supporting evidence, never as a failure,
because a word list cannot prove independence.

Standard library only (no import of the rest of the package), so the pre-commit hook and CI can run it from a bare
checkout: `python -m supervisor.boundary [--root <framework>]`; installed, it is `supervisor boundary check`."""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_NAME = "boundary.toml"
PACKAGE = "supervisor"
TEMPLATE = "supervisor/template/"
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".html", ".js", ".css", ".txt", ".json", ".bat", ".ps1",
                 ".sh", ".cfg", ".ini", ""}
SKIP_DIRS = {"__pycache__"}

DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:(?:\\\\|\\|/)[^\s'\"`<>|*?)\]]*")
HOME_PATH = re.compile(r"(?:/home/|/Users/|[\\/]Users[\\/]+)([A-Za-z0-9][A-Za-z0-9._-]*)")
URL_HOST = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^/\s@'\"`]*@)?(\[[0-9a-fA-F:]+\]|[^/\s:'\"`<>)\]}]+)")
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")
EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


@dataclass
class Finding:
    rule: str            # G1a / G1b / G1c, or "domain" for the supporting scan
    path: str            # framework-root-relative POSIX path
    line: int
    text: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.detail}: {self.text!r}"


@dataclass
class Report:
    violations: list[Finding] = field(default_factory=list)
    evidence: list[Finding] = field(default_factory=list)       # domain-word hits: reported, never failing
    notes: list[str] = field(default_factory=list)              # stale exceptions and similar housekeeping

    @property
    def ok(self) -> bool:
        return not self.violations


class ConfigError(ValueError):
    pass


def load_config(root: Path) -> dict:
    path = Path(root) / CONFIG_NAME
    if not path.is_file():
        raise ConfigError(f"{path} missing: the boundary check needs its explicit lists")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def package_files(root: Path) -> list[Path]:
    base = Path(root) / PACKAGE
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.suffix.lower() in TEXT_SUFFIXES or name.startswith("_dot_"):
                out.append(p)
    return out


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


# ---------------------------------------------------------------- G1a imports

def _stdlib() -> set[str]:
    return set(sys.stdlib_module_names) | {"__future__"}


# Functions that load code by name or path; True when their first argument is a module name a literal can show.
DYNAMIC = {"import_module": True, "__import__": True, "run_module": True,
           "spec_from_file_location": False, "run_path": False}
_DYNAMIC_SOURCES = {"importlib", "importlib.util", "builtins", "runpy"}


def _dynamic_name(expr: ast.AST, aliases: dict[str, str]) -> str | None:
    """The loader function `expr` evaluates to — `importlib.import_module`, a name bound to one (`from importlib
    import import_module as load`, `load = importlib.import_module`), `getattr(importlib, "import_module")` or a
    `functools.partial` of one — else None."""
    if isinstance(expr, ast.Attribute) and expr.attr in DYNAMIC:
        return expr.attr
    if isinstance(expr, ast.Name):
        return aliases.get(expr.id) or ("__import__" if expr.id == "__import__" else None)
    if isinstance(expr, ast.Call):
        fn = expr.func
        fname = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
        if (fname == "getattr" and len(expr.args) >= 2 and isinstance(expr.args[1], ast.Constant)
                and expr.args[1].value in DYNAMIC):
            return expr.args[1].value
        if fname == "partial" and expr.args:
            return _dynamic_name(expr.args[0], aliases)
    return None


def _dynamic_aliases(tree: ast.AST) -> dict[str, str]:
    """Names the module binds to a loader function, through imports and (chains of) assignments."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _DYNAMIC_SOURCES:
            for a in node.names:
                if a.name in DYNAMIC:
                    aliases[a.asname or a.name] = a.name
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            name = _dynamic_name(value, aliases)
            for t in targets:
                if name and isinstance(t, ast.Name) and aliases.get(t.id) != name:
                    aliases[t.id] = name
                    changed = True
    return aliases


def _import_targets(tree: ast.AST, loaders_ok: bool):
    """(line, module, how) for every absolute import and every dynamic import in the module. A loader function is
    judged wherever it is referenced, however it is named: called with a literal module name, that name is checked
    like a static import; any other use (a computed name, a path, passing the function on, binding it to another
    name) is allowed only in the listed loader files."""
    aliases = _dynamic_aliases(tree)
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._boundary_parent = parent                       # local annotation for this walk only
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, "import"
            continue
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                yield node.lineno, node.module, "import"
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            continue
        name = _dynamic_name(node, aliases)
        if not name:
            continue
        parent = getattr(node, "_boundary_parent", None)
        if isinstance(parent, ast.Call) and parent.func is node:
            arg = parent.args[0] if parent.args else None
            if DYNAMIC[name] and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found = (parent.lineno, arg.value, "dynamic import")
            elif loaders_ok:
                continue
            else:
                found = (parent.lineno, "", f"dynamic {name}() outside the listed loaders")
        elif loaders_ok:
            continue
        else:
            found = (node.lineno, "", f"reference to {name} outside the listed loaders")
        if found not in seen:
            seen.add(found)
            yield found


def check_imports(root: Path, files: list[Path], cfg: dict) -> list[Finding]:
    section = cfg.get("imports") or {}
    allowed = _stdlib() | {PACKAGE} | set(section.get("allowed") or [])
    loaders = set(section.get("dynamic_loaders") or [])
    out = []
    for p in files:
        rel = _rel(root, p)
        if p.suffix != ".py" or rel.startswith(TEMPLATE):      # the template is a project's code, born from here
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError as exc:
            out.append(Finding("G1a", rel, exc.lineno or 0, "", f"cannot be parsed ({exc.msg})"))
            continue
        for line, module, how in _import_targets(tree, rel in loaders):
            if not module:
                out.append(Finding("G1a", rel, line, "", how))
            elif module.split(".")[0] not in allowed:
                out.append(Finding("G1a", rel, line, module, f"{how} of a module the framework does not own or "
                                                             "declare (a project or extension module?)"))
    return out


# ---------------------------------------------------------------- G1b literals

def _allowed_literal(cfg: dict, rel: str, text: str) -> bool:
    for entry in (cfg.get("literals") or {}).get("allowed") or []:
        if entry.get("file") in (rel, "*") and entry.get("text") == text:
            return True
    return False


# RFC 2606 / RFC 6761 names no real host can have. Not `.local`: that is multicast DNS, where a machine on someone's
# network answers to it (`render-box.local`), so it is exactly the kind of project-specific host G1b refuses.
RESERVED_SUFFIXES = (".test", ".example", ".invalid", ".localhost")
RESERVED_HOSTS = {"localhost", "::1", "0.0.0.0", "example.com", "example.net", "example.org"}


def _host_ok(host: str, cfg: dict) -> bool:
    """Loopback, bind-all, the reserved example and test names, a templated host (`{host}`), or one listed in
    boundary.toml `[literals] allowed_url_hosts` (public services every project reaches the same way)."""
    lit = cfg.get("literals") or {}
    host = host.strip("[]").lower()
    if "{" in host or "$" in host or "%" in host:
        return True
    if host in RESERVED_HOSTS or host.startswith("127.") or host.endswith(RESERVED_SUFFIXES):
        return True
    if any(host == h or host.endswith("." + h) for h in ("example.com", "example.net", "example.org")):
        return True
    return host in {h.lower() for h in lit.get("allowed_url_hosts") or []}


def check_literals(root: Path, files: list[Path], cfg: dict, user_names: list[str]) -> list[Finding]:
    lit = cfg.get("literals") or {}
    mail_ok = {d.lower() for d in lit.get("allowed_mail_domains") or []}
    names = [n for n in user_names if n and len(n) >= 3]
    name_re = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9])",
                         re.IGNORECASE) if names else None
    out = []
    for p in files:
        rel = _rel(root, p)
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            hits: list[tuple[str, str]] = []
            hits += [(m.group(0), "drive-letter path") for m in DRIVE_PATH.finditer(line)]
            hits += [(m.group(0), "home-directory path") for m in HOME_PATH.finditer(line)]
            for m in URL_HOST.finditer(line):
                if not _host_ok(m.group(1), cfg):
                    hits.append((m.group(1), "non-loopback host"))
            for m in IPV4.finditer(line):
                if not _host_ok(m.group(1), cfg) and not m.group(1).startswith("0."):
                    hits.append((m.group(1), "non-loopback IP address"))
            for m in EMAIL.finditer(line):
                if line[m.end():m.end() + 1] == ":" and m.group(0).lower().startswith("git@"):
                    if not _host_ok(m.group(1), cfg):                # an ssh remote: judged by its host
                        hits.append((m.group(1), "non-loopback host"))
                elif m.group(1).lower() not in mail_ok and not _host_ok(m.group(1), cfg):
                    hits.append((m.group(0), "e-mail address"))
            if name_re:
                hits += [(m.group(0), "user name") for m in name_re.finditer(line)]
            for found, what in hits:
                if not _allowed_literal(cfg, rel, found):
                    out.append(Finding("G1b", rel, no, found, what))
    return out


# ---------------------------------------------------------------- G1c vendor names

def _term_re(terms: list[str]) -> re.Pattern | None:
    if not terms:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(t) for t in terms) + r")(?![A-Za-z])", re.IGNORECASE)


def check_vendors(root: Path, files: list[Path], cfg: dict) -> tuple[list[Finding], list[str]]:
    section = cfg.get("vendor") or {}
    rx = _term_re(list(section.get("terms") or []))
    listed = set(section.get("files") or [])
    out, carrying = [], set()
    if rx is None:
        return out, []
    for p in files:
        rel = _rel(root, p)
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            for m in rx.finditer(line):
                carrying.add(rel)
                if rel not in listed:
                    out.append(Finding("G1c", rel, no, m.group(0), "model or vendor name outside the listed files"))
    stale = [f"boundary.toml [vendor] files lists {f}, which no longer names a vendor or model: remove it"
             for f in sorted(listed - carrying)]
    return out, stale


# ---------------------------------------------------------------- supporting evidence

def scan_domain_words(root: Path, files: list[Path], cfg: dict) -> list[Finding]:
    out = []
    for project, entry in sorted((cfg.get("projects") or {}).items()):
        rx = _term_re(list((entry or {}).get("terms") or []))
        if rx is None:
            continue
        for p in files:
            rel = _rel(root, p)
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for no, line in enumerate(text.splitlines(), 1):
                for m in rx.finditer(line):
                    out.append(Finding("domain", rel, no, m.group(0), f"a word registered for project {project!r}"))
    return out


def default_user_names(cfg: dict) -> list[str]:
    """The names G1b looks for: `boundary.toml [literals] user_names`, plus this machine's login name when the check
    runs on a contributor's machine (the pre-commit hook) rather than in CI, whose login (`runner`) is a word."""
    lit = cfg.get("literals") or {}
    names = list(lit.get("user_names") or [])
    login = "" if os.environ.get("CI") else (os.environ.get("USERNAME") or os.environ.get("USER") or "")
    if login and login.lower() not in {n.lower() for n in lit.get("not_user_names") or []}:
        names.append(login)
    return names


def check(root: Path, cfg: dict | None = None, user_names: list[str] | None = None) -> Report:
    root = Path(root).resolve()
    cfg = load_config(root) if cfg is None else cfg
    files = package_files(root)
    if not any(p.suffix == ".py" for p in files):          # a wrong --root must not pass as "nothing to report"
        raise ConfigError(f"no {PACKAGE}/ package with Python files under {root}: nothing to check")
    names = default_user_names(cfg) if user_names is None else user_names
    report = Report()
    report.violations += check_imports(root, files, cfg)
    report.violations += check_literals(root, files, cfg, names)
    vendor, stale = check_vendors(root, files, cfg)
    report.violations += vendor
    report.notes += stale
    report.evidence += scan_domain_words(root, files, cfg)
    return report


def framework_root(start: Path | None = None) -> Path | None:
    """The framework checkout this module runs from (or the first folder above `start` holding boundary.toml)."""
    here = Path(start).resolve() if start else Path(__file__).resolve().parents[1]
    for d in (here, *here.parents):
        if (d / CONFIG_NAME).is_file() and (d / PACKAGE).is_dir():
            return d
    return None


def run(root: Path | None = None, as_json: bool = False, echo=print) -> int:
    """CLI body shared by `python -m supervisor.boundary` and `supervisor boundary check`. Exit 0 clean, 1 violations,
    2 when the check cannot run (no framework checkout or no valid boundary.toml)."""
    root = Path(root) if root else framework_root()
    if root is None:
        echo("boundary check: no framework checkout with boundary.toml found (pass --root)")
        return 2
    try:
        report = check(root)
    except ConfigError as exc:
        echo(f"boundary check: {exc}")
        return 2
    if as_json:
        echo(json.dumps({"ok": report.ok, "violations": [asdict(f) for f in report.violations],
                         "evidence": [asdict(f) for f in report.evidence], "notes": report.notes}, indent=2))
        return 0 if report.ok else 1
    for f in report.violations:
        echo(str(f))
    for n in report.notes:
        echo(f"note: {n}")
    if report.evidence:
        echo(f"supporting evidence (not a failure): {len(report.evidence)} registered domain word(s)")
        for f in report.evidence:
            echo(f"  {f}")
    echo(f"boundary check: {'OK' if report.ok else f'{len(report.violations)} violation(s)'} ({root})")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m supervisor.boundary", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", help="framework checkout (default: the one this module runs from)")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args(argv)
    return run(Path(args.root) if args.root else None, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
