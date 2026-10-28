"""1.12.1, F-C: the framework's state writers keep each file's line endings, and write new files with LF. They used to
write text with the platform's newline, so on Windows every rewrite turned an LF file into CRLF, and a one-line change
showed in git as the whole file changed (a project's task seed: 5222 changed lines, 26 of them real). A file that is
CRLF stays CRLF; a file that is LF stays LF; a new file is LF, as git stores it. Bytes are compared, so these tests
mean the same on Windows and Linux."""
from __future__ import annotations

import pytest

from supervisor import conversations, decisions, render, store, tasks as tasks_mod, testing
from supervisor.paths import Layout


@pytest.fixture
def layout(tmp_path):
    testing.reset()
    lay = testing.project(tmp_path)
    yield Layout(lay.root)
    testing.reset()


def crlf(path) -> bool:
    return b"\r\n" in path.read_bytes()


def lf_only(path) -> bool:
    data = path.read_bytes()
    return b"\n" in data and b"\r\n" not in data


def as_crlf(path):
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))


def as_lf(path):
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def test_a_new_store_document_is_lf(tmp_path):
    p = tmp_path / "doc.yaml"
    store.write_new(p, {"a": 1, "b": [1, 2]})
    assert lf_only(p)


@pytest.mark.parametrize("ending", ["lf", "crlf"])
def test_a_store_update_keeps_the_files_line_endings(tmp_path, ending):
    p = tmp_path / "doc.yaml"
    store.write_new(p, {"a": 1, "b": [1, 2]})
    if ending == "crlf":
        as_crlf(p)
    store.update(p, lambda d: d.update(a=2))
    assert (crlf(p) if ending == "crlf" else lf_only(p))
    assert store.read(p)["a"] == 2


@pytest.mark.parametrize("ending", ["lf", "crlf"])
def test_a_projection_keeps_the_files_line_endings(layout, ending):
    path = render.render_tasks(layout)
    (as_crlf if ending == "crlf" else as_lf)(path)
    render.render_tasks(layout)
    assert (crlf(path) if ending == "crlf" else lf_only(path))


def test_a_new_projection_is_lf(layout):
    path = layout.root / "TASKS.md"
    path.unlink(missing_ok=True)
    assert lf_only(render.render_tasks(layout))


def test_a_new_decision_record_is_lf(layout):
    created = decisions.create(layout, "Use the smaller model", "Because it fits.")
    path = next(layout.decisions.glob(f"{created['id']}-*.md"))
    assert lf_only(path)


@pytest.mark.parametrize("ending", ["lf", "crlf", "new"])
def test_the_event_log_is_appended_in_its_own_line_endings(layout, ending):
    log = layout.log
    if ending == "new":
        log.unlink(missing_ok=True)
    else:
        log.write_bytes(b'{"event": "first"}' + (b"\r\n" if ending == "crlf" else b"\n"))
    tasks_mod.log(layout, "second", "test")
    tasks_mod.log(layout, "third", "test")
    data = log.read_bytes()
    assert data.count(b"\n") == (2 if ending == "new" else 3)
    assert (data.count(b"\r\n") == 3) if ending == "crlf" else (b"\r\n" not in data)


@pytest.mark.parametrize("ending", ["lf", "crlf"])
def test_a_conversation_keeps_its_line_endings(tmp_path, ending):
    p = tmp_path / "thread.jsonl"
    conversations._write_lines(p, [{"a": 1}, {"b": 2}])
    if ending == "crlf":
        as_crlf(p)
    conversations._write_lines(p, [{"a": 1}, {"b": 2}, {"c": 3}])
    assert (crlf(p) if ending == "crlf" else lf_only(p))
    assert conversations._read_lines(p) == [{"a": 1}, {"b": 2}, {"c": 3}]
