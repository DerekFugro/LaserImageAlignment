#!/usr/bin/env bash
# LaserImageAlignment launcher for macOS / Linux.
#
# The GUI needs PySide6, which is fine off PyPI here; the bundled wheels\ folder
# is Windows-only so it is ignored on this platform. `core/` and `cli.py` are
# pure Python and have always been portable - this script exists so that is
# actually reachable without a Windows box.
set -euo pipefail
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
    exec uv run app.py "$@"
fi

# Same reasoning as launch.bat: a venv copied from another machine still
# exists but cannot run, so test it rather than trusting the folder.
if [ -d .venv ] && ! .venv/bin/python -c "" >/dev/null 2>&1; then
    echo "Existing .venv does not work on this machine - rebuilding it..."
    rm -rf .venv
fi
if [ ! -d .venv ]; then
    echo "First run: creating virtual environment..."
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python app.py "$@"
