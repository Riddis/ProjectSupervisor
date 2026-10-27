"""`supervisor.ext.ui`: the UI kit an extension page uses so it looks, authenticates and writes like the framework's
own pages (docs/EXTENSION_API.md, Control Center pages). Needs the framework's `ui` extra (FastAPI); importing
`supervisor.ext` alone does not load it."""
from __future__ import annotations

from pathlib import Path

from ..paths import Layout
from ..ui import thumbs as _thumbs
from ..ui.auth import require_auth, require_csrf
from ..ui.deps import act, render
from ..ui.deps import encode_flash as flash

__all__ = ["act", "flash", "is_image", "render", "require_auth", "require_csrf", "thumbnail"]


def is_image(path: str | Path) -> bool:
    """Whether the Control Center can show `path` as an image (by its suffix)."""
    return _thumbs.is_image(Path(path))


def thumbnail(layout: Layout, path: str | Path, size: tuple[int, int] = (256, 256)) -> str | None:
    """The cache key of a thumbnail of `path` no larger than `size` (made on first use), served at
    `/thumb/<key>`; None for a file that is not a readable image inside the project root."""
    return _thumbs.ensure(layout, Path(path), size)
