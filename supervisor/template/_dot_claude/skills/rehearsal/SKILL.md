---
name: rehearsal
description: Try a risky change on a copy of this project before doing it to the live one - an upgrade, a migration, a restore, a change to state or config. Use when the user says "rehearse", "try it on a copy first", "/rehearsal", or before any maintenance window whose change has not been run on this project's own data yet.
---

# Rehearsal on a copy

A rehearsal runs the window's change on a copy of the live project, so the first time it meets this project's own
data is not on the live tree. Nothing here touches the live project, its loop, its environment or its framework
checkout.

## 1. Make the copy

Pick the lighter way that is enough for the change:

- **A clone of the project's repository** (`git clone --no-hardlinks <project> <copy>`), when only committed files
  matter (a migration, a state commit).
  - Then compare what matters, ignoring line endings, which the clone's checkout may change:
    `git diff --no-index --ignore-cr-at-eol <project>/project.yaml <copy>/project.yaml` prints nothing.
  - A clone has no `state/runtime/` (it is ignored), so it holds no job records. Say so.
- **A copy of the working tree**, when the uncommitted files matter too.
  - Copy `.git/` with it, so the copy can commit.
  - Leave out what is large, secret or live: the virtual environment, model or data caches, `secrets/`, `runs/`,
    and `state/.lock` with its `.lock.owner`.
  - Then commit the copy's uncommitted changes in the copy, so the change starts from a known commit.
  - Copied job records still name the live project's `runs/` folders. Never let the copy's loop collect them: no
    record may be open (all DONE or FAILED).

Then, in the copy:
- set `SUPERVISOR_ROOT` to the copy for every command, and check the `project:` line of `supervisor status`;
- make it unable to reach a real agent. Hiding the agent CLIs from `PATH` is not enough: the framework also finds
  them in their default install places. So, in the copy's `config/routing.yaml`:
  - set `escalation.diagnosis: false`: a stuck task otherwise dispatches a diagnosis to an agent role;
  - point every task at a deterministic job (`python` or `process`), or do not start the loop at all.

  A copy that dispatches spends real money and real work.
- for a Control Center check, start it on another port (`supervisor ui --port <free port>`): the live project
  holds its own.

## 2. Rehearse the change

Run the window's change exactly as the window will (the maintenance-window or project-upgrade skill), against the
copy: the framework under test on `PYTHONPATH` (or a second checkout at the new tag), never by moving the live
project's framework checkout.

Record, for each step, what it printed: the migrate dry run, the apply, the repeat, `supervisor doctor`, the
project's suite, and a Control Center page loaded on the copy if the change touches the UI.

## 3. Rehearse the way back

Restore the copy to its pre-change state, as rollback A would (the project-restore skill). That includes the skills
folder (skills an update installed) and uninstalling any package the change installed. Then run doctor and the
suite again. A change whose rollback has not run on the copy is not ready for the live project.

## 4. Report

Write the rehearsal down in the project's own records, beside the window it prepares (for example
`docs/runbooks/rehearsals/<date>-<change>.md`). Include:
- the copy's path, and what was left out of it and why;
- each step's output;
- every difference from what the change promised;
- every failure with its cause. A failure caused by the copy itself (a left-out folder, a path that names the live
  tree) is named as such, with the evidence.

The copy stays until the owner says to delete it.

## Checks it ends with

- `supervisor doctor` (G6) on the copy, after the change and after the rollback. A FAIL the copy causes (an agent
  CLI made unreachable, a release not yet tagged) is named as such.
- The project's test suite on the copy, after the change and after the rollback.
- The live project untouched. Before you start, note its `git rev-parse HEAD`, `git status --porcelain` and the
  newest entry of `state/log.jsonl`. At the end, compare: each is unchanged, apart from what the live loop itself
  did meanwhile.
