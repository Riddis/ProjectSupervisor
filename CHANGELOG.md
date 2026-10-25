# Changelog

All notable changes to the framework. Versions follow semver: patch = fix, minor = feature or new extension hook,
major = a change that needs `supervisor migrate` (and says so under **Migration**).

## 1.8.4 — 2026-10-25

Fixes from the Studio migration's phase-4 rehearsal (its record PHASE4.md, findings F2–F5), plus the licence.
No state or config shape change; `supervisor migrate` only records the new version.

- **The loop polls Codex capacity only when something can use the Codex lane** (F2). `codex_cli` is always
  registered, so every iteration asked the installed CLI for its usage windows (an app-server RPC) whatever the
  project's config said — a rehearsal copy with only fake lanes still reached it. `scheduler.lane_in_use()` now
  decides: an active role whose `adapter` or `fallback.adapter` is the lane, a `cost_lanes` override naming it, a
  live task that resolves to it, or an open job record on it. The adapters' own capacity checks before a launch are
  unchanged.
- **`supervisor doctor` names the project root** in the `project` check (F3), so a rehearsal can show which tree it
  examined.
- **`supervisor task import --dry-run`** (F4): the import's full validation (seed contracts, graph, ownership,
  artifacts, and each `--update-contracts` refresh), then what would be created and updated; writes nothing and
  logs nothing, so it is safe while a supervisor runs. `commands.seed_path()` is the one seed-path check for both.
