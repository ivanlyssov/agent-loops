# loop-evals — the loops' "exam" (triggering evals)

Catches a loop's filter/prompt degrading BEFORE junk reaches production output. Runs a
frozen set of examples (a golden set) through the **actual production code** of the loop
(an imported function, not a copy of its prompt) and compares the result to a recorded
baseline.

## In one sentence
The filter has an exam made of old questions with known answers. Change its brain
(prompt/code) and it re-sits the exam. Fail it, and you find out immediately — not a
month later when the digest quietly turns into noise.

## Usage
```
python3 run_evals.py content-filter                  # exam for one loop
python3 run_evals.py --all                            # every loop with cases/
python3 run_evals.py content-filter --set-baseline    # record a new baseline (deliberately!)
```

## How it's built
- `cases/<label>.jsonl` — cases as `{"id","input","expect":"keep"|"drop"}` (15-30 of them).
- `baselines.json` — the accuracy each label is expected to hit; degradation = below
  baseline - 0.10 -> exit 1 + Telegram alert. Not checked into git — `baselines.example.json`
  ships instead, with the same numbers this repo's own example case set produces.
- Results land in `loop-status/status.db`, table `evals` (read by `loop-supervisor/quality.py`).
- Local model unreachable -> skipped (advisory, doesn't block anything).
- `examples/content_filter.py` is a small, real, runnable filter bundled with this repo so
  the `content-filter` adapter has genuine production code to import — swap it for your own
  loop's real filter and the eval machinery is unchanged.

## Triggers
1. **git post-commit** in the repo whose loop code changed — an immediate exam.
2. **Weekly**, from `loop-supervisor/quality.py` — catches environment drift (the local
   model got updated and behavior shifted even though the code didn't change).
3. Manually, before flipping a loop into unattended mode.

## Adding coverage
Add an adapter function to `ADAPTERS` in `run_evals.py` that imports your loop's real
classify/filter function, then add `cases/<label>.jsonl`. Rejections from the approval
queue and low-grade artifacts are natural sources of new cases. Only update a baseline
deliberately, after reviewing why accuracy moved.
