#!/usr/bin/env python3
"""Meta-supervisor for the loop harness (P3). A weekly self-check that acts as EYES over
the autonomous loops, so "set it and forget it" doesn't quietly turn into "set it and
never notice it rotted".

Deterministic checks ($0, stdlib): scheduler drift, run statuses (status.db), the
approval-queue's health (queue.db), disk space, whether a knowledge base is still being
written to. Findings are bucketed 🔴 regression / ⚠️ attention / ✅ fine. A short narrative
summary is optionally added via a local model (does not block). Report -> Telegram + an
optional shared state doc.

Principle: this does NOT fix anything automatically — it only reports. A regression shows
up as its own line in the daily/weekly channel.

CLI:
  supervisor.py            — full run (report to stdout + Telegram + state doc + last-report.md)
  supervisor.py --dry      — same, but no Telegram and no state-doc write (stdout only)
  supervisor.py --no-llm   — skip the local-model narrative (faster)
"""
import sys, os, re, json, time, sqlite3, subprocess, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))
from config import deployment_root  # noqa: E402

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline.json"               # snapshot of the expected set of scheduled services
REPORT = HERE / "last-report.md"
LABEL_PREFIX = os.environ.get("LAUNCHD_LABEL_PREFIX", "com.example.agent-loops.")

# Example set of paths this repo doesn't ship (they live in the wider deployment). All
# are optional: every check below degrades gracefully (a "note", not a crash) if a path
# doesn't exist. Point AGENT_LOOPS_HOME at a real deployment to make these resolve.
STALE_PATHS = ("legacy/old-investing-module", "legacy/retired-ci/")

# Expected freshness window per task (hours). Default is daily-with-slack.
CADENCE_H = {
    "content-sync": 30, "feed-ingest": 30, "vault-hygiene": 30,
    "backup-pull": 30, "revenue-sync": 30,
    "loop-watchdog": 30, "loop-supervisor": 8 * 24, "loop-synthesis": 8 * 24, "idea-digest": 30,
    "ad-factory-burnout": 30, "ad-factory-weekly": 8 * 24, "design-audit": 8 * 24,
}
DEFAULT_CADENCE_H = 30


def _now():
    return time.time()


def _paths():
    """Cross-repo paths, resolved lazily so `--dry`/`--help` don't require AGENT_LOOPS_HOME
    unless a check that actually needs it runs."""
    root = deployment_root()
    return {
        "status_db": root / "loop-status" / "status.db",
        "queue_db": root / "loop-queue" / "queue.db",
        "notify": root / "loop-status" / "notify.py",
        "state_md": root / "docs" / "STATE.md",
        "vault": Path(os.environ.get("KNOWLEDGE_VAULT", root / "knowledge-vault")),
        "tasks_md": root / "docs" / "TASKS.md",
        "claude_md": root / "CLAUDE.md",
        "claude_dir": root / ".claude",
        "docs_dir": root / "docs",
        "root": root,
    }


# ── checks ────────────────────────────────────────────────────────────────

def check_launchd():
    """Scheduler drift: something from the baseline disappeared from launchctl."""
    out = {"red": [], "warn": [], "note": [], "loaded": []}
    try:
        raw = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=20).stdout
    except Exception as e:
        out["warn"].append(f"launchctl unavailable: {e}")
        return out
    loaded = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].startswith(LABEL_PREFIX):
            label = parts[2].strip()
            exit_code = parts[1].strip()
            loaded[label] = exit_code
    out["loaded"] = sorted(loaded)
    base = set()
    if BASELINE.exists():
        try:
            base = set(json.loads(BASELINE.read_text()))
        except Exception:
            base = set()
    if not base:
        # first run — record the baseline, catch drift from the next run on
        BASELINE.write_text(json.dumps(sorted(loaded), ensure_ascii=False, indent=1))
        out["note"].append(f"baseline recorded ({len(loaded)} services) — drift is caught from the next run")
        return out
    missing = base - set(loaded)
    added = set(loaded) - base
    for m in sorted(missing):
        out["red"].append(f"service unloaded: {m} (was in the baseline, missing from launchctl)")
    for a in sorted(added):
        out["note"].append(f"new service: {a} — add it to monitoring if it should be watched")
    # only grow the baseline with additions (don't "forget" disappearances, so the alert holds)
    if added:
        BASELINE.write_text(json.dumps(sorted(base | added), ensure_ascii=False, indent=1))
    return out


