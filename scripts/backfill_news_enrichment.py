#!/usr/bin/env python3
"""Step 5: gradually replace fallback news enrichment with DeepSeek output.

The main Step-3 analyzer preserves coverage even when the model is unavailable.
This job specifically revisits fallback stories so historical summaries do not
remain permanently generic. It uses the best already-collected public context available: article text/excerpts first, then RSS metadata.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import analyze_news as an  # noqa: E402

STEP2_CURRENT = ROOT / "data" / "news.json"
STEP2_ARCHIVE = ROOT / "data" / "news_archive.json"
FEED = ROOT / "data" / "news_feed.json"
FEED_ARCHIVE = ROOT / "data" / "news_feed_archive.json"
STATUS = ROOT / "data" / "news_enrichment_status.json"
CONFIG = ROOT / "data" / "news_priority_entities.json"

BATCH_SIZE = max(1, int(os.getenv("NEWS_BACKFILL_BATCH_SIZE", "6")))
MAX_STORIES = max(1, int(os.getenv("NEWS_BACKFILL_MAX_STORIES", "240")))

def load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dt(value):
    return an.dtparse(value) or datetime.min.replace(tzinfo=timezone.utc)


def main() -> int:
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not key:
        print(json.dumps({"status": "skipped", "reason": "DEEPSEEK_API_KEY missing"}, ensure_ascii=False))
        return 0

    current2 = load(STEP2_CURRENT, {})
    archive2 = load(STEP2_ARCHIVE, {})
    current_payload = load(FEED, {})
    archive_payload = load(FEED_ARCHIVE, {})
    status = load(STATUS, {})
    config = load(CONFIG, {})
    raw = an.raw_lookup()

    if not isinstance(archive2.get("stories"), list) or not isinstance(archive_payload.get("stories"), list):
        raise SystemExit("Step-2/Step-3 archive payload missing")

    step2_by_id = {str(x.get("id")): x for x in archive2.get("stories", []) if x.get("id")}
    enriched_by_id = {str(x.get("id")): x for x in archive_payload.get("stories", []) if x.get("id")}
    current_ids = {str(x.get("id")) for x in current2.get("stories", []) if x.get("id")}
    allowed_macro = {an.clean(x.get("tag")) for x in (config.get("macro_topics") or []) if an.clean(x.get("tag"))}

    candidates = []
    for sid, story in step2_by_id.items():
        old = enriched_by_id.get(sid, {})
        mode = an.clean(old.get("step3_ai_mode"))
        generic = an.clean(old.get("summary_zh")).startswith("根据公开标题与元数据：")
        if mode == "deepseek" and not generic:
            continue
        text = an.story_text(story, raw)
        priority = an.priority_matches(text, config)
        macros = an.macro_tags(text, config)
        old_score = int(old.get("importance_score") or story.get("importance_score") or 0)
        candidates.append((story, priority, macros, text, old_score))

    # Current feed first, then important/priority stories, then newest archive items.
    candidates.sort(
        key=lambda row: (
            1 if str(row[0].get("id")) in current_ids else 0,
            1 if row[1] else 0,
            row[4],
            dt(row[0].get("published_at")),
        ),
        reverse=True,
    )
    selected = candidates[:MAX_STORIES]

    calls = 0
    updated = 0
    failed_batches = 0
    errors = []
    now = an.now_utc()

    for start in range(0, len(selected), BATCH_SIZE):
        batch = selected[start:start + BATCH_SIZE]
        compact = [an.ai_candidate(story, raw, priority, macros) for story, priority, macros, _, _ in batch]
        try:
            data = an.call_ai(key, compact)
            calls += 1
            returned = {an.clean(x.get("id")): x for x in (data.get("stories") or []) if an.clean(x.get("id"))}
            for story, priority, macros, text, _old_score in batch:
                sid = str(story.get("id"))
                row = returned.get(sid)
                if not row:
                    errors.append(f"batch_{start // BATCH_SIZE + 1}: missing story id {sid}")
                    continue
                fallback = an.fallback_ai(story, text, priority, macros)
                analysis = an.sanitize_ai(row, fallback, allowed_macro)
                enriched_by_id[sid] = an.enrich_story(story, analysis, config, raw, now)
                updated += 1
        except Exception as exc:
            failed_batches += 1
            errors.append(f"batch_{start // BATCH_SIZE + 1}: {type(exc).__name__}: {exc}")

    enriched_archive = [enriched_by_id[str(x.get("id"))] for x in archive2.get("stories", []) if str(x.get("id")) in enriched_by_id]
    sort_key = lambda story: (int(story.get("importance_score") or 0), dt(story.get("published_at")))
    enriched_archive.sort(key=sort_key, reverse=True)
    current = [enriched_by_id[str(x.get("id"))] for x in current2.get("stories", []) if str(x.get("id")) in enriched_by_id]
    current.sort(key=sort_key, reverse=True)
    critical = sorted([x for x in current if x.get("critical")], key=sort_key, reverse=True)

    category_counts = Counter(an.clean(x.get("category")) for x in current)
    priority_counts = Counter(
        item.get("name") for story in current for item in (story.get("priority_matches") or [])
        if isinstance(item, dict) and item.get("name")
    )
    deepseek_current = sum(1 for x in current if x.get("step3_ai_mode") == "deepseek")
    fallback_current = len(current) - deepseek_current
    deepseek_archive = sum(1 for x in enriched_archive if x.get("step3_ai_mode") == "deepseek")
    fallback_archive = len(enriched_archive) - deepseek_archive

    base_stats = dict(current_payload.get("stats") or {})
    base_stats.update({
        "stories": len(current),
        "critical_stories": len(critical),
        "priority_entity_stories": sum(1 for x in current if x.get("priority_matches")),
        "macro_related_stories": sum(1 for x in current if (x.get("score_components") or {}).get("macro_related")),
        "by_category": dict(sorted(category_counts.items())),
        "priority_mentions": dict(priority_counts.most_common()),
        "step5_backfill_candidates": len(candidates),
        "step5_backfill_selected": len(selected),
        "step5_backfill_updated": updated,
        "step5_backfill_calls": calls,
        "step5_backfill_failed_batches": failed_batches,
        "step5_deepseek_current": deepseek_current,
        "step5_fallback_current": fallback_current,
    })

    common = {k: v for k, v in current_payload.items() if k not in {"stats", "homepage", "stories", "generated_at", "model", "analysis_mode"}}
    common.update({
        "schema_version": 3,
        "generated_at": an.iso(now),
        "model": an.MODEL,
        "analysis_mode": "deepseek+deterministic_priority",
    })
    homepage = {
        "feed_story_ids": [x["id"] for x in current[:10]],
        "critical_story_ids": [x["id"] for x in critical[:10]],
        "feed_limit": 10,
        "critical_limit": 10,
    }
    new_current = {**common, "stats": base_stats, "homepage": homepage, "stories": current}

    archive_stats = dict(archive_payload.get("stats") or {})
    archive_stats.update(base_stats)
    archive_stats.update({
        "stories": len(enriched_archive),
        "step5_deepseek_archive": deepseek_archive,
        "step5_fallback_archive": fallback_archive,
    })
    new_archive = {**common, "stats": archive_stats, "stories": enriched_archive}

    status.update({
        "schema_version": 3,
        "checked_at": an.iso(now),
        "status": "pass" if current else "warn",
        "deepseek_configured": True,
        "model": an.MODEL,
        "current_enriched_stories": len(current),
        "archive_enriched_stories": len(enriched_archive),
        "critical_stories": len(critical),
        "priority_entity_stories": base_stats["priority_entity_stories"],
        "macro_related_stories": base_stats["macro_related_stories"],
        "fallback_story_count": fallback_current,
        "deepseek_story_count": deepseek_current,
        "backfill_candidates_remaining_before_run": len(candidates),
        "backfill_selected": len(selected),
        "backfill_updated": updated,
        "backfill_calls": calls,
        "backfill_failed_batches": failed_batches,
        "backfill_errors": errors[:30],
        "archive_fallback_story_count": fallback_archive,
        "archive_deepseek_story_count": deepseek_archive,
    })

    write(FEED, new_current)
    write(FEED_ARCHIVE, new_archive)
    write(STATUS, status)

    print(json.dumps({
        "status": "pass",
        "candidates_before_run": len(candidates),
        "selected": len(selected),
        "updated": updated,
        "calls": calls,
        "failed_batches": failed_batches,
        "current_deepseek": deepseek_current,
        "current_fallback": fallback_current,
        "archive_deepseek": deepseek_archive,
        "archive_fallback": fallback_archive,
        "errors": errors[:10],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
