"""Behaviour of the ComfyUI lane, against a temporary project. Ported from a project-local extension's tests, onto
supervisor.plugin and supervisor.testing only - no framework internal: every test here calls this package's own
functions directly.

Three tests from the original suite are not ported at all: `test_overview_panels_show_both_cards_by_configured_role`,
`test_overview_secondary_panel_without_a_second_card` and `test_gpu_roles_read_from_configured_device_index` proved
a project's own multi-device GPU topology, a project internal this package cannot import. This package's own
`check_gpu`/`gpu_panel` read the single-value probe `supervisor.plugin.gpu` instead; `test_doctor.py` here covers
them."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import requests

from supervisor import plugin, testing

import supervisor_plugin_comfy as comfy


def ctask(layout, tid="CF", vram_mb=0, workflow=None):
    testing.make_task(layout, tid, job={"adapter": "comfy", "vram_mb": vram_mb,
                                        "workflow": workflow or {"1": {"class_type": "noop"}}},
                      cost_category="local", artifacts=[])


class FakeResponse:
    def __init__(self, payload=None, status_code=200, content=b"", text=""):
        self._payload = payload
        self.status_code = status_code
        self.content = content
        self.text = text or json.dumps(payload or {})

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"http {self.status_code}")


@pytest.fixture
def ad():
    return comfy.ADAPTER


def test_vram_preflight_refuses_below_floor(layout, ad, monkeypatch):
    ctask(layout, vram_mb=4000)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda: 1000)
    with pytest.raises(plugin.CapacityDeferred):
        ad.submit(spec)


def test_submit_persists_prompt_id_before_polling(layout, ad, monkeypatch):
    ctask(layout, vram_mb=0)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    monkeypatch.setattr(ad, "_get", lambda base, path, **kw: FakeResponse({"ok": True}))
    monkeypatch.setattr(ad, "_post", lambda base, path, payload, **kw: FakeResponse({"prompt_id": "abc123"}))
    handle = ad.submit(spec)
    assert handle["prompt_id"] == "abc123"
    persisted = json.loads((Path(spec["run_dir"]) / "prompt_id.json").read_text())
    assert persisted["prompt_id"] == "abc123"


def test_recover_resumes_without_resubmitting(layout, ad):
    ctask(layout, vram_mb=0)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    plugin.atomic_json(Path(spec["run_dir"]) / "prompt_id.json",
                       {"kind": "comfy", "prompt_id": "resumed-id", "client_id": spec["client_id"]})
    handle = ad.recover(spec)
    assert handle is not None and handle["prompt_id"] == "resumed-id"


def test_collect_still_running_returns_none(layout, ad, monkeypatch):
    ctask(layout, vram_mb=0)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    monkeypatch.setattr(ad, "_get", lambda base, path, **kw: FakeResponse({}))
    monkeypatch.setattr(ad, "_poll_progress", lambda spec: None)
    result = ad.collect(spec, {"prompt_id": "abc123", "client_id": "c1"})
    assert result is None


def test_collect_completes_and_downloads_outputs(layout, ad, monkeypatch):
    ctask(layout, vram_mb=0)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    monkeypatch.setattr(ad, "_poll_progress", lambda spec: None)
    history = {"abc123": {"status": {"completed": True, "status_str": "success"},
                           "outputs": {"9": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}}}}

    def fake_get(base, path, **kw):
        if path.startswith("/history"):
            return FakeResponse(history)
        if path == "/view":
            return FakeResponse(content=b"PNGDATA")
        raise AssertionError(path)
    monkeypatch.setattr(ad, "_get", fake_get)
    result = ad.collect(spec, {"prompt_id": "abc123", "client_id": "c1"})
    assert result is not None and result.status == "COMPLETE"
    assert result.artifacts and (layout.root / result.artifacts[0]).read_bytes() == b"PNGDATA"


def test_collect_error_status_fails(layout, ad, monkeypatch):
    ctask(layout, vram_mb=0)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    monkeypatch.setattr(ad, "_poll_progress", lambda spec: None)
    history = {"abc123": {"status": {"completed": True, "status_str": "error", "messages": ["boom"]}}}
    monkeypatch.setattr(ad, "_get", lambda base, path, **kw: FakeResponse(history))
    result = ad.collect(spec, {"prompt_id": "abc123", "client_id": "c1"})
    assert result is not None and result.status == "FAILED"


def _history(outputs):
    return {"abc123": {"status": {"completed": True, "status_str": "success"}, "outputs": outputs}}


def _serve(ad, monkeypatch, history, view, queue=None):
    """Mock the three endpoints collect() uses: /history, /view (a callable taking the requested filename) and /queue."""
    def fake_get(base, path, **kw):
        if path.startswith("/history"):
            return FakeResponse(history)
        if path == "/queue":
            return FakeResponse(queue if queue is not None else {"queue_running": [], "queue_pending": []})
        if path == "/system_stats":
            return FakeResponse({"ok": True})
        if path == "/view":
            return view(kw.get("params") or {})
        raise AssertionError(path)
    monkeypatch.setattr(ad, "_get", fake_get)
    monkeypatch.setattr(ad, "_poll_progress", lambda spec: None)


@pytest.fixture
def fast_layout(tmp_path):
    """download_retry_s=0: the retry-on-failed-download tests exercise the real retry loop without sleeping."""
    return testing.project(tmp_path, plugins=["comfy"], routing={"plugins": {"comfy": {"download_retry_s": 0}}})


def test_collect_never_completes_when_an_output_cannot_be_downloaded(fast_layout, ad, monkeypatch):
    """A /view connection that drops must not finish the job: COMPLETE with no images is the silent data loss."""
    layout = fast_layout
    ctask(layout, vram_mb=0)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    attempts = []

    def view(params):
        attempts.append(params["filename"])
        raise requests.ConnectionError("connection reset")
    _serve(ad, monkeypatch, _history({"9": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}}), view)
    assert ad.collect(spec, {"prompt_id": "abc123", "client_id": "c1"}) is None      # retried on the next tick
    assert len(attempts) == spec["download_attempts"]                               # and retried within the tick
    failures = json.loads((Path(spec["run_dir"]) / "collect_failures.json").read_text())
    assert "out.png" in failures["failures"][0]
    expired = {"prompt_id": "abc123", "client_id": "c1", "deadline": time.time() - 1}   # out of budget: fail, do not lie
    result = ad.collect(spec, expired)
    assert result is not None and result.status == "FAILED" and "could not collect 1" in result.error


def test_collect_recovers_when_a_retried_download_succeeds(fast_layout, ad, monkeypatch):
    layout = fast_layout
    ctask(layout, vram_mb=0)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    calls = []

    def view(params):
        calls.append(params["filename"])
        if len(calls) == 1:
            raise requests.ConnectionError("connection reset")
        return FakeResponse(content=b"PNGDATA")
    _serve(ad, monkeypatch, _history({"9": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}}), view)
    result = ad.collect(spec, {"prompt_id": "abc123", "client_id": "c1"})
    assert result is not None and result.status == "COMPLETE"
    assert (layout.root / result.artifacts[0]).read_bytes() == b"PNGDATA"
    assert not (Path(spec["run_dir"]) / "collect_failures.json").exists()


@pytest.mark.parametrize("filename", ["../prompt_id.json", "..\\..\\escaped.png", "C:\\Windows\\Temp\\escaped.png",
                                      "/etc/passwd", "sub/../../escaped.png"])
def test_collect_refuses_output_names_that_escape_the_run_dir(layout, ad, monkeypatch, filename):
    """Server-supplied filenames are untrusted: none of them may become a write destination outside run_dir/output."""
    ctask(layout, vram_mb=0)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    run_dir = Path(spec["run_dir"])
    (run_dir / "prompt_id.json").write_text("sentinel", encoding="utf-8")
    _serve(ad, monkeypatch, _history({"9": {"images": [{"filename": filename, "subfolder": "", "type": "output"}]}}),
           lambda params: FakeResponse(content=b"EVIL"))
    assert ad.collect(spec, {"prompt_id": "abc123", "client_id": "c1"}) is None
    expired = {"prompt_id": "abc123", "client_id": "c1", "deadline": time.time() - 1}
    result = ad.collect(spec, expired)
    assert result is not None and result.status == "FAILED" and "does not resolve inside" in result.error
    assert (run_dir / "prompt_id.json").read_text() == "sentinel"                    # nothing was overwritten
    assert not list((run_dir / "output").rglob("*EVIL*"))
    assert all(p.read_bytes() != b"EVIL" for p in run_dir.rglob("*") if p.is_file())


def test_collect_keeps_equal_filenames_from_different_subfolders_apart(layout, ad, monkeypatch):
    ctask(layout, vram_mb=0)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    outputs = {"9": {"images": [{"filename": "same.png", "subfolder": "a", "type": "output"},
                                {"filename": "same.png", "subfolder": "b", "type": "output"},
                                {"filename": "same.png", "subfolder": "", "type": "temp"}]}}
    _serve(ad, monkeypatch, _history(outputs), lambda params: FakeResponse(content=params["subfolder"].encode() or b"root"))
    result = ad.collect(spec, {"prompt_id": "abc123", "client_id": "c1"})
    assert result is not None and result.status == "COMPLETE"
    assert len(result.artifacts) == len(set(result.artifacts)) == 3
    assert [(layout.root / a).read_bytes() for a in sorted(result.artifacts)] == [b"a", b"b", b"root"]


def gpu_rec(layout, ad, tid="CF", expired=True):
    """A comfy job record in RUNNING with its deadline passed - the state the gate counts as the GPU reservation.
    The deadline lives in the handle (submit()'s contract), not the spec: settle() reads `rec["handle"]`."""
    spec = ad.prepare(layout, testing.task(layout, tid), f"{tid}.a1")
    deadline = time.time() - 1 if expired else time.time() + 1800
    return testing.running_job(layout, tid, "comfy", spec, {"kind": "comfy", "prompt_id": "abc123", "client_id": "c1",
                                                            "base_url": spec["base_url"], "deadline": deadline}, gpu=True)


def test_expired_but_still_queued_job_keeps_its_gpu_slot(layout, ad, monkeypatch):
    """An expired prompt that ComfyUI is still running must not settle its record: settling releases the GPU
    reservation (the scheduler counts running records) while the remote job keeps using the card."""
    ctask(layout, "CF"); ctask(layout, "CF2")
    gpu_rec(layout, ad)
    posts = []
    _serve(ad, monkeypatch, {}, lambda params: FakeResponse(content=b""),
           queue={"queue_running": [[0, "abc123"]], "queue_pending": []})
    monkeypatch.setattr(ad, "_post", lambda base, path, payload, **kw: posts.append(path) or FakeResponse({}))
    testing.settle(layout)
    assert posts.count("/queue") >= 1 and "/interrupt" in posts      # cancellation really was requested
    assert testing.job(layout, "CF.a1")["phase"] == "RUNNING"        # the record (and the slot) is retained
    assert testing.plan(layout) == []                                # so no second GPU job is admitted
    assert json.loads((Path(testing.job(layout, "CF.a1")["spec"]["run_dir"]) / "cancel_pending.json").read_text())["remote_stopped"] is False


def test_expired_job_fails_once_comfy_confirms_the_prompt_stopped(layout, ad, monkeypatch):
    ctask(layout, "CF"); ctask(layout, "CF2")
    gpu_rec(layout, ad)
    _serve(ad, monkeypatch, {}, lambda params: FakeResponse(content=b""),
           queue={"queue_running": [], "queue_pending": []})
    monkeypatch.setattr(ad, "_post", lambda base, path, payload, **kw: FakeResponse({}))
    testing.settle(layout)
    rec = testing.job(layout, "CF.a1")
    assert rec["phase"] == "DONE" and rec["result"] == "FAILED"      # terminal only after confirmation
    assert [t for t, _ in testing.plan(layout)] == ["CF2"]           # and the slot is free again


def _unreadable_queue_case(monkeypatch, ad, get_queue):
    """/history also errors (so collect() falls through to the queue-vanished check) and /queue behaves per
    `get_queue` (a callable building the response or raising); /system_stats and /interrupt/delete always succeed."""
    def fake_get(base, path, **kw):
        if path.startswith("/history"):
            raise requests.ConnectionError("history unreachable")
        if path == "/queue":
            return get_queue()
        if path == "/system_stats":
            return FakeResponse({"ok": True})
        raise AssertionError(path)
    monkeypatch.setattr(ad, "_get", fake_get)
    monkeypatch.setattr(ad, "_post", lambda base, path, payload, **kw: FakeResponse({}))
    monkeypatch.setattr(ad, "_poll_progress", lambda spec: None)


@pytest.mark.parametrize("get_queue", [
    pytest.param(lambda: (_ for _ in ()).throw(requests.ReadTimeout("slow")), id="read_timeout"),
    pytest.param(lambda: FakeResponse(status_code=503, text="busy"), id="http_503"),
    pytest.param(lambda: FakeResponse({}), id="empty_body"),
    pytest.param(lambda: FakeResponse({"queue_running": "nope", "queue_pending": []}), id="non_list_bucket"),
    pytest.param(lambda: FakeResponse({"queue_running": [["not-a-list-entry"]], "queue_pending": []}), id="malformed_entry"),
    pytest.param(lambda: FakeResponse({"queue_running": [[0, None]], "queue_pending": []}), id="null_prompt_id"),
    pytest.param(lambda: FakeResponse({"queue_running": [[0, []]], "queue_pending": []}), id="list_prompt_id"),
    pytest.param(lambda: FakeResponse({"queue_running": [[0, {}]], "queue_pending": []}), id="dict_prompt_id"),
    pytest.param(lambda: FakeResponse({"queue_running": [[0, 17]], "queue_pending": []}), id="numeric_prompt_id"),
    pytest.param(lambda: FakeResponse({"queue_running": [[0, "   "]], "queue_pending": []}), id="blank_prompt_id"),
    pytest.param(lambda: FakeResponse({"queue_running": [], "queue_pending": [[0, None]]}), id="null_id_in_pending"),
])
def test_expired_job_holds_the_slot_on_every_unknown_queue_shape(layout, ad, monkeypatch, get_queue):
    ctask(layout, "CF"); ctask(layout, "CF2")
    gpu_rec(layout, ad)
    _unreadable_queue_case(monkeypatch, ad, get_queue)
    testing.settle(layout)                                           # must not raise, whatever the body looked like
    rec = testing.job(layout, "CF.a1")
    assert rec["phase"] == "RUNNING"                                 # never settled while the state is unknown
    assert testing.plan(layout) == []                                # so no second GPU job is admitted
    cancel = json.loads((Path(rec["spec"]["run_dir"]) / "cancel_pending.json").read_text())
    assert cancel["remote_stopped"] is None


@pytest.mark.parametrize("body", [
    {"queue_running": [[0, None]], "queue_pending": []},
    {"queue_running": [[0, []]], "queue_pending": []},
    {"queue_running": [[0, {}]], "queue_pending": []},
    {"queue_running": [[0, ""]], "queue_pending": []},
    {"queue_running": [[0, "other"], [1, None]], "queue_pending": []},   # one bad entry poisons the whole body
])
def test_parse_queue_rejects_malformed_prompt_ids_without_raising(ad, body):
    assert ad._parse_queue(body) is None


def test_parse_queue_still_reads_a_well_formed_body(ad):
    """The id validation must not be so strict that a real ComfyUI queue stops parsing."""
    running, pending = ad._parse_queue({"queue_running": [[0, "abc123", {"1": {}}, [], []]],
                                        "queue_pending": [[1, "def456"]]})
    assert running == {"abc123"} and pending == {"def456"}


def test_expired_job_holds_the_slot_while_a_connection_error_probe_is_inconclusive(layout, ad, monkeypatch):
    """A ConnectionError alone is never proof: when the independent process probe also cannot tell (AccessDenied,
    or any other psutil failure), the reservation stays held."""
    ctask(layout, "CF"); ctask(layout, "CF2")
    gpu_rec(layout, ad)

    def fake_get(base, path, **kw):
        if path.startswith("/history") or path == "/queue":
            raise requests.ConnectionError("broken")
        raise AssertionError(path)
    monkeypatch.setattr(ad, "_get", fake_get)
    monkeypatch.setattr(ad, "_post", lambda base, path, payload, **kw: FakeResponse({}))
    monkeypatch.setattr(ad, "_poll_progress", lambda spec: None)
    monkeypatch.setattr(comfy, "_process_probe_confirms_stopped", lambda base_url, root: None)
    testing.settle(layout)
    rec = testing.job(layout, "CF.a1")
    assert rec["phase"] == "RUNNING"
    assert testing.plan(layout) == []


def test_expired_job_fails_when_the_server_is_genuinely_gone(layout, ad, monkeypatch):
    """A ConnectionError plus a process probe that independently confirms nothing is running is the only path from
    lost HTTP contact to a terminal result: the GPU reservation is released."""
    ctask(layout, "CF"); ctask(layout, "CF2")
    gpu_rec(layout, ad)

    def dead(base, path, *a, **kw):
        raise requests.ConnectionError("no server")
    monkeypatch.setattr(ad, "_get", dead)
    monkeypatch.setattr(ad, "_post", dead)
    monkeypatch.setattr(ad, "_poll_progress", lambda spec: None)
    monkeypatch.setattr(comfy, "_process_probe_confirms_stopped", lambda base_url, root: True)
    testing.settle(layout)
    rec = testing.job(layout, "CF.a1")
    assert rec["phase"] == "DONE" and rec["result"] == "FAILED"
    assert [t for t, _ in testing.plan(layout)] == ["CF2"]      # the slot is free again


def test_submit_defers_when_comfy_unreachable(layout, ad, monkeypatch):
    ctask(layout, vram_mb=0)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")

    def raise_conn(base, path, **kw):
        raise requests.ConnectionError("no server")
    monkeypatch.setattr(ad, "_get", raise_conn)
    with pytest.raises(plugin.CapacityDeferred):
        ad.submit(spec)


# ---------------------------------------------------------------- process probe (module function, unit-level)

def test_process_probe_confirms_stopped_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(comfy.psutil, "net_connections", lambda kind="inet": [])
    monkeypatch.setattr(comfy.psutil, "process_iter", lambda attrs=None: iter(()))
    assert comfy._process_probe_confirms_stopped("http://127.0.0.1:8188", "D:\\comfy-install") is True


def test_process_probe_finds_a_listener_on_the_port(monkeypatch):
    class Laddr:
        port = 8188
    class Conn:
        laddr = Laddr()
        status = comfy.psutil.CONN_LISTEN
    monkeypatch.setattr(comfy.psutil, "net_connections", lambda kind="inet": [Conn()])
    assert comfy._process_probe_confirms_stopped("http://127.0.0.1:8188", "D:\\comfy-install") is False


def test_process_probe_finds_a_matching_cmdline(monkeypatch):
    class P:
        info = {"cmdline": ["D:\\comfy-install\\venv\\Scripts\\python.exe", "main.py"]}
    monkeypatch.setattr(comfy.psutil, "net_connections", lambda kind="inet": [])
    monkeypatch.setattr(comfy.psutil, "process_iter", lambda attrs=None: iter([P()]))
    assert comfy._process_probe_confirms_stopped("http://127.0.0.1:8188", "D:\\comfy-install") is False


def test_process_probe_inconclusive_on_access_denied(monkeypatch):
    def boom(kind="inet"):
        raise comfy.psutil.AccessDenied()
    monkeypatch.setattr(comfy.psutil, "net_connections", boom)
    assert comfy._process_probe_confirms_stopped("http://127.0.0.1:8188", "D:\\comfy-install") is None


# an unreadable command line is not evidence that a process is innocent, and must not let the probe release the
# GPU reservation (psutil reports a denied attribute as None, never as an empty value).

def _probe_with(monkeypatch, procs, port_in_use=False):
    class Laddr:
        port = 8188
    class Conn:
        laddr = Laddr()
        status = comfy.psutil.CONN_LISTEN
    monkeypatch.setattr(comfy.psutil, "net_connections", lambda kind="inet": [Conn()] if port_in_use else [])
    monkeypatch.setattr(comfy.psutil, "process_iter",
                        lambda attrs=None: iter([type("P", (), {"info": info})() for info in procs]))
    return comfy._process_probe_confirms_stopped("http://127.0.0.1:8188", "D:\\comfy-install")


@pytest.mark.parametrize("info", [
    pytest.param({"name": "python.exe", "exe": None, "cmdline": None}, id="python_cmdline_denied"),
    pytest.param({"name": "pythonw.exe", "exe": None, "cmdline": None}, id="pythonw_cmdline_denied"),
    pytest.param({"name": "ComfyUI.exe", "exe": None, "cmdline": None}, id="packaged_comfy_cmdline_denied"),
    pytest.param({"name": None, "exe": None, "cmdline": None}, id="name_and_cmdline_denied"),
    pytest.param({}, id="nothing_readable_at_all"),
])
def test_process_probe_is_inconclusive_when_a_possible_host_cmdline_is_unreadable(monkeypatch, info):
    """The reservation must survive a process inspection that could not be completed: an elevated or otherwise
    unreadable python process may be the ComfyUI server still holding the card."""
    assert _probe_with(monkeypatch, [info]) is None


def test_process_probe_is_inconclusive_when_psutil_raises_access_denied_per_process(monkeypatch):
    class Denied:
        @property
        def info(self):
            raise comfy.psutil.AccessDenied()
    monkeypatch.setattr(comfy.psutil, "net_connections", lambda kind="inet": [])
    monkeypatch.setattr(comfy.psutil, "process_iter", lambda attrs=None: iter([Denied()]))
    assert comfy._process_probe_confirms_stopped("http://127.0.0.1:8188", "D:\\comfy-install") is None


def test_process_probe_ignores_unreadable_cmdlines_of_processes_that_cannot_host_comfy(monkeypatch):
    """Windows denies the command line of some system/elevated processes. Those are ruled out by a name that
    cannot be a Python server - otherwise the probe would never confirm a shutdown at all, and a genuinely dead
    server would hold its GPU slot until an operator intervened."""
    procs = [{"name": "svchost.exe", "exe": None, "cmdline": None},
             {"name": "csrss.exe", "exe": None, "cmdline": None},
             {"name": "", "exe": "", "cmdline": None},           # kernel minimal process: no image name at all
             {"name": "explorer.exe", "exe": "C:\\Windows\\explorer.exe", "cmdline": ["explorer.exe"]}]
    assert _probe_with(monkeypatch, procs) is True


def test_process_probe_finds_the_server_by_exe_even_when_the_cmdline_is_hidden(monkeypatch):
    procs = [{"name": "python.exe", "exe": "D:\\comfy-install\\venv\\Scripts\\python.exe", "cmdline": None}]
    assert _probe_with(monkeypatch, procs) is False


def test_process_probe_reports_a_found_server_over_an_unreadable_process(monkeypatch):
    """A positively identified server outranks an inconclusive one: keep scanning instead of returning None early."""
    procs = [{"name": "python.exe", "exe": None, "cmdline": None},
             {"name": "python.exe", "exe": None, "cmdline": ["python.exe", "D:\\comfy-install\\main.py"]}]
    assert _probe_with(monkeypatch, procs) is False


def test_process_probe_is_inconclusive_without_a_configured_install_path(monkeypatch):
    monkeypatch.setattr(comfy.psutil, "net_connections", lambda kind="inet": [])
    monkeypatch.setattr(comfy.psutil, "process_iter", lambda attrs=None: iter(()))
    assert comfy._process_probe_confirms_stopped("http://127.0.0.1:8188", None) is None


def test_expired_job_holds_the_slot_when_a_process_cmdline_is_unreadable(layout, ad, monkeypatch):
    """End to end at scheduler level, through the real probe: HTTP contact lost and one python process whose command
    line the OS will not disclose -> the job stays RUNNING and no second GPU job is admitted."""
    ctask(layout, "CF"); ctask(layout, "CF2")
    gpu_rec(layout, ad)

    def dead(base, path, *a, **kw):
        raise requests.ConnectionError("no server")
    monkeypatch.setattr(ad, "_get", dead)
    monkeypatch.setattr(ad, "_post", dead)
    monkeypatch.setattr(ad, "_poll_progress", lambda spec: None)
    monkeypatch.setattr(comfy.psutil, "net_connections", lambda kind="inet": [])
    monkeypatch.setattr(comfy.psutil, "process_iter",
                        lambda attrs=None: iter([type("P", (), {"info": {"name": "python.exe", "exe": None,
                                                                        "cmdline": None}})()]))
    testing.settle(layout)
    rec = testing.job(layout, "CF.a1")
    assert rec["phase"] == "RUNNING"
    assert testing.plan(layout) == []
    cancel = json.loads((Path(rec["spec"]["run_dir"]) / "cancel_pending.json").read_text())
    assert cancel["remote_stopped"] is None


# ---------------------------------------------------------------- deadline: computed at submit(), not prepare()

def test_prepare_sets_no_deadline(layout, ad):
    ctask(layout, vram_mb=0)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    assert "deadline" not in spec


def test_capacity_deferred_submit_keeps_the_full_budget_on_a_later_success(layout, ad, monkeypatch):
    ctask(layout, vram_mb=4000)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda: 1000)
    with pytest.raises(plugin.CapacityDeferred):
        ad.submit(spec)                                             # deferred: no deadline exists anywhere yet
    time.sleep(0.05)                                                 # simulate time passing while deferred
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda: 8000)
    monkeypatch.setattr(ad, "_get", lambda base, path, **kw: FakeResponse({"ok": True}))
    monkeypatch.setattr(ad, "_post", lambda base, path, payload, **kw: FakeResponse({"prompt_id": "late-id"}))
    before = time.time()
    handle = ad.submit(spec)
    assert handle["deadline"] >= before + spec["timeout_s"] - 1      # full budget, not reduced by the defer wait


