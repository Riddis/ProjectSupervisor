"""Owned inbox items (docs/PLUGIN_API.md, Inbox items): a plugin that claimed a kind with
`registry.inbox_kind()` lists, reads, updates and closes items of that kind, and only that kind."""
from __future__ import annotations

import copy
from collections.abc import Callable

from .. import inbox as _inbox
from .. import store
from ..plugins import PluginError
from ..inbox import InboxError
from ..paths import Layout

_FIXED = ("id", "kind", "status", "answer", "answered_at")      # what an update may not change: identity and closure
_ABSENT = object()


def _caller(layout: Layout, what: str) -> tuple[str, dict[str, str]]:
    from .. import plugins
    reg = plugins.load(layout)
    who = plugins.calling_plugin(layout)
    if who is None:
        raise PluginError(f"{what} acts on a plugin's own inbox items: call it from that plugin's code")
    return who, reg.inbox_kind_owners


def _check(item_id: str, kind, who: str, owners: dict[str, str]) -> None:
    owner = owners.get(str(kind))
    if owner != who:
        whose = f"plugin {owner!r}'s" if owner else "a built-in or unclaimed kind"
        raise InboxError(f"{item_id}: a {kind!r} item is not plugin {who!r}'s to act on ({whose})")


def owned_items(layout: Layout, kind: str | None = None) -> list[dict]:
    """The inbox items of every kind the calling plugin claimed (or of `kind`, which it must have claimed), as
    read now, sorted by id. Raises InboxError for a kind that is not the caller's."""
    who, owners = _caller(layout, "owned_items()")
    kinds = {k for k, owner in owners.items() if owner == who}
    if kind is not None:
        _check("owned_items", kind, who, owners)
        kinds = {kind}
    return [item for item in _inbox.all_items(layout) if item.get("kind") in kinds]


def owned_item(layout: Layout, item_id: str) -> dict:
    """One inbox item of a kind the calling plugin claimed, as read now. Raises InboxError when there is no such
    item or it is not the caller's."""
    who, owners = _caller(layout, "owned_item()")
    item = _inbox.load(layout, item_id)
    _check(item_id, item.get("kind"), who, owners)
    return item


def update_owned_item(layout: Layout, item_id: str, fn: Callable[[dict], dict | None]) -> dict:
    """Read-modify-write one item of a kind the calling plugin claimed, under the inbox lock (state/.inbox.lock,
    the lock every inbox write takes): `fn(doc)` gets the item as read under the lock, changes it (or returns a new
    mapping) and may raise to abort without writing. It may not change the item's id, kind, status or answer:
    closing goes through `close_owned_item`. Returns the item as written. Raises InboxError when the item does not
    exist, is not the caller's, or `fn` changed a fixed field."""
    who, owners = _caller(layout, "update_owned_item()")
    path = _inbox.path_for(layout, item_id)
    if not path.exists():
        raise InboxError(f"no such inbox item {item_id}")

    def guarded(doc: dict) -> dict:
        _check(item_id, doc.get("kind"), who, owners)
        # A deep copy: `fn` gets the document itself and may change the answer dict in place (review 1.11.0 F2).
        before = {k: copy.deepcopy(doc[k]) if k in doc else _ABSENT for k in _FIXED}
        out = fn(doc)
        doc = out if isinstance(out, dict) else doc
        changed = [k for k in _FIXED if doc.get(k, _ABSENT) != before[k]]
        if changed:
            raise InboxError(f"{item_id}: an update may not change {', '.join(changed)} (close with close_owned_item)")
        return doc

    return store.update(path, guarded, lock_path=layout.state / ".inbox.lock")


def close_owned_item(layout: Layout, item_id: str, response: str, note: str = "", by: str = "owner",
                     command_id: str | None = None) -> dict:
    """Answer (close) one item of a kind the calling plugin claimed: the only way an item of a kind claimed with
    `answerable=False` is closed. Idempotent by `command_id`: a replay with the same id returns the item unchanged.
    Raises InboxError when the item does not exist, is not the caller's, or is already closed by another command."""
    who, owners = _caller(layout, "close_owned_item()")
    item = _inbox.load(layout, item_id)
    _check(item_id, item.get("kind"), who, owners)
    return _inbox.answer(layout, item_id, response, note=note, by=by, command_id=command_id, internal=True)
