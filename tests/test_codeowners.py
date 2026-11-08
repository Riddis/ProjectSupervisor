"""CODEOWNERS covers the plugin surface (supervisor/plugin/, supervisor/plugins.py, supervisor/plugin_check.py,
supervisor/migrations.py, docs/PLUGIN_API.md, docs/PLUGIN_STANDARD.md) and still covers /packages/ (the boundary
plan's "Open to contributors" phase)."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

COVERED = (
    "/packages/",
    "/supervisor/plugin/",
    "/supervisor/plugins.py",
    "/supervisor/plugin_check.py",
    "/supervisor/migrations.py",
    "/docs/PLUGIN_API.md",
    "/docs/PLUGIN_STANDARD.md",
)


def _lines() -> list[str]:
    path = REPO / ".github" / "CODEOWNERS"
    assert path.is_file(), ".github/CODEOWNERS is missing"
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_still_covers_packages():
    lines = _lines()
    assert any(line.startswith("/packages/") for line in lines)


def test_covers_the_plugin_surface():
    lines = _lines()
    for entry in COVERED:
        if entry == "/packages/":
            continue
        assert any(line.startswith(entry) for line in lines), f"{entry} is not covered by CODEOWNERS"


def test_every_covered_path_names_an_owner():
    lines = _lines()
    for line in lines:
        parts = line.split()
        assert len(parts) >= 2, f"{line!r} names no owner"
        assert all(part.startswith("@") for part in parts[1:])
