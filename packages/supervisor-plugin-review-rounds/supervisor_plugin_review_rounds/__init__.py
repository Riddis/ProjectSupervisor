"""review_rounds: a Supervisor plugin package.

Import only `supervisor.plugin` (the public plugin API), and register only inside setup(): importing this package
must change nothing. Everything setup() registers is declared in supervisor-plugin.toml."""
from __future__ import annotations

from supervisor import plugin  # noqa: F401 - imported for the plugins that move in here next


def setup(registry) -> None:
    """Registers nothing yet: the review grid, its commands, CLI, routes and check move in next (boundary plan
    phase 5, step 5)."""
