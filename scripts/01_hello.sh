#!/usr/bin/env bash
# Demo script: prints a greeting and forwards its arguments.
set -euo pipefail

echo "hello from $(basename "$0")"
echo "received ${#} argument(s): ${*:-<none>}"
exit 0
