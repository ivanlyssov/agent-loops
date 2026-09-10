#!/usr/bin/env python3
"""Shared configuration helpers for agent-loops.

Design rule: every script's OWN files (its database, its rubrics, its logs) live next
to it and are found via ``Path(__file__).resolve().parent`` — no environment variable
needed. Only paths that reach OUTSIDE this repo (another project's task backlog, a
knowledge vault, a shared status doc) go through ``deployment_root()`` below, which
reads exactly one environment variable: ``AGENT_LOOPS_HOME``.

There is no silent default. A script that needs a cross-repo path and doesn't have one
exits loudly with a clear message, instead of guessing a path or creating directories
in the wrong place. This mirrors a real lesson from running this harness in production:
hard-coded absolute paths silently broke every time the surrounding tree moved.
"""
import os
import sys
from pathlib import Path


def deployment_root() -> Path:
    """Root of the wider deployment this harness supervises.

    Only the cross-loop scripts need this: loop-supervisor, loop-synthesis,
    loop-nightly and loop-design-audit all look at *other* repositories/documents
    (a task backlog, a knowledge base, a shared STATE.md) that live outside this
    repo. Point ``AGENT_LOOPS_HOME`` at whatever root directory holds those for
    your setup — see .env.example for the shape it expects.
    """
    v = os.environ.get("AGENT_LOOPS_HOME")
    if not v:
        sys.exit(
            "AGENT_LOOPS_HOME is not set. Point it at the root directory this "
            "harness should supervise (see .env.example). Refusing to guess a path."
        )
    root = Path(v).expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"AGENT_LOOPS_HOME={root} does not exist.")
    return root


def env_or_dotenv(*keys: str, dotenv_path: Path | None = None) -> dict:
    """Read ``keys`` from the process environment, falling back to a plain
    ``KEY=value`` .env file (repo root by default). No external dependency.

    Returns a dict with one entry per requested key (value is ``None`` if not found
    anywhere). Process environment always wins over the .env file.
    """
    out = {k: os.environ.get(k) for k in keys}
    if all(out.values()):
        return out
    path = dotenv_path or (Path(__file__).resolve().parents[1] / ".env")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k in keys and not out.get(k):
                out[k] = v.strip().strip('"').strip("'")
    return out


def optional_path(env_var: str, default: Path | None = None) -> Path | None:
    """A path that's genuinely optional (feature degrades gracefully if absent).

    Unlike ``deployment_root()`` this never exits — callers are expected to check
    for ``None``/non-existence themselves and skip the feature.
    """
    v = os.environ.get(env_var)
    if v:
        return Path(v).expanduser().resolve()
    return default
