---
name: project-restore
description: Recover this project after a change that went wrong - restore the matching set of framework version, project code, config and state from a window's snapshot, and decide what to do with work done since. Use when the user says "roll back", "restore", "undo the upgrade", "/project-restore", or a window ended with a failure it could not explain.
---

# Restoring the project

A restore puts back a **matching set**: the framework release, the project's code, `config/`, `state/` and
`project.yaml`, as they were together before the change. Restoring only one of them leaves a project that matches
nothing. Every restore runs inside the maintenance-window skill (stop, nothing open, snapshot what is there now).

## 1. Find the set

From the window's snapshot folder and its README:
- the framework tag before the change (the snapshot's record of the checkout);
- the project's commit before the change (`HEAD` recorded there);
- the snapshot's `project.yaml`, `config/` and `state/`.

Then find out whether work has run since the change: tasks completed, jobs run, inbox items answered, commits made
by the loop (`git log <commit before>..HEAD`: checkpoint commits, state commits, migrate's commit). The answer
decides between A and B.

## 2. Rollback A: no work has run since

1. `git -C <framework checkout> checkout <previous tag>`.
2. In the project, `git reset --keep <commit before>`. That undoes the change's commits and keeps the uncommitted
   files. If it refuses, a file would be lost: stop and say which.
3. Restore `project.yaml` and `config/` from the snapshot (and `state/` too, if the change rewrote it).
4. `supervisor migrate`: it records the older version again. Then `supervisor doctor`.

## 3. Rollback B: work has run since

Work done on the new version must not vanish in silence. Before restoring:
1. List what happened since the change: each completed task and its delivery, each answered inbox item, each
   applied command, each commit.
2. For each, the owner decides: carry it over to the restored state (re-apply the answer, re-run the task), or
   discard it, explicitly.
3. Then restore as in A, and re-apply what the owner chose to keep, through owner commands (`supervisor cmd`,
   the Control Center), never by editing state files.

## 4. Verify before resuming

- `supervisor doctor` (G6): the `framework` check names the restored tag, and `project` the matching version.
- The project's test suite on the live files.
- `supervisor status`: the tasks, inbox and gates read as expected.
- Start PAUSED, and tell the owner what was restored, what was carried over, and what was discarded.

## Checks it ends with

- `supervisor doctor` (G6): OK, or every FAIL explained.
- The project's test suite on the live files: green.
- A written record of the restore: the set restored, and every item of work carried over or discarded.
