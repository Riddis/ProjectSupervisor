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
    task = _task(layout, prompt="x" * 1000)                 # ~250-330 estimated tokens at 3-4 chars/token
    with pytest.raises(plugin.PluginError, match="budget"):
        llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")


def test_prepare_refuses_a_code_heavy_prompt_at_the_package_defaults_and_names_the_ratio_it_assumed(tmp_path):
    """F7 (RELEASE_REVIEW_local-lane.md): a flat 4 chars/token admits 21,296 characters at the package defaults
    (0.9 x 8192 - 2048 = 5324 tokens). Code, diffs, YAML and non-English text tokenise at ~2.5-3.5 chars/token
    with every tokenizer this lane will meet, so 21,000 characters of code-shaped text is really ~7,000 tokens -
    the server would then shift or truncate the prompt silently, the exact failure D-013 section 2.1 says must
    fail loudly. This is the reviewer's own probe
    (runs/T5-04/T5-04.a3/probes/test_llm_endpoint_probes.py::test_token_budget_admits_a_code_heavy_prompt_that_
    overflows_at_three_chars_per_token), made into a tracked regression: it must be refused at the package
    defaults, with the ratio the refusal assumed stated in its message."""
    layout = _layout(tmp_path, plugin_cfg={})                      # package defaults: context_tokens=8192, max_tokens=2048
    code = ("if (x[i] != y[j]) { z += f(a, b); }\n" * 600)[:21000]
    task = _task(layout, prompt=code)
    with pytest.raises(plugin.PluginError, match="chars/token") as excinfo:
        llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")
    assert "3 chars/token" in str(excinfo.value)


def test_chars_per_token_is_a_settings_key_that_reaches_the_computation(tmp_path):
    """The knob is honoured, not just defaulted: the same code-heavy prompt that overflows at 3 chars/token
    (the test above) is admitted when a project overrides the ratio to 4 - proving `chars_per_token` reaches
    `prepare()`'s budget arithmetic rather than sitting unused in config.py."""
    code = ("if (x[i] != y[j]) { z += f(a, b); }\n" * 600)[:21000]
    admitted = _layout(tmp_path / "admitted", plugin_cfg={"chars_per_token": 4})
    spec = llm_endpoint.ADAPTER.prepare(admitted, _task(admitted, prompt=code), "T.a1")
    assert spec["adapter"] == "llm_endpoint"                       # 21000 // 4 = 5250, under the 5324 budget

    refused = _layout(tmp_path / "refused", plugin_cfg={"chars_per_token": 3})
    with pytest.raises(plugin.PluginError, match="3 chars/token"):
        llm_endpoint.ADAPTER.prepare(refused, _task(refused, prompt=code), "T.a1")


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


def test_submit_reaches_the_server_despite_low_free_vram_when_the_endpoint_is_already_healthy(tmp_path, monkeypatch):
    """F3 (RELEASE_REVIEW_local-lane.md): the floor is a start check ("free VRAM says whether a start will
    *work*", LOCAL_MODEL_LANE.md section 4), not a per-request one. A resident server holds exactly the VRAM
    the floor guards, so a floor applied regardless of health defers every request against a server that is
    already up and answering - the case the original test_adapter.py never combined."""
    layout = _layout(tmp_path, devices={"alpha": {"index": 3, "usable_vram_floor_mb": 6500},
                                        "beta": {"index": 5, "usable_vram_floor_mb": 6500}})
    task = _task(layout, prompt="hello")
    spec = llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: 900)          # the resident server's own VRAM
    monkeypatch.setattr(_server, "_health_ok", lambda *a, **k: True)
    monkeypatch.setattr(_server, "ensure_started", lambda cfg, root: True)
    monkeypatch.setattr(plugin.ProcessAdapter, "submit", lambda self, spec: {"ok": True})
    assert llm_endpoint.ADAPTER.submit(spec) == {"ok": True}


def test_submit_refuses_with_the_floors_own_message_when_the_endpoint_is_not_healthy_and_vram_is_short(tmp_path, monkeypatch):
    """The complement the fix must keep: when the endpoint is not up (a start would actually be attempted),
    VRAM below the floor still refuses the start with the floor's own message - so the fix cannot be "drop
    the floor", only "stop applying it once the server is already serving requests"."""
    layout = _layout(tmp_path, devices={"alpha": {"index": 3, "usable_vram_floor_mb": 6500},
                                        "beta": {"index": 5, "usable_vram_floor_mb": 6500}})
    task = _task(layout, prompt="hello")
    spec = llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: 900)
    monkeypatch.setattr(_server, "_health_ok", lambda *a, **k: False)
    with pytest.raises(plugin.CapacityDeferred, match="below floor 6500MiB"):
        llm_endpoint.ADAPTER.submit(spec)


def test_submit_runs_a_second_job_on_one_load_when_the_server_stays_healthy_and_full(tmp_path, monkeypatch):
    """D-013 section 1.2's whole reason for idle_stop_s: a chain of jobs against one loaded server pays one
    cold start. Two submits against one already-healthy server, VRAM "full" (every card below every
    configured floor) throughout, both reach the endpoint - the thing F3 made impossible."""
    layout = _layout(tmp_path, devices={"alpha": {"index": 3, "usable_vram_floor_mb": 6500},
                                        "beta": {"index": 5, "usable_vram_floor_mb": 6500}})
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: 900)   # the resident server's own VRAM, always
    monkeypatch.setattr(_server, "_health_ok", lambda *a, **k: True)
    monkeypatch.setattr(_server, "ensure_started", lambda cfg, root: True)
    reached = []
    monkeypatch.setattr(plugin.ProcessAdapter, "submit", lambda self, spec: reached.append(spec["task_id"]) or {"ok": True})
    for i in range(2):
        task = _task(layout, tid=f"T{i}", prompt="hello")
        spec = llm_endpoint.ADAPTER.prepare(layout, task, f"T{i}.a1")
        assert llm_endpoint.ADAPTER.submit(spec) == {"ok": True}
    assert reached == ["T0", "T1"]                                      # both jobs reached the server, one load
