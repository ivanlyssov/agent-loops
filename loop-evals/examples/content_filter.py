#!/usr/bin/env python3
"""Example content-relevance filter — the kind of small classifier a feed-ingestion loop
runs on every incoming item to decide keep/drop before it reaches a digest or a knowledge
base. This is real, runnable code (not a mock): `run_evals.py`'s `content-filter` adapter
imports `ollama_batch` from this module directly, the same way a production eval harness
should import the actual loop code rather than re-implement its prompt for the test. Swap
this module for your own loop's real filter and the eval wiring doesn't change.
"""
import json
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/generate"

PROMPT = """You curate a knowledge feed for someone running a few small projects. Decide
whether this item is worth keeping.

KEEP: a concrete tool, technique, or insight with enough detail to actually use (a
config, a number, a step-by-step, a named product/feature).
DROP: an ad, a course/webinar pitch, a giveaway, bare motivational text, a vague news
headline with no actionable detail, or a repost/announcement with nothing substantive.

Item:
---
{text}
---

Answer strictly JSON: {{"relevant": true|false}}"""


def classify_one(text: str, model: str) -> dict | None:
    body = json.dumps({
        "model": model, "prompt": PROMPT.format(text=text[:2000]),
        "format": "json", "stream": False, "options": {"temperature": 0},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        raw = urllib.request.urlopen(req, timeout=60).read()
        return json.loads(json.loads(raw).get("response", "{}"))
    except Exception:
        return None


def ollama_batch(posts: list[tuple[int, str]], model: str) -> dict | None:
    """posts: list of (index, text). Returns {index: {"relevant": bool}}, or None if the
    model is completely unreachable (caller treats that as "skip this eval run")."""
    out = {}
    reached = False
    for i, text in posts:
        r = classify_one(text, model)
        if r is not None:
            reached = True
            out[i] = r
    return out if reached else None
