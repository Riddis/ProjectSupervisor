---
name: maintenance-window
description: Stop this project safely for a change that needs it, make the change, and bring it back. Use for a framework or extension upgrade, a restore, a toolchain or environment change, or whenever the user says "maintenance window", "take the loop down", "/maintenance-window". Other skills (project-upgrade, project-restore) call this procedure instead of repeating it.
---

# Maintenance window

A window is the only time the project's live state may change underneath the supervisor: the loop is stopped, nothing
holds `state/.lock`, and every step is recorded so it can be undone. Do every step; say which one you are on.

## 1. Before: nothing may be open

1. Ask the loop to finish what it is doing: `supervisor drain`. It launches nothing more; running jobs finish, are
   verified and collected, and the loop stops on its own. (`supervisor pause` is not enough: a paused loop still
   holds its jobs.)
2. Confirm, from the records, not from the Control Center's "Now running":
   - `state/runtime/supervisor.json` says STOPPED (or PAUSED with `open_jobs: []`), and its heartbeat is recent or
     the loop process is gone;
   - every file in `state/runtime/jobs/` has `phase` DONE or FAILED: a PREPARED or RUNNING record is an open job;
   - `state/commands/` holds no queued `*.json` (applied ones live in `state/commands/applied/`);
   - nothing holds `state/.lock`: no `supervisor run` process, and the Control Center is closed.
3. If a job stays open, stop here and say which one and why. Never delete or edit a job record to get past this.

## 2. Snapshot

Copy, into a dated folder outside the project (an archive the owner keeps), before anything changes:
- `project.yaml`, `config/` and `state/`;
- the git state: `git rev-parse HEAD` and the branch, `git status --porcelain`, and `git diff --binary` (the
  uncommitted changes);
- the framework checkout's commit and tag (`git -C <runtime checkout> describe --tags`).

Compare the copy with the live files (`diff -r`, `cmp`) and say they are identical. Write a README there with the
rollback-A steps for this window (the project-restore skill uses it).

## 3. The change

Do exactly the change the window is for, and nothing else. If the change is an upgrade, the project-upgrade skill
says what that is. Write down every command you ran and what it printed.

## 4. Bring it back

1. `supervisor migrate --dry-run`, then `supervisor migrate`: the dry run lists what will change and writes nothing;
   the apply makes exactly those changes and commits them; a repeat says "nothing to migrate".
2. `supervisor doctor`: every check OK, or each FAIL explained (a service that is simply not started is the usual
   one). An unexplained FAIL ends the window with a restore, not a start.
3. The project's own test suite, against the live files: green before the loop may run. A failure caused by the
   change is a stop; a failure that existed before the window is named as such, with the evidence.
4. `supervisor pause`, then start the loop and the Control Center (the project's start script). The loop comes up
   PAUSED, so the owner decides when work resumes.

## Checks it ends with

- `supervisor doctor` (G6): OK, or every FAIL explained.
- The project's test suite on the live files: green.
- The snapshot compared byte for byte, and its README written.

Report the window as a short record: when it ran, what was snapshot and where, each step's result, and the state it
left the project in (PAUSED, for the owner to resume).
