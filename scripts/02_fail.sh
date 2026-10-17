#!/usr/bin/env bash
# Demo script: always fails to show return-code handling.
set -euo pipefail

echo "about to fail" >&2
exit 3
