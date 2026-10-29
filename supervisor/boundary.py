"""Framework boundary check (gate G1 of docs/BOUNDARY.md): narrow, testable rules over the `supervisor/` package, so
"is this generic?" is answered by code and not by memory.

  G1a  no static import of a project or plugin module: a module may import the standard library, `supervisor`
       itself and the third-party packages the framework declares; dynamic loading is allowed only in the files
       listed as loaders (the plugin loader is the one sanctioned path to project code);
  G1b  no project-specific literals: drive-letter or home-directory paths, non-loopback hosts in URLs or IP
       literals, e-mail addresses and the user names the check is given;
  G1c  no model or vendor names outside the explicit list of files that legitimately carry them (the agent
       adapters, the capacity and usage code, the CLI doctor checks, the template config, the docs);
  G1d  no project information anywhere in the repository (every file git tracks or would add, not only `supervisor/`): the
       framework repo never contains a project's files or information. A project's words are listed in a local
       file outside the repository (`SUPERVISOR_PROJECT_TERMS`, else ~/.config/supervisor/project-terms.toml), never
       in `boundary.toml`; without that file, as in CI, G1d reports that it did not run. File names count (1.14.0).

`--words <target>` runs only G1d's word list over any other folder or built archive (a plugin package's
repository or folder and its wheel, 1.14.0); there a missing word list fails the scan instead of skipping it.

Every exception is an explicit entry in `boundary.toml` at the framework root, so a new one is a reviewed change.

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
PROJECT_TERMS_ENV = "SUPERVISOR_PROJECT_TERMS"
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
    rule: str            # G1a / G1b / G1c / G1d
    path: str            # framework-root-relative POSIX path
    line: int
    text: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.detail}: {self.text!r}"


@dataclass
class Report:
    violations: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)              # stale exceptions and similar housekeeping
    skipped: list[str] = field(default_factory=list)            # rules that could not run here, and why

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


def _dynamic_names(expr: ast.AST, aliases: dict[str, set[str]]) -> set[str]:
    """The loader functions `expr` may evaluate to — `importlib.import_module`, a name bound to one (`from importlib
    import import_module as load`, `load = importlib.import_module`), `getattr(importlib, "import_module")` or a
    `functools.partial` of one. A name rebound to different loaders may be any of them. Empty when none."""
    if isinstance(expr, ast.Attribute) and expr.attr in DYNAMIC:
        return {expr.attr}
    if isinstance(expr, ast.Name):
        return set(aliases.get(expr.id, ())) | ({"__import__"} if expr.id == "__import__" else set())
    if isinstance(expr, ast.Call):
        fn = expr.func
        fname = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
        if (fname == "getattr" and len(expr.args) >= 2 and isinstance(expr.args[1], ast.Constant)
                and expr.args[1].value in DYNAMIC):
            return {expr.args[1].value}
        if fname == "partial" and expr.args:
            return _dynamic_names(expr.args[0], aliases)
    return set()


def _dynamic_aliases(tree: ast.AST) -> dict[str, set[str]]:
    """Names the module binds to a loader function, through imports and (chains of) assignments. Flow-insensitive
    and only growing: a name keeps every loader any binding gives it, so a rebinding (`load = import_module`, later
    `load = run_path`) or a cycle (`a = b`, `b = a`) adds to the sets until nothing new appears — the loop ends
    because the sets are bounded by the loader names."""
    aliases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _DYNAMIC_SOURCES:
            for a in node.names:
                if a.name in DYNAMIC:
                    aliases.setdefault(a.asname or a.name, set()).add(a.name)
    bindings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            bindings += [(t.id, node.value) for t in node.targets if isinstance(t, ast.Name)]
        elif (isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None
              and isinstance(node.target, ast.Name)):
            bindings.append((node.target.id, node.value))
    changed = True
    while changed:
        changed = False
        for target, value in bindings:
            names = _dynamic_names(value, aliases)
            if names - aliases.get(target, set()):
                aliases.setdefault(target, set()).update(names)
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
        names = _dynamic_names(node, aliases)
        if not names:
            continue
        name = "/".join(sorted(names))
        parent = getattr(node, "_boundary_parent", None)
        if isinstance(parent, ast.Call) and parent.func is node:
            arg = parent.args[0] if parent.args else None
            if all(DYNAMIC[n] for n in names) and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
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
                                                             "declare (a project or plugin module?)"))
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


# ---------------------------------------------------------------- G1d project information

def project_terms_path(environ=None) -> Path | None:
    """The local file listing projects' words: `SUPERVISOR_PROJECT_TERMS`, else ~/.config/supervisor/project-terms.toml
    (None without a home directory, as in a bare CI environment). It lives outside the framework repository, so the
    list is never project information inside it."""
    env = (os.environ if environ is None else environ).get(PROJECT_TERMS_ENV)
    if env:
        return Path(env)
    try:
        return Path.home() / ".config" / "supervisor" / "project-terms.toml"
    except RuntimeError:                                    # no home directory: there is no local file
        return None


def load_project_terms(path: Path | None = None) -> dict[str, list[str]] | None:
    """{project: [words]} from the local file (`[projects.<name>] terms = [...]`), or None when there is no file."""
    path = Path(path) if path else project_terms_path()
    if path is None or not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path} (the local project-terms file) is not valid TOML: {exc}") from exc
    return {str(name): [str(t) for t in (entry or {}).get("terms") or []]
            for name, entry in sorted((data.get("projects") or {}).items())}


def _distribution(name: str) -> str:
    """A distribution name as packaging compares them: case and runs of -, _ and . do not matter."""
    return re.sub(r"[-_.]+", "-", name).lower()


def load_package_allowances(path: Path | None = None) -> dict[str, dict[str, list[str]]] | None:
    """{project: {distribution: [words]}} from the local file's `[projects.<name>.packages]` tables, or None when
    there is no file. A project word that is also the public name of a plugin package kept under packages/ (an
    identity the project's saved state already uses) may be allowed there:

        [projects.<name>.packages]
        supervisor-plugin-<name> = ["<word>"]

    It is allowed only inside `packages/<distribution>/` (or that folder scanned on its own) and in that
    distribution's wheel or sdist; everywhere else it is still a hit. Each word must be one of the project's own."""
    path = Path(path) if path else project_terms_path()
    if path is None or not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path} (the local project-terms file) is not valid TOML: {exc}") from exc
    out = {}
    for name, entry in sorted((data.get("projects") or {}).items()):
        table = (entry or {}).get("packages")
        if table is None:
            continue
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: [projects.{name}] packages must be a table of distribution = [words]")
        own = {str(w).lower() for w in (entry or {}).get("terms") or []}
        allowed = {}
        for dist, words in table.items():
            if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
                raise ConfigError(f"{path}: [projects.{name}.packages] {dist} must be a list of words")
            stray = [w for w in words if w.lower() not in own]
            if stray:
                raise ConfigError(f"{path}: [projects.{name}.packages] {dist} allows {stray}, which are not among "
                                  f"project {name!r}'s terms")
            allowed[_distribution(dist)] = list(words)
        out[str(name)] = allowed
    return out


def _allowed_here(target: Path, rel: str, allowed: dict[str, list[str]]) -> set[str]:
    """The words a project allows in this file: its package's own name, inside packages/<distribution>/ (or that
    folder scanned on its own) or in that distribution's archive."""
    if not allowed:
        return set()
    if not target.is_dir():
        m = re.match(r"(.+?)-\d", target.name)
        words = allowed.get(_distribution(m.group(1))) if m else None
        return {w.lower() for w in words or []}
    parts = [target.name, *rel.split("/")]
    dists = {_distribution(parts[0])} | {_distribution(parts[i + 1]) for i in range(len(parts) - 2)
                                         if parts[i] == "packages"}
    return {w.lower() for d in dists for w in allowed.get(d, [])}


