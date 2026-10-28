---
name: rehearsal
description: Try a risky change on a copy of this project before doing it to the live one - an upgrade, a migration, a restore, a change to state or config. Use when the user says "rehearse", "try it on a copy first", "/rehearsal", or before any maintenance window whose change has not been run on this project's own data yet.
---

# Rehearsal on a copy

A rehearsal runs the window's change on a copy of the live project, so the first time it meets this project's own
data is not on the live tree. Nothing here touches the live project, its loop or its framework checkout.

## 1. Make the copy

Pick the lighter way that is enough for the change:

- **A clone of the project's repository** (`git clone --no-hardlinks <project> <copy>`) when only committed files
  matter (a migration, a state commit). Set `core.autocrlf` in the clone to the live repository's value and check
  the files out again (`git rm -r --cached -q . && git reset -q --hard`), then confirm with `cmp` that
  `project.yaml` is byte-identical to the live one.
- **A copy of the working tree** when the uncommitted files matter too. Leave out what is large or secret: the
  virtual environment, model or data caches, `secrets/`, `runs/`, lock files. Then commit the copy's uncommitted
  changes in the copy, so the change starts from a known commit.

Then, in the copy:
- set `SUPERVISOR_ROOT` to the copy for every command, and check it with `supervisor status`;
- make sure no job record is open (`state/runtime/jobs/` all DONE or FAILED);
- never start the loop with real agent lanes: hide the agent CLIs from `PATH` and route model tasks to fake lanes,
  or do not start the loop at all. A copy that dispatches spends real money and real work.

## 2. Rehearse the change

Run the window's change exactly as the window will (the maintenance-window or project-upgrade skill), against the
copy: the framework under test on `PYTHONPATH` (or a second checkout at the new tag), never by moving the live
project's framework checkout.

Record, for each step, what it printed: the migrate dry run, the apply, the repeat, `supervisor doctor`, the
project's suite, and a Control Center page loaded on the copy if the change touches the UI.

## 3. Rehearse the way back

Restore the copy to its pre-change state, as rollback A would (the project-restore skill), and run doctor and the
suite again. A change whose rollback has not run on the copy is not ready for the live project.

## 4. Report

Write the rehearsal down where the owner keeps such records: the copy's path, what was left out of it and why, each
step's output, every difference from what the change promised, and every failure with its cause. A failure caused
by the copy itself (a left-out folder, a path that names the live tree) is named as such, with the evidence.

The copy stays until the owner says to delete it.

## Checks it ends with

- `supervisor doctor` (G6) on the copy, after the change and after the rollback.
- The project's test suite on the copy, after the change and after the rollback.
- The live project, its loop and its framework checkout: untouched (say how you know).
