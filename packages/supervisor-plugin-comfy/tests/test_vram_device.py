"""The device-aware preflight (framework 2.3.0's `supervisor.plugin.gpu_device`): the optional `vram_device` key
resolves a role's own card and floor from `resources.gpu.devices`, read by `comfy_config()`, carried in the spec
`prepare()` returns, and consulted by `submit()`'s preflight, `_record_peak_vram()`, `check_gpu` and `gpu_panel`.
Invented device names and numbers throughout (D-005): no project's own role names, indices or floors appear here."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from supervisor import plugin, testing

import supervisor_plugin_comfy as comfy

DEVICES = {
    "alpha": {"index": 3, "usable_vram_floor_mb": 5000},
    "beta": {"index": 5, "usable_vram_floor_mb": 4000},
}


class FakeResponse:
    def __init__(self, payload=None, status_code=200, content=b""):
        self._payload = payload
        self.status_code = status_code
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"http {self.status_code}")


def devices_layout(tmp_path, *, vram_device, devices=None):
    return testing.project(tmp_path, plugins=["comfy"],
                           routing={"resources": {"gpu": {"devices": devices if devices is not None else DEVICES}},
                                    "plugins": {"comfy": {"vram_device": vram_device}}})


def ctask(layout, tid="CF", vram_mb=None, workflow=None):
    job = {"adapter": "comfy", "workflow": workflow or {"1": {"class_type": "noop"}}}
    if vram_mb is not None:
        job["vram_mb"] = vram_mb
    testing.make_task(layout, tid, job=job, cost_category="local", artifacts=[])


def test_configured_device_below_floor_while_the_other_card_is_free_defers_naming_the_device(tmp_path, monkeypatch):
    layout = devices_layout(tmp_path, vram_device="alpha")
    ctask(layout)
    spec = comfy.ADAPTER.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    assert spec["vram_device_index"] == 3 and spec["vram_device_name"] == "alpha"
    assert spec["vram_floor_mb"] == 5000
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: {3: 4500, 5: 9000}.get(index))
    with pytest.raises(plugin.CapacityDeferred) as excinfo:
        comfy.ADAPTER.submit(spec)
    assert str(excinfo.value) == "GPU alpha free VRAM 4500MiB below floor 5000MiB"


def test_configured_device_free_while_the_other_card_is_full_admits(tmp_path, monkeypatch):
    layout = devices_layout(tmp_path, vram_device="alpha")
    ctask(layout)
    spec = comfy.ADAPTER.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    calls = []

    def fake_free(index=None):
        calls.append(index)
        return {3: 9000, 5: 200}.get(index)
    monkeypatch.setattr(plugin.gpu, "free_mb", fake_free)
    monkeypatch.setattr(comfy.ADAPTER, "_get", lambda base, path, **kw: FakeResponse({"ok": True}))
    monkeypatch.setattr(comfy.ADAPTER, "_post", lambda base, path, payload, **kw: FakeResponse({"prompt_id": "abc"}))
    handle = comfy.ADAPTER.submit(spec)
    assert handle["prompt_id"] == "abc"
    assert calls == [3]            # read the configured card, never the other one


def test_vram_device_unset_reproduces_1_0_0_argv_and_message(tmp_path, monkeypatch):
    layout = testing.project(tmp_path, plugins=["comfy"], routing={"plugins": {"comfy": {}}})
    ctask(layout, vram_mb=4000)
    spec = comfy.ADAPTER.prepare(layout, testing.task(layout, "CF"), "CF.a1")
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
        comfy.ADAPTER.submit(spec)
    assert captured == [["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"]]
    assert str(excinfo.value) == "GPU free VRAM 500MiB below floor 4000MiB"


def test_vram_device_naming_no_entry_raises_from_the_config_helper_and_fails_the_doctor_check(tmp_path):
    layout = devices_layout(tmp_path, vram_device="gamma")
    with pytest.raises(plugin.PluginError, match="gamma"):
        comfy.comfy_config(layout)
    check = comfy.check_gpu(layout)
    assert check.name == "gpu"
    assert not check.ok and "gamma" in check.detail and "vram_device" in check.detail


def test_a_bare_integer_is_used_as_the_index(tmp_path):
    layout = devices_layout(tmp_path, vram_device=7)
    cfg = comfy.comfy_config(layout)
    assert cfg["vram_device_index"] == 7
    assert cfg.get("vram_device_name") is None
    ctask(layout)
    spec = comfy.ADAPTER.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    assert spec["vram_device_index"] == 7
    assert "vram_device_name" not in spec


def test_peak_vram_record_reads_the_configured_card(tmp_path, monkeypatch):
    layout = devices_layout(tmp_path, vram_device="alpha")
    ctask(layout)
    spec = comfy.ADAPTER.prepare(layout, testing.task(layout, "CF"), "CF.a1")
    calls = []

    def fake_used(index=None):
        calls.append(index)
        return {3: 2048, 5: 9000}.get(index)
    monkeypatch.setattr(plugin.gpu, "used_mb", fake_used)
    comfy.ADAPTER._record_peak_vram(spec)
    assert calls == [3]
    peak = json.loads((Path(spec["run_dir"]) / "vram_peak.json").read_text())
    assert peak["peak_mb"] == 2048


def test_check_gpu_reads_the_configured_card_and_its_own_floor(tmp_path, monkeypatch):
    layout = devices_layout(tmp_path, vram_device="alpha")
    monkeypatch.setattr(plugin.gpu, "available", lambda index=None: index == 3)
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: {3: 4096}.get(index))
    monkeypatch.setattr(plugin.gpu, "total_mb", lambda index=None: {3: 8192}.get(index))
    check = comfy.check_gpu(layout)
    assert check.ok and "alpha" in check.detail and "4096MiB free" in check.detail and "floor 5000MiB" in check.detail


def test_gpu_panel_reads_the_configured_card(tmp_path, monkeypatch):
    layout = devices_layout(tmp_path, vram_device="alpha")
    monkeypatch.setattr(plugin.gpu, "available", lambda index=None: index == 3)
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: {3: 2048}.get(index))
    monkeypatch.setattr(plugin.gpu, "total_mb", lambda index=None: {3: 8192}.get(index))
    panel = comfy.gpu_panel(layout)
    assert "alpha" in panel["title"]
    assert panel["progress"] == (6144, 8192)
