#!/usr/bin/env python3
"""Unified cost ledger for AI task calls — loop-status/status.db (table `costs`).
Any channel (a coding agent, a local model, an API model, ...) logs the ACTUAL cost of
a call here (not an estimate). Basis for a "what actually cost money vs. what was $0
(flat subscription / local)" view."""
import argparse, json, sqlite3, sys, time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "status.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS costs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  channel TEXT NOT NULL,        -- e.g. codex|ollama|openrouter|claude|...
  project TEXT DEFAULT '',
  task_ref TEXT DEFAULT '',     -- e.g. nightly:42, idea-build:71, cli
  model TEXT DEFAULT '',
  tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0,
  usd REAL DEFAULT 0,
  note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_costs_task ON costs(task_ref);
CREATE INDEX IF NOT EXISTS idx_costs_channel_ts ON costs(channel, ts);
"""


def connect():
    c = sqlite3.connect(DB_PATH)
    c.executescript(SCHEMA)
    return c


def log_cost(channel, project="", task_ref="", model="", tokens_in=0, tokens_out=0, usd=0.0, note=""):
    """Record the actual cost of one call. usd=0 for flat subscriptions and local
    channels — that's an honest $0, not "not measured"."""
    c = connect()
    c.execute(
        "INSERT INTO costs(ts,channel,project,task_ref,model,tokens_in,tokens_out,usd,note) VALUES(?,?,?,?,?,?,?,?,?)",
        (int(time.time()), channel, project, task_ref, model, tokens_in, tokens_out, usd, note),
    )
    c.commit(); c.close()


def summary(days=30):
    """Per-channel totals for the last N days — feeds a cost dashboard."""
    c = connect()
    cutoff = int(time.time()) - days * 86400
    rows = c.execute(
        """SELECT channel, COUNT(*) n, SUM(usd) usd, SUM(tokens_in) ti, SUM(tokens_out) tout
           FROM costs WHERE ts>=? GROUP BY channel ORDER BY usd DESC""",
        (cutoff,),
    ).fetchall()
    c.close()
    return [
        {"channel": r[0], "calls": r[1], "usd": round(r[2] or 0, 4), "tokens_in": r[3] or 0, "tokens_out": r[4] or 0}
        for r in rows
    ]


def by_task(task_ref):
    """All cost rows for one task (e.g. nightly:42) — for "cost per task"."""
    c = connect()
    rows = c.execute(
        "SELECT channel, model, usd, tokens_in, tokens_out, note FROM costs WHERE task_ref=? ORDER BY ts",
        (task_ref,),
    ).fetchall()
    c.close()
    return [{"channel": r[0], "model": r[1], "usd": r[2], "tokens_in": r[3], "tokens_out": r[4], "note": r[5]} for r in rows]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AI task cost ledger (loop-status/status.db).")
    ap.add_argument("cmd", choices=["add", "summary", "task"])
    ap.add_argument("--channel", default="")
    ap.add_argument("--project", default="")
    ap.add_argument("--task_ref", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--tokens_in", type=int, default=0)
    ap.add_argument("--tokens_out", type=int, default=0)
    ap.add_argument("--usd", type=float, default=0.0)
    ap.add_argument("--note", default="")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    if a.cmd == "add":
        if not a.channel:
            print("missing --channel", file=sys.stderr); sys.exit(2)
        log_cost(a.channel, a.project, a.task_ref, a.model, a.tokens_in, a.tokens_out, a.usd, a.note)
        print("ok")
    elif a.cmd == "summary":
        print(json.dumps(summary(a.days), ensure_ascii=False, indent=2))
    else:
        if not a.task_ref:
            print("missing --task_ref", file=sys.stderr); sys.exit(2)
        print(json.dumps(by_task(a.task_ref), ensure_ascii=False, indent=2))
