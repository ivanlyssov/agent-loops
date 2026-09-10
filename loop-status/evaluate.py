#!/usr/bin/env python3
"""Judge (worker/evaluator) — checks not just the exit code, but "was the success
criterion actually met?" A cheap local model (Ollama) reads the criterion plus the run's
log tail and returns a verdict. This is an advisory layer: if Ollama is unavailable or
broken, the verdict is "skipped" (NOT a failure — the task already passed on exit code
via loop-run.sh).

CLI:
  evaluate.py <label> <criteria_text> <log_tail_file>
      prints JSON {"passed": bool|null, "reason": str, "verdict": "passed"|"failed"|"skipped"}
      always exits 0 (this is an advisor, not a gate) — the caller decides whether to alert.
"""
import sys, json, time, urllib.request

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:7b"
TIMEOUT_S = 120
MAX_TAIL_LINES = 80

PROMPT_TMPL = (
    "Check this task's log against its success criterion. Answer strictly in JSON.\n\n"
    "SUCCESS CRITERION: {criteria}\n\n"
    "LOG:\n---\n{tail}\n---\n\n"
    "Steps:\n"
    "1. Does the log contain a Traceback/Error/Exception (an explicit failure)? If so — passed=false.\n"
    "2. If there's no error — does the log's content match the criterion in substance "
    "(paraphrases count, it doesn't need to be a literal match)?\n\n"
    "JSON answer: {{\"passed\": true|false, \"reason\": \"one short sentence why\"}}"
)


def _tail_lines(text: str, n: int = MAX_TAIL_LINES) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def evaluate(label: str, criteria: str, tail: str) -> dict:
    prompt = PROMPT_TMPL.format(criteria=criteria.strip(), tail=_tail_lines(tail))
    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "options": {"num_ctx": 8192, "temperature": 0.1},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        raw = urllib.request.urlopen(req, timeout=TIMEOUT_S).read()
        resp = json.loads(raw).get("response", "").strip()
        parsed = json.loads(resp)
        passed = bool(parsed.get("passed"))
        reason = str(parsed.get("reason", "")).strip() or "(no reason given)"
        return {"verdict": "passed" if passed else "failed", "passed": passed, "reason": reason}
    except Exception as e:
        return {"verdict": "skipped", "passed": None, "reason": f"judge unavailable: {e}"}


def main():
    if len(sys.argv) < 4:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    label, criteria_arg, tail_file = sys.argv[1], sys.argv[2], sys.argv[3]

    # the criterion can be either literal text or a path to a file containing the text
    from pathlib import Path
    crit_path = Path(criteria_arg)
    criteria = crit_path.read_text(encoding="utf-8", errors="replace") if crit_path.exists() else criteria_arg

    try:
        tail = Path(tail_file).read_text(encoding="utf-8", errors="replace")
    except Exception:
        tail = ""

    result = evaluate(label, criteria, tail)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0)  # the judge is an advisor, never fails the caller


if __name__ == "__main__":
    main()
