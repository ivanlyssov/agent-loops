#!/usr/bin/env bash
# Coverage check: every scheduled agent must be under SOME form of supervision.
#
# Why this exists: a real audit of this harness's production deployment found 17
# scheduled agents whose failures produced zero alerts. It wasn't that anyone forgot —
# it's that nothing ever checked the link between "agent exists" and "agent is
# watched". This script checks that link and says so out loud when it's missing.
#
# Three legitimate ways to be supervised:
#   1. loop-run  — ProgramArguments/command goes through loop-run.sh: it records every
#      run and alerts on a nonzero exit;
#   2. daemon    — the service is listed in daemon-watch.sh: liveness is checked, not runs;
#   3. excluded  — deliberately, with a reason string (see below).
# Anything else is a gap, and this script says so.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"
LA="${COVERAGE_LA_DIR:-$HOME/Library/LaunchAgents}"           # overridable for tests
REGISTRY="${COVERAGE_REGISTRY:-$HERE/services.example.toml}"  # see that file's format
LABEL_PREFIX="${LAUNCHD_LABEL_PREFIX:-com.example.agent-loops.}"

# Deliberate exclusions are read from the registry (supervision="excluded"), not from a
# second hard-coded list — keeping one source of truth is the whole point of this check.
REPORT="$(python3 - "$LA" "$HERE" "$REGISTRY" "$LABEL_PREFIX" <<'PY'
import plistlib, pathlib, re, sys, tomllib

la, here, registry, prefix = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), sys.argv[4]
reg = tomllib.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {}
excluded = {name: entry.get("why", "(no reason given in the registry)")
            for name, entry in reg.items() if entry.get("supervision") == "excluded"}

# who daemon-watch is already watching — read from it directly so the two lists can't drift apart
dw = (here / "daemon-watch.sh").read_text(encoding="utf-8")
block = re.search(r'DAEMONS="(.*?)"', dw, re.S)
daemons = {l.split(":")[0].strip() for l in (block.group(1).splitlines() if block else []) if l.strip()}

run_sh = str(here / "loop-run.sh")
uncovered, by_mode = [], {"loop-run": 0, "daemon": 0, "excluded": 0}

for f in sorted(la.glob(prefix + "*.plist")):
    name = f.stem[len(prefix):]
    try:
        args = plistlib.loads(f.read_bytes()).get("ProgramArguments", [])
    except Exception as e:
        uncovered.append(f"{name} — plist unreadable: {e}")
        continue
    # loop-run.sh is invoked two ways (directly, or through /bin/bash) — accept both, but
    # the label argument right after the path must match the agent's own name, or
    # monitoring and the scheduler have silently drifted apart.
    idx = next((i for i, a in enumerate(args[:3]) if str(a).endswith("loop-run.sh")), None)
    if idx is not None:
        label = args[idx + 1] if len(args) > idx + 1 else ""
        if label != name:
            uncovered.append(f"{name} — runs through loop-run, but under label \"{label}\": "
                             f"monitoring and the scheduler have drifted apart")
            continue
        by_mode["loop-run"] += 1
    elif name in daemons:
        by_mode["daemon"] += 1
    elif name in excluded:
        by_mode["excluded"] += 1
    else:
        uncovered.append(f"{name} — not in loop-run, daemon-watch, or the exclusion list")

total = sum(by_mode.values()) + len(uncovered)
print(f"TOTAL {total}: loop-run {by_mode['loop-run']} · daemons {by_mode['daemon']} · "
      f"excluded {by_mode['excluded']} · UNSUPERVISED {len(uncovered)}")
for u in uncovered:
    print("• " + u)
sys.exit(1 if uncovered else 0)
PY
)"
RC=$?

echo "$REPORT"
if [ "$RC" -ne 0 ]; then
  python3 "$HERE/notify.py" "🔴 agent-loops: unsupervised agents appeared

$REPORT

Fix: route it through loop-run.sh, add it to daemon-watch.sh,
or add it to the registry with supervision=\"excluded\" and a why." 2>/dev/null || true
fi
exit $RC
