#!/usr/bin/env python3
"""Collects the nightly candidate pool from an idea inbox, per-repo IDEAS.md files, and
the supervisor's report."""
import argparse, hashlib, re, sqlite3, sys
from pathlib import Path

import db, policy


def norm(text: str) -> str:
    """Normalize text for rough string comparison."""
    return re.sub(r"\s+", " ", text).strip().lower()


def is_link_line(text: str) -> bool:
    """Lines that are just a link aren't ideas."""
    t = text.strip()
    return (
        t.startswith(("http://", "https://"))
        or bool(re.match(r"^\[[^]]+]\([^)]+\)(?:\s*[—-].*)?$", t))
    )


def clean_idea(text: str) -> str:
    """Strips markdown bullet/bold markers, keeping the human-readable phrasing."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text).strip()
    return re.sub(r"\s+", " ", text).strip(" -–—\t")


def parse_idea_line(line: str, numbered: bool = True) -> str | None:
    """Parses a bullet/numbered idea line; everything else returns None."""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    m = re.match(r"^[-*]\s+(.+)$", raw)
    if not m and numbered:
        m = re.match(r"^\d+[.)]\s+(.+)$", raw)
    if not m:
        return None
    text = clean_idea(m.group(1))
    if len(text) < 15 or is_link_line(text):
        return None
    return text


def parse_numbered_line(line: str) -> str | None:
    """For an index file: only lines shaped like 'N. text'."""
    raw = line.strip()
    m = re.match(r"^\d+[.)]\s+(.+)$", raw)
    if not m:
        return None
    text = clean_idea(m.group(1))
    if len(text) < 15 or is_link_line(text):
        return None
    return text


def project_from_path(path: Path, root: Path) -> str | None:
    """Maps a file path back to a project name by matching REPO_MAP path prefixes."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    rel_str = str(rel)
    for project, repo_rel in policy.REPO_MAP.items():
        if rel_str == repo_rel or rel_str.startswith(repo_rel + "/"):
            return project
    return None


def project_from_text(text: str) -> str | None:
    """Tries to pick out a project from a line of text by matching its name."""
    low = text.lower()
    for project in policy.REPO_MAP:
        if project.lower() in low:
            return project
    return None


def source_idea_inbox() -> list[dict]:
    """Candidates from the idea-inbox sqlite database: only track='dev'."""
    out = []
    try:
        ideas_db = policy.ideas_db()
        if not ideas_db.exists():
            return out
        con = sqlite3.connect(ideas_db)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute("SELECT id, project, idea FROM ideas WHERE track='dev'").fetchall()
        finally:
            con.close()
        for r in rows:
            text = norm(str(r["idea"] or ""))
            if not text:
                continue
            out.append({
                "source": "idea-inbox",
                "source_ref": f"idea:{r['id']}",
                "project": r["project"],
                "text": str(r["idea"]).strip(),
            })
    except Exception as e:
        print(f"collect: idea-inbox skipped: {e}", file=sys.stderr)
    return out


def source_ideas_md() -> list[dict]:
    """Candidates from each project's own IDEAS.md, plus a global index file."""
    out = []
    root = policy.root()
    try:
        for project, path in policy.ideas_md_paths().items():
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception as e:
                print(f"collect: {path} skipped: {e}", file=sys.stderr)
                continue
            rel = path.relative_to(root)
            for n, line in enumerate(lines, 1):
                text = parse_idea_line(line)
                if text:
                    out.append({
                        "source": "ideas-md",
                        "source_ref": f"{rel}#{n}",
                        "project": project,
                        "text": text,
                    })
        path = policy.ideas_index()
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            rel = path.relative_to(root)
            for n, line in enumerate(lines, 1):
                text = parse_numbered_line(line)
                project = project_from_text(line)
                if text and project in policy.REPO_MAP:
                    out.append({
                        "source": "ideas-md",
                        "source_ref": f"{rel}#{n}",
                        "project": project,
                        "text": text,
                    })
    except Exception as e:
        print(f"collect: IDEAS.md skipped: {e}", file=sys.stderr)
    return out


def source_supervisor() -> list[dict]:
    """Candidates from ⚠️-lines in the supervisor's report, where a project is recognized."""
    out = []
    try:
        path = policy.supervisor_report()
        if not path.exists():
            return out
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "⚠️" not in line or "🔴" in line:
                continue
            text = clean_idea(line.replace("⚠️", ""))
            project = project_from_text(text)
            if project not in policy.REPO_MAP or len(text) < 15:
                continue
            h = hashlib.sha1(line[:12].encode("utf-8")).hexdigest()[:12]
            out.append({
                "source": "supervisor",
                "source_ref": f"supervisor#{h}",
                "project": project,
                "text": text,
            })
    except Exception as e:
        print(f"collect: supervisor skipped: {e}", file=sys.stderr)
    return out


def blacklist() -> list[str]:
    """The 'needs a human' section plus every 🔴 line of the shared state doc."""
    state_md = policy.state_md()
    if not state_md.exists():
        return []
    try:
        lines = state_md.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        print(f"collect: STATE.md unreadable: {e}", file=sys.stderr)
        return []
    out, in_human_section = [], False
    for line in lines:
        if line.startswith("## "):
            in_human_section = line.startswith("## 🔴 Needs a human")
            continue
        if in_human_section or "🔴" in line:
            text = norm(line.replace("🔴", ""))
            if len(text) >= 15:
                out.append(text)
    return out


def blocked(text: str, black: list[str]) -> bool:
    """True if a candidate overlaps a blacklisted line by a >=15-char substring."""
    t = norm(text)
    for b in black:
        if len(b) >= 15 and (b in t or t in b):
            return True
        for i in range(0, max(0, len(b) - 14)):
            if b[i:i + 15] in t:
                return True
    return False


def collect() -> tuple[list[dict], int]:
    """All sources — one source failing doesn't take down the others."""
    black = blacklist()
    candidates, filtered = [], 0
    for fn in (source_idea_inbox, source_ideas_md, source_supervisor):
        for cand in fn():
            if cand.get("project") not in policy.REPO_MAP:
                continue
            if blocked(cand["text"], black):
                filtered += 1
                print(f"collect: blacklisted: {cand['source']} {cand['source_ref']} {cand['text'][:80]}",
                      file=sys.stderr)
                continue
            candidates.append(cand)
    return candidates, filtered


def print_table(candidates: list[dict]) -> None:
    for c in candidates:
        print(f"{c['source']} | {c['project']} | {c['text'][:80]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    candidates, filtered = collect()
    if args.dry:
        print_table(candidates)
        return 0

    con = db.connect()
    added = dup = 0
    night = db.tonight()
    with con:
        for c in candidates:
            cur = con.execute(
                "INSERT OR IGNORE INTO candidates(night, source, source_ref, project, text) VALUES(?,?,?,?,?)",
                (night, c["source"], c["source_ref"], c["project"], c["text"]),
            )
            if cur.rowcount:
                added += 1
            else:
                dup += 1
    print(f"added {added} new, skipped {dup} duplicates, filtered {filtered} by the blacklist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
