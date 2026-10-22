#!/usr/bin/env bash
# setup_env.sh - Linux/macOS twin of setup_env.ps1: python3.11 venv + editable install of the framework.
#   tools/setup_env.sh [/path/to/Supervisor]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRAMEWORK="${1:-${SUPERVISOR_FRAMEWORK:-$(dirname "$ROOT")/Supervisor}}"
[ -f "$FRAMEWORK/pyproject.toml" ] || { echo "Supervisor framework not found at $FRAMEWORK"; exit 1; }
PY="$(command -v python3.11 || command -v python3)"
[ -x "$ROOT/.venv/bin/python" ] || "$PY" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install --upgrade pip wheel >/dev/null
"$ROOT/.venv/bin/python" -m pip install -e "$FRAMEWORK[ui]"
mkdir -p "$ROOT/runs/tmp"
cd "$ROOT" && .venv/bin/python -m supervisor version && .venv/bin/python -m supervisor doctor || true
echo "Next: .venv/bin/python -m supervisor ui   (and, in another shell, .venv/bin/python -m supervisor run)"
