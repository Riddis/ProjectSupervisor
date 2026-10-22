# Updating the framework under running projects

Projects install the framework **editable** from a local clone (`pip install -e <Supervisor>[ui]`), so a framework
commit is live in every project on that machine the next time a process starts. That is the point — one fix, every
project — and it is also why the framework carries a version and migrations.

## As a project owner

```
git -C D:\Repos\Supervisor pull
cd D:\Repos\my-project
.venv\Scripts\python.exe -m supervisor doctor          # "project.yaml records framework 1.0.0; installed 1.1.0 — run `supervisor migrate`"
.venv\Scripts\python.exe -m supervisor migrate --dry-run
.venv\Scripts\python.exe -m supervisor migrate
.venv\Scripts\python.exe -m supervisor render
```

Then **Reload** in the Control Center (or restart `start_supervisor.bat`) so the loop imports the new code; a
running loop keeps the old code until it restarts. Restart the UI too. Projects that are not touched keep working
on the old version in their running processes and pick the new one up on their next start.

If a release says a *major* migration is required, do not start the loop before `supervisor migrate` has run:
the doctor check for the project is FAIL until then, and the Control Center's Start button reports it.

## As the framework author

See `.claude/skills/supervisor-update/SKILL.md` for the step list. The rules:

- Every change has a test. `python -m pytest -q` is green on Linux and Windows before a version bump.
- `supervisor/__init__.py` `__version__` and `pyproject.toml` `version` move together.
- A change to the shape of anything under a project's `state/` or `config/` gets a migration in
  `supervisor/migrations.py` (idempotent, honours `dry_run`) and a **Migration** line in `CHANGELOG.md`.
- `supervisor/template/` is the only place project files are born; keep it generic (`tests/test_framework.py`
  checks it) and keep its comments current — they are the documentation most owners actually read.
- Extension hooks are an API: adding one is a minor bump; changing one is a major bump with a migration note
  telling extension authors what to change.

## Multiple machines / a friend's machine

Clone the framework, create the project venv, install editable, done — the project folder carries no framework
code. If two machines run the same project (shared via git), both must run the same framework version; the
doctor check names the mismatch. Pin with a git tag when that matters: `git -C Supervisor checkout v1.1.0`.
