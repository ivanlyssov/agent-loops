#!/usr/bin/env bash
# Wrapper for the scheduler/CLI: fixes cwd and calls python3.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" && exec python3 audit.py "$@"
