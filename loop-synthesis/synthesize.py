#!/usr/bin/env python3
"""Knowledge synthesis from a vault of feed digests (P4). Closes the "green" loop for
knowledge: feeds drop RAW material all day (daily digests from various sources), and once
a week that needs to become an INSIGHT — what actually matters and is worth acting on,
not a pile of raw bullet points nobody reads. "Set and forget" for a knowledge base means
raw material piles up and nobody reads it.

Does: collects daily digests from the last 7 days (from configured source folders) ->
synthesizes with a cheap model (a cheap hosted model preferred, public, low-cost data;
falls back to a local model, $0) into one weekly insight for the operator's projects ->
writes a curated note into the vault + sends it to Telegram. Internal/reversible -> no
approval gate needed.

CLI:
  synthesize.py             — run (writes the vault + Telegram)
  synthesize.py --dry       — stdout only (vault/Telegram untouched)
  synthesize.py --model M   — override the hosted model (default: a fast, cheap model)
"""
import sys, re, time, subprocess, json, urllib.request, os
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))
from config import deployment_root, optional_path  # noqa: E402

HERE = Path(__file__).resolve().parent
FEED_DIRS = ["source-feed-a", "source-feed-b", "source-feed-c"]
DEFAULT_MODEL = os.environ.get("SYNTHESIS_MODEL", "deepseek/deepseek-chat")

PROJECTS = ("a small set of independent projects the operator runs — replace this with "
            "your own project names so the synthesis actually ties advice to them")


def _paths():
    root = deployment_root()
    vault = Path(os.environ.get("KNOWLEDGE_VAULT", root / "knowledge-vault"))
    return {
        "vault": vault,
        "sources": vault / "sources",
        "out_dir": vault / "sources" / "weekly-synthesis",
        "notify": root / "loop-status" / "notify.py",
    }


def collect(sources_dir: Path, days=7):
    """Collects bullets from daily digests within the window. Returns (corpus, n_files, n_bullets)."""
    cutoff = date.today() - timedelta(days=days)
    chunks, n_files, n_bul = [], 0, 0
    for d in FEED_DIRS:
        dd = sources_dir / d
        if not dd.exists():
            continue
        for f in sorted(dd.glob("*.md")):
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", f.name)
            if m:
                fd = date(int(m[1]), int(m[2]), int(m[3]))
                if fd < cutoff:
                    continue
            elif f.stat().st_mtime < time.time() - days * 86400:
                continue
            bullets = []
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s.startswith(("- ", "• ", "## ")) and "tags:" not in s.lower():
                    bullets.append(s)
            if any(b.startswith(("- ", "• ")) for b in bullets):
                n_files += 1
                n_bul += sum(1 for b in bullets if b.startswith(("- ", "• ")))
                chunks.append(f"### {d} · {f.stem}\n" + "\n".join(bullets))
    return "\n\n".join(chunks)[:12000], n_files, n_bul


def synth(corpus, model):
    prompt = (
        "You are a knowledge analyst for an operator running several small projects. Below "
        "is a week's worth of raw material from daily feed digests. Produce a WEEKLY INSIGHT, "
        "short and to the point, in markdown:\n\n"
        "## 🔑 Main themes this week\n3-5 points: what actually kept coming up (merge similar items, don't just restate bullets verbatim).\n\n"
        "## 🎯 Worth acting on\nConcrete, tied to the operator's projects (" + PROJECTS + "): "
        "a tool/technique/trend worth trying or adopting. If there's nothing, say so honestly.\n\n"
        "## 👀 On the radar\nWorth noticing, not urgent.\n\n"
        "No fluff, no preamble. Don't invent anything — only from the material below.\n\n=== MATERIAL ===\n" + corpus)
    # a hosted cheap model, if configured
    or_script = optional_path("OPENROUTER_SCRIPT")
    if or_script and or_script.exists():
        try:
            r = subprocess.run(["zsh", str(or_script), model, prompt],
                               capture_output=True, text=True, timeout=180)
            out = r.stdout.strip()
            if out and not out.startswith("ERROR") and len(out) > 80:
                return out, f"hosted/{model}"
        except Exception:
            pass
    # local fallback ($0)
    try:
        body = json.dumps({"model": "qwen2.5:7b", "prompt": prompt, "stream": False,
                           "options": {"num_predict": 700}}).encode()
        req = urllib.request.Request(os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate"),
                                     data=body, headers={"Content-Type": "application/json"})
        out = json.loads(urllib.request.urlopen(req, timeout=180).read()).get("response", "").strip()
        if out:
            return out, "local/qwen2.5:7b"
    except Exception as e:
        return f"_(synthesis unavailable: {e})_", "none"
    return "_(empty model response)_", "none"


def run(dry=False, model=DEFAULT_MODEL):
    paths = _paths()
    corpus, n_files, n_bul = collect(paths["sources"])
    today = date.today().isoformat()
    if not corpus.strip():
        print("synthesis: no feed material in the last 7d — skipping.")
        return
    print(f"synthesis: {n_files} digests, {n_bul} bullets -> model…")
    insight, used = synth(corpus, model)

    note = (f"---\ntitle: synthesis-{today}\ntype: note\n---\n\n"
            f"# 🧬 Weekly knowledge synthesis — {today}\n\n"
            f"_source: {n_files} digests / {n_bul} signals over 7d · model: {used}_\n\n"
            f"{insight}\n")
    print("\n" + note)

    if dry:
        print("[dry] vault/Telegram not touched.")
        return

    paths["out_dir"].mkdir(parents=True, exist_ok=True)
    out_file = paths["out_dir"] / f"synthesis-{today}.md"
    out_file.write_text(note, encoding="utf-8")
    print(f"written: {out_file}")

    tg = f"🧬 Weekly knowledge synthesis — {today}\n({n_bul} signals over 7d, {used})\n\n{insight}"
    try:
        subprocess.run(["python3", str(paths["notify"]), tg], timeout=40)
    except Exception as e:
        print(f"telegram error: {e}", file=sys.stderr)


if __name__ == "__main__":
    a = sys.argv[1:]
    model = a[a.index("--model") + 1] if "--model" in a and a.index("--model") + 1 < len(a) else DEFAULT_MODEL
    run(dry="--dry" in a, model=model)
