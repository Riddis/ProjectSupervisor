# Changelog

All notable changes to the framework. Versions follow semver: patch = fix, minor = feature or new extension hook,
major = a change that needs `supervisor migrate` (and says so under **Migration**).

## 1.2.2 — 2026-10-23

Third review round (on 1.2.1, commit c7274b9).

- Fix: the launch gate receives the role of every agent job kind — a diagnosis runs as its `diag_role`, a chat
  turn as the `conversation` lane role, a review as its reviewer — so `active: false` holds diagnoses too (they
  used to go SUBMITTED).
- Fix (process adapter): `recover()` looks for `result.json` again after the process scan. A child that finished
  while the scan ran was reported lost and launched a second time (duplicate execution of one attempt).
- Fix: recorded projection paths are keyed by the extension that rendered them (`state/runtime/projection_paths.json`
  `by_extension`); removing the extension from project.yaml removes its exclusions, so a hand edit to a former
  projection file is an undeclared write again.

## 1.2.1 — 2026-10-23

Second review round (on 1.2.0, commit 52cf048).

- Fix: a role configured `active: false` is refused by the launch gate itself (`gate.check`), not only by the
  scheduler's plan — a job queued before the role was disabled, a review for a dormant reviewer role (skipped in
  `review.pending`, logged once as `review_skip`) and a direct dispatch all stay PREPARED/waiting instead of going
  RUNNING/SUBMITTED.
- Fix (updater): a doctor child that exits non-zero without printing a FAIL line (an exception on the new code),
  and a pip reinstall that fails with empty output, fail the update; the exit code is the verdict, FAIL lines are
  detail.
- Fix: a `project.yaml` that cannot be parsed blocks the loop and the packet screens like a failed required
  extension (its `extensions:` list — and the policy screens it names — cannot be read), instead of loading no
  extensions at all.
- Control Center: the Roadmap has a **Pass gate** action (with the evidence an evidence gate requires); the
  Control Center acts as owner, so an owner gate is passed and acknowledged in one step and needs no CLI.
- Fix: projection files learned at render time are recorded in `state/runtime/projection_paths.json`, so the
  verify child and a restarted loop exclude them too (an untouched WEEKLY.md came back as an undeclared write
  after a restart). Declaring `registry.projection(fn, path=...)` remains the explicit form.
- Tests: scratch projects under `runs/` are fenced from the framework clone's git (`GIT_CEILING_DIRECTORIES`), so
  the suite passes with its default in-repo basetemp.

## 1.2.0 — 2026-10-23

Review round on 1.1.0 (seven blocking, eleven should-fix findings). No state migration is needed for projects
created with 1.0.0 or later; the one migration below only touches approvals recorded before 1.0.0.

- Fix (Control Center): Stop/Pause/Resume and every other HTMX action returned HTTP 500 on current Starlette
  (`TemplateResponse(name, context)` was removed upstream) although the command had been applied. The action
  helper now renders request-first like every page. The test suite exercises real HTMX requests and never fetches
  `origin` (`SUPERVISOR_UPDATE_CHECK=0`, also honoured by `supervisor ui` on offline machines).
- Fix (scheduler): `roles.<role>.adapter` in routing.yaml now decides the provider of agent work (model cost
  categories); it used to be ignored in favour of `cost_lanes`, so an engineer configured for Codex still ran on
  Claude. A role with `active: false` is never dispatched: its tasks are parked with a `schedule_skip` log entry.
- Fix (scheduler): the unattended `claude_cli → claude_api` switch is opt-in through
  `capacity.unattended_prefers_api_credits: true`; it used to spend API credits whenever a key was present.
- Fix (extensions): a configured extension that fails to import now blocks the loop (`BLOCKED`, reason names the
  extension) and makes `assert_packet_clean` refuse every packet, instead of silently running without the hooks
  it provides (a failed privacy screen let customer text through). `extensions: [{name: x, required: false}]`
  opts an extension out of blocking. Extensions load by file path under a per-project namespace
  (`supervisor_ext.<hash>.<name>`), no `sys.path` mutation: two projects' same-named extensions no longer share an
  implementation and an extension called `json` no longer resolves to the standard library.
- Fix (loop): an adapter that raises in `prepare/recover/submit/collect` fails that attempt (error on the job and
  the task) instead of consuming the loop's error budget and blocking the whole project.
- Fix (verify): extension-registered projections (`registry.projection(fn, path=...)`) are excluded from
  "undeclared writes" like the built-in views; a projection re-rendered during an attempt no longer fails it.
- Fix (gates): gates declare `owner_ack_required` / `evidence_required` (the template's G0 sets both). A gate is
  open only when PASSED and those are satisfied; an agent actor cannot pass an owner gate and PASSED without
  evidence is refused. Absent keys keep the old behaviour. `supervisor gate set` takes `--actor owner`.
- Fix (store): atomic writes use `tempfile.mkstemp` temp names; PID + `time_ns()` collided between threads on
  Windows (PermissionError on replace). The concurrency test fails on worker exceptions.
- Doctor: new `policy` check (`policy.config_problems()`), as the policy runbook already claimed.
- Docs/template: agents never commit (the supervisor checkpoints); the setup skill interviews and writes the plan
  before `supervisor init` and says which steps are CLI and which are file authoring; architecture/runbook
  statements corrected (owner-forced COMPLETE, `.jsonl` conversations, which commands queue vs refuse while the
  loop runs, what drain does); the Control Center launcher sets project-local TMP/TEMP; remaining references to
  the framework's source project removed and the blacklist test now covers `_vendor` and the template.

- Fix (updater): the updater stops a running loop (`loop.stop`, waits for `state/.lock` with a bounded timeout) before
  pulling and migrating — `supervisor migrate` refuses while the loop holds the lock, and the update used to report
  success anyway. A failing reinstall, migration or newly failing doctor check now fails the update (no restart,
  the error says what to do); a stopped loop is started again on the new code.
- `supervisor.launcher`: restart-on-exit-3 wrapper for the loop; the Control Center's Start button launches
  through it, so a UI-started loop survives Reload and framework updates like the console launchers.
- **Migration**: owner approval responses recorded before 1.0.0 are mapped to `ApprovalResponse`:
  `MORE_LIKE` → `REDO`, `REGENERATE` → `REDO` (both asked for another attempt at the same deliverable),
  `APPROVE` → `ACCEPT` (the inbox spelling). A task holding an old value could not be loaded. Inbox items'
  `allowed_responses` are mapped the same way. `supervisor migrate` applies it.
- Doctor: a malformed `project.yaml` or one without `framework_version` fails the project check (it used to pass
  on defaults); `supervisor migrate` refuses without a usable `project.yaml` instead of reporting a version change.
- `project.write` (migrations, `supervisor extension new`) preserves the comments and layout of `project.yaml`
  (ruamel round-trip, as Settings does for routing.yaml) instead of rewriting it from scratch.

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
