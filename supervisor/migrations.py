"""State/config migrations between framework versions. Each migration is idempotent and records nothing but the new
`framework_version` in project.yaml. `supervisor doctor` reports a mismatch; `supervisor migrate` applies these.

After the framework's own, `supervisor migrate` runs the migrations each configured plugin's manifest names
(`migrations = "<module>"`, a module exposing MIGRATIONS with the same `(layout, dry_run) -> notes` contract). Only
that module is imported, never the plugin's setup(); a plugin whose framework range excludes this framework
is skipped (it does not load either)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any, Callable

import yaml

from . import __version__, project, store, yaml_edit
from . import jobs as jobs_mod
from . import tasks as tasks_mod
from .paths import Layout

Migration = Callable[[Layout, bool], list[str]]

# Owner responses recorded before 1.0.0 (the project-specific supervisor) → ApprovalResponse. MORE_LIKE and
# REGENERATE both asked for another attempt at the same deliverable, which is what REDO means now; APPROVE was the
# inbox spelling of ACCEPT (still accepted as an alias when answering, see inbox.APPROVAL_ALIASES).
LEGACY_RESPONSES = {"MORE_LIKE": "REDO", "REGENERATE": "REDO", "APPROVE": "ACCEPT"}
# An inbox item's allowed_responses are what the owner may answer, not a recorded verdict. APPROVE is the current
# spelling there: every approval item the framework opens offers it (inbox.APPROVAL_RESPONSES) and answering maps it
# to ACCEPT. Only the responses no version since 1.0.0 offers are legacy in that list.
LEGACY_ALLOWED_RESPONSES = {"MORE_LIKE": "REDO", "REGENERATE": "REDO"}


class MigrationError(RuntimeError):
    """The project cannot be migrated as it stands (no or malformed project.yaml); the message says what to do."""


def _routing_limits_rename(layout: Layout, dry_run: bool) -> list[str]:
    """limits.max_gpu_jobs → limits.max_resource_jobs (1.0.0)."""
    cfg = layout.config / "routing.yaml"
    if not cfg.exists():
        return []
    text = cfg.read_text(encoding="utf-8")
    new = text.replace("max_gpu_jobs", "max_resource_jobs")
    if new == text:
        return []
    if not dry_run:
        store.rewrite_text(cfg, new)
    return ["config/routing.yaml: limits.max_gpu_jobs renamed to max_resource_jobs"]


def _task_field_tokens(text: str, field: str, value: str) -> list[tuple[int, int, str | None]]:
    """(start, end, quote style) of the scalar token of every task-level `field: value` in a task file (its top-level
    mapping) or a seed (each item of its top-level `tasks` list), located through the YAML node tree. Anything else
    that merely looks like the field — a line inside a block-scalar note, a quoted example, a comment — is not a
    task's field and is not returned. The span is the scalar token alone, as written (quotes, escapes, folded lines),
    never the anchor or tag in front of it, and a value shared through an alias appears once. An unparsable file
    yields nothing (the doctor reports it)."""
    try:
        root = yaml.compose(text)
        tokens = {t.end_mark.index: t for t in yaml.scan(text) if isinstance(t, yaml.ScalarToken)}
    except yaml.YAMLError:
        return []
    if not isinstance(root, yaml.MappingNode):
        return []
    mappings = [root]
    for key, node in root.value:
        if isinstance(key, yaml.ScalarNode) and key.value == "tasks" and isinstance(node, yaml.SequenceNode):
            mappings += [item for item in node.value if isinstance(item, yaml.MappingNode)]
    found = set()
    for mapping in mappings:
        for key, node in mapping.value:
            if (isinstance(key, yaml.ScalarNode) and key.value == field
                    and isinstance(node, yaml.ScalarNode) and node.value == value):
                token = tokens.get(node.end_mark.index)          # a scalar node ends where its token ends
                if token is not None:
                    found.add((token.start_mark.index, token.end_mark.index, token.style))
    return sorted(found)


def _tasks_cost_category(layout: Layout, dry_run: bool) -> list[str]:
    """cost_category local_gpu → local (1.0.0). Only a task's own field is rewritten, in place: the value's token is
    replaced in its own quoting style, its anchor, tag and trailing comment kept, so a file that mentions local_gpu
    in a note, an example or a comment is neither changed nor reported, and a second run reports nothing."""
    notes = []
    for p in sorted(layout.tasks.glob("*.yaml")) + [layout.root / "state" / "tasks_seed.yaml"]:
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        new = text
        # Last token first, so each replacement leaves the positions of the ones still to come unchanged.
        for start, end, style in reversed(_task_field_tokens(text, "cost_category", "local_gpu")):
            raw = new[start:end]
            if style in ("'", '"'):
                replacement = f"{style}local{style}"
            else:                       # plain, or a block scalar (`|-`), whose token also holds the line breaks after
                replacement = "local" + raw[len(raw.rstrip("\n")):]     # it: written as plain `local`, breaks kept
            new = new[:start] + replacement + new[end:]
        if new == text:
            continue
        if not dry_run:
            store.rewrite_text(p, new)
        notes.append(f"{p.relative_to(layout.root).as_posix()}: cost_category local_gpu → local")
    return notes


def _approval_responses(layout: Layout, dry_run: bool) -> list[str]:
    """Task approvals with a legacy response → the current enum (1.1.1): MORE_LIKE/REGENERATE → REDO, APPROVE →
    ACCEPT. A task holding an old value fails validation and cannot load. An inbox item's allowed_responses map only
    MORE_LIKE/REGENERATE → REDO; APPROVE stays, since the framework writes it into every approval item (1.10.1)."""
    notes = []
    for p in sorted(layout.tasks.glob("*.yaml")) + sorted(layout.inbox.glob("*.yaml")):
        try:
            doc = store.read(p)
        except Exception:                                   # noqa: BLE001 - an unreadable file is the doctor's job
            continue
        changed = []
        delivery = doc.get("delivery") if isinstance(doc.get("delivery"), dict) else {}
        for a in list(delivery.get("approvals") or []) + list(doc.get("approvals") or []):
            old = str(a.get("response") or "") if isinstance(a, dict) else ""
            if old in LEGACY_RESPONSES:
                a["response"] = LEGACY_RESPONSES[old]
                changed.append(f"{old} → {LEGACY_RESPONSES[old]}")
        allowed = doc.get("allowed_responses")
        if isinstance(allowed, list) and any(str(x) in LEGACY_ALLOWED_RESPONSES for x in allowed):
            new = []
            for x in allowed:
                mapped = LEGACY_ALLOWED_RESPONSES.get(str(x), x)
                if mapped not in new:
                    new.append(mapped)
            doc["allowed_responses"] = new
            changed.append("allowed_responses")
        if not changed:
            continue
        if not dry_run:
            store.rewrite_text(p, store.dump(doc))
        notes.append(f"{p.relative_to(layout.root).as_posix()}: response {', '.join(changed)}")
    return notes


def _configured_manifests(layout: Layout) -> list:
    """The manifests of the configured plugins the loader's import-free approval accepts (plugins.approval).
    A manifest the loader would refuse directs no config move and has no migration imported (review 1.12.0 R1); an
    plugin that only fails in setup() is not refused here, since migrate never runs setup()."""
    from . import plugins
    plan = plugins.approval(layout)
    plugins.guard_refused(layout, plan)                 # an approved migration cannot import a refused one
    return plan.approved()


def _resources_gpu(layout: Layout, dry_run: bool) -> list[str]:
    """routing.yaml `gpu` -> `resources.gpu`, the shared resource policy the framework owns (1.12.0). Comments move
    with it. When both exist the file is left as it is (`resources.gpu` is what is read)."""
    cfg = layout.config / "routing.yaml"
    if not cfg.exists():
        return []
    text = cfg.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []                                           # doctor reports an unreadable routing.yaml
    shared = doc.get("resources") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or "gpu" not in doc or (shared is not None and not isinstance(shared, dict)) \
            or (isinstance(shared, dict) and "gpu" in shared):
        return []
    new = yaml_edit.move_sections(text, [("gpu", ("resources", "gpu"))])
    if not dry_run:
        store.rewrite_text(cfg, new)
    return ["config/routing.yaml: gpu moved to resources.gpu (shared resource policy)"]


def _plugin_config(layout: Layout, dry_run: bool) -> list[str]:
    """Each configured plugin's `config.legacy_sections` -> `plugins.<identity>` (1.12.0): the section named like
    the identity at the top, any other nested under its own name. Comments move with them. Driven by the manifests
    discovery reads; no plugin code runs. A plugin whose section already exists is left alone."""
    cfg = layout.config / "routing.yaml"
    if not cfg.exists():
        return []
    text = cfg.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    notes = []
    for m in _configured_manifests(layout):
        owned = doc.get("plugins")
        if m.config is None or (isinstance(owned, dict) and m.identity in owned) \
                or (owned is not None and not isinstance(owned, dict)):
            continue
        present = [s for s in m.config.legacy_sections if s in doc]
        if not present:
            continue
        moves = [(m.identity, ("plugins", m.identity))] if m.identity in present else []
        moves += [(s, ("plugins", m.identity, s)) for s in present if s != m.identity]
        text = yaml_edit.move_sections(text, moves)
        doc = yaml.safe_load(text)
        notes.append(f"config/routing.yaml: {', '.join(present)} moved to plugins.{m.identity} "
                     f"(plugin {m.identity})")
    if notes and not dry_run:
        store.rewrite_text(cfg, text)
    return notes


def _plugin_migrations(layout: Layout, dry_run: bool) -> list[str]:
    """The migrations each configured plugin's manifest names, after the framework's. Notes are prefixed with the
    plugin's identity; a failing one stops the run with MigrationError naming it."""
    from . import plugins
    notes = []
    for m in _configured_manifests(layout):
        if not m.migrations:
            continue
        try:
            with plugins.importing_for(layout):        # its imports follow this project's approval
                module = plugins.import_named_module(layout, m.migrations)
                for migration in getattr(module, "MIGRATIONS", []):
                    notes += [f"{m.identity}: {n}" for n in migration(layout, dry_run)]
        except store.CannotKeep:
            raise
        except Exception as exc:                            # named and stopped, never half-reported
            raise MigrationError(f"plugin {m.identity!r}: its migration ({m.migrations}) failed: {exc}") from exc
    return notes


