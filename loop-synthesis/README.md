# loop-synthesis — knowledge synthesis from a vault (P4)

Closes the "green" loop for knowledge. Feeds drop **raw material** all day (daily digests
from whatever sources you feed in), and once a week that needs to become an **insight**:
what actually matters and is worth acting on — not a pile of raw bullets nobody reads.

## What it does
1. Collects daily digests from the last 7 days across a configurable set of source
   folders, pulls bullets+headings (no frontmatter/tags), caps at ~12k characters.
2. Synthesizes with a **cheap hosted model** (configurable, public/low-cost data — set
   `OPENROUTER_SCRIPT`; falls back to a **local model**, $0) into a weekly insight for the
   operator's projects: `🔑 Main themes · 🎯 Worth acting on (by project) · 👀 On the radar`.
3. Writes a curated note into the vault (`<vault>/sources/weekly-synthesis/synthesis-YYYY-MM-DD.md`) + sends it to Telegram.

Internal/reversible -> 🟢 **no approval gate** (this never goes outward).

## Running it
- Needs `AGENT_LOOPS_HOME` (see the repo-root README). Vault location defaults to
  `$AGENT_LOOPS_HOME/knowledge-vault`, override with `KNOWLEDGE_VAULT`.
- Manually: `python3 synthesize.py` · stdout-only: `--dry` · a different model: `--model <name>`.

## Rough edges
- No material in the window -> a silent skip (never writes an empty note).
- The hosted model is unavailable/errors -> automatic fallback to the local model. Both
  down -> a note is still written, marked "synthesis unavailable".
- The collection window is keyed off the date in each filename (`*-YYYY-MM-DD.md`), with
  an mtime fallback. Add a new source by adding its folder name to `FEED_DIRS`.
