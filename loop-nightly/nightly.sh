#!/bin/bash
# Nightly autonomous worker — entry point (meant to run at night, via loop-run.sh).
# collect -> rank -> run_task for each selected candidate. Locked against a double run,
# stops at NIGHT_END.
set -uo pipefail
cd "$(dirname "$0")"

# macOS doesn't ship flock. mkdir is atomic on every supported filesystem;
# trap releases the lock on any normal exit or signal.
LOCK_DIR="${NIGHTLY_LOCK_DIR:-$PWD/.nightly.lock.d}"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -d "$LOCK_DIR" ]; then
    echo "another run is already in progress — exiting"
    exit 0
  fi
  echo "could not create lock dir: $LOCK_DIR" >&2
  exit 1
fi
cleanup_lock() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup_lock EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found — stopping" >&2
  exit 127
fi

echo "=== night $(date '+%F %T') ==="
python3 collect.py || { echo "collect: failed — stopping"; exit 1; }
python3 rank.py    || { echo "rank: failed — stopping"; exit 1; }

if ! IDS=$(python3 - <<'EOF'
import db
c = db.connect()
rows = c.execute("SELECT id FROM candidates WHERE night=? AND status='selected'", (db.tonight(),)).fetchall()
print("\n".join(str(r["id"]) for r in rows))
EOF
); then
  echo "could not read the selected tasks — stopping" >&2
  exit 1
fi
if [ -z "$IDS" ]; then
  echo "pool is empty — nothing to take (one such night is normal)"
  # One empty night is normal; a long streak means the task SOURCE has dried up, not
  # that there's no work. Count days since the last night that selected anything.
  python3 - <<'EOF' || true
import datetime, subprocess, sys
from pathlib import Path
import db

STALE_DAYS = 3
c = db.connect()
row = c.execute("SELECT MAX(night) AS n FROM candidates WHERE status='selected'").fetchone()
last = row["n"] if row else None
if not last:
    print("warning: no task has ever been selected in this database's history")
    sys.exit(0)
days = (datetime.date.today() - datetime.date.fromisoformat(last)).days
if days < STALE_DAYS:
    sys.exit(0)
msg = (f"🌙 The nightly worker has been idle for {days} days with no task taken "
       f"(last selection: {last}). The pool is empty not because there's no work, "
       f"but because nothing is filling it — check the upstream collectors.")
print(msg)
notify = Path.cwd().parent / "loop-status" / "notify.py"
if notify.exists():
    subprocess.run(["python3", str(notify), msg], timeout=40)
EOF
  exit 0
fi

if ! NIGHT_END_H=$(python3 -c "import policy; print(policy.NIGHT_END_HOUR)"); then
  echo "could not read NIGHT_END_HOUR — stopping" >&2
  exit 1
fi
RUN_FAILED=0
for id in $IDS; do
  H=$(date +%H)
  # curfew: after NIGHT_END (morning) and before 22:00, don't start a new task if the run
  # started during the day (a manual daytime run is opt-out via NIGHTLY_NO_CURFEW=1)
  if [ "${NIGHTLY_NO_CURFEW:-0}" != "1" ] && [ "$H" -ge "$NIGHT_END_H" ] && [ "$H" -lt 22 ]; then
    echo "time's up (>= 0${NIGHT_END_H}:00) — not starting the remaining tasks"
    break
  fi
  echo "--- task $id ---"
  if ! python3 run_task.py --once "$id"; then
    echo "task $id: did not reach a PR (see logs/)"
    RUN_FAILED=1
  fi
done

if ! python3 - <<'EOF'
import db
c = db.connect()
n = db.tonight()
rows = c.execute("SELECT status, count(*) c FROM runs WHERE night=? GROUP BY status", (n,)).fetchall()
print("night summary:", ", ".join(f"{r['status']}={r['c']}" for r in rows) or "nothing")
EOF
then
  echo "could not summarize the night" >&2
  exit 1
fi

exit "$RUN_FAILED"
