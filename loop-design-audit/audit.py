#!/usr/bin/env python3
"""Weekly design audit: LOOKS at live screens (screenshots + a local vision model) and
drops visual findings into each project's design backlog (an IDEAS.md-style file, parsed
by loop-nightly/collect.py).

Does:
1. Takes screenshots (Playwright, mobile 390x844 + desktop 1440x900) for the pages listed
   in `targets.json` (or `targets.example.json` if you haven't copied it yet).
2. Sends each screenshot to a local vision model (free, local) with a checklist: hierarchy/
   readability, contrast, spacing/alignment, empty states, mobile clipping, consistency —
   asks for at most 5 concrete issues, no fluff, no praise.
3. Dedupes findings against existing "Design:" lines already in that target's IDEAS.md
   (fuzzy substring match), so the same issue doesn't get logged every week.
4. Appends new findings to the target's configured `ideas_md` path, in a format
   `loop-nightly/collect.py` parses:
     - **Design: <topic>** — <description> _(design-audit YYYY-MM-DD)_
5. Reports to Telegram via `loop-status/notify.py`.
6. Screenshots go to `shots/<date>/`, retained 30 days (older folders are deleted).

CLI:
  audit.py            — full run (writes IDEAS.md files, sends Telegram)
  audit.py --dry       — does everything (screenshots+analysis) but writes nothing and
                         sends nothing — prints it all to stdout instead.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "common"))
from config import deployment_root  # noqa: E402

TARGETS_CONFIG = HERE / "targets.json"
TARGETS_CONFIG_EXAMPLE = HERE / "targets.example.json"
SHOTS_DIR = HERE / "shots"
SHOTS_RETENTION_DAYS = 30

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
# A transient Ollama failure (model not loaded yet, VRAM busy) shouldn't quietly turn
# into "0 findings" — retry a few times, then fail loudly.
OLLAMA_RETRIES = 3
OLLAMA_RETRY_SLEEP_S = 5
# Cap image height for retries (a full-page screenshot can be 6000+ px tall).
MAX_IMG_HEIGHT_PX = 3000
OLLAMA_MODEL = "qwen2.5vl:7b"
OLLAMA_TIMEOUT = 180

VIEWPORTS = [
    ("mobile", "390,844"),
    ("desktop", "1440,900"),
]

CHECKLIST_PROMPT = """You are a design reviewer. Here is a screenshot of a web screen ({viewport_label}, {viewport_size}).
Screen: {page_label}.

Evaluate against this checklist:
- hierarchy / readability
- contrast
- spacing / alignment
- empty states
- clipping/overflow on mobile (if this is a mobile screenshot)
- consistency with the site's other screens

Give STRICTLY a list of AT MOST 5 of the most important concrete issues (or fewer, if
there are fewer). Format each line:
- <short issue> — <exactly where on the screen>

No fluff, no preamble, no praise. If there are truly no issues, answer with exactly one
line: "No issues found."""


def load_targets() -> list[dict]:
    path = TARGETS_CONFIG if TARGETS_CONFIG.exists() else TARGETS_CONFIG_EXAMPLE
    if not path.exists():
        return []
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return cfg.get("targets", [])


def sh(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)
    except Exception as e:
        return 1, str(e)


PLAYWRIGHT_VERSION = "1.63.0"  # see the comment in take_screenshot


def take_screenshot(url: str, out_path: Path, viewport_size: str) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # --wait-for-timeout 3000: let reveal-on-scroll animations settle first, or the
    # vision model sees "empty" blocks; --full-page: the whole page, not just the first
    # viewport (avoids false "the bottom is empty" findings).
    # The version is pinned deliberately: an unpinned "latest" once silently bumped the
    # bundled browser revision, the browser failed to install, and every screenshot in
    # the run failed while the run itself still reported "0 screens" instead of erroring.
    # Bumping the version needs `npx playwright@<new> install chromium` first.
    cmd = ["npx", f"playwright@{PLAYWRIGHT_VERSION}", "screenshot", "--viewport-size", viewport_size,
           "--wait-for-timeout", "3000", "--full-page", url, str(out_path)]
    ec, out = sh(cmd, timeout=90)
    if ec != 0 or not out_path.exists():
        print(f"  [screenshot FAIL] {url} ({viewport_size}): {out.strip()[:300]}", file=sys.stderr)
        return False
    return True


def _build_body(prompt: str, img_b64: str) -> bytes:
    return json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "images": [img_b64],
        "stream": False,
        "options": {"num_predict": 500},
    }).encode()


