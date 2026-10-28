---
name: role-change
description: Add, activate, retire or re-model an agent role - its card under agents/, its routing entry in config/routing.yaml and its result schemas. Use when the user says "add a role", "activate the X role", "switch this role to another model", "retire this role", "/role-change".
---

# Changing a role

A role is three things that must agree: its **card** (`agents/<role>.md`, what it does and how it reports), its
**routing entry** (`config/routing.yaml`, `roles.<role>`: lane, model, effort, tools, review lane, whether active)
and the **schemas** its results are checked against (`agents/schemas/`).

## 1. The card

- At most about 400 words: the mandate, what it delivers, what it must not do, how it ends (the fenced result
  block its schema asks for).
- No model or vendor names in the card: those belong to routing, so a role can move lanes without a rewrite.
- No secrets, no machine paths.

## 2. The routing entry

- `adapter` (the lane), `model`, `effort`, the tools it may use, and its review lane.
- `active: false` parks the role: its tasks wait with a `schedule_skip` log entry. Activate it when the phase that
  needs it opens (`active_from` records which).
- A reviewer runs on a different vendor's lane than the workers it reviews, where one exists.
- Edit values the Control Center's Settings page shows through Settings; for others, edit the file by hand and keep
  each value's comment (an owner decision often lives there).

## 3. Schemas

A role that returns a new kind of result gets its schema in `agents/schemas/<kind>.json`, with a valid and an
invalid sample beside it if the project keeps samples.

## 4. Retiring or re-modelling

- Retire: set `active: false` first, and check that no open task names the role as `owner_role` or `reviewer`
  (`supervisor task show`, or the seed). Move those tasks to another role with the task-author skill.
- Re-model: change only routing. If the new model needs a different effort or tool set, say so in the change.

## Checks it ends with

- `supervisor doctor` (G6): routing and role cards OK.
- `supervisor check`: no task names a role that no longer exists or is inactive without a reason.
- The project's own card or schema tests, if it has them.
