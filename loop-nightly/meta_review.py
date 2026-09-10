#!/usr/bin/env python3
"""Meta-review of the nightly worker — once a week, looks NOT at the product code but at
the PROCESS of loop-nightly itself over the last 7 days: run statuses, triggering-eval
warnings, common reasons for escalation/failure -> a short verdict on "what to improve in
the harness" via a local model ($0) -> a report + Telegram. Never fixes anything
automatically — it only reports; reacting is a human's job. Meant to run via loop-run.sh
(so it's supervised too)."""
import json, sqlite3, sys, time, urllib.request, os
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import policy

NOTIFY = HERE.parent / "loop-status" / "notify.py"
REPORT = HERE / "meta-review-last.md"
WINDOW_DAYS = 7


def collect(days=WINDOW_DAYS):
    """Summary of runs over the window: statuses, triggering-eval warnings, reasons from notes."""
    cutoff = int(time.time()) - days * 86400
    c = sqlite3.connect(policy.DB_PATH)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT id,night,status,pr_url,note,started,finished FROM runs WHERE started>=? ORDER BY started",
        (cutoff,),
    ).fetchall()
    c.close()

    by_status, te_warn, reasons, durations = {}, [], [], []
    tech_errors = {}   # technical cause from stderr: signature -> how many runs it affected
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        note = r["note"] or ""
        for line in note.splitlines():
            if "triggering-eval: ⚠️" in line:
                te_warn.append(f"run{r['id']}: {line.split('⚠️',1)[-1].strip()}")
            if r["status"] in ("escalated", "failed", "timeout") and any(
                k in line.lower() for k in ("made no changes", "red", "timed out", "failed", "not created", "worktree")
            ):
                reasons.append(f"run{r['id']}[{r['status']}]: {line.strip()[:120]}")
            # An infrastructure failure (a broken CLI, an expired token, a 4xx/5xx from a
            # provider) looks in the status log exactly like "a bad task". Pull it out
            # separately, or the verdict drifts toward "improve the prompt" when the real
            # fix is the tooling.
            for sig in ("requires a newer version", "invalid_request_error", "invalid_token",
                        "AuthRequired", "rate limit", "insufficient_quota",
                        "Transport channel closed", "command not found", "No such file"):
                if sig.lower() in line.lower():
                    tech_errors[sig] = tech_errors.get(sig, 0) + 1
        if r["finished"] and r["started"]:
            durations.append(r["finished"] - r["started"])

    return {
        "total": len(rows),
        "by_status": by_status,
        "te_warn": te_warn,
        "reasons": reasons[:15],
        "tech_errors": tech_errors,
        "avg_dur_min": round(sum(durations) / len(durations) / 60, 1) if durations else None,
    }


def verdict_local_model(summary: dict) -> str:
    """A short "what to improve in the harness" verdict from a cheap local model ($0)."""
    prompt = (
        "You are an autonomous-agent engineer. Below is a week's summary of a nightly "
        "worker (it takes tasks from a backlog, a coding agent writes the code, a local "
        "model judges it, a PR is opened). Give a SHORT verdict (3-5 lines): what's worth "
        "improving in the PROCESS/harness itself (prompt, task filter, gates) — not in the "
        "product code. If everything looks normal, say so. "
        "IMPORTANT: if tech_errors is non-empty, that's an infrastructure failure (a broken "
        "CLI, an expired token, a provider outage). The cause is then NOT prompts or bad "
        "tasks: name the tooling fix first and don't speculate about prompt quality. "
        f"Conclusions only, no fluff.\n\nSUMMARY:\n{json.dumps(summary, ensure_ascii=False, indent=2)}"
    )
    body = json.dumps({"model": "qwen2.5:7b", "prompt": prompt, "stream": False}).encode()
    try:
        req = urllib.request.Request(os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate"),
                                     data=body, headers={"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=120))
        return (r.get("response") or "").strip() or "(empty response)"
    except Exception as e:
        return f"(local model unavailable: {e}) — see the summary above, review by hand."


def main():
    s = collect()
    if s["total"] == 0:
        # An earlier version of this exited silently here — a downstream check then saw
        # no artifact and flagged the label red, even though the run behaved correctly.
        # An empty week is still a result, and deserves a report.
        report = ("# 🔭 Nightly-worker meta-review (week)\n\n"
                  "_no runs in the last 7 days — nothing to review._\n\n"
                  "This is a normal outcome, not a failure: the nightly worker simply didn't "
                  "run this week.\nIf that repeats week after week, check whether anything is "
                  "still filling the task queue.\n")
        REPORT.write_text(report, encoding="utf-8")
        print(report)
        print(f"\n[artifact] meta-review report written: {REPORT} ({REPORT.stat().st_size} bytes)")
        return 0

    v = verdict_local_model(s)
    statuses = ", ".join(f"{k}:{n}" for k, n in sorted(s["by_status"].items()))
    lines = [
        "# 🔭 Nightly-worker meta-review (week)",
        f"_runs: {s['total']} · {statuses} · avg duration: {s['avg_dur_min']} min_",
        "",
    ]
    if s["te_warn"]:
        lines += ["## ⚠️ triggering-eval warnings:"] + [f"- {w}" for w in s["te_warn"]] + [""]
    if s["tech_errors"]:
        lines += ["## 🔧 Infrastructure failures (fix the tooling, not the prompts):"] + [
            f"- `{sig}` — affected runs: {n}" for sig, n in sorted(
                s["tech_errors"].items(), key=lambda kv: -kv[1])] + [""]
    if s["reasons"]:
        lines += ["## Common reasons for escalation/failure:"] + [f"- {r}" for r in s["reasons"]] + [""]
    lines += ["## 🧠 Verdict (what to improve in the harness):", v]
    report = "\n".join(lines)

    REPORT.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[artifact] meta-review report written: {REPORT} ({REPORT.stat().st_size} bytes)")
    try:
        import subprocess
        subprocess.run([sys.executable, str(NOTIFY), report[:3500]], timeout=60)
    except Exception as e:
        print(f"notify error: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
