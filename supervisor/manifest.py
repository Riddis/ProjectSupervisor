"""The extension manifest, `supervisor-extension.toml` (boundary plan phase 3; docs/EXTENSION_STANDARD.md).

A manifest says who an extension is (`identity`, the stable name saved state refers to), its own `version`, which
framework versions it supports (`framework`, a PEP 440 range), what it registers (`provides`, with `legacy_ids` for
identifiers older than the namespace rule), its configuration section and schema, what it needs (`requires`) and
where its migrations are. The framework reads it before anything the extension ships is imported: the schema and
migrations are module names here, imported later and only once the extension is approved.

Parsing is strict and complete: an unknown key is a problem (a misspelt key would otherwise be ignored silently), and
every problem is reported at once in `ManifestError.problems`."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from . import __version__

FILENAME = "supervisor-extension.toml"

# What `provides` may list. Every kind but `hooks` holds identifiers, which must start with the identity or be listed
# in `legacy_ids`; `hooks` names the registry hooks that register something without an identifier of its own.
IDENTIFIER_KINDS = ("adapters", "python_jobs", "commands", "inbox_kinds", "doctor_checks", "cli", "routes", "static",
                    "projections")
HOOKS = ("packet_screen", "overview_panel", "template_dir")
RESOURCES = ("gpu",)                                        # shared resource policy the framework owns (resources.*)

_TOP_LEVEL = ("identity", "version", "framework", "provides", "legacy_ids", "config", "requires", "migrations")
_IDENTITY = re.compile(r"^[a-z][a-z0-9_]*$")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SEPARATORS = "_.-:/"


class ManifestError(ValueError):
    """A manifest that cannot be used; `problems` lists every reason."""

    def __init__(self, source: str, problems: list[str]):
        self.source = source
        self.problems = problems
        super().__init__(f"{source}: " + "; ".join(problems))


@dataclass(frozen=True)
class Config:
    section: str                                            # extensions.<identity>
    schema: str                                             # module:Class, imported only after approval


@dataclass(frozen=True)
class Requires:
    resources: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()                        # identities this one calls into; they load first


@dataclass(frozen=True)
class Manifest:
    identity: str
    version: str
    framework: str
    source: str
    provides: dict[str, tuple[str, ...]] = field(default_factory=dict)
    legacy_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)
    config: Config | None = None
    requires: Requires = Requires()
    migrations: str | None = None

    def supports(self, version: str = __version__) -> bool:
        """Whether the framework `version` (default: the one installed) is in this extension's range, by number."""
        return SpecifierSet(self.framework).contains(Version(version), prereleases=True)

    def unsupported_reason(self, version: str = __version__) -> str | None:
        """None when supported; otherwise a sentence naming the extension, its range and the framework version."""
        if self.supports(version):
            return None
        return (f"extension {self.identity!r} {self.version} supports framework {self.framework}; "
                f"this is framework {version}")


def namespaced(identity: str, kind: str, name: str) -> bool:
    """Whether identifier `name` of `kind` belongs to `identity`: the identity itself, or the identity followed by a
    separator (`review_rounds.close`, `review_rounds_queue`). A route is the same with a leading slash."""
    if kind == "routes":
        if not name.startswith("/"):
            return False
        name = name[1:]
    return name == identity or (name.startswith(identity) and name[len(identity)] in _SEPARATORS)


