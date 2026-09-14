#!/usr/bin/env python3
"""Run Step 3 with a capped per-request network timeout.

The underlying analyzer already preserves coverage with deterministic fallback.
This launcher prevents a temporarily slow DeepSeek endpoint from occupying an
hourly GitHub Actions worker for tens of minutes during bootstrap.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import analyze_news  # noqa: E402

_original_post = analyze_news.requests.post


def capped_post(*args, **kwargs):
    requested = kwargs.get("timeout", 35)
    try:
        requested = float(requested)
    except Exception:
        requested = 35
    kwargs["timeout"] = min(requested, 35)
    return _original_post(*args, **kwargs)


analyze_news.requests.post = capped_post
analyze_news.time.sleep = lambda _seconds: None

if __name__ == "__main__":
    raise SystemExit(analyze_news.main())
