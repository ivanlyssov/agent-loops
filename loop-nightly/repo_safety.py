#!/usr/bin/env python3
"""Repo map + diff-safety check for the nightly worker.

REPO_MAP is the only place that maps a project name to a repo path — everything else
(collect.py, rank.py, run_task.py) imports it from here rather than hard-coding paths.
is_safe() is the single gate that decides whether a diff is small and boring enough to
ever be eligible for auto-merge (see policy.AUTOMERGE_ENABLED, which is off by default).
"""
import re
import subprocess
import time

# project name -> path, relative to AGENT_LOOPS_HOME. Replace with your own repos.
REPO_MAP = {
    "app-one": "apps/app-one",
    "app-two": "apps/app-two",
    "app-three": "apps/app-three",
}

# 🔴 markers in a diff -> never auto-merge, always escalate to a human.
RED_PATTERNS = [r"\.env", r"secret", r"token", r"password", r"DROP\s+(TABLE|COLUMN)",
                r"\.github/workflows/", r"DELETE\s+FROM", r"--force", r"rm\s+-rf"]
MAX_SAFE_FILES = 8   # a large diff goes to human review, never auto


def is_safe(diff_files, diff_text):
    """Safe to consider for auto-merge? Small, and free of 🔴 markers."""
    if len(diff_files) > MAX_SAFE_FILES:
        return False, f"large diff ({len(diff_files)} files)"
    for pat in RED_PATTERNS:
        if re.search(pat, diff_text, re.I):
            return False, f"🔴 marker in the diff ({pat})"
    return True, ""


def wait_ci(wt, branch, max_wait=720):
    """Waits for CI. True=green, False=red, None=undetermined within the window."""
    waited = 0
    while waited < max_wait:
        r = subprocess.run(["gh", "pr", "checks", branch], cwd=wt,
                            capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        if "no checks" in out.lower() or "no commit statuses" in out.lower():
            return True                                # no CI configured = treat as passing
        if re.search(r"\bfail", out, re.I):
            return False
        if r.returncode == 0 and "pass" in out.lower() and "pending" not in out.lower():
            return True
        time.sleep(30); waited += 30
    return None
