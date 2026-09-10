#!/usr/bin/env python3
"""Shared Telegram Bot API client with a retry policy, used by every notifier in this repo.

Why this exists as one module instead of an inline ``urlopen`` per script: a single
unretried request is exactly the kind of thing that fails silently — a dropped
connection or a transient 5xx eats the alert and nobody finds out until the outage
that alert was supposed to catch. This module is imported everywhere a message goes
out (loop-status/notify.py, loop-queue/queue-bot.py) instead of being copy-pasted, so
the policy only has one place to go stale.

Retry policy — what we retry and what we don't:
  * network errors / timeouts and 5xx — retried: the service or channel is likely
    down only temporarily;
  * 429 — retried, honoring the ``retry_after`` Telegram sends back;
  * other 4xx (400 bad request, 403 bot was blocked) — NOT retried: that's a mistake
    in the request itself, and retrying would just reproduce the same failure. It is
    still printed to stderr so it doesn't read as a silent success.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ATTEMPTS = 4
DEFAULT_PAUSE = 3.0
MAX_TEXT = 3800          # Telegram's hard limit is 4096; leave room for escaping


def request(url, data=None, headers=None, timeout=30,
            attempts=DEFAULT_ATTEMPTS, pause=DEFAULT_PAUSE, tag="tg",
            quiet=False, raise_http=False):
    """Perform one Bot API request under the retry policy described above.

    Returns the parsed JSON response (dict) on success, ``None`` on failure. ``None``
    is the only "not delivered" signal — callers must check for it, otherwise you get
    exactly the silent loss this module exists to prevent.

    quiet=True suppresses the per-attempt log lines (useful for long-running pollers
    that track their own consecutive-failure count — otherwise a flaky network fills
    the log with the same line every few seconds). A non-retryable 4xx is still
    printed regardless — that one must never disappear silently.

    raise_http=True re-raises a non-retryable HTTPError instead of returning None,
    for callers that need to distinguish status codes (e.g. a poller treating 409
    "another poller is already running" differently from anything else).
    """
    req = urllib.request.Request(url, data=data, headers=headers or {})
    last = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            try:
                return json.loads(raw) if raw else {"ok": True}
            except ValueError:
                return {"ok": True}          # non-JSON body, but the request went through
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code == 429:
                wait = pause
                try:
                    wait = float(json.loads(body).get("parameters", {}).get("retry_after", pause))
                except Exception:
                    pass
                if attempt < attempts:
                    if not quiet:
                        print(f"{tag}: 429, waiting {wait:.0f}s ({attempt}/{attempts})", file=sys.stderr)
                    time.sleep(min(wait, 60)); last = e; continue
            elif 500 <= e.code < 600 and attempt < attempts:
                if not quiet:
                    print(f"{tag}: HTTP {e.code}, retry {attempt}/{attempts}", file=sys.stderr)
                time.sleep(pause); last = e; continue
            print(f"{tag}: HTTP {e.code} — not retrying: {body}", file=sys.stderr)
            if raise_http:
                raise
            return None
        except Exception as e:
            last = e
            if attempt < attempts:
                if not quiet:
                    print(f"{tag}: {e} — attempt {attempt}/{attempts}, waiting {pause:.0f}s", file=sys.stderr)
                time.sleep(pause)
    if not quiet:
        print(f"{tag}: not delivered after {attempts} attempts: {last}", file=sys.stderr)
    if raise_http and last is not None:
        raise last
    return None


def post(url, data, headers, timeout=30,
         attempts=DEFAULT_ATTEMPTS, pause=DEFAULT_PAUSE, tag="tg"):
    """Same request, but the caller only needs "delivered / not delivered"."""
    return request(url, data, headers, timeout, attempts, pause, tag) is not None


def call(token, method, params=None, timeout=30, attempts=DEFAULT_ATTEMPTS,
         pause=DEFAULT_PAUSE, tag=None, api_base="https://api.telegram.org",
         json_body=False, quiet=False, raise_http=False):
    """Call one Bot API method. ``token`` is either a bare token or a ready base URL
    that already contains it.

    json_body=True sends ``application/json`` (needed for reply_markup and other
    nested structures); otherwise a normal urlencoded body.
    """
    base = token if token.startswith("http") else f"{api_base.rstrip('/')}/bot{token}"
    if json_body:
        data = json.dumps(params or {}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        data = urllib.parse.urlencode(params or {}).encode()
        headers = {}
    return request(f"{base.rstrip('/')}/{method}", data, headers,
                   timeout, attempts, pause, tag or f"tg {method}",
                   quiet=quiet, raise_http=raise_http)


def send_message(token, chat, text, thread_id=None, parse_mode=None, reply_to=None,
                 reply_markup=None, preview=False, timeout=30,
                 attempts=DEFAULT_ATTEMPTS, pause=DEFAULT_PAUSE, tag="tg",
                 api_base="https://api.telegram.org", quiet=False):
    """Send text, split to respect Telegram's length limit. True iff EVERY chunk landed.

    A chunk that fails doesn't cancel the rest — half a long report beats none — but
    the return value will honestly be False.
    """
    ok = True
    chunks = [text[i:i + MAX_TEXT] for i in range(0, max(len(text), 1), MAX_TEXT)] or [text]
    for chunk in chunks:
        p = {"chat_id": chat, "text": chunk, "disable_web_page_preview": not preview}
        if thread_id: p["message_thread_id"] = thread_id
        if parse_mode: p["parse_mode"] = parse_mode
        if reply_to: p["reply_to_message_id"] = reply_to
        if reply_markup: p["reply_markup"] = json.dumps(reply_markup)
        r = call(token, "sendMessage", p, timeout=timeout, attempts=attempts,
                 pause=pause, tag=tag, api_base=api_base, quiet=quiet)
        if r is None:
            ok = False
    return ok
