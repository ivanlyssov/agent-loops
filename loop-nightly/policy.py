#!/usr/bin/env python3
"""The nightly worker's policy — the ONE place for its constants/rules. Maturity-level
changes (P0->P1->P2) are made here; the code doesn't change."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "common"))
from config import deployment_root  # noqa: E402
from repo_safety import REPO_MAP, is_safe, wait_ci  # noqa: E402,F401  (re-exported for run_task/rank)

# --- maturity level ---
# Ship this at P0 first: NO auto-merge, PR-only, and — if your idea backlog is large
# enough that a docs/tests-only whitelist would starve it — skip straight to P1's
# is_safe()-by-diff gate instead. (In the reference deployment, the P0 whitelist matched
# literally 0 of 50 real backlog ideas; every night it just reported "pool empty".)
P0_DOCS_TESTS_ONLY = False    # P1: code diffs <=8 files; the gate is is_safe() on the diff + the judge
AUTOMERGE_ENABLED = False     # deliberately OFF: a PR waits for a human every morning, no auto-merge
FABLE_PLAN_ENABLED = False    # P2: plan with a stronger model first (verify its auth from the scheduler before enabling)

# --- limits ---
MAX_TASKS_PER_NIGHT = 3
TASK_TIMEOUT_MIN = 40         # plan+code+CI budget for one task
NIGHT_END_HOUR = 6            # stop at 06:00 — don't start anything new after this
ESCALATED_MAX_RETRIES = 2     # within 14 days
ESCALATED_RETRY_DAYS = 14

# --- 🔴 stop-words: a task containing these is never taken autonomously (text pre-filter).
# The technical barrier after code exists is repo_safety.is_safe() (diffs). Both are required.
RED_WORDS = [
    ".env", "secret", "token", "password", "api key",
    "prod", "production", "migrat", "deploy", "domain", "dns", "ssl",
    "payment", "billing", "charge", "stripe",
    "newsletter", "publish", "post to", "send email", "mailing",
    "delete", "drop", "revert", "force", "rm -rf",
    "workflow", "ci.yml", "github secrets", "vps", "ssh",
]

# --- P0 whitelist: while still calibrating, only safe kinds of work ---
P0_GREEN_WORDS = ["test", "readme", "doc", "comment", "typo", "lint"]

# --- "idea -> spec" gate ---
# Backlog items tend to be margin notes ("build an AI agent to automate the business"),
# not assignments. A local model ($0) either rewrites the idea into a concrete spec or
# rejects it as not actionable. Without this gate, lifting the P0 whitelist just floods
# the backlog with junk PRs. Model unavailable -> the candidate is REJECTED with an
# explicit reason, never silently passed through — a silent pass-through is exactly the
# false green that let this loop sit unnoticed for weeks in the reference deployment.
SPEC_GATE_ENABLED = True
SPEC_GATE_MODEL = "qwen2.5:7b"
SPEC_GATE_TIMEOUT = 90
SPEC_GATE_URL = "http://localhost:11434/api/generate"

# --- scoring (rank.py) ---
SCORE_IDEA_INBOX = 30         # explicitly flagged by the operator
SCORE_HAS_CI = 20
SCORE_CONCRETE = 15           # names files / uses concrete verbs
SCORE_AGE_MAX = 10            # aging bonus, capped
SCORE_VAGUE = -20             # "improve", "make better" with no specifics

# --- this loop's own files ---
DB_PATH = HERE / "nightly.db"
LOGS_DIR = HERE / "logs"
BRIEFS_DIR = HERE / "briefs"


def root() -> Path:
    """The wider deployment root. Resolved lazily (called only from inside a function,
    never at import time) so `python3 -m py_compile` and the test suite never need
    AGENT_LOOPS_HOME — only an actual run does."""
    return deployment_root()


# --- paths that reach outside this repo (all lazy — see root() above) ---

def ideas_db() -> Path:
    return root() / "knowledge-vault" / "idea-inbox" / "ideas.db"


def ideas_index() -> Path:
    return root() / "docs" / "IDEAS_INDEX.md"


def ideas_md_paths() -> dict:
    """project -> path to that repo's own IDEAS.md-style backlog file."""
    return {project: root() / rel / "IDEAS.md" for project, rel in REPO_MAP.items()}


def state_md() -> Path:
    return root() / "docs" / "STATE.md"


def supervisor_report() -> Path:
    return root() / "loop-supervisor" / "last-report.md"


def repo_has_ci(project: str) -> bool:
    repo = root() / REPO_MAP.get(project, "@none")
    return (repo / ".github" / "workflows").is_dir()
