"""review_rounds: a Supervisor plugin package.

Per-image verdicts and label files layered over an inbox item of kind `round` or `labelling`. The item's own
`items: list[dict]` *is* the round's state - each entry carries at least `id` and `path`, plus whatever score
fields the dispatching task put there, and gains `verdict`, `note`, `reason_tags`, `verdict_by`, `verdict_at` as
the owner works through the grid. Writing verdicts straight into the item means `submit()` has nothing to
translate: the per-image verdicts are already sitting on the item that `close_owned_item()` closes out.

The read helpers (`load_round`, `image_ids`, `label_order`, `progress`, `agreement_stats`) never touch disk, so
the review page calls them directly even from a GET. The two mutations (`set_verdict`, `submit`) go through
`supervisor.plugin.submit_command` instead (kinds `review.set_verdict` / `review.submit`), same as every other
mutating Control Center action - a running supervisor holds the state lock, and a command submitted while it does
is queued rather than applied inline.

Ported from a project-local extension of the same shape (boundary plan phase 5, step 5). Two parts of that
extension stay behind, for a project's own plugin (boundary D6, D7): a request kind whose page is an upload form
rather than a grid, and a label destination tied to a project's own identity-folder layout. This package's own
label destination is a project-configured template over the round's own fields instead - see `labels_path()` and
`plugins.review_rounds.labels_path_template`.

Import only `supervisor.plugin`, and `supervisor.testing` in tests; register only inside setup()."""
from __future__ import annotations

import random
import re
from pathlib import Path

import yaml

from supervisor import plugin

ROUND_KINDS = ("round", "labelling")
# REDO is this package's own word for "make this one again". A project migrating from an older, project-local
# extension may still carry its own legacy words for the same thing (MORE_LIKE, REGENERATE in the extension this
# package was ported from) on rows written before the cutover; `canonical_verdict()`/`migrate_legacy_verdicts()`
# below read and rewrite those two specific words as REDO so an unmigrated round still behaves, and
# `migrate_legacy_verdicts()` never touches an already-answered item or a written label file - those are historical
# records, kept as written.
VERDICTS = ("ACCEPT", "REJECT", "MODIFY", "REDO")
LEGACY_VERDICTS = {"MORE_LIKE": "REDO", "REGENERATE": "REDO"}

_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def review_rounds_config(layout: plugin.Layout) -> dict:
    """plugins.review_rounds, with its schema's defaults filled in."""
    return plugin.config(layout, "review_rounds")


def canonical_verdict(value) -> str | None:
    """The verdict as the grid spells it today: a legacy word reads as REDO; None stays None."""
    if not value:
        return None
    v = str(value).strip().upper()
    return LEGACY_VERDICTS.get(v, v)


def recorded_verdict(row: dict) -> str | None:
    """The word actually recorded for a row when it differs from the canonical verdict: the migrated
    `verdict_legacy` field first, else a legacy word still stored as the verdict itself. A new owner verdict
    supersedes the old one, so set_verdict drops the field and this returns None for the row."""
    legacy = row.get("verdict_legacy")
    if legacy:
        return str(legacy)
    word = str(row.get("verdict") or "").upper()
    return row.get("verdict") if word in LEGACY_VERDICTS else None


def legacy_verdict_rows(item: dict) -> list[dict]:
    return [r for r in (item.get("items") or []) if isinstance(r, dict) and str(r.get("verdict") or "").upper() in LEGACY_VERDICTS]


class RoundError(ValueError):
    pass


def _dump(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120)


def load_round(layout: plugin.Layout, item_id: str) -> dict:
    """The round or labelling item `item_id`. plugin.InboxError when there is no such item; RoundError for an
    item of any other kind (this package may read only the kinds it claimed)."""
    try:
        item = plugin.owned_item(layout, item_id)
    except plugin.InboxError:
        if not (layout.inbox / f"{item_id}.yaml").exists():
            raise
        raise RoundError(f"{item_id} is not a review round") from None
    if item.get("kind") not in ROUND_KINDS:
        raise RoundError(f"{item_id} is not a review round (kind={item.get('kind')!r})")
    return item


def image_ids(item: dict) -> list[str]:
    return [str(i["id"]) for i in item.get("items") or []]


def label_order(item: dict) -> list[str]:
    """Read-only display order for a labelling item's images: deterministic from the item's own id, so every
    call agrees without writing anything. A plain `round` keeps the order it was created with - only `labelling`
    sessions are blind to ordering. Honours an order already persisted by `ensure_label_order` (e.g. from an
    earlier submit) if it still names exactly this item's images."""
    ids = image_ids(item)
    if item.get("kind") != "labelling":
        return ids
    existing = item.get("label_order")
    if existing and sorted(str(x) for x in existing) == sorted(ids):
        return [str(x) for x in existing]
    order = list(ids)
    random.Random(str(item.get("id"))).shuffle(order)
    return order


