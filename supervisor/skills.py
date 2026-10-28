"""The framework's skills in a project (1.13.0). `supervisor init` stamps the template's skills into
`.claude/skills/` and records the hash of each file it wrote in `.claude/supervisor-skills.json`. Later releases ship
newer skills; `supervisor skills update` brings them in without losing a project's own edits:

- **missing:** the template ships it and the project has no copy. Update installs it.
- **current:** the copy equals the template. Update records it.
- **outdated:** the copy equals what was recorded, or a version an earlier release shipped
  (`supervisor/skills_history.json`), and the template has moved on. Update replaces it.
- **edited:** the copy differs from both, or was never recorded and differs. Update leaves it and says to merge by
  hand from the template.
- **retired:** recorded, but no longer shipped. Update leaves it.

A hash ignores line endings, so a checkout that turned LF into CRLF still counts as unedited. The copies are written
as `supervisor init` writes them: the `{{ name }}` and `{{ framework_version }}` placeholders filled in, LF. Update
commits what it wrote (and the record), naming only those paths, when the project is its own repository."""
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
HISTORY = Path(__file__).resolve().parent / "skills_history.json"   # tools/skills_history.py writes it


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


def history() -> dict[str, set[str]]:
    """The hashes of every version of each skill file that earlier releases shipped."""
    try:
        files = json.loads(HISTORY.read_text(encoding="utf-8")).get("files") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {rel: set(hashes) for rel, hashes in files.items()}


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
    name, record, earlier, out = _name(root), _record(root), history(), []
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
        elif record.get(rel) == have or have in earlier.get(rel, ()):
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
    name, record, ship, out, written = _name(root), _record(root), shipped(), [], []
    for e in status(root):
        if e.state in ("missing", "outdated"):
            text = render(ship[e.rel].read_text(encoding="utf-8"), name)
            done = "installed" if e.state == "missing" else "updated"
            if not dry_run:
                dest = root / SKILLS / e.rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8", newline="\n")
                record[e.rel] = _digest(text)
                written.append((SKILLS / e.rel).as_posix())
            out.append(Entry(e.rel, done if not dry_run else "would " + done[:-1]))
        else:
            if e.state == "current":
                record[e.rel] = _digest((root / SKILLS / e.rel).read_text(encoding="utf-8"))
            out.append(e)
    if not dry_run:
        write_record(root, record)
        if written:
            _commit(root, written + [RECORD.as_posix()])
    return out


def _commit(root: Path, paths: list[str]) -> None:
    """Commit the skills update wrote, by name, as migrate commits its rewrite (state_commit.py): nothing else in the
    working tree is swept in, and nothing happens outside a project that is its own repository."""
    from . import state_commit
    from .paths import Layout
    try:
        state_commit.commit(Layout(root), reason="skills update", extra=paths,
                            title=f"skills: the framework's skills from {__version__}")
    except Exception:                                       # noqa: BLE001 - the files are written either way
        pass
