# Project setup interview

This is the procedure that turns an empty folder (or a bare `supervisor init` one) into a planned, gated,
ready-to-run project. It
is written for any agent (Claude Code, Codex, Cowork, a human with a notebook); the Claude skill in
`.claude/skills/supervisor-setup/SKILL.md` is a thin wrapper around it. The output is a plan the owner approves, then
a project whose first phase the supervisor can start.

It assumes the hard technical investigation is **not** part of setup: the owner already knows roughly what they are
building and on what. Where they do not, the plan's first phase becomes a research phase run *by* the supervisor
(research_strategist tasks), not by this interview.

Principles: ask one thing at a time, offer a recommended default for every question, never ask something you can
read from the folder, stop and write the plan as soon as you have enough — a plan the owner can correct beats a
longer interview. Nothing below the **Approval** line happens without an explicit "approved".

---

## Stage 0 — look before asking

Read, if present: `project.yaml`, `README.md`, `docs/plan/PROJECT_PLAN.md`, `config/routing.yaml`,
`state/tasks_seed.yaml`, `state/gates.yaml`, the git log, any existing source tree. Run `supervisor doctor` and
`supervisor version` if `project.yaml` exists. Note what is already decided; skip those questions (a freshly stamped
template — untouched placeholders, the template's comments intact — is *not* a decision: its values are the defaults
you offer). If the folder is
not a supervisor project yet, do **not** run `supervisor init` now: init stamps `config/`, `agents/`, `state/gates.yaml`
and `extensions/` with generic defaults, and nothing but the plan is written before approval (Stage 3 step 0 runs it).

## Stage 1 — the interview

Each question lists what it decides and a default. Record every answer verbatim; they become the plan.

**1. What is the project, in one paragraph, and what does "done" look like?**
Decides: `project.yaml description`, plan §1. No default. If the owner cannot say what done looks like, the
first phase is "define done" with a research/strategy task.

**2. What kind of thing is it?**
Options: software (library/app/service/game), content (documents, a book, a site), data/analysis, operations (a
recurring process), mixed. Decides: the role set (software → engineer/code_review/architect; content → writer/
editor/reviewer; data → analyst/engineer/reviewer), the artifact conventions, the verification commands.

**3. Where does the work live?**
This folder (default), another existing repo (path), or a fresh repo to create. Remote URL if known (informational;
pushes stay manual). Default branch. Decides: `project.yaml repo`, checkpoint behaviour (needs a git repo).

**4. Language, runtime, toolchain, and the single command that proves it works.**
E.g. "Python 3.12, uv, `python -m pytest -q`"; "Node 22, `npm test`"; "Rust 1.80, `cargo test`"; "none — prose,
checked by `vale .`". Decides: `verification.allowed_test_commands`,
`claude.bash_allow`, the engineer card's toolchain line, `checks` on tasks. Default for Python: the template's list.

**5. Which agent lanes does the owner have, and what should run where?**
Claude Code subscription (yes/no, which plan), Codex CLI (yes/no), an API key budget (default: none), local models
(default: none). Decides: `config/routing.yaml` roles' `adapter`/`model`, `capacity` reserves (default 20 % claude /
10 % codex — "stop automation with this much of the window left for the owner's own use"), `limits.max_agent_jobs`
(default 2), whether `code_review` can be cross-vendor (needs both CLIs; otherwise same-vendor with the flag and
a note in the plan). Ask which Codex model the account accepts if Codex is used (`codex` app-server model/list);
record it.

**6. Any tool, service, device or resource the agents must drive that is not a CLI or git?**
E.g. a renderer, a simulator, a database, a hardware device, an external API, a browser. For each: how it is invoked,
whether only one job may use it at a time, what a health check looks like, what credentials it needs. Decides:
`extensions/` modules to scaffold (`supervisor extension new <name>`), `cost_lanes.local`, `limits.max_resource_jobs`,
doctor checks, `secrets/` entries, owner tasks for installs/logins. Default: none.

**7. Who are the roles?**
Show the default set for the project kind (from Q2) with one line each; ask what to add, remove, rename. For every
role: what it decides, what it may not decide, who it escalates to, which result kinds it produces. Decides:
`agents/<role>.md` cards (written from the template cards; cards never name a model), routing entries,
`result_kinds`. Default: the template's five plus owner.

**8. What must the owner personally approve?**
Options: nothing beyond gates (default), every delivery of certain tasks (`human_approval: required`), spend, anything
external (publishing, sending, deploying, purchasing), anything irreversible. Decides: `human_approval` on tasks,
inbox kinds, a packet screen if some content must not leave the machine, the plan's §2 constraints.

