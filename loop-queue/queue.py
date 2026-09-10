#!/usr/bin/env python3
"""Batch approval queue (P1). Loops drop outward-facing actions here; a human approves
or rejects them, typically once a day. Storage: SQLite queue.db. Executing an approved
item runs its command as an argv LIST — never through a shell (no injection): the
`command` field is a JSON array of arguments, e.g. ["python3","publish.py","--id","7"].
Need a pipe/shell? Wrap it in a script file and queue the path to that script instead.

CLI:
  queue.py add --kind K --title T --summary S --argv PROG ARG... [--risk low|high]  — enqueue (prints id)
  queue.py pending                  — list pending items
  queue.py decide <id> approve|reject
  queue.py worker                   — execute every approved item (-> done/failed)
  queue.py expire [days]            — expire old pending items (default 3 days)
  queue.py get <id>                 — JSON for one item
"""
import sys, sqlite3, time, json, subprocess, argparse
from pathlib import Path

DB = Path(__file__).resolve().parent / "queue.db"

def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS proposals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER, kind TEXT, title TEXT,
        summary TEXT, command TEXT, risk TEXT DEFAULT 'low', status TEXT DEFAULT 'pending',
        decided INTEGER, msg_id INTEGER, result TEXT)""")
    return c

def add(kind, title, summary, argv, risk="low"):
    """argv — a list of arguments (executed without a shell). Stored as a JSON array."""
    if not isinstance(argv, list) or not argv:
        raise ValueError("argv must be a non-empty list")
    c = conn()
    cur = c.execute("INSERT INTO proposals(created,kind,title,summary,command,risk,status) VALUES(?,?,?,?,?,?, 'pending')",
                    (int(time.time()), kind, title, summary, json.dumps(argv, ensure_ascii=False), risk))
    c.commit(); pid = cur.lastrowid; c.close(); return pid

def set_msg(pid, msg_id):
    c = conn(); c.execute("UPDATE proposals SET msg_id=? WHERE id=?", (msg_id, pid)); c.commit(); c.close()

def decide(pid, action):
    st = "approved" if action == "approve" else "rejected"
    c = conn(); c.execute("UPDATE proposals SET status=?, decided=? WHERE id=? AND status='pending'",
                          (st, int(time.time()), pid)); n = c.total_changes; c.commit(); c.close()
    return n > 0

def get(pid):
    c = conn(); r = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone(); c.close()
    return dict(r) if r else None

def pending():
    c = conn(); rows = [dict(r) for r in c.execute("SELECT * FROM proposals WHERE status='pending' ORDER BY risk DESC, created")]; c.close()
    return rows

def run_one(pid):
    """Execute one approved item. Returns (ok, output)."""
    p = get(pid)
    if not p or p["status"] != "approved":
        return False, "not approved"
    try:
        argv = json.loads(p["command"])
        if not isinstance(argv, list) or not argv:
            raise ValueError("command is not an argv list")
        r = subprocess.run(argv, capture_output=True, text=True, timeout=600)  # no shell — no injection
        ok = r.returncode == 0
        out = (r.stdout + r.stderr)[-1000:]
    except Exception as e:
        ok, out = False, str(e)
    c = conn(); c.execute("UPDATE proposals SET status=?, result=? WHERE id=?",
                          ("done" if ok else "failed", out, pid)); c.commit(); c.close()
    return ok, out

def worker():
    c = conn(); ids = [r["id"] for r in c.execute("SELECT id FROM proposals WHERE status='approved'")]; c.close()
    for pid in ids:
        ok, out = run_one(pid); print(f"#{pid}: {'done' if ok else 'FAILED'}")

def expire(days=3):
    cutoff = int(time.time()) - days*86400
    c = conn(); c.execute("UPDATE proposals SET status='expired' WHERE status='pending' AND created<?", (cutoff,))
    n = c.total_changes; c.commit(); c.close(); print(f"expired: {n}")

if __name__ == "__main__":
    a = sys.argv[1:]
    if not a: print(__doc__); sys.exit(1)
    if a[0] == "add":
        ap = argparse.ArgumentParser(); ap.add_argument("--kind", required=True); ap.add_argument("--title", required=True)
        ap.add_argument("--summary", default=""); ap.add_argument("--risk", default="low")
        ap.add_argument("--argv", required=True, nargs="+")
        n = ap.parse_args(a[1:]); print(add(n.kind, n.title, n.summary, n.argv, n.risk))
    elif a[0] == "pending":
        for p in pending(): print(f"#{p['id']} [{p['risk']}] {p['kind']}: {p['title']}")
    elif a[0] == "decide": print("ok" if decide(int(a[1]), a[2]) else "unchanged")
    elif a[0] == "worker": worker()
    elif a[0] == "expire": expire(int(a[1]) if len(a) > 1 else 3)
    elif a[0] == "get": print(json.dumps(get(int(a[1])), ensure_ascii=False, indent=1))
    else: print(__doc__); sys.exit(1)