- **Migrations change and report only what they change, and keep line endings** (F5). The 1.0.0 `local_gpu → local`
  migration reported (and rewrote) any file that mentioned `local_gpu` anywhere, so a second `migrate` reported the
  same files again; it now rewrites only a task's own `cost_category` field, found through the YAML node tree, so
  an example of the old value inside a note or a quoted string stays as written. Each field is rewritten once, in file order
  (a YAML alias shares its anchor's span), and only the value token: an anchor or tag before it
  (`&local_gpu !!str local_gpu`) keeps its name. Every migration write, and `project.write()`,
  goes through `store.rewrite_text()`, which writes only when the content changes and keeps the file's LF or CRLF
  style (`Path.write_text` turned LF files into CRLF on Windows).
- Tests: `test_capacity_poll.py`; the owner decision-item tests the Studio carried (REQUEUE salvage, DEFER, CANCEL on
  a review-blocked task) moved here as `test_owner_decision_items.py`.
- **Licence.** The framework is licensed under Apache License 2.0 with the Commons Clause (`LICENSE`): free to use,
  modify and share; not to be sold, nor offered as a product or service whose value derives substantially from it.
  `pyproject.toml` declares it; the README explains it. No code change.

## 1.8.3 — 2026-10-24

Review round on 1.8.2: `tools/test_windows.ps1` did not parse — `"$out:"` inside a string is read by PowerShell as a
scope qualifier. Now `${out}:`; the other `$out` interpolations are braced too. No other change.

## 1.8.2 — 2026-10-24

Review round on 1.8.1 (verdict REVISE, one remaining finding on the Windows validation script).

- **`tools/test_windows.ps1` fails loudly when its results file cannot be written.** `Set-Content`/`Out-File`
  errors are non-terminating by default, so a busy `runs/pytest_windows.txt` (another run holding it) let the
  script continue, show the previous run's output and exit 0 — a false pass. Every write to the results file is
  now a terminating error: a busy or unwritable log ends the run with exit 2 before pytest starts (or voids it if
  the file becomes unwritable mid-run); a log that ends without a pytest summary exits 3; otherwise the exit code
  is pytest's own. `tools/test_windows_selfcheck.ps1` holds the results file open, runs the script and expects
  exit 2 with the file untouched.

## 1.8.1 — 2026-10-24

Review round on 1.8.0 (verdict REVISE): two reproduced findings.

- **A failed required extension no longer unlocks the inbox items it owns.** When a required extension failed to
  import, its `inbox_kind()` registrations never ran, and the generic answer path read the missing metadata as
  permission: a `labelling` item could be closed by `inbox.answer()`, the `inbox.answer` command or the inbox form
  without the extension's per-option validation, and the inbox page rendered its blind rows. Now a kind that is
  neither built in nor claimed, while any required extension is blocking, has an owner that cannot be established
  (`inbox.ownership_unknown`): the generic path refuses (the error names the failed extension), the card is rendered
  blind and without a form and says why. Built-in kinds (`gate`, `decision`, `approval`, …) stay answerable, so the
  owner can still act; with every extension loaded, an unclaimed kind is ordinary data as before.
- **`doctor`'s framework check counts untracked files.** `updates.runtime_checkout()` ran `git status
  --untracked-files=no`, so a tagged checkout with an extra, untracked Python module was reported clean — and an
  editable install imports that module like any other. The status now includes non-ignored untracked files
  (`untracked` lists them, the check names them), and a `git status` that fails is a failed verification
  (`status_error`, `dirty: None`, FAIL "could not be verified"), never "clean".
- `tools/test_windows.ps1` sets `PYTHONPATH` to the checkout for the run (process-local, so a venv that does not
  have the framework installed can still run the updater tests, whose subprocesses import `supervisor`) and exits
  with pytest's exit code instead of 0.
- Tests: the owned item after its extension stops loading (API, command, UI render and UI answer), the unclaimed
  kind with everything loaded, and a real tagged temporary repository for the cleanliness check (untracked file,
  ignored file, failing `git status`).

## 1.8.0 — 2026-10-24

Seven extension hooks and three fixes, for a project whose work runs on one heavy local resource, reviews images in
its own Control Center pages and screens content by what a job *is*. Nothing in them is specific to any project.

- **A model-lane job can hold the resource lock** — `limits.resource_cost_categories` (routing.yaml, default `[]`).
  The single resource slot (`max_resource_jobs`) used to be held only by adapters declared `gpu = True`; an agent
  session doing the resource's own engineering work (installing into it, starting it, benchmarking it) ran on
  `claude_cli` and could overlap with a lane job on the same card. Listing a cost category (`[local]`) makes the
  *delivery* of such a task hold the slot on any lane (`gate.holds_resource`), the scheduler counts it in a plan,
  and the job record's `gpu` flag follows the same rule so a restarted loop re-adopts the lock.
- **`registry.command(kind, fn)`** — an extension's owner command kind, applied by the loop, the Control Center's
  direct path and the CLI like a built-in; a kind that already exists is an error, never a silent overwrite.
- **`registry.inbox_kind(kind, label, page=, answerable=, show_evidence=, score_keys=)`** — an inbox item kind an
  extension owns. The inbox page groups it under its label, links the extension's page, hides the per-option rows
  and their scores when `show_evidence=False` (blind labelling: the card shows a count), and drops the generic form
  when `answerable=False`; `inbox.answer()`, the `inbox.answer` command and the UI route refuse to close such an
  item (the error names the page), while the extension finalises it with `inbox.answer(..., internal=True)`.
  Before, unknown kinds fell under "Agent questions" with their scores visible and a working answer form.
- **Job-aware packet screens** — `assert_packet_clean(text, layout, where, *, job=None, task=None, lane=None)`.
  The hosted lanes (`claude_cli`, `codex_cli`, `claude_api`) pass the task's `job` block, the Task and the lane name
  from `prepare()`; a screen receives exactly the keyword arguments it declares (`**kwargs` gets all), so a screen
  can refuse on a job's channel or measured content classes, and `fn(layout, text, where)` screens keep working.
  Decided from the signature, never by catching `TypeError`.
- **`registry.template_dir(path)`** — extension Jinja templates behind the framework's own in a ChoiceLoader, so an
  extension page can `{% extends "base.html" %}` and a framework template can never be shadowed.
- **`registry.static_dir(path, prefix)`** — extension assets at `/static/<prefix>/...`, mounted ahead of the
  framework's `/static`.
- **`supervisor.gpu`** — the shared nvidia-smi probe (`free_mb`, `used_mb`, `total_mb`, `available`): lanes,
  doctor checks, overview panels and tools outside the framework import it from one place; it never raises.
- Fix: a `required: false` extension that fails to load is logged once per process (`extension_error`) and said on
  stderr; before, it showed only in `supervisor doctor`, indistinguishable from an extension left out on purpose.
- Fix: `supervisor --help` no longer silently drops a broken extension's command group — the CLI says on stderr
  which extension did not load and that its commands are missing.
- Fix: `supervisor doctor` has a `framework` check: the installed version plus, for an editable install of a git
  checkout, its commit and tag, FAILING when the checkout is dirty or not exactly at a tag — a project must run from
  code a review accepted and a CHANGELOG names, never a working tree being edited. An installed package (no
  checkout) reports its version only. `updates.runtime_checkout()` is the underlying probe.
- Docs: `docs/FRAMEWORK_VS_EXTENSION.md` lists the new hooks; the routing template documents
  `limits.resource_cost_categories`.
- Tests: `tests/test_hooks_180.py` (hooks 1–7 and the three fixes, 17 tests).

## 1.7.3 — 2026-10-24

Migration plan phase 1b (periphery modules of the drift ledger): one port.

- **Control Center timestamps in local time.** Store values are ISO-8601 UTC; every timestamp the Control Center
  shows (activity table, log page, inbox answers, task history, notes, approvals, attempts, the heartbeat tooltip
  and the live log stream) now renders as the machine's local `YYYY-MM-DD HH:MM` through the `localtime` Jinja
  filter (`supervisor/ui/timefmt.local_stamp`; a value that does not parse is shown unchanged). Before, pages
  showed raw UTC ISO strings or a clipped slice of one.

## 1.7.2 — 2026-10-24

Fixes ported from the project the framework was extracted from (migration plan phase 1a: the drift ledger's
"upstream" rows). Generic hardening the project had made after the extraction and the framework lacked.

- **`claude_api` results were read as empty.** `collect()` returned the result-file envelope as `Result.raw`, while
  `review.ingest` reads the verdict, `loop.ingest` the declared `tests` and a diagnosis its decision off `raw`
  — so every API-lane review came back "no verdict" and every API-lane delivery lost its test evidence. `raw` is
  now the structured object itself (the envelope only when there is none, so the error still travels).
- **One paid POST per attempt, ever.** The response body is persisted (`response.json`) before the ledger append
  and the result write; a submit re-entered after a kill in that window finishes from the saved body instead
  of calling the API again, and the ledger entry is keyed by the response id so it is never billed twice
  (`claude_api.entries`, `recorded`, `record_usage(..., response_id=)`).
- **The unattended switch only moves work the tool-less lane can do.** `claude_api.unattended_eligible`: the pick
  must be the cost-category default (an explicit `job.adapter` is never overridden), the contract must declare no
  artifacts, tests or executable checks (there is no Read/Write/Bash through the Messages API), and the task or
  its role must be opted in (`job.api_credits: true` or routing.yaml `claude_api.eligible_roles`). Before, any
  `claude_cli` pick was switched once credits were available, engineering tasks included.
- **Fail-closed credential scanning** (`policy`). *There is no credential store* (absent `secrets/`, the normal
  state of a fresh clone) is now distinct from *the store could not be read*: an unreadable directory or file,
  a non-regular entry, a file too large to scan, one that is not UTF-8, a half-written `.json` token file or a
  relocated `secrets.dir` that does not exist is a scan failure (`secret_scan_problems()`), never cached, and
  while one stands `assert_packet_clean()` refuses every dispatch and the log redactor withholds every field
  value long enough to be a credential (`policy_unscreened` names why). Both halves of the packet judgement
  come from one scan. A config file that cannot be stat-ed is reported and never served from cache.
- **Numeric credentials and credential keys.** A digit-only password stored as a JSON number is collected (in
  both the file's literal and the canonical `str()` form) and screened wherever it appears — a packet, a log
  value, a log key, a numeric field name — while unrelated numbers keep their value and their type. Mapping
  keys are redacted like values; two keys that redact alike both keep their record (`[REDACTED]#2`).
- **The log's `actor` and `event` are screened.** `tasks.log()` passes both through `screen_log_identity()`:
  screened for literals and patterns, never blanked (the trail stays readable while the store is unreadable).
  `actor` is data — `commands.apply_all()` reads it from a queued command file.
- **Review verdicts are not lost over a category spelling.** `_vendor/contract` maps the near-misses reviewers
  write ("acceptance", "requirements", "bug", …) onto the canonical categories and names the offending finding
  in its error; `packets.review_brief` tells the reviewer the vocabulary; `claude_cli.follow_up(error)` quotes
  the contract's own error back to the model so it fixes only that; and `review.salvage()` ingests a finished
  review attempt whose answer parses now, before paying for a fresh review (`review_salvaged` event, `salvaged`
  on the job record).
- `tasks.record_delivery` filters the delivery's paths through `verify.excluded_files`, so an extension
  projection is never frozen as a review input (the loop already dropped it from the artifact list).
- Tests: `test_policy_hardening.py` (40), `test_review_salvage.py`, `test_delivery_paths.py`, the `claude_api`
  resume/ledger/eligibility cases, the follow-up prompt and the review-brief vocabulary.

## 1.7.1 — 2026-10-24

Review round on 1.7.0 (verdict REVISE): three time-limit edges around upgraded and interrupted jobs.

- **The first activity sample no longer kills a busy job.** `timeouts.last_active` dated a job's first record back
  to its launch, so a session adopted after the upgrade (no `activity.json` yet) that had already run longer than
  the idle limit was stopped as idle on its very first look, while its output was still growing — and so was any
  job whose first collection came late. The first sample now opens the observation window at that moment; idleness
  is judged only from two samples. A quiet job is still stopped one full idle window later.
- **One authoritative deadline.** `timeouts.effective_limits` returns what the adapter enforces (for a job prepared
  before 1.7.0, `claude_cli.model_limits` moves it onto the current policy). `agent.extend`, `status.json`, the
  time-limit handoff withdrawal, the *Now running* row and the agent page all use it; before, Extend added its hour
  to the obsolete one-hour deadline still in the handle, so the effective cap moved but the recorded one stayed in
  the past and the session was still told to stop and hand off. Extend now writes the effective deadline + 1 h to
  the handle, so the recorded and effective caps agree from then on.
- **Replaying Extend repairs an interrupted update.** The job record is written first, then `status.json` and the
  handoff request. A crash between those writes made the replay skip the rest (*already applied*): the hour was
  in the job, not in the `status.json` that `recover()` re-adopts from, and the stop request survived. A replay
  now re-applies both from the job record without adding the hour again, reports `repaired`, and logs
  `job_extend_repaired`.
- Tests: a busy legacy session through the real `ProcessAdapter` subprocess path with the real `model_limits`
  survives its first collect, a quiet one is still stopped after a full window; Extend on a session 5h55m into a
  pre-1.7.0 run; fault injection between the job write and the status write, then replay.

## 1.7.0 — 2026-10-24

Agent sessions are no longer killed at one hour while they are still working.

- **Activity-based time limits** (`supervisor/timeouts.py`). A `claude_cli`/`codex_cli` job used to get one fixed
  deadline, 3600 s after launch, and long sessions were killed mid-work. Now a job is stopped only when it is
  **idle** — its `stdout.jsonl`/`stderr.txt` have not grown and its process tree has used less than `idle_cpu_s`
  CPU seconds for `idle_s` (default 20 min; Claude Code's 30 s tool heartbeats and any child process doing work count
  as activity) — or when it reaches its **hard cap** (default 6 h). Measured per run dir in `activity.json`, so a
  restarted loop keeps measuring where the last one stopped.
- **Configurable** in `routing.yaml` `timeouts` (`idle_s`, `idle_cpu_s`, `hard_cap_s`, `by_cost.<cost_category>`),
  per role (`roles.<role>.timeouts`) and per task (`job.timeout_s` = hard cap, `job.idle_timeout_s` = idle limit,
  0 = none). Plain `process` jobs keep `job.timeout_s` (default 3600) and get an idle limit only when they ask.
- **Extend.** `agent.extend` (Control Center **+1h** on a *Now running* row, **Extend +1h** on the agent page)
  raises one running job's hard cap by an hour; idempotent per command id, written to the job record and
  `status.json` (so a re-adopted child keeps it), and it withdraws a pending time-limit handoff request. The row
  shows elapsed time against the cap and, after two quiet minutes, how long the job has been quiet.
- **A timeout is a continuation, not a failure.** A model-lane job stopped on time is requeued like a capacity
  limit (`Supervisor.requeue`, reason *time limit (idle)* / *time limit (hard cap)*): a fresh session picks up the
  `HANDOFF.md`, no revision is spent, the usual continuation cap (3, then an owner decision) applies. Ten minutes
  before the cap the session gets `HANDOFF_REQUEST.json` with reason `time limit`; a handoff written in answer is
  requeued as *time limit handoff*.
- Jobs already running when the loop is updated carry no idle limit in their spec: model lanes move them onto the
  current policy (`claude_cli.model_limits`) instead of killing them at the old hour; a deliberate cap (a chat
  turn's 600 s) is kept.
- No migration. Add a `timeouts:` block to `config/routing.yaml` only to change the defaults (the template shows it).

## 1.6.1 — 2026-10-23

Review round on 1.6.0 (verdict REVISE): owner intent wins over the stall sweep, the live refresh keeps the page
structure, and the suite can no longer start a real model session.

- **Stall sweep respects deliberate holds.** A stop the owner made (Control Center **Hold**, `set-status` by an
  `owner*` actor), a **snoozed** decision item, and a task whose latest delivery attempt is on the `human` lane (the
  owner's REJECT/MORE_LIKE) are never retried or diagnosed. The human case is linked through the attempt, not by
  comparing timestamps: the loop collects an answer after it was given, so the answer always predates the stop.
- **Live refresh no longer nests `<main>`/`<nav>`.** `live.js` handed idiomorph the parsed wrapper element; even with
  `morphStyle: "innerHTML"` that element *is* the new content, so every refresh inserted a second `<main>` (and
  `<nav>`) inside the first and shifted the page. The child nodes are now passed as an array. Checked in Chromium
  against a scratch project: before the fix 2 `<main>`/2 `<nav>` after one refresh; after it 1/1 through an automatic
  refresh, a held-then-applied refresh (draft kept in its own form) and three more in a row, with no layout shift.
- **`task.note {guidance: true}` is one idempotent write.** Note and guidance go in a single task update
  (`tasks.add_note(guidance=True)`), each keyed by the command id; a replay of a command whose note exists but whose
  guidance is missing completes the guidance (`{"repaired": "guidance"}`) instead of answering "already applied".
- **No real model client in the test suite.** An autouse guard (`tests/conftest.py`) refuses any launch of the
  installed `claude`/`codex` CLI (version/help probes allowed) and fails the test with the command it tried;
  `@pytest.mark.model_integration` opts out (`capacity_integration` does not). The tests that reached a real client
  now use fake lanes: the two loop-command tests run a python worker, and the Codex failure tests stub the diagnosis
  dispatch.
- `loop.py`: removed the duplicated `_epoch`/`scheduler_routing` helpers (`ruff check --select F supervisor` clean).

## 1.6.0 — 2026-10-23

Nothing stops silently: every failure is retried or escalated; the owner hears about it only when an action is theirs.

- A worker that ended **BLOCKED**, FAILED or PARTIAL (or crashed, timed out, was lost) used to leave its task FAILED
  with no retry, no diagnosis and no owner item — only "the worker claimed COMPLETE and verification disagreed"
  reached escalation. Now: BLOCKED → straight to the manager diagnosis (`escalation.record_failure(retry=False)`;
  re-sending the same brief reproduces the same block); a model-lane FAILED/PARTIAL → the normal revision budget,
  then diagnosis; an owner REJECT/MORE_LIKE on a `human` task stands. Issues are titled by cause (*worker reported
  BLOCKED*, *worker run failed repeatedly*, *stopped with nothing routing it*).
- The failure detail carries the worker's notes and the head of its `HANDOFF.md` (`loop.worker_report`), so revision
  briefs, diagnosis briefs, issues and owner items say *why*, not just `status BLOCKED; missing:`.
- **Stall sweep** (`Supervisor.sweep_stalled`, every iteration): a FAILED/BLOCKED task with nothing in flight and no
  open owner item, past `routing.yaml escalation.stall_grace_s` (default 600 s), is escalated once per stop (FAILED →
  retry within budget, BLOCKED → diagnosis); still unrouted a grace period later → one owner decision item.
  Deterministic lanes (process/python/extensions) are retried this way, so the grace period is their back-off.
  Owner answers and diagnosis `defer` after the stop are respected as deliberate holds.
- **Idle alert** (`Supervisor.alert_if_idle`): nothing running, nothing eligible, nothing waiting on the owner, work
  remaining, for twice the grace period → one *Pipeline idle — nothing can run* owner item listing the stuck tasks
  and their last note; once per stuck set (`state/runtime/idle_alert.json`).
- Agent sessions can be given folders outside the repository: role `add_dirs` and
  `claude.add_dirs_by_cost.<cost_category>` in `routing.yaml` (Claude Code `--add-dir`; relative entries resolve
  against the project root). Without them the CLI denies even `ls` there and the worker can only end BLOCKED.
- `task.note` with `"guidance": true` also hands the text to the next delivery brief.
- Escalation decisions carry `at` (used by the sweep). No migration; existing escalation state files are read as
  before.

## 1.5.0 — 2026-10-23

Live Control Center: open pages keep themselves current; no more F5.

- Every page polls `GET /live/version` every 2 s (only while its tab is visible). The value is a stat-only
  fingerprint of `state/` (minus applied commands, lock/temp/log files and the update-check cache), `config/` and
  `project.yaml`, cached for 1 s so many tabs cost one scan (`supervisor/ui/live.py`). `state/runtime/supervisor.json`
  counts by content without its heartbeat fields: a loop tick is not a change, a status or decision change is.
- When it moves, `static/live.js` re-fetches the page and morphs `<main>` and the sidebar (inbox badge) into place
  with the vendored idiomorph 0.7.4 (0BSD, `static/idiomorph.min.js`). Scroll position, focus and opened
  `<details>` survive. Pages also refresh every 30 s regardless, so ages and timers stay current.
- Nothing the owner is doing is lost: while a form field has been changed, a file is chosen or text is selected,
  the update is held back and the new **Live** badge in the top bar says **Updates waiting — apply**; clicking it
  applies the update and keeps typed values in their own form (matched by form action and field name). Elements
  marked `data-live-keep` (the Overview's action result) and SSE streams (log and transcript tails) are never
  touched. Inbox items, owner tasks and roadmap gates carry stable ids so a morph never pairs one item's form with
  another's.
- Clicking **Live** pauses updates in that browser (remembered); clicking again resumes. **Reconnecting…** shows
  while the Control Center does not answer; when it comes back as a new process (an update, a restart) or its
  stylesheet changed, open pages reload fully onto the new templates.
- A page that keeps its own client-side state opts out with any element carrying `data-live-off`: it is never
  refreshed and the badge reads **Live off on this page**.
- Each page is stamped (`<meta name="live-version">`) with the fingerprint taken in middleware before the route reads
  any state, so a change landing mid-render refreshes the page once more instead of being missed.

**Upgrade note.** No migration. Extension pages get the same behaviour; an extension page that renders from files
outside `state/` and `config/` refreshes on the 30 s fallback (or when project state changes). Mark any element an
extension fills in client-side with `data-live-keep`.

## 1.4.1 — 2026-10-23

Fixes from the independent review of 1.4.0.

- Fix: the owner's REQUEUE text never reached the next worker. It was saved only as a task note, which no packet
  reads, so the item was applied and the task went READY while the agent got none of the correction. It is now
  written to the task's `guidance` (as the diagnosis requeue already was), and the delivery brief carries it under
  "Guidance". Adapters also append it to a delivery prompt that did not come from the default brief (a job's own
  `prompt`/`prompt_file`, the codex fallback prompt). A review resume scopes the text to the review of that digest
  ("Owner guidance for this review"); it never reaches a delivery brief. Re-applying an item never adds it twice.
- Fix: REQUEUE did not reset an exhausted escalation budget. The spent revisions and failure signatures survived, so
  the next failure escalated at once. Every owner REQUEUE now resets them (`escalation.reset_retries`, keyed by the
  inbox item so a replay after a crash never grants a second reset).
- Fix: valid artifact paths were refused. Spaces and placeholder-like spellings (`Final Report.md`, `out/xx.json`,
  `round_N_results.txt`) are legal file names that verify fine, but `task create` and `task import` rejected them.
  `tasks.artifact_problems()` now splits **errors** (no file can ever match: absolute/escaping paths, directories,
  `<>"|` and control characters, padded or empty entries) from **warnings** (prose- or placeholder-like but legal;
  none when the entry already matches a file). Warnings are returned as `warnings`, logged, and printed by the CLI.
- Fix: projection exclusions outlived their registration. Declared paths accumulated, so changing `path=` from
  WEEKLY.md to REPORT.md kept both excluded; a projection removed from a still-configured extension stayed excluded;
  and with no projection registered, 1.2.1 legacy entries were never dropped. `extensions.load()` now reconciles the
  record with what each successfully loaded extension registers: unregistered keys go, declared paths are replaced
  (a rendered path learned from the old declaration goes with it), and legacy entries go when nothing is registered.
  Entries of an extension that failed to load, or that is not configured, are kept (re-adding one restores them).
- Fix: distinct projections with the same `__name__` (two lambdas, two closures from one factory) shared one record
  key, so rendering the second retired the first. Keys are now unique (`<lambda>`, `<lambda>#2`, ...);
  `registry.projection(fn, path=None, name=None)` takes an explicit `name` for an identity that survives reordering
  (a duplicate `name` within one extension is a setup error).
- Tests: the Windows recovery adoption test holds its child on a release flag instead of a 1.5 s sleep (a child that
  finished during the process scan was correctly adopted as finished, and the test failed). The suite no longer calls
  the workstation's real Codex usage RPC: a nearly spent real window gated the fake jobs of `test_codex_cli`. An
  autouse fixture reports the RPC unavailable; `@pytest.mark.capacity_integration` opts a test back in.

**Upgrade note.** No migration. Existing `guidance` entries (diagnosis) are read as before. A seed entry 1.4.0
refused for its spelling alone now imports with a warning.

## 1.4.0 — 2026-10-23

Owner decisions that actually reach the owner and take effect; inbox text that is never cut short; contract fixes
that reach a running loop.

- Inbox items keep their full text. `inbox.fit()` splits long prose into a `summary` (≤ 600 chars, cut on a word
  boundary) and a `detail` (≤ 8000 chars); `create(detail=...)` stores it, the Control Center shows it behind
  **Show full message** and `HUMAN_INBOX.md` renders the full text. Before, every writer hard-cut at 600 characters
  and whatever came last was lost — for an escalation that was the question and the options. Diagnosis owner route,
  delivery approvals and the human adapter all use it; a diagnosis summary is kept up to 4000 characters (was 1500).
- New `decision` inbox kind (`REQUEUE / CANCEL / DEFER`, "Stopped tasks — your decision" in the inbox). The loop
  opens one per block for a task BLOCKED with "owner decision needed" (review budget spent, continuations
  exhausted, review invalidated) — before, such a task only had an issue and a note and nothing reached the owner.
  The diagnosis owner route opens one for the FAILED task, with the question and the options first.
  `inbox.apply_decision` acts on the answer: REQUEUE resets the budget that stopped the task and resumes it
  (`resume: review | delivery | task`), CANCEL cancels, DEFER annotates; the outcome is recorded as `applied`, and
  the loop re-applies an answered item whose effect never landed.
- Fix: REQUEUE/CANCEL on a diagnosis escalation did nothing — it was a `question` item, so the answer was recorded
  and ignored. Such items written by earlier versions are recognised (from `escalation`, REQUEUE/CANCEL/DEFER) and
  acted on when answered.
- Fix: text typed next to a button ("REQUEUE use the cached outputs", the Control Center sends both as one
  response) was dropped; it is now added to the task as owner guidance.
- `supervisor task import [--update-contracts ...]` goes through the command queue (`task.import`, payload
  `{seed?, update_contracts?, create?}`), so it works while a supervisor runs. Before, it refused while
  `state/.lock` was held — and since every agent works under the loop, no agent could ever apply a contract fix it
  had made in the seed. The seed must be a file inside the project.
- Task contracts: artifact entries the floor can never satisfy are refused by `task import` and `task create`
  (`tasks.artifact_errors`): prose ("contact sheets"), placeholders (`<id>`, `{x}`, `S1..S6`, `EXP-1xx`, `round_N`),
  directories (`src/`, or an existing directory named without a glob) and absolute or escaping paths. Such an entry
  failed every delivery with "missing: …" forever. A non-glob entry that names a directory now says so in the
  verification detail instead of plain "missing". `docs/TASK_CONTRACT.md` documents the rule; its seed example
  (`src/`) is now `src/**/*.py`.

**Upgrade note.** No migration. A project whose seed still contains such an entry gets a clear error from
`task import` naming each one — fix the seed (glob or file path) and import again. Stored contracts are not
re-checked; run `supervisor task import --update-contracts <ids>` after fixing the seed to refresh live tasks.

## 1.3.0 — 2026-10-23

Control Center Overview: clearer controls and a live view of current work.

- Control row: no button is highlighted any more. The purple "suggested next action" (Pause while running) read as
  the current state. Each button is now enabled only when it applies (Start only while offline, Pause only while
  running, Resume only while paused or draining, Stop/Reload only while live) and a disabled one says why on hover.
  A decision the loop has not applied yet already counts, so Pause greys out the moment it is clicked.
- Supervisor card: the "Current task" cell is gone, so the uptime no longer sits beside a task and reads as the
  task's age. The card shows Workspace, Process (PID), **Supervisor uptime** (with "Running since HH:MM") and Open
  jobs; draining, a pending power-off and pause show as a note under the details.
- New **Now running** panel under the metric cards: every open agent/process job (owner jobs excepted; they are
  counted as "waiting for you") with task title and ID, what it is doing (Working / Reviewing / Diagnosing a
  failure / Answering chat / Verifying), role, model and effort, attempt number, running time against the job
  timeout, and a link to the live transcript; deferred jobs show when they retry. An **Up next** column lists the
  next eligible tasks.
- The panel and the Supervisor card refresh themselves every 5 s (`GET /overview/live`, card swapped out-of-band;
  polling pauses while the tab is hidden) and right after any control is clicked. Action results are kept.
- Fix: a view that cannot render no longer fails the loop iteration. A YAML record caught half-written (an agent
  editing it) made `render_all` raise on every pass; five in ten minutes stopped the supervisor BLOCKED with the
  agent's delivery still uncollected. Each built-in view is now rendered on its own: a failure skips that view
  (its previous file stays), logs `render_error` once per distinct message and `render_recovered` when it renders
  again. Extension projections already behaved this way.

## 1.2.3 — 2026-10-23

Fourth review round (on 1.2.2, commit 52c639e).

- Fix: chat replies stalled on a freshly stamped project — 1.2.2 made the gate read the `conversation` lane's
  `active` flag and the template ships it as false. The chat lane is owner-driven, not a role that has or lacks
  work, so `active` is ignored for it (projects stamped by 1.2.2 or earlier need no change; the template now says
  `active: true` with a note that it is informational).
- Fix: projection exclusions are recorded per projection (`state/runtime/projection_paths.json` `projections`:
  `<extension>:<function>` → declared paths + last rendered path). A projection that starts writing REPORT.md
  instead of WEEKLY.md retires the old file (1.2.2 kept both excluded forever). A 1.2.1 flat `paths` list and the
  1.2.2 `by_extension` map are read as unattributed entries and honoured until every projection has rendered once
  on the new code, so an upgrade never rejects an untouched view before the first render.

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
