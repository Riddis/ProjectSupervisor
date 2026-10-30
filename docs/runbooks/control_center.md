# Runbook — Control Center

## Start

- `start_control_center.bat` in the project (UI only; opens the browser) or `start_supervisor.bat` (loop + UI).
- Or `.venv\Scripts\python.exe -m supervisor ui` from the project folder. Port from `project.yaml ui.port`
  (default 8790) or `--port`.
- Local access needs no login. The title and brand letter come from `project.yaml name`.

## Screens

Overview (a "Now running" panel refreshed every 5 s — one row per open job with task, activity, role/model, attempt,
running time against the hard cap, how long a job has been quiet, the stage the job reports about itself
(*downloading*, *training*, … with a bar when it gives numbers; see below), **+1h** to extend one running job's hard cap
and a live-log link, plus the next eligible tasks; supervisor controls
Start/Pause/Resume/Stop/Reload/Doctor/Shut down PC when idle, enabled only when they apply; capacity, disk,
plugin panels, phase & gates, recent activity), Roadmap (phases, gates, tasks per phase), Tasks (list + detail:
attempts, artifacts, notes, re-queue/cancel/hold/priority), Agents (live jobs, SSE transcript tail, terminate, drop a
note the agent reads between steps), Inbox (answer/snooze/reopen, gate acknowledgement, file upload to
`uploads/<item>/`; a delivery approval shows what it approves — every file frozen in the digest, readable in place or
opened from `/tasks/<id>/files/<n>`, the verification and each live review's verdict, summary and findings, and a
warning naming any declared artifact the digest does not cover; the task page shows the same), Chat (threads with a role, grounded in the live state; convert a reply into a task / decision /
issue / owner task), Log (SSE tail of `state/log.jsonl`), Owner tasks, Settings (the few numeric routing knobs).
Plugins can add sidebar pages (`registry.ui_router`) and overview panels (`registry.overview_panel`).

## Live pages

Every page updates itself while it is open — no F5. It polls `GET /live/version` every 2 s while the tab is visible
(a cheap fingerprint of `state/`, `config/` and `project.yaml`; loop heartbeats alone do not count) and, when the
project changed, morphs the new page into place; it also refreshes every 30 s for ages and timers. The **Live** badge
in the top bar shows the state:

- **Live** — up to date. Click to pause updates in this browser (remembered); click **Live updates paused** to resume.
- **Updates waiting — apply** — held back because a form on the page has unsent input or text is selected. Clear the
  field (or send it) and the update applies by itself; click the badge to apply now (typed text is kept).
- **Reconnecting…** — the Control Center is not answering. When it is back as a new process, open pages reload.
- **Live off on this page** — the page keeps its own client-side state (it carries `data-live-off`); reload it to
  see changes.

Scroll position, opened sections, the Overview's action result and the log/transcript tails are kept across
updates. Implementation: `supervisor/ui/live.py`, `supervisor/ui/static/live.js` (vendored idiomorph). Template
authors: give repeated items with forms a stable `id`, and mark elements filled client-side with `data-live-keep`.

## Framework updates

At launch the UI fetches the framework clone's `origin` and, on a terminal, asks whether to apply what it is
behind; while running, the Overview shows an "Framework update available" banner with the commit list and an
**Update now** button (`POST /overview/update`). See `docs/UPDATING.md` for what applying does. The UI exits with
code 3 afterwards; `tools/run_control_center.ps1` relaunches it. `--no-update-check` skips the launch check.

## How writes work

Every mutating route calls `supervisor.commands.submit_or_apply(layout, kind, payload, actor="owner-ui")` — the
same command-file mechanism the CLI uses. If a loop holds `state/.lock` the command is queued and the page shows
**pending** with the command id; the loop applies it within one iteration. With no loop running the UI applies it
itself and shows **applied**. The UI never writes `state/` directly.

## Security

- Binds `127.0.0.1`. A Host/Origin middleware rejects non-loopback hosts (400) and mutating requests without a
  loopback Origin/Referer when one is present. Treat any non-loopback bind as experimental.
- CSRF: an HttpOnly `supervisor_csrf` cookie is issued on first visit; forms carry the matching field.
- No route accepts a filesystem path from the browser: artifacts are looked up by task id, thumbnails by an opaque
  key, run dirs by attempt id. Uploads are sanitised to a filename under `uploads/<item id>/`.
- The UI never touches `secrets/`.

## Shut down PC when idle

With a live supervisor the button submits `loop.drain {"shutdown": true}`: nothing new launches (no deliveries,
reviews or conversations), running jobs finish and their results are collected and ingested, then the loop sets STOP
and asks the OS to power off after 60 s (`shutdown /s /t 60` on Windows, `shutdown -h +1` elsewhere). Reviews,
approvals and checkpoints that the ingested results call for are not dispatched while draining; they run on the next
start. A `human` job never holds the drain open. The button becomes **Cancel shutdown** while draining or counting down. With the
supervisor offline the same button powers off after the grace period, cancellable. From the CLI: `supervisor drain
--shutdown`, `supervisor resume`.

## Troubleshooting

- An action stays "pending": a loop runs elsewhere and has not applied the file yet — check `state/commands/` and
  `supervisor supervisor`.
- Start does nothing: `state/runtime/supervisor_ui_launch.log` has the child's output; the usual cause is a
  framework version mismatch (run `supervisor doctor`).
- Disk panel shows "Unavailable": the drive could not be queried; the page still renders.

## Job time limits (1.7.0)

A running job is stopped only when it goes **quiet** (no new output and under `idle_cpu_s` CPU seconds of work in its
processes for `idle_s`, default 20 min) or reaches its **hard cap** (default 6 h for agent sessions) — see
`supervisor/timeouts.py` and `config/routing.yaml: timeouts`. **+1h** on a *Now running* row (or **Extend +1h** on
the agent page) is the `agent.extend` command: it raises that one job's cap by an hour, idempotently per command id;
the quiet limit still applies. Ten minutes before the cap an agent session is asked to write its handoff; a
timed-out agent session is continued from its HANDOFF.md by a fresh session (`job_requeued`, reason *time limit*),
not counted as a failed attempt.

Quiet time is measured from the first time the loop looks at a job, not from its launch, so a session that was
already running when the loop was updated gets a full quiet window before it can be stopped. The cap shown, the cap
Extend raises and the cap enforced are the same number (`timeouts.effective_limits`): a session started before
1.7.0 shows and extends the current policy's cap, not the old one-hour deadline.

Each owner action writes one log line: a command whose handler logs its own event (`job_extended`, a status change,
…) adds no generic `command` line; the event carries the command's id in `command`. The generic line remains for a
failed command and for one whose handler logs nothing.

**Progress reports** (`supervisor/progress.py`). A running job may say what it is doing in
`<run dir>/PROGRESS.json` — `{"stage": "training", "detail": "step 300 of 500", "done": 300, "total": 500}`,
overwritten at each step. Delivery briefs ask agent sessions to write it before any step longer than a few minutes;
every launched job and the scripts it starts inherit `SUPERVISOR_PROGRESS_FILE`, and
`from supervisor.progress import report` writes it from Python (a no-op outside the supervisor). The *Now running*
row and the agent page show the stage, the detail, a bar when `done`/`total` are given and when it was last updated;
the log gets one `job_progress` line per stage change, not per step. It is advisory: never evidence, never trusted
for verification, and a missing or malformed file shows nothing.
