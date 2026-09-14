#!/usr/bin/env python3
"""Validate Step-4 homepage/full-page news UI wiring and payload contracts."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
NEWS_PAGE = ROOT / "news.html"
CURRENT = ROOT / "data" / "news_feed.json"
ARCHIVE = ROOT / "data" / "news_feed_archive.json"


def require(text: str, needle: str, where: str, errors: list[str]) -> None:
    if needle not in text:
        errors.append(f"{where}: missing {needle}")


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    for path in (INDEX, NEWS_PAGE, CURRENT, ARCHIVE):
        if not path.exists():
            errors.append(f"{path.name} missing")
    if errors:
        print(json.dumps({"status":"fail","errors":errors}, ensure_ascii=False, indent=2))
        return 2

    index = INDEX.read_text(encoding="utf-8")
    page = NEWS_PAGE.read_text(encoding="utf-8")
    current = json.loads(CURRENT.read_text(encoding="utf-8"))
    archive = json.loads(ARCHIVE.read_text(encoding="utf-8"))

    for needle in (
        "STEP3_NEWS_CSS", "STEP3_NEWS_HTML", "STEP3_NEWS_JS",
        'id="homeNewsFeed"', 'id="homeCriticalNews"',
        'href="news.html"', 'href="news.html?critical=1"',
        "data/news_feed.json", "slice(0,10)",
    ):
        require(index, needle, "index.html", errors)

    for needle in (
        "data/news_feed_archive.json", 'id="search"', 'id="dateFrom"', 'id="dateTo"',
        'id="source"', 'id="category"', 'id="region"', 'id="company"', 'id="aircraft"',
        'id="critical"', 'id="sort"', "Why it matters for lessors", "STEP4_URL_FILTERS",
        'params.get("critical")', 'params.get("priority")', 'params.get("macro")',
    ):
        require(page, needle, "news.html", errors)

    if current.get("schema_version") != 3:
        errors.append("news_feed.json: schema_version must be 3")
    if archive.get("schema_version") != 3:
        errors.append("news_feed_archive.json: schema_version must be 3")

    stories = current.get("stories") or []
    ids = {x.get("id") for x in stories}
    homepage = current.get("homepage") or {}
    feed_ids = homepage.get("feed_story_ids") or []
    critical_ids = homepage.get("critical_story_ids") or []
    if len(feed_ids) > 10:
        errors.append("homepage News Feed contains more than 10 IDs")
    if len(critical_ids) > 10:
        errors.append("homepage Critical News contains more than 10 IDs")
    if any(x not in ids for x in feed_ids):
        errors.append("homepage News Feed references an unknown story ID")
    if any(x not in ids for x in critical_ids):
        errors.append("homepage Critical News references an unknown story ID")

    by_id = {x.get("id"): x for x in stories}
    for sid in critical_ids:
        if sid in by_id and not by_id[sid].get("critical"):
            errors.append(f"homepage Critical story {sid} is not marked critical")

    top10 = [x.get("id") for x in stories[:10]]
    if feed_ids != top10:
        errors.append("homepage News Feed is not the first 10 stories in importance-ranked payload")

    if len(archive.get("stories") or []) < len(stories):
        errors.append("archive contains fewer stories than current 7-day feed")
    if not stories:
        errors.append("news_feed.json has no stories")

    # User-facing fields should be available on every current story.
    for i, story in enumerate(stories):
        for field in ("title", "summary_zh", "why_it_matters_zh", "category", "importance_score", "published_at", "primary_url"):
            if story.get(field) in (None, ""):
                errors.append(f"story[{i}] missing user-facing field {field}")
        if not isinstance(story.get("priority_matches"), list):
            errors.append(f"story[{i}] priority_matches must be list")
        if not isinstance(story.get("macro_tags"), list):
            errors.append(f"story[{i}] macro_tags must be list")

    report = {
        "status": "pass" if not errors else "fail",
        "homepage_feed_count": len(feed_ids),
        "homepage_critical_count": len(critical_ids),
        "current_stories": len(stories),
        "archive_stories": len(archive.get("stories") or []),
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
