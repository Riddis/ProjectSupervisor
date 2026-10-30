"""A read-only handle on an agent lane (docs/PLUGIN_API.md, Agent lanes): for a plugin that launches an agent
itself, in a way one job step cannot express (a JSON-RPC session, an exec with images), under the lane's
environment rules. The lane's name is passed in by the plugin; nothing here names one."""
from __future__ import annotations

from collections.abc import Callable

from ..plugins import PluginError
from ..paths import Layout


class AgentLane:
    """A named agent lane as a plugin may use it: `name`, `executable` (the agent's executable as the lane
    resolves it), `model` (the model routing.yaml configures for the lane's engine, or None) and `environment()`.
    Read-only; obtained from `agent_lane()`, never constructed by a plugin."""

    __slots__ = ("_environment", "_executable", "_model", "_name")

    def __init__(self, name: str, executable: str, model: str | None, environment: Callable[[], dict]):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_executable", executable)
        object.__setattr__(self, "_model", model)
        object.__setattr__(self, "_environment", environment)

    def __setattr__(self, name, value):
        raise AttributeError(f"an agent lane handle is read-only ({name})")

    @property
    def name(self) -> str:
        return self._name

    @property
    def executable(self) -> str:
        return self._executable

    @property
    def model(self) -> str | None:
        return self._model

    def environment(self) -> dict:
        """A fresh copy of the environment the lane gives its agent: the loop's environment with every API key,
        token and nesting marker the lane strips (`process.scrub_env()`, policy `env_key_suffixes`) removed. Pass it
        as the child's whole environment; changing the copy changes nothing else."""
        return dict(self._environment())

    def __repr__(self) -> str:
        return f"AgentLane({self._name!r}, executable={self._executable!r}, model={self._model!r})"


def agent_lane(layout: Layout, name: str) -> AgentLane:
    """The handle on agent lane `name` (a registered lane that runs an agent CLI). Raises PluginError for a name
    that is no lane, or a lane that is not an agent lane."""
    from ..adapters import REGISTRY
    adapter = REGISTRY.get(name)
    if adapter is None:
        raise PluginError(f"no lane {name!r} is registered")
    handle = getattr(adapter, "agent_handle", None)
    if handle is None:
        raise PluginError(f"lane {name!r} is not an agent lane: it has no agent to hand out")
    info = handle(layout)
    return AgentLane(name, str(info["executable"]), info.get("model"), info["environment"])
