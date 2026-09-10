# loop-nightly — an autonomous nightly worker

Overnight, on its own: takes safe tasks from a backlog -> does them (a coding agent in a
git worktree) -> a judge checks the result -> opens a PR. In the morning: a voice +
text briefing. Maturity levels and every rule live in `policy.py` (ships at P0: docs/tests
only, PR-only, no auto-merge).

## Flow
`nightly.sh` (meant to run once a night, via `loop-status/loop-run.sh`):
1. `collect.py` — builds the candidate pool: an idea inbox (`track='dev'`) + each
   project's own `IDEAS.md` + ⚠️-findings from `loop-supervisor`'s report; a "needs a
   human" section of the shared state doc acts as a blacklist.
2. `rank.py` — 🔴 stop-words filter -> an "idea to spec" gate (a local model turns a vague
   backlog note into an actionable spec, or rejects it) -> scoring -> top-3 selected.
3. `run_task.py --once <id>` for each: worktree -> `codex exec` -> tests (if the repo has
   any) -> the judge (`loop-status/evaluate.py`) -> PR. The merge gate
   (`repo_safety.is_safe()` + CI + judge) exists in code but sits behind
   `policy.AUTOMERGE_ENABLED`, which defaults to **off**.

`briefing.sh` (meant to run once each morning): a summary of the night's runs -> text ->
an optional TTS voice clip (best-effort) + text with PR links -> Telegram.

## CLI
```
python3 collect.py [--dry]     # build the candidate pool
python3 rank.py [--dry]        # filter + select
python3 run_task.py --once ID  # one task through to a PR
bash nightly.sh                # the whole night (NIGHTLY_NO_CURFEW=1 for a daytime manual run)
python3 briefing.py [--dry]    # the morning report
```

## Data
`nightly.db`: `candidates` (deduped on source+source_ref) + `runs` (statuses:
building|pr_open|merged|escalated|failed|timeout). Full task logs in `logs/`, voice clips
in `briefs/` (neither checked into git — runtime artifacts).

## Design notes
- **`repo_safety.py`** is the single place that knows which project maps to which repo
  path (`REPO_MAP`) and what makes a diff "safe enough to ever auto-merge" (small,
  free of secret/deploy/destructive-command patterns). Every other file imports it rather
  than re-implementing the check — the reference deployment learned the hard way that
  two copies of a safety check drift apart silently.
- **The idea-to-spec gate fails loud.** If the local model that turns a vague backlog
  note into a concrete spec is unavailable, the candidate is rejected with an explicit
  reason — never silently passed through. A silent pass-through here is exactly the bug
  that let a misconfigured version of this loop report "pool empty" every night for
  weeks, with no ideas ever having actually gone through the actionability gate.
- **PR-only by default.** `AUTOMERGE_ENABLED=False` means every run of this loop stops
  after opening a PR — a human reviews it in the morning. The three-gate merge path
  (`is_safe` + green CI + a passing judge) is real code, exercised by tests, but reaching
  it in production is a deliberate, explicit decision, not a default.

## Rough edges
- A fresh worktree with no `node_modules` — `run_task` runs `npm ci` itself before testing.
- Judge unavailable -> `skipped` (doesn't block); only an explicit `failed` blocks.
- Re-running the same night is safe: an atomic `mkdir` lock, plus a `(night, candidate_id)`
  uniqueness check on `runs`. A concurrent run exits 0; a failure in collect/rank/any task
  makes the overall exit code nonzero, so `loop-status` never shows a false `ok`.

## Honest status
This pattern ran in the reference deployment for about two months: 14 nightly runs (one
opened a PR, twelve escalated to the operator, one timed out), then paused by the operator after diagnosing two
compounding failures — an out-of-date CLI version, and an upstream collector that had
quietly stopped producing candidates. Both were root-caused and fixed; the harness stayed
paused after that by deliberate choice, not because it was broken. Shipped here as a
demonstration of the pattern (spec'd, gated, testable, and observable end to end) rather
than as a claim of heavy unattended mileage.
