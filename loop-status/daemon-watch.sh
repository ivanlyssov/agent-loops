#!/usr/bin/env bash
# Watchdog for long-running daemons.
#
# Why this is separate from loop-watchdog: that one catches tasks that were SUPPOSED to
# run and didn't, based on rows in status.db. Daemons don't leave rows — they don't
# "run and exit", they just live. Wrapping them in loop-run.sh doesn't work either: it
# writes a row on EXIT, so a healthy daemon would never produce one, and the watchdog
# would call it overdue forever.
#
# What actually means "healthy" for a daemon:
#   • KeepAlive service    → it has a PID in launchctl;
#   • interval-based poller → its last exit was zero (LastExitStatus).
#
# A real audit of this harness's deployment found six such services with no supervision
# at all — including the dashboard process itself, a digest bot, and a partner-sync bot.
# A crash would only have been noticed by someone checking manually.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL_PREFIX="${LAUNCHD_LABEL_PREFIX:-com.example.agent-loops.}"

# name:mode — pid (must be running) | exit (must have last exited cleanly)
# Example set — swap in your own long-running services.
DAEMONS="
dashboard:pid
digest-bot:pid
chat-relay:pid
loop-queue-poller:pid
media-transcriber:pid
partner-sync-bot:exit
"

PROBLEMS=""
CHECKED=0

for entry in $DAEMONS; do
  name="${entry%%:*}"; mode="${entry##*:}"
  CHECKED=$((CHECKED+1))
  info="$(launchctl list "${LABEL_PREFIX}${name}" 2>/dev/null)"

  if [ -z "$info" ]; then
    PROBLEMS="$PROBLEMS
• $name — NOT LOADED in launchd (agent was removed, or its plist is gone)"
    continue
  fi

  pid="$(printf '%s' "$info"  | awk -F'= ' '/"PID"/{gsub(/[;" ]/,"",$2); print $2}')"
  last="$(printf '%s' "$info" | awk -F'= ' '/"LastExitStatus"/{gsub(/[;" ]/,"",$2); print $2}')"

  case "$mode" in
    pid)
      if [ -z "$pid" ]; then
        PROBLEMS="$PROBLEMS
• $name — no process running (last exit: ${last:-?}); expected to run continuously"
      fi ;;
    exit)
      if [ -n "$last" ] && [ "$last" != "0" ]; then
        PROBLEMS="$PROBLEMS
• $name — last run exited with code $last"
      fi ;;
  esac
done

if [ -n "$PROBLEMS" ]; then
  python3 "$HERE/notify.py" "🔴 agent-loops: long-running services are unhealthy ($CHECKED checked):$PROBLEMS

Restart: launchctl kickstart -k gui/<uid>/${LABEL_PREFIX}<name>" 2>/dev/null || true
  echo "daemon-watch: problems found:$PROBLEMS"
  exit 1
fi

echo "daemon-watch: all $CHECKED services are alive"
