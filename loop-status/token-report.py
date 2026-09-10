#!/usr/bin/env python3
"""Weekly token-spend report (ccusage -> Telegram). Shows, for the last 7 days: total $
(API-equivalent) + breakdown by model + the single most expensive day. So the operator
sees where tokens go instead of guessing. Meant to run weekly via loop-run.sh.
The measurement itself is $0 (ccusage reads local Claude Code logs). Requires `ccusage`."""
import json, subprocess, sys
from pathlib import Path

NOTIFY = Path(__file__).resolve().parent / "notify.py"


def main():
    try:
        raw = subprocess.run(["ccusage", "--json"], capture_output=True, text=True, timeout=120)
        data = json.loads(raw.stdout)
    except Exception as e:
        print(f"token-report: ccusage unavailable ({e})", file=sys.stderr)
        return 0  # don't alert on a missing tool — this loop isn't the source of truth for that

    daily = data.get("daily", [])[-7:]
    if not daily:
        print("token-report: no data for the last 7 days"); return 0

    by_model, total = {}, 0.0
    top_day = ("", 0.0)
    for d in daily:
        day_cost = 0.0
        for mb in d.get("modelBreakdowns", []):
            c = mb.get("cost", 0) or 0
            name = (mb.get("modelName") or "?").replace("claude-", "")
            by_model[name] = by_model.get(name, 0) + c
            day_cost += c
        total += day_cost
        date = d.get("date", "?")
        if day_cost > top_day[1]:
            top_day = (date, day_cost)

    lines = [
        "💸 *Tokens, last 7 days* (API-equivalent; included in a flat subscription's limit)",
        f"Total: ~${total:.0f}",
        "",
        "By model:",
    ]
    for name, c in sorted(by_model.items(), key=lambda x: -x[1]):
        lines.append(f"• {name}: ${c:.0f}")
    if top_day[0]:
        lines.append(f"\nPeak: {top_day[0]} (${top_day[1]:.0f})")
    lines.append("\n_Keep the priciest model for judgment calls, delegate routine work down._")
    report = "\n".join(lines)

    print(report)
    try:
        subprocess.run([sys.executable, str(NOTIFY), report], timeout=60)
    except Exception as e:
        print(f"notify error: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
