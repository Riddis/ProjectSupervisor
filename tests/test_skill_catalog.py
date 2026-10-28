"""1.13.0, phase 4: the skills hold to what the boundary plan's "Which gates apply" table says of them.

- Every skill the plan lists exists where it belongs: the contributor skills in the framework repo's
  `.claude/skills/`, the project-side ones in the template, which `supervisor init` and `supervisor skills update`
  deliver. Each one's front matter names it after its folder and describes when to use it.
- Each phase 4 skill ends with the checks the table gives it ("Checks it ends with").
- Every `supervisor ...` command a skill names exists, so a renamed command breaks this test, not a user.
- No skill carries a machine's path or a non-loopback host. Projects' words are gate G1d's job, from its local list."""
from __future__ import annotations

import re
from pathlib import Path

import click
import pytest
import typer

from supervisor.cli import app

REPO = Path(__file__).resolve().parents[1]
FRAMEWORK = REPO / ".claude" / "skills"
TEMPLATE = REPO / "supervisor" / "template" / "_dot_claude" / "skills"

FRAMEWORK_SKILLS = {"framework-change", "framework-release", "migration-author", "supervisor-setup", "extension-new",
                    "extension-promote", "extension-release", "boundary-review"}
TEMPLATE_SKILLS = {"supervisor-setup", "maintenance-window", "rehearsal", "project-upgrade", "project-restore",
                   "runtime-diagnose", "task-author", "stuck-task", "role-change"}

# What each phase 4 skill's "Checks it ends with" must name (the plan's "Which gates apply" table).
ENDS_WITH = {
    "extension-new": ["supervisor extension check", "G5"],
    "extension-promote": ["supervisor extension check", "G5"],
    "extension-release": ["G5", "sha256"],
    "boundary-review": ["G1", "G1d", "G2", "G3", "G5", "G7"],
    "maintenance-window": ["supervisor doctor", "G6", "test suite"],
    "rehearsal": ["supervisor doctor", "G6", "test suite"],
    "project-upgrade": ["supervisor doctor", "G6", "test suite", "supervisor skills status"],
    "project-restore": ["supervisor doctor", "G6", "test suite"],
    "runtime-diagnose": ["supervisor doctor", "G6"],
    "task-author": ["supervisor task import --dry-run", "supervisor check", "supervisor doctor"],
    "stuck-task": ["supervisor check", "supervisor doctor"],
    "role-change": ["supervisor doctor", "supervisor check"],
}


def skill_files() -> list[Path]:
    return sorted(FRAMEWORK.glob("*/SKILL.md")) + sorted(TEMPLATE.glob("*/SKILL.md"))


def front_matter(text: str) -> dict[str, str]:
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    assert m, "no front matter"
    out = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def test_every_planned_skill_is_where_it_belongs():
    assert {p.parent.name for p in FRAMEWORK.glob("*/SKILL.md")} >= FRAMEWORK_SKILLS
    assert {p.parent.name for p in TEMPLATE.glob("*/SKILL.md")} >= TEMPLATE_SKILLS


@pytest.mark.parametrize("path", skill_files(), ids=lambda p: f"{p.parents[2].name}/{p.parent.name}")
def test_each_skill_is_named_after_its_folder_and_says_when_to_use_it(path):
    meta = front_matter(path.read_text(encoding="utf-8"))
    assert meta.get("name") == path.parent.name
    description = meta.get("description", "")
    assert len(description) > 80 and "use " in description.lower(), description


@pytest.mark.parametrize("name", sorted(ENDS_WITH))
def test_each_phase_4_skill_ends_with_the_checks_the_plan_gives_it(name):
    path = (TEMPLATE if name in TEMPLATE_SKILLS else FRAMEWORK) / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "## Checks it ends with" in text, name
    ending = text.split("## Checks it ends with", 1)[1]
    missing = [w for w in ENDS_WITH[name] if w not in ending]
    assert not missing, (name, missing)


def _commands() -> click.Group:
    return typer.main.get_command(app)


def _named_commands(text: str) -> set[tuple[str, ...]]:
    """`supervisor <command> [<subcommand>]` as written in backticks, placeholders and options left out."""
    found = set()
    for span in re.findall(r"`supervisor ([^`]+)`", text):
        words = [w for w in span.split() if re.fullmatch(r"[a-z][a-z0-9_-]*", w)]
        if words:
            found.add(tuple(words[:2]))
    return found


@pytest.mark.parametrize("path", skill_files(), ids=lambda p: f"{p.parents[2].name}/{p.parent.name}")
def test_every_command_a_skill_names_exists(path):
    root = _commands()
    ctx = click.Context(root)
    for words in sorted(_named_commands(path.read_text(encoding="utf-8"))):
        cmd = root.get_command(ctx, words[0])
        assert cmd is not None, (path.parent.name, "supervisor " + " ".join(words))
        group = getattr(cmd, "commands", None)              # a command group (typer bundles its own click)
        if group is not None and len(words) > 1:
            assert words[1] in group, (path.parent.name, "supervisor " + " ".join(words))


DRIVE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")
HOME = re.compile(r"(?:/home/|/Users/|\\Users\\)[A-Za-z0-9]")
HOST = re.compile(r"\b[a-z][a-z0-9+.-]*://(?!127\.0\.0\.1|localhost)[^/\s`'\"]+")


@pytest.mark.parametrize("path", skill_files(), ids=lambda p: f"{p.parents[2].name}/{p.parent.name}")
def test_no_skill_carries_a_machines_path_or_host(path):
    text = path.read_text(encoding="utf-8")
    hits = DRIVE.findall(text) + HOME.findall(text) + [h for h in HOST.findall(text) if "github.com" not in h]
    assert not hits, (path.parent.name, hits)
