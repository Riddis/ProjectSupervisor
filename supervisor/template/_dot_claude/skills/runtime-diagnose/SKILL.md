---
name: runtime-diagnose
description: Find out why the whole project misbehaves - a stopped or blocked loop, a stale heartbeat, a lane that never runs, a missing extension, a service that is unavailable - from read-only evidence, and end with a supported recovery or a reproducible issue. Use when the user says "the loop is stuck", "nothing runs", "why is it blocked", "/runtime-diagnose". For one task that is BLOCKED or FAILED, use stuck-task instead.
---

# Diagnosing the running project

Read first, change nothing. Every step below only reads, with one exception: `supervisor doctor` and
`supervisor extension list` set the project's extensions up in order to report on them. That runs the extensions'
own code, and a load writes the loader's records: an `extension_error` line in `state/log.jsonl` for each optional
extension that fails, and the projection paths in `state/runtime/`. Run them after the other steps. The recovery at
the end goes through a supported command.

## 1. Collect the evidence

- `supervisor status`:
  - `project:` names the project it read;
  - `lock:` says whether a loop holds `state/.lock` (and its pid);
  - then the store's summary with the live runtime record. It writes nothing.
- `supervisor supervisor`, the runtime record itself:
  - `status`, `heartbeat`, `open_jobs` and `busy`;
  - `decision`, with who set it and when (`decision_by`, `decision_at`). `reason` can be stale: it is the last
    reason written, not necessarily the current one.

  A heartbeat much older than the loop's sleep interval, with the lock free, means the loop process is gone.
- `state/log.jsonl`, the last few hundred lines:
  - `loop_error`, `schedule_skip`, `capacity_*`, `extension_error`, `checkpoint_*` and `state_commit_*` events say
    what the loop decided and why;
  - the `command` events say which owner command (a `loop.pause`, say) changed it.
- `supervisor doctor`: which check is FAIL or WARN.
- `state/runtime/jobs/*.json`: the open records (phase PREPARED, SUBMITTED, RUNNING) and the newest finished ones.
- `supervisor extension list`: whether every configured extension loaded, and each error.
- `supervisor check`: WARN lines for tasks waiting on a role that is unknown or inactive, and for jobs naming a lane
  or python job that neither the framework nor an extension's manifest provides. It sets no extension up.
- For a lane: `state/runtime/usage_*.json` (its capacity windows) and the project's `config/routing.yaml` (its
  limits, reserves and whether its role is active).

## 2. Name the failing layer

Say which one, with the evidence that shows it:
- **the loop:** stopped, BLOCKED after repeated errors (read the `loop_error` events), or never started;
- **a decision:** PAUSE or DRAIN in force, set by the owner or by the loop (`decision_by`);
- **scheduling:** nothing eligible (dependencies, gates, an inactive role, overlapping `owned_paths`, a limit
  reached), or a lane deferring on its capacity reserve;
- **a lane or service:** an agent CLI missing or failing, a local service not started, credentials missing;
- **an extension:** failed to load (a required one blocks the loop), or refused by its manifest;
- **the framework:** a defect the evidence reproduces;
- **the environment:** disk, network, a driver, the operating system.

## 3. End with one of

- **A supported recovery,** stated as the exact command and why it is safe. For example:
  - `supervisor resume` or `supervisor reload`;
  - `supervisor cmd lane.reset --payload '{"lane": "<adapter>"}'`, or `supervisor cmd capacity.reset`;
  - starting a service;
  - fixing a config value, through the Control Center's Settings where it shows the value.

  The owner runs it, or approves it.
- **A reproducible issue:** what happens, the smallest steps that make it happen, the evidence, and which
  repository it belongs to: the project's (an extension, its config), an extension package's, or the
  framework's (`framework-change` there).

Never: delete job records or lock files, edit `state/` by hand, or restart repeatedly hoping it clears.

## Checks it ends with

- `supervisor doctor` (G6), read before and, after a recovery, again.
- No file under `state/` or `config/` changed by the diagnosis itself. Note `git status --porcelain -- state config`
  before you start, and compare at the end. A running loop changes its own records meanwhile (the log, the task
  records it updates), and `doctor` and `extension list` write the loader's records named at the top (an
  `extension_error` line; `state/runtime/` is not tracked). Any other change you made counts.
