# Updating the framework under running projects

Projects install the framework **editable** from a local clone (`pip install -e <Supervisor>[ui]`), so a framework
commit is live in every project on that machine the next time a process starts. That is the point — one fix, every
project — and it is also why the framework carries a version and migrations.

## As a project owner

You normally do nothing by hand. When the Control Center launches (`start_control_center.bat`) it fetches the
framework's `origin`, prints the commits your clone is behind, and asks `Update now? [y/N]` in its window; the
same appears as a banner on the Overview with an **Update now** button while it runs (re-checked every 30 min).
Saying yes does, in order: `git pull --ff-only` in the framework clone, `pip install -e` (dependencies may have
changed), `supervisor migrate` and `supervisor doctor` in a child process on the new code, `loop.reload` if a loop
is running (its launcher restarts it), and finally the Control Center restarts itself (exit code 3, which
`tools/run_control_center.ps1` turns into a relaunch). A clone with uncommitted changes is never pulled over; the
banner says to commit or stash first.

The same by hand, from the project folder:

```
.venv\Scripts\python.exe -m supervisor update --check     # list what origin has
.venv\Scripts\python.exe -m supervisor update             # asks, then applies
.venv\Scripts\python.exe -m supervisor update --yes       # unattended
```

Or fully manual: `git -C <framework> pull`, then `supervisor doctor` (reports the version mismatch),
`supervisor migrate --dry-run`, `supervisor migrate`, `supervisor render`, Reload in the Control Center, restart
the UI. `supervisor run` never prompts (it may be unattended); it prints the available update and carries on.
Projects that are not touched keep working on the old version in their running processes and pick the new one up
on their next start.

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
