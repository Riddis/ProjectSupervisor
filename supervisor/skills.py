"""The framework's skills in a project (1.13.0). `supervisor init` stamps the template's skills into
`.claude/skills/` and records the hash of each file it wrote in `.claude/supervisor-skills.json`. Later releases ship
newer skills; `supervisor skills update` brings them in without losing a project's own edits:

- **missing:** the template ships it and the project has no copy. Update installs it.
- **current:** the copy equals the template. Update records it.
- **outdated:** the copy equals what was recorded, and the template has moved on. Update replaces it.
- **edited:** the copy differs from both, or was never recorded and differs. Update leaves it and says to merge by
  hand from the template.
- **retired:** recorded, but no longer shipped. Update leaves it.

A hash ignores line endings, so a checkout that turned LF into CRLF still counts as unedited. The copies are written
as `supervisor init` writes them: the `{{ name }}` and `{{ framework_version }}` placeholders filled in, LF."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from . import __version__, store
from .template import TEMPLATE_DIR

TEMPLATE_SKILLS = TEMPLATE_DIR / "_dot_claude" / "skills"
RECORD = Path(".claude") / "supervisor-skills.json"          # outside skills/, so it is never read as a skill
SKILLS = Path(".claude") / "skills"


@dataclass(frozen=True)
class Entry:
    rel: str             # the file's path under .claude/skills/, POSIX
    state: str           # current, missing, outdated, edited, retired; after update also installed, updated
    detail: str = ""


def _digest(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _name(root: Path) -> str:
    try:
        from . import project
        from .paths import Layout
        return project.name(Layout(root))
    except Exception:                                       # noqa: BLE001 - a bare folder still gets its own name
        return Path(root).name


def render(text: str, name: str) -> str:
    """A template file's text as `supervisor init` writes it."""
    return text.replace("{{ name }}", name).replace("{{ framework_version }}", __version__)


def shipped() -> dict[str, Path]:
    """The template's skill files, by their path under skills/."""
    if not TEMPLATE_SKILLS.is_dir():
        return {}
    return {p.relative_to(TEMPLATE_SKILLS).as_posix(): p for p in sorted(TEMPLATE_SKILLS.rglob("*")) if p.is_file()}


def _record(root: Path) -> dict[str, str]:
    try:
        return dict(json.loads((Path(root) / RECORD).read_text(encoding="utf-8")).get("files") or {})
    except (OSError, ValueError, AttributeError):
        return {}


def write_record(root: Path, files: dict[str, str]) -> None:
    path = Path(root) / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"framework": __version__, "files": dict(sorted(files.items()))}
    store.write_text(path, json.dumps(doc, indent=2) + "\n")


def record_written(root: Path, written: list[Path]) -> None:
    """After `supervisor init`: record the skill files it wrote, as written."""
    base = Path(root) / SKILLS
    files = _record(root)
    for p in written:
        try:
            rel = Path(p).relative_to(base).as_posix()
        except ValueError:
            continue
        files[rel] = _digest(Path(p).read_text(encoding="utf-8"))
    if files:
        write_record(root, files)


def status(root: Path) -> list[Entry]:
    """Every shipped or recorded skill file and its state, sorted by path."""
    root = Path(root)
    name, record, out = _name(root), _record(root), []
    ship = shipped()
    for rel, src in ship.items():
        want = _digest(render(src.read_text(encoding="utf-8"), name))
        dest = root / SKILLS / rel
        if not dest.is_file():
            out.append(Entry(rel, "missing"))
            continue
        have = _digest(dest.read_text(encoding="utf-8"))
        if have == want:
            out.append(Entry(rel, "current"))
        elif record.get(rel) == have:
            out.append(Entry(rel, "outdated"))
        else:
            out.append(Entry(rel, "edited", f"changed here; merge by hand from {src}"))
    for rel in sorted(set(record) - set(ship)):
        out.append(Entry(rel, "retired", "no longer shipped by the framework; left as it is"))
    return sorted(out, key=lambda e: e.rel)


def update(root: Path, dry_run: bool = False) -> list[Entry]:
    """Install missing skills and replace unedited outdated ones; leave edited and retired ones. Returns what it did
    (or would do, with `dry_run`) for each file."""
    root = Path(root)
    name, record, ship, out = _name(root), _record(root), shipped(), []
    for e in status(root):
        if e.state in ("missing", "outdated"):
            text = render(ship[e.rel].read_text(encoding="utf-8"), name)
            if not dry_run:
                dest = root / SKILLS / e.rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8", newline="\n")
                record[e.rel] = _digest(text)
            out.append(Entry(e.rel, "installed" if e.state == "missing" else "updated"))
        else:
            if e.state == "current":
                record[e.rel] = _digest((root / SKILLS / e.rel).read_text(encoding="utf-8"))
            out.append(e)
    if not dry_run:
        write_record(root, record)
    return out
