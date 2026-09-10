# loop-design-audit — weekly design audit of live screens

Once a week, LOOKS at live screens (screenshots + a local vision model) instead of just
checking "the page returns 200". Visual findings (hierarchy, contrast, spacing, mobile
clipping, consistency) are appended to each project's design backlog as `Design:` lines
in its `IDEAS.md`.

## Flow

1. `audit.py` walks `targets.json` (config, see `targets.example.json`) and takes 2
   screenshots per page: mobile (390x844) and desktop (1440x900), via `npx playwright screenshot`.
2. Each screenshot goes to a **local vision model** (`localhost:11434/api/generate`,
   $0) with a checklist: hierarchy/readability, contrast, spacing/alignment, empty states,
   mobile clipping, consistency. Asks for at most 5 concrete issues, no fluff.
3. Findings are deduped against existing `Design:` lines in that target's `IDEAS.md`
   (fuzzy substring match) — so the same issue doesn't get logged every week.
4. New findings are appended to `$AGENT_LOOPS_HOME/<ideas_md>` in a format
   `loop-nightly/collect.py` parses:
   ```
   - **Design: <topic>** — <description> _(design-audit YYYY-MM-DD)_
   ```
5. A report goes to Telegram (`loop-status/notify.py`): `🎨 Design audit: N screens, M new findings`.
6. Screenshots land in `shots/<date>/`, retained 30 days (older folders are deleted automatically).

## CLI

```
python3 audit.py            # full run: writes IDEAS.md files + Telegram
python3 audit.py --dry      # captures and analyzes, but writes and sends NOTHING — stdout only
```

## Adding a target

Copy `targets.example.json` to `targets.json` (gitignored) and add an entry:
```json
{
    "name": "short-id-for-logs",
    "url": "https://prod-domain-or-localhost:port",
    "ideas_md": "path/relative/to/AGENT_LOOPS_HOME/IDEAS.md",
    "pages": ["/", "/some-page"]
}
```

## Running it
- Needs `AGENT_LOOPS_HOME` set (see the repo-root README).
- Manually: `./audit.sh` or `python3 audit.py`.

## Rough edges
- Playwright runs via `npx` (no global install) — the first run may need to download a
  browser: `npx playwright install chromium`.
- One screenshot's vision analysis takes ~30-60s — a long `targets`/`pages` list can make
  a run take a few minutes. That's fine, this loop is weekly.
- If the model answers "No issues found", nothing is written (no noise for noise's sake).
- Dedup is a fuzzy substring match on the first ~25-40 characters of a finding; if the
  model phrases the same issue very differently week to week, near-duplicates can slip
  through. Manual cleanup of `IDEAS.md` as needed.
