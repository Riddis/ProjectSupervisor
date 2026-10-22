# Runbook — Control Center

## Start

- `start_control_center.bat` in the project (UI only; opens the browser) or `start_supervisor.bat` (loop + UI).
- Or `.venv\Scripts\python.exe -m supervisor ui` from the project folder. Port from `project.yaml ui.port`
  (default 8790) or `--port`.
- Local access needs no login. The title and brand letter come from `project.yaml name`.

## Screens

Overview (supervisor controls Start/Pause/Resume/Stop/Reload/Doctor/Shut down PC when idle, capacity, disk,
extension panels, phase & gates, recent activity), Roadmap (phases, gates, tasks per phase), Tasks (list + detail:
attempts, artifacts, notes, re-queue/cancel/hold/priority), Agents (live jobs, SSE transcript tail, terminate, drop a
note the agent reads between steps), Inbox (answer/snooze/reopen, gate acknowledgement, file upload to
`uploads/<item>/`), Chat (threads with a role, grounded in the live state; convert a reply into a task / decision /
issue / owner task), Log (SSE tail of `state/log.jsonl`), Owner tasks, Settings (the few numeric routing knobs).
Extensions can add sidebar pages (`registry.ui_router`) and overview panels (`registry.overview_panel`).

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
