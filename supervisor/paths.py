"""Project layout in one place. The framework package lives wherever it is installed; a *project* is the directory
that holds `project.yaml` (plus `state/`, `config/`, `agents/`, …). Everything derives from that root:

    1. `SUPERVISOR_ROOT` (environment) when set — what the Control Center uses to launch `supervisor run`;
    2. otherwise the nearest ancestor of the current working directory that contains `project.yaml`;
    3. otherwise the current working directory itself (so `supervisor init .` can bootstrap an empty folder).
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_FILE = "project.yaml"


class NoProject(RuntimeError):
    """No project.yaml found from the current directory upwards and SUPERVISOR_ROOT is unset."""


def find_root(start: Path | None = None) -> Path | None:
    env = os.environ.get("SUPERVISOR_ROOT")
    if env:
        return Path(env).resolve()
    here = (Path(start) if start else Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / PROJECT_FILE).is_file():
            return candidate
    return None


def root() -> Path:
    return find_root() or Path.cwd().resolve()


def require_root() -> Path:
    found = find_root()
    if found is None:
        raise NoProject(f"no {PROJECT_FILE} found in {Path.cwd()} or any parent; run `supervisor init <dir>` "
                        "or set SUPERVISOR_ROOT")
    return found


class Layout:
    def __init__(self, base: Path | None = None):
        self.root = Path(base).resolve() if base else root()
        self.project_file = self.root / PROJECT_FILE
        self.state = self.root / "state"
        self.tasks = self.state / "tasks"
        self.decisions = self.state / "decisions"
        self.issues = self.state / "issues"
        self.inbox = self.state / "inbox"
        self.commands = self.state / "commands"
        self.commands_applied = self.commands / "applied"
        self.runtime = self.state / "runtime"
        self.jobs = self.runtime / "jobs"
        self.conversations = self.state / "conversations"
        self.gates = self.state / "gates.yaml"
        self.owner_tasks = self.state / "owner_tasks.yaml"
        self.log = self.state / "log.jsonl"
        self.lock = self.state / ".lock"
        self.render_lock = self.state / ".render.lock"
        self.wake = self.runtime / "wake"
        self.config = self.root / "config"
        self.runs = self.root / "runs"
        self.agents = self.root / "agents"
        self.secrets = self.root / "secrets"
        self.uploads = self.root / "uploads"          # files the owner drops for an agent (Control Center inbox)
        self.extensions = self.root / "extensions"    # project-specific Python (adapters, checks, UI panels)
        self.docs = self.root / "docs"

    def ensure(self):
        for p in (self.tasks, self.decisions, self.issues, self.inbox, self.commands_applied, self.jobs,
                  self.conversations, self.runs):
            p.mkdir(parents=True, exist_ok=True)
        return self