def ensure_label_order(layout: plugin.Layout, item_id: str) -> list[str]:
    """Persist (once) the randomised display order `label_order()` computes, so it's part of the record a
    labelling session leaves behind. Idempotent: once `label_order` is on disk and still names exactly this
    item's images, later calls return it unchanged rather than reshuffling under the owner's feet. Only called
    from `submit()` - never from a GET, which must stay read-only."""
    item = load_round(layout, item_id)
    ids = image_ids(item)
    if item.get("kind") != "labelling":
        return ids
    existing = item.get("label_order")
    if existing and sorted(str(x) for x in existing) == sorted(ids):
        return [str(x) for x in existing]
    order = label_order(item)

    def fn(doc):
        current = [str(x) for x in (doc.get("label_order") or [])]
        if current and sorted(current) == sorted(image_ids(doc)):
            return doc                                       # a concurrent request already set it
        doc["label_order"] = order
        return doc

    doc = plugin.update_owned_item(layout, item_id, fn)
    return [str(x) for x in doc["label_order"]]


def set_verdict(layout: plugin.Layout, item_id: str, image_id: str, verdict: str, note: str = "",
                reason_tags: list[str] | None = None, by: str = "owner") -> dict:
    verdict = (verdict or "").strip().upper()
    if verdict not in VERDICTS:
        raise RoundError(f"{verdict!r} is not one of {VERDICTS}")
    load_round(layout, item_id)                       # another kind is a RoundError, as before; fn re-checks it under the lock

    def fn(doc):
        if doc.get("kind") not in ROUND_KINDS:
            raise RoundError(f"{item_id} is not a review round (kind={doc.get('kind')!r})")
        if doc.get("status") != "open":
            raise RoundError(f"{item_id} is already {doc.get('status')}: cannot record a verdict")
        rows = doc.get("items") or []
        for row in rows:
            if str(row.get("id")) == str(image_id):
                row["verdict"] = verdict
                row.pop("verdict_legacy", None)      # a new owner verdict supersedes the migrated word
                row["note"] = note
                if reason_tags is not None:
                    row["reason_tags"] = list(reason_tags)
                row["verdict_by"] = by
                row["verdict_at"] = plugin.now_iso()
                doc["items"] = rows
                return doc
        raise RoundError(f"no image {image_id!r} in {item_id}")

    doc = plugin.update_owned_item(layout, item_id, fn)
    plugin.log_event(layout, "review_verdict", by, item=item_id, image=image_id, verdict=verdict)
    return doc


def progress(item: dict) -> tuple[int, int]:
    rows = item.get("items") or []
    return sum(1 for r in rows if r.get("verdict")), len(rows)


def _retest_pairs(rows: list[dict]) -> list[tuple[dict, dict]]:
    by_id = {str(r["id"]): r for r in rows if r.get("id") is not None}
    pairs = []
    for row in rows:
        source = row.get("retest_of")
        if source is not None and str(source) in by_id:
            pairs.append((by_id[str(source)], row))
    return pairs


def agreement_stats(item: dict) -> dict:
    """Test-retest agreement over `items[*].retest_of` pairs: how often the second (blind) look at an image
    already labelled earlier in the same session landed on the same verdict."""
    rows = item.get("items") or []
    pairs = _retest_pairs(rows)
    scored = [(a, b) for a, b in pairs if a.get("verdict") and b.get("verdict")]
    agreed = sum(1 for a, b in scored if canonical_verdict(a["verdict"]) == canonical_verdict(b["verdict"]))
    return {"pairs": len(pairs), "scored": len(scored), "agreed": agreed,
            "agreement_rate": (agreed / len(scored)) if scored else None}


def _safe_value(value) -> str:
    return _SAFE.sub("", str(value))[:64]


