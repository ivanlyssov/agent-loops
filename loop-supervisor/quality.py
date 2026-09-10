#!/usr/bin/env python3
"""Quality meta-agent — the "teacher with a gradebook", the supervisor's second leg (P3).
Not "alive/dead" (that's supervisor.py) but "is it working WELL" over a period.

Layers (cheap -> judgment only on what was actually found):
  A (script, $0)   — trends from status.db: average grade-score this week vs. the prior
                     4 weeks, eval degradation, reject-rate in queue.db (approval fatigue).
  B (local model, $0) — a sample of up to 5 fresh weekly-synthesis artifacts, checked
                     cross-artifact for repetition/drift/usefulness.
  C (not here)     — if A/B find degradation, it surfaces 🔴/⚠️ in the supervisor's
                     report; a human (or a stronger model, on request) decides what to
                     change — never automatically.

Called from supervisor.py (weekly). Returns (red, warn, notes) in its format. Also runs
the weekly golden-set evals (loop-evals) — that's trigger #2 for that layer.
"""
import json, sqlite3, subprocess, sys, time, urllib.request, os
from pathlib import Path

HERE = Path(__file__).parent
MODEL = "qwen2.5:7b"
WEEK = 7 * 86400


def _rows(db, sql, args=()):
    if not Path(db).exists():
        return []
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def layer_a(status_db, queue_db):
    """DB trends. Returns (red, warn, notes)."""
    red, warn, notes = [], [], []
    now = int(time.time())
    # 1. grade scores: this week vs. the prior 4
    for (label,) in _rows(status_db, "SELECT DISTINCT label FROM grades"):
        cur = _rows(status_db, "SELECT AVG(score), COUNT(*) FROM grades WHERE label=? AND ts>?",
                    (label, now - WEEK))[0]
        prev = _rows(status_db, "SELECT AVG(score) FROM grades WHERE label=? AND ts BETWEEN ? AND ?",
                     (label, now - 5 * WEEK, now - WEEK))[0]
        if cur[0] is None:
            continue
        notes.append(f"quality {label}: {cur[0]:.2f} over 7d ({cur[1]} grades)")
        if prev[0] and cur[0] < prev[0] - 0.15:
            warn.append(f"📉 {label}: average score dropped {prev[0]:.2f}->{cur[0]:.2f} this week")
    # 2. evals: any degradation this week
    bad = _rows(status_db, "SELECT label, COUNT(*) FROM evals WHERE ts>? AND passed=0 GROUP BY label",
                (now - WEEK,))
    for label, n in bad:
        tot = _rows(status_db, "SELECT COUNT(*) FROM evals WHERE ts>? AND label=?", (now - WEEK, label))[0][0]
        if tot and n / tot > 0.25:
            red.append(f"🔴 evals {label}: {n}/{tot} cases failed this week — the filter is drifting")
    # 3. approval gate: human reject-rate (fall back across queue schema variants)
    q = _rows(queue_db, "SELECT status, COUNT(*) FROM queue GROUP BY status") or \
        _rows(queue_db, "SELECT status, COUNT(*) FROM items GROUP BY status") or \
        _rows(queue_db, "SELECT status, COUNT(*) FROM proposals GROUP BY status")
    d = dict(q)
    seen = d.get("approved", 0) + d.get("rejected", 0) + d.get("done", 0)
    if seen >= 10:
        rej = d.get("rejected", 0) / seen
        notes.append(f"gate: reject-rate {rej:.0%} ({seen} decisions total)")
        if rej > 0.5:
            warn.append(f"⚠️ gate: {rej:.0%} of proposals are being rejected — proposal quality is low")
    return red, warn, notes


def layer_b(synth_dir):
    """Local-model sample of fresh synthesis artifacts: repetition/drift. Advisory."""
    if not synth_dir.exists():
        return [], [], []
    now = time.time()
    fresh = sorted([p for p in synth_dir.glob("*.md") if now - p.stat().st_mtime < 5 * WEEK],
                   key=lambda p: p.stat().st_mtime)[-5:]
    if len(fresh) < 2:
        return [], [], []
    corpus = "\n\n===DOCUMENT===\n".join(p.read_text(encoding="utf-8", errors="replace")[:1500] for p in fresh)
    prompt = (
        "Below are several weekly knowledge-synthesis notes from one system (oldest to "
        "newest). Answer strictly JSON: {\"repetitive\": true|false, \"drift\": true|false, "
        "\"note\": \"one line: the main observation\"}. repetitive=true if the notes largely "
        "repeat each other. drift=true if the latest notes are noticeably less concrete/useful "
        "than the earlier ones.\n\n" + corpus)
    try:
        body = json.dumps({"model": MODEL, "stream": False, "format": "json",
                           "options": {"temperature": 0}, "prompt": prompt}).encode()
        req = urllib.request.Request(os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate"),
                                     data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            out = json.loads(json.loads(r.read()).get("response", "{}"))
    except Exception:
        return [], [], ["synthesis sample: local model unavailable, skipped"]
    warn, notes = [], [f"synthesis sample ({len(fresh)} notes): {out.get('note','')}"]
    if out.get("repetitive"):
        warn.append("⚠️ synthesis: weekly notes repeat each other — the feeds/filter are producing the same thing")
    if out.get("drift"):
        warn.append("⚠️ synthesis: recent notes are less concrete than earlier ones — the prompt/input is degrading")
    return [], warn, notes


def run_weekly_evals(evals_script):
    """Trigger #2 for loop-evals: run every golden-set weekly."""
    if not evals_script.exists():
        return [], [], []
    try:
        r = subprocess.run([sys.executable, str(evals_script), "--all"],
                           capture_output=True, text=True, timeout=900)
        tail = (r.stdout or "").strip().splitlines()[-3:]
        if r.returncode != 0:
            return [f"🔴 loop-evals: filter degradation (see alert): {' | '.join(tail)}"], [], []
        return [], [], [f"evals: {' | '.join(tail) or 'ok'}"]
    except Exception as e:
        return [], [f"⚠️ loop-evals did not complete: {e}"], []


def run(paths=None):
    if paths is None:
        # allow `python3 quality.py` standalone if AGENT_LOOPS_HOME is set
        sys.path.insert(0, str(HERE.parent / "common"))
        from config import deployment_root
        root = deployment_root()
        paths = {
            "status_db": root / "loop-status" / "status.db",
            "queue_db": root / "loop-queue" / "queue.db",
            "evals": root / "loop-evals" / "run_evals.py",
            "synth_dir": Path(os.environ.get("KNOWLEDGE_VAULT", root / "knowledge-vault")) / "weekly-synthesis",
        }
    red, warn, notes = [], [], []
    for r, w, n in (
        layer_a(paths["status_db"], paths["queue_db"]),
        run_weekly_evals(paths.get("evals", HERE.parent / "loop-evals" / "run_evals.py")),
        layer_b(paths.get("synth_dir", HERE / "weekly-synthesis")),
    ):
        red += r; warn += w; notes += n
    return red, warn, notes


if __name__ == "__main__":
    r, w, n = run()
    for x in r + w + n:
        print(x)
