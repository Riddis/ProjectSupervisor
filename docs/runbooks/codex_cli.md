# Runbook — `codex_cli` adapter

Second vendor for reviews and second opinions (and deliveries, if a role is routed to it).

```
codex -a never exec --ephemeral -s <read-only|workspace-write> -m <model> -c model_reasoning_effort="<effort>" \
      --json --output-last-message runs/<task>/<attempt>/result.txt [-i <image> …] -   < prompt.md
```

- Prompt on stdin; `--ephemeral` keeps no session on disk; `-a never` means the sandbox decides (review lanes run
  `read-only`, so an interrupted review returns its handoff as prose before a BLOCKED verdict).
- `--json` streams `thread.started` / `item.*` / `turn.completed` / `turn.failed` events to `stdout.jsonl`;
  `result.txt` is the last agent message; the fenced JSON result object is extracted from it.
- Env: `OPENAI_API_KEY` removed (ChatGPT-login quota, never pay-per-token); `HOME`/`USERPROFILE` ensured;
  `SUPERVISOR_ATTEMPT` tags the child.
- Model: `routing.yaml roles.<role>.model`, else `codex.model`. Accounts reject models they do not have — verify with
  the app-server `model/list` during setup and record the accepted name.
- Capacity: `poll_capacity()` reads usage windows through the app-server RPC (no model turn), every iteration with a
  10-minute staleness guard, into `state/runtime/usage_codex.json`.

| Symptom | Result |
|---|---|
| stderr mentions `codex login` / `401` | FAILED `login_required` → owner runs `codex login` |
| usage-limit wording | DEFERRED; continuation queued |
| no `turn.completed` or a `turn.failed` | FAILED (retry per budget) |
| answer without a fenced JSON result | FAILED `no structured result` |

Executable discovery: `routing.yaml codex.executable` → PATH → `%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe`
→ newest `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`.
