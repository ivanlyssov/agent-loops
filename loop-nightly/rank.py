#!/usr/bin/env python3
"""Filters and scores the night's candidates: red ones go to skipped, the top-N go to selected."""
import argparse, datetime as dt, json, re, urllib.request

import db, policy

SPEC_PROMPT = """Backlog idea for the repo "{project}":
{text}

Decide whether this can be turned into code overnight, with no human involved.

actionable = true if it's clear WHAT to change: a text/docs/README edit, a styling or
layout fix, adding a test, a rename, fixing one specific bug.
actionable = false if it's research, a discussion, adopting a new system, picking a
tool, a task spanning multiple repos, or something that needs a human decision.
actionable = false ALSO if the task is big: rewriting/building a project from scratch,
"get it into a working state", a large refactor, a migration, a new subsystem.
Overnight work is a small edit (up to ~8 files), not a project.

Return ONLY JSON, no markdown, no explanation:
{{"actionable": true, "spec": "exactly what to change and where, 1-2 sentences"}}
or
{{"actionable": false, "spec": ""}}"""

CONCRETE_VERBS = ("add", "fix", "write a test", "remove", "rename")
VAGUE_WORDS = ("improve", "optimize", "make better", "figure out")
FILE_EXT_RE = re.compile(r"\.(py|ts|tsx|md|sh)\b", re.I)


def red_reason(row) -> str:
    """The reason for the red filter, or an empty string."""
    text = (row["text"] or "").lower()
    for word in policy.RED_WORDS:
        if word.lower() in text:
            return f"red-word:{word}"
    if row["project"] not in policy.REPO_MAP:
        return "no-repo"
    if policy.P0_DOCS_TESTS_ONLY and not any(w.lower() in text for w in policy.P0_GREEN_WORDS):
        return "p0-scope"
    return ""


def spec_gate(row) -> tuple[str, str]:
    """Idea -> an actionable spec via a local model ($0). Returns (text, rejection reason).
    A non-empty reason sends the candidate to skipped."""
    if not policy.SPEC_GATE_ENABLED:
        return row["text"], ""
    prompt = SPEC_PROMPT.format(project=row["project"], text=(row["text"] or "")[:1200])
    try:
        req = urllib.request.Request(
            policy.SPEC_GATE_URL,
            data=json.dumps({"model": policy.SPEC_GATE_MODEL, "prompt": prompt,
                             "stream": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=policy.SPEC_GATE_TIMEOUT) as r:
            out = json.loads(r.read())["response"].strip()
    except Exception as e:
        # Fail loud: a silent pass-through here means a stream of junk PRs
        return row["text"], f"spec-gate-unavailable:{type(e).__name__}"
    m = re.search(r"\{.*\}", out, re.S)          # the model likes to wrap JSON in prose
    if not m:
        return row["text"], "spec-gate-bad-json"
    try:
        d = json.loads(m.group(0))
    except Exception:
        return row["text"], "spec-gate-bad-json"
    spec = (d.get("spec") or "").strip()
    if not d.get("actionable") or len(spec) < 15:
        return row["text"], "not-actionable"
    return spec[:600], ""


def is_concrete(text: str) -> bool:
    """Concrete: a filename/extension, or an explicit action verb."""
    low = text.lower()
    return bool(FILE_EXT_RE.search(text)) or any(v in low for v in CONCRETE_VERBS)


def is_vague(text: str) -> bool:
    """Fluff: too few words, or only generic phrasing."""
    low = text.lower()
    words = re.findall(r"\w+", low)
    return len(words) < 6 or any(w in low for w in VAGUE_WORDS)


def age_score(night: str, current: str) -> int:
    """Candidate age in days, capped by a policy constant."""
    try:
        first = dt.date.fromisoformat(night)
        now = dt.date.fromisoformat(current)
    except Exception:
        return 0
    return max(0, min(policy.SCORE_AGE_MAX, (now - first).days))


def score(row, current_night: str) -> int:
    """Score a green candidate."""
    total = 0
    text = row["text"] or ""
    if row["source"] == "idea-inbox":
        total += policy.SCORE_IDEA_INBOX
    if policy.repo_has_ci(row["project"]):
        total += policy.SCORE_HAS_CI
    if is_concrete(text):
        total += policy.SCORE_CONCRETE
    if is_vague(text):
        total += policy.SCORE_VAGUE
    total += age_score(row["night"], current_night)
    return total


def ranked_rows():
    """Every pending candidate for tonight, with its future status attached."""
    con = db.connect()
    night = db.tonight()
    rows = con.execute(
        "SELECT * FROM candidates WHERE night=? AND status='pending' ORDER BY id",
        (night,),
    ).fetchall()
    out, green = [], []
    for row in rows:
        reason = red_reason(row)
        item = dict(row)
        if not reason:
            # passed the red filter — now turn the idea into a spec (or reject it)
            spec, reason = spec_gate(row)
            item["text"] = spec
        if reason:
            item.update(score=0, green=0, status="skipped", reason=reason)
        else:
            item.update(score=score(item, night), green=1, status="skipped", reason="not-top")
            green.append(item)
        out.append(item)

    top_ids = {r["id"] for r in sorted(green, key=lambda r: (-r["score"], r["id"]))[:policy.MAX_TASKS_PER_NIGHT]}
    for item in out:
        if item["id"] in top_ids:
            item["status"] = "selected"
            item["reason"] = ""
    return out


def print_table(rows: list[dict]) -> None:
    for r in rows:
        print(f"{r['score']} | {r['green']} | {r['status']} | {r['reason']} | {r['text'][:70]}")


def write(rows: list[dict]) -> None:
    con = db.connect()
    with con:
        for r in rows:
            # text is saved back too: spec_gate rewrote the idea into a spec, and that's
            # exactly what the executor should see — not the original vague phrasing
            con.execute(
                "UPDATE candidates SET score=?, green=?, status=?, reason=?, text=? WHERE id=?",
                (r["score"], r["green"], r["status"], r["reason"], r["text"], r["id"]),
            )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    rows = ranked_rows()
    if args.dry:
        print_table(rows)
    else:
        write(rows)
        print(f"selected {sum(r['status'] == 'selected' for r in rows)}, "
              f"skipped {sum(r['status'] == 'skipped' for r in rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