_OLD_KEY = re.compile(r"^extensions(?=[ \t]*:)", re.MULTILINE)
# A manifest's old names in each form TOML allows: the key in [requires] (bare or quoted), the dotted key
# `requires.extensions`, the inline table `requires = { extensions = [...] }`, and the config section's value.
_OLD_REQUIRES = re.compile(r"""^([ \t]*)(["']?)extensions\2(?=[ \t]*=)""", re.MULTILINE)
_OLD_DOTTED = re.compile(r"""^([ \t]*requires[ \t]*\.[ \t]*)(["']?)extensions\2(?=[ \t]*=)""", re.MULTILINE)
# review 2.0.0 F1: the old single regex (`(["'])extensions\.`) renamed every quoted string starting `extensions.`
# wherever it occurred — a legacy `extensions.<name>` command identifier in [provides]/[legacy_ids], or syntax-
# looking text inside an unrelated string, as well as the one field it was meant for. `requires = {...}` and
# `config = {...}` inline tables are now rewritten key/value by key/value (`_rewrite_inline`, below), skipping over
# string content entirely, and the plain tables' key lines are matched only inside their own table's span
# (`_table_span`, `_in_table`): `extensions` is renamed only where `[requires]` declares it, `section` only where
# `[config]` does, never anywhere else in the file.
_INLINE_TABLE = re.compile(r"""^[ \t]*(?:requires|config)[ \t]*=[ \t]*\{.*\}.*$""", re.MULTILINE)
# A key may be bare or quoted (`section` or `"section"`/`'section'`); either spelling names the same TOML field.
_OLD_SECTION_LINE = re.compile(r"""^([ \t]*)(["']?)section\2([ \t]*=[ \t]*)(["'])extensions\.""", re.MULTILINE)
# `config.section = "extensions.<id>"`: the same field as `_OLD_SECTION_LINE`, written as a top-level dotted key
# rather than inside a `[config]` table (TOML allows both for the same field).
_OLD_DOTTED_SECTION = re.compile(
    r"""^([ \t]*config[ \t]*\.[ \t]*)(["']?)section\2([ \t]*=[ \t]*)(["'])extensions\.""", re.MULTILINE)
# A table's header name may be bare or quoted (`[requires]`, `["requires"]`, `['requires']`); same table either way.
_TABLE_HEADER = re.compile(
    r"""^[ \t]*\[[ \t]*(?:([A-Za-z_][A-Za-z0-9_-]*)|"((?:[^"\\]|\\.)*)"|'([^']*)')[ \t]*\][ \t]*(?:\#.*)?$""",
    re.MULTILINE)


def _header_name(header: re.Match) -> str:
    """The table name a `_TABLE_HEADER` match actually declares, quoted or not (TOML's basic-string escapes
    unescaped for the quoted form; the names migrate looks for, `requires` and `config`, never need them)."""
    if header.group(1) is not None:
        return header.group(1)
    if header.group(2) is not None:
        return header.group(2).replace("\\\\", "\\").replace('\\"', '"')
    return header.group(3)


def _table_span(text: str, name: str) -> tuple[int, int] | None:
    """The text span holding table `[name]`'s body: after its header line, up to the next `[other]` header or the
    end of the file. None when the manifest has no such header (an inline `name = { ... }` is `_rewrite_inline`'s,
    not this)."""
    headers = list(_TABLE_HEADER.finditer(text))
    for i, h in enumerate(headers):
        if _header_name(h) == name:
            return h.end(), headers[i + 1].start() if i + 1 < len(headers) else len(text)
    return None


def _in_table(text: str, name: str, rewrite: Callable[[str], str]) -> str:
    """`text` with `rewrite` applied to table `[name]`'s body and to nothing else, so a key of the same name in
    another table (or at the top level) is never touched. Unchanged when the manifest has no such header."""
    span = _table_span(text, name)
    if not span:
        return text
    start, end = span
    return text[:start] + rewrite(text[start:end]) + text[end:]


def _inline_pairs(body: str) -> list[tuple[int, int, int, int]]:
    """(key_start, key_end, value_start, value_end) for each top-level key=value pair in the text strictly between
    one inline table's braces, skipping over any string's content and any nested array/table, so text inside a
    value is never mistaken for another pair (review 2.0.0 F1's `"remote, extensions = [x]"`)."""
    pairs, i, n = [], 0, len(body)
    while i < n:
        while i < n and (body[i].isspace() or body[i] == ","):
            i += 1
        if i >= n:
            break
        key_start = i
        if body[i] in "\"'":
            q = body[i]
            i += 1
            while i < n and body[i] != q:
                i += 2 if q == '"' and body[i] == "\\" else 1
            i = min(i + 1, n)
        else:
            while i < n and (body[i].isalnum() or body[i] in "_-"):
                i += 1
        key_end = i
        while i < n and body[i].isspace():
            i += 1
        if i >= n or body[i] != "=":
            break                                             # not a key = value pair; stop rather than guess
        i += 1
        while i < n and body[i].isspace():
            i += 1
        value_start = i
        depth, quote = 0, None
        while i < n:
            c = body[i]
            if quote:
                i += 2 if quote == '"' and c == "\\" else 1
                quote = None if c == quote else quote
                continue
            if c in "\"'":
                quote = c
            elif c in "[{":
                depth += 1
            elif c in "]}":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                break
            i += 1
        pairs.append((key_start, key_end, value_start, i))
        if i < n and body[i] == ",":
            i += 1
    return pairs


def _requote(token: str, name: str) -> str:
    return f"{token[0]}{name}{token[0]}" if token[:1] in "\"'" else name


def _rewrite_inline(line: str) -> str:
    """One `requires = { ... }` or `config = { ... }` line, its `extensions` key renamed `plugins` and a `section`
    key's `extensions.`-prefixed string value renamed `plugins.`, structurally: every other key, value and byte of
    the line is untouched."""
    start, end = line.index("{"), line.rindex("}")
    body = line[start + 1:end]
    out, pos = [], 0
    for ks, ke, vs, ve in _inline_pairs(body):
        name = body[ks:ke].strip("\"'")
        out.append(body[pos:ks])
        out.append(_requote(body[ks:ke], "plugins") if name == "extensions" else body[ks:ke])
        out.append(body[ke:vs])
        raw_value = body[vs:ve]
        value = raw_value.rstrip()                       # the value token, apart from whitespace before `,`/`}`
        trailing = raw_value[len(value):]
        if (name == "section" and len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
                and value[1:-1].startswith("extensions.")):
            value = value[0] + "plugins." + value[1:-1][len("extensions."):] + value[-1]
        out.append(value)
        out.append(trailing)
        pos = ve
    out.append(body[pos:])
    return line[:start + 1] + "".join(out) + line[end:]