def check_status(paths):
    """status.db: tasks with no success in their window (regression), or a failing last run."""
    out = {"red": [], "warn": [], "note": []}
    status_db = paths["status_db"]
    if not status_db.exists():
        out["warn"].append("status.db missing — is observability even writing?")
        return out
    c = sqlite3.connect(status_db)
    labels = [r[0] for r in c.execute("SELECT DISTINCT label FROM runs")]
    for label in labels:
        row = c.execute("SELECT ts,status FROM runs WHERE label=? ORDER BY ts DESC LIMIT 1", (label,)).fetchone()
        if not row:
            continue
        ts, st = row
        age_h = (_now() - ts) / 3600
        win = CADENCE_H.get(label, DEFAULT_CADENCE_H)
        last_ok = c.execute("SELECT MAX(ts) FROM runs WHERE label=? AND status='ok'", (label,)).fetchone()[0]
        ok_age = (_now() - last_ok) / 3600 if last_ok else None
        if ok_age is None or ok_age > win:
            out["red"].append(f"{label}: no success " +
                              ("ever" if ok_age is None else f"in {ok_age:.0f}h (window {win}h)"))
        elif st == "error":
            out["warn"].append(f"{label}: last run was an error ({age_h:.0f}h ago), but there was a success in-window")
    c.close()
    return out


def check_queue(paths):
    """Approval-queue health: stuck pending items, expirations, worker failures, filter drift (reject-rate)."""
    out = {"red": [], "warn": [], "note": [], "stats": {}}
    queue_db = paths["queue_db"]
    if not queue_db.exists():
        out["note"].append("queue.db missing — the approval gate hasn't been used yet")
        return out
    c = sqlite3.connect(queue_db)
    week = int(_now()) - 7 * 86400
    month = int(_now()) - 30 * 86400
    pend = c.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0]
    stale = c.execute("SELECT COUNT(*) FROM proposals WHERE status='pending' AND created<?",
                      (int(_now()) - 3 * 86400,)).fetchone()[0]
    expired = c.execute("SELECT COUNT(*) FROM proposals WHERE status='expired' AND created>?", (week,)).fetchone()[0]
    failed = c.execute("SELECT COUNT(*) FROM proposals WHERE status='failed' AND created>?", (week,)).fetchone()[0]
    appr = c.execute("SELECT COUNT(*) FROM proposals WHERE status IN('approved','done') AND decided>?", (month,)).fetchone()[0]
    rej = c.execute("SELECT COUNT(*) FROM proposals WHERE status='rejected' AND decided>?", (month,)).fetchone()[0]
    c.close()
    out["stats"] = {"pending": pend, "stale": stale, "expired7d": expired, "failed7d": failed,
                    "approved30d": appr, "rejected30d": rej}
    out["note"].append(f"queue: {pend} waiting, {appr} approved / {rej} rejected over 30d")
    if stale:
        out["warn"].append(f"queue: {stale} pending items older than 3d — is the batch being ignored? (approval fatigue)")
    if expired:
        out["warn"].append(f"queue: {expired} expired over 7d — proposals are going unreviewed")
    if failed:
        out["red"].append(f"queue: {failed} approved actions FAILED to execute over 7d")
    total_dec = appr + rej
    if total_dec >= 8 and rej / total_dec > 0.6:
        out["warn"].append(f"queue: reject-rate {rej}/{total_dec} over 30d — the proposal generator is drifting (too much noise going out)")
    return out


def check_disk(paths):
    out = {"red": [], "warn": [], "note": []}
    try:
        r = subprocess.run(["df", "-Pk", str(paths["root"])], capture_output=True, text=True, timeout=15).stdout.splitlines()
        avail_k, cap = int(r[1].split()[3]), r[1].split()[4]
        free_gb = avail_k / 1024 / 1024
        used_pct = int(cap.rstrip("%"))
        if free_gb < 10:
            out["red"].append(f"disk: {free_gb:.0f}GB free ({used_pct}% used) — loops may start failing")
        elif free_gb < 25:
            out["warn"].append(f"disk: {free_gb:.0f}GB free ({used_pct}% used)")
        out["note"].append(f"disk: {free_gb:.0f}GB free ({used_pct}% used)")
    except Exception as e:
        out["warn"].append(f"disk: could not check ({e})")
    return out


