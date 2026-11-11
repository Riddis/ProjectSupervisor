"""The llm_endpoint adapter's prepare()/submit(): the job contract of D-013 section 2.1 (prompt/schema/output_file,
the context-budget arithmetic that fails an attempt rather than silently truncating) and the VRAM preflight of
section 1 ("both cards, each against its own floor" - unlike the framework's single-device GPU-locked lanes)."""
from __future__ import annotations

import json

import pytest

from supervisor import plugin, testing

import supervisor_plugin_llm_endpoint as llm_endpoint
from supervisor_plugin_llm_endpoint import _server


def _layout(tmp_path, *, devices=None, plugin_cfg=None):
    routing = {"plugins": {"llm_endpoint": plugin_cfg or {}}}
    if devices is not None:
        routing["resources"] = {"gpu": {"devices": devices}}
    return testing.project(tmp_path, plugins=["llm_endpoint"], routing=routing)


def _task(layout, tid="T", **job):
    testing.make_task(layout, tid, job={"adapter": "llm_endpoint", **job}, cost_category="local", artifacts=[])
    return testing.task(layout, tid)


def test_prepare_writes_the_request_and_the_worker_argv(tmp_path):
    layout = _layout(tmp_path, plugin_cfg={"base_url": "http://127.0.0.1:8799", "model": "demo-model"})
    (layout.root / "prompt.md").write_text("summarise this\n", encoding="utf-8")
    task = _task(layout, prompt_file="prompt.md", output_file="docs/out.md", max_tokens=64)
    spec = llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")
    assert spec["adapter"] == "llm_endpoint"
    assert spec["output_file"] == "docs/out.md"
    from pathlib import Path
    req = json.loads((Path(spec["run_dir"]) / "request.json").read_text(encoding="utf-8"))
    assert req["base_url"] == "http://127.0.0.1:8799"
    assert req["model"] == "demo-model"
    assert req["prompt"] == "summarise this\n"
    assert req["max_tokens"] == 64
    assert Path(spec["argv"][1]).name == "_worker.py"


def test_prepare_fails_the_attempt_when_the_prompt_exceeds_the_context_budget(tmp_path):
    layout = _layout(tmp_path, plugin_cfg={"context_tokens": 100, "max_tokens": 50})
    task = _task(layout, prompt="x" * 1000)                 # ~250 estimated tokens at 4 chars/token
    with pytest.raises(plugin.PluginError, match="budget"):
        llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")


def test_submit_defers_when_any_configured_cards_free_vram_is_below_its_own_floor(tmp_path, monkeypatch):
    layout = _layout(tmp_path, devices={"alpha": {"index": 3, "usable_vram_floor_mb": 5000},
                                        "beta": {"index": 5, "usable_vram_floor_mb": 4000}})
    task = _task(layout, prompt="hello")
    spec = llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: {3: 9000, 5: 3500}.get(index))
    with pytest.raises(plugin.CapacityDeferred, match="beta"):
        llm_endpoint.ADAPTER.submit(spec)


def test_submit_admits_when_every_configured_card_meets_its_floor(tmp_path, monkeypatch):
    layout = _layout(tmp_path, devices={"alpha": {"index": 3, "usable_vram_floor_mb": 5000},
                                        "beta": {"index": 5, "usable_vram_floor_mb": 4000}})
    task = _task(layout, prompt="hello")
    spec = llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: {3: 9000, 5: 9000}.get(index))
    monkeypatch.setattr(_server, "ensure_started", lambda cfg, root: True)
    monkeypatch.setattr(plugin.ProcessAdapter, "submit", lambda self, spec: {"ok": True})
    assert llm_endpoint.ADAPTER.submit(spec) == {"ok": True}


def test_submit_with_no_devices_configured_falls_back_to_the_shared_floor(tmp_path, monkeypatch):
    layout = _layout(tmp_path, plugin_cfg={})
    task = _task(layout, prompt="hello")
    spec = llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: 100)
    monkeypatch.setattr(_server, "ensure_started", lambda cfg, root: True)
    monkeypatch.setattr(plugin.ProcessAdapter, "submit", lambda self, spec: {"ok": True})
    assert llm_endpoint.ADAPTER.submit(spec) == {"ok": True}      # no floor configured anywhere: nothing to defer on


def test_submit_defers_rather_than_block_while_the_server_is_cold_starting(tmp_path, monkeypatch):
    layout = _layout(tmp_path, plugin_cfg={})
    task = _task(layout, prompt="hello")
    spec = llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: None)
    monkeypatch.setattr(_server, "ensure_started", lambda cfg, root: False)
    with pytest.raises(plugin.CapacityDeferred, match="not ready"):
        llm_endpoint.ADAPTER.submit(spec)
