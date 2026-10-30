"""State/config migrations between framework versions. Each migration is idempotent and records nothing but the new
`framework_version` in project.yaml. `supervisor doctor` reports a mismatch; `supervisor migrate` applies these.

After the framework's own, `supervisor migrate` runs the migrations each configured plugin's manifest names
(`migrations = "<module>"`, a module exposing MIGRATIONS with the same `(layout, dry_run) -> notes` contract). Only
that module is imported, never the plugin's setup(); a plugin whose framework range excludes this framework
is skipped (it does not load either)."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Callable

import yaml

from . import __version__, project, store, yaml_edit
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
_OLD_SECTION_LINE = re.compile(r"""^([ \t]*section[ \t]*=[ \t]*)(["'])extensions\.""", re.MULTILINE)
_TABLE_HEADER = re.compile(r"""^[ \t]*\[[ \t]*([A-Za-z_][A-Za-z0-9_-]*)[ \t]*\][ \t]*(?:#.*)?$""", re.MULTILINE)


def _table_span(text: str, name: str) -> tuple[int, int] | None:
    """The text span holding table `[name]`'s body: after its header line, up to the next `[other]` header or the
    end of the file. None when the manifest has no such header (an inline `name = { ... }` is `_rewrite_inline`'s,
    not this)."""
    headers = list(_TABLE_HEADER.finditer(text))
    for i, h in enumerate(headers):
        if h.group(1) == name:
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
        value = body[vs:ve]
        if (name == "section" and len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
                and value[1:-1].startswith("extensions.")):
            value = value[0] + "plugins." + value[1:-1][len("extensions."):] + value[-1]
        out.append(value)
        pos = ve
    out.append(body[pos:])
    return line[:start + 1] + "".join(out) + line[end:]


def _renamed_manifest(text: str) -> str:
    text = _in_table(text, "requires", lambda body: _OLD_REQUIRES.sub(r"\1\2plugins\2", body))
    text = _OLD_DOTTED.sub(r"\1\2plugins\2", text)       # `requires.extensions`, scoped by the key itself
    text = _INLINE_TABLE.sub(lambda m: _rewrite_inline(m.group(0)), text)
    return _in_table(text, "config", lambda body: _OLD_SECTION_LINE.sub(r"\1\2plugins.", body))


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


def _still_old(text: str) -> bool:
    """Whether a rewritten manifest still reads the old names (a form no rule above renames), so the plugin would
    not load after migrate recorded 2.0.0 (review 2.0.0). A manifest TOML cannot read is doctor's to report."""
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
