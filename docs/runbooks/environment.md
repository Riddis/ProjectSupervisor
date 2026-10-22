# Runbook — environment

- Python 3.11+ (3.11 is what the Windows launchers look for via `py -3.11`). One venv per project at
  `<project>/.venv`, with the framework installed editable from the local clone (`tools\setup_env.ps1 -Framework
  <path>` / `tools/setup_env.sh <path>`).
- `PYTHONDONTWRITEBYTECODE=1` everywhere (set by the launchers): a project may be shared between Windows and a Linux
  VM, and stale bytecode across platforms has bitten before.
- Temp: the launchers point `TMP`/`TEMP` at `<project>/runs/tmp` so verification subprocesses never depend on a
  user temp directory that may be locked down.
- Agent CLIs: Claude Code (`claude`, signed in with `claude /login`), Codex CLI (`codex login`). `supervisor doctor`
  reports which it found and which the routing actually uses.
- Git: the project folder must be a repository for checkpoints to work (`git init`); the supervisor commits accepted
  work with the identity the repository has configured and never pushes.
- Windows: `start_control_center.bat` / `start_supervisor.bat`; PowerShell scripts are ASCII-only on purpose
  (PowerShell 5 reads unmarked files as ANSI).
- Linux/macOS: `python -m supervisor ui` and `python -m supervisor run` from the project folder; the shutdown
  control uses `shutdown -h +1` / `shutdown -c`.
