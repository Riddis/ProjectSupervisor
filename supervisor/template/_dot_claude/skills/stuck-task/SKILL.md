---
name: stuck-task
description: Decide what to do with one task that is BLOCKED, FAILED, escalated or waiting on the owner - read why it stopped, then continue it only through owner commands. Use when the user says "this task is stuck", "why is T-x blocked", "requeue it", "what do I do with this failure", "/stuck-task". For the whole loop misbehaving, use runtime-diagnose instead.
---

# A stuck task

The supervisor already retried what it could: up to two revisions per failure source (fewer when the same failure
repeats exactly), then a diagnosis by the manager role. When a task still stops, the decision is the owner's. This
skill reads, recommends, and acts only through the same commands the Control Center uses.

## 1. Read why it stopped

- `supervisor task show <id>`: status, history, attempts, the delivery's verification and reviews, notes, guidance.
- The issue that names it (`state/issues/`), and its owner inbox item if one is open (`supervisor inbox list`).
- The attempts' run folders (`runs/<id>/<attempt>/`):
  - for an agent task, the last attempt's `HANDOFF.md` (when it wrote one), its result, the verification report and
    the reviews' verdicts;
  - for a deterministic task (`python`, `process`), delivery and verification are separate attempts: the job's
    output (`stdout.jsonl`, `stderr.txt`, `result.json`) is in the delivery attempt's folder, and the verification
    report in the verify attempt's folder. There is no HANDOFF.
- The task's contract: did it fail on its contract (an artifact, a check, a role), on the work, on the job's own
  code, or on the environment (a lane, a service, capacity)?

Say which, with the evidence.

## 2. Choose one continuation

- **Requeue with guidance:** the work was close; say exactly what to change. Answer the task's owner decision item
  REQUEUE with the guidance as its text: `supervisor inbox answer <item> "REQUEUE <guidance>"` (or the Control
  Center). The next packet carries it, with a fresh retry budget. Without such an item, set the task READY with the
  reason as the note: `supervisor cmd task.set_status --payload '{"task_id": "<id>", "status": "READY", "note":
  "<why>"}'`.
- **Fix the job's own code:** for a deterministic task, guidance changes nothing; the script or the registered
  function is wrong. Fix it in the project (and commit the fix), then requeue as above.
- **Fix the contract first:** the task-author skill, then requeue.
- **Defer:** it waits for something else (a dependency, a decision, a release). Answer its owner decision item with
  DEFER, or leave it BLOCKED with a task note saying what it waits for; the stall sweep leaves an owner's hold alone.
- **Cancel:** it is no longer wanted. Set it to CANCELLED with the reason as the note.
- **Answer the owner item:** when an inbox item asks the question, answer it there (the Control Center, or
  `supervisor inbox answer`, or the `inbox.answer` command while a loop runs).

Recommend one, with why; the owner decides. Then apply exactly that.

## 3. Rules

- Never edit `state/tasks/`, `state/inbox/`, `state/issues/` or job records by hand, and never delete a run folder:
  every change is an owner command, so it is logged and committed.
- Never mark a task COMPLETE to get past it: only verification, reviews and approval reach COMPLETE. A forced
  status (`supervisor task set-status --force`) carries a note and is shown as forced.
- Log the reason as a task note (`supervisor task note <id> "<reason>"`), so the next reader knows what was decided.
- An issue stays as the record of what went wrong, even after the task completes. There is no command to close
  one: the task note says how it was resolved.

## Checks it ends with

- `supervisor check`: no ERROR.
- `supervisor task show <id>`: the task reads as intended, with the note.
- `supervisor doctor` (G6).
