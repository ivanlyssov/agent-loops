#!/usr/bin/env python3
"""Daily auto-review of uncommitted changes, across a configured list of repos. For each
repo: if there are uncommitted changes, a read-only coding-agent CLI (`codex`) gives a
short verdict (bugs/risks/loose ends) → summary to Telegram. CHANGES NOTHING and commits
nothing — operators can have local edits in progress; this only reports. Clean repos are
skipped silently. Meant to run every morning via loop-run.sh. Requires `codex` on PATH."""
import json, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))
from config import deployment_root  # noqa: E402

HERE = Path(__file__).resolve().parent
NOTIFY = HERE / "notify.py"
REPOS_CONFIG = HERE / "repos.json"
REPOS_CONFIG_EXAMPLE = HERE / "repos.example.json"
REVIEW_PROMPT = (
    "Read this repository's uncommitted changes (`git diff` and any new files). "
    "Give a VERY SHORT review (English, <=5 lines): are there obvious bugs, security risks, "
    "unfinished stubs/TODOs, forgotten console.log/debug output, or leaked secrets? If it's "
    "all clean, answer with the single word 'clean'. Do NOT modify files, only read."
)


def sh(args, cwd=None, timeout=60):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def load_repos() -> dict:
    path = REPOS_CONFIG if REPOS_CONFIG.exists() else REPOS_CONFIG_EXAMPLE
    if not path.exists():
        return {}
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def main():
    repos = load_repos()
    if not repos:
        print("daily-review: no repos configured (see repos.example.json) — exiting quietly")
        return 0
    root = deployment_root()

    findings = []
    for name, rel in repos.items():
        repo = root / rel
        if not (repo / ".git").exists():
            continue
        try:
            dirty = sh(["git", "status", "--porcelain"], cwd=repo).stdout.strip()
        except Exception:
            continue
        if not dirty:
            continue  # clean repo — skip silently
        try:
            r = sh(["codex", "exec", "-C", str(repo), "-s", "read-only",
                    "--skip-git-repo-check", REVIEW_PROMPT],
                   cwd=repo, timeout=300)
            verdict = (r.stdout or "").strip()[-800:] or "(codex produced no output)"
        except subprocess.TimeoutExpired:
            verdict = "⏱️ codex timed out"
        except Exception as e:
            verdict = f"codex error: {e}"
        n_files = len(dirty.splitlines())
        if "clean" in verdict.lower() and len(verdict) < 30:
            findings.append(f"✅ *{name}* ({n_files} files) — clean")
        else:
            findings.append(f"🔍 *{name}* ({n_files} files):\n{verdict}")

    if not findings:
        print("daily-review: all repos are clean (nothing uncommitted) — exiting quietly")
        return 0

    report = "🌅 *Morning auto-review of uncommitted changes* (read-only)\n\n" + "\n\n".join(findings)
    report += "\n\n_Nothing was changed — this is a read-only pass. Decide what to commit/fix._"
    print(report)
    try:
        subprocess.run([sys.executable, str(NOTIFY), report[:3800]], timeout=60)
    except Exception as e:
        print(f"notify error: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
