# Records: tasks, gates, decisions, issues, inbox, owner tasks

Everything the supervisor decides from lives under `state/` as YAML (one file per record) and is rendered to the
markdown projections at the project root (`TASKS.md`, `STATE.md`, `ISSUES.md`, `DECISIONS.md`, `HUMAN_INBOX.md`,
`OWNER_TASKS.md`). Projections are never parsed back and must not be edited.

## Task (`state/tasks/<ID>.yaml`)

The **contract** fields (what the setup skill and the Senior PM write; `supervisor task import` refreshes them from
`state/tasks_seed.yaml` for the ids you name):

| Field | Meaning |
|---|---|
| `id` | `T<phase>-<nn>` by convention (`T0-01`); anything unique works |
| `phase` | integer; the task is eligible only while `state/gates.yaml` says its phase is open |
| `title`, `objective` | one line; one paragraph |
| `owner_role` | the role id (routing entry) that does the work |
| `depends_on` | task ids that must be COMPLETE first (same or earlier phase) |
| `actions` | the steps, as the worker should take them |
| `artifacts` | repo-relative file paths or globs (`*?[`, `**`) the delivery must produce — **the floor**: a worker may add, never omit. Every entry must be able to match a file. `task import` and `task create` **refuse** an entry no file can ever satisfy, because the floor would fail every delivery forever: absolute or escaping paths, directories (`src/` — write `src/**/*.py`) and characters no file name can hold (`<id>`). They **warn** about, but accept, a legal name that reads like prose or a placeholder (`contact sheets`, `S1..S6`, `EXP-1xx`, `round_N`, `{x}`) — `Final Report.md` and `out/xx.json` are real file names; an entry that already matches a file raises no warning |
| `tests` | prose: what must be true (for the worker and reviewers) |
| `checks` | executable: `[{command, timeout_s?}]` the supervisor re-runs on every delivery, whatever the worker reports; commands must match `verification.allowed_test_commands` |
| `owned_paths` | roots the worker may write; `["."]` = exclusive (whole tree, nothing else writes meanwhile). Required for every model-lane task |
| `reviewer` | role ids whose ACCEPT is required (each reviews frozen inputs in its own lane) |
| `review_note` | guidance for the reviewers |
| `acceptance` | one sentence a reviewer can check without asking what you meant |
| `escalation` | free text: who decides when this goes wrong |
| `human_approval` | `none` / `required` (owner must ACCEPT the delivery in the inbox) / `gate` |
| `approval_note` | what the owner should look at when approving |
| `cost_category` | `deterministic` / `local` / `claude` / `codex` / `human` / `mixed` / `paid` → default adapter via `routing.yaml cost_lanes` |
| `priority` | higher first among eligible tasks |
| `job` | optional: `{adapter, ...}` overrides the lane — `{adapter: python, fn, params}`, `{adapter: process, argv, timeout_s}`, `{adapter: claude_cli, model?, effort?, tools?, prompt?/prompt_file?, write_scope?, timeout_s?, idle_timeout_s?}` (`timeout_s` = hard cap, `idle_timeout_s` = stop after this long quiet, 0 = never; defaults from routing.yaml `timeouts`), `{adapter: human, subject, summary, allowed_responses?, free_text?}`, or an extension lane |

