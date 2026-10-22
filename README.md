# Supervisor

A project-agnostic framework for running a project with AI agents: a deterministic Python **supervisor loop**
dispatches contracted tasks to agent CLIs (Claude Code, Codex) or deterministic jobs, verifies every delivery against
its contract, routes reviews to an independent vendor lane, parks anything that needs a human in the owner's inbox,
and checkpoints accepted work to git. The owner runs it from a local **Control Center** (web UI) and never has to
touch YAML or a console.

Software does software; agents do judgement; the owner decides.

```
Supervisor/                 the framework (this repo) — generic, installed into every project
  supervisor/               the Python package: loop, store, gates, review, inbox, adapters, Control Center
  supervisor/template/      what `supervisor init` stamps into a new project
  .claude/skills/           the setup skill (interview → plan → approval → scaffold)
  docs/                     how it works, how to extend it, runbooks

<your project>/             a thin project folder — everything specific to one project
  project.yaml              name, phase labels, extensions, framework version
  config/routing.yaml       roles → lanes/models, limits, verification commands
  agents/                   role cards + result schemas
  state/                    tasks, gates, decisions, issues, inbox (the truth; rendered to *.md)
  extensions/               project-specific Python the framework loads (adapters, checks, UI panels)
  docs/plan/PROJECT_PLAN.md the approved plan
```

## Quick start

```powershell
git clone <this repo> D:\Repos\Supervisor
mkdir D:\Repos\my-project && cd D:\Repos\my-project
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -e "D:\Repos\Supervisor[ui]"
.venv\Scripts\python.exe -m supervisor init . --name "My project"
git init
```

Then either run the **setup skill** from Claude Code / Cowork with this folder open (`/supervisor-setup`, or
"set up this project with the supervisor"), which interviews you, writes the plan, waits for your approval and fills
`project.yaml`, `config/`, `agents/`, gates and the first tasks — or follow `docs/SETUP_INTERVIEW.md` by hand with
any agent. Afterwards:

```powershell
.venv\Scripts\python.exe -m supervisor doctor        # tools, state, routing, extensions
.venv\Scripts\python.exe -m supervisor task import   # tasks from state/tasks_seed.yaml
start_control_center.bat                             # UI at http://127.0.0.1:8790/
start_supervisor.bat                                 # loop + UI
```

Linux/macOS: `tools/setup_env.sh` in the project does the venv + install; run `python -m supervisor ...` from the
project folder.

## How a task flows

1. A task record (`state/tasks/<id>.yaml`) carries the **contract**: objective, actions, artifacts, tests, executable
   checks, owned paths, reviewer roles, whether the owner must approve. See `docs/TASK_CONTRACT.md`.
2. The loop picks eligible tasks (dependencies complete, phase gate open) and **dispatches** each to the adapter its
   role/cost category routes to — through one launch gate that checks the owner's decision (RUN/PAUSE/DRAIN/STOP),
   measured subscription capacity, concurrency limits and file ownership.
3. The agent works in a fresh session with a packet (role card + contract + handoff instructions) and ends with a
   fenced JSON result. The supervisor **verifies** deterministically: artifacts exist, nothing was written outside the
   owned paths, declared tests and contract checks pass when re-run (never on the agent's word).
4. Required **reviews** run on frozen inputs in an independent lane; a REVISE sends the failure verbatim to a revision
   attempt (second revision on the senior model); after the retry budget a bounded **diagnosis** decides requeue /
   defer / architect / owner.
5. Tasks that need the **owner** park in `WAITING_FOR_HUMAN` with an inbox item; the Control Center shows it. A task
   reaches `COMPLETE` only through verification ∧ reviews ∧ (approval if required) — then the supervisor **checkpoints**
   exactly the accepted files to git.
6. Phases open when their **gates** pass (evidence + owner acknowledgement where the plan says so).

## Documentation

- `docs/ARCHITECTURE.md` — the modules and the invariants they keep.
- `docs/TASK_CONTRACT.md` — task, gate, decision, issue, inbox and owner-task records.
- `docs/FRAMEWORK_VS_EXTENSION.md` — what goes in the framework, what goes in a project, how extensions work.
- `docs/SETUP_INTERVIEW.md` — the project setup interview (system-agnostic; the skill wraps it).
- `docs/UPDATING.md` — updating the framework under running projects (the launch-time check and **Update now**); versioning and migrations.
- `docs/runbooks/` — supervisor loop, Control Center, Claude/Codex/API lanes, policy, environment.
- `CHANGELOG.md`.

## Requirements

Python 3.11+ (3.11 recommended on Windows), git, and the agent CLIs your roles use: Claude Code (`claude`) signed in
to a subscription, and/or the Codex CLI (`codex`) signed in. No API keys are needed unless you enable the metered
`claude_api` lane. Windows is the primary platform (PowerShell/batch launchers ship with the template); the package
itself runs on Linux/macOS and the test suite runs there.

## Naming

The package is `supervisor`; the distribution is `supervisor-framework`; the GitHub repo is `ProjectSupervisor`. If `supervisor` ever collides with another
package in a project venv (the unrelated `supervisord` is published as `supervisor` on PyPI), install this framework
first or in its own venv per project — the template's `setup_env.ps1` does the latter.

## Tests

```
python -m pytest -q
```

Runs on Linux and Windows; the agent CLIs are faked (`tests/fixtures/`). ~400 tests, under a minute.
