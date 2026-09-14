#!/usr/bin/env python3
"""Validate Step-3 ranked/enriched aviation-leasing news payloads."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / "data" / "news_feed.json"
ARCHIVE = ROOT / "data" / "news_feed_archive.json"
STEP2 = ROOT / "data" / "news.json"
STATUS = ROOT / "data" / "news_enrichment_status.json"
CONFIG = ROOT / "data" / "news_priority_entities.json"

EXPECTED = [
    "Leasing & Trading",
    "Aircraft & OEM",
    "Airlines & Credit",
    "Financing & Capital Markets",
    "Engines & MRO",
    "Values & Lease Rates",
    "Legal & Regulatory",
    "Macro & Geopolitics",
]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def parse_dt(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def validate_payload(data: dict, name: str, current: bool, config: dict) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if data.get("schema_version") != 3:
        errors.append(f"{name}: schema_version must be 3")
    if data.get("categories") != EXPECTED:
        errors.append(f"{name}: category taxonomy mismatch")
    stories = data.get("stories")
    if not isinstance(stories, list):
        return [f"{name}: stories must be a list"], warnings

    priority_floor = int(config.get("priority_entity_floor", 86))
    priority_material_floor = int(config.get("priority_material_event_floor", 92))
    macro_floor = int(config.get("macro_relevance_floor", 83))
    now = datetime.now(timezone.utc)
    min_date = now - timedelta(days=8 if current else 122)
    ids = set()

    for i, story in enumerate(stories):
        tag = f"{name} story[{i}]"
        sid = story.get("id")
        if not sid:
            errors.append(f"{tag}: missing id")
        elif sid in ids:
            errors.append(f"{tag}: duplicate id {sid}")
        else:
            ids.add(sid)

        if story.get("category") not in EXPECTED:
            errors.append(f"{tag}: invalid category")
        if not str(story.get("summary_zh") or "").strip():
            errors.append(f"{tag}: summary_zh is empty")
        if not str(story.get("why_it_matters_zh") or "").strip():
            errors.append(f"{tag}: why_it_matters_zh is empty")
        if not isinstance(story.get("priority_matches"), list):
            errors.append(f"{tag}: priority_matches must be a list")
        if not isinstance(story.get("macro_tags"), list):
            errors.append(f"{tag}: macro_tags must be a list")
        if not isinstance(story.get("aircraft_tags"), list):
            errors.append(f"{tag}: aircraft_tags must be a list")
        if not isinstance(story.get("critical"), bool):
            errors.append(f"{tag}: critical must be boolean")
        if not isinstance(story.get("score_components"), dict):
            errors.append(f"{tag}: score_components missing")
            components = {}
        else:
            components = story["score_components"]

        try:
            score = int(story.get("importance_score"))
            if not 0 <= score <= 100:
                raise ValueError
        except Exception:
            errors.append(f"{tag}: importance_score must be 0..100")
            score = 0

        if story.get("priority_matches") and score < priority_floor:
            errors.append(f"{tag}: priority entity story score {score} below floor {priority_floor}")
        if components.get("priority_material_event") and score < priority_material_floor:
            errors.append(f"{tag}: priority material event score {score} below floor {priority_material_floor}")
        if components.get("macro_related") and score < macro_floor:
            errors.append(f"{tag}: macro-related story score {score} below floor {macro_floor}")
        if story.get("critical") and score < 90:
            warnings.append(f"{tag}: Critical score is below 90")

        published = parse_dt(story.get("published_at"))
        if not published:
            errors.append(f"{tag}: invalid published_at")
        elif published < min_date:
            errors.append(f"{tag}: outside expected retention window")

    if current:
        home = data.get("homepage") or {}
        feed_ids = home.get("feed_story_ids")
        critical_ids = home.get("critical_story_ids")
        if not isinstance(feed_ids, list) or len(feed_ids) > 10:
            errors.append(f"{name}: homepage feed_story_ids must be a list of at most 10")
            feed_ids = []
        if not isinstance(critical_ids, list) or len(critical_ids) > 10:
            errors.append(f"{name}: homepage critical_story_ids must be a list of at most 10")
            critical_ids = []
        for sid in feed_ids:
            if sid not in ids:
                errors.append(f"{name}: homepage feed id {sid} not found")
        by_id = {story.get("id"): story for story in stories}
        for sid in critical_ids:
            if sid not in ids:
                errors.append(f"{name}: homepage critical id {sid} not found")
            elif not by_id[sid].get("critical"):
                errors.append(f"{name}: homepage critical id {sid} is not Critical")
        expected_feed = [story.get("id") for story in stories[:10]]
        if feed_ids != expected_feed:
            errors.append(f"{name}: homepage feed is not the top-ranked 10")

    return errors, warnings


def main() -> int:
    errors, warnings = [], []
    for path in (CURRENT, ARCHIVE, STEP2, STATUS, CONFIG):
        if not path.exists():
            errors.append(f"{path.name} missing")
    if errors:
        print(json.dumps({"status": "fail", "errors": errors}, ensure_ascii=False, indent=2))
        return 2

    try:
        current = load(CURRENT)
        archive = load(ARCHIVE)
        step2 = load(STEP2)
        status = load(STATUS)
        config = load(CONFIG)
    except Exception as exc:
        print(json.dumps({"status": "fail", "errors": [f"invalid JSON: {exc}"]}, ensure_ascii=False, indent=2))
        return 2

    e, w = validate_payload(current, CURRENT.name, True, config); errors += e; warnings += w
    e, w = validate_payload(archive, ARCHIVE.name, False, config); errors += e; warnings += w

    step2_ids = {str(x.get("id")) for x in step2.get("stories", []) if x.get("id")}
    current_ids = {str(x.get("id")) for x in current.get("stories", []) if x.get("id")}
    missing = step2_ids - current_ids
    if missing:
        errors.append(f"news_feed.json missing {len(missing)} Step-2 current stories")

    if status.get("schema_version") != 3:
        errors.append("news_enrichment_status.json schema_version must be 3")
    if status.get("deepseek_configured") and status.get("pending_before_ai", 0) > 0 and status.get("deepseek_calls", 0) == 0:
        warnings.append("DeepSeek was configured but no Step-3 batch succeeded; deterministic fallback preserved coverage.")
    if status.get("deepseek_failed_batches", 0):
        warnings.append(f"Step-3 DeepSeek failed batches: {status.get('deepseek_failed_batches')}")

    report = {
        "status": "pass" if not errors else "fail",
        "current_stories": len(current.get("stories", [])),
        "archive_stories": len(archive.get("stories", [])),
        "critical_stories": current.get("stats", {}).get("critical_stories"),
        "priority_entity_stories": current.get("stats", {}).get("priority_entity_stories"),
        "macro_related_stories": current.get("stats", {}).get("macro_related_stories"),
        "deepseek_calls": status.get("deepseek_calls"),
        "deepseek_failed_batches": status.get("deepseek_failed_batches"),
        "errors": errors,
        "warnings": warnings[:20],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
