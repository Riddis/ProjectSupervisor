# Runbook — supervisor loop

- Start: the project's `tools\run_supervisor.ps1` (restarts the loop when it exits with code 3 = reload) or directly
  `.venv\Scripts\python.exe -m supervisor run` from the project folder (holds `state/.lock`; exit 0 = stopped, 3 =
  reload requested, 2 = another supervisor runs, 1 = blocked after 5 loop errors in 10 min). `start_supervisor.bat`
  does this plus the Control Center.
- Which project: `SUPERVISOR_ROOT` if set, else the nearest ancestor of the working directory with a `project.yaml`.
- Single writer, two kinds of mutating CLI command. Most task commands (`task set-status/create/set-job/deliver/
  verify/review/approve/advance/note/import`, `pause/resume/stop/reload/drain`, `cmd <kind> <json>`) go through
  `submit_or_apply`: with a loop running they queue a command file the loop applies; without one the CLI applies
  them directly. The few that write `state/` themselves (`task migrate-ownership`, `gate set/ack`,
  `inbox answer`, `migrate`) refuse while a supervisor holds `state/.lock`; stop the loop first, or use the Control
  Center / `supervisor cmd` equivalents (`gate.set`, `gate.ack`).
- Control (any shell or the Control Center): `supervisor pause` (dispatch stops at the next iteration, running jobs
  finish), `resume`, `stop`, `reload`, `drain [--shutdown]`. These queue command files in `state/commands/`; the loop
  applies them within one iteration (≤ 5 s; immediately via the wake file). With no loop running the CLI applies
  them directly.
- Any other owner action is also a command: `supervisor cmd task.note --payload '{"task_id":"T0-01","note":"..."}'`,
  `task.set_priority`, `task.set_status`, `task.approve`, `gate.ack`, `gate.set`, `capacity.reset`,
  `lane.reset` (`{"lane": "<adapter>"}`), `owner_task.set_status`, `agent.terminate`, `agent.note`, `conversation.*`, `system.shutdown[_cancel]`.
- Jobs: every attempt has `state/runtime/jobs/<task>.a<n>.json` (PREPARED → SUBMITTED → RUNNING → DONE/FAILED). On
  start the loop reconciles these before dispatching: PREPARED is launched under the same id, live children are
  adopted, finished ones ingested, dead ones marked lost. Never delete these files while a job may be running.
- A task's `job` block selects the adapter (`docs/TASK_CONTRACT.md`); without one, `cost_category` →
  `routing.yaml cost_lanes`, except that a model category (claude/codex/mixed/paid) runs on the owner role's own
  lane (`routing.yaml roles.<owner_role>.adapter`) when the role configures one. A role
  with `active: false` is never dispatched: its tasks are parked with a `schedule_skip` log entry until the owner
  activates the role.
- Limits (`config/routing.yaml: limits`): `max_agent_jobs` (model-lane jobs of any kind), `max_resource_jobs`
  (extension lanes with `gpu = True`). Tasks whose `owned_paths` overlap a running job's are never dispatched together.
- Capacity: `state/runtime/usage_claude.json` / `usage_codex.json` hold the measured windows; a lane below its
  reserve defers (no attempt spent) until the reset or an owner `capacity.reset`.
- `--unattended`: a `claude_cli` pick becomes `claude_api` while API credits remain, but only when
  `routing.yaml capacity.unattended_prefers_api_credits` is true (needs a key; see `claude_api.md`). With the
  preference off the pick stays on `claude_cli` and waits on its capacity gate. Interactive runs never take this
  branch.
- Observability: `supervisor status` (STATE.md's summary with the live runtime record; writes nothing), `supervisor
  supervisor` (runtime JSON), `state/log.jsonl` (events, redacted), `runs/<task>/<attempt>/` (prompt, stdout,
  result.json, verification.json, HANDOFF.md).
- Loop error budget: five exceptions in ten minutes → status BLOCKED, exit 1; read the `loop_error` events.