def _renamed_manifest(text: str) -> str:
    text = _in_table(text, "requires", lambda body: _OLD_REQUIRES.sub(r"\1\2plugins\2", body))
    text = _OLD_DOTTED.sub(r"\1\2plugins\2", text)       # `requires.extensions`, scoped by the key itself
    text = _OLD_DOTTED_SECTION.sub(r"\1\2section\2\3\4plugins.", text)   # `config.section = "extensions...`
    text = _INLINE_TABLE.sub(lambda m: _rewrite_inline(m.group(0)), text)
    return _in_table(text, "config", lambda body: _OLD_SECTION_LINE.sub(r"\1\2section\2\3\4plugins.", body))


def _unexpected_rewrite(before: str, after: str) -> bool:
    """Whether `after` (what `_renamed_manifest` made of `before`) changed anything beyond `requires.extensions`
    renamed `requires.plugins` (the key only; its value is untouched) and a `config.section` value's `extensions.`
    prefix renamed `plugins.`: a second, structural check that the targeted rewrite above stayed inside the two
    fields migrate names, by comparing the parsed documents rather than trusting the regex (review 2.0.0 F1).
    Unparsable text is `_still_old`'s and doctor's to report, not this check's."""
    try:
        before_doc, after_doc = tomllib.loads(before), tomllib.loads(after)
    except tomllib.TOMLDecodeError:
        return False
    expected = dict(before_doc)
    requires = expected.get("requires")
    if isinstance(requires, dict) and "extensions" in requires:
        requires = dict(requires)
        requires["plugins"] = requires.pop("extensions")
        expected["requires"] = requires
    config = expected.get("config")
    if isinstance(config, dict) and str(config.get("section") or "").startswith("extensions."):
        config = dict(config)
        config["section"] = "plugins." + config["section"][len("extensions."):]
        expected["config"] = config
    return expected != after_doc


def _rewrite_unparsable(before: str, after: str) -> bool:
    """Whether `after` is TOML a rewrite broke, although `before` parsed fine (review 2.0.0 F3: `[requires]
    extensions = []` next to `plugins = []` is valid TOML; renaming `extensions` to `plugins` then produces two
    `plugins` keys, which does not parse). `before` failing to parse is its own, pre-existing error — doctor's to
    report, never this check's or the rewrite's — so it is never read here as something the transformation broke."""
    try:
        tomllib.loads(before)
    except tomllib.TOMLDecodeError:
        return False
    try:
        tomllib.loads(after)
    except tomllib.TOMLDecodeError:
        return True
    return False


def _still_old(text: str) -> bool:
    """Whether a rewritten manifest still reads the old names (a form no rule above renames), so the plugin would
    not load after migrate recorded 2.0.0 (review 2.0.0). Called only once `_rewrite_unparsable` has cleared `text`,
    so a TOMLDecodeError reaching here is always the original manifest's own, pre-existing error (doctor's to
    report), never one the rewrite just created."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    requires, config = doc.get("requires"), doc.get("config")
    return ((isinstance(requires, dict) and "extensions" in requires)
            or (isinstance(config, dict) and str(config.get("section") or "").startswith("extensions.")))


_OLD_IMPORT = re.compile(r"^[ \t]*(?:from|import)[ \t]+(?:supervisor\.ext\b|extensions\b)|"
                         r"^[ \t]*from[ \t]+supervisor[ \t]+import[ \t]+[^\n]*\bext\b", re.MULTILINE)
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "runs", "state", "__pycache__"}


def _key_rename(layout: Layout, rel: str) -> tuple[Path, str] | None:
    """(file, its text with the top-level `extensions:` key renamed `plugins:`), or None when it has no such key.
    Raises MigrationError for a file holding both names, or the old one in a form a line edit cannot rename."""
    path = layout.root / rel
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None                                          # doctor reports an unreadable file
    if not isinstance(doc, dict) or "extensions" not in doc:
        return None
    if "plugins" in doc:
        raise MigrationError(f"{rel} has both extensions and plugins (2.0.0 renamed extensions to plugins): keep "
                             f"one by hand, then run `supervisor migrate` again")
    new, n = _OLD_KEY.subn("plugins", text, count=1)
    if n != 1 or (yaml.safe_load(new) or {}).get("plugins") != doc["extensions"]:
        raise MigrationError(f"{rel}: its extensions key is not a plain top-level line; rename it plugins by hand, "
                             f"then run `supervisor migrate` again")
    return path, new


def _old_code(layout: Layout, moved: Path) -> list[str]:
    """Project Python that imports supervisor.ext or the old folder: the project's to change, never migrate's."""
    import subprocess
    root = layout.root
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                             "--", "*.py"], capture_output=True, check=False)
    if listed.returncode == 0:
        files = [root / rel for rel in sorted(set(listed.stdout.decode("utf-8").split("\0"))) if rel]
    else:
        files = sorted(p for p in root.rglob("*.py") if not _SKIP_DIRS & set(p.relative_to(root).parts))
    out = []
    for p in files:
        rel = p.relative_to(root).as_posix()
        if rel.startswith("extensions/"):                    # read where the folder is now
            p = moved / rel[len("extensions/"):]
            rel = "plugins/" + rel[len("extensions/"):]
        if rel in out:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _OLD_IMPORT.search(text):
            out.append(rel)
    return out


def _dotted_old_entries(layout: Layout, folder: Path) -> list[str]:
    """project.yaml entries naming a module of the old folder by its dotted name (`extensions.<name>`, imported with
    the project folder on the path before 2.0.0). A dotted name the folder does not hold is an installed module."""
    doc = project.raw(layout)
    entries = doc.get("extensions") if "extensions" in doc else doc.get("plugins")
    out = []
    for entry in entries if isinstance(entries, list) else []:
        name = str((entry.get("name") or entry.get("module") or "") if isinstance(entry, dict) else entry or "")
        rest = name.strip()[len("extensions."):] if name.strip().startswith("extensions.") else ""
        rel = Path(*rest.split(".")) if rest else None
        if rel and folder.is_dir() and ((folder / rel).with_suffix(".py").is_file()
                                        or (folder / rel / "__init__.py").is_file()):
            out.append(name.strip())
    return out


def _out_of_range(folder: Path) -> list[str]:
    """Local manifests whose `framework` range leaves out this framework: the plugin does not load. A range is its
    author's promise, so it is named, never widened."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    out = []
    if not folder.is_dir():
        return out
    for p in sorted([*folder.glob("*.toml"), *folder.glob("*/supervisor-plugin.toml"),
                     *folder.glob("*/supervisor-extension.toml")]):
        try:
            rng = str(tomllib.loads(p.read_text(encoding="utf-8")).get("framework") or "")
            if rng and not SpecifierSet(rng).contains(__version__, prereleases=True):
                out.append(f"plugins/{p.relative_to(folder).as_posix()}: framework {rng} leaves out this framework "
                           f"({__version__}), so the plugin does not load until its range includes it")
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, InvalidSpecifier):
            continue                                         # doctor's plugin_manifests reports a broken manifest
    return out