def _shrink_b64(img_path: Path, max_height: int):
    """Shrinks a screenshot to max_height. None if Pillow is missing or it wasn't needed."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        import io
        with Image.open(img_path) as im:
            if im.height <= max_height:
                return None
            ratio = max_height / im.height
            small = im.resize((max(1, int(im.width * ratio)), max_height))
            buf = io.BytesIO()
            small.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def analyze_screenshot(img_path: Path, page_label: str, viewport_label: str, viewport_size: str):
    """Returns the model's response text, or None if analysis failed.

    None and "" are different: "" means "the model looked and found nothing", None means
    "this screen was never actually checked". Conflating them used to make a failed
    analysis look identical to a clean pass.
    """
    prompt = CHECKLIST_PROMPT.format(
        viewport_label=viewport_label, viewport_size=viewport_size, page_label=page_label
    )
    # A full-page screenshot of a long homepage can be 1440x6600 — combined with a long
    # prompt that overflows the model's context and reliably returns HTTP 400. Shrink up front.
    img_b64 = _shrink_b64(img_path, MAX_IMG_HEIGHT_PX) or base64.b64encode(img_path.read_bytes()).decode()
    body = _build_body(prompt, img_b64)
    last_err = None
    for attempt in range(1, OLLAMA_RETRIES + 1):
        if attempt > 1:
            # The model sometimes rejects a very tall full-page screenshot with a 400.
            # On retry, resend the same page shrunk further — better to check it at a
            # lower resolution than not at all.
            shrunk = _shrink_b64(img_path, MAX_IMG_HEIGHT_PX // (2 ** attempt))
            if shrunk:
                body = _build_body(prompt, shrunk)
        req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
        try:
            out = json.loads(urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT).read())
            return (out.get("response") or "").strip()
        except Exception as e:
            last_err = e
            if attempt < OLLAMA_RETRIES:
                print(f"  [ollama retry {attempt}/{OLLAMA_RETRIES - 1}] {page_label}/{viewport_label}: {e}",
                      file=sys.stderr)
                time.sleep(OLLAMA_RETRY_SLEEP_S * attempt)
    print(f"  [ollama FAIL] {page_label}/{viewport_label}: {last_err}", file=sys.stderr)
    return None


def parse_findings(raw: str) -> list[str]:
    """Extracts issue items from the model's response — bullets '- text'/'* text' OR a
    numbered list '1. text' (the model doesn't always stick to the prompt's format).
    Multi-line items (a continuation line with an indent/dash but no marker) get merged
    into the previous item. Empty list if the model said 'No issues found' or gave nothing."""
    if not raw or "no issues found" in raw.lower():
        return []
    items: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^[-*]\s+(.+)$", s) or re.match(r"^\d+[.)]\s+(.+)$", s)
        if m:
            text = re.sub(r"\*\*([^*]+)\*\*", r"\1", m.group(1)).strip()
            items.append(text)
        elif items and s.startswith(("-", "—", "–")):
            # continuation of the previous item, e.g. an indented "   - Issue: ..."
            cont = re.sub(r"^[-—–]\s*", "", s)
            items[-1] = f"{items[-1]} — {cont}".strip()
    out = [t for t in items if len(t) >= 10]
    return out[:5]


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def existing_design_lines(ideas_path: Path) -> list[str]:
    """Already-recorded 'Design:' findings from a target's IDEAS.md — for dedup."""
    if not ideas_path.exists():
        return []
    out = []
    for line in ideas_path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if re.match(r"^[-*]\s+\*\*Design:", s):
            out.append(norm(s))
    return out


def is_duplicate(finding_text: str, existing_norm: list[str]) -> bool:
    """Fuzzy substring dedup: if a meaningful chunk of the new finding (>=20 chars) is
    already present in an existing line (or vice versa), treat it as a duplicate."""
    ft = norm(finding_text)
    for ex in existing_norm:
        if len(ft) >= 20 and ft[:40] in ex:
            return True
        if len(ex) >= 20 and ex[:40] in ft:
            return True
        # a shorter rolling substring check to catch partial phrasing matches
        window = 25
        if len(ft) >= window:
            for i in range(0, len(ft) - window + 1, 5):
                if ft[i:i + window] in ex:
                    return True
    return False


def cleanup_old_shots():
    if not SHOTS_DIR.exists():
        return
    cutoff = date.today() - timedelta(days=SHOTS_RETENTION_DAYS)
    for d in SHOTS_DIR.iterdir():
        if not d.is_dir():
            continue
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", d.name)
        if not m:
            continue
        try:
            dd = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            continue
        if dd < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            print(f"[retention] deleted old screenshot folder: {d}")


