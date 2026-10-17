#!/usr/bin/env bash
# Demo script intentionally left WITHOUT the executable bit.
# On Linux it is skipped unless you run with --force (or chmod +x it).
set -euo pipefail

echo "you bypassed the exec-bit check with --force"
exit 0