The **runtime** fields the supervisor maintains: `status`, `attempts` (one per dispatch), `history` (status
changes), `delivery` (`digest`, `verification`, `reviews[]`, `approvals[]`, all bound to the digest of the current
artifacts), `result`, `paths`, `notes`, `guidance` (from a diagnosis requeue or the owner's REQUEUE text — the next packet carries it), `forced`, `rev`, `created`, `updated`.

### Lifecycle

```
NOT_STARTED/READY ─dispatch─▶ IN_PROGRESS ─delivery+verify ok─▶ WAITING_FOR_REVIEW ─all ACCEPT─▶ WAITING_FOR_HUMAN ─owner ACCEPT─▶ COMPLETE
                                   │                                   │ REVISE/REJECT                 │ MODIFY/REJECT
                                   │ verify fails                      ▼                               ▼
                                   ▼                            IN_PROGRESS (revision)          IN_PROGRESS (revision)
                                FAILED ─retry budget─▶ BLOCKED (diagnosis → requeue / defer / architect / owner)
```

Only `tasks.advance()` reaches COMPLETE: verification ok ∧ every required review ACCEPT on the current digest ∧
(no approval needed ∨ owner ACCEPT for this digest). A new delivery digest supersedes older verification, reviews and
approvals. An owner may force a status with a note (`supervisor task set-status --force --actor owner`); it is
persisted and rendered as *(forced)*.

Retry budget: two automatic revisions per failure source (verification, review), the second on the role's
`senior_model`; then one bounded diagnosis by the manager role (`routing.yaml escalation`) which can requeue with
guidance, defer, hand to the architect, or open an owner inbox item. Capacity limits never spend an attempt: the job
is DEFERRED and continued (max 3 continuations) from its `HANDOFF.md`.

Nothing stops silently. A worker that ends **BLOCKED** goes straight to that diagnosis (no blind retry of the same
brief). An agent run that ends FAILED/PARTIAL, crashes, times out or is lost gets the normal revision budget. A
deterministic lane (process/python/extension adapters) that FAILED is retried by the stall sweep after the grace
period, which doubles as its back-off. The failure detail always carries the worker's own notes and the head of its
`HANDOFF.md`. An owner REJECT/MORE_LIKE on a `human` task stands and is never retried.

The stall sweep (every loop iteration): a FAILED or BLOCKED task with nothing in flight and no open owner item, past
`routing.yaml escalation.stall_grace_s` (default 600), is escalated once for that stop (FAILED → retry within budget,
BLOCKED → diagnosis); if it is still unrouted a grace period later, one owner decision item is opened. Deliberate holds
are left alone: a stop the owner made (Hold / set-status), an open or snoozed owner item, an owner answer after
the stop, a failed `human` attempt (the owner's own REJECT/MORE_LIKE), or a diagnosis `defer`. When the whole pipeline is idle (nothing
running, nothing eligible, nothing waiting on the owner, work remaining) for twice the grace period, one
**Pipeline idle — nothing can run** item lists the stuck tasks and what each waits for (once per stuck set).

## Gates (`state/gates.yaml`)

```yaml
phases:
  0: {requires: []}
  1: {requires: [G0]}
  2: {requires: [G1], note: "docs may start on G0"}
gates:
  G0: {status: OPEN, phase: 0, owner_ack_required: true, evidence_required: true, evidence: [], passed_at: null, owner_ack: null}
```

A phase is open when every gate in `requires` is open; an unlisted phase is closed. A gate is open when its status is
PASSED **and** its declared requirements are met: `evidence_required: true` needs a non-empty `evidence` list,
`owner_ack_required: true` needs an `owner_ack` recorded by an owner actor (`owner`, `owner-ui`). Both keys default
to false when absent. `supervisor gate set G0 PASSED --evidence path1,path2 --actor owner` and `supervisor gate ack
G0` (or the Control Center's *Acknowledge gate* button) record it; the set is refused with a clear error when the
evidence is missing or when a non-owner actor (an agent, the default `cli`) tries to pass an owner gate — an owner's
own PASSED doubles as the acknowledgement. Gates are not necessarily linear — a phase may require a gate from a
non-adjacent phase.

## Decisions (`state/decisions/D-nnn-<slug>.md`)

Markdown with YAML front matter (`id`, `date`, `title`, `status: proposed|accepted|superseded`, `decided_by`,
`supersedes?`) and sections *Context*, *Decision*, *Consequences*. Written by agents inside their owned paths, by the
owner, or from a Control Center chat reply ("convert to decision"). Rendered index: `DECISIONS.md`.

## Issues (`state/issues/I-nnn.yaml`)

`id, created, status: open|resolved|wontfix, severity: blocking|high|medium|low, title, owner_role,
resolution_task?, systemic?, notes[]`. Escalation opens one when retries are exhausted; agents and the owner may add
them.

## Inbox (`state/inbox/H-nnn.yaml`)

`kind: question|decision|approval|gate|round|spend|policy`, `for` (role, normally `owner`), `from`, `subject`,
`summary` (≤ 600 chars, what the owner must act on first), `detail` (optional, ≤ 8000 chars: the full text when the
summary had to be cut — `inbox.fit()` makes the split; the Control Center shows it behind "Show full message"),
`allowed_responses` (default `ACCEPT, REJECT, MODIFY, REDO`), `free_text`, `evidence[]` (repo-relative paths: images
render as thumbnails, markdown inline), `items[]` (sub-options), `task_id`, `attempt_id`, `approval_digest`. At most 3
open items per role: the owner's attention is the scarce resource. A human gate never times out.

A `decision` item (`REQUEUE / CANCEL / DEFER`, plus `resume: review|delivery|task`) is opened for a task BLOCKED with
"owner decision needed" and when a diagnosis hands a FAILED task to the owner. REQUEUE resets the budget that stopped
the task — and the task's escalation budget (spent revisions, failure signatures), so the next failure gets the normal
revisions again — and puts it back where it stopped, CANCEL cancels it, DEFER leaves it. Text typed with the button
("REQUEUE use the cached outputs") is written to the task's `guidance`: the next delivery brief carries it (whatever
the prompt source — the default brief, a job's own `prompt`/`prompt_file`), or, for a review resume, the next review
brief of that digest. It is also kept as a note. The outcome is written to the item as `applied`; a re-applied item
never adds the guidance or the reset twice.

## Owner tasks (`state/owner_tasks.yaml`)

`id: OT-nnn, title, status: open|done|snoozed, priority, blocks: [task ids], guidance`. Things only the owner can
do (log in, install, pay, decide). Marking one `done` re-queues the FAILED/BLOCKED tasks it blocked.

## Seed file (`state/tasks_seed.yaml`)

```yaml
tasks:
  - id: T0-01
    phase: 0
    title: Project skeleton and test runner
    objective: ...
    owner_role: engineer
    depends_on: []
    actions: [...]
    artifacts: ["src/**/*.py", tests/test_smoke.py]
    tests: ["python -m pytest passes"]
    checks: ["python -m pytest -q"]
    owned_paths: ["."]
    reviewer: [code_review]
    acceptance: ...
    escalation: chief_architect
    human_approval: none
    cost_category: claude
```

`supervisor task import` is idempotent: it creates what does not exist and leaves existing tasks alone unless
`--update-contracts T0-01,T0-02` names them. It goes through the command queue (`task.import`), so a corrected seed
also reaches a running supervisor — an agent that fixes a contract in the seed can have it applied without anyone
stopping the loop. `--dry-run` runs the same validation and prints what would be created and refreshed, writing
nothing (safe while the loop runs; use it before an import into a live project).
