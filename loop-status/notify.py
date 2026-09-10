#!/usr/bin/env python3
"""Shared Telegram notifier for the loop harness. Credentials come from the environment
(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) or a .env file at the repo root — see .env.example.
CLI: echo "text" | python3 notify.py   OR   python3 notify.py "text"
Voice:            python3 notify.py --voice /path.mp3 "caption"
Flush a digest:   python3 notify.py --flush   — combine everything spooled into ONE message and send it
Returns 0 on success, 1 if no credentials / an error (so the caller doesn't crash on it).

Quiet mode (introduced to fight message-flood): if NOTIFY_SPOOL=1 is set, send() doesn't
send immediately — it appends to spool/daily.jsonl. Two scheduled jobs (morning/evening)
call --flush and send everything accumulated as one message. loop-run.sh decides who gets
spooled vs. sent immediately, based on notify-policy.json. Urgent (P0/P1) alerts bypass
the spool."""
import sys, os, json, time, mimetypes, uuid
from pathlib import Path

# The retry policy lives once, for the whole repo — see common/tg_post.py. Path is
# resolved from this file, not from $HOME: absolute paths have broken before when a
# tree gets moved.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))
from tg_post import post as _post  # noqa: E402
from config import env_or_dotenv  # noqa: E402

SPOOL = Path(__file__).resolve().parent / "spool" / "daily.jsonl"

def _creds():
    cfg = env_or_dotenv("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    token, chat = cfg.get("TELEGRAM_BOT_TOKEN"), cfg.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("notify: no TELEGRAM_BOT_TOKEN/CHAT_ID (env or .env)", file=sys.stderr); return None, None
    return token, chat

def send(text: str, force: bool = False) -> bool:
    """force=True — send immediately, bypassing the spool (used by --flush and urgent alerts)."""
    if not force and os.environ.get("NOTIFY_SPOOL"):
        SPOOL.parent.mkdir(parents=True, exist_ok=True)
        with SPOOL.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()),
                                "label": os.environ.get("NOTIFY_LABEL", "?"),
                                "text": text.strip()}, ensure_ascii=False) + "\n")
        return True
    token, chat = _creds()
    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for i in range(0, len(text), 3800):
        body = json.dumps({"chat_id": chat, "text": text[i:i+3800],
                           "disable_web_page_preview": True}).encode("utf-8")
        if not _post(url, body, {"Content-Type": "application/json"}, tag="notify"):
            ok = False
    return ok

def send_voice(mp3_path: str, caption: str = "") -> bool:
    """sendVoice: multipart/form-data, field voice=file, caption truncated to 1024 chars."""
    token, chat = _creds()
    if not token:
        return False
    path = Path(mp3_path)
    if not path.is_file():
        print(f"notify: file not found: {mp3_path}", file=sys.stderr); return False
    caption = (caption or "")[:1024]
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "audio/mpeg"

    def field(name, value):
        return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode("utf-8")

    parts = [field("chat_id", chat)]
    if caption:
        parts.append(field("caption", caption))
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"voice\"; filename=\"{path.name}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n".encode("utf-8")
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    url = f"https://api.telegram.org/bot{token}/sendVoice"
    return _post(url, body,
                 {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                 timeout=120, tag="notify")

def flush() -> bool:
    """Combine everything spooled into one message. Nothing spooled → stay silent (no "all ok" spam)."""
    if not SPOOL.exists():
        return True
    items = []
    for line in SPOOL.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                items.append(json.loads(line))
            except Exception:
                pass
    if not items:
        return True
    by_label = {}
    for it in items:
        by_label.setdefault(it.get("label", "?"), []).append(it.get("text", ""))
    when = time.strftime("%H:%M")
    parts = [f"🗂 Digest ({when}) — {len(items)} events from {len(by_label)} sources\n"]
    for label, texts in sorted(by_label.items()):
        parts.append(f"\n▸ {label}")
        for t in texts:
            t = " ".join(t.split())
            parts.append(f"   {t[:400]}{'…' if len(t) > 400 else ''}")
    ok = send("\n".join(parts), force=True)
    if ok:  # only archive after a successful send — otherwise we'd lose what accumulated
        arch = SPOOL.parent / "sent"
        arch.mkdir(parents=True, exist_ok=True)
        SPOOL.rename(arch / f"{time.strftime('%Y-%m-%dT%H%M%S')}.jsonl")
    return ok

if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        if args and args[0] == "--flush":
            sys.exit(0 if flush() else 1)
        if args and args[0] == "--voice":
            if len(args) < 2:
                print("notify: needs an mp3 path: --voice <mp3> [caption]", file=sys.stderr)
                sys.exit(1)
            mp3 = args[1]
            caption = args[2] if len(args) > 2 else ""
            sys.exit(0 if send_voice(mp3, caption) else 1)
        msg = args[0] if args else sys.stdin.read()
        sys.exit(0 if send(msg.strip()) else 1)
    except Exception as e:
        print(f"notify: fatal error: {e}", file=sys.stderr)
        sys.exit(1)