def _plugins_rename(layout: Layout, dry_run: bool) -> list[str]:
    """2.0.0 renamed "extension" to "plugin" (a clean break): project.yaml and config/routing.yaml `extensions:` ->
    `plugins:`, the folder `extensions/` -> `plugins/`, and in each local manifest `[requires] extensions` ->
    `plugins`, the config section `extensions.<identity>` -> `plugins.<identity>` and `supervisor-extension.toml` ->
    `supervisor-plugin.toml`. Every refusal is decided before anything is written. Code that imports supervisor.ext
    or the old folder is named, not edited."""
    keys = [k for k in (_key_rename(layout, "project.yaml"), _key_rename(layout, "config/routing.yaml")) if k]
    old, new = layout.root / "extensions", layout.root / "plugins"
    move = old.is_dir()
    if move and new.exists() and (not new.is_dir() or any(new.iterdir())):
        raise MigrationError("the project has both extensions/ and plugins/ (2.0.0 renamed extensions/ to "
                             "plugins/): merge them into plugins/ by hand, then run `supervisor migrate` again")
    folder = old if move else new
    dotted = _dotted_old_entries(layout, folder)
    if dotted:                                               # decided before anything is written (review 2.0.0)
        raise MigrationError(f"project.yaml lists {', '.join(dotted)} by the old folder's dotted name, which no longer "
                             f"imports once extensions/ is plugins/: list each by its bare name (or plugins.<name>) "
                             f"by hand, then run `supervisor migrate` again")
    manifests = []
    if folder.is_dir():
        clash = sorted(p.parent.name for p in folder.glob("*/supervisor-extension.toml")
                       if (p.parent / "supervisor-plugin.toml").exists())
        if clash:                                            # decided before anything is written (review 2.0.0)
            raise MigrationError(f"{', '.join(clash)}: both supervisor-extension.toml and supervisor-plugin.toml "
                                 f"(2.0.0 renamed the first to the second): keep one by hand, then run `supervisor "
                                 f"migrate` again")
        for p in sorted([*folder.glob("*.toml"), *folder.glob("*/supervisor-extension.toml"),
                         *folder.glob("*/supervisor-plugin.toml")]):
            text = p.read_text(encoding="utf-8")
            changed = _renamed_manifest(text)
            if _rewrite_unparsable(text, changed):           # decided before anything is written (review 2.0.0 F3)
                raise MigrationError(f"{p.relative_to(layout.root).as_posix()}: renaming extensions to plugins "
                                     f"would make this parseable TOML unreadable (likely both `extensions` and "
                                     f"`plugins` already set in the same table) — this is the rename's error, not "
                                     f"the manifest's: keep one name by hand, then run `supervisor migrate` again")
            if _still_old(changed):                          # decided before anything is written
                raise MigrationError(f"{p.relative_to(layout.root).as_posix()}: its `[requires] extensions` or "
                                     f"`section = \"extensions.<identity>\"` is in a form migrate cannot rename: "
                                     f"rename it plugins by hand, then run `supervisor migrate` again")
            if _unexpected_rewrite(text, changed):           # decided before anything is written (review 2.0.0 F1)
                raise MigrationError(f"{p.relative_to(layout.root).as_posix()}: its rewrite would change something "
                                     f"beyond `requires` and `config.section`: rename it plugins by hand, then run "
                                     f"`supervisor migrate` again")
            if changed != text or p.name == "supervisor-extension.toml":
                manifests.append((p.relative_to(folder), text, changed))
    notes = [f"{path.relative_to(layout.root).as_posix()}: extensions renamed plugins" for path, _t in keys]
    if move:
        notes.append("extensions/ moved to plugins/")
    for rel, text, changed in manifests:
        what = []
        if changed != text:
            what.append("its extensions names renamed plugins")
        if rel.name == "supervisor-extension.toml":
            what.append("renamed supervisor-plugin.toml")
        notes.append(f"plugins/{rel.as_posix()}: {', '.join(what)}")
    if not dry_run:
        for path, text in keys:
            store.rewrite_text(path, text)
        if move:
            if new.is_dir():
                new.rmdir()                                  # an empty plugins/ (nothing in it to merge)
            old.rename(new)
        for rel, _text, changed in manifests:
            path = new / rel
            store.rewrite_text(path, changed)
            if rel.name == "supervisor-extension.toml":
                path.rename(path.with_name("supervisor-plugin.toml"))
    notes += _out_of_range(new if (move and not dry_run) else folder)
    code = _old_code(layout, new if (move and not dry_run) else old)
    if code:
        notes.append(f"{', '.join(code)}: import supervisor.ext or the extensions folder; change them to "
                     f"supervisor.plugin and plugins (migrate does not edit code)")
    return notes


MIGRATIONS: list[Migration] = [_plugins_rename, _routing_limits_rename, _tasks_cost_category, _approval_responses,
                               _resources_gpu, _plugin_config]


def run(layout: Layout, dry_run: bool = False) -> list[str]:
    """Apply every migration, then record the installed version in project.yaml. Raises MigrationError when there
    is no project.yaml to record it in, or the file is malformed — never reports success without a usable file —
    and when a file could not be rewritten with its owner, permissions and links kept (that file is left as it was;
    files already migrated stay migrated, and a rerun after the fix finishes the job)."""
    if not project.exists(layout):
        raise MigrationError(f"no project.yaml at {layout.root}; run `supervisor init {layout.root}` first")
    note = project.problem(layout)
    if note and "framework_version" not in note:
        raise MigrationError(f"cannot migrate: {note}; fix the file first")
    from . import state_commit
    config_paths = ["config", "project.yaml"]
    changed_before = set(state_commit.changed(layout, config_paths)) if not dry_run else set()
    notes: list[str] = []
    try:
        for m in MIGRATIONS:
            notes += m(layout, dry_run)
        notes += _plugin_migrations(layout, dry_run)
        raw = project.raw(layout)
        recorded = str(raw.get("framework_version") or "")
        if recorded != __version__:
            notes.append(f"project.yaml: framework_version {recorded or 'unset'} → {__version__}")
            if not dry_run:
                raw["framework_version"] = __version__
                project.write(layout, raw)
    except store.CannotKeep as exc:
        raise MigrationError(f"cannot migrate: {exc}") from exc
    if notes and not dry_run:
        # Commit what migrate rewrote (1.12.1), with the loop's records: a config file that already had uncommitted
        # changes is left for the owner, since its commit would carry their edit too.
        rewritten = [p for p in state_commit.changed(layout, config_paths) if p not in changed_before]
        out = state_commit.commit(layout, reason="migrate", extra=rewritten, title=f"migrate: framework {__version__}")
        if out["status"] == "committed":
            left = sorted(changed_before)
            notes.append(f"committed as {str(out['commit'])[:12]}" + (f"; left uncommitted (they had changes "
                                                                       f"before): {', '.join(left)}" if left else ""))
    return notes


# ---- the attempt-identity migration's dry run ----
#
# `migrate attempts --dry-run` derives a mapping of every job record's attempt identity (section 8.2) from the
# fields and evidence a store already carries -- never from content equality, never by parsing an id -- and writes
# it where a human reads it before anything is migrated. The one exception is dispatch order itself (`_dispatch_
# order`, below): a record with no identity fields yet carries no other evidence of when it was dispatched, so
# establishing that order is this migration's own job, not an identity field it invents. `apply` (section 8, step
# 7) is not built yet; this is the whole of what the dry run does: read the store, write `--out`, touch nothing
# else.

_DELIVERY_SESSION_CAUSES = ("continuation", "reroute")   # join the open attempt as its next session

_LEGACY_SUFFIX_RE = re.compile(r"\.a(\d+)$")   # the pre-allocator id shape: "<task>.a<n>", one flat counter


def _has_identity_fields(rec: dict) -> bool:
    """Migration-stamped or allocator-written -- the same test `loop.py`'s start-up guard (section 8.6) uses to
    decide a record needs no further migration: it already carries the fields `_map_task` would otherwise have
    to derive an order for."""
    if (rec.get("migrated") or {}).get("attempts"):
        return True
    return rec.get("kind") is not None and isinstance(rec.get("seq"), int)


def _dispatch_order(records: list[dict]) -> list[dict]:
    """The dispatch order `jobs.for_task` no longer gives an un-migrated record (section 8.2's own walk):
    `jobs.sort_key` dropped its id-suffix fallback at the writers switch, on the grounds
    that every record it ever sorts by then is migration-stamped or allocator-written -- which is exactly not
    true of the records this migration exists to place. A record with no identity fields yet is ordered by the
    one thing it does carry that still reflects when it was dispatched: the numeric suffix of its old-shape id,
    the flat counter `tasks.add_attempt` minted before the allocator existed. A record that already has fields
    (a previous migration run's work, or one dispatched by the allocator since) is left in exactly the order
    `jobs.sort_key` already gives it -- never re-ordered by suffix, which a freshly allocated id does not even
    carry in the same shape. The two groups are never interleaved: a task's fielded records are always the ones
    a past `apply_attempts` call saw, and a task's own flat counter only grows, so every record dispatched since
    sorts after all of them regardless of which rule placed it."""
    def key(rec):
        if _has_identity_fields(rec):
            attempt = rec.get("attempt")
            return (0, attempt if attempt is not None else -1,
                   jobs_mod.KIND_ORDER.get(jobs_mod.kind_of(rec), 99), rec.get("seq") or 0,
                   str(rec.get("attempt_id", "")))
        m = _LEGACY_SUFFIX_RE.search(str(rec.get("attempt_id", "")))
        suffix = int(m.group(1)) if m else None
        return (1, suffix if suffix is not None else float("inf"), str(rec.get("attempt_id", "")))
    return sorted(records, key=key)


