"""1.13.0: the framework's skills in a project (`.claude/skills/`) stay current without losing a project's own edits.
`supervisor init` stamps the template's skills and records the hash of each file it wrote in
`.claude/supervisor-skills.json`. `supervisor skills status` compares each copy with the template and that record;
`supervisor skills update` installs a missing skill, replaces a copy nobody edited, and never touches an edited one.

| state | the copy | update |
| --- | --- | --- |
| current | equals the template | records it |
| missing | absent | installs it |
| outdated | equals what was recorded, the template has moved on | replaces it |
| edited | differs from both (or was never recorded and differs) | leaves it, flagged for a merge |
| retired | recorded, but the template no longer ships it | leaves it |

Hashes ignore line endings, so a checkout that turned LF into CRLF still counts as unedited."""
from __future__ import annotations

import json
import shutil

import pytest
from typer.testing import CliRunner

from supervisor import skills, template


def tmpl(tmp_path, files: dict[str, str]):
    """A template skills folder of our own, in place of the framework's."""
    root = tmp_path / "template-skills"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    return root


@pytest.fixture
def project(tmp_path, monkeypatch):
    shipped = tmpl(tmp_path, {"alpha/SKILL.md": "---\nname: alpha\n---\nalpha v1\n",
                              "beta/SKILL.md": "---\nname: beta\n---\nbeta v1\n"})
    monkeypatch.setattr(skills, "TEMPLATE_SKILLS", shipped)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "project.yaml").write_text('name: "demo"\n', encoding="utf-8")
    assert {e.state for e in skills.update(root)} == {"installed"}       # as `supervisor init` leaves a project
    return root, shipped


def states(root) -> dict[str, str]:
    return {e.rel: e.state for e in skills.status(root)}


def copy(root, rel):
    return root / ".claude" / "skills" / rel


def test_installed_skills_are_recorded_and_current(project):
    root, shipped = project
    assert copy(root, "alpha/SKILL.md").read_text(encoding="utf-8") == "---\nname: alpha\n---\nalpha v1\n"
    record = json.loads((root / ".claude" / "supervisor-skills.json").read_text(encoding="utf-8"))
    assert set(record["files"]) == {"alpha/SKILL.md", "beta/SKILL.md"}
    assert states(root) == {"alpha/SKILL.md": "current", "beta/SKILL.md": "current"}


