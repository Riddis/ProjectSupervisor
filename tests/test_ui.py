"""Control Center tests: FastAPI TestClient against the fixture state store.

httpx2/Starlette (this environment's versions) send Host: testserver by default, which our Host/Origin check must
reject just like any other spoofed host, so every client here is built with base_url="http://127.0.0.1" to match
what a real loopback browser would send.
"""
from __future__ import annotations

import hashlib
import re

import pytest
from fastapi.testclient import TestClient

from supervisor import commands, store
from supervisor import jobs as jobs_mod
from supervisor.locks import FileLock
from supervisor.ui.app import create_app
from supervisor.ui.auth import CSRF_COOKIE
from tests.conftest import make_task

CSRF = "test-csrf-value"


@pytest.fixture
def app(layout):
    return create_app(layout)


@pytest.fixture
def client(app):
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def auth_client(layout, client):
    client.cookies.set(CSRF_COOKIE, CSRF)
    return client


def post(client, url, **data):
    return client.post(url, data={"csrf_token": CSRF, **data}, follow_redirects=False)


# --------------------------------------------------------------------- auth


def test_local_access_needs_no_login(layout, client):
    r = client.get("/overview")
    assert r.status_code == 200
    assert client.cookies.get(CSRF_COOKIE)
    assert not (layout.runtime / "ui_token").exists()
    assert 'name="csrf_token" value="' + client.cookies.get(CSRF_COOKIE) in r.text
    assert 'aria-current="page"' in r.text


def test_page_and_styles_cannot_reuse_a_stale_cached_response(client, monkeypatch, tmp_path):
    from supervisor.ui import assets

    css_file = tmp_path / "style.css"
    css_file.write_bytes(b".metric-body { display: block; }")
    monkeypatch.setattr(assets, "STYLESHEET_PATH", css_file)

    page = client.get("/overview")
    assert page.headers["cache-control"] == "no-store"
    style_url = re.search(r'<link rel="stylesheet" href="([^"]+)"', page.text)[1]
    assert hashlib.sha256(css_file.read_bytes()).hexdigest()[:16] in style_url
    style = client.get(style_url, headers={"If-Modified-Since": "Wed, 21 Oct 2099 07:28:00 GMT",
                                          "If-None-Match": '"previous-stylesheet"'})
    assert style.status_code == 200
    assert style.content == css_file.read_bytes()
    assert style.headers["cache-control"] == "no-store"

    # Simulate another edit within the same second and with the same file size.
    css_file.write_bytes(b".metric-body { display: grid;  }")
    next_page = client.get("/overview")
    next_url = re.search(r'<link rel="stylesheet" href="([^"]+)"', next_page.text)[1]
    assert next_url != style_url
    assert client.get(next_url).content == css_file.read_bytes()
    assert client.get(style_url).status_code == 404


def test_bad_host_rejected(client):
    assert client.get("/overview", headers={"Host": "evil.example.com"}).status_code == 400