def labels_path(layout: plugin.Layout, item: dict, item_id: str) -> Path:
    """Where a labelling session's record lands: `plugins.review_rounds.labels_path_template`, root-relative,
    formatted against the round's own top-level fields (plus `item_id`). Every substituted value is reduced to
    `[A-Za-z0-9_-]` first - a round's fields may ride in from a dispatching task, so a traversal or an absolute
    path hiding in one of them must not become a write destination - and the formatted path is containment-checked
    against the project root again afterwards, belt and braces."""
    cfg = review_rounds_config(layout)
    template = cfg["labels_path_template"]
    values = {k: _safe_value(v) for k, v in item.items() if isinstance(v, (str, int, float))}
    values["item_id"] = _safe_value(item_id)
    try:
        rel = template.format_map(values)
    except KeyError as exc:
        raise RoundError(f"{item_id}: plugins.review_rounds.labels_path_template names a field {exc} this round "
                          f"does not have") from None
    segments = rel.replace("\\", "/").split("/")
    if any(not s or s in (".", "..") for s in segments):
        raise RoundError(f"{item_id}: labels_path_template produced an invalid path ({rel!r}); check the fields "
                          f"it names resolve to a non-empty, non-traversal value for this round")
    dest = layout.root / rel
    try:
        ok = plugin.contained(dest, [layout.root], root=layout.root)
    except ValueError as exc:
        raise RoundError(f"{item_id}: cannot validate the labels destination: {exc}") from exc
    if not ok:
        raise RoundError(f"{item_id}: labels_path_template resolves outside the project root")
    return dest


def submit(layout: plugin.Layout, item_id: str, partial: bool = False, session_seconds: float | None = None,
          by: str = "owner", command_id: str | None = None) -> dict:
    """Finalise a round: refuse an incomplete round unless `partial` is explicit, write the labelling file (if
    this is a labelling session) and close the inbox item - the one close the task's submit asks for, carrying
    the per-image verdicts already written onto the item by `set_verdict()`."""
    item = load_round(layout, item_id)
    if item.get("status") != "open":
        # A replayed command_id is a no-op that must not touch the label file a second time; any other
        # resubmission of an already-closed round is refused outright rather than silently overwriting the
        # record the owner (or a labeller) already walked away from.
        stored_cid = (item.get("answer") or {}).get("command_id")
        if command_id and stored_cid == command_id:
            answered, total = progress(item)
            is_labelling = item.get("kind") == "labelling"
            label_file = labels_path(layout, item, item_id) if is_labelling else None
            return {"status": item.get("status"), "answered": answered, "total": total,
                   "agreement": agreement_stats(item) if is_labelling else None,
                   "label_file": str(label_file.relative_to(layout.root)) if label_file and label_file.exists() else None}
        raise RoundError(f"{item_id}: already {item.get('status')}")
    answered, total = progress(item)
    if not partial and answered < total:
        raise RoundError(f"{total - answered} of {total} images have no verdict; pass partial=true to override")
    is_labelling = item.get("kind") == "labelling"
    if is_labelling:
        ensure_label_order(layout, item_id)                      # lock in the order the owner actually saw
    stats = agreement_stats(item) if is_labelling else None
    label_file = None
    if is_labelling:
        label_file = labels_path(layout, item, item_id)
        payload = {
            "item_id": item_id, "submitted_at": plugin.now_iso(),
            "submitted_by": by, "session_seconds": session_seconds, "agreement": stats, "partial": answered < total,
            "items": [{"id": r.get("id"), "verdict": canonical_verdict(r.get("verdict")),
                       **({"verdict_legacy": recorded_verdict(r)} if recorded_verdict(r) else {}),
                       "note": r.get("note"),
                      "reason_tags": r.get("reason_tags"), "retest_of": r.get("retest_of"),
                      "group_key": r.get("group_key")} for r in (item.get("items") or [])],
        }
        plugin.atomic_write_text(label_file, _dump(payload))
    summary = f"{answered}/{total} images answered" + (" (partial)" if answered < total else "")
    if stats and stats["scored"]:
        summary += f"; test-retest agreement {stats['agreed']}/{stats['scored']}"
    answered_item = plugin.close_owned_item(layout, item_id, "SUBMIT", note=summary, by=by, command_id=command_id)
    return {"status": answered_item.get("status"), "answered": answered, "total": total, "agreement": stats,
           "label_file": str(label_file.relative_to(layout.root)) if label_file else None}


