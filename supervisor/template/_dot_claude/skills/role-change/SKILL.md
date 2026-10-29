---
name: role-change
description: Add, activate, retire or re-model an agent role - its card under agents/, its routing entry in config/routing.yaml and its result schemas. Use when the user says "add a role", "activate the X role", "switch this role to another model", "retire this role", "/role-change".
---

# Changing a role

A role is three things that must agree: its **card** (`agents/<role>.md`, what it does and how it reports), its
**routing entry** (`config/routing.yaml`, under `roles:`) and the **schemas** its results are checked against
(`agents/schemas/`). Roles are edited by hand: the Control Center's Settings page shows capacity and limits, not
roles.

## 1. The routing entry

The keys, as the template's `config/routing.yaml` shows them:
- `adapter`: the lane (an agent lane, as the template's routing names them; `python` or `process` for a deterministic
  role, whose tasks then carry a `job` block);
- for an agent lane: `model`, `effort`, optionally `senior_model` (the second revision runs on it), and `tools`;
- `card`: the card file under `agents/` (`card: null` for a role with no card yet, which must then be inactive);
- `active`: `true`, or `false` to park the role, with `active_from: <phase>` recording when it starts;
- `result_kinds`: the kinds of result it returns (`delivery`, `review`, ...);
- `review`: how it runs when it reviews (`{tools: reviewer, write_scope: run_dir}` in the template).

A reviewer runs on a different vendor's lane than the workers it reviews, where one exists. Keep each value's
comment (an owner decision often lives there).

A **deterministic role** (`adapter: python` or `process`) takes none of the agent-lane keys (`model`, `effort`,
`senior_model`, `tools`, `review`). Its tasks carry the `job` block that runs them, and its jobs end with
`result.json`, read like a delivery: `result_kinds: [delivery]`. It still needs a card while it is active.

## 2. The card

- At most 400 words (count them: `wc -w agents/<role>.md`): the mandate, what it delivers, what it must not do, how
  it ends (the fenced result block its schema asks for).
- A deterministic role's card is read by people, not by an agent: what its jobs do, what they write, and that each
  ends with `result.json`.
- No model or vendor names (search the card for the names of the lanes and models in routing.yaml): those belong
  to routing, so a role can move lanes without a rewrite.
- No secrets, no machine paths.

## 3. Schemas

A role that returns a new kind of result gets its schema in `agents/schemas/<kind>.json`, with a valid and an
invalid sample beside it if the project keeps samples.

## 4. Retiring or re-modelling

- **Retire** means keeping the entry and the card, with `active: false` and a note saying why: tasks and records
  that name the role keep resolving. In this order:
  1. Set `active: false`. The role's open tasks are parked from then on, never sent elsewhere.
  2. `supervisor check` now warns about each open task naming the inactive role: that is the list to move.
  3. Move each with the task-author skill (`--update-contracts`), until `check` warns about none.
- **Re-model:** change only routing. If the new model needs a different effort or tool set, say so in the change.

## 5. Make the loop see it

Commit the change to `config/routing.yaml`, `agents/` and `agents/schemas/` yourself, with a message that says why:
the loop's own commits take only `state/` and the rendered files. You are the owner's session; a dispatched worker
never commits.

A running loop started before the change: `supervisor reload`. The loop keeps its decision across the reload (a
PAUSED loop comes back PAUSED). A loop started by `start_supervisor.bat` (`tools/run_supervisor.ps1`) restarts on the
new configuration; one started any other way stops (`supervisor run` exits with code 3), so start it again.

## Checks it ends with

- `supervisor doctor` (G6): routing and role cards OK. Doctor checks that the cards exist; the word count and the
  absence of vendor names are the checks in section 2.
- `supervisor check`: no WARN about a role that is unknown or inactive, unless the task is meant to wait for it.
- The project's own card or schema tests, if it has them. A new project has none, so the checks in section 2 are
  the only ones.
- `git status`: the routing, card and schema changes are committed.
