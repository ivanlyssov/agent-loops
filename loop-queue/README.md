# loop-queue — batch approval gate (P1)

Part of the loop architecture (see the repo-root README), phase **P1**. Loops never act
outward immediately — they drop a proposal into the queue. Once a day, a human sees the
batch in Telegram and taps ✅/❌. An approved item is executed by the worker.

## Flow
```
a loop → queue.py add → [pending] → morning digest (✅/❌ buttons) → human taps
   → poller catches the callback → approve: worker executes the argv → done | reject → rejected
```

## Components
- **`queue.py`** — the queue's core (SQLite `queue.db`):
  - `add --kind K --title T --summary S --argv PROG ARG... [--risk low|high]` — enqueue (prints id). **This is how loops drop outward-facing actions in.**
  - `pending` · `decide <id> approve|reject` · `worker` (execute approved items) · `expire [days]` · `get <id>`
  - Execution is an argv list, **never through a shell** (no injection). Need a pipe? Wrap it in a script and queue the path.
- **`queue-bot.py`** — Telegram side (its own bot, credentials from env/`.env`):
  - `digest` — send pending items with inline buttons (run once a day; auto-`expire`s items >3 days old)
  - `poll` — long-running poller for button presses, a SINGLETON via flock (KeepAlive-style process).
    On approve it executes the item immediately and edits the message with the result.

## Why a separate bot
`getUpdates` is exclusive — one receiver per token. If a P0 alert bot is already polled
24/7 somewhere else, this loop would get 409 Conflict and button presses would be lost.
Hence a dedicated bot for the queue (`QUEUE_BOT_TOKEN`/`QUEUE_CHAT_ID` in `.env`, chmod 600,
gitignored). P0 alerts keep using the other bot — only sending, so it doesn't conflict.

## How a loop enqueues an action
```bash
python3 loop-queue/queue.py add \
  --kind content --title "Post: Tuesday promo" \
  --summary "IG post, visual ready, prices checked" \
  --risk high \
  --argv python3 /path/publish_instagram.py --draft 42
```
The item shows up in the next morning's batch; on ✅ this exact argv runs.

## Rough edges
- `risk: high` shows with 🔴 and its own line — for money/deletions/publishing.
- `queue.db` / `.env` / `.tg_offset` / `.poller.lock` are not checked into git.
- Only one poller per bot (the flock enforces it). Running a second poller against the
  same bot produces 409s.
- The queue bot's chat_id is fixed to one chat — presses from anywhere else are rejected.
