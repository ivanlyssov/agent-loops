#!/usr/bin/env python3
"""nightly.db — the single source of truth for a night's work (schema below)."""
import sqlite3, time
from policy import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  night TEXT,                -- 'YYYY-MM-DD' of the night it was added
  source TEXT,               -- idea-inbox | ideas-md | supervisor
  source_ref TEXT,           -- idea:<id> | <file>#<n> — dedup key
  project TEXT,
  text TEXT,
  score REAL DEFAULT 0,
  green INTEGER DEFAULT 0,
  reason TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',   -- pending|selected|skipped
  UNIQUE(source, source_ref)
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  night TEXT,
  candidate_id INTEGER,
  branch TEXT, pr_url TEXT,
  plan_source TEXT DEFAULT 'self-planned',
  judge_verdict TEXT, safe INTEGER, ci TEXT,
  status TEXT DEFAULT 'building',  -- building|pr_open|merged|escalated|failed|timeout
  started INTEGER, finished INTEGER, note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_night ON runs(night, candidate_id);
"""

def connect():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c

def tonight() -> str:
    """Night = the date it started: before 12:00 counts as yesterday (a 23:00 run and an
    08:00 briefing belong to the same night)."""
    t = time.localtime(time.time() - 12 * 3600)
    return time.strftime("%Y-%m-%d", t)
