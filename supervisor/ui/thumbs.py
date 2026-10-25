"""Thumbnail cache: Pillow, cached once per source file under runs/thumbs/.

The client never sends a filesystem path. Server-side code (template rendering) resolves a real path,
calls `ensure()`, and embeds the returned opaque cache key in an `<img src="/thumb/{key}">` URL; the `/thumb/{key}`
route only ever opens files inside the cache directory, by key, never by a path the browser supplied."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image

from .._vendor.contract import contained
from ..paths import Layout

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def cache_dir(layout: Layout) -> Path:
    d = layout.root / "runs" / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


def _key(path: Path, size: tuple[int, int], mtime: float) -> str:
    h = hashlib.sha256(f"{path}|{size}|{mtime}".encode()).hexdigest()
    return f"{h}.jpg"


def ensure(layout: Layout, source: Path, size: tuple[int, int] = (256, 256)) -> str | None:
    """Return the cache key for a thumbnail of `source` (generating it if needed), or None if it is not a readable
    image inside the repository (never serves or reads anything outside layout.root)."""
    source = Path(source)
    if not is_image(source) or not contained(source, [layout.root], root=layout.root):
        return None
    try:
        mtime = source.stat().st_mtime
    except OSError:
        return None
    key = _key(source, size, mtime)
    out = cache_dir(layout) / key
    if out.exists():
        return key
    try:
        with Image.open(source) as im:
            im = im.convert("RGB")
            im.thumbnail(size)
            im.save(out, "JPEG", quality=85)
    except (OSError, ValueError):
        return None
    return key


def ensure_bytes(layout: Layout, data: bytes, size: tuple[int, int] = (256, 256)) -> str | None:
    """Like ensure(), for bytes already read and verified by the caller (supervisor.ui.delivery): the key is the
    content hash, so the thumbnail always shows exactly those bytes, whatever the file on disk holds by then."""
    key = f"{hashlib.sha256(data).hexdigest()}-{size[0]}x{size[1]}.jpg"
    out = cache_dir(layout) / key
    if out.exists():
        return key
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail(size)
            im.save(out, "JPEG", quality=85)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    return key


def path_for_key(layout: Layout, key: str) -> Path | None:
    if "/" in key or "\\" in key or ".." in key:
        return None
    p = cache_dir(layout) / key
    return p if p.exists() else None
