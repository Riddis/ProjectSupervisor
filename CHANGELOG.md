# Changelog

All notable changes to the framework. Versions follow semver: patch = fix, minor = feature or new extension hook,
major = a change that needs `supervisor migrate` (and says so under **Migration**).

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
