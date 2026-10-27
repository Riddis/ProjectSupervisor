"""State/config migrations between framework versions. Each migration is idempotent and records nothing but the new
`framework_version` in project.yaml. `supervisor doctor` reports a mismatch; `supervisor migrate` applies these.

After the framework's own, `supervisor migrate` runs the migrations each configured extension's manifest names
(`migrations = "<module>"`, a module exposing MIGRATIONS with the same `(layout, dry_run) -> notes` contract). Only
that module is imported, never the extension's setup(); an extension whose framework range excludes this framework
is skipped (it does not load either)."""
from __future__ import annotations

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
    """The manifests of the configured extensions that support this framework, read by discovery (no import)."""
    from . import discovery, extensions
    out = []
    for name, _required in extensions.specs(layout):
        m = discovery.find(layout, name).manifest
        if m is not None and m.supports():
            out.append(m)
    return out


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


def _extension_config(layout: Layout, dry_run: bool) -> list[str]:
    """Each configured extension's `config.legacy_sections` -> `extensions.<identity>` (1.12.0): the section named like
    the identity at the top, any other nested under its own name. Comments move with them. Driven by the manifests
    discovery reads; no extension code runs. An extension whose section already exists is left alone."""
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
        owned = doc.get("extensions")
        if m.config is None or (isinstance(owned, dict) and m.identity in owned) \
                or (owned is not None and not isinstance(owned, dict)):
            continue
        present = [s for s in m.config.legacy_sections if s in doc]
        if not present:
            continue
        moves = [(m.identity, ("extensions", m.identity))] if m.identity in present else []
        moves += [(s, ("extensions", m.identity, s)) for s in present if s != m.identity]
        text = yaml_edit.move_sections(text, moves)
        doc = yaml.safe_load(text)
        notes.append(f"config/routing.yaml: {', '.join(present)} moved to extensions.{m.identity} "
                     f"(extension {m.identity})")
    if notes and not dry_run:
        store.rewrite_text(cfg, text)
    return notes


def _extension_migrations(layout: Layout, dry_run: bool) -> list[str]:
    """The migrations each configured extension's manifest names, after the framework's. Notes are prefixed with the
    extension's identity; a failing one stops the run with MigrationError naming it."""
    from . import extensions
    notes = []
    for m in _configured_manifests(layout):
        if not m.migrations:
            continue
        try:
            module = extensions.import_named_module(layout, m.migrations)
            for migration in getattr(module, "MIGRATIONS", []):
                notes += [f"{m.identity}: {n}" for n in migration(layout, dry_run)]
        except store.CannotKeep:
            raise
        except Exception as exc:                            # named and stopped, never half-reported
            raise MigrationError(f"extension {m.identity!r}: its migration ({m.migrations}) failed: {exc}") from exc
    return notes


MIGRATIONS: list[Migration] = [_routing_limits_rename, _tasks_cost_category, _approval_responses, _resources_gpu,
                               _extension_config]


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
    notes: list[str] = []
    try:
        for m in MIGRATIONS:
            notes += m(layout, dry_run)
        notes += _extension_migrations(layout, dry_run)
        raw = project.raw(layout)
        recorded = str(raw.get("framework_version") or "")
        if recorded != __version__:
            notes.append(f"project.yaml: framework_version {recorded or 'unset'} → {__version__}")
            if not dry_run:
                raw["framework_version"] = __version__
                project.write(layout, raw)
    except store.CannotKeep as exc:
        raise MigrationError(f"cannot migrate: {exc}") from exc
    return notes
