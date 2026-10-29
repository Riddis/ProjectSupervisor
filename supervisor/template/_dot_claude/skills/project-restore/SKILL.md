---
name: project-restore
description: Recover this project after a change that went wrong - restore the matching set of framework version, project code, config, state, skills and installed packages from a window's snapshot, and decide what to do with work done since. Use when the user says "roll back", "restore", "undo the upgrade", "/project-restore", or a window ended with a failure it could not explain.
---

# Restoring the project

A restore puts back a **matching set**, as it was together before the change:
- the framework release;
- the project's code;
- `config/`, `state/`, `project.yaml` and the skills folder (the folder this skill file is in);
- the packages installed in its environment.

Restoring only some of them leaves a project that matches nothing. Every restore runs inside the maintenance-window
skill: stop, nothing open, snapshot what is there now. That snapshot is the way back from the restore itself, which
re-applies the change.

## 1. Find the set

From the window's snapshot folder and its README:
- the framework tag before the change (the snapshot's record of the checkout);
- the project's commit before the change (`HEAD` recorded there);
- the snapshot's `project.yaml`, `config/`, `state/` and skills folder, and its list of installed packages.

## 2. Has work run since the change?

The window's own commands (its pause, drain, migrate) do not count. Work does: list what happened after the window
ended:
- `git log <commit before>..HEAD`: checkpoint commits (one per accepted task) and state commits;
- `state/log.jsonl` after the window's end: the events of tasks dispatched, delivered or completed, of inbox items
  answered, and the owner's `command` events;
- `state/commands/applied/` files newer than the snapshot;
- `state/runtime/jobs/` records newer than the snapshot.

Nothing there: rollback A. Anything: rollback B.

## 3. Rollback A: no work has run since

1. `git -C <framework checkout> checkout <previous tag>`. Skip it when the window did not move the framework (an
   extension switch): the snapshot's record of the checkout equals its current `describe --tags`.
2. In the project, `git reset --keep <commit before>`. That undoes the change's commits and keeps the uncommitted
   files. If it refuses, a file would be lost: stop and say which.
3. Restore `project.yaml`, `config/` and the skills folder from the snapshot. Restore `state/` too when the change's
   migrate notes changed a file there (the window's record lists them).
4. Uninstall every package the change installed, and reinstall the ones it replaced, to match the snapshot's list.
   In a venv, `pip uninstall <distribution>`. Pip refuses a package in a target folder on `PYTHONPATH` ("outside
   environment"): delete the files listed in `<folder>/<distribution>-<version>.dist-info/RECORD`, then that
   `.dist-info` folder. A package left in place makes the loop find its extension twice and block.
5. `supervisor migrate`: it records the older version again. Then `supervisor doctor`.

## 4. Rollback B: work has run since

Work done on the new version must not vanish in silence. Before restoring:
1. List each item from section 2: each completed task and its delivery, each answered inbox item, each applied
   command, each commit.
2. For each, the owner decides: carry it over to the restored state (re-apply the answer, re-run the task), or
   discard it, explicitly.
3. Then restore as in A, and re-apply what the owner chose to keep, through owner commands (`supervisor cmd`,
   the Control Center), never by editing state files.

## 5. Verify before resuming

- `supervisor doctor` (G6): the `framework` check names the restored tag, and `project` the matching version.
- The skills match the restored release: `supervisor skills status` shows none the older release does not ship.
  A release before 1.13.0 has no `skills` command: then the skills folder equals the snapshot's.
- The project's test suite on the live files.
- `supervisor status`: the tasks, inbox and gates read as expected.
- `supervisor pause`, then start the loop and the Control Center (the project's start script, or `supervisor run`
  and `supervisor ui`), and tell the owner what was restored, what was carried over, and what was discarded.

## Checks it ends with

- `supervisor doctor` (G6): OK, or every FAIL explained.
- The project's test suite on the live files: green.
- A written record of the restore in the project's runbook: the set restored, and every item of work carried over
  or discarded.