def load(path: str | Path) -> Manifest:
    """Read and validate the manifest at `path`. ManifestError on an unreadable file, bad TOML or any rule broken."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(str(path), [f"cannot read the manifest: {exc}"]) from exc
    return parse(text, source=str(path))


def parse(text: str, source: str) -> Manifest:
    """Validate manifest `text`; `source` names it in errors (normally its path)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(source, [f"not valid TOML: {exc}"]) from exc
    problems: list[str] = []
    for key in data:
        if key not in _TOP_LEVEL:
            problems.append(f"unknown key {key!r} (allowed: {', '.join(_TOP_LEVEL)})")

    identity = _string(data, "identity", problems)
    if identity is not None and not _IDENTITY.match(identity):
        problems.append(f"identity {identity!r} must be lower case letters, digits and _, starting with a letter")
        identity = None
    version = _string(data, "version", problems)
    if version is not None:
        try:
            Version(version)
        except InvalidVersion:
            problems.append(f"version {version!r} is not a version number (PEP 440, e.g. 1.0.0)")
    framework = _string(data, "framework", problems)
    if framework is not None:
        try:
            SpecifierSet(framework)
        except InvalidSpecifier:
            problems.append(f"framework {framework!r} is not a version range (PEP 440, e.g. \">=1.12,<2\")")

    provides = _names_table(data, "provides", IDENTIFIER_KINDS + ("hooks",), problems)
    for hook in provides.get("hooks", ()):
        if hook not in HOOKS:
            problems.append(f"provides.hooks: unknown hook {hook!r} (allowed: {', '.join(HOOKS)})")
    for route in provides.get("routes", ()):
        if not route.startswith("/"):
            problems.append(f"provides.routes: {route!r} must start with /")
    legacy_ids = _names_table(data, "legacy_ids", IDENTIFIER_KINDS, problems)
    for kind, names in legacy_ids.items():
        for name in names:
            if name not in provides.get(kind, ()):
                problems.append(f"legacy_ids.{kind}: {name!r} is not in provides.{kind}")
    if identity is not None:
        for kind in IDENTIFIER_KINDS:
            for name in provides.get(kind, ()):
                if not namespaced(identity, kind, name) and name not in legacy_ids.get(kind, ()):
                    problems.append(f"provides.{kind}: {name!r} neither starts with the identity {identity!r} nor is "
                                    f"listed in legacy_ids.{kind}")

    config = _config(data, identity, problems)
    requires = _requires(data, identity, problems)
    migrations = data.get("migrations")
    if migrations is not None and not (isinstance(migrations, str) and _MODULE.match(migrations)):
        problems.append(f"migrations must be a module name (e.g. my_extension.migrations), not {migrations!r}")

    if problems:
        raise ManifestError(source, problems)
    return Manifest(identity=identity, version=version, framework=framework, source=source, provides=provides,
                    legacy_ids=legacy_ids, config=config, requires=requires, migrations=migrations)


def _string(data: dict, key: str, problems: list[str]) -> str | None:
    value = data.get(key)
    if value is None:
        problems.append(f"{key} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{key} must be a non-empty string")
        return None
    return value.strip()


def _names_table(data: dict, key: str, kinds: tuple[str, ...], problems: list[str]) -> dict[str, tuple[str, ...]]:
    """`key` as {kind: (name, ...)}: a table of lists of distinct non-empty strings, kinds from `kinds`."""
    table = data.get(key, {})
    if not isinstance(table, dict):
        problems.append(f"{key} must be a table")
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for kind, names in table.items():
        if kind not in kinds:
            problems.append(f"{key}: unknown kind {kind!r} (allowed: {', '.join(kinds)})")
            continue
        if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() for n in names):
            problems.append(f"{key}.{kind} must be a list of names")
            continue
        seen: set[str] = set()
        for name in names:
            if name in seen:
                problems.append(f"{key}.{kind}: {name!r} is listed twice")
            seen.add(name)
        out[kind] = tuple(names)
    return out


def _config(data: dict, identity: str | None, problems: list[str]) -> Config | None:
    table = data.get("config")
    if table is None:
        return None
    if not isinstance(table, dict) or set(table) - {"section", "schema"}:
        problems.append("config must be a table with exactly `section` and `schema`")
        return None
    section, schema = table.get("section"), table.get("schema")
    ok = True
    if identity is not None and section != f"extensions.{identity}":
        problems.append(f"config.section must be extensions.{identity} (an extension owns only its own section), "
                        f"not {section!r}")
        ok = False
    module, _, name = schema.partition(":") if isinstance(schema, str) else ("", "", "")
    if not (_MODULE.match(module) and name.isidentifier()):
        problems.append(f"config.schema must name a class as module:Class, not {schema!r}")
        ok = False
    return Config(section=section, schema=schema) if ok else None


def _requires(data: dict, identity: str | None, problems: list[str]) -> Requires:
    table = data.get("requires", {})
    if not isinstance(table, dict):
        problems.append("requires must be a table")
        return Requires()
    lists = _names_table({"requires": table}, "requires", ("resources", "services", "extensions"), problems)
    for resource in lists.get("resources", ()):
        if resource not in RESOURCES:
            problems.append(f"requires.resources: unknown resource {resource!r} (allowed: {', '.join(RESOURCES)})")
    for other in lists.get("extensions", ()):
        if other == identity:
            problems.append(f"requires.extensions: {identity!r} cannot require itself")
        elif not _IDENTITY.match(other):
            problems.append(f"requires.extensions: {other!r} is not an identity")
    return Requires(resources=lists.get("resources", ()), services=lists.get("services", ()),
                    extensions=lists.get("extensions", ()))