def _task_notes(task) -> dict[str, str]:
    """The task document's own attempt note, by job id -- `Attempt.note` today holds it for every job kind
    (`tasks.add_attempt` is the loop's one call site, for delivery, verify, review and diagnosis jobs alike); the
    migration reads it only where section 8.2 names it (delivery's cause prefixes, verify's parent fallback).

    Read from each stored attempt's own dump, never through `Attempt.id` as a declared field (gone as of section
    4.6, step 15): only an un-migrated, per-job entry was ever written with an `id` key, and this walks every task
    in the store, so an allocator-written entry (section 2.7) simply contributes nothing."""
    dumps = (a.model_dump() for a in task.attempts)
    return {ad["id"]: (ad.get("note") or "") for ad in dumps if ad.get("id")}


_VERIFY_NOTE_RE = re.compile(r"verify delivery (\S+)")


def _nearest_owner_history(task, at: str) -> dict[str, Any] | None:
    """The task's own `history[]` entry, authored by the owner, at or before `at`, latest first -- quoted
    verbatim beside a `lane_changed_on_plain_dispatch` flag (section 8.2's "knowingly imprecise case")."""
    owned = [h for h in (task.history or []) if h.get("by") == "owner" and str(h.get("at") or "") <= at]
    return max(owned, key=lambda h: str(h.get("at") or "")) if owned else None


def _escalation_path(layout: Layout, task_id: str) -> Path:
    return layout.runtime / "escalation" / f"{task_id}.json"


def _read_escalation_readonly(layout: Layout, task_id: str) -> tuple[dict, str | None]:
    """`(state, digest)` for a task's escalation file, opened read-only: `state["events"]` (or `{}` when the task
    has none) and the sha256 of the raw bytes, or `None` digest when there is no file. Never written back --
    the dry run's read-only proof is comparing this digest before and after the whole run."""
    p = _escalation_path(layout, task_id)
    if not p.exists():
        return {}, None
    raw = p.read_bytes()
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        state = {}
    return state, hashlib.sha256(raw).hexdigest()