**9. Phases and gates.**
Propose 2–6 phases from the answers so far (a small recurring operation may need only two: prove it, then run it),
each with a goal, an exit gate, the evidence that gate needs, and whether the owner must acknowledge it. Ask the
owner to edit. Default phase 0 is always "Foundations": repo, toolchain, test runner, the project's own runbook —
ending at G0, which the owner acknowledges. Decides: `project.yaml phases`,
`state/gates.yaml`.

**10. The first phase's tasks.**
Decompose phase 0 (and phase 1 if the owner wants a running start) into 4–10 tasks following
`docs/TASK_CONTRACT.md`: one owner role, named artifacts, prose tests, executable checks where a command exists,
owned paths, reviewers, acceptance sentence. Ask the owner to edit. Decides: `state/tasks_seed.yaml`.

**11. What does the tracker need to show that the standard records do not?**
E.g. a field per task (an estimate, a client, a component), an extra projection (a weekly digest), an extra
Control Center panel (a build status). Decides: extra task fields (tasks accept extra keys; name them in the plan so
agents fill them), `extensions/` projections/panels. Default: nothing extra.

**12. Owner tasks and open questions.**
Logins, installs, purchases, decisions the owner still owes. Decides: `state/owner_tasks.yaml`, plan §8.

**13. Operating preferences.**
Unattended overnight runs (default no), "shut down PC when idle" (default off), Control Center port (default 8790),
how much the owner wants to be asked (default: gates + spend + irreversible). Decides: plan §2, routing
`unattended_prefers_api_credits`, `ui.port`.

## Stage 2 — write the plan

Write `docs/plan/PROJECT_PLAN.md` by hand (no CLI command does this) with the template's eight sections filled from
the answers (quote the owner where it matters; the headings are in `supervisor/template/docs/plan/PROJECT_PLAN.md`
in the framework), §5 including the first phase's task list from Q10, plus a short **Setup summary** at the top
listing exactly which files the approval will create or change. Show it to the owner. Iterate until they say
"approved" (record the date). `supervisor init` keeps existing files, so the plan survives step 0 below.

---

## Approval line — nothing below runs without an explicit "approved"

## Stage 3 — scaffold

In this order, each step idempotent. Steps marked *CLI* are `supervisor` (or git) commands; the rest are files you
author directly.

0. *CLI* — if there is no `project.yaml`: `supervisor init <folder> --name "<name>"` (ask for the name if it is not
   obvious). This stamps generic defaults for everything below; the approved plan may still change any of them.
1. `project.yaml`: `name`, `description`, `repo`, `phases` (labels), `ui.port`, `extensions` (names only; modules
   come in step 6).
2. `config/routing.yaml`: roles (adapter/model/effort/tools/card/active/active_from/result_kinds/review/fallback),
   `capacity`, `limits`, `verification.allowed_test_commands`, `claude.bash_allow`, `codex.model`, `cost_lanes`.
3. `agents/<role>.md` for every active role (≤ 400 words; mandate, authority, escalates to, how you work, deliver,
   lines you do not cross; no model or vendor names). Remove cards for roles the owner dropped; keep schemas.
4. `state/gates.yaml` from §5 of the plan; `state/owner_tasks.yaml` from §8.
5. `state/tasks_seed.yaml` from the phase-0 task list in §5 of the plan; then *CLI* `supervisor task import`.
6. *CLI* `supervisor extension new <name>` for each resource from Q6, then fill the adapter/check skeleton with what is known,
   leave a `TODO` for what is not, add a task in phase 0 to finish and prove it.
7. `state/decisions/D-001-project-setup.md` recording the approved plan (date, the owner's answers that constrain
   later work), and one record per numbered decision in plan §7.
8. `secrets/README.md` listing the credentials the project will need (names only).
9. `README.md`: replace the "Status" line with the current phase and the next task.
10. *CLI* `supervisor doctor` → every check OK or explained; `supervisor check` → graph valid; `supervisor render`.
11. *CLI* `git add -A && git commit -m "Project setup: approved plan, roles, gates, phase 0 tasks"` — the one commit setup
    makes itself (the supervisor checkpoints everything after this).
12. Tell the owner: how to start (`start_control_center.bat`, then Start in the UI, or `start_supervisor.bat`), what
    the first task is, which owner tasks are open.

## Stage 4 — hand over

The supervisor runs phase 0. The Senior PM role plans phase 1 in detail when G0 is near (in a project that keeps no
planning role, the owner does, as an owner task); the owner approves gates in the inbox. This interview is not run
again: later changes go through the Control Center chat ("convert to task / decision"), decision records, and
`supervisor task import --update-contracts`.
