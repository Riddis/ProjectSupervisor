---
name: supervisor-update
description: Improve the Supervisor framework itself (the generic supervisor loop, Control Center, adapters, docs) or decide whether a change belongs in the framework or in a project's extensions, then version, migrate and ship it. Use when the user says "update the framework", "this supervisor fix should go upstream", "/supervisor-update", "add this to the control center for every project", or asks why a project folder and the framework differ.
---

# Updating the Supervisor framework

Two repos are involved: the **framework** (this repo: `supervisor/` package, `docs/`, `tests/`) and one or more
**projects** that `pip install -e` it. Read `docs/FRAMEWORK_VS_EXTENSION.md` first and decide where the change goes:

- Would a project about something completely different want it? → framework.
- Only this project? → the project's `extensions/`, `config/` or `agents/` — stop here and do it there.

## Making a framework change

1. Branch in the framework repo. Reproduce or specify the behaviour with a test in `tests/` (the suite runs on
   Linux and Windows; agent CLIs are faked under `tests/fixtures/`). Run `python -m pytest -q` before and after.
2. Keep the invariants in `docs/ARCHITECTURE.md` (single writer, one transition function, one launch gate,
   verification trusts only what it observed, independent reviews, no credential in a prompt or log, crash-safe).
3. If `state/` or `config/` shapes change: add a migration in `supervisor/migrations.py` (idempotent, dry-run aware),
   bump `supervisor/__init__.py` `__version__` and `pyproject.toml` the same way (patch = fix, minor = feature or
   new hook, major = migration required), and describe it in `CHANGELOG.md` under the new version with a
   "Migration" line.
4. If the change adds or alters an extension hook, update `docs/FRAMEWORK_VS_EXTENSION.md` and the template's
   `project.yaml`/`routing.yaml` comments.
5. Keep the template (`supervisor/template/`) generic: `tests/test_framework.py` fails on any project-specific or
   historical reference. If the change needs a new template file, add it there (dot-files as `_dot_<name>`).
6. Commit in the framework repo with a message that names the hook/module. Never commit framework changes from a
   project repo; never put project files in the framework.

## Rolling it out to a project

In each project folder: `git -C <framework> pull` (the editable install picks it up), then
`.venv\Scripts\python.exe -m supervisor doctor` — a version mismatch is reported — then
`.venv\Scripts\python.exe -m supervisor migrate` (dry run first with `--dry-run`), then `supervisor render`. Restart
the loop (Reload in the Control Center) so the running process imports the new code.

## Promoting a project extension

Port the code and its tests into `supervisor/` (an adapter under `adapters/`, a check into `doctor.py`, a UI page
into `ui/routes/`), follow steps 1–6, then delete the extension from the project and its entry in `project.yaml`.
