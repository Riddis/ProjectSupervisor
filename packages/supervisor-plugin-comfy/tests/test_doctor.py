"""The generic comfy doctor checks (install pins, the HTTP API, ffmpeg, the GPU probe) and the overview panel.
Ported from a project-local extension's tests, onto supervisor.plugin and supervisor.testing only - no framework
internal: every test here calls this package's own functions directly. The pinned node names used here
(`node-a`, `node-b`) are invented for the test; a real project's own node list is configuration
(`plugins.comfy.pinned_nodes`), never a default this package ships."""
from __future__ import annotations

import json

import pytest
import requests

from supervisor import plugin

import supervisor_plugin_comfy as comfy

SHA = "a" * 40
PINNED_NODES = ["node-a", "node-b"]
NODE_SHAS = {n: f"{i}" * 40 for i, n in enumerate(PINNED_NODES, start=1)}


def test_comfy_config_defaults_when_the_section_is_absent(layout):
    """`comfy_config` itself, called directly, on a project with no `plugins.comfy` section at all."""
    cfg = comfy.comfy_config(layout)
    assert cfg["base_url"] == "http://127.0.0.1:8188"
    assert cfg["vram_floor_mb"] == 0


def fake_comfy(layout, monkeypatch, pins, heads, nodes=PINNED_NODES):
    """A ComfyUI tree that looks installed, a pin manifest with `pins`, and `heads` as the git HEAD of each dir."""
    root = layout.root / "ComfyUI"
    (root / "custom_nodes").mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("", encoding="utf-8")
    for node in nodes:
        (root / "custom_nodes" / node).mkdir(exist_ok=True)
    pins_file = layout.root / "comfy_pins.json"
    if pins is not None:
        pins_file.write_text(pins if isinstance(pins, str) else json.dumps(pins), encoding="utf-8")
    monkeypatch.setattr(comfy, "comfy_config",
                        lambda lay: {"root": str(root), "base_url": "http://127.0.0.1:8188",
                                    "pinned_nodes": nodes, "pins_file": "comfy_pins.json"})
    monkeypatch.setattr(comfy, "_git_head", lambda repo: heads.get(repo.name))
    return root


def all_heads(root_name="ComfyUI", nodes=PINNED_NODES):
    return {root_name: SHA, **{n: NODE_SHAS[n] for n in nodes}}


def test_comfy_install_green_when_every_pin_matches(layout, monkeypatch):
    fake_comfy(layout, monkeypatch, {"ComfyUI": SHA, **NODE_SHAS}, all_heads())
    check = comfy.check_comfy_install(layout)
    assert check.ok and "pinned" in check.detail


def test_comfy_install_green_with_no_pinned_nodes_at_all(layout, monkeypatch):
    """The neutral default: a project that configures no `pinned_nodes` needs only ComfyUI itself pinned."""
    fake_comfy(layout, monkeypatch, {"ComfyUI": SHA}, all_heads(nodes=[]), nodes=[])
    check = comfy.check_comfy_install(layout)
    assert check.ok


def test_comfy_install_fails_without_a_pin_manifest(layout, monkeypatch):
    fake_comfy(layout, monkeypatch, None, all_heads())
    check = comfy.check_comfy_install(layout)
    assert not check.ok and "missing" in check.detail


def test_comfy_install_fails_on_an_incomplete_pin_manifest(layout, monkeypatch):
    fake_comfy(layout, monkeypatch, {"ComfyUI": SHA}, all_heads())           # nodes unpinned: not a pinned install
    check = comfy.check_comfy_install(layout)
    assert not check.ok and PINNED_NODES[0] in check.detail


def test_comfy_install_fails_on_a_malformed_pin_manifest(layout, monkeypatch):
    fake_comfy(layout, monkeypatch, "{not json", all_heads())
    assert not comfy.check_comfy_install(layout).ok
    fake_comfy(layout, monkeypatch, {"ComfyUI": "HEAD", **NODE_SHAS}, all_heads())   # not a commit
    assert not comfy.check_comfy_install(layout).ok


