# {{ name }}

A project run by the [Supervisor framework](https://github.com/) (framework {{ framework_version }}): a deterministic
Python supervisor dispatches contracted tasks to AI agents, verifies every delivery, routes reviews to an independent
lane, and parks anything that needs a human in the owner's inbox. The owner works from the **Control Center**
(`start_control_center.bat`), never from YAML.

**Status:** fresh project — the setup skill (or you) still has to fill in `project.yaml`, `docs/plan/PROJECT_PLAN.md`,
`config/routing.yaml`, `agents/`, `state/gates.yaml` and `state/tasks_seed.yaml`.

## Resume protocol (for a fresh agent session — do not rely on chat history)

1. Read `STATE.md` — current phase, gates, in-flight work, open owner items, "next steps".
2. Read `TASKS.md` — eligible tasks are marked ⚑; open `state/tasks/<ID>.yaml` for the full contract.
3. Read the newest files in `state/decisions/` and open items in `state/issues/`.
4. Run `supervisor check` and `supervisor status` (from this folder; on Windows `.venv\Scripts\python.exe -m supervisor`).
   `supervisor task import` only creates tasks that do not exist yet.
5. Work **one** task at a time: `supervisor task set-status <id> IN_PROGRESS` → implement → test →
   `supervisor task digest/deliver/verify` → review (`supervisor task review`) and owner approval
   (`supervisor task approve`) when the task requires them → `supervisor render` → commit with the task id → next task.
   COMPLETE is only reachable through those records.
6. State is single-writer: never edit `state/` while `supervisor run` holds `state/.lock`; use `supervisor` commands or
   the Control Center (they queue command files the loop applies).
7. Near a usage limit or interruption: write `runs/<task>/<attempt>/HANDOFF.md`, record its path in the task YAML,
   commit. Leave the repository consistent.

## Layout

`project.yaml` identity, phase labels, extensions · `docs/plan/` the approved plan · `docs/runbooks/` project
runbooks · `state/` authoritative tasks/gates/decisions/issues/inbox · `agents/` role cards and schemas · `config/`
routing and policy · `extensions/` project-specific Python the framework loads · `tools/` scripts · `runs/`,
`secrets/`, `uploads/` git-ignored.

Rendered files (`STATE.md`, `TASKS.md`, `ISSUES.md`, `DECISIONS.md`, `HUMAN_INBOX.md`, `OWNER_TASKS.md`) are
projections of `state/` and must not be edited by hand.

## Key rules

- Software does software; agents do judgement.
- Process success ≠ acceptance: every delivery is verified deterministically, important work is independently reviewed.
- No new recurring cost, external commitment or irreversible action without an owner-approved proposal.
- Framework code lives in the Supervisor repo; anything specific to this project lives here (`extensions/` for code).
