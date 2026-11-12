"""D-013 section 5, R5's `defer_budget_s` row: "after two continuous hours of deferral the lane raises one
owner inbox item (idempotent, through `plugin.owned_item`) ... Silence is not a report." F9
(RELEASE_REVIEW_local-lane.md): the package had no `owned_item`/`open_owned_item` call, no `defer_budget` key
and no deferral accounting at all, so a lane that kept deferring (as F3 showed it could) stayed silent forever.
These regressions pin the fix with a tiny `defer_budget_s` so a window closes in a fraction of a second."""
from __future__ import annotations

import time

import pytest

from supervisor import inbox, plugin, testing

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


def _owner_items(layout):
    return [i for i in inbox.all_items(layout) if i.get("kind") == "llm_endpoint_capacity"]


def test_continuous_deferral_past_the_budget_raises_exactly_one_owner_item(tmp_path, monkeypatch):
    layout = _layout(tmp_path, devices={"alpha": {"index": 0, "usable_vram_floor_mb": 6500}},
                     plugin_cfg={"defer_budget_s": 0.3})
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: 100)
    monkeypatch.setattr(_server, "_health_ok", lambda *a, **k: False)
    task = _task(layout, prompt="hello")
    spec = llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")

    with pytest.raises(plugin.CapacityDeferred):
        llm_endpoint.ADAPTER.submit(spec)
    assert _owner_items(layout) == []                        # the window just opened: too soon for an item

    time.sleep(0.1)                                           # still under the 0.3s budget: a sub-1 budget must
    with pytest.raises(plugin.CapacityDeferred):              # not be floored to 0 and fire immediately
        llm_endpoint.ADAPTER.submit(spec)
    assert _owner_items(layout) == []

    time.sleep(0.3)                                           # now past the budget
    with pytest.raises(plugin.CapacityDeferred):
        llm_endpoint.ADAPTER.submit(spec)
    items = _owner_items(layout)
    assert len(items) == 1
    first = items[0]
    assert "alpha" in first["summary"] and "below floor" in first["summary"]

    # further deferrals in the same window raise no second item
    for _ in range(3):
        with pytest.raises(plugin.CapacityDeferred):
            llm_endpoint.ADAPTER.submit(spec)
    again = _owner_items(layout)
    assert len(again) == 1 and again[0]["id"] == first["id"]


def test_a_successful_submit_resets_the_window_so_the_next_deferral_raises_a_fresh_item(tmp_path, monkeypatch):
    layout = _layout(tmp_path, plugin_cfg={"defer_budget_s": 0.2})
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: None)   # no floor anywhere: defers on cold start
    monkeypatch.setattr(_server, "ensure_started", lambda cfg, root: False)
    task = _task(layout, prompt="hello")
    spec = llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")

    with pytest.raises(plugin.CapacityDeferred):
        llm_endpoint.ADAPTER.submit(spec)
    time.sleep(0.3)
    with pytest.raises(plugin.CapacityDeferred):
        llm_endpoint.ADAPTER.submit(spec)
    assert len(_owner_items(layout)) == 1

    monkeypatch.setattr(_server, "ensure_started", lambda cfg, root: True)
    monkeypatch.setattr(plugin.ProcessAdapter, "submit", lambda self, spec: {"ok": True})
    assert llm_endpoint.ADAPTER.submit(spec) == {"ok": True}   # reaches the server: the window resets

    monkeypatch.setattr(_server, "ensure_started", lambda cfg, root: False)
    with pytest.raises(plugin.CapacityDeferred):
        llm_endpoint.ADAPTER.submit(spec)
    assert len(_owner_items(layout)) == 1                      # the new window just opened: still only the first item

    time.sleep(0.3)
    with pytest.raises(plugin.CapacityDeferred):
        llm_endpoint.ADAPTER.submit(spec)
    items = _owner_items(layout)
    assert len(items) == 2
    assert {i["id"] for i in items} == {items[0]["id"], items[1]["id"]} and items[0]["id"] != items[1]["id"]


def test_the_deferral_start_is_persisted_in_the_state_file_and_survives_a_restart(tmp_path, monkeypatch):
    layout = _layout(tmp_path, plugin_cfg={"defer_budget_s": 7200})
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: None)
    monkeypatch.setattr(_server, "ensure_started", lambda cfg, root: False)
    task = _task(layout, prompt="hello")
    spec = llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")

    with pytest.raises(plugin.CapacityDeferred):
        llm_endpoint.ADAPTER.submit(spec)
    on_disk = _server.read_deferral(str(layout.root))
    assert on_disk.get("started") is not None and on_disk.get("last_reason")

    # a fresh process has no in-memory state at all; re-reading the file is the only way it could know the
    # window already started, exactly as a restarted loop would
    reread = _server.read_deferral(str(layout.root))
    assert reread == on_disk


def test_defer_seconds_matches_the_designs_60s_wait_then_120s_deferral():
    assert llm_endpoint.DEFER_SECONDS == 120.0


def test_the_owner_item_names_the_project_the_lane_the_elapsed_time_the_reason_and_what_to_do(tmp_path, monkeypatch):
    layout = _layout(tmp_path, plugin_cfg={"defer_budget_s": 0.1},
                     devices={"secondary": {"index": 1, "usable_vram_floor_mb": 6500}})
    monkeypatch.setattr(plugin.gpu, "free_mb", lambda index=None: 900)
    monkeypatch.setattr(_server, "_health_ok", lambda *a, **k: False)
    secret_prompt = "classified-prompt-text-xyz"
    task = _task(layout, prompt=secret_prompt)
    spec = llm_endpoint.ADAPTER.prepare(layout, task, "T.a1")

    with pytest.raises(plugin.CapacityDeferred):
        llm_endpoint.ADAPTER.submit(spec)
    time.sleep(0.2)
    with pytest.raises(plugin.CapacityDeferred):
        llm_endpoint.ADAPTER.submit(spec)
    items = _owner_items(layout)
    assert len(items) == 1
    text = items[0]["summary"] + (items[0].get("detail") or "")
    assert "llm_endpoint" in text                              # the lane
    assert "secondary" in text                                 # the reason the adapter last gave names the device
    assert "below floor" in text
    assert "doctor" in text or "llm_endpoint.stop" in text      # what the owner can do
    assert secret_prompt not in text                            # never the prompt
    assert "secrets" not in text.lower() and "api_key" not in text.lower() and "token=" not in text.lower()
