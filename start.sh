#!/usr/bin/env bash
# Sets up (or reuses) a local venv, makes sure deps are installed, then
# always launches Maestro through that venv's Python — never the
# system one. Safe to re-run any time; every step is idempotent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "error: '$PYTHON_BIN' not found. Install Python 3.9+ first." >&2
    exit 1
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "==> No venv found at $VENV_DIR — creating one..."
    if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
        echo "error: could not create venv (often means the venv module is" >&2
        echo "  missing). On Debian/Ubuntu try:  sudo apt install python3-venv" >&2
        exit 1
    fi
fi

VENV_PY="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

echo "==> Checking pip in venv..."
if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    echo "==> pip missing in venv, bootstrapping it..."
    "$VENV_PY" -m ensurepip --upgrade
fi

echo "==> Ensuring dependencies are installed (requirements.txt)..."
"$VENV_PIP" install -q --upgrade pip
"$VENV_PIP" install -q -r requirements.txt

if ! command -v claude >/dev/null 2>&1; then
    echo "warning: 'claude' CLI not found on PATH. Maestro needs it" >&2
    echo "  to run agents — install Claude Code first: https://docs.claude.com/claude-code" >&2
fi

echo "==> Starting Maestro..."
exec "$VENV_PY" -m maestro.main "$@"