def check_vault(paths):
    """Is the knowledge base still alive: any .md changed in the last 7 days? Zero, while
    feeds are supposedly running, is suspicious (are they actually writing anything?)."""
    out = {"red": [], "warn": [], "note": []}
    vault = paths["vault"]
    if not vault.exists():
        out["note"].append("knowledge vault not found locally")
        return out
    cutoff = _now() - 7 * 86400
    n = 0
    try:
        for p in vault.rglob("*.md"):
            if "/_archive/" in str(p) or "/.git/" in str(p):
                continue
            if p.stat().st_mtime > cutoff:
                n += 1
    except Exception as e:
        out["warn"].append(f"vault: could not scan ({e})")
        return out
    out["note"].append(f"vault: {n} notes changed/created in the last 7d")
    if n == 0:
        out["warn"].append("vault: 0 new notes in 7d — are the feeds/imports still writing? (a quiet death of the knowledge loop)")
    return out


def check_doc_drift(paths):
    """Doc hygiene: freshness of the shared state doc / task backlog, broken local links
    in the top-level agent instructions, stale path references in live docs (outside
    archive/cleanup folders)."""
    out = {"red": [], "warn": [], "note": []}
    root, state_md, tasks_md = paths["root"], paths["state_md"], paths["tasks_md"]
    claude_md, claude_dir, docs_dir = paths["claude_md"], paths["claude_dir"], paths["docs_dir"]

    # 1. freshness of the shared state doc
    if not state_md.exists():
        out["warn"].append("STATE.md not found — sessions start with no picture of current state")
    else:
        age_d = (_now() - state_md.stat().st_mtime) / 86400
        if age_d > 7:
            out["warn"].append(f"STATE.md hasn't been updated in {age_d:.0f}d — sessions start from a stale picture")
        else:
            out["note"].append(f"STATE.md last updated {age_d:.1f}d ago")

    # 2. broken local links in the agent-instructions docs
    link_re = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    docs_to_scan = []
    if claude_md.exists():
        docs_to_scan.append(claude_md)
    if claude_dir.exists():
        docs_to_scan += sorted(claude_dir.glob("*.md"))
    broken = []
    for doc in docs_to_scan:
        try:
            text = doc.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in link_re.finditer(text):
            target = m.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_clean = target.split("#")[0].strip()
            if not target_clean:
                continue
            candidate = (doc.parent / target_clean).resolve()
            if not candidate.exists():
                broken.append(f"{doc.relative_to(root)}: [{target}] -> file not found")
    if broken:
        shown = broken[:10]
        more = f" (+{len(broken) - 10} more)" if len(broken) > 10 else ""
        out["warn"].append("broken local links in docs:\n  " + "\n  ".join(shown) + more)
    else:
        out["note"].append(f"local links in the agent-instructions docs checked ({len(docs_to_scan)} files) — none broken")

    # 3. stale path references in live docs (outside archive/cleanup folders)
    live_docs = []
    if claude_md.exists():
        live_docs.append(claude_md)
    if claude_dir.exists():
        live_docs += sorted(claude_dir.glob("*.md"))
    if docs_dir.exists():
        for p in docs_dir.rglob("*.md"):
            if "/_archive/" in str(p) or "/cleanup-" in str(p):
                continue
            live_docs.append(p)
    stale_hits = []
    for doc in live_docs:
        try:
            text = doc.read_text(encoding="utf-8")
        except Exception:
            continue
        for stale in STALE_PATHS:
            if stale in text:
                stale_hits.append(f"{doc.relative_to(root)}: references stale path \"{stale}\"")
    if stale_hits:
        shown = stale_hits[:10]
        more = f" (+{len(stale_hits) - 10} more)" if len(stale_hits) > 10 else ""
        out["warn"].append("stale path references in live docs:\n  " + "\n  ".join(shown) + more)

    # 4. freshness of the task backlog (soft check)
    if not tasks_md.exists():
        out["note"].append("TASKS.md not found")
    else:
        age_d = (_now() - tasks_md.stat().st_mtime) / 86400
        if age_d > 14:
            out["warn"].append(f"TASKS.md hasn't been updated in {age_d:.0f}d — the backlog may be stale")
        else:
            out["note"].append(f"TASKS.md last updated {age_d:.1f}d ago")

    return out


