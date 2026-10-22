"""Content-addressed UI styles keep cached CSS in sync with rendered templates."""
from __future__ import annotations

import hashlib
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
STYLESHEET_PATH = STATIC_DIR / "style.css"


def stylesheet() -> tuple[str, bytes]:
    content = STYLESHEET_PATH.read_bytes()
    return hashlib.sha256(content).hexdigest()[:16], content


def stylesheet_url() -> str:
    version, _ = stylesheet()
    return f"/assets/control-center-{version}.css"
