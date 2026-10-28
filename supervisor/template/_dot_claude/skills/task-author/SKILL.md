---
name: task-author
description: Write or fix a task contract - in the seed (state/tasks_seed.yaml) or as a new task - so the supervisor can dispatch it, verify it and review it without guessing. Use when the user says "add a task", "write the task for X", "fix this task's contract", "why does this task keep failing verification", "/task-author".
---

# Writing a task contract

Most tasks that fail, fail on their contract: an artifact nobody can match, a check that is not on the allowed
list, a reviewer that is not an active role. The field list is the framework's `docs/TASK_CONTRACT.md` ("Task");
find it with `python -c "import supervisor, pathlib; print(pathlib.Path(supervisor.__file__).parents[1])"`.

## 1. Where the contract lives

- A task the plan already has goes in `state/tasks_seed.yaml`. `supervisor task import` creates the seed's tasks
  that do not exist yet, and `--update-contracts <id>,<id>` refreshes the contract fields of tasks that do. That
  keeps the seed the source.
- A one-off task: `supervisor task create`. It goes through the loop while one runs.
- Never edit `state/tasks/<id>.yaml` by hand.

## 2. The fields that decide whether it can work

- **`artifacts`:** every file the task writes, as repo-relative files or globs (`docs/report.md`,
  `research/*.json`), never a bare folder.
  - Each entry must be able to match a file.
  - The list is the floor: list the worker's own tests, notes and evidence too. A file the worker writes outside
    its artifacts is not delivered, so it is neither verified nor committed when the task is accepted.
- **`checks`:** commands the supervisor re-runs on every delivery, each one matching
  `verification.allowed_test_commands` in `config/routing.yaml`. Prefer a test file the task itself delivers.
- **`tests` and `acceptance`:** what must be true, in words a reviewer can check without asking what you meant.
- **`owned_paths`:** required for every model-lane task. Keep it to the roots the work needs; `["."]` makes the
  task exclusive (nothing else runs meanwhile).
- **`owner_role` and `reviewer`:** active roles from `config/routing.yaml`. A reviewer on a different vendor's lane
  than the worker, where one exists.
- **`cost_category`, or a `job` block:** which lane runs it. A deterministic job (`python`, `process`) needs its
  `fn` or `argv`; a model lane needs nothing more than the role's routing.
- **`depends_on` and `phase`:** only what the task truly waits for. A phase whose gate is closed holds it.
- **`human_approval`:** `required` only when the owner must look at this delivery, with an `approval_note` saying
  what to look at.

## 3. Check before it runs

1. `supervisor task import --dry-run` (with `--update-contracts <ids>` when refreshing): it reports what would be
   created or refreshed, writes nothing, and refuses an artifact that cannot match a file, an unknown role, a
   cycle.
2. `supervisor check`: the whole store validates (fields, enums, dependencies, cycles).
3. Then import for real, and `supervisor task show <id>` to read it back.

When fixing a task that already ran, say which attempt failed on which field, and what changed. If the task is
BLOCKED, the stuck-task skill decides how it continues; this skill only fixes the contract.

## Checks it ends with

- `supervisor task import --dry-run` (or `task create`'s validation): no refusal.
- `supervisor check`: the store validates.
- `supervisor doctor` (G6): routing and roles OK.
