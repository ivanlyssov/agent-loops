#!/usr/bin/env python3
"""Telegram bot for the batch approval queue: a morning digest with ✅/❌ buttons, plus a
poller for button presses. Uses its OWN bot (QUEUE_BOT_TOKEN/QUEUE_CHAT_ID) — deliberately
separate from any P0 alert bot, because getUpdates is exclusive per token and an alert
bot may already be polled elsewhere. The poller is a SINGLETON (flock): you cannot run two
getUpdates loops against the same token.

  queue-bot.py digest   — send pending items with buttons (run once a day)
  queue-bot.py poll      — long-running poller for button callbacks (KeepAlive)
"""
import sys, json, time, fcntl, urllib.request, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))
import tg_post  # noqa: E402
from config import env_or_dotenv  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import queue as Q

OFFSET_FILE = HERE / ".tg_offset"
LOCK_FILE = HERE / ".poller.lock"

def creds():
    """Queue bot token/chat from env or .env (QUEUE_BOT_TOKEN/QUEUE_CHAT_ID) — a
    dedicated bot, since getUpdates is exclusive and an alert bot may be polled elsewhere."""
    cfg = env_or_dotenv("QUEUE_BOT_TOKEN", "QUEUE_CHAT_ID")
    token, chat = cfg.get("QUEUE_BOT_TOKEN"), cfg.get("QUEUE_CHAT_ID")
    if not token or not chat:
        sys.exit("queue-bot: no QUEUE_BOT_TOKEN/QUEUE_CHAT_ID (env or .env) — create a bot via @BotFather")
    return token, chat

TOKEN, CHAT = creds()
API = f"https://api.telegram.org/bot{TOKEN}/"

def api(method, params, timeout=35):
    """Bot API call with retries (network/5xx/429 — shared common/tg_post.py).

    raise_http=True: poll() needs the real status code — it waits 30s on 409
    (a second getUpdates on the same bot) and 10s on anything else. Swallowing the
    exception here would spin an empty loop without a pause and hammer Telegram.
    """
    r = tg_post.call(API, method, params, timeout=timeout, json_body=True,
                     tag=f"queue {method}", raise_http=True)
    return r if r is not None else {}

def kb(pid):
    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"q:approve:{pid}"},
        {"text": "❌ Reject", "callback_data": f"q:reject:{pid}"}]]}

def card(p):
    risk = "🔴 RISK" if p["risk"] == "high" else "🟢"
    return f"{risk} <b>{p['title']}</b>\n<i>{p['kind']}</i>\n\n{p['summary']}\n\n#{p['id']}"

def digest():
    Q.expire(3)                                        # expire stale pending items (>3 days)
    items = Q.pending()
    if not items:
        api("sendMessage", {"chat_id": CHAT, "text": "☀️ The approval queue is empty — nothing outward-facing is waiting."})
        print("digest: empty"); return
    api("sendMessage", {"chat_id": CHAT, "text": f"☀️ Morning batch: {len(items)} to decide. Tap ✅/❌:"})
    for p in items:
        r = api("sendMessage", {"chat_id": CHAT, "text": card(p), "parse_mode": "HTML", "reply_markup": kb(p["id"])})
        if r.get("ok"):
            Q.set_msg(p["id"], r["result"]["message_id"])
    print(f"digest: sent {len(items)}")

def read_offset():
    try: return int(OFFSET_FILE.read_text())
    except Exception: return 0

def handle_callback(cbq):
    data = cbq.get("data", "")
    cid = cbq["id"]; msg = cbq.get("message", {}); chat_id = msg.get("chat", {}).get("id")
    if str(chat_id) != str(CHAT):                      # only accept presses from the configured chat
        api("answerCallbackQuery", {"callback_query_id": cid, "text": "not allowed"}); return
    if not data.startswith("q:"):
        api("answerCallbackQuery", {"callback_query_id": cid}); return
    _, action, pid = data.split(":"); pid = int(pid)
    p = Q.get(pid)
    if not p or p["status"] != "pending":
        api("answerCallbackQuery", {"callback_query_id": cid, "text": "already decided"})
        return
    Q.decide(pid, action)
    if action == "approve":
        api("answerCallbackQuery", {"callback_query_id": cid, "text": "✅ running…"})
        ok, out = Q.run_one(pid)
        tail = (out or "")[-300:]
        new = f"✅ <b>{p['title']}</b> — {'done' if ok else 'FAILED'}\n#{pid}" + (f"\n<code>{tail}</code>" if not ok else "")
    else:
        api("answerCallbackQuery", {"callback_query_id": cid, "text": "❌ rejected"})
        new = f"❌ <b>{p['title']}</b> — rejected\n#{pid}"
    try:
        api("editMessageText", {"chat_id": chat_id, "message_id": msg["message_id"], "text": new, "parse_mode": "HTML"})
    except Exception: pass

def poll():
    lf = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("poll: another poller is already running — exiting"); return
    offset = read_offset()
    print(f"poll: starting, offset={offset}")
    while True:
        try:
            r = api("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["callback_query"]}, timeout=35)
            if not r:                                  # retries exhausted, no exception raised
                print("poll: getUpdates failed; pausing 10s"); time.sleep(10); continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                if "callback_query" in u:
                    handle_callback(u["callback_query"])
            OFFSET_FILE.write_text(str(offset))
        except urllib.error.HTTPError as e:
            if e.code == 409:
                print("poll: 409 conflict — another getUpdates on this bot; pausing 30s"); time.sleep(30)
            else:
                print(f"poll HTTP {e.code}; pausing 10s"); time.sleep(10)
        except Exception as e:
            print(f"poll error: {e}; pausing 10s"); time.sleep(10)

if __name__ == "__main__":
    if len(sys.argv) < 2: print(__doc__); sys.exit(1)
    {"digest": digest, "poll": poll}.get(sys.argv[1], lambda: (print(__doc__), sys.exit(1)))()
