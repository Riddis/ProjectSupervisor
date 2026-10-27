"""Owned inbox items through supervisor.ext (phase 2 step 5): an extension that claimed a kind with
`registry.inbox_kind()` lists, reads, updates and closes items of that kind, and only that kind. Updates are
read-modify-write under state/.inbox.lock, so two processes updating one item both land; another extension's update
or close is refused by the owner check; an update cannot change what the item is (id, kind) or close it (status,
answer); a close is idempotent by command id."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from supervisor import ext, extensions, inbox, testing
from supervisor.paths import Layout

REPO = Path(__file__).resolve().parents[1]

OWNER = '''
import time
from supervisor import ext

def setup(registry):
    registry.inbox_kind("grid", "Grids", page="/grid/{id}", answerable=False)

def items(layout, kind=None):
    return ext.owned_items(layout, kind)

def get(layout, item_id):
    return ext.owned_item(layout, item_id)

def set_row(layout, item_id, row_id, value, delay=0.0):
    def fn(doc):
        rows = doc["items"]
        time.sleep(delay)                     # widen the read-modify-write window
        for row in rows:
            if row["id"] == row_id:
                row["value"] = value
        return doc
    return ext.update_owned_item(layout, item_id, fn)

def tamper(layout, item_id, field, value):
    def fn(doc):
        doc[field] = value
        return doc
    return ext.update_owned_item(layout, item_id, fn)

def close(layout, item_id, command_id=None):
    return ext.close_owned_item(layout, item_id, "SUBMIT", note="2/2", by="owner", command_id=command_id)
'''

OTHER = '''
from supervisor import ext

def setup(registry):
    registry.inbox_kind("other_kind", "Others")

def set_row(layout, item_id, row_id, value):
    return ext.update_owned_item(layout, item_id, lambda doc: doc)

def close(layout, item_id):
    return ext.close_owned_item(layout, item_id, "SUBMIT")

def get(layout, item_id):
    return ext.owned_item(layout, item_id)
'''


def configure(root: Path) -> Layout:
    lay = testing.add_extensions(Layout(root), {"owner_ext": OWNER, "other_ext": OTHER})
    assert extensions.load(lay).errors == []
    return lay


@pytest.fixture
def lay(layout):
    lay = configure(layout.root)
    yield lay
    extensions.reset()


def grid(lay, rows=("a", "b")):
    return inbox.create(lay, for_role="owner", subject="grid", summary="pick", kind="grid",
                        extra={"items": [{"id": r} for r in rows]})["id"]


def mod(lay, name):
    return extensions._import(lay, name)


def test_an_extension_lists_and_reads_only_the_kinds_it_owns(lay):
    g = grid(lay)
    q = inbox.create(lay, for_role="owner", subject="q", summary="?", kind="question")["id"]
    assert [i["id"] for i in mod(lay, "owner_ext").items(lay)] == [g]
    assert [i["id"] for i in mod(lay, "owner_ext").items(lay, "grid")] == [g]
    assert mod(lay, "owner_ext").get(lay, g)["kind"] == "grid"
    with pytest.raises(ext.InboxError, match="owner_ext"):
        mod(lay, "owner_ext").get(lay, q)                      # a built-in kind is not the extension's
    with pytest.raises(ext.InboxError, match="other_ext"):
        mod(lay, "other_ext").get(lay, g)
    with pytest.raises(ext.InboxError):
        mod(lay, "owner_ext").items(lay, "other_kind")
    with pytest.raises(ext.InboxError, match="no such"):
        mod(lay, "owner_ext").get(lay, "H-999")


def test_two_processes_updating_one_item_both_land(lay):
    """Read-modify-write under state/.inbox.lock: without the lock the slower writer would overwrite the other's row."""
    g = grid(lay)
    code = ("import sys; from supervisor import extensions; from supervisor.paths import Layout; "
            "lay = Layout(sys.argv[1]); extensions.load(lay); "
            "extensions._import(lay, 'owner_ext').set_row(lay, sys.argv[2], sys.argv[3], sys.argv[4], 0.6)")
    env = {**os.environ, "PYTHONPATH": str(REPO), "SUPERVISOR_ROOT": str(lay.root)}
    procs = [subprocess.Popen([sys.executable, "-c", code, str(lay.root), g, row, f"v-{row}"], cwd=str(lay.root),
                              env=env, stderr=subprocess.PIPE, text=True, encoding="utf-8") for row in ("a", "b")]
    for p in procs:
        assert p.wait(timeout=60) == 0, p.stderr.read()
    rows = {r["id"]: r.get("value") for r in inbox.load(lay, g)["items"]}
    assert rows == {"a": "v-a", "b": "v-b"}


def test_another_extension_cannot_update_or_close_the_item(lay):
    g = grid(lay)
    before = inbox.load(lay, g)
    with pytest.raises(ext.InboxError, match="owner_ext"):
        mod(lay, "other_ext").set_row(lay, g, "a", "x")
    with pytest.raises(ext.InboxError, match="owner_ext"):
        mod(lay, "other_ext").close(lay, g)
    assert inbox.load(lay, g) == before


@pytest.mark.parametrize("field,value", [("kind", "question"), ("id", "H-777"), ("status", "answered"),
                                         ("answer", {"response": "SUBMIT"})])
def test_an_update_cannot_change_what_the_item_is_or_close_it(lay, field, value):
    g = grid(lay)
    before = inbox.load(lay, g)
    with pytest.raises(ext.InboxError, match=field):
        mod(lay, "owner_ext").tamper(lay, g, field, value)
    assert inbox.load(lay, g) == before


def test_closing_is_the_owners_and_idempotent_by_command_id(lay):
    g = grid(lay)
    with pytest.raises(ext.InboxError, match="finalised by its extension"):
        inbox.answer(lay, g, "SUBMIT")                         # the generic path still may not close it
    first = mod(lay, "owner_ext").close(lay, g, command_id="c-1")
    again = mod(lay, "owner_ext").close(lay, g, command_id="c-1")
    assert first["status"] == again["status"] == "answered" and again["answer"]["command_id"] == "c-1"
    answers = [json.loads(line) for line in lay.log.read_text(encoding="utf-8").splitlines()
               if json.loads(line)["event"] == "inbox_answer"]
    assert len(answers) == 1
    with pytest.raises(ext.InboxError, match="already answered"):
        mod(lay, "owner_ext").close(lay, g, command_id="c-2")


def test_owned_item_operations_are_for_extension_code(lay):
    g = grid(lay)
    with pytest.raises(ext.ExtensionError, match="extension's code"):
        ext.owned_item(lay, g)
    with pytest.raises(ext.ExtensionError, match="extension's code"):
        ext.update_owned_item(lay, g, lambda doc: doc)
