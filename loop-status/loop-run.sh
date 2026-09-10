#!/usr/bin/env bash
# Observability wrapper. Runs a task, captures its exit code + output tail, writes the
# result to status.db, and alerts Telegram on failure. Every scheduled task in this
# harness runs through this wrapper.
#
# Usage:  loop-run.sh <label> [--verify "<success criterion>"] [--grade "<artifact glob>"] -- <command...>
#   --verify — optional: after a successful exit code, a cheap local model (Ollama)
#   checks whether the criterion was ACTUALLY met (worker/evaluator judge, see
#   evaluate.py). This is advisory, not a gate — on passed=false it sends a separate
#   Telegram alert but does NOT change the run's exit code.
#   --grade — optional: self-grade the QUALITY of fresh artifacts (see grade.py,
#   rubric rubrics/<label>.json) against files changed during the run. Advisory:
#   score below threshold → Telegram alert, exit code unchanged. (A hard outward-
#   facing gate is the caller's job — e.g. the label's own loop calls grade.py --gate
#   before it queues anything for human approval.)
set -uo pipefail
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"

LABEL="$1"; shift

VERIFY=""; GRADE_GLOB=""
while true; do
  case "${1:-}" in
    --verify) VERIFY="$2"; shift 2 ;;
    --grade)  GRADE_GLOB="$2"; shift 2 ;;
    *) break ;;
  esac
done
[ "${1:-}" = "--" ] && shift

# Quiet mode: anything not listed as "immediate" in notify-policy.json gets spooled and
# sent as two digests a day instead of one message per event.
if ! python3 -c "
import json,sys,pathlib
p=pathlib.Path('$HERE/notify-policy.json')
imm=json.loads(p.read_text())['immediate'] if p.exists() else []
sys.exit(0 if '$LABEL' in imm else 1)" 2>/dev/null; then
  export NOTIFY_SPOOL=1
  export NOTIFY_LABEL="$LABEL"
fi

LOG="$(mktemp)"
START=$(date +%s)
# run the command, teeing output to both a log file and our own stdout (launchd/cron
# will usually capture the latter too)
"$@" > >(tee "$LOG") 2>&1
EC=$?
DUR=$(( $(date +%s) - START ))

python3 "$HERE/status.py" record "$LABEL" "$EC" "$LOG" 2>/dev/null || true

if [ "$EC" -ne 0 ]; then
  TAIL="$(tail -c 800 "$LOG" 2>/dev/null)"
  python3 "$HERE/notify.py" "⚠️ agent-loops: task *$LABEL* failed (exit $EC, ${DUR}s)
$TAIL" 2>/dev/null || true
elif [ -n "$VERIFY" ]; then
  # exit=0 — but the judge checks whether the criterion was ACTUALLY met, not just the exit code
  VERDICT_JSON="$(python3 "$HERE/evaluate.py" "$LABEL" "$VERIFY" "$LOG" 2>/dev/null)"
  VERDICT="$(python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('verdict','skipped'))" "$VERDICT_JSON" 2>/dev/null || echo skipped)"
  PASSED="$(python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('passed'))" "$VERDICT_JSON" 2>/dev/null || echo None)"
  REASON="$(python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('reason',''))" "$VERDICT_JSON" 2>/dev/null || echo "")"

  echo "[judge] $LABEL: verdict=$VERDICT passed=$PASSED — $REASON"
  python3 "$HERE/status.py" record-verdict "$LABEL" "$VERDICT" "$PASSED" "$REASON" 2>/dev/null || true

  if [ "$VERDICT" = "failed" ]; then
    python3 "$HERE/notify.py" "⚠️ $LABEL: exit 0, but the judge did not confirm success: $REASON" 2>/dev/null || true
  fi
fi

# self-grade the quality of artifacts created/changed during the run (advisory)
if [ "$EC" -eq 0 ] && [ -n "$GRADE_GLOB" ]; then
  FRESH="$(find $GRADE_GLOB -type f -newermt "@$START" 2>/dev/null | head -10)"
  if [ -n "$FRESH" ]; then
    GRADE_OUT="$(echo "$FRESH" | xargs python3 "$HERE/grade.py" "$LABEL" 2>/dev/null)"
    echo "[grade] $GRADE_OUT"
    LOW="$(printf '%s' "$GRADE_OUT" | python3 -c "
import json,sys
low=[]
for line in sys.stdin:
    try: d=json.loads(line)
    except: continue
    if d.get('verdict')=='graded' and d['score']<d['threshold']:
        low.append(f\"{d['artifact'].rsplit('/',1)[-1]}: {d['score']:.2f} — {d.get('reason','')}\")
print('\n'.join(low))" 2>/dev/null)"
    if [ -n "$LOW" ]; then
      python3 "$HERE/notify.py" "📉 $LABEL: artifacts below the quality threshold:
$LOW" 2>/dev/null || true
    fi
  fi
fi

rm -f "$LOG"
exit $EC
