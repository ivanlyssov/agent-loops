#!/usr/bin/env python3
"""Executes ONE nightly task: worktree -> coding agent -> tests -> judge -> PR -> (merge
gate, behind a flag). CLI: run_task.py --once <candidate_id> — called by nightly.sh for
each selected candidate.
Safety: is_safe() (diffs) from repo_safety.py — the only implementation of that check;
the judge is loop-status/evaluate.py; with AUTOMERGE_ENABLED=False (the default) this
always stops after opening the PR."""
import json, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import db, policy
from policy import REPO_MAP, is_safe, wait_ci

LOOP_STATUS = HERE.parent / "loop-status"
sys.path.insert(0, str(LOOP_STATUS))
import cost as cost_ledger


def sh(args, cwd=None, timeout=600):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def log(run_note: list, msg: str):
    print(msg, flush=True)
    run_note.append(msg)


def detect_and_run_tests(wt: Path, note: list) -> bool:
    """Runs the repo's own tests, if it has any. No tests found is not a failure (True)."""
    try:
        pkg = wt / "package.json"
        if pkg.exists() and '"test"' in pkg.read_text(encoding="utf-8", errors="ignore"):
            if not (wt / "node_modules").exists():   # fresh worktree — install deps first
                cmd = ["npm", "ci"] if (wt / "package-lock.json").exists() else ["npm", "install"]
                r = sh(cmd + ["--no-audit", "--no-fund"], cwd=wt, timeout=900)
                log(note, f"{' '.join(cmd)}: exit={r.returncode}")
                if r.returncode != 0:
                    return False
            r = sh(["npm", "test", "--silent"], cwd=wt, timeout=900)
            log(note, f"npm test: exit={r.returncode}")
            return r.returncode == 0
        if (wt / "pytest.ini").exists() or list(wt.glob("tests/test_*.py")):
            r = sh(["python3", "-m", "pytest", "-q"], cwd=wt, timeout=900)
            log(note, f"pytest: exit={r.returncode}")
            return r.returncode == 0
    except subprocess.TimeoutExpired:
        log(note, "tests: timed out")
        return False
    log(note, "no tests found in the repo — skipping")
    return True


def judge(label: str, criteria: str, log_text: str, note: list) -> str:
    """The judge via loop-status/evaluate.py -> passed|failed|skipped."""
    try:
        tf = Path(f"/tmp/nightly-judge-{label}.log")
        tf.write_text(log_text[-8000:], encoding="utf-8")
        r = sh(["python3", str(LOOP_STATUS / "evaluate.py"), label, criteria, str(tf)], timeout=180)
        verdict = json.loads(r.stdout.strip() or "{}").get("verdict", "skipped")
    except Exception as e:
        log(note, f"judge unavailable ({e}) -> skipped")
        verdict = "skipped"
    log(note, f"judge: {verdict}")
    return verdict


