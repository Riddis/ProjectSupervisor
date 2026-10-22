# Architecture

One Python package, one process per role of the system (loop, UI, CLI), one directory of YAML as the truth.

## Invariants

1. **Single writer.** Whoever holds `state/.lock` (normally the running loop) is the only process that mutates
   `state/`. Everyone else — CLI, Control Center — writes a command file (`state/commands/<id>.json`) that the lock
   holder applies within one iteration (`commands.py`). With no loop running, the CLI/UI take the lock and apply the
   command themselves. Handlers are idempotent by command id.
2. **One transition function.** `tasks.advance()` is the only path to COMPLETE; `tasks._evaluate()` is the rule.
   Every status change happens inside the store lock, deciding from the freshly read document.
3. **One launch gate.** Every job — delivery, review, revision, continuation, verify job, chat turn — reaches an
   adapter's `submit()` only through `Supervisor.launch()`, which asks `gate.check()`: owner decision, measured
   capacity, concurrency limits, file ownership. A refused job waits as PREPARED; nothing is dropped or spent.
4. **Verification trusts nothing it did not observe.** Artifacts are hashed; the working tree is compared before and
   after; declared tests and contract checks are re-run from an allow-list, never through a shell.
5. **Reviews are independent.** Frozen inputs (sha256 manifest), a different vendor lane than the producer, read-only
   tools, never the producer's self-assessment. A changed input invalidates the review.
6. **Credentials never reach a prompt or a log.** `policy.assert_packet_clean()` refuses; `tasks.log()` redacts;
   `process.scrub_env()` strips the child environment.
7. **Crash-safe at every point.** Job records are written before launch; adapters can `recover()` a handle after a
   crash; the loop reconciles open records before dispatching anything.
8. **Projections are views.** `render.py` rewrites the markdown files every iteration from the store; they are
   excluded from verification manifests and never parsed.

## Modules (supervisor/)

| Module | Role |
|---|---|
| `paths.py`, `project.py` | project root discovery (`SUPERVISOR_ROOT` → nearest `project.yaml`), `project.yaml` access |
| `extensions.py` | loads a project's `extensions/`; the registry of hooks (adapters, checks, UI, screens, projections, CLI, python jobs) |
| `store.py`, `locks.py` | YAML documents with revisions and compare-and-swap; the OS-level file lock |
| `models/` | pydantic schemas: `Task`, `Delivery`, records (`Decision`, `Issue`, `InboxItem`, `Gate`, `OwnerTask`) |
| `tasks.py` | the task store, `advance()`, delivery/verification/review/approval records, the single log path |
| `gates.py` | phase/gate eligibility |
| `scheduler.py`, `gate.py` | what to dispatch now; the one launch gate; ownership overlap |
| `loop.py` | `Supervisor`: apply commands → honour decision → reconcile → settle → dispatch → render → sleep |
| `jobs.py` | durable job records (`state/runtime/jobs/`), phases PREPARED → SUBMITTED → RUNNING → DONE/FAILED |
| `adapters/` | `base` (interface), `python`, `process`, `claude_cli`, `claude_api`, `codex_cli`, `human` |
| `packets.py` | delivery/review/revision briefs (role card + contract + ownership + result contract) |
| `verify.py`, `verify_job.py` | deterministic verification; the tracked process job that runs the commands |
| `review.py` | review dispatch on frozen inputs, invalidation, same-vendor fallback |
| `escalation.py`, `diagnosis.py` | retry budgets, failure signatures, the manager → architect → owner exception route |
| `inbox.py`, `owner_tasks.py`, `decisions.py`, `conversations.py` | the human-facing stores; Control Center chat threads |
| `capacity.py`, `_vendor/usage_policy.py` | measured subscription windows and reserves per engine |
| `checkpoint.py` | commits exactly the accepted contents of a COMPLETE task |
| `policy.py` | credential isolation (packets, logs, child env) + extension packet screens |
| `commands.py` | command files and their handlers |
| `runtime.py`, `shutdown.py` | heartbeat/decision document; drain-then-power-off |
| `render.py` | the markdown projections |
| `settings.py` | the few routing.yaml knobs the UI may edit (round-trip YAML, comments preserved) |
| `doctor.py`, `migrations.py`, `template.py` | health checks; version migrations; `supervisor init` |
| `ui/` | FastAPI + Jinja + HTMX Control Center: overview, roadmap, tasks, agents, inbox, chat, log, owner tasks, settings |
| `cli.py` | `supervisor ...` |

## A job's life

```
scheduler.plan() ──▶ Supervisor.dispatch(task, adapter)
                       adapter.prepare()  → spec (packet written to runs/<task>/<attempt>/)
                       jobs.prepare()     → durable record (PREPARED)            ← crash here: reconcile relaunches
                       Supervisor.launch() → gate.check() → adapter.submit() → handle (SUBMITTED)
                     … each iteration: adapter.collect(spec, handle) → None (RUNNING) | Result
                       Supervisor.ingest(rec, result)
                         delivery: digest artifacts → tasks.record_delivery → verify (inline file checks,
                                   commands as a tracked verify job) → tasks.record_verification → tasks.advance
                         review:   review.ingest → tasks.record_review → advance; REVISE → revision attempt
                         DEFERRED/handoff: requeue a continuation (bounded)
                       jobs DONE
```

## Data on disk (project)

```
state/tasks/*.yaml  state/gates.yaml  state/decisions/*.md  state/issues/*.yaml  state/inbox/*.yaml
state/owner_tasks.yaml  state/conversations/*.yaml  state/log.jsonl
state/commands/*.json (+applied/)  state/runtime/{supervisor.json, usage_*.json, jobs/*.json, wake}
runs/<task>/<attempt>/{prompt.md, stdout.jsonl, stderr.txt, result.json, HANDOFF.md, verification.json, ...}
```

`runs/`, `state/runtime/`, `secrets/`, `uploads/` are git-ignored; the rest is committed by checkpoints.