def test_an_unedited_copy_is_updated_when_the_template_moves_on(project):
    root, shipped = project
    (shipped / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\nalpha v2\n", encoding="utf-8")
    assert states(root)["alpha/SKILL.md"] == "outdated"
    done = {e.rel: e.state for e in skills.update(root)}
    assert done["alpha/SKILL.md"] == "updated"
    assert copy(root, "alpha/SKILL.md").read_text(encoding="utf-8").endswith("alpha v2\n")
    assert states(root) == {"alpha/SKILL.md": "current", "beta/SKILL.md": "current"}


def test_an_edited_copy_is_never_overwritten(project):
    root, shipped = project
    copy(root, "alpha/SKILL.md").write_text("---\nname: alpha\n---\nalpha v1, with the project's notes\n",
                                            encoding="utf-8")
    (shipped / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\nalpha v2\n", encoding="utf-8")
    assert states(root)["alpha/SKILL.md"] == "edited"
    done = {e.rel: e for e in skills.update(root)}
    assert done["alpha/SKILL.md"].state == "edited" and "merge" in done["alpha/SKILL.md"].detail
    assert "the project's notes" in copy(root, "alpha/SKILL.md").read_text(encoding="utf-8")


def test_a_new_skill_is_installed_and_a_removed_one_is_left(project):
    root, shipped = project
    (shipped / "gamma").mkdir()
    (shipped / "gamma" / "SKILL.md").write_text("---\nname: gamma\n---\ngamma v1\n", encoding="utf-8")
    shutil.rmtree(shipped / "beta")
    assert states(root) == {"alpha/SKILL.md": "current", "beta/SKILL.md": "retired", "gamma/SKILL.md": "missing"}
    done = {e.rel: e.state for e in skills.update(root)}
    assert done["gamma/SKILL.md"] == "installed" and done["beta/SKILL.md"] == "retired"
    assert copy(root, "gamma/SKILL.md").exists() and copy(root, "beta/SKILL.md").exists()


def test_a_dry_run_writes_nothing(project):
    root, shipped = project
    (shipped / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\nalpha v2\n", encoding="utf-8")
    record = (root / ".claude" / "supervisor-skills.json").read_bytes()
    done = {e.rel: e.state for e in skills.update(root, dry_run=True)}
    assert done["alpha/SKILL.md"] == "would update"                     # says it did not write (trial finding)
    assert copy(root, "alpha/SKILL.md").read_text(encoding="utf-8").endswith("alpha v1\n")
    assert (root / ".claude" / "supervisor-skills.json").read_bytes() == record


def test_a_dry_run_names_a_missing_skill_as_would_install(project):
    """1.13.0 printed "would installe" (the past tense cut by one letter; rehearsal finding)."""
    root, shipped = project
    (shipped / "gamma").mkdir()
    (shipped / "gamma" / "SKILL.md").write_text("---\nname: gamma\n---\ngamma v1\n", encoding="utf-8")
    done = {e.rel: e.state for e in skills.update(root, dry_run=True)}
    assert done["gamma/SKILL.md"] == "would install"
    assert not copy(root, "gamma/SKILL.md").exists()


def test_line_endings_do_not_count_as_an_edit(project):
    root, shipped = project
    p = copy(root, "alpha/SKILL.md")
    p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    assert states(root)["alpha/SKILL.md"] == "current"
    (shipped / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\nalpha v2\n", encoding="utf-8")
    assert states(root)["alpha/SKILL.md"] == "outdated"


def test_a_project_from_before_the_record_adopts_identical_copies_and_flags_others(project):
    root, shipped = project
    (root / ".claude" / "supervisor-skills.json").unlink()
    copy(root, "beta/SKILL.md").write_text("---\nname: beta\n---\nbeta, changed locally\n", encoding="utf-8")
    assert states(root) == {"alpha/SKILL.md": "current", "beta/SKILL.md": "edited"}
    skills.update(root)
    record = json.loads((root / ".claude" / "supervisor-skills.json").read_text(encoding="utf-8"))
    assert "alpha/SKILL.md" in record["files"] and "beta/SKILL.md" not in record["files"]
    assert "changed locally" in copy(root, "beta/SKILL.md").read_text(encoding="utf-8")


def test_the_cli_reports_and_updates(project, monkeypatch):
    from supervisor.cli import app
    root, shipped = project
    (shipped / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\nalpha v2\n", encoding="utf-8")
    monkeypatch.setenv("SUPERVISOR_ROOT", str(root))
    runner = CliRunner()
    out = runner.invoke(app, ["skills", "status"])
    assert out.exit_code == 0 and ["outdated", "alpha/SKILL.md"] in [line.split() for line in out.output.splitlines()]
    out = runner.invoke(app, ["skills", "update"])
    assert out.exit_code == 0 and ["updated", "alpha/SKILL.md"] in [line.split() for line in out.output.splitlines()]
    assert copy(root, "alpha/SKILL.md").read_text(encoding="utf-8").endswith("alpha v2\n")


def test_the_framework_ships_its_template_skills_through_this(tmp_path):
    """The real template: every skill it ships is current right after init."""
    root = tmp_path / "fresh"
    template.stamp(root, name="fresh")
    found = states(root)
    assert found and set(found.values()) == {"current"}, found
    record = json.loads((root / ".claude" / "supervisor-skills.json").read_text(encoding="utf-8"))
    assert set(record["files"]) == set(found)                            # init recorded what it stamped


def test_an_older_shipped_version_nobody_edited_is_outdated_not_edited(project, monkeypatch):
    """Trial finding: a project made before the record existed holds a copy an earlier release shipped. Its hash is
    in the history of shipped versions (supervisor/skills_history.json), so it is outdated, not edited."""
    root, shipped = project
    (root / ".claude" / "supervisor-skills.json").unlink()
    old = "---\nname: alpha\n---\nalpha v0, as an earlier release shipped it\n"
    copy(root, "alpha/SKILL.md").write_text(old, encoding="utf-8")
    monkeypatch.setattr(skills, "history", lambda: {"alpha/SKILL.md": {skills._digest(old)}})
    assert states(root)["alpha/SKILL.md"] == "outdated"
    assert {e.rel: e.state for e in skills.update(root)}["alpha/SKILL.md"] == "updated"
    assert copy(root, "alpha/SKILL.md").read_text(encoding="utf-8").endswith("alpha v1\n")


def test_the_shipped_history_holds_what_earlier_releases_shipped():
    """The file tools/skills_history.py writes from the release tags: every entry names a skill file and full hashes."""
    found = skills.history()
    assert "supervisor-setup/SKILL.md" in found
    assert all(len(h) == 64 for hashes in found.values() for h in hashes)


def test_an_update_commits_what_it_wrote(project):
    """Trial finding: nothing committed the skills update wrote, so `git status` kept showing them."""
    import subprocess
    root, shipped = project
    git = lambda *a: subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=False)
    git("init", "-q"); git("config", "user.email", "t@example.com"); git("config", "user.name", "t")
    git("add", "-A"); git("commit", "-qm", "init")
    (shipped / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\nalpha v2\n", encoding="utf-8")
    skills.update(root)
    assert git("log", "-1", "--format=%s").stdout.startswith("skills:")
    assert git("status", "--porcelain", "--", ".claude").stdout.strip() == ""


def test_an_update_commits_only_what_it_wrote(project):
    """Review 1.13.0 R2 (P2): the update's commit also took every changed record under state/ and every changed
    projection, though it promises to commit only the skill files it wrote and its record. Unrelated state, a changed
    projection and an owner's staged file stay as they were."""
    import subprocess
    root, shipped = project
    git = lambda *a: subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=False)
    (root / "state").mkdir()
    (root / "state" / "unrelated.txt").write_text("before\n", encoding="utf-8", newline="\n")
    (root / "STATE.md").write_text("before\n", encoding="utf-8", newline="\n")
    (root / "notes.txt").write_text("before\n", encoding="utf-8", newline="\n")
    git("init", "-q"); git("config", "user.email", "t@example.com"); git("config", "user.name", "t")
    git("add", "-A"); git("commit", "-qm", "init")
    (root / "state" / "unrelated.txt").write_text("after\n", encoding="utf-8", newline="\n")
    (root / "STATE.md").write_text("after\n", encoding="utf-8", newline="\n")
    (root / "notes.txt").write_text("staged\n", encoding="utf-8", newline="\n")
    git("add", "notes.txt")
    (shipped / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\nalpha v2\n", encoding="utf-8")
    skills.update(root)
    committed = git("show", "--pretty=", "--name-only", "HEAD").stdout.split()
    assert sorted(committed) == [".claude/skills/alpha/SKILL.md", ".claude/supervisor-skills.json"], committed
    status = git("status", "--porcelain").stdout.splitlines()
    assert " M state/unrelated.txt" in status and " M STATE.md" in status and "M  notes.txt" in status, status
