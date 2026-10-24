"""Live Control Center pages: the fingerprint an open page polls, and the page plumbing that lets it refresh itself."""
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from supervisor import runtime
from supervisor.ui import live
from supervisor.ui.app import create_app
from tests.conftest import make_task


@pytest.fixture
def client(layout):
    return TestClient(create_app(layout, update_check=False), base_url="http://127.0.0.1")


def fresh(layout):
    return live.fingerprint(layout, max_age_s=0)


def bump(path):
    """Move a file's mtime forward without relying on the filesystem clock's resolution."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def test_fingerprint_moves_with_project_state(layout):
    before = fresh(layout)
    assert before == fresh(layout)                                   # stable while nothing changes
    make_task(layout, "T0-01")
    after_task = fresh(layout)
    assert after_task != before
    (layout.inbox / "INB-1.yaml").write_text("id: INB-1\n", encoding="utf-8")
    assert fresh(layout) != after_task
    after_inbox = fresh(layout)
    bump(layout.state / "gates.yaml")
    assert fresh(layout) != after_inbox
    (layout.config).mkdir(exist_ok=True)
    (layout.config / "routing.yaml").write_text("x: 1\n", encoding="utf-8")
    assert fresh(layout) != after_inbox


def test_heartbeat_alone_is_not_a_change_but_a_status_is(layout):
    runtime.heartbeat(layout, status="RUNNING")
    running = fresh(layout)
    runtime.heartbeat(layout, status="RUNNING")                      # a loop tick: new heartbeat, updated, epoch
    assert fresh(layout) == running
    runtime.heartbeat(layout, status="PAUSED")
    assert fresh(layout) != running


def test_noise_files_are_ignored(layout):
    before = fresh(layout)
    (layout.state / ".lock.owner").write_text("pid", encoding="utf-8")
    (layout.state / ".tasks.lock").write_text("", encoding="utf-8")
    (layout.runtime / "supervisor_ui_launch.log").write_text("x", encoding="utf-8")
    (layout.runtime / "ui_token").write_text("t", encoding="utf-8")
    (layout.tasks / ".T0-01.yaml.123.abc.tmp").write_text("half", encoding="utf-8")
    (layout.commands_applied / "C-1.json").write_text("{}", encoding="utf-8")
    assert fresh(layout) == before


def test_fingerprint_is_cached_briefly_for_many_tabs(layout):
    cached = live.fingerprint(layout, max_age_s=0)
    make_task(layout, "T0-02")
    assert live.fingerprint(layout, max_age_s=60) == cached           # served from the cache
    assert live.fingerprint(layout, max_age_s=0) != cached            # a forced scan sees the task


def test_version_endpoint(client, layout):
    r = client.get("/live/version")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body == {"v": fresh(layout), "boot": live.BOOT_ID}


def test_version_endpoint_refuses_foreign_hosts(layout):
    c = TestClient(create_app(layout, update_check=False), base_url="http://evil.example")
    assert c.get("/live/version").status_code == 400


@pytest.mark.parametrize("path", ["/overview", "/inbox", "/tasks", "/agents", "/roadmap", "/chat", "/log",
                                  "/owner-tasks", "/settings"])
def test_every_page_is_stamped_and_loads_the_live_script(client, layout, path):
    client.get(path)                                                 # first render may create files (caches, dirs)
    before = fresh(layout)
    r = client.get(path)
    assert r.status_code == 200
    stamped = re.search(r'<meta name="live-version" content="([0-9a-f]*)">', r.text)
    assert stamped and stamped.group(1) == before
    # Rendering a page must not move the fingerprint, or an open page would refresh itself every poll.
    assert fresh(layout) == before
    assert f'<meta name="live-boot" content="{live.BOOT_ID}">' in r.text
    assert '<script src="/static/idiomorph.min.js" defer></script>' in r.text
    assert '<script src="/static/live.js" defer></script>' in r.text
    assert 'id="live-status"' in r.text and 'id="main"' in r.text


def test_page_is_stamped_before_its_data_is_read(client, layout, monkeypatch):
    """A change landing while a page renders must leave the page with the older stamp, so it refreshes again."""
    from supervisor.ui.routes import tasks as tasks_routes
    stamp_before = fresh(layout)
    real = tasks_routes.tasks_mod.load_all

    def load_all_then_change(lay):
        result = real(lay)
        if not (layout.tasks / "T9-99.yaml").exists():
            make_task(layout, "T9-99")                               # lands after the stamp, before the response
        return result

    monkeypatch.setattr(tasks_routes.tasks_mod, "load_all", load_all_then_change)
    r = client.get("/tasks")
    assert f'<meta name="live-version" content="{stamp_before}">' in r.text
    assert fresh(layout) != stamp_before


def test_static_assets_and_kept_regions(client):
    js = client.get("/static/live.js")
    assert js.status_code == 200 and "/live/version" in js.text and "data-live-keep" in js.text and "data-live-off" in js.text
    morph = client.get("/static/idiomorph.min.js")
    assert morph.status_code == 200 and "Idiomorph" in morph.text
    assert 'id="ctrl-result" role="status" aria-live="polite" data-live-keep' in client.get("/overview").text


def test_streams_and_assets_take_no_stamp():
    assert live.skip_stamp("/log/stream") and live.skip_stamp("/agents/T0-01.a1/stream")
    assert live.skip_stamp("/static/live.js") and live.skip_stamp("/live/version") and live.skip_stamp("/thumb/abc")
    assert not live.skip_stamp("/inbox") and not live.skip_stamp("/tasks/T0-01")


def test_live_morph_is_handed_children_not_the_wrapper_element():
    """Idiomorph treats an Element passed as new content as THE content, even with morphStyle "innerHTML": handing it
    the parsed <main>/<nav> nested a second wrapper inside the live one on every refresh (independent review of
    1.6.0, finding 3; reproduced and fixed in Chromium: 2 <main> after a refresh before, 1 after). The children go
    in as an array."""
    import re
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "supervisor" / "ui" / "static" / "live.js").read_text(encoding="utf-8")
    calls = re.findall(r"Idiomorph\.morph\(([^,]+),\s*([^,]+),", js)
    assert calls, "live.js no longer morphs"
    for target, content in calls:
        assert "childNodes" in content or content.strip() == "nextChildren", (target, content)
    assert "Array.prototype.slice.call(next.childNodes)" in js
