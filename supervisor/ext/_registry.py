"""The registry an extension's `setup(registry)` receives, as extensions see it (docs/EXTENSION_API.md, Registry).
The loader's implementation is `supervisor.extensions.Registry`; a test keeps the two signatures identical."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ..adapters.base import Adapter
from ..paths import Layout


class Registry(Protocol):
    """The hooks an extension registers through, from `setup(registry)` only: importing an extension module must
    change nothing. Extensions never construct one."""

    layout: Layout

    def adapter(self, adapter: Adapter) -> None:
        """A job lane, registered under `adapter.name`; a name already taken is an error."""
        ...

    def python_job(self, name: str, fn: Callable[..., dict]) -> None:
        """A deterministic job `fn(root, run_dir, **params) -> dict` for `job: {adapter: python, fn: <name>}`."""
        ...

    def doctor_check(self, fn: Callable[[Layout], Any]) -> None:
        """`fn(layout) -> Check`, shown by `supervisor doctor` and the Control Center."""
        ...

    def overview_panel(self, fn: Callable[[Layout], dict]) -> None:
        """`fn(layout) -> {"title", "rows": [(label, value)], "progress"?}`, a block on the Overview."""
        ...

    def packet_screen(self, fn: Callable[..., None]) -> None:
        """`fn(layout, text, where, *, job=None, task=None, lane=None)`, raising to refuse a brief."""
        ...

    def projection(self, fn: Callable[[Layout], Path], path: str | None = None, name: str | None = None) -> None:
        """`fn(layout) -> Path`, an extra rendered markdown view."""
        ...

    def cli(self, app: Any, name: str) -> None:
        """A Typer app as the `supervisor <name> ...` command group."""
        ...

    def command(self, kind: str, fn: Callable[[Layout, dict, str], dict]) -> None:
        """An owner command kind `fn(layout, payload, actor) -> dict`, idempotent by `payload["_id"]`."""
        ...

    def inbox_kind(self, kind: str, label: str, page: str | None = None, answerable: bool = False,
                   show_evidence: bool = True, score_keys: tuple[str, ...] | list[str] = ()) -> None:
        """Claim an inbox item kind the extension creates."""
        ...

    def ui_router(self, router: Any, label: str, path: str) -> None:
        """A FastAPI router and its sidebar entry."""
        ...

    def template_dir(self, path: str | Path) -> None:
        """Jinja templates for the extension's pages, loaded after the framework's."""
        ...

    def static_dir(self, path: str | Path, prefix: str) -> None:
        """Static files served at `/static/<prefix>/...`."""
        ...
