"""Adapters run one task attempt on one resource. Same interface for all:

    spec   = adapter.prepare(layout, task, attempt_id)   # writes the packet/run dir, returns a JSON-able spec
    handle = adapter.submit(spec)                          # launches; returns a JSON-able handle (pid+create_time, ...)
    alive  = adapter.alive(handle)                         # is the external work still running?
    result = adapter.collect(spec, handle)                 # None while running; a Result once finished

Adapters never touch state/; the loop ingests Results into the task store. The one deliberate exception is `human`:
it writes the inbox item and sets WAITING_FOR_HUMAN itself, because the owner's answer is not a delivery digest to
verify/review — collect() just waits for it.

Built-in lanes: python (deterministic in-process jobs), process (any subprocess), claude_cli, claude_api, codex_cli,
human. A project adds its own lanes through plugins (supervisor.plugins.Registry.adapter).
"""
from .base import Adapter, Result, REGISTRY, get  # noqa: F401
from . import python as _python  # noqa: F401  (registers "python")
from . import process as _process  # noqa: F401  (registers "process")
from . import claude_cli as _claude  # noqa: F401  (registers "claude_cli")
from . import claude_api as _claude_api  # noqa: F401  (registers "claude_api")
from . import codex_cli as _codex  # noqa: F401  (registers "codex_cli")
from . import human as _human  # noqa: F401  (registers "human")
