#!/usr/bin/env bash
# Scheduler entry point for the morning briefing (meant to run once each morning).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
exec python3 briefing.py "$@"