def repository_files(root: Path) -> list[Path]:
    """Every file the repository tracks, and every untracked file it does not ignore, which the next commit would add
    (`git ls-files --cached --others --exclude-standard`, 1.13.0); without git, every file but caches, `runs/` and
    `.git/`. An ignored file, such as a contributor's scratch notes under `runs/`, is not scanned."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, check=False, timeout=60)
        if out.returncode == 0:
            return [Path(root) / rel for rel in sorted(set(out.stdout.decode("utf-8").split("\0"))) if rel]
    except (OSError, subprocess.SubprocessError):
        pass
    return _walk_files(root)


def _walk_files(root: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS | {".git", "runs", ".pytest_cache",
                                                                         ".ruff_cache"})
        found += [Path(dirpath) / n for n in sorted(filenames)]
    return found


def check_project_info(root: Path, cfg: dict, terms: dict[str, list[str]] | None,
                       allowed: dict[str, dict[str, list[str]]] | None = None) -> tuple[list[Finding], list[str]]:
    """G1d: (violations, why it did not run). A `[projects]` table in boundary.toml is itself a violation: it is a
    project's vocabulary inside the framework repo."""
    out = []
    if cfg.get("projects"):
        out.append(Finding("G1d", CONFIG_NAME, 1, "[projects]", f"project words listed in the framework repo: move "
                           f"them to the local file {project_terms_path()} ({PROJECT_TERMS_ENV})"))
    if terms is None:
        return out, [f"G1d (project information) did not run: no local project-terms file at {project_terms_path()}"]
    return out + scan_words(root, terms, where="the framework repo", allowed=allowed), []


