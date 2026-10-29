---
name: framework-change
description: Change the Supervisor framework itself (loop, store, gates, adapters, Control Center, CLI, template, docs) or decide whether a change belongs in the framework, a plugin or a project, and take it to a reviewable state with gates G1, G3 and G7 green. Use when the user says "change the framework", "this fix should go upstream", "add this to the control center for every project", "/framework-change", "/supervisor-update", or asks why a project folder and the framework differ. For cutting the release afterwards use framework-release; for any state or config format change use migration-author.
---

# Changing the Supervisor framework

Two kinds of folder are involved: the **framework** (this repo: `supervisor/`, `docs/`, `tests/`, `boundary.toml`)
and **projects**, which run a tagged framework checkout installed editable. A project never edits the framework;
the framework never names a project. Read `docs/BOUNDARY.md` and `docs/FRAMEWORK_VS_PLUGIN.md` first.

## 1. Decide where the change goes, in writing

Answer before writing code, and keep the answer for the pull request (G7):

- **Rule 1:** would a project about something completely different want this? Yes → framework, continue here.
  Some projects → a plugin package (`supervisor plugin new <name> --package`, `docs/PLUGIN_STANDARD.md`). Only this project →
  the project's `plugins/`, `config/` or `agents/`: stop here and do it there.
- **Rule 3, for a new or changed hook:** name its second user — a generic use beyond the project asking. With
  only one user the project solves it in its plugin.
- **Rule 4:** a default that is right for only one project is that project's value, not a framework default.

## 2. Set up the clone once

`git config core.hooksPath .githooks` in the framework clone (git does not version hooks), so `.githooks/pre-commit`
runs the boundary check (G1) before every commit. Work on a branch; `main` takes changes only through a pull request
whose CI is green.

## 3. Test first

Reproduce or specify the behaviour with a failing test in `tests/` before the fix. Agent CLIs are faked under
`tests/fixtures/`; the suite must never reach a real model client or the real Codex usage RPC (`tests/conftest.py`
fails such a test). Run `python -m pytest -q` before and after; on Windows `tools\test_windows.ps1` saves the log.

## 4. Make the change within the invariants

- Keep `docs/ARCHITECTURE.md`'s invariants: single writer, one transition function, one launch gate, verification
  trusts only what it observed, independent reviews, no credential in a prompt or log, crash-safe.
- `supervisor/` imports only the standard library, itself and the dependencies `pyproject.toml` declares; a new
  dependency goes into `pyproject.toml` and `boundary.toml [imports] allowed` in the same commit.
- No paths, hosts, people or project vocabulary in `supervisor/`; model and vendor names only in the files
  `boundary.toml [vendor] files` lists. Adding a file to that list, or any exception, is a boundary decision the
  commit message explains.
- A change to the shape of anything under a project's `state/` or `config/` is a migration: use the
  **migration-author** skill.
- A hook added or changed: update `docs/FRAMEWORK_VS_PLUGIN.md`, `supervisor/plugin/_registry.py` (the documented
  `Registry`), the reference plugin and its contract test (`tests/test_hook_contracts.py` fails until the new hook
  has one), and the template's `project.yaml` / `routing.yaml` comments. Adding one is a minor version, changing one
  a major version with a migration note for plugin authors.
- The public plugin API (`supervisor/plugin/`, `docs/PLUGIN_API.md`) is a promise to every plugin: export a
  name only when a plugin needs it (rule 3 for a second user), with a behaviour test through `supervisor.plugin`,
  its section in `docs/PLUGIN_API.md`, `python -m supervisor.api_contract record` to update
  `docs/plugin_api.json` in the same commit, and a "Plugin API" line in `CHANGELOG.md` naming it. Removing a
  name or changing a signature so a caller breaks needs a minor release that warns first, then a major one.
  `supervisor.testing` is unstable and carries no such promise.
- `supervisor/template/` is the only place project files are born; keep it generic and its comments current (dot
  files are stored as `_dot_<name>`).

## 5. Gates before asking for review

All four, and say in the pull request which ran (the gate table in `docs/BOUNDARY.md` says which apply):

- **G1** `python -m supervisor.boundary` from the framework root: OK, no notes. G1d needs the local project-terms
  file (`docs/BOUNDARY.md`); when it says it did not run, create that file before asking for review. The framework
  repo never holds a project's files or information: examples in code, docs, tests and the CHANGELOG use invented
  names.
- **G2** `python -m supervisor.api_contract check`: the recorded contract is the code, every public name has its own
  documented section, and the difference from the last release tag (signatures and documented sections) follows the
  version rule and is classified in the CHANGELOG (CI runs it with full history).
- **G3** `python -m pytest -q` green on Windows (`tools\test_windows.ps1`) and on Linux (CI runs both).
- **G7** the written answers from step 1, plus: plugin API impact (names added, deprecated or changed),
  migration impact, docs updated.

Then `CHANGELOG.md` under `## Unreleased` (what changed and why, for owners and plugin authors), and the
commit naming the module or hook. Never commit framework changes from a project repo; never put project files in
the framework.

## 6. Release and rollout

A change reaches projects only as a tagged release: **framework-release** cuts it (version, changelog, review,
tag). Projects take the tag in a maintenance window — drain until no job record is open and no command queued,
stop, move their framework checkout to the tag (`git -C <runtime worktree> fetch origin tag vX.Y.Z`, then
`git -C <runtime worktree> checkout vX.Y.Z`), reinstall only if
dependencies changed, `supervisor migrate --dry-run`, `supervisor migrate`, `supervisor doctor`, start. Never `git
pull` a project's framework checkout: doctor refuses an untagged or dirty one, by design.

## Promoting a project plugin into the framework

Rule 1 must hold for the whole of it: split the project-specific parts out first and leave them in the project.
Port the code with its tests (an adapter under `adapters/`, a check into `doctor.py`, a page into `ui/routes/`),
follow steps 3–5, and in the same change set the project switches to the framework version and deletes its copy —
one copy afterwards, never two.
