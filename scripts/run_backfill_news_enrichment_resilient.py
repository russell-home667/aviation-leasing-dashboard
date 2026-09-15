#!/usr/bin/env python3
"""Run historical aviation-news AI backfill using the unified DeepSeek behavior."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import analyze_news  # noqa: E402
from unified_news_ai import install  # noqa: E402

install(analyze_news)

import backfill_news_enrichment  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(backfill_news_enrichment.main())
