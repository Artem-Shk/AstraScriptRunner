#!/usr/bin/env bash
# Convenience launcher for script_runner on Linux.
#
# Usage:
#   ./run.sh                       # run every executable script in ./scripts
#   ./run.sh -r -m parallel -j 4   # any script_runner.py flags are forwarded
#   ./run.sh -- --env prod         # args after -- go to every script
#
# It ensures the Python file is executable, then delegates to it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRY="${HERE}/script_runner.py"

# Prefer the system python3, fall back to python.
PY="$(command -v python3 || command -v python || true)"
if [[ -z "${PY}" ]]; then
    echo "error: python3 not found in PATH" >&2
    exit 2
fi

chmod +x "${ENTRY}" 2>/dev/null || true

# Default target directory is ./scripts when the caller did not pass one.
if [[ $# -eq 0 ]]; then
    exec "${PY}" "${ENTRY}" "${HERE}/scripts" -r
fi
exec "${PY}" "${ENTRY}" "$@"