def test_comfy_install_fails_when_a_head_is_unreadable(layout, monkeypatch):
    pins = {"ComfyUI": SHA, **NODE_SHAS}
    fake_comfy(layout, monkeypatch, pins, {})                                # no git / not a checkout anywhere
    check = comfy.check_comfy_install(layout)
    assert not check.ok and "HEAD" in check.detail
    heads = all_heads(); heads.pop(PINNED_NODES[0])                          # only one node's HEAD unreadable
    fake_comfy(layout, monkeypatch, pins, heads)
    check = comfy.check_comfy_install(layout)
    assert not check.ok and PINNED_NODES[0] in check.detail


def test_comfy_install_fails_when_a_node_is_off_its_pin(layout, monkeypatch):
    heads = all_heads(); heads[PINNED_NODES[1]] = "f" * 40
    fake_comfy(layout, monkeypatch, {"ComfyUI": SHA, **NODE_SHAS}, heads)
    check = comfy.check_comfy_install(layout)
    assert not check.ok and PINNED_NODES[1] in check.detail


def test_comfy_install_fails_when_root_is_missing(layout, monkeypatch):
    """No `plugins.comfy.root` configured: the failure must name the key to set, and it must say so
    without touching the filesystem at all - not even constructing a `Path` from the unset value."""
    class _NoFilesystemAccess:
        def __init__(self, *_a, **_k):
            raise AssertionError("check_comfy_install touched the filesystem with no root configured")
    monkeypatch.setattr(comfy, "Path", _NoFilesystemAccess)
    check = comfy.check_comfy_install(layout)
    assert not check.ok
    assert "plugins.comfy.root" in check.detail


# ---------------------------------------------------------------- comfy_api

def test_comfy_api_fails_cleanly_when_unreachable(layout, monkeypatch):
    monkeypatch.setattr(comfy.requests, "get",
                        lambda url, timeout=None: (_ for _ in ()).throw(requests.ConnectionError("down")))
    check = comfy.check_comfy_api(layout)
    assert not check.ok and "unreachable" in check.detail


def test_comfy_api_ok_when_reachable(layout, monkeypatch):
    class Resp:
        def raise_for_status(self):
            pass
    monkeypatch.setattr(comfy.requests, "get", lambda url, timeout=None: Resp())
    assert comfy.check_comfy_api(layout).ok


# ---------------------------------------------------------------- ffmpeg

def test_ffmpeg_fails_when_root_is_unset(layout):
    check = comfy.check_ffmpeg(layout)
    assert not check.ok and "missing" in check.detail


def test_ffmpeg_ok_when_the_binary_runs(layout, monkeypatch, tmp_path):
    root = tmp_path / "ffmpeg"
    exe = root / "bin" / "ffmpeg.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(comfy, "comfy_config", lambda lay: {"ffmpeg": {"root": str(root)}})

    class Out:
        returncode, stdout = 0, "ffmpeg version 6.0\n"
    monkeypatch.setattr(comfy.subprocess, "run", lambda *a, **kw: Out())
    check = comfy.check_ffmpeg(layout)
    assert check.ok and "ffmpeg version" in check.detail


# ---------------------------------------------------------------- gpu

def test_gpu_check_fails_when_nvidia_smi_is_unavailable(layout, monkeypatch):
    monkeypatch.setattr(plugin.gpu, "available", lambda index=None: False)
    check = comfy.check_gpu(layout)
    assert not check.ok and "unavailable" in check.detail


def test_gpu_check_ok_when_nvidia_smi_answers(layout, monkeypatch):
    monkeypatch.setattr(plugin.gpu, "available", lambda index=None: True)
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: 4096)
    monkeypatch.setattr(plugin.gpu, "total_mb", lambda index=None: 8192)
    check = comfy.check_gpu(layout)
    assert check.ok and "4096MiB free" in check.detail and "8192MiB total" in check.detail


def test_gpu_panel_reports_used_and_free(layout, monkeypatch):
    monkeypatch.setattr(plugin.gpu, "available", lambda index=None: True)
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: 2048)
    monkeypatch.setattr(plugin.gpu, "total_mb", lambda index=None: 8192)
    panel = comfy.gpu_panel(layout)
    assert panel["title"] == "GPU" and panel["progress"] == (6144, 8192)


def test_gpu_panel_without_nvidia_smi(layout, monkeypatch):
    monkeypatch.setattr(plugin.gpu, "available", lambda index=None: False)
    assert comfy.gpu_panel(layout)["rows"] == [("VRAM", "nvidia-smi unavailable")]
