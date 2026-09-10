#!/usr/bin/env python3
"""Self-grading — the harness's "mirror": grades the QUALITY of an artifact (not the log,
that's evaluate.py's job) against a rubric in rubrics/<label>.json, right in the run.
Ollama (a small local model), $0, temperature 0.

A rubric is 3-5 ATOMIC binary sub-criteria with weights (a small local model is reliable
on yes/no, unreliable on "rate this overall"). Score = weighted sum in 0..1.

CLI:
  grade.py <label> <artifact_file> [...]      — grade, print JSON, record to status.db
  grade.py <label> <artifact_file> --gate     — same, plus exit 1 if score < the rubric's threshold
                                                (for a hard outward-facing gate, e.g. before
                                                publishing generated content)
Ollama unavailable / no rubric → verdict "skipped", exit 0 (fail-open, advisory).
"""
import json, sys, time, sqlite3, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
RUBRICS = HERE / "rubrics"
DB = HERE / "status.db"
OLLAMA = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:7b"
MAX_CHARS = 6000


def _db():
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS grades("
                 "label TEXT NOT NULL, ts INTEGER NOT NULL, artifact TEXT, "
                 "score REAL, rubric_json TEXT, reason TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grades ON grades(label, ts)")
    return conn


def _ollama(prompt):
    body = json.dumps({"model": MODEL, "stream": False, "format": "json",
                       "options": {"temperature": 0}, "prompt": prompt}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(json.loads(r.read()).get("response", "{}"))


def grade(label, path):
    rub_file = RUBRICS / f"{label}.json"
    if not rub_file.exists():
        return {"verdict": "skipped", "reason": f"no rubric at rubrics/{label}.json"}
    rub = json.loads(rub_file.read_text(encoding="utf-8"))
    text = Path(path).read_text(encoding="utf-8", errors="replace")[:MAX_CHARS]
    crits = rub["criteria"]  # [{"id","q","weight"}]
    qs = "\n".join(f'- "{c["id"]}": {c["q"]}' for c in crits)
    prompt = (
        "Grade this artifact against atomic criteria. Answer EACH one strictly true or false.\n"
        f"Context: {rub.get('context','')}\n\nCriteria:\n{qs}\n\n"
        "ARTIFACT:\n---\n" + text + "\n---\n\n"
        'JSON answer: {"answers": {"<id>": true|false, ...}, "reason": "one line: main flaw, or \'ok\'"}'
    )
    try:
        out = _ollama(prompt)
        ans = out.get("answers", {})
        total = sum(c["weight"] for c in crits)
        score = sum(c["weight"] for c in crits if ans.get(c["id"]) is True) / total
        res = {"verdict": "graded", "score": round(score, 3),
               "threshold": rub.get("threshold", 0.6),
               "answers": ans, "reason": out.get("reason", "")}
    except Exception as e:
        return {"verdict": "skipped", "reason": f"ollama: {e}"}
    conn = _db()
    conn.execute("INSERT INTO grades VALUES(?,?,?,?,?,?)",
                 (label, int(time.time()), str(path), res["score"],
                  json.dumps(res["answers"], ensure_ascii=False), res["reason"]))
    conn.commit(); conn.close()
    return res


def main():
    args = sys.argv[1:]
    gate = "--gate" in args
    args = [a for a in args if a != "--gate"]
    if len(args) < 2:
        print(__doc__); sys.exit(2)
    label, files = args[0], args[1:]
    worst_fail = False
    for f in files:
        res = grade(label, f)
        print(json.dumps({"artifact": f, **res}, ensure_ascii=False))
        if res.get("verdict") == "graded" and res["score"] < res["threshold"]:
            worst_fail = True
    sys.exit(1 if (gate and worst_fail) else 0)


if __name__ == "__main__":
    main()
