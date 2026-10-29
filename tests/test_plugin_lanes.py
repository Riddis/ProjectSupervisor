"""plugin.agent_lane (phase 2 step 5, owner decision B): a read-only handle on a named agent lane for a plugin
that launches the agent itself (a JSON-RPC session, an exec with images). It carries the lane's executable, its
configured model and environment(): the lane's scrubbed copy of the environment, so no API key or other credential
`process.scrub_env()` strips reaches the child, whatever the parent environment holds."""
from __future__ import annotations

import os

import pytest
import yaml

from supervisor import plugin, policy
from supervisor.adapters import REGISTRY
from supervisor.adapters.process import NESTING_ENV

AGENT_LANES = sorted(name for name, adapter in REGISTRY.items() if hasattr(adapter, "agent_handle"))


def test_the_framework_has_agent_lanes_to_hand_out():
    assert len(AGENT_LANES) >= 2


@pytest.fixture
def planted(layout, monkeypatch):
    """Every credential shape the scrub knows, planted in the parent environment."""
    names = [f"PLANTED{suffix}" for suffix in policy.env_key_suffixes(layout)] + sorted(NESTING_ENV)
    for name in names:
        monkeypatch.setenv(name, "s3cr3t-value-that-must-not-leak")
    monkeypatch.setenv("PLANTED_HARMLESS", "fine")
    return names


@pytest.mark.parametrize("lane", AGENT_LANES)
def test_the_lane_environment_carries_no_credential(layout, planted, lane):
    env = plugin.agent_lane(layout, lane).environment()
    assert not [n for n in planted if n in env]
    assert "s3cr3t-value-that-must-not-leak" not in env.values()
    assert env.get("PLANTED_HARMLESS") == "fine" and env.get("PATH") == os.environ.get("PATH")


@pytest.mark.parametrize("lane", AGENT_LANES)
def test_each_environment_is_a_fresh_copy(layout, lane):
    handle = plugin.agent_lane(layout, lane)
    env = handle.environment()
    env["INJECTED"] = "x"
    assert "INJECTED" not in handle.environment() and "INJECTED" not in os.environ


def test_environment_is_a_copy_even_of_a_shared_mapping(layout, monkeypatch):
    """The handle copies whatever the lane hands it, so no lane implementation can leak a shared environment."""
    shared = {"PATH": "x"}

    class Lane:
        name = "shared_env_lane"

        def agent_handle(self, layout):
            return {"executable": "agent", "model": None, "environment": lambda: shared}
    monkeypatch.setitem(REGISTRY, Lane.name, Lane())
    plugin.agent_lane(layout, Lane.name).environment()["INJECTED"] = "x"
    assert shared == {"PATH": "x"}


@pytest.mark.parametrize("lane", AGENT_LANES)
def test_the_handle_names_the_executable_and_the_configured_model(layout, lane):
    engine = REGISTRY[lane].engine
    layout.config.mkdir(parents=True, exist_ok=True)
    (layout.config / "routing.yaml").write_text(yaml.safe_dump({engine: {"model": "model-under-test"}}),
                                                encoding="utf-8")
    handle = plugin.agent_lane(layout, lane)
    assert handle.name == lane and isinstance(handle.executable, str) and handle.executable
    assert handle.model == "model-under-test"


@pytest.mark.parametrize("lane", AGENT_LANES)
def test_the_handle_is_read_only(layout, lane):
    handle = plugin.agent_lane(layout, lane)
    for attr in ("name", "executable", "model"):
        with pytest.raises(AttributeError):
            setattr(handle, attr, "other")


@pytest.mark.parametrize("name,why", [("process", "not an agent lane"), ("python", "not an agent lane"),
                                      ("no_such_lane", "no lane")])
def test_only_agent_lanes_have_a_handle(layout, name, why):
    with pytest.raises(plugin.PluginError, match=why):
        plugin.agent_lane(layout, name)
