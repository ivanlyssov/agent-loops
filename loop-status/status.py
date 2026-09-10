#!/usr/bin/env python3
"""Run log for the loop harness (SQLite, stdlib only). Every scheduled-task run writes a row here.
CLI:
  status.py record <label> <exit_code> [tail_file]   — record one run
  status.py last <label>                              — print the last run for a label
  status.py overdue <max_age_h> <label...>            — labels with no SUCCESS in the window (for the watchdog)
  status.py summary                                   — summary across all known labels
  status.py record-verdict <label> <verdict> <passed> <reason>  — record a judge verdict (worker/evaluator)
  status.py last-verdict <label>                      — print the last judge verdict
  status.py failing [label...]                        — labels whose LAST run is red (for the watchdog)
"""
import sys, sqlite3, time
from pathlib import Path

DB = Path(__file__).resolve().parent / "status.db"

def conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS runs(
        label TEXT NOT NULL, ts INTEGER NOT NULL, exit_code INTEGER NOT NULL,
        status TEXT NOT NULL, tail TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_runs ON runs(label, ts)")
    # judge verdicts (worker/evaluator) — separate table, doesn't touch the runs schema
    c.execute("""CREATE TABLE IF NOT EXISTS verdicts(
        label TEXT NOT NULL, ts INTEGER NOT NULL, verdict TEXT NOT NULL,
        passed INTEGER, reason TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_verdicts ON verdicts(label, ts)")
    return c

def record(label, exit_code, tail_file=None):
    tail = ""
    if tail_file and Path(tail_file).exists():
        tail = Path(tail_file).read_text(encoding="utf-8", errors="replace")[-1500:]
    status = "ok" if int(exit_code) == 0 else "error"
    c = conn()
    c.execute("INSERT INTO runs(label, ts, exit_code, status, tail) VALUES(?,?,?,?,?)",
              (label, int(time.time()), int(exit_code), status, tail))
    c.commit(); c.close()

def last(label):
    c = conn()
    row = c.execute("SELECT ts, exit_code, status, tail FROM runs WHERE label=? ORDER BY ts DESC LIMIT 1",
                    (label,)).fetchone()
    c.close()
    if not row:
        print(f"{label}: no records"); return
    ts, ec, st, tail = row
    age_h = (time.time() - ts) / 3600
    print(f"{label}: {st} (exit {ec}) {age_h:.1f}h ago")
    if st == "error" and tail:
        print("--- tail ---\n" + tail)

def overdue(max_age_h, labels):
    """Prints labels that have NO successful run within the last max_age_h hours."""
    c = conn()
    cutoff = time.time() - float(max_age_h) * 3600
    out = []
    for label in labels:
        # skip labels with no history at all (never run under supervision yet) — only alert on regressions
        seen = c.execute("SELECT COUNT(*) FROM runs WHERE label=?", (label,)).fetchone()[0]
        if not seen:
            continue
        row = c.execute("SELECT MAX(ts) FROM runs WHERE label=? AND status='ok'", (label,)).fetchone()
        last_ok = row[0] if row and row[0] else 0
        if last_ok < cutoff:
            age = "errors only" if not last_ok else f"{(time.time()-last_ok)/3600:.0f}h ago"
            out.append(f"{label} (last success: {age})")
    c.close()
    print("\n".join(out))

def failing(labels=None):
    """Prints labels whose LAST run failed — regardless of window.

    Added after a real incident: `overdue` only alerts once there's been no success for
    longer than the window (26h for daily jobs). A single failure of a critical job
    produced exactly one message at the moment it failed — then silence until the window
    expired. A backup job sat red for 10 hours this way before anyone noticed. This looks
    at current state, not staleness of the last success: red is red, right now.
    """
    c = conn()
    known = [r[0] for r in c.execute("SELECT DISTINCT label FROM runs").fetchall()]
    targets = labels if labels else known
    out = []
    for label in targets:
        if label in RETIRED:
            continue
        row = c.execute("SELECT ts, exit_code, status, tail FROM runs WHERE label=? ORDER BY ts DESC LIMIT 1",
                        (label,)).fetchone()
        if not row or row[2] != "error":
            continue
        ts, ec, _, tail = row
        age_h = (time.time() - ts) / 3600
        line = f"{label}: exit {ec}, {age_h:.1f}h ago"
        if tail:
            last_lines = [l for l in tail.strip().splitlines() if l.strip()][-3:]
            if last_lines:
                line += "\n    " + "\n    ".join(l[:160] for l in last_lines)
        out.append(line)
    c.close()
    print("\n".join(out))

def record_verdict(label, verdict, passed, reason):
    passed_val = None
    if passed in ("true", "True", "1", 1, True):
        passed_val = 1
    elif passed in ("false", "False", "0", 0, False):
        passed_val = 0
    c = conn()
    c.execute("INSERT INTO verdicts(label, ts, verdict, passed, reason) VALUES(?,?,?,?,?)",
              (label, int(time.time()), verdict, passed_val, reason))
    c.commit(); c.close()

def last_verdict(label):
    c = conn()
    row = c.execute("SELECT ts, verdict, passed, reason FROM verdicts WHERE label=? ORDER BY ts DESC LIMIT 1",
                    (label,)).fetchone()
    c.close()
    if not row:
        print(f"{label}: no judge verdicts"); return
    ts, verdict, passed, reason = row
    age_h = (time.time() - ts) / 3600
    print(f"{label}: judge={verdict} passed={passed} {age_h:.1f}h ago — {reason}")

# Labels taken off the schedule. Run history stays in the database — erasing it would be
# dishonest — but showing "error N hours ago" for something intentionally retired is
# exactly the false signal this file exists to prevent. Mark it, don't hide it.
RETIRED = {
    "nightly-worker":           "retired by the operator — see the harness's own changelog",
    "nightly-worker-briefing":  "retired together with nightly-worker",
    "design-audit":             "retired by the operator",
}


def summary():
    c = conn()
    labels = [r[0] for r in c.execute("SELECT DISTINCT label FROM runs ORDER BY label")]
    for label in labels:
        ts, ec, st = c.execute("SELECT ts, exit_code, status FROM runs WHERE label=? ORDER BY ts DESC LIMIT 1",
                               (label,)).fetchone()
        if label in RETIRED:
            print(f"{label:28} {'retired':7} {(time.time()-ts)/3600:6.1f}h ago  ({RETIRED[label]})")
            continue
        verdict = c.execute(
            "SELECT verdict, passed, ts FROM verdicts WHERE label=? ORDER BY ts DESC, rowid DESC LIMIT 1",
            (label,),
        ).fetchone()
        # We don't change the runs schema or the summary format: a negative verdict just
        # turns the *displayed* effective status red. We check both verdict and passed:
        # older rows could hold an inconsistent pair. An old failure must not "poison" a
        # newer run that has no verdict yet. Equal timestamps are allowed on purpose:
        # status.py stores whole seconds, and a run and its verdict are usually written
        # within the same second.
        verdict_belongs_to_latest_run = verdict and verdict[2] >= ts
        if verdict_belongs_to_latest_run and (verdict[0] == "failed" or verdict[1] == 0):
            st = "error"
        n_run_err = c.execute(
            "SELECT COUNT(*) FROM runs WHERE label=? AND status='error'", (label,)
        ).fetchone()[0]
        n_verdict_err = c.execute(
            "SELECT COUNT(*) FROM verdicts WHERE label=? AND (verdict='failed' OR passed=0)",
            (label,),
        ).fetchone()[0]
        print(
            f"{label:28} {st:6} {(time.time()-ts)/3600:6.1f}h ago  "
            f"(errors/failed checks: {n_run_err + n_verdict_err})"
        )
    c.close()

if __name__ == "__main__":
    a = sys.argv[1:]
    if not a: print(__doc__); sys.exit(1)
    cmd = a[0]
    if cmd == "record": record(a[1], a[2], a[3] if len(a) > 3 else None)
    elif cmd == "last": last(a[1])
    elif cmd == "overdue": overdue(a[1], a[2:])
    elif cmd == "summary": summary()
    elif cmd == "record-verdict": record_verdict(a[1], a[2], a[3], a[4] if len(a) > 4 else "")
    elif cmd == "last-verdict": last_verdict(a[1])
    elif cmd == "failing": failing(a[1:] if len(a) > 1 else None)
    else: print(__doc__); sys.exit(1)
