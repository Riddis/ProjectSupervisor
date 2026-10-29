---
name: supervisor-setup
description: Set up a new project to run under the Supervisor framework — interview the owner about the project, write a plan (environment, roles, phases/gates, tracker, repo), get it approved, then scaffold project.yaml, routing, agent cards, gates, first tasks and extensions. Use when the user says "set up a project with the supervisor", "new supervisor project", "/supervisor-setup", "plan this project for the control center", or opens an empty/fresh `supervisor init` folder and wants to start.
---

# Supervisor project setup

You are turning a folder into a planned, gated project that the Supervisor framework can run. The owner is a person
who wants to answer questions, approve a plan, and then watch the Control Center — not edit YAML. Follow
`docs/SETUP_INTERVIEW.md` in the framework repo exactly; this file tells you how to run it well from here.

## Before you ask anything

1. Find the framework and the project. The framework is the repo containing this skill (`supervisor/` package,
   `docs/SETUP_INTERVIEW.md`); the project is the folder the owner is working in. If they are the same folder, stop:
   projects are never created inside the framework repo — ask where the project should live.
2. Read `docs/SETUP_INTERVIEW.md`, `docs/TASK_CONTRACT.md` and `docs/FRAMEWORK_VS_EXTENSION.md` from the framework.
3. In the project folder: read everything Stage 0 lists. If it already is a supervisor project (`project.yaml`
   exists) also run `supervisor doctor`. If it is not, do **not** run `supervisor init` yet — init stamps config,
   cards, gates and extension skeletons, and nothing but the plan is written before approval. Tell the owner in two
   lines what you found already decided.

## Running the interview

- One question per turn, in the order of Stage 1, each with its default stated so the owner can just say "default".
  Skip questions the folder already answers; say which ones you skipped and why.
- Use the host's question tool when it has one (multiple-choice with the default first); otherwise ask in plain
  text. Never batch more than two questions.
- Keep a running "answers so far" in the plan draft itself (`docs/plan/PROJECT_PLAN.md`), so an interrupted session
  can resume and the plan stays the only file written before approval.
- When the owner's answer reveals a technical unknown ("I don't know which test runner"), do not investigate now:
  write it as an open question and make it a phase-0 task.
- After Q9 (phases) and Q10 (tasks), show the proposal as a compact table and ask for edits before moving on.

## The plan and the approval

- Write `docs/plan/PROJECT_PLAN.md` by hand (the template's eight sections, filled, with a "Setup summary" at the
  top listing the files the approval will create/change; copy the headings from
  `supervisor/template/docs/plan/PROJECT_PLAN.md` in the framework when the folder has no plan yet, and fill its
  `{{ name }}` by hand: nothing fills it before `init`). Its header names the roles that rule and plan from it;
  name the kept ones, or the owner when the plan keeps no agent role. Show it. Revise
  until the owner says "approved" — the literal word, or an unambiguous equivalent. Record the date in the plan.
- **Do not create or modify config, cards, gates, tasks or extensions before approval.** The plan file is the only
  thing you write before then; `supervisor init` keeps an existing plan file, so writing it first is safe.

## Scaffolding (after approval)

