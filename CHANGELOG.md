# Changelog

All notable changes to the framework. Versions follow semver: patch = fix, minor = feature or new extension hook,
major = a change that needs `supervisor migrate` (and says so under **Migration**).

## 1.1.0 — 2026-10-23

- Framework update check (`supervisor/updates.py`): at launch, `supervisor ui` fetches `origin`, lists the commits
  the clone is behind (with the version change and a migration hint from the changelog) and, on a terminal, asks
  whether to apply; `supervisor run` only reports. The Control Center shows the same as a banner with an **Update
  now** button (a background thread refreshes the check every 30 min). Applying = `git pull --ff-only`, reinstall
  the package, `supervisor migrate` + `supervisor doctor` in a child on the new code, `loop.reload` if a loop is
  live, and the UI exits with code 3 so the launcher restarts it. New CLI: `supervisor update [--check] [--yes]`.
  A dirty framework clone blocks the update and says so.
- Template: `tools/run_control_center.ps1` (restart-on-exit-3 wrapper); `start_control_center.bat` uses it.
- Guard rails: `supervisor init` refuses a target inside the framework clone; the framework `.gitignore` ignores
  project-shaped paths at its root so project work cannot be committed to the framework repo by accident.
- Fix: the Overview's Disk bar filled with the free fraction while its caption said "used"; it now fills with the
  used fraction (extension panels pass their own `progress` and are unaffected).
- Fix: `tasks.import_seed` now writes `status: NOT_STARTED` and the runtime fields explicitly, so a hand-written
  seed dispatches on the first run (the first launch used to crash on a missing `status` key).

## 1.0.0 — 2026-10-22

Initial release, extracted and generalised from a project-specific supervisor.

- `supervisor` package: loop, task store with the single `advance()` rule, one launch gate (decision, capacity,
  limits, file ownership), deterministic verification with a tracked verify job, independent reviews on frozen
  inputs with same-vendor fallback, retry budgets and the manager → architect → owner diagnosis route, owner inbox
  with digest-bound approvals, owner tasks, decisions, issues, chat threads, accepted-work git checkpoints, drain
  and shut-down-when-idle, credential policy (packet refusal, log redaction, child-env scrubbing).
- Adapters: `python`, `process`, `claude_cli`, `claude_api`, `codex_cli`, `human`.
- Control Center (FastAPI/HTMX): overview, roadmap, tasks, agents (live transcript, terminate, note), inbox
  (answer/snooze/reopen/gate ack/upload), chat with convert-to-task/decision/issue/owner-task, log, owner tasks,
  settings.
- Project boundary: `project.yaml` discovery (`SUPERVISOR_ROOT` or nearest ancestor), `supervisor init` from a
  packaged template, `extensions/` with a registry (adapters, doctor checks, overview panels, UI routers, packet
  screens, projections, CLI groups, python jobs), `supervisor doctor`, `supervisor migrate`, version check.
- Generic template: five role cards (senior_pm, chief_architect, engineer, code_review, research_strategist),
  four result schemas with samples, routing/policy/gates/seed templates, Windows launchers and a POSIX setup script.
- Setup skill (`.claude/skills/supervisor-setup`) wrapping `docs/SETUP_INTERVIEW.md`; `supervisor-update` skill.

**Migration** (from the pre-1.0 project-specific supervisor, if you ever point it at such a state directory):
`limits.max_gpu_jobs` → `limits.max_resource_jobs`; `cost_category: local_gpu` → `local`; inbox responses
`MORE_LIKE`/`REGENERATE` → `REDO`. `supervisor migrate` handles the first two.
