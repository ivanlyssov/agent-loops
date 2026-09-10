# loop-supervisor — meta-supervisor (P3)

A weekly self-check across the autonomous loops. Eyes over "set it and forget it", so the
infrastructure doesn't rot silently.

## What it checks (deterministic, $0, stdlib)
1. **Scheduler drift** — `launchctl list` vs `baseline.json`. A service missing from the baseline -> 🔴 regression.
2. **Run statuses** — `loop-status/status.db`: a task with no success in its own window (`CADENCE_H`) -> 🔴; last run was an error but a success was still in-window -> ⚠️.
3. **Approval gate** — `loop-queue/queue.db`: pending items stuck >3d, expirations over 7d, worker failures (🔴), reject-rate >60% over 30d (filter drift).
4. **Disk** — <10GB -> 🔴, <25GB -> ⚠️.
5. **Knowledge base liveness** — new `.md` files in the last 7d; 0 -> ⚠️ (feeds/imports died quietly).
6. **Doc drift** — freshness of a shared state doc (>7d -> ⚠️) and a task backlog (>14d -> ⚠️), broken local links in the agent-instructions docs (-> ⚠️, up to 10 listed), stale path references in live docs outside archive/cleanup folders (-> ⚠️).

Findings bucket into 🔴 regression / ⚠️ attention / ✅ facts. A short verdict is optionally
added via a **local model** (doesn't block). **Never fixes anything automatically** — it
only reports. A regression shows up as its own line.

## Where the report goes
- **Telegram**, via `loop-status/notify.py` — compact, regressions/attention separated.
- **A shared state doc** (`$AGENT_LOOPS_HOME/docs/STATE.md`) — the block between `<!-- SUPERVISOR:BEGIN/END -->` is replaced, not appended (doesn't grow forever). Optional: if the file doesn't exist, this step is skipped.
- **`last-report.md`** — the full latest report.

## Running it
- Needs `AGENT_LOOPS_HOME` set — see the repo-root README and `.env.example`.
- Manually: `python3 supervisor.py` · without Telegram/state-doc: `--dry` · without the local-model narrative: `--no-llm`.

## Files
- `supervisor.py` — the checks + report assembly.
- `quality.py` — second leg: quality trends (grades/evals/queue reject-rate + an artifact sample). Called from `supervisor.py`, runnable standalone too.
- `baseline.json` — snapshot of the expected set of scheduled services (self-updates with additions; disappearances keep the alert). Not checked into git — `baseline.example.json` shows the shape; the real file is created on first run.
- `last-report.md` — the last report (not checked into git; regenerated each run).

## Rough edges
- **baseline:** the first run just records the current set; drift is caught starting the second run. A newly-added wanted service gets folded into the baseline automatically (shown as a "note"); an unwanted disappearance stays 🔴 until you either bring it back or edit the baseline by hand.
- **CADENCE_H** — freshness windows per task. Add a new daily task to the dict (the 30h default works fine too).
- Reacting to a regression is still a human's job (Telegram is the only channel). Auto-filing a fix into the approval queue is a deliberate non-goal — no auto-repair, ever.
