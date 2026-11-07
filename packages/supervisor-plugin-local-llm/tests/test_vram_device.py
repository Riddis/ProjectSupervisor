"""The device-aware preflight (framework 2.3.0's `supervisor.plugin.gpu_device`): the optional `vram_device` key
resolves a role's own card and floor from `resources.gpu.devices`, read by `local_llm_config()`, carried in the
spec `prepare()` returns, and consulted by `submit()`'s preflight. Invented device names and numbers throughout
(D-005): no project's own role names, indices or floors appear here."""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from supervisor import plugin, testing

import supervisor_plugin_local_llm as local_llm

DEVICES = {
    "alpha": {"index": 3, "usable_vram_floor_mb": 5000},
    "beta": {"index": 5, "usable_vram_floor_mb": 4000},
}


class _Quiet(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_server():
    srv = HTTPServer(("127.0.0.1", 0), _Quiet)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def devices_layout(tmp_path, *, vram_device, devices=None):
    return testing.project(tmp_path, plugins=["local_llm"],
                           routing={"resources": {"gpu": {"devices": devices if devices is not None else DEVICES}},
                                    "plugins": {"local_llm": {"vram_device": vram_device}}})


def lltask(layout, base_url, tid="LL", vram_mb=None):
    job = {"adapter": "local_llm", "base_url": base_url, "model": "pixnote:4b", "prompt": "describe",
           "schema": {"type": "object"}, "timeout_s": 30}
    if vram_mb is not None:
        job["vram_mb"] = vram_mb
    testing.make_task(layout, tid, job=job, cost_category="local", artifacts=[])


def test_configured_device_below_floor_while_the_other_card_is_free_defers_naming_the_device(tmp_path, fake_server, monkeypatch):
    layout = devices_layout(tmp_path, vram_device="alpha")
    lltask(layout, fake_server)
    spec = local_llm.ADAPTER.prepare(layout, testing.task(layout, "LL"), "LL.a1")
    assert spec["vram_device_index"] == 3 and spec["vram_device_name"] == "alpha"
    assert spec["vram_floor_mb"] == 5000
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: {3: 4500, 5: 9000}.get(index))
    with pytest.raises(plugin.CapacityDeferred) as excinfo:
        local_llm.ADAPTER.submit(spec)
    assert str(excinfo.value) == "GPU alpha free VRAM 4500MiB below floor 5000MiB"


def test_configured_device_free_while_the_other_card_is_full_admits(tmp_path, fake_server, monkeypatch):
    layout = devices_layout(tmp_path, vram_device="alpha")
    lltask(layout, fake_server)
    spec = local_llm.ADAPTER.prepare(layout, testing.task(layout, "LL"), "LL.a1")
    calls = []

    def fake_free(index=None):
        calls.append(index)
        return {3: 9000, 5: 200}.get(index)
    monkeypatch.setattr(plugin.gpu, "free_mb", fake_free)
    monkeypatch.setattr(plugin.ProcessAdapter, "submit", lambda self, spec: {"ok": True})
    result = local_llm.ADAPTER.submit(spec)
    assert result == {"ok": True}
    assert calls == [3]            # read the configured card, never the other one


def test_vram_device_unset_reproduces_1_0_0_argv_and_message(tmp_path, fake_server, monkeypatch):
    layout = testing.project(tmp_path, plugins=["local_llm"], routing={"plugins": {"local_llm": {}}})
    lltask(layout, fake_server, vram_mb=4000)
    spec = local_llm.ADAPTER.prepare(layout, testing.task(layout, "LL"), "LL.a1")
    assert "vram_device_index" not in spec and "vram_device_name" not in spec
    captured = []

    def fake_run(argv, **kw):
        captured.append(argv)
        class R:
            returncode = 0
            stdout = "500\n"
        return R()
    monkeypatch.setattr(plugin.gpu.subprocess, "run", fake_run)
    with pytest.raises(plugin.CapacityDeferred) as excinfo:
        local_llm.ADAPTER.submit(spec)
    assert captured == [["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"]]
    assert str(excinfo.value) == "GPU free VRAM 500MiB below floor 4000MiB"


def test_vram_device_naming_no_entry_raises_from_the_config_helper_and_fails_the_doctor_check(tmp_path):
    layout = devices_layout(tmp_path, vram_device="gamma")
    with pytest.raises(plugin.PluginError, match="gamma"):
        local_llm.local_llm_config(layout)
    check = local_llm.check_ollama(layout)
    assert check.name == "ollama"
    assert not check.ok and "gamma" in check.detail and "vram_device" in check.detail


def test_a_bare_integer_is_used_as_the_index(tmp_path, fake_server):
    layout = devices_layout(tmp_path, vram_device=7)
    cfg = local_llm.local_llm_config(layout)
    assert cfg["vram_device_index"] == 7
    assert cfg.get("vram_device_name") is None
    lltask(layout, fake_server)
    spec = local_llm.ADAPTER.prepare(layout, testing.task(layout, "LL"), "LL.a1")
    assert spec["vram_device_index"] == 7
    assert "vram_device_name" not in spec


def test_vram_device_true_is_refused_not_silently_coerced_to_index_1(tmp_path):
    layout = devices_layout(tmp_path, vram_device=True)
    with pytest.raises(plugin.PluginError) as excinfo:
        local_llm.local_llm_config(layout)
    assert "plugins.local_llm.vram_device" in str(excinfo.value)
    assert "True" in str(excinfo.value)


def test_vram_device_negative_index_is_refused_from_the_config_helper(tmp_path):
    layout = devices_layout(tmp_path, vram_device=-1)
    with pytest.raises(plugin.PluginError) as excinfo:
        local_llm.local_llm_config(layout)
    assert "plugins.local_llm.vram_device" in str(excinfo.value)
    assert "-1" in str(excinfo.value)