def run(dry: bool = False) -> int:
    targets = load_targets()
    if not targets:
        print("audit: no targets configured (see targets.example.json) — exiting")
        return 0
    root = deployment_root()
    notify = root / "loop-status" / "notify.py"

    today = date.today().isoformat()
    shots_today = SHOTS_DIR / today

    total_screens = 0
    # target name -> list of (page_label, viewport_label, finding_text)
    findings_by_target: dict[str, list[tuple[str, str, str]]] = {}
    unchecked: list[str] = []       # screens the model couldn't look at
    shot_failed: list[str] = []     # screenshot itself failed — "the camera is broken", not "clean"
    new_by_target: dict[str, list[str]] = {}

    for target in targets:
        name = target["name"]
        base_url = target["url"].rstrip("/")
        for page in target.get("pages", ["/"]):
            page_url = base_url + page if page != "/" else base_url + "/"
            page_slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", page.strip("/") or "home")
            page_label = f"{name}{page}".rstrip("/") or name

            for viewport_label, viewport_size in VIEWPORTS:
                img_name = f"{name}_{page_slug}_{viewport_label}.png"
                img_path = shots_today / img_name
                print(f"[screenshot] {page_url} ({viewport_label}) -> {img_path.name}")
                ok = take_screenshot(page_url, img_path, viewport_size)
                if not ok:
                    shot_failed.append(f"{page_label}/{viewport_label}")
                    continue
                total_screens += 1

                print(f"  [analyze] {OLLAMA_MODEL} ...")
                t0 = time.time()
                raw = analyze_screenshot(img_path, page_label, viewport_label, viewport_size)
                dt = time.time() - t0
                if raw is None:
                    unchecked.append(f"{page_label}/{viewport_label}")
                    print(f"  [analyze] NOT CHECKED (analysis failed) after {dt:.0f}s")
                    continue
                findings = parse_findings(raw)
                print(f"  [analyze] {len(findings)} findings in {dt:.0f}s")
                for f in findings:
                    findings_by_target.setdefault(name, []).append((page_label, viewport_label, f))

    # ── dedup + write ──────────────────────────────────────────────────
    report_lines = []
    total_new = 0
    targets_by_name = {t["name"]: t for t in targets}
    for name, findings in findings_by_target.items():
        ideas_rel = targets_by_name.get(name, {}).get("ideas_md")
        if not ideas_rel:
            print(f"  [warn] no ideas_md configured for target {name}, skipping", file=sys.stderr)
            continue
        ideas_path = root / ideas_rel
        existing_norm = existing_design_lines(ideas_path)

        new_lines = []
        for page_label, viewport_label, f in findings:
            dedup_key = f"{page_label} {viewport_label}: {f}"
            if is_duplicate(dedup_key, existing_norm):
                continue
            # topic = the gist up to the first " - "/" — " (or 60 chars), description = the rest
            m = re.split(r"\s+[-—–]\s+", f, maxsplit=1)
            if len(m) == 2:
                topic, desc = m
            else:
                topic, desc = f[:60], f
            topic = topic.strip().rstrip(":").strip()[:60]
            desc = f"{desc.strip()} (screen: {page_label}, {viewport_label})"
            line = f"- **Design: {topic}** — {desc} _(design-audit {today})_"
            new_lines.append(line)
            existing_norm.append(norm(line))  # don't duplicate within this same run

        if not new_lines:
            continue
        total_new += len(new_lines)
        new_by_target[name] = new_lines
        report_lines.append(f"### {name} ({ideas_rel})")
        report_lines.extend(new_lines)
        report_lines.append("")

    if shot_failed:
        print(f"\n[WARNING] {len(shot_failed)} screenshots could not be taken: "
              + ", ".join(shot_failed), file=sys.stderr)
    print(f"\nScreens captured: {total_screens}. Not captured: {len(shot_failed)}. Not checked: {len(unchecked)}. "
          f"New findings: {total_new}.\n")
    print("\n".join(report_lines) if report_lines else "(no new findings)")
    if unchecked:
        print(f"\n[WARNING] analysis failed on {len(unchecked)} screens: "
              + ", ".join(unchecked), file=sys.stderr)

    if dry:
        print("\n[--dry] no IDEAS.md files touched, no Telegram sent.")
        return 1 if (unchecked or shot_failed) else 0

    # write findings into each target's IDEAS.md
    for name, new_lines in new_by_target.items():
        ideas_path = root / targets_by_name[name]["ideas_md"]
        ideas_path.parent.mkdir(parents=True, exist_ok=True)
        with ideas_path.open("a", encoding="utf-8") as fh:
            if ideas_path.stat().st_size > 0:
                fh.write("\n")
            fh.write(f"## Design audit {today}\n")
            for line in new_lines:
                fh.write(line + "\n")
        print(f"wrote {len(new_lines)} findings to {ideas_path}")

    cleanup_old_shots()

    # Telegram
    top = []
    for name, lines in new_by_target.items():
        for line in lines[:3]:
            m = re.match(r"^- \*\*Design: ([^*]+)\*\*", line)
            top.append(f"{name}: {m.group(1) if m else line[:60]}")
    top = top[:3]
    tg = f"🎨 Design audit: {total_screens} screens, {total_new} new findings"
    if shot_failed:
        tg += f"\n⚠️ FAILED to capture: {len(shot_failed)} screens — check Playwright"
    if unchecked:
        tg += f"\n⚠️ not checked: {len(unchecked)} screens"
    if top:
        tg += "\n\nTop:\n" + "\n".join(f"- {t}" for t in top)
    try:
        subprocess.run(["python3", str(notify), tg], timeout=40)
    except Exception as e:
        print(f"telegram error: {e}", file=sys.stderr)

    # an unchecked OR uncaptured screen is a run failure, not "clean"
    return 1 if (unchecked or shot_failed) else 0


if __name__ == "__main__":
    raise SystemExit(run(dry="--dry" in sys.argv[1:]))
