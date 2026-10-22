"""`supervisor.launcher`: the restart-on-reload wrapper the Control Center's Start button launches the loop through."""
from __future__ import annotations

import subprocess


from supervisor import launcher, loop, template, updates
from supervisor.paths import Layout


def test_wrapper_relaunches_on_exit_3_and_stops_on_other_codes(monkeypatch):
    codes = iter([loop.EXIT_RELOAD, loop.EXIT_RELOAD, 0])
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda argv, **kw: calls.append(argv) or next(codes))
    assert launcher.run(["--sleep", "1"], python="py", delay_s=0) == 0
    assert len(calls) == 3 and all(c == ["py", "-B", "-m", "supervisor", "run", "--sleep", "1"] for c in calls)
    monkeypatch.setattr(subprocess, "call", lambda argv, **kw: calls.append(argv) or 1)
    assert launcher.run(delay_s=0) == 1 and len(calls) == 4
    assert launcher.EXIT_RELOAD == loop.EXIT_RELOAD == updates.RESTART_EXIT_CODE


def test_control_center_start_launches_through_the_wrapper(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from supervisor.ui.app import create_app
    target = tmp_path / "demo"
    template.stamp(target, name="Demo")
    layout = Layout(target).ensure()
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: launched.append((argv, kw)))
    client = TestClient(create_app(layout), base_url="http://127.0.0.1")
    client.cookies.set("supervisor_csrf", "t0ken")
    r = client.post("/overview/start", data={"csrf_token": "t0ken"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "starting" in r.text
    (argv, kw), = launched
    assert argv[1:] == ["-m", "supervisor.launcher"] and kw["cwd"] == str(target)
    assert kw["env"]["SUPERVISOR_ROOT"] == str(target)