def migrate_legacy_verdicts(layout: plugin.Layout, dry_run: bool = False, by: str = "migration") -> dict:
    """Rewrite legacy per-image verdicts on OPEN round/labelling items to today's words, keeping the original
    word in `verdict_legacy`. Idempotent: a second run finds nothing. Answered items and label files are left as
    written - they are the record of what the owner actually said at the time. Returns {"items": {id: n_rows}}.

    This is this package's own data migration, run on demand (`supervisor review migrate-verdicts`, below), not
    the framework's generic plugin-`MIGRATIONS` mechanism (this manifest carries no `migrations` key) - a round's
    legacy words are a deliberate, owner-visible rewrite, not a config-shape move `supervisor migrate` can drive
    by itself."""
    changed: dict[str, int] = {}
    skipped_answered: dict[str, int] = {}
    for item in plugin.owned_items(layout):
        if item.get("kind") not in ROUND_KINDS:
            continue
        rows = legacy_verdict_rows(item)
        if not rows:
            continue
        if item.get("status") != "open":
            skipped_answered[item["id"]] = len(rows)
            continue
        changed[item["id"]] = len(rows)
        if dry_run:
            continue

        def fn(doc):
            for row in doc.get("items") or []:
                legacy = str(row.get("verdict") or "").upper()
                if legacy in LEGACY_VERDICTS:
                    row["verdict_legacy"] = row["verdict"]
                    row["verdict"] = LEGACY_VERDICTS[legacy]
            return doc
        plugin.update_owned_item(layout, item["id"], fn)
        plugin.log_event(layout, "review_verdicts_migrated", by, item=item["id"], rows=changed[item["id"]])
    return {"items": changed, "answered_left_as_written": skipped_answered, "dry_run": dry_run}


# ------------------------------------------------------------------ owner commands (registry.command)

def _review_set_verdict(layout, payload, actor):
    """One image's verdict in a Control Center review grid. Overwriting the same image with the same verdict is
    a no-op by construction, so this needs no command-id gate the way closing an item does."""
    set_verdict(layout, payload["item_id"], payload["image_id"], payload["verdict"],
                payload.get("note", ""), payload.get("reason_tags"), by=actor)
    return {"status": "ok"}


def _review_submit(layout, payload, actor):
    return submit(layout, payload["item_id"], bool(payload.get("partial")),
                  payload.get("session_seconds"), by=actor, command_id=payload.get("_id"))


# ------------------------------------------------------------------ CLI and doctor

HERE = Path(__file__).resolve().parent


def _cli():
    import typer
    review_app = typer.Typer(help="review rounds (per-image verdicts over inbox items of kind round / labelling)")

    @review_app.command("migrate-verdicts")
    def migrate_verdicts(dry_run: bool = typer.Option(False, help="report only; change nothing")):
        """Rewrite legacy per-image verdicts on OPEN rounds to today's words, keeping the original in
        verdict_legacy. Answered items and label files are left as written. Idempotent; run after `supervisor
        migrate`."""
        out = migrate_legacy_verdicts(plugin.Layout().ensure(), dry_run=dry_run, by="cli")
        for item_id, n in out["items"].items():
            typer.echo(f"{'would rewrite' if dry_run else 'rewrote'} {n} verdict(s) on {item_id}")
        for item_id, n in out["answered_left_as_written"].items():
            typer.echo(f"left {n} legacy verdict(s) on answered item {item_id} as written (historical record)")
        if not out["items"]:
            typer.echo("no open round carries a legacy verdict")
    return review_app


def check_legacy_verdicts(layout: plugin.Layout) -> plugin.Check:
    """doctor: an open round still carrying a legacy per-image verdict needs `supervisor review
    migrate-verdicts` (the grid reads it as today's word meanwhile)."""
    pending = {i["id"]: len(legacy_verdict_rows(i)) for i in plugin.owned_items(layout)
               if i.get("kind") in ROUND_KINDS and i.get("status") == "open" and legacy_verdict_rows(i)}
    if pending:
        return plugin.Check("review_rounds", False, "open rounds with legacy per-image verdicts (run `supervisor "
                            "review migrate-verdicts`): " + ", ".join(f"{k} ({n})" for k, n in pending.items()))
    return plugin.Check("review_rounds", True, "no legacy per-image verdicts on open rounds")


def setup(registry) -> None:
    """Rounds and labelling sessions over inbox items of kind `round` / `labelling` (owned kinds: only
    `submit()` closes them; a labelling card shows a count, never scores), the `/review` pages (the grid, its
    template and script), the two owner commands, the CLI and the doctor check.

    `external_edit` (its page is an upload form, not a grid) and a label destination tied to a project's own
    identity-folder layout stay with a project's own plugin (boundary D6, D7) - not registered here."""
    from . import pages
    cfg = review_rounds_config(registry.layout)
    registry.inbox_kind("round", "Rounds", page="/review/{id}", answerable=False, show_evidence=True,
                        score_keys=cfg["score_keys"])
    registry.inbox_kind("labelling", "Labelling sessions", page="/review/{id}", answerable=False,
                        show_evidence=False, score_keys=cfg["score_keys"])
    registry.command("review.set_verdict", _review_set_verdict)
    registry.command("review.submit", _review_submit)
    registry.cli(_cli(), "review")
    registry.doctor_check(check_legacy_verdicts)
    registry.template_dir(HERE / "templates")
    registry.static_dir(HERE / "static", "review")
    registry.ui_router(pages.router, "Review", "/review")