def _load_overrides(path: Path | str | None) -> tuple[dict[str, str], dict[str, Any] | None]:
    """`(job id -> cause, {"path", "sha256"})` from an overrides file (section 8.2: `overrides.yaml`, job id ->
    cause), or `({}, None)` when none is given. The file is named and hashed so a re-run with the same file is
    still byte-identical and the human's judgement is itself a reviewed, versioned artifact."""
    if not path:
        return {}, None
    p = Path(path)
    raw = p.read_bytes()
    try:
        doc = yaml.safe_load(raw.decode("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise MigrationError(f"{p}: not a valid overrides file: {exc}") from exc
    if not isinstance(doc, dict):
        raise MigrationError(f"{p}: an overrides file is a mapping of job id to cause")
    overrides = {str(k): str(v) for k, v in doc.items()}
    return overrides, {"path": p.name, "sha256": hashlib.sha256(raw).hexdigest()}


def _map_task(task, records: list[dict], overrides: dict[str, str],
             escalation_events: dict[str, Any]) -> tuple[dict[str, dict], list[dict], list[dict]]:
    """The per-job mapping for one task, walked in `records`' own order -- the caller's job, not this
    function's: `dry_run_attempts` passes them through `_dispatch_order` first, which is what establishes
    dispatch order for a record that carries none of the fields that would otherwise say what it is (section
    8.2). Returns `(entries, flags, unplaced)`; `entries` is job id -> `{task, attempt, kind, seq, parent,
    cause, scope, source}`. Nothing here ever reads two tasks together -- every piece of evidence section 8.2
    names (the attempt note, `verify_delivery`, `review_digest`, the escalation event) is this task's own."""
    notes = _task_notes(task)
    order_index = {r.get("attempt_id"): i for i, r in enumerate(records)}
    entries: dict[str, dict] = {}
    flags: list[dict] = []
    unplaced: list[dict] = []

    attempt_count = 0
    open_attempt: int | None = None
    sessions: dict[int, list[str]] = {}
    verify_seq: dict[int, int] = {}
    review_seq: dict[int, int] = {}
    diag_seq: dict[int, int] = {}
    attempt_opened_at: dict[int, str] = {}
    task_diag_seq = 0
    verify_by_digest: dict[str, list[tuple[int, str, str]]] = {}
    verify_count_by_parent: dict[str, int] = {}
    prev_delivery_adapter: str | None = None
    prev_delivery_job: str | None = None
    first_delivery_seen = False

    def place(job_id, kind, attempt, seq, parent, cause, source, scope=None):
        entries[job_id] = {"task": task.id, "attempt": attempt, "kind": kind, "seq": seq, "parent": parent,
                           "cause": cause, "scope": scope, "source": source}

    def unplace(rec, kind, reason):
        job_id = rec.get("attempt_id")
        unplaced.append({"job": job_id, "task": task.id, "kind": kind, "reason": reason})
        place(job_id, kind, None, None, None, None, "unplaced", scope="task")

    for rec in records:
        job_id = rec["attempt_id"]
        kind = jobs_mod.kind_of(rec)
        created = str(rec.get("created") or "")
        override_cause = overrides.get(job_id)

        if kind == "delivery":
            note = notes.get(job_id, "")
            deferred_reason = str(rec.get("deferred_reason") or "")
            is_continuation = (note.startswith("continuation") or deferred_reason.startswith("continuation")
                               or bool(rec.get("continuation_grant")))
            is_reroute = bool(rec.get("rerouted_from"))
            is_revision = note.startswith("revision after")
            if override_cause:
                cause = override_cause
            elif is_continuation:
                cause = "continuation"
            elif is_reroute:
                cause = "reroute"
            elif is_revision:
                cause = "revision"
            else:
                cause = "scheduled"

            if cause in _DELIVERY_SESSION_CAUSES and open_attempt is not None:
                attempt = open_attempt
                seq = len(sessions[attempt]) + 1
                parent = sessions[attempt][-1]
            else:
                attempt_count += 1
                attempt = attempt_count
                seq = 1
                parent = None
                open_attempt = attempt
                sessions[attempt] = []
                verify_seq[attempt] = 0
                review_seq[attempt] = 0
                diag_seq[attempt] = 0
                attempt_opened_at[attempt] = created
                if (cause == "scheduled" and first_delivery_seen and prev_delivery_adapter is not None
                        and rec.get("adapter") != prev_delivery_adapter):
                    flags.append({"job": job_id, "task": task.id, "reason": "lane_changed_on_plain_dispatch",
                                 "from_lane": prev_delivery_adapter, "to_lane": rec.get("adapter"),
                                 "history": _nearest_owner_history(task, created)})
            sessions[attempt].append(job_id)
            prev_delivery_adapter, prev_delivery_job, first_delivery_seen = rec.get("adapter"), job_id, True
            place(job_id, "delivery", attempt, seq, parent, cause, "note")

        elif kind == "verify":
            parent, source = rec.get("verify_delivery"), "verify_delivery"
            if not parent or parent not in entries or entries[parent]["kind"] != "delivery":
                m = _VERIFY_NOTE_RE.match(notes.get(job_id, ""))
                parent = m.group(1) if m and m.group(1) in entries else None
                source = "note"
            if not parent:
                unplace(rec, kind, "no verify_delivery parent resolves, and the note names none that does")
                continue
            attempt = entries[parent]["attempt"]
            if override_cause:
                cause = override_cause
            else:
                cause = "reverify" if verify_count_by_parent.get(parent, 0) else None
            verify_count_by_parent[parent] = verify_count_by_parent.get(parent, 0) + 1
            verify_seq[attempt] = verify_seq.get(attempt, 0) + 1
            digest = rec.get("verify_digest")
            if digest:
                verify_by_digest.setdefault(digest, []).append((order_index[job_id], parent, job_id))
            place(job_id, "verify", attempt, verify_seq[attempt], parent, cause, source)

        elif kind == "review":
            digest = rec.get("review_digest")
            candidates = verify_by_digest.get(digest, []) if digest else []
            distinct = sorted({p for _, p, _ in candidates})
            if len(distinct) == 1:
                parent, source = distinct[0], "review_digest"
            elif len(distinct) > 1:
                before = [p for p in distinct if order_index.get(p, -1) < order_index[job_id]]
                pool = before or distinct
                parent = max(pool, key=lambda p: order_index.get(p, -1))
                source = "review_digest"
            else:
                parent, source = prev_delivery_job, "preceding_delivery"
            if not parent:
                unplace(rec, kind, "no verify record shares review_digest, and no delivery precedes it")
                continue
            attempt = entries[parent]["attempt"]
            review_seq[attempt] = review_seq.get(attempt, 0) + 1
            cause = override_cause or ("rereview" if review_seq[attempt] > 1 else None)
            place(job_id, "review", attempt, review_seq[attempt], parent, cause, source)

        elif kind == "diagnosis":
            sig = rec.get("diag_signature")
            matches = [(k, ev) for k, ev in escalation_events.items()
                      if ev.get("signature") == sig and str(ev.get("at") or "") <= created]
            if not matches:
                unplace(rec, kind, "no escalation event shares diag_signature at or before this record's created")
                continue
            key, _ev = max(matches, key=lambda kv: str(kv[1].get("at") or ""))
            parent = None
            if key.startswith("stall:"):
                stall_at = key.split(":", 1)[1]
                open_at_stall = [a for a, opened in attempt_opened_at.items() if opened <= stall_at]
                attempt = max(open_at_stall) if open_at_stall else None
            elif key in entries:
                attempt, parent = entries[key]["attempt"], key
            else:
                unplace(rec, kind, f"the escalation event names {key!r}, which no job record resolves to")
                continue
            cause = override_cause or "diagnose"
            if attempt is None:
                task_diag_seq += 1
                place(job_id, "diagnosis", None, task_diag_seq, parent, cause,
                     "stall_key" if key.startswith("stall:") else "escalation_event", scope="task")
            else:
                diag_seq[attempt] = diag_seq.get(attempt, 0) + 1
                place(job_id, "diagnosis", attempt, diag_seq[attempt], parent, cause,
                     "stall_key" if key.startswith("stall:") else "escalation_event")

    return entries, flags, unplaced


def _compare_mapping(new_mapping: dict[str, dict], baseline_path: Path) -> dict[str, Any]:
    """`new_mapping` against an earlier run's `mapping.json`: every row present in both and equal is unchanged,
    one in `new_mapping` only is added (a record dispatched since), one in both but different is changed -- the
    dry run raises on that last case (section 8.4: a re-run on an unchanged store is byte-identical, so a
    changed row means the store moved under the mapping, or a different `overrides.yaml` was given)."""
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    unchanged, added, changed = 0, [], []
    for job_id, entry in new_mapping.items():
        if job_id not in baseline:
            added.append(job_id)
        elif baseline[job_id] == entry:
            unchanged += 1
        else:
            changed.append(job_id)
    return {"baseline": str(baseline_path), "unchanged": unchanged, "added": sorted(added),
           "changed": sorted(changed), "removed": sorted(j for j in baseline if j not in new_mapping)}


def dry_run_attempts(layout: Layout, out: Path | str, *, overrides_path: Path | str | None = None,
                     compare_path: Path | str | None = None) -> dict[str, Any]:
    """Derive the attempt-identity mapping (section 8.2) from the store as it stands, and write `mapping.json`,
    `report.json` and `summary.md` under `out` -- nothing else, nowhere else (section 8.3/8.4). Every job record
    and task document is only ever read; every escalation file under `state/runtime/escalation/` is opened
    read-only and its digest recorded before and after, so a difference would fail loudly rather than silently.
    `overrides_path` (section 8.2) re-derives the whole mapping with the named causes forced; `compare_path`
    (an earlier run's `mapping.json`) raises MigrationError if any row changed, after the files are written, so
    the evidence of what changed is on disk even when the run is refused."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    overrides, overrides_meta = _load_overrides(overrides_path)

    tasks_by_id = tasks_mod.load_all(layout)
    mapping: dict[str, dict] = {}
    flags: list[dict] = []
    unplaced: list[dict] = []
    by_kind: dict[str, int] = {}
    by_source: dict[str, int] = {}
    histogram: dict[str, int] = {}
    escalation_digests: dict[str, str] = {}
    run_dir_baseline: dict[str, dict] = {}

    for task_id in sorted(tasks_by_id):
        records = _dispatch_order(jobs_mod.for_task(layout, task_id))
        if not records:
            continue
        task = tasks_by_id[task_id]
        events, digest_before = _read_escalation_readonly(layout, task_id)
        if digest_before is not None:
            escalation_digests[task_id] = digest_before
        entries, task_flags, task_unplaced = _map_task(task, records, overrides, events.get("events") or {})
        mapping.update(entries)
        flags += task_flags
        unplaced += task_unplaced
        for entry in entries.values():
            by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
            by_source[entry["source"]] = by_source.get(entry["source"], 0) + 1
        attempts_here = [e["attempt"] for e in entries.values() if e["attempt"] is not None]
        if attempts_here:
            n = max(attempts_here)
            histogram[str(n)] = histogram.get(str(n), 0) + 1
        for rec in records:
            run_dir = (rec.get("spec") or {}).get("run_dir")
            if run_dir:
                run_dir_baseline[rec["attempt_id"]] = {"run_dir": run_dir, "existed": Path(run_dir).exists()}
        _, digest_after = _read_escalation_readonly(layout, task_id)
        if digest_after != digest_before:
            raise MigrationError(f"{task_id}: its escalation file changed while the dry run read it")

    import supervisor as _pkg   # resolved here, not imported at module load, so a test can patch the attribute
    report: dict[str, Any] = {
        "totals": {"records": len(mapping) + len(unplaced), "placed": len(mapping), "unplaced": len(unplaced)},
        "by_kind": by_kind, "by_source": by_source, "unplaced": unplaced, "flags": flags,
        "attempts_per_task": histogram, "escalation_digests": escalation_digests,
        "run_dir_baseline": run_dir_baseline, "overrides": overrides_meta,
        "package": {"file": _pkg.__file__, "version": _pkg.__version__},
    }
    if compare_path:
        report["compare"] = _compare_mapping(mapping, compare_path)

    summary = [f"supervisor {_pkg.__version__} ({_pkg.__file__})", "",
              f"placed {report['totals']['placed']} of {report['totals']['records']} job record(s), "
              f"{report['totals']['unplaced']} unplaced", f"by kind: {by_kind}", f"by evidence source: {by_source}"]
    if flags:
        summary.append(f"flagged {len(flags)}: {', '.join(sorted(f['job'] for f in flags))}")
    if unplaced:
        summary.append(f"unplaced {len(unplaced)}: {', '.join(sorted(u['job'] for u in unplaced))}")
    if overrides_meta:
        summary.append(f"overrides: {overrides_meta['path']} ({overrides_meta['sha256'][:12]})")
    if "compare" in report:
        c = report["compare"]
        summary.append(f"compared with {c['baseline']}: {c['unchanged']} unchanged, "
                       f"{len(c['added'])} added, {len(c['changed'])} changed")

    (out / "mapping.json").write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")

    if report.get("compare", {}).get("changed"):
        raise MigrationError(f"--compare {compare_path}: {len(report['compare']['changed'])} row(s) changed since "
                             f"the baseline: {', '.join(report['compare']['changed'])} "
                             f"(written to {out} for inspection)")
    return report


# ---- the attempt-identity migration's apply and check ----
#
# `apply` takes a reviewed `mapping.json` (never derives its own) and writes its identity fields onto the job
# records it names, under the existing atomic write and the task lock; `check` re-reads the store and asserts the
# seven claims of section 8.5 against it and against the dry run's own `report.json`. Neither ever opens
# `state/runtime/escalation/` for anything but a read (apply does not open it at all), never renames a job record
# file or a run directory, and never invents a mapping of its own.

IDENTITY_FIELDS = ("attempt", "kind", "seq", "parent", "cause", "scope")


class _NoCountersChange(Exception):
    """Raised inside a `store.update` callback to abort a task write whose counters already cover the observed
    maxima -- the write that never happens is what keeps a second apply a no-op (§8.4)."""


def _check_preconditions(layout: Layout) -> None:
    """Section 8.1: the loop must be stopped before apply writes anything -- `state/.lock` free and the runtime's
    own status not RUNNING -- so a live dispatch can never land on a record apply is mid-rewriting. The CLI's
    `_writer()` already refuses a held lock for every command that mutates the store; this is the same rule,
    checked again here so a caller of the library function directly (a test, a script) gets it too."""
    from . import locks
    held = locks.holder(layout.lock) if layout.lock.exists() else None
    if held is not None:
        raise MigrationError(f"state/.lock is held by {held.get('owner', 'unknown')}: stop the loop before "
                             f"applying the attempts migration")
    from . import runtime as runtime_mod
    status = runtime_mod.read(layout).get("status")
    if status == "RUNNING":
        raise MigrationError(f"the runtime status is RUNNING: stop the loop before applying the attempts migration")


def _load_mapping(mapping_path: Path | str | None) -> dict[str, dict]:
    """The reviewed `mapping.json` apply/check take as their one input. Refuses when none is given or the file
    does not exist (§8.4: apply never derives its own mapping -- no dry run, no apply)."""
    if not mapping_path:
        raise MigrationError("no mapping file given: pass --mapping <dir>/mapping.json from a reviewed dry run")
    p = Path(mapping_path)
    if not p.exists():
        raise MigrationError(f"{p}: no such mapping file -- apply/check refuse without a reviewed dry run's "
                             f"mapping.json")
    return json.loads(p.read_text(encoding="utf-8"))


def _identity_fields(entry: dict) -> dict[str, Any]:
    """The subset of `entry` apply ever writes: §2.6's identity fields, omitting the ones the mapping leaves
    unset (`None`) exactly as `jobs.prepare` already does -- so a task-scoped job's record gains no stray
    `attempt: null` and the write this produces is the same whether it runs once or twice."""
    return {k: entry[k] for k in IDENTITY_FIELDS if entry.get(k) is not None}


def _record_matches(rec: dict, wanted: dict[str, Any]) -> bool:
    return all(rec.get(k) == v for k, v in wanted.items())


def _counter_maxima(entries: list[dict]) -> dict[str, Any]:
    """From one task's mapping entries: the highest attempt number, and the highest per-kind `seq` inside each
    attempt and among its task-scoped jobs -- the maxima §8.3 says counters must be set to, never below, so the
    allocator that starts using these counters (step 9) can never mint an id already used here."""
    attempt_max = 0
    per_attempt: dict[int, dict[str, int]] = {}
    task_jobs: dict[str, int] = {}
    for e in entries:
        kind, seq = e.get("kind"), int(e.get("seq") or 0)
        if e.get("attempt") is not None:
            attempt_max = max(attempt_max, int(e["attempt"]))
            bucket = per_attempt.setdefault(int(e["attempt"]), {})
            bucket[kind] = max(bucket.get(kind, 0), seq)
        elif e.get("scope") == "task":
            task_jobs[kind] = max(task_jobs.get(kind, 0), seq)
    return {"attempt": attempt_max, "jobs": per_attempt, "task_jobs": task_jobs}


def _merge_counters(existing: dict, wanted: dict[str, Any]) -> tuple[dict, bool]:
    """`existing` (a task's current `counters`) with `wanted`'s maxima merged in monotonically -- nothing here
    ever lowers a value (§8.3) -- and whether anything actually changed, so a task already at or above every
    maximum is left untouched rather than rewritten with an identical document."""
    changed = False
    out = dict(existing)
    if wanted["attempt"] > int(out.get("attempt") or 0):
        out["attempt"], changed = wanted["attempt"], True
    jobs = {int(k): dict(v) for k, v in (out.get("jobs") or {}).items()}
    for attempt, kinds in wanted["jobs"].items():
        bucket = jobs.setdefault(attempt, {})
        for kind, seq in kinds.items():
            if seq > int(bucket.get(kind) or 0):
                bucket[kind], changed = seq, True
    out["jobs"] = jobs
    task_jobs = dict(out.get("task_jobs") or {})
    for kind, seq in wanted["task_jobs"].items():
        if seq > int(task_jobs.get(kind) or 0):
            task_jobs[kind], changed = seq, True
    out["task_jobs"] = task_jobs
    return out, changed


def _bump_task_counters(layout: Layout, task_id: str, wanted: dict[str, Any]) -> bool:
    """Merge `wanted`'s maxima into the task document's own `counters`, under the same lock and atomic write
    every other task mutation uses -- never a second one of apply's own. Returns whether anything was written."""
    bumped = False

    def fn(doc: dict) -> dict:
        nonlocal bumped
        merged, changed = _merge_counters(doc.get("counters") or {}, wanted)
        if not changed:
            raise _NoCountersChange()
        doc["counters"] = merged
        bumped = True
        return doc

    try:
        store.update(tasks_mod.path_for(layout, task_id), fn, lock_path=layout.state / ".tasks.lock")
    except _NoCountersChange:
        pass
    return bumped


def _by_task(mapping: dict[str, dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for entry in mapping.values():
        out.setdefault(entry["task"], []).append(entry)
    return out


def apply_attempts(layout: Layout, mapping_path: Path | str, out: Path | str) -> dict[str, Any]:
    """Section 8, step 7: write a reviewed `mapping.json`'s identity fields onto the job records it names, and
    the counters its tasks imply. Refuses without a mapping file and refuses while the loop runs (§8.1). A
    record already carrying `migrated.attempts` is verified against the mapping, never rewritten; a mismatch is
    an error, not an overwrite, so a hand-edited mapping can never silently relabel an already-migrated record.
    An allocator-written record with no stamp gets the same treatment, narrowed to the fields the allocator
    itself derives (`attempt`/`kind`/`seq`/`parent`): verified, never rewritten, and never compared on
    `cause`/`scope`, which this migration's own re-derivation can get wrong for such a record without that ever
    being a placement error (P3-R2-A).
    The backup of `state/runtime/jobs/` and `state/tasks/` is taken once, before this call's own first write --
    never when there is nothing left to write, which is what makes a second apply with the same mapping a true
    no-op, and what lets an apply interrupted mid-way simply resume: every record this call has already written
    is independently complete, and the next call treats it as already migrated."""
    _check_preconditions(layout)
    mapping_path = Path(mapping_path)
    mapping = _load_mapping(mapping_path)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)

    to_write: list[str] = []
    already: list[str] = []
    conflicts: list[str] = []
    missing: list[str] = []
    for job_id, entry in mapping.items():
        rec = jobs_mod.resolve(layout, job_id)
        if rec is None:
            missing.append(job_id)
            continue
        wanted = _identity_fields(entry)
        if (rec.get("migrated") or {}).get("attempts"):
            (already if _record_matches(rec, wanted) else conflicts).append(job_id)
        elif _has_identity_fields(rec):
            # Allocator-written but never stamped (P3-R2-A, carried from the 2.8.0 round-2 review): verify it
            # against the mapping the same way a stamped record is, but only on the fields the allocator itself
            # derives -- attempt, kind, seq, parent. Never compare, and so never rewrite, cause/scope: this
            # migration's own re-derivation reads the task-side attempt note, which an allocator-written record
            # never had one written for, so a revision's `cause` would otherwise re-derive as `scheduled` and
            # overwrite the allocator's own value on every apply.
            core = {k: wanted[k] for k in ("attempt", "kind", "seq", "parent") if k in wanted}
            (already if _record_matches(rec, core) else conflicts).append(job_id)
        else:
            to_write.append(job_id)

    if missing:
        raise MigrationError(f"{len(missing)} job id(s) in the mapping resolve to no record (nothing was "
                             f"written): {', '.join(sorted(missing))}")
    if conflicts:
        raise MigrationError(f"{len(conflicts)} record(s) already migration-stamped disagree with this mapping "
                             f"(left as they were, not overwritten): {', '.join(sorted(conflicts))}")

    backup = None
    if to_write:
        backup = _backup_store(layout)
        mapping_sha256 = hashlib.sha256(mapping_path.read_bytes()).hexdigest()
        for job_id in sorted(to_write):
            entry = mapping[job_id]
            rec = dict(jobs_mod.resolve(layout, job_id))
            rec.update(_identity_fields(entry))
            rec["migrated"] = {**(rec.get("migrated") or {}),
                               "attempts": {"at": store.now_iso(), "mapping_sha256": mapping_sha256}}
            jobs_mod.write(layout, rec)

    # Raised unconditionally, even when `to_write` is empty: an interruption that lands after every job record
    # is written but before (or partway through) this loop must still find every counter short of its maximum
    # on the next call, since that call's own `to_write` is empty and nothing else would ever raise them.
    bumped: list[str] = []
    for task_id, entries in sorted(_by_task(mapping).items()):
        if _bump_task_counters(layout, task_id, _counter_maxima(entries)):
            bumped.append(task_id)

    report = {"mapping": str(mapping_path), "written": sorted(to_write), "already_migrated": sorted(already),
             "counters_bumped": sorted(bumped), "backup": str(backup) if backup else None}
    (out / "apply_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _backup_store(layout: Layout) -> Path:
    """`state/runtime/migrations/attempts-<timestamp>/backup/{jobs,tasks}` -- a plain copy of `state/runtime/jobs/`
    and `state/tasks/` as they stand right now, taken once per call that is about to write (never when nothing
    is), before the first of this call's writes. A microsecond-resolution timestamp (rather than `store.now_iso`'s
    seconds) keeps two backups inside the same second, from two apply calls a few milliseconds apart, from ever
    landing on the same directory."""
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    backup = layout.runtime / "migrations" / f"attempts-{ts}" / "backup"
    shutil.copytree(layout.jobs, backup / "jobs")
    shutil.copytree(layout.tasks, backup / "tasks")
    return backup


def check_attempts(layout: Layout, mapping_path: Path | str, out: Path | str) -> dict[str, Any]:
    """Section 8.5: the seven assertions, each checked and every failure of each collected before any is raised,
    so one run names everything wrong instead of one thing per re-run. Reads the store and the mapping's own
    `report.json` (written beside `mapping.json` by the dry run that produced it); writes only `check_report.json`
    under `--out`. Assertion 6 reads `expect.json` beside the mapping -- the committed, by-hand mapping of a
    handful of named tasks that a deployed project keeps for its own migration (a neutral fixture's equivalent in this
    framework's own tests) -- and fails loudly when it is missing, since an anonymous or absent expectation file
    cannot fail when a mapping rule changes."""
    mapping_path = Path(mapping_path)
    mapping = _load_mapping(mapping_path)
    report_path = mapping_path.parent / "report.json"
    if not report_path.exists():
        raise MigrationError(f"no report.json beside {mapping_path}: run the dry run that produced this mapping "
                             f"first (its report.json is check's own baseline)")
    dry_report = json.loads(report_path.read_text(encoding="utf-8"))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)

    failures: dict[str, list[str]] = {}

    def fail(key: str, msg: str) -> None:
        failures.setdefault(key, []).append(msg)

    # 1. every pre-migration job id resolves to a record.
    for job_id in mapping:
        if jobs_mod.resolve(layout, job_id) is None:
            fail("1_job_resolves", job_id)

    # 2. the run-directory baseline: a value the dry run saw existing must still exist at the same path, and no
    # recorded run_dir value may differ -- a path absent already at dry-run time is not a failure either way.
    for job_id, info in (dry_report.get("run_dir_baseline") or {}).items():
        rec = jobs_mod.resolve(layout, job_id)
        if rec is None:
            continue   # already named by assertion 1; a missing record is not a second, run_dir failure too
        current = (rec.get("spec") or {}).get("run_dir")
        if current != info.get("run_dir"):
            fail("2_run_dir_baseline", f"{job_id}: run_dir changed ({info.get('run_dir')!r} -> {current!r})")
        elif info.get("existed") and not Path(info["run_dir"]).exists():
            fail("2_run_dir_baseline", f"{job_id}: run_dir existed at dry-run time and is gone now "
                                      f"({info['run_dir']})")

    # 3. counters >= every ordinal the mapping observes for that task, kind by kind.
    by_task = _by_task(mapping)
    for task_id, entries in sorted(by_task.items()):
        wanted = _counter_maxima(entries)
        try:
            task = tasks_mod.load(layout, task_id)
        except tasks_mod.TaskError as exc:
            fail("3_counters", f"{task_id}: {exc}")
            continue
        counters = task.counters or {}
        if int(counters.get("attempt") or 0) < wanted["attempt"]:
            fail("3_counters", f"{task_id}: counters.attempt {counters.get('attempt')!r} < {wanted['attempt']}")
        jobs_counters = counters.get("jobs") or {}
        for attempt, kinds in wanted["jobs"].items():
            bucket = jobs_counters.get(attempt) or jobs_counters.get(str(attempt)) or {}
            for kind, seq in kinds.items():
                if int(bucket.get(kind) or 0) < seq:
                    fail("3_counters", f"{task_id} attempt {attempt}: counters.jobs[{kind}] "
                                      f"{bucket.get(kind)!r} < {seq}")
        task_jobs_counters = counters.get("task_jobs") or {}
        for kind, seq in wanted["task_jobs"].items():
            if int(task_jobs_counters.get(kind) or 0) < seq:
                fail("3_counters", f"{task_id}: counters.task_jobs[{kind}] {task_jobs_counters.get(kind)!r} < {seq}")

    # 4. per-kind counts, the attempts-per-task histogram and the unplaced list equal the dry run's own.
    computed_by_kind: dict[str, int] = {}
    computed_unplaced: set[str] = set()
    for job_id, entry in mapping.items():
        computed_by_kind[entry["kind"]] = computed_by_kind.get(entry["kind"], 0) + 1
        if entry.get("source") == "unplaced":
            computed_unplaced.add(job_id)
    computed_histogram: dict[str, int] = {}
    for entries in by_task.values():
        attempts_here = [e["attempt"] for e in entries if e.get("attempt") is not None]
        if attempts_here:
            n = str(max(attempts_here))
            computed_histogram[n] = computed_histogram.get(n, 0) + 1
    if computed_by_kind != (dry_report.get("by_kind") or {}):
        fail("4_counts", f"by_kind {computed_by_kind} != report.json's {dry_report.get('by_kind')}")
    if computed_histogram != (dry_report.get("attempts_per_task") or {}):
        fail("4_counts", f"attempts_per_task {computed_histogram} != report.json's "
                        f"{dry_report.get('attempts_per_task')}")
    reported_unplaced = {u["job"] for u in (dry_report.get("unplaced") or [])}
    if computed_unplaced != reported_unplaced:
        fail("4_counts", f"unplaced {sorted(computed_unplaced)} != report.json's {sorted(reported_unplaced)}")

    # 5. every escalation file's digest is unchanged from the dry run's own reading of it.
    for task_id, digest in (dry_report.get("escalation_digests") or {}).items():
        _, current_digest = _read_escalation_readonly(layout, task_id)
        if current_digest != digest:
            fail("5_escalation_digests", f"{task_id}: escalation digest changed or the file is gone")

    # 6. the committed expectation file, holding a handful of named tasks' by-hand mapping, matches exactly.
    expect_path = mapping_path.parent / "expect.json"
    if not expect_path.exists():
        fail("6_expectation_file", f"no committed expectation file at {expect_path}")
    else:
        expected = json.loads(expect_path.read_text(encoding="utf-8"))
        for job_id, want in expected.items():
            got = mapping.get(job_id)
            have = [got.get("attempt"), got.get("kind"), got.get("seq")] if got else None
            if have != list(want):
                fail("6_expectation_file", f"{job_id}: expected {want}, mapping has {have}")

    # 7. every attempt has at least one delivery session, every non-task-scoped job has an attempt that exists,
    # and every parent resolves.
    attempt_kinds: dict[tuple[str, int], set[str]] = {}
    for job_id, entry in mapping.items():
        parent = entry.get("parent")
        if parent is not None and parent not in mapping:
            fail("7_integrity", f"{job_id}: parent {parent!r} does not resolve")
        if entry.get("scope") != "task" and entry.get("attempt") is None:
            fail("7_integrity", f"{job_id}: not task-scoped but has no attempt")
        if entry.get("attempt") is not None:
            attempt_kinds.setdefault((entry["task"], entry["attempt"]), set()).add(entry["kind"])
    for (task_id, attempt), kinds in attempt_kinds.items():
        if "delivery" not in kinds:
            fail("7_integrity", f"{task_id} attempt {attempt}: no delivery session")

    check_report = {"mapping": str(mapping_path), "report": str(report_path), "failures": failures,
                    "ok": not failures}
    (out / "check_report.json").write_text(json.dumps(check_report, indent=2, sort_keys=True) + "\n",
                                           encoding="utf-8")
    if failures:
        lines = [f"{key}: {'; '.join(msgs)}" for key, msgs in sorted(failures.items())]
        raise MigrationError("migrate attempts --check failed:\n" + "\n".join(lines))
    return check_report