def test_recover_preserves_the_original_submission_deadline(layout, ad, monkeypatch):
    ctask(layout, vram_mb=0)
    spec = ad.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    monkeypatch.setattr(ad, "_get", lambda base, path, **kw: FakeResponse({"ok": True}))
    monkeypatch.setattr(ad, "_post", lambda base, path, payload, **kw: FakeResponse({"prompt_id": "abc"}))
    first = ad.submit(spec)
    time.sleep(0.05)                                                 # a later process (crash/resume) recovers it
    recovered = ad.recover(spec)
    assert recovered["deadline"] == first["deadline"]                # not recomputed


@pytest.mark.parametrize("spec,handle", [
    pytest.param({"timeout_s": 1800}, {"deadline": time.time() + 500}, id="handle_deadline"),
    pytest.param({"timeout_s": 1800, "deadline": time.time() + 500}, {}, id="spec_deadline_legacy"),
    pytest.param({"timeout_s": 500}, {"submitted": plugin.now_iso()}, id="submitted_plus_timeout"),
    pytest.param({"timeout_s": 500}, {}, id="no_source_anchor_persisted"),
])
def test_deadline_is_anchored_and_constant_across_calls(ad, tmp_path, spec, handle, monkeypatch):
    """For a fixed (spec, handle, run_dir), two calls to _deadline with the clock advanced in between must return
    the SAME value, across all four resolution sources including the no-source-at-all (persisted anchor) case."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    first = ad._deadline(spec, handle, run_dir)
    monkeypatch.setattr(comfy.time, "time", lambda: time.time() + 10_000)
    second = ad._deadline(spec, handle, run_dir)
    assert second == first


def legacy_spec_deadline_rec(layout, ad, tid="CF", expired=True):
    """A pre-move handle: the deadline lives only in spec, never in the handle at all. Regression coverage for the
    sliding-deadline bug where this shape never expired because the fallback recomputed `now + timeout_s` on every
    poll instead of reading spec['deadline']."""
    spec = ad.prepare(layout, testing.task(layout, tid), f"{tid}.a1")
    spec["deadline"] = time.time() - 1 if expired else time.time() + 1800
    return testing.running_job(layout, tid, "comfy", spec, {"kind": "comfy", "prompt_id": "abc123", "client_id": "c1",
                                                            "base_url": spec["base_url"]}, gpu=True)


def anchorless_rec(layout, ad, tid="CF", anchor_deadline=None):
    """Neither the handle nor the spec carries any deadline information - the run-dir anchor is the only source.
    `anchor_deadline`, when given, pre-seeds run_dir/deadline.json exactly as a first collect() call would have
    written it, so the test can exercise an already-expired or not-yet-expired anchor without waiting out a real
    timeout_s."""
    spec = ad.prepare(layout, testing.task(layout, tid), f"{tid}.a1")
    if anchor_deadline is not None:
        plugin.atomic_json(Path(spec["run_dir"]) / "deadline.json", {"deadline": anchor_deadline})
    return testing.running_job(layout, tid, "comfy", spec, {"kind": "comfy", "prompt_id": "abc123", "client_id": "c1",
                                                            "base_url": spec["base_url"]}, gpu=True)


def test_legacy_spec_deadline_expires_when_history_is_unreachable(layout, ad, monkeypatch):
    """Both non-expiring paths of collect() must still route an expired legacy job into _expire(): here /history
    raises (the requests.RequestException branch) rather than returning a 404/empty body."""
    ctask(layout, "CF"); ctask(layout, "CF2")
    legacy_spec_deadline_rec(layout, ad)

    def fake_get(base, path, **kw):
        if path.startswith("/history"):
            raise requests.ConnectionError("history unreachable")
        if path == "/queue":
            return FakeResponse({"queue_running": [[0, "abc123"]], "queue_pending": []})
        raise AssertionError(path)
    monkeypatch.setattr(ad, "_get", fake_get)
    posts = []
    monkeypatch.setattr(ad, "_post", lambda base, path, payload, **kw: posts.append(path) or FakeResponse({}))
    monkeypatch.setattr(ad, "_poll_progress", lambda spec: None)
    testing.settle(layout)
    rec = testing.job(layout, "CF.a1")
    assert rec["phase"] == "RUNNING"                                  # expired, but the server still has it queued
    assert testing.plan(layout) == []                                 # so no second GPU job is admitted
    assert "/interrupt" in posts                                      # cancellation really was requested

    def fake_get_gone(base, path, **kw):
        if path.startswith("/history"):
            raise requests.ConnectionError("history unreachable")
        if path == "/queue":
            return FakeResponse({"queue_running": [], "queue_pending": []})
        raise AssertionError(path)
    monkeypatch.setattr(ad, "_get", fake_get_gone)
    testing.settle(layout)
    rec = testing.job(layout, "CF.a1")
    assert rec["phase"] == "DONE" and rec["result"] == "FAILED"       # settles only once confirmed gone


def test_legacy_spec_deadline_expires_when_the_prompt_is_absent_from_history_and_queue(layout, ad, monkeypatch):
    """The other non-expiring path: /history answers but has no entry for our prompt_id (entry is None)."""
    ctask(layout, "CF"); ctask(layout, "CF2")
    legacy_spec_deadline_rec(layout, ad)

    def fake_get(base, path, **kw):
        if path.startswith("/history"):
            return FakeResponse({})
        if path == "/queue":
            return FakeResponse({"queue_running": [[0, "abc123"]], "queue_pending": []})
        raise AssertionError(path)
    monkeypatch.setattr(ad, "_get", fake_get)
    posts = []
    monkeypatch.setattr(ad, "_post", lambda base, path, payload, **kw: posts.append(path) or FakeResponse({}))
    monkeypatch.setattr(ad, "_poll_progress", lambda spec: None)
    testing.settle(layout)
    rec = testing.job(layout, "CF.a1")
    assert rec["phase"] == "RUNNING"
    assert testing.plan(layout) == []
    assert "/interrupt" in posts

    def fake_get_gone(base, path, **kw):
        if path.startswith("/history"):
            return FakeResponse({})
        if path == "/queue":
            return FakeResponse({"queue_running": [], "queue_pending": []})
        raise AssertionError(path)
    monkeypatch.setattr(ad, "_get", fake_get_gone)
    testing.settle(layout)
    rec = testing.job(layout, "CF.a1")
    assert rec["phase"] == "DONE" and rec["result"] == "FAILED"


def test_anchorless_job_expires_from_its_persisted_run_dir_deadline(layout, ad, monkeypatch):
    """Neither the handle nor the spec carries a deadline: the run-dir anchor (pre-seeded here exactly as a first
    collect() call would have written it) is the only thing that can expire the job. Same two-phase proof as the
    legacy-spec-deadline tests: held while the server still shows the prompt, settled once it confirms gone."""
    ctask(layout, "CF"); ctask(layout, "CF2")
    anchorless_rec(layout, ad, anchor_deadline=time.time() - 1)

    def fake_get(base, path, **kw):
        if path.startswith("/history"):
            raise requests.ConnectionError("history unreachable")
        if path == "/queue":
            return FakeResponse({"queue_running": [[0, "abc123"]], "queue_pending": []})
        raise AssertionError(path)
    monkeypatch.setattr(ad, "_get", fake_get)
    posts = []
    monkeypatch.setattr(ad, "_post", lambda base, path, payload, **kw: posts.append(path) or FakeResponse({}))
    monkeypatch.setattr(ad, "_poll_progress", lambda spec: None)
    testing.settle(layout)
    rec = testing.job(layout, "CF.a1")
    assert rec["phase"] == "RUNNING"
    assert testing.plan(layout) == []
    assert "/interrupt" in posts

    def fake_get_gone(base, path, **kw):
        if path.startswith("/history"):
            raise requests.ConnectionError("history unreachable")
        if path == "/queue":
            return FakeResponse({"queue_running": [], "queue_pending": []})
        raise AssertionError(path)
    monkeypatch.setattr(ad, "_get", fake_get_gone)
    testing.settle(layout)
    rec = testing.job(layout, "CF.a1")
    assert rec["phase"] == "DONE" and rec["result"] == "FAILED"


def test_unexpired_legacy_job_is_not_cancelled_early(layout, ad, monkeypatch):
    """The symmetric mistake: a legacy spec deadline (or anchor) still in the future must not be treated as expired
    - collect() returns None without ever asking ComfyUI to cancel the prompt."""
    ctask(layout, "CF"); ctask(layout, "CF2")
    rec = legacy_spec_deadline_rec(layout, ad, expired=False)
    cancelled = []

    def fake_get(base, path, **kw):
        if path.startswith("/history"):
            raise requests.ConnectionError("history unreachable")
        raise AssertionError(path)
    monkeypatch.setattr(ad, "_get", fake_get)
    monkeypatch.setattr(ad, "_post", lambda base, path, payload, **kw: cancelled.append(path) or FakeResponse({}))
    monkeypatch.setattr(ad, "_poll_progress", lambda spec: None)
    result = ad.collect(rec["spec"], rec["handle"])
    assert result is None
    assert cancelled == []                                            # _cancel() was never invoked

    rec2 = anchorless_rec(layout, ad, tid="CF2", anchor_deadline=time.time() + 1800)
    result2 = ad.collect(rec2["spec"], rec2["handle"])
    assert result2 is None
    assert cancelled == []
