"""The server watchdog (D-013 section 1.2): starts the configured `argv` as its own child, polls the configured
health URL until ready, holds the machine-wide GPU lease for as long as it runs, and stops on idle, on another
project's release request, or on the owner command `llm_endpoint.stop`. Built out in its own commit; `submit()`
calls `ensure_started(cfg)` unconditionally, so the adapter's own tests stub it rather than waiting on it."""
from __future__ import annotations


def ensure_started(cfg: dict) -> None:
    """A no-op placeholder until the watchdog itself is built (next commit): the design's §1.2 server-lifecycle
    behaviour belongs here, not in the adapter, which calls this unconditionally from submit()."""
