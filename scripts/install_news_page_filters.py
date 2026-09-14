#!/usr/bin/env python3
"""Idempotently add URL deep-link modes to the full news page."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "news.html"
MARK = "// STEP4_URL_FILTERS"
PATCH = r'''
  // STEP4_URL_FILTERS
  function applyUrlFilters() {
    const params = new URLSearchParams(window.location.search);
    if (params.get("critical") === "1") $("critical").value = "critical";
    else if (params.get("priority") === "1") $("critical").value = "priority";
    else if (params.get("macro") === "1") $("critical").value = "macro";
    if (params.get("q")) $("search").value = params.get("q");
    if (params.get("from")) $("dateFrom").value = params.get("from");
    if (params.get("to")) $("dateTo").value = params.get("to");
  }

  applyUrlFilters();
'''


def main() -> int:
    text = PAGE.read_text(encoding="utf-8")
    if MARK in text:
        print("Step 4 URL filters already installed")
        return 0
    marker = "  load();"
    if marker not in text:
        raise SystemExit("Could not find news page load() insertion point")
    text = text.replace(marker, PATCH + "\n" + marker, 1)
    PAGE.write_text(text, encoding="utf-8")
    print("Installed Step 4 URL filters into news.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
