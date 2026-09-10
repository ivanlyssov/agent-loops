#!/usr/bin/env python3
"""Morning briefing for the nightly worker.

CLI:
  python3 briefing.py            — real run: voice (if the night wasn't empty) + text to Telegram
  python3 briefing.py --dry      — just print the text, send nothing

Logic:
  - Reads runs for db.tonight() (joined with candidates for the task's gist).
  - Builds a ~30-60s spoken summary: how many were taken / ready for review (PR) / merged /
    failed-or-timed-out (with a reason). An empty pool gets a short phrase.
  - Real mode: voice is best-effort (must never take down the text copy); text is always
    sent, with a list of PR links.
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import db  # noqa: E402
from policy import BRIEFS_DIR  # noqa: E402

NOTIFY = HERE.parent / "loop-status" / "notify.py"
TTS = os.environ.get("TTS_SCRIPT")  # optional: path to a script(text, out_mp3) -> mp3


def _task_summary(text: str, limit: int = 120) -> str:
    """The gist of a task in one phrase — the candidate's first line/sentence, trimmed."""
    if not text:
        return "no description"
    line = text.strip().splitlines()[0].strip()
    line = line.lstrip("-*• ").strip()
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return line or "no description"


def fetch_runs(night: str):
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT r.*, c.text AS candidate_text, c.project AS project
            FROM runs r
            LEFT JOIN candidates c ON c.id = r.candidate_id
            WHERE r.night = ?
            ORDER BY r.id
            """,
            (night,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def build_text(night: str, runs: list) -> str:
    if not runs:
        return "Good morning. The pool was empty last night — nothing to take on."

    n = len(runs)
    pr_open = [r for r in runs if r["status"] == "pr_open"]
    merged = [r for r in runs if r["status"] == "merged"]
    failed = [r for r in runs if r["status"] in ("failed", "timeout", "escalated")]

    lines = [f"Good morning. Took on {n} task" + ("s" if n != 1 else "") + " overnight."]

    if pr_open:
        lines.append(f"Ready for review: {len(pr_open)}.")
        for r in pr_open:
            summary = _task_summary(r.get("candidate_text") or r.get("note") or "")
            lines.append(f"— {summary}.")

    if merged:
        lines.append(f"Merged: {len(merged)}.")
        for r in merged:
            summary = _task_summary(r.get("candidate_text") or r.get("note") or "")
            lines.append(f"— {summary}.")

    if failed:
        lines.append(f"Failed or timed out: {len(failed)}.")
        for r in failed:
            summary = _task_summary(r.get("candidate_text") or r.get("note") or "")
            reason = (r.get("note") or r.get("judge_verdict") or r["status"] or "").strip()
            reason = reason.splitlines()[0][:100] if reason else r["status"]
            lines.append(f"— {summary}: {reason}.")

    return " ".join(lines)


def build_text_links(runs: list) -> str:
    links = [r["pr_url"] for r in runs if r.get("pr_url")]
    if not links:
        return ""
    return "\n\nLinks:\n" + "\n".join(f"— {u}" for u in links)


def main():
    dry = "--dry" in sys.argv[1:]
    night = db.tonight()
    runs = fetch_runs(night)
    text = build_text(night, runs)

    if dry:
        print(text)
        return 0

    # (a) voice — best-effort, only if TTS_SCRIPT is set and the night wasn't empty
    if runs and TTS:
        try:
            BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
            mp3_path = BRIEFS_DIR / f"{night}.mp3"
            result = subprocess.run(
                [TTS, text, str(mp3_path)],
                capture_output=True, text=True, timeout=180,
            )
            if result.returncode == 0 and mp3_path.is_file():
                subprocess.run(
                    [sys.executable, str(NOTIFY), "--voice", str(mp3_path),
                     f"🌙 Night report {night}"],
                    capture_output=True, text=True, timeout=60,
                )
            else:
                print(f"briefing: TTS_SCRIPT failed: {result.stderr[:300]}", file=sys.stderr)
        except Exception as e:
            print(f"briefing: voice branch failed (best-effort, continuing): {e}", file=sys.stderr)

    # (b) text copy — always, with the full text + links
    full_text = text + build_text_links(runs)
    try:
        subprocess.run([sys.executable, str(NOTIFY), full_text], timeout=60)
    except Exception as e:
        print(f"briefing: text notifier failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
