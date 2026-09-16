#!/bin/sh
# Preserve the documented entrypoint; Python parses compiler argv without eval.
set -eu
exec python3 "$(dirname -- "$0")/concurrent-tsan.py" "$@"