@pytest.mark.parametrize("method", ["get", "post"])
def test_old_login_redirects_without_credentials(client, method):
    r = getattr(client, method)("/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/overview"


def test_first_visit_form_can_submit(layout, client):
    make_task(layout, "T0-01")
    client.get("/tasks/T0-01")
    r = client.post("/tasks/T0-01/priority", data={"csrf_token": client.cookies.get(CSRF_COOKIE), "priority": 3}, follow_redirects=False)
    assert r.status_code == 303
    from supervisor import tasks
    assert tasks.load(layout, "T0-01").priority == 3


def test_foreign_origin_rejected(client):
    r = client.post("/overview/pause", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 400


def test_post_without_csrf_cookie_rejected(auth_client):
    r = auth_client.post("/tasks/T0-01/note", data={"note": "x", "csrf_token": "something-else"})
    assert r.status_code == 403


# ------------------------------------------------------------- every panel


@pytest.mark.parametrize("path", ["/overview", "/roadmap", "/tasks", "/agents", "/chat", "/log", "/owner-tasks", "/settings"])
def test_panel_renders_against_fixture_state(auth_client, path):
    r = auth_client.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_overview_resolves_busy_attempt_and_renders_real_gates(layout, auth_client):
    from supervisor import runtime
    from supervisor.ui.routes.overview import overview_context

    make_task(layout, "T0-05")
    jobs_mod.prepare(layout, "T0-05", "T0-05.a1", "process", {})
    jobs_mod.set_phase(layout, "T0-05.a1", "SUBMITTED", submitted="now")
    runtime.write(layout, busy="T0-05.a1", started="unavailable")
    context = overview_context(layout)
    assert context["busy_task"].id == "T0-05"
    assert context["uptime_seconds"] is None
    assert context["nearby_gates"][0]["id"] == "G0"
    assert context["nearby_gates"][0]["status"] == "OPEN"
    response = auth_client.get("/overview")
    assert response.status_code == 200
    assert 'class="gate-open"' in response.text


def test_task_detail_renders_a_fixture_task_with_attempts(layout, auth_client):
    make_task(layout, "T0-01")

    def fn(doc):
        doc.setdefault("attempts", []).append({"id": "T0-01.a1", "resource": "claude_cli",
                                               "at": "2026-01-01T00:00:00+00:00", "note": "dispatch", "outcome": "ok"})
    from supervisor import tasks as tasks_mod
    tasks_mod._update(layout, "T0-01", fn)

    r = auth_client.get("/tasks/T0-01")
    assert r.status_code == 200
    assert "T0-01.a1" in r.text
    assert "claude_cli" in r.text


def test_no_route_accepts_a_filesystem_path_from_the_client(layout, auth_client):
    # path traversal through the one route that takes an opaque "key" must not resolve outside the cache dir
    r = auth_client.get("/thumb/" + "..%2F..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (404, 400)
    r2 = auth_client.get("/thumb/does-not-exist.jpg")
    assert r2.status_code == 404
    # the open-run-dir action takes only an attempt id, never a path
    make_task(layout, "T0-02")
    r3 = post(auth_client, "/tasks/T0-02/attempts/not-a-real-attempt/open-run-dir")
    assert r3.status_code == 404


# --------------------------------------------------------------- commands / pending


def test_action_while_loop_runs_is_pending_until_applied(layout, auth_client):
    make_task(layout, "T0-03")
    with FileLock(layout.lock, owner="fake supervisor"):
        r = post(auth_client, "/tasks/T0-03/priority", priority="5")
        assert r.status_code == 303
        assert "flash=pending" in r.headers["location"]
        pending_files = list(layout.commands.glob("*.json"))
        assert len(pending_files) == 1
    # not applied yet (lock was held)
    from supervisor import tasks as tasks_mod
    assert tasks_mod.load(layout, "T0-03").priority == 0
    # now apply it the way the loop would
    commands.apply_all(layout)
    assert tasks_mod.load(layout, "T0-03").priority == 5


def test_action_applies_immediately_when_no_loop_holds_the_lock(layout, auth_client):
    make_task(layout, "T0-04")
    r = post(auth_client, "/tasks/T0-04/priority", priority="7")
    assert r.status_code == 303
    assert "flash=applied" in r.headers["location"]
    from supervisor import tasks as tasks_mod
    assert tasks_mod.load(layout, "T0-04").priority == 7


def test_pause_button_results_in_supervisor_pause_within_one_loop_iteration(layout, auth_client):
    from supervisor.loop import Supervisor
    sup = Supervisor(layout, sleep_s=0)
    sup._lock = FileLock(layout.lock, owner="test supervisor").acquire()
    try:
        assert sup.once() == "RUN"
        r = post(auth_client, "/overview/pause")
        assert r.status_code == 303
        decision = sup.once()
        assert decision == "PAUSE"
        from supervisor import runtime
        assert runtime.read(layout)["status"] == "PAUSED"
    finally:
        sup._lock.release()


def test_shutdown_button_drains_a_live_supervisor_and_offers_cancel(layout, auth_client, monkeypatch):
    """Shut down PC when idle" sits in the control row; with a live supervisor it arms a drain, the row
    then shows "Cancel shutdown", and cancelling resumes the loop."""
    from supervisor import runtime, shutdown as shutdown_mod
    from supervisor.loop import Supervisor
    calls = []
    monkeypatch.setattr(shutdown_mod, "run_command", lambda argv: (calls.append(argv), (0, ""))[1])
    page = auth_client.get("/overview").text
    assert 'action="/overview/shutdown"' in page and "Shut down PC when idle" in page
    sup = Supervisor(layout, sleep_s=0)
    sup._lock = FileLock(layout.lock, owner="test supervisor").acquire()
    try:
        assert sup.once() == "RUN"
        r = post(auth_client, "/overview/shutdown")
        assert r.status_code == 303 and "flash=pending" in r.headers["location"]      # queued for the running loop
        assert sup.once() == "STOP"                                   # nothing was running: drain completes at once
        rt = runtime.read(layout)
        assert rt["shutdown_issued"] is True and calls[0] == shutdown_mod.shutdown_argv()
    finally:
        sup._lock.release()
    page = auth_client.get("/overview").text
    assert "Cancel shutdown" in page and 'action="/overview/shutdown-cancel"' in page and "PC shutting down" in page
    r = post(auth_client, "/overview/shutdown-cancel")
    assert r.status_code == 303 and "flash=applied" in r.headers["location"]
    assert runtime.read(layout)["shutdown_issued"] is False and calls[-1] == shutdown_mod.cancel_argv()
    assert "Shut down PC when idle" in auth_client.get("/overview").text


def test_shutdown_button_with_running_work_shows_the_drain_state(layout, auth_client, monkeypatch):
    from supervisor import runtime, shutdown as shutdown_mod
    monkeypatch.setattr(shutdown_mod, "run_command", lambda argv: (0, ""))
    runtime.heartbeat(layout, "RUNNING")
    jobs_mod.write(layout, {"attempt_id": "T.a1", "task_id": "T", "adapter": "claude_cli", "phase": "RUNNING",
                            "spec": {"kind": "delivery"}, "handle": {}, "created": "2026-01-01T00:00:00+00:00"})
    r = post(auth_client, "/overview/shutdown")
    assert r.status_code == 303
    assert runtime.read(layout)["decision"] == "DRAIN" and runtime.read(layout)["shutdown_on_drain"] is True
    runtime.heartbeat(layout, "DRAINING")
    page = auth_client.get("/overview").text
    assert "Draining" in page and "1 job running" in page and "PC shuts down when they finish" in page
    assert "Cancel shutdown" in page
    r = post(auth_client, "/overview/shutdown-cancel")
    assert r.status_code == 303 and runtime.read(layout)["decision"] == "RUN"


def test_shutdown_button_with_the_supervisor_offline_powers_off_after_the_grace_period(layout, auth_client, monkeypatch):
    from supervisor import runtime, shutdown as shutdown_mod
    calls = []
    monkeypatch.setattr(shutdown_mod, "run_command", lambda argv: (calls.append(argv), (0, ""))[1])
    r = post(auth_client, "/overview/shutdown")
    assert r.status_code == 303 and "flash=applied" in r.headers["location"]
    assert calls == [shutdown_mod.shutdown_argv()] and runtime.read(layout)["shutdown_issued"] is True


# --------------------------------------------------------------------- agents


def test_agents_list_and_terminate(layout, auth_client):
    make_task(layout, "T0-05")
    jobs_mod.prepare(layout, "T0-05", "T0-05.a1", "process", {"run_dir": str(layout.runs / "T0-05" / "T0-05.a1")})
    jobs_mod.set_phase(layout, "T0-05.a1", "SUBMITTED", handle={"pid": 999999, "create_time": 0}, submitted="now")
    r = auth_client.get("/agents")
    assert r.status_code == 200
    assert "T0-05.a1" in r.text
    r2 = post(auth_client, "/agents/T0-05.a1/terminate")
    assert r2.status_code == 303
    assert jobs_mod.read(layout, "T0-05.a1")["phase"] == "FAILED"


def test_agent_stream_delivers_appended_lines(layout, auth_client):
    run_dir = layout.runs / "T0-06" / "T0-06.a1"
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout = run_dir / "stdout.jsonl"
    stdout.write_text("")
    jobs_mod.prepare(layout, "T0-06", "T0-06.a1", "process", {"run_dir": str(run_dir)})
    jobs_mod.set_phase(layout, "T0-06.a1", "DONE", result="COMPLETE", finished="now")

    with auth_client.stream("GET", "/agents/T0-06.a1/stream") as resp:
        assert resp.status_code == 200
        stdout.write_text('{"type": "assistant", "text": "hello"}\n')
        lines = []
        for chunk in resp.iter_lines():
            lines.append(chunk)
            if any("hello" in l for l in lines):
                break
            if len(lines) > 50:
                break
        assert any("hello" in l for l in lines)


# --------------------------------------------------------------------- owner tasks


def test_start_supervisor_targets_this_layout_not_the_real_repo(monkeypatch, layout, auth_client):
    """Never actually spawns a process in the test suite (it would be orphaned once the test ends) — just checks the
    launch is wired to the fixture layout, not whatever SUPERVISOR_ROOT defaults to on this machine."""
    calls = []

    def fake_popen(argv, **kw):
        calls.append((argv, kw))

        class _P:
            pid = 12345
        return _P()

    import subprocess as subprocess_mod
    monkeypatch.setattr(subprocess_mod, "Popen", fake_popen)
    r = post(auth_client, "/overview/start")
    assert r.status_code == 303
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv[-3:] == ["-m", "supervisor", "run"]
    assert kw["cwd"] == str(layout.root)
    assert kw["env"]["SUPERVISOR_ROOT"] == str(layout.root)


def test_owner_tasks_done_and_reopen(layout, auth_client):
    layout.owner_tasks.write_text(store.dump({"owner_tasks": [{"id": "OT-01", "title": "x", "status": "open",
                                                               "blocks": [], "priority": "low", "guidance": "g"}]}),
                                  encoding="utf-8")
    r = auth_client.get("/owner-tasks")
    assert "OT-01" in r.text
    r2 = post(auth_client, "/owner-tasks/OT-01/done")
    assert r2.status_code == 303
    from supervisor import owner_tasks as owner_tasks_mod
    rows = owner_tasks_mod.load(layout)
    assert rows[0]["status"] == "done"


# --------------------------------------------------------------------- settings


def test_settings_validated_edit(layout, auth_client, tmp_path):
    config_dir = layout.config
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "routing.yaml").write_text(
        "capacity:\n  claude: {reserve_percent: 3}\n  codex: {reserve_percent: 3}\nlimits: {max_agent_jobs: 1, max_gpu_jobs: 1}\nroles: {}\n",
        encoding="utf-8")
    r = auth_client.get("/settings")
    assert r.status_code == 200
    r2 = post(auth_client, "/settings/routing", field="limits.max_agent_jobs", value="2")
    assert r2.status_code == 303
    from supervisor import settings as settings_mod
    assert settings_mod.view(layout)["limits.max_agent_jobs"] == 2

    r3 = post(auth_client, "/settings/routing", field="limits.max_agent_jobs", value="999")
    assert r3.status_code == 303
    assert "flash=error" in r3.headers["location"]


# --------------------------------------------------------------------- chat


def test_chat_create_ask_and_convert_round_trip(layout, auth_client):
    r = post(auth_client, "/chat", role="senior_pm")
    assert r.status_code == 303
    from supervisor import conversations as conversations_mod
    [thread] = conversations_mod.list_threads(layout)
    thread_id = thread["id"]

    r2 = auth_client.get(f"/chat/{thread_id}")
    assert r2.status_code == 200 and "senior_pm" in r2.text

    r3 = post(auth_client, f"/chat/{thread_id}/ask", text="why is T0-02 blocked?")
    assert r3.status_code == 303
    turns = conversations_mod.load(layout, thread_id)["turns"]
    assert turns[0]["text"] == "why is T0-02 blocked?" and turns[1]["status"] == "pending"

    conversations_mod._finish_turn(layout, thread_id, 2, "done", "nothing blocks it; dispatch is just pending")
    r4 = auth_client.get(f"/chat/{thread_id}")
    assert "nothing blocks it" in r4.text

    r5 = post(auth_client, f"/chat/{thread_id}/convert", turn="2", into="owner_task", title="Follow up on T0-02")
    assert r5.status_code == 303
    from supervisor import owner_tasks as owner_tasks_mod
    rows = owner_tasks_mod.load(layout)
    assert any(row["title"] == "Follow up on T0-02" for row in rows)


def test_chat_ask_rejects_empty_question(layout, auth_client):
    post(auth_client, "/chat", role="senior_pm")
    from supervisor import conversations as conversations_mod
    [thread] = conversations_mod.list_threads(layout)
    r = post(auth_client, f"/chat/{thread['id']}/ask", text="   ")
    assert r.status_code == 400


def test_chat_unknown_thread_is_404(auth_client):
    r = auth_client.get("/chat/does-not-exist")
    assert r.status_code == 404
