#!/usr/bin/env python3
"""Run Step 3 with one unified DeepSeek classification + enrichment pass.

Step 2 is deterministic and does not call DeepSeek. This launcher installs the unified
AI behavior so Step 3 assigns the final category and performs summary/scoring enrichment
in the same model request, while preserving deterministic ranking and fallback coverage.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import analyze_news  # noqa: E402
from unified_news_ai import install  # noqa: E402

install(analyze_news)

if __name__ == "__main__":
    raise SystemExit(analyze_news.main())
