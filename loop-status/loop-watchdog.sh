#!/usr/bin/env bash
# Watchdog: catches tasks that did NOT complete successfully within their expected
# window (machine was asleep, the scheduler didn't fire, or the task quietly failed
# several times in a row). Meant to run daily via loop-run.sh. Only alerts on a
# regression (the label has history but no success in-window) — never on a label that
# simply hasn't run yet.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"

# Windows are sized to each task's real schedule plus slack. Example label sets below —
# swap in your own; keep the buckets (daily/frequent/weekly), the watchdog logic itself
# doesn't care about the names.

# daily: expect a success within the last 26h (slack over the 24h cadence)
DAILY="content-sync feed-ingest vault-hygiene backup-pull backup-push
       revenue-sync map-check daily-review idea-digest tasks-digest
       partner-sync loop-queue-digest notify-flush-morning notify-flush-evening"

# frequent: interval 5-30 min, window 3h
FREQUENT="daemon-watch dashboard-collect reminders-sync"

# weekly: window 8 days
WEEKLY="partner-weekly backup-cloud ad-factory-weekly weekly-pulse"

OVERDUE="$(python3 "$HERE/status.py" overdue 26  $DAILY)
$(python3 "$HERE/status.py" overdue 3   $FREQUENT)
$(python3 "$HERE/status.py" overdue 192 $WEEKLY)"
OVERDUE="$(printf '%s' "$OVERDUE" | sed '/^[[:space:]]*$/d')"

if [ -n "$OVERDUE" ]; then
  python3 "$HERE/notify.py" "🔴 agent-loops watchdog: tasks with no success in 26h:
$OVERDUE

Check: did the machine sleep? logs? status.py summary" 2>/dev/null || true
  echo "watchdog: alert sent"; echo "$OVERDUE"
else
  echo "watchdog: all daily tasks are fresh"
fi
# Status is written by loop-run.sh from the actual exit code. Earlier versions of this
# script wrote an unconditional success here — the watchdog reported "ok" even when it
# had itself crashed.

# Currently-red tasks — independent of the overdue window.
# Rationale: `overdue` only fires once a task has had no success for longer than its
# window (26h for daily jobs), so a single failure produced exactly one message at the
# time it failed, then silence until the window expired. Now every watchdog run
# reminds you while a label stays red.
FAILING="$(python3 "$HERE/status.py" failing 2>/dev/null)"
if [ -n "$FAILING" ]; then
  python3 "$HERE/notify.py" "🔴 agent-loops: red tasks (last run failed)

$FAILING

Red doesn't fix itself. Find the cause, run it by hand, confirm it's green." 2>/dev/null || true
  echo "watchdog: there are red tasks"; echo "$FAILING"
else
  echo "watchdog: no red tasks"
fi

# Coverage check: did an agent start up without anyone watching it? Alerts itself.
bash "$HERE/coverage-check.sh" || true
