# loop-status — observability for the harness (P0 foundation)

Part of the loop architecture (see the repo-root README), phase **P0**. Goal: no scheduled
task's failure gets lost silently, ever again. Every run is logged; an error or a long
silence triggers a Telegram alert.

## Components
- **`notify.py`** — Telegram notifier (credentials from env / `.env`). `python3 notify.py "text"`.
- **`status.py`** — run log (SQLite `status.db`):
  - `record <label> <exit> [tail_file]` — record one run
  - `summary` — status of every known task (state, age, total errors)
  - `last <label>` — the last run + its tail on failure
  - `overdue <hours> <label...>` — labels with no success in the window (for the watchdog; skips labels that have never run)
- **`loop-run.sh <label> [--verify "<criterion>"] -- <command…>`** — the runner wrapper: runs
  the command, captures exit code + output tail, records status, alerts Telegram on
  `exit≠0`. With `--verify` it calls the judge after a successful exit (see below). Every
  supervised scheduled task goes through this.
- **`evaluate.py`** — the judge (worker/evaluator): a local model checks the success
  criterion against the log tail, not just the exit code. See "The judge" below.
- **`loop-watchdog.sh`** — catches "a task hasn't completed successfully in N hours"
  (machine asleep / scheduler didn't fire / a string of failures). Meant to run daily.
- **`daemon-watch.sh`** — the same idea for long-running services (not one-shot tasks):
  checks a PID is alive, or that the last exit was clean.
- **`coverage-check.sh`** — meta-check: every scheduled agent must be routed through
  loop-run, listed in daemon-watch, or explicitly excluded with a reason.
- **`grade.py`** + **`rubrics/*.json`** — self-grading of artifact quality against small,
  atomic, weighted rubrics (see the repo-root README's P5 section).
- **`cost.py`** — a $0-friendly ledger of what AI calls actually cost, by channel/model/task.

## Checking status
```
python3 loop-status/status.py summary
python3 loop-status/status.py last-verdict <label>
```

## Putting a task under supervision
Wrap its launch command:
`/bin/bash …/loop-status/loop-run.sh <label> -- <old command…>`
Add the label to the right bucket (`DAILY`/`FREQUENT`/`WEEKLY`) in `loop-watchdog.sh`.

## The judge (worker/evaluator)

The problem: an exit code of 0 doesn't mean "the task actually did what it was supposed
to" — a script can swallow an error and exit clean. The judge is a second, independent
look: a cheap local model reads the success criterion plus the log tail and decides
whether the criterion was met in substance, not just by return code.

**Component:** `evaluate.py <label> <criterion|criterion-file> <log-tail-file>` — prints
JSON `{"verdict": "passed"|"failed"|"skipped", "passed": true|false|null, "reason": "..."}`.
120s timeout. Calls the model with `format: json`, `temperature: 0.1` (low, for verdict
consistency between runs). If the model is unavailable/broken → `verdict: skipped` — the
judge is advisory, it never fails the task and is never counted as an error.

**Wiring it in:** the `--verify "<one-sentence criterion>"` flag on `loop-run.sh` (a path
to a file with the criterion also works — `evaluate.py` figures out which it got):
```
loop-run.sh <label> --verify "<success criterion>" -- <command...>
```
The judge only runs after a successful exit code. Without `--verify`, `loop-run.sh`'s
behavior is unchanged (fully backward compatible).

**Storage:** a separate `verdicts` table in `status.db` (doesn't touch the `runs` schema):
`label, ts, verdict, passed, reason`.

**On `passed=false`** — `loop-run.sh` sends a separate Telegram alert via `notify.py`:
`⚠️ <label>: exit 0, but the judge did not confirm success: <reason>`. The run's exit code
is unchanged — this is an alert, not a pipeline gate. `status.py summary` still shows that
label as `error` though: advisory for the pipeline, but not hidden from the operator's view.

**Rough edges:** small local models are sensitive to prompt wording — vague instructions
give inconsistent verdicts on the same log. The working prompt in `evaluate.py` is
step-by-step (explicit failure signals first, semantic match second) at
`temperature: 0.1`; change it carefully, testing on known good/bad logs before deploying.

## Rough edges
- `status.db` is a local SQLite file, not checked into git (runtime data).
- Alerts go to Telegram (`notify.py`) — one bot, or split traffic between an
  "immediate" bot and a digest bot per `notify-policy.json`.