# ── narrative (optional, local model) ───────────────────────────────────────

def narrative(red, warn, notes):
    facts = "REGRESSIONS:\n" + ("\n".join(red) or "-") + "\n\nATTENTION:\n" + ("\n".join(warn) or "-")
    prompt = ("You supervise an autonomous agent infrastructure. Give a 2-3 line verdict: "
              "what matters most, what to look at first. No fluff, don't just restate the facts.\n\n" + facts)
    try:
        body = json.dumps({"model": "qwen2.5:7b", "prompt": prompt, "stream": False,
                           "options": {"num_predict": 160}}).encode()
        req = urllib.request.Request(os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate"),
                                     data=body, headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        return resp.get("response", "").strip()
    except Exception:
        return ""


# ── report assembly ─────────────────────────────────────────────────────────

def run(dry=False, use_llm=True):
    paths = _paths()
    sections = [check_launchd(), check_status(paths), check_queue(paths), check_disk(paths),
                check_vault(paths), check_doc_drift(paths)]
    red, warn, note = [], [], []
    for s in sections:
        red += s.get("red", []); warn += s.get("warn", []); note += s.get("note", [])

    # second leg: quality trends over the period (grades/evals/queue reject-rate + a
    # sample of artifacts), see quality.py
    try:
        import quality
        qr, qw, qn = quality.run(paths)
        red += qr; warn += qw
        note += [f"[quality] {x}" for x in qn]
    except Exception as e:
        warn.append(f"⚠️ quality.py did not complete: {e}")

    verdict = "🔴 REGRESSION" if red else ("⚠️ ATTENTION" if warn else "✅ ALL HEALTHY")
    when = time.strftime("%Y-%m-%d %H:%M")
    lines = [f"# 🔭 Meta-supervision — {verdict}", f"_{when}_", ""]
    if red:
        lines.append("## 🔴 Regressions (fix):")
        lines += [f"- {x}" for x in red]; lines.append("")
    if warn:
        lines.append("## ⚠️ Attention:")
        lines += [f"- {x}" for x in warn]; lines.append("")
    lines.append("## ✅ Normal / facts:")
    lines += [f"- {x}" for x in note]

    nar = narrative(red, warn, note) if use_llm else ""
    if nar:
        lines += ["", "## 🧠 Verdict:", nar]

    report = "\n".join(lines)
    print(report)
    REPORT.write_text(report + "\n", encoding="utf-8")

    if dry:
        return report

    # Telegram — compact
    tg = [f"🔭 Meta-supervision: {verdict}"]
    if red:
        tg.append("\n🔴 Regressions:\n" + "\n".join(f"• {x}" for x in red))
    if warn:
        tg.append("\n⚠️ Attention:\n" + "\n".join(f"• {x}" for x in warn))
    if not red and not warn:
        tg.append("All loops are fresh, queue and disk are fine.")
    if nar:
        tg.append("\n🧠 " + nar)
    try:
        subprocess.run(["python3", str(paths["notify"]), "\n".join(tg)], timeout=40)
    except Exception as e:
        print(f"telegram error: {e}", file=sys.stderr)

    _write_state(paths, report)
    return report


def _write_state(paths, report):
    """Replaces a marker-delimited block in the shared state doc (doesn't grow forever)."""
    state_md = paths["state_md"]
    if not state_md.exists():
        return
    beg, end = "<!-- SUPERVISOR:BEGIN -->", "<!-- SUPERVISOR:END -->"
    block = f"{beg}\n## 🔭 Meta-supervision (last run)\n\n{report}\n{end}"
    txt = state_md.read_text(encoding="utf-8")
    if beg in txt and end in txt:
        txt = re.sub(re.escape(beg) + r".*?" + re.escape(end), block, txt, flags=re.S)
    else:
        txt = txt.rstrip() + "\n\n" + block + "\n"
    state_md.write_text(txt, encoding="utf-8")


if __name__ == "__main__":
    a = sys.argv[1:]
    run(dry="--dry" in a, use_llm="--no-llm" not in a)