Do Stage 3 steps 0–12 in order. Step 0 is the CLI command `python -m supervisor init <folder> --name "<name>"` (the
project's venv if it exists, else any Python with the framework installed) when the folder has no `project.yaml`;
it stamps generic defaults that the following steps adapt. If the folder is not a git repository yet, `git init` it
right after: doctor's `git` check and the setup commit need one, and the loop commits accepted work into it. Steps
1–4 and 7–9 are direct file authoring; steps 5, 6, 10 and 11 run CLI/git commands. Specifics that matter:

- Cards: start from `supervisor/template/agents/*.md`; adapt mandate/authority/escalation to the owner's answers;
  ≤ 400 words; never a model or vendor name (the framework's tests enforce this for the template, and the project's
  reviewers will hold the same line). Every role in `routing.yaml` with `active: true` needs a card; dormant roles
  get `card: null`, `active: false`, and a `note`.
- `routing.yaml`: keep the template's comments on what stays; only change values. Remove, with their comments,
  whatever the owner does not have or want:
  - roles the plan drops;
  - `cost_lanes` entries for lanes the owner has no account or CLI for (a local-only project keeps none of the agent
    lanes; otherwise the loop still polls an installed CLI's capacity). Removing an entry stops that poll, not the
    routing: the defaults fill it in again, so a task on a model `cost_category` (`claude`, `codex`, `mixed`,
    `paid`) still goes to an agent lane. In a project with no agent lane, every task has a `job` block or a
    non-model `cost_category` (`deterministic`, `local`, `human`);
  - the agent sections such a project has no use for: `capacity`, `claude_api`, `claude`, `codex`;
  - the `escalation` roles, when they are agent roles the plan does not keep: point them at kept roles, or set
    `escalation.diagnosis: false` and remove `manager_role` and `architect_role` with it.

  Keep the `conversation` entry: Control Center chat runs on its lane (the agent CLI lane when the entry names
  none), and `active` never parks a reply. A project with no agent CLI therefore has no chat; say so to the owner
  and in the plan.

  If only one agent CLI exists, set `code_review` to that lane and keep `fallback.flag: same_vendor` with a plan note
  that reviews are same-vendor until a second lane exists. A role whose work is deterministic (no model) keeps a
  routing entry and a card like any other, with a deterministic lane (`adapter: python` or `process`); its tasks carry
  a `job` block, and a reviewer that is not an agent is either none (`reviewer: []`) or the owner (`human_approval:
  required`, answered APPROVE in the inbox).
- Tasks: each one must pass `supervisor check` (dependencies exist, no cycles, model-lane tasks declare
  `owned_paths`). Phase 0 always ends with a task whose artifact is the project's own runbook
  (`docs/runbooks/<project>.md`: how to run, test, and recover it).
- A project that keeps no agent role: the owner plans the later phases and writes what no job can (an owner task
  for each, with a date), and the runbook comes from a deterministic job or an owner task. Say so in the plan, in
  place of the interview's planning roles.
- Extensions: first look for an existing extension package that does the job (a package the owner names; the
  framework's catalog index arrives with phase 6), and install it rather than write a new one. Otherwise follow the
  framework's `extension-new` skill: `supervisor extension new <name>` writes the module and its manifest in the
  standard shape (`docs/EXTENSION_STANDARD.md`); implement what the owner described; anything unknown becomes a
  `TODO` plus a phase-0 task "finish and prove the <name> lane" with a doctor check as acceptance. `extension new`
  lists the extension in `project.yaml` itself; do not add it a second time. Each extension passes `supervisor
  extension check <name>` (G5) before the setup commit.
- Decision records: `D-001-project-setup.md` plus one per numbered decision in plan §7; front matter
  `id/date/title/status: accepted/decided_by: owner`. Quote the title (`title: "Project setup: the approved
  plan"`): a title holding `: ` is invalid YAML without quotes.
- Line endings: the template ships no `.gitattributes`. When the project keeps files git could read as text but
  must keep byte for byte (a placeholder PDF, a fixture), add one (`*.pdf binary`) before the setup commit.
- Project code stays in the project. If the interview shows the owner also needs a framework change (a generic
  hook, a fix), that is separate work through the framework's `framework-change` skill, never an edit made during
  setup.
- Only when the owner will also change the framework from this machine, and with their go (both touch files
  outside the project): check `git config --get core.hooksPath` in the framework clone first, and only when it does
  not print `.githooks`, run `git config core.hooksPath .githooks` there, so its boundary check (G1,
  `docs/BOUNDARY.md`) runs before every commit there. Record the owner's answer in `D-001`. Also add the
  project's distinctive words (its name, its extensions' identities, its own terms) to the local project-terms
  file, `~/.config/supervisor/project-terms.toml` (or the path in `SUPERVISOR_PROJECT_TERMS`), outside every
  repository. Its format is one table per project:

  ```toml
  [projects.<project>]
  terms = ["<word>", "<another word>"]
  ```

  G1d fails any of them in the framework repo, which never holds a project's files or information.
- The project's own skills: `supervisor init` stamped the framework's project-side skills into `.claude/skills/`
  (this setup stub, and maintenance-window, rehearsal, project-upgrade, project-restore, runtime-diagnose,
  task-author, stuck-task, role-change). Leave them as shipped; `supervisor skills status` says whether they are
  current, and later releases bring newer ones with `supervisor skills update`. A skill only this project needs goes
  beside them under its own name.
- Finish with `supervisor doctor`, `supervisor check` (no ERROR, and every WARN explained), `supervisor render`, and
  the single setup commit. Report the doctor output verbatim; a FAIL you cannot fix becomes an owner task. One FAIL
  is expected when the framework checkout is not at a release tag (a development clone): `framework: HEAD is not at
  a tag`. Say so; it clears once the project runs a tagged checkout (the framework's `docs/UPDATING.md`, "Pinned
  checkouts").

## Hand-over message

End with: how to start (Control Center first), the first eligible task, open owner tasks, and the one sentence that
the interview is not re-run — later changes go through the Control Center chat, decision records and
`supervisor task import --update-contracts`.

## If the framework is not installed in the project

`py -3.11 -m venv .venv` then `.venv\Scripts\python.exe -m pip install -e "<framework path>[ui]"` (Windows), or
`tools/setup_env.sh <framework path>` once the template is stamped. Both download the framework's dependencies, so
they need network access, and `setup_env.ps1` also sets a user environment variable: say so to the owner before
running it. Never `pip install supervisor` from PyPI — that is an unrelated package. Until the venv exists, the
start scripts do not work; the hand-over then says how to start once it does.