def run_one(cand_id: int) -> str:
    c = db.connect()
    cand = c.execute("SELECT * FROM candidates WHERE id=?", (cand_id,)).fetchone()
    if not cand:
        print(f"candidate {cand_id} not found"); return "failed"
    night = db.tonight()
    if c.execute("SELECT 1 FROM runs WHERE night=? AND candidate_id=?", (night, cand_id)).fetchone():
        print(f"candidate {cand_id} already ran tonight — skip"); return "dup"

    started = int(time.time())
    cur = c.execute("INSERT INTO runs(night, candidate_id, started) VALUES(?,?,?)",
                    (night, cand_id, started))
    run_id = cur.lastrowid
    c.commit()
    note: list = []

    repo = policy.root() / REPO_MAP[cand["project"]]
    branch = f"nightly/{run_id}-{started}"
    wt = policy.HERE / "worktrees" / branch.replace("/", "-")
    wt.parent.mkdir(exist_ok=True)
    status, pr_url, verdict, safe_flag, ci_res = "failed", "", "", None, ""

    def finish():
        # triggering-eval: a binary check that the loop actually did the expected thing,
        # not just recorded a status (catches a "silent failure").
        te = []
        if status in ("pr_open", "merged") and not pr_url:
            te.append(f"status={status}, but the PR url is empty")
        if status == "merged" and not policy.AUTOMERGE_ENABLED:
            te.append("status=merged while auto-merge is disabled (shouldn't happen at P0)")
        if status == "pr_open" and not branch:
            te.append("pr_open with no branch")
        log(note, "triggering-eval: " + ("⚠️ " + "; ".join(te) if te else "✅ artifacts are in place"))
        c.execute("""UPDATE runs SET branch=?, pr_url=?, judge_verdict=?, safe=?, ci=?,
                     status=?, finished=?, note=? WHERE id=?""",
                  (branch, pr_url, verdict, safe_flag, ci_res, status,
                   int(time.time()), "\n".join(note)[-4000:], run_id))
        c.commit()
        # the full task log — for the morning briefing / diagnosis
        policy.LOGS_DIR.mkdir(exist_ok=True)
        (policy.LOGS_DIR / f"{night}-run{run_id}.log").write_text("\n".join(note), encoding="utf-8")

    try:
        log(note, f"task #{cand_id} [{cand['project']}]: {cand['text'][:120]}")
        sh(["git", "fetch", "origin"], cwd=repo, timeout=120)
        r = sh(["git", "worktree", "add", "-b", branch, str(wt), "origin/main"], cwd=repo)
        if r.returncode != 0:
            log(note, f"worktree not created: {r.stderr[-300:]}"); finish(); return status

        # --- the coding agent executes (P0: self-planned; a stronger model could plan first) ---
        ui_task = any(w in cand["text"].lower() for w in
                      ("page", "screen", "form", "component", "design", "layout",
                       "ui", "landing", "css", "style"))
        design_gate = (
            "\nDESIGN GATE (this task touches UI): FIRST create an HTML mock at "
            "docs/design/<feature>.html (self-contained, inline CSS following the repo's "
            "design tokens if it has any, otherwise match the current styles), THEN write "
            "code exactly to that mock. Commit the mock together with the code — it gets "
            "reviewed in the morning." if ui_task else ""
        )
        task_brief = (
            f"Task from the overnight backlog: {cand['text']}\n"
            f"Rules: minimal diff (YAGNI, <=8 files), don't touch .env/secrets/migrations/"
            f".github/workflows, follow the repo's own conventions (read its own agent "
            f"instructions file if it has one). Run the repo's tests at the end, if it has any. "
            f"SELF-CHECK before finishing: re-read your own diff and confirm it does EXACTLY "
            f"the stated task — not more, not less; fix it before exiting if it drifted or "
            f"left stubs/TODOs behind.{design_gate}"
        )
        msg_f = Path(f"/tmp/nightly-{run_id}-agent.txt")
        try:
            r = sh(["codex", "exec", "-C", str(wt), "-s", "workspace-write",
                    "-c", 'approval_policy="never"', "--skip-git-repo-check",
                    "--output-last-message", str(msg_f), task_brief],
                   cwd=wt, timeout=policy.TASK_TIMEOUT_MIN * 60 - 600)
        except subprocess.TimeoutExpired:
            log(note, "coding agent: timed out"); status = "timeout"; finish(); return status
        agent_out = msg_f.read_text(encoding="utf-8", errors="ignore") if msg_f.exists() else ""
        log(note, f"agent: exit={r.returncode}; output: {agent_out[:400]}")
        if r.returncode != 0:
            # Without this, a failed run showed up as a bare "exit=1" with an empty
            # summary and the real cause was gone for good. stderr was already captured —
            # it just wasn't printed.
            err = (r.stderr or "").strip() or "(the agent wrote nothing to stderr)"
            log(note, f"agent stderr: {err[-1200:]}")
        cost_ledger.log_cost("coding-agent", project=cand["project"], task_ref=f"nightly:{run_id}",
                              note="flat subscription — no per-call marginal cost")

        diff = sh(["git", "diff", "--stat"], cwd=wt).stdout
        if not diff.strip():
            log(note, "the agent made no changes"); status = "escalated"; finish(); return status

        if not detect_and_run_tests(wt, note):
            log(note, "tests are red — escalating without a PR"); status = "escalated"; finish(); return status

        verdict = judge(f"nightly-{run_id}",
                        f"The change should do exactly this: {cand['text']}. No errors/tracebacks.",
                        agent_out + "\n" + diff, note)
        cost_ledger.log_cost("ollama", project=cand["project"], task_ref=f"nightly:{run_id}",
                              model="qwen2.5:7b", note="the judge — local, $0")

        # --- commit -> push -> PR ---
        sh(["git", "add", "-A"], cwd=wt)
        sh(["git", "commit", "-m",
            f"nightly: {cand['text'][:60]}\n\nSource: {cand['source']}/{cand['source_ref']}; judge: {verdict}\n\n"
            f"Co-Authored-By: Claude <noreply@anthropic.com>"], cwd=wt)
        r = sh(["git", "push", "-u", "origin", branch], cwd=wt, timeout=180)
        if r.returncode != 0:
            log(note, f"push failed: {r.stderr[-300:]}"); finish(); return status
        r = sh(["gh", "pr", "create", "--head", branch, "--base", "main",
                "--title", f"nightly: {cand['text'][:60]}",
                "--body", f"Nightly worker. Source: {cand['source']}/{cand['source_ref']}\n"
                          f"Judge: {verdict}"],
               cwd=wt, timeout=120)
        pr_url = (r.stdout.strip().splitlines() or [""])[-1] if r.returncode == 0 else ""
        log(note, f"PR: {pr_url or 'not created: ' + r.stderr[-200:]}")
        if not pr_url:
            finish(); return status
        status = "pr_open"

        # --- auto-merge gate (P1; off by default at P0) ---
        files = [l.split("|")[0].strip() for l in diff.splitlines() if "|" in l]
        full_diff = sh(["git", "diff", "origin/main...HEAD"], cwd=wt).stdout
        safe, why = is_safe(files, full_diff)
        safe_flag = int(safe)
        log(note, f"is_safe: {safe} ({why})")
        if policy.AUTOMERGE_ENABLED:
            ci_ok = wait_ci(wt, branch)
            ci_res = str(ci_ok)
            log(note, f"CI: {ci_res}")
            if safe and ci_ok is True and verdict != "failed":
                r = sh(["gh", "pr", "merge", branch, "--squash", "--delete-branch", "--auto"],
                       cwd=wt, timeout=120)
                if r.returncode == 0:
                    status = "merged"; log(note, "merged (all three gates passed)")
                else:
                    log(note, f"merge failed: {r.stderr[-200:]}")
            else:
                log(note, "gate not passed — the PR stays open for the morning")
        else:
            log(note, "P0: auto-merge is off — PR stays open for the morning")
        finish(); return status
    finally:
        sh(["git", "worktree", "remove", "--force", str(wt)], cwd=repo)
        c.execute("UPDATE runs SET finished=COALESCE(finished, ?) WHERE id=?",
                  (int(time.time()), run_id)); c.commit()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--once":
        sys.exit(0 if run_one(int(sys.argv[2])) in ("pr_open", "merged", "dup") else 1)
    print("usage: run_task.py --once <candidate_id>"); sys.exit(2)
