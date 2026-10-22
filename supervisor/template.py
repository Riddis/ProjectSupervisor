"""`supervisor init`: stamp the project template (supervisor/template/**) into a directory. Files are copied
verbatim except `{{ name }}` / `{{ framework_version }}` placeholders in text files. Existing files are kept
unless `force`."""
from __future__ import annotations

from pathlib import Path

from . import __version__

TEMPLATE_DIR = Path(__file__).resolve().parent / "template"
TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".txt", ".py", ".bat", ".ps1", ".json", ".gitignore", ""}


def files() -> list[Path]:
    return sorted(p for p in TEMPLATE_DIR.rglob("*") if p.is_file())


def stamp(target: Path, name: str, force: bool = False) -> list[Path]:
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for src in files():
        rel = src.relative_to(TEMPLATE_DIR)
        # dot-files cannot live in a Python package tree under some packaging tools: `_gitignore` → `.gitignore`
        parts = [("." + p[5:]) if p.startswith("_dot_") else p for p in rel.parts]
        dest = target.joinpath(*parts)
        if dest.exists() and not force:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() in TEXT_SUFFIXES:
            text = src.read_text(encoding="utf-8")
            text = text.replace("{{ name }}", name).replace("{{ framework_version }}", __version__)
            dest.write_text(text, encoding="utf-8", newline="\n")
        else:
            dest.write_bytes(src.read_bytes())
        written.append(dest)
    for d in ("state/tasks", "state/decisions", "state/issues", "state/inbox", "state/commands/applied",
              "state/runtime/jobs", "state/conversations", "runs", "secrets", "uploads", "docs/plan", "docs/runbooks"):
        (target / d).mkdir(parents=True, exist_ok=True)
    return written