def _members(target: Path):
    """(path, bytes or None) of each file in `target`: a folder (what git tracks or would add there, else every file
    but caches), a wheel or zip, or a tar archive (an sdist). Bytes are None when the file cannot be read."""
    import tarfile
    import zipfile
    if target.is_dir():
        # git lists nothing for a folder that an enclosing repository ignores (a package unpacked under runs/):
        # then every file is read, never none
        for p in repository_files(target) or _walk_files(target):
            if p.is_file():
                try:
                    yield _rel(target, p), p.read_bytes()
                except OSError:
                    yield _rel(target, p), None
    elif zipfile.is_zipfile(target):
        with zipfile.ZipFile(target) as z:
            for info in z.infolist():
                if not info.is_dir():
                    yield f"{target.name}!{info.filename}", z.read(info)
    elif tarfile.is_tarfile(target):
        with tarfile.open(target) as t:
            for info in t.getmembers():
                if info.isfile():
                    f = t.extractfile(info)
                    yield f"{target.name}!{info.name}", f.read() if f else None
    else:
        raise ConfigError(f"{target}: not a folder, a wheel, a zip or a tar archive")


def scan_words(target: Path, terms: dict[str, list[str]], where: str = "",
               allowed: dict[str, dict[str, list[str]]] | None = None) -> list[Finding]:
    """G1d's word list over any folder or built archive (1.14.0): each text file's lines, and every file's path (line
    0), since a module or data file named after a project is project information too. A binary file is checked by
    its name only. `where` names the place in each finding (default: the target). `allowed` is
    load_package_allowances(): a package's own name, allowed in that package only."""
    target = Path(target)
    if not target.exists():
        raise ConfigError(f"{target}: no such folder or archive")
    rules = [(project, rx) for project, words in terms.items() if (rx := _term_re(words)) is not None]
    out = []
    for rel, data in _members(target):
        try:
            lines = data.decode("utf-8").splitlines() if data is not None else []
        except UnicodeDecodeError:
            lines = []
        for project, rx in rules:
            ok = _allowed_here(target, rel.split("!", 1)[-1], (allowed or {}).get(project) or {})
            detail = f"a word of project {project!r} in {where or target.name}"
            out += [Finding("G1d", rel, 0, m.group(0), detail + " (a file name)") for m in rx.finditer(rel)
                    if m.group(0).lower() not in ok]
            for no, line in enumerate(lines, 1):
                out += [Finding("G1d", rel, no, m.group(0), detail) for m in rx.finditer(line)
                        if m.group(0).lower() not in ok]
    return out


def words(targets: list[Path], echo=print) -> int:
    """CLI body of `python -m supervisor.boundary --words <target>...` and `supervisor boundary words`: the project
    words scan over a plugin package's repository or folder and its built wheel (plugin-release). Exit 0
    clean, 1 hits, 2 when the scan cannot run: without the local word list a release check has not passed."""
    terms, allowed = load_project_terms(), load_package_allowances()
    if terms is None:
        echo(f"project words: cannot run, no local project-terms file at {project_terms_path()} "
             f"({PROJECT_TERMS_ENV}): a release check that did not run has not passed")
        return 2
    found = []
    for target in targets:
        try:
            hits = scan_words(Path(target), terms, allowed=allowed)
        except ConfigError as exc:
            echo(f"project words: {exc}")
            return 2
        for f in hits:
            echo(f"{target}: {f}")
        found += hits
    echo(f"project words: {'OK' if not found else f'{len(found)} hit(s)'} "
         f"({len(targets)} target(s), {sum(len(w) for w in terms.values())} word(s))")
    return 1 if found else 0


def default_user_names(cfg: dict) -> list[str]:
    """The names G1b looks for: `boundary.toml [literals] user_names`, plus this machine's login name when the check
    runs on a contributor's machine (the pre-commit hook) rather than in CI, whose login (`runner`) is a word."""
    lit = cfg.get("literals") or {}
    names = list(lit.get("user_names") or [])
    login = "" if os.environ.get("CI") else (os.environ.get("USERNAME") or os.environ.get("USER") or "")
    if login and login.lower() not in {n.lower() for n in lit.get("not_user_names") or []}:
        names.append(login)
    return names


def check(root: Path, cfg: dict | None = None, user_names: list[str] | None = None,
          terms: dict[str, list[str]] | None = ..., allowed: dict | None = None) -> Report:
    """Every rule over the framework at `root`. `terms` is G1d's word list and `allowed` its package allowances; by
    default both from the local project-terms file."""
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
    info, skipped = check_project_info(root, cfg, load_project_terms() if terms is ... else terms,
                                       load_package_allowances() if terms is ... else allowed)
    report.violations += info
    report.skipped += skipped
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
                         "notes": report.notes, "skipped": report.skipped}, indent=2))
        return 0 if report.ok else 1
    for f in report.violations:
        echo(str(f))
    for n in report.notes:
        echo(f"note: {n}")
    for s in report.skipped:
        echo(f"note: {s}")
    echo(f"boundary check: {'OK' if report.ok else f'{len(report.violations)} violation(s)'} ({root})")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m supervisor.boundary", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", help="framework checkout (default: the one this module runs from)")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--words", action="append", metavar="TARGET",
                    help="only the project words scan (G1d's local word list) over this folder or built archive, "
                         "for example a plugin package and its wheel; repeatable")
    args = ap.parse_args(argv)
    if args.words:
        return words([Path(t) for t in args.words])
    return run(Path(args.root) if args.root else None, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
