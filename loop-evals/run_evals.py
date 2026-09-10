#!/usr/bin/env python3
"""Triggering evals — an "exam" for a loop's filter/prompt, run on a frozen golden set.
Catches degradation BEFORE junk reaches production output: runs cases through the SAME
production code the loop actually uses (an imported function, not a copy of its prompt)
and compares the result against a recorded baseline accuracy.

CLI:
  run_evals.py <label>          — run one loop's exam (label = a key in ADAPTERS)
  run_evals.py --all            — every loop that has cases/<label>.jsonl
  run_evals.py <label> --set-baseline   — record the current result as the baseline (deliberately!)

Cases: cases/<label>.jsonl — one JSON object per line: {"id": "...", "input": "...", "expect": "keep"|"drop"}
Verdict: accuracy is compared against baselines.json; a drop below (baseline - tolerance)
is a degradation -> exit 1 + Telegram alert. Every case is recorded to status.db (table `evals`).
Local model unavailable -> exit 0, marked skipped (this exam is advisory, it never blocks a loop's own run).
Typical triggers: a git post-commit hook (loop code changed), weekly from quality.py, or by hand.
"""
import json, sys, time, sqlite3, subprocess, os
from pathlib import Path

HERE = Path(__file__).parent
CASES_DIR = HERE / "cases"
BASELINES = HERE / "baselines.json"
BASELINES_EXAMPLE = HERE / "baselines.example.json"
TOLERANCE = 0.10  # acceptable accuracy drop from the baseline

sys.path.insert(0, str(HERE.parent / "common"))
from config import optional_path  # noqa: E402


def _status_db():
    root = optional_path("AGENT_LOOPS_HOME")
    if root:
        return root / "loop-status" / "status.db"
    return HERE.parent / "loop-status" / "status.db"


def _notify_script():
    root = optional_path("AGENT_LOOPS_HOME")
    if root:
        return root / "loop-status" / "notify.py"
    return HERE.parent / "loop-status" / "notify.py"


def _db():
    conn = sqlite3.connect(_status_db())
    conn.execute("CREATE TABLE IF NOT EXISTS evals("
                 "label TEXT NOT NULL, ts INTEGER NOT NULL, case_id TEXT, "
                 "expected TEXT, got TEXT, passed INTEGER)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evals ON evals(label, ts)")
    return conn


def _notify(text):
    script = _notify_script()
    if not script.exists():
        return
    try:
        subprocess.run(["python3", str(script), text], timeout=30)
    except Exception:
        pass


# ---------- adapters: label -> function(cases) -> list[got: "keep"|"drop"] ----------

def eval_content_filter(cases):
    """Adapter for the bundled example filter — see examples/content_filter.py. Swap this
    for an import of your own loop's real filter function; the eval machinery doesn't care."""
    sys.path.insert(0, str(HERE / "examples"))
    import content_filter as cf
    posts = [(i, c["input"]) for i, c in enumerate(cases)]
    verdicts = cf.ollama_batch(posts, "qwen2.5:7b")
    if not verdicts:  # model unreachable
        return None
    return ["keep" if verdicts.get(i, {}).get("relevant") else "drop"
            for i in range(len(cases))]


ADAPTERS = {
    "content-filter": eval_content_filter,
    # add more as coverage grows, e.g.: "idea-triage": ..., "synthesis": ...
}


# ---------- run ----------

def run_label(label, set_baseline=False):
    cases_file = CASES_DIR / f"{label}.jsonl"
    if label not in ADAPTERS or not cases_file.exists():
        print(f"{label}: no adapter or no cases/{label}.jsonl")
        return "skipped"
    cases = [json.loads(l) for l in cases_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    got = ADAPTERS[label](cases)
    if got is None:
        print(f"{label}: local model unavailable — skipped")
        return "skipped"

    ts = int(time.time())
    conn = _db()
    ok = 0
    fails = []
    for c, g in zip(cases, got):
        passed = int(g == c["expect"])
        ok += passed
        if not passed:
            fails.append(f"  ✗ {c['id']}: expected {c['expect']}, got {g}")
        conn.execute("INSERT INTO evals VALUES(?,?,?,?,?,?)",
                     (label, ts, c["id"], c["expect"], g, passed))
    conn.commit(); conn.close()
    acc = ok / len(cases)
    print(f"{label}: accuracy {acc:.2f} ({ok}/{len(cases)})")
    for f in fails:
        print(f)

    baselines_path = BASELINES if BASELINES.exists() else BASELINES_EXAMPLE
    baselines = json.loads(baselines_path.read_text()) if baselines_path.exists() else {}
    if set_baseline:
        baselines[label] = round(acc, 3)
        BASELINES.write_text(json.dumps(baselines, indent=1) + "\n", encoding="utf-8")
        print(f"{label}: baseline recorded = {acc:.2f}")
        return "baseline"
    base = baselines.get(label)
    if base is None:
        print(f"{label}: no baseline set — record one: run_evals.py {label} --set-baseline")
        return "no-baseline"
    if acc < base - TOLERANCE:
        msg = (f"📉 loop-evals: filter \"{label}\" degraded: accuracy {acc:.2f} "
               f"(baseline {base:.2f}). {len(fails)} cases failed. "
               f"Check recent changes to the loop's prompt/code.")
        print(msg)
        _notify(msg)
        return "degraded"
    return "ok"


def main():
    args = [a for a in sys.argv[1:]]
    set_base = "--set-baseline" in args
    args = [a for a in args if a != "--set-baseline"]
    if not args:
        print(__doc__); sys.exit(2)
    labels = ([p.stem for p in CASES_DIR.glob("*.jsonl")] if args[0] == "--all" else [args[0]])
    worst = "ok"
    for lb in labels:
        r = run_label(lb, set_baseline=set_base)
        if r == "degraded":
            worst = "degraded"
    sys.exit(1 if worst == "degraded" else 0)


if __name__ == "__main__":
    main()
