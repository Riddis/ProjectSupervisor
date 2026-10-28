---
name: supervisor-setup
description: Set up this project to run under the Supervisor framework — interview the owner, write the plan, get approval, scaffold roles/gates/tasks. Use when the user says "set up this project", "/supervisor-setup", or the project is freshly initialised and has no approved plan yet.
---

# Supervisor project setup (project-side stub)

This project is run by the Supervisor framework, which is installed into `.venv` from a local clone. The full
procedure lives in the framework repo, not here, so it stays current when the framework updates.

1. Locate the framework: `python -c "import supervisor, pathlib; print(pathlib.Path(supervisor.__file__).parents[1])"`
   (use `.venv\Scripts\python.exe` on Windows). If that fails, the framework is not installed yet: run
   `tools\setup_env.ps1 -Framework <path to the Supervisor clone>` (or `tools/setup_env.sh <path>`).
2. Read, from that folder: `.claude/skills/supervisor-setup/SKILL.md`, `docs/SETUP_INTERVIEW.md`,
   `docs/TASK_CONTRACT.md`, `docs/FRAMEWORK_VS_EXTENSION.md`.
3. Follow them, with **this** folder as the project. Nothing in the framework folder is edited during setup.
4. The other skills beside this one came with the framework and stay as shipped: `supervisor skills status` says
   whether they are current, and `supervisor skills update` brings in a newer release's versions without touching
   one you edited.
