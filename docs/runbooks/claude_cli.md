# Runbook — `claude_cli` adapter

- Spawns the official `claude` binary (PATH, `~/.local/bin/claude.exe`, or `claude.executable` in
  `config/routing.yaml`) with `-p --model <m> [--effort <e>] --tools <list> --strict-mcp-config --permission-mode
  dontAsk --output-format stream-json --verbose --session-id <uuid> [--allowedTools … --disallowedTools …]
  --add-dir <root>`; the packet goes in on stdin.
- Role → model/effort/tools from `config/routing.yaml roles.<role>`; a task's `job` block may override `model`,
  `effort`, `tools`, `prompt`/`prompt_file`, `timeout_s`, `result_kind`, `write_scope`.
- Tools: `writer` (Read/Write/Edit/Glob/Grep/Bash), `reader`, `reviewer` (reads everything, writes only the run
  dir), `web` (adds WebSearch/WebFetch), `none`, or an explicit list. `dontAsk` auto-denies anything that would
  prompt, so every allowed tool is passed explicitly; Bash only through the allow patterns (`BASH_ALLOW` plus
  `routing.yaml claude.bash_allow`), destructive git always denied. Denials are recorded in the delivery notes and
  `result.json`; they are never fatal by themselves.
- Environment: `*_API_KEY`/`*_TOKEN`/… and vendor-nesting markers removed (subscription login only),
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `SUPERVISOR_ATTEMPT=<attempt>` for crash recovery.
- Before launch the adapter checks capacity (`state/runtime/usage_claude.json`, reserves from
  `routing.yaml capacity.claude`); a gated engine raises `CapacityDeferred`: the record stays PREPARED, no attempt is
  consumed.
- Results: the last `type=result` event; `result_meta.json` (session id, usage, permission denials), `result.txt`,
  `result.json` (the fenced `delivery`/`review` object). A missing JSON object triggers exactly one tool-less
  `--resume <session>` follow-up (`followup_stdout.jsonl`, 120 s cap).
- Failure classes: `login_required` (owner runs `claude /login`), usage limit → DEFERRED with a synthetic or measured
  window and a continuation pointed at the prior `HANDOFF.md` (max 3), timeout → child tree killed and the work
  continued the same way (no revision spent), `max turns`.
- Time limits are activity-based (1.7.0, `supervisor/timeouts.py`): a session is stopped after `timeouts.idle_s`
  (default 20 min) with no new output and under `timeouts.idle_cpu_s` CPU seconds of work in its processes, or at
  `timeouts.hard_cap_s` (default 6 h; Extend in the Control Center adds an hour). Ten minutes before the cap the
  session gets a `HANDOFF_REQUEST.json` (reason `time limit`). Overrides: `timeouts.by_cost.<cost_category>`,
  `roles.<role>.timeouts`, and per task `job.timeout_s` (hard cap) / `job.idle_timeout_s` (idle limit, 0 = none).
- Diagnostics are matched only against stderr, `system`/`error` events and an `is_error` result — never against the
  model's ordinary answer.
- `--effort` is gated by `routing.yaml claude.supports_effort`.
