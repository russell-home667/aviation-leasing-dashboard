#!/usr/bin/env python3
"""Step 5 operational health check for the aviation-leasing news pipeline.

This check is deliberately stricter on source transport/coverage and looser on
AI enrichment quality. A source/coverage failure blocks publication so the
previous good snapshot remains live; enrichment degradation is surfaced as a
warning because deterministic fallback preserves news coverage.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_HEALTH = ROOT / "data" / "news_source_health.json"
COVERAGE = ROOT / "data" / "news_coverage.json"
PROCESSING = ROOT / "data" / "news_processing_status.json"
ENRICHMENT = ROOT / "data" / "news_enrichment_status.json"
FEED = ROOT / "data" / "news_feed.json"
ARCHIVE = ROOT / "data" / "news_feed_archive.json"
OUT = ROOT / "data" / "news_pipeline_health.json"


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


def age_hours(value, now):
    d = parse_dt(value)
    if not d:
        return None
    return max(0.0, (now - d).total_seconds() / 3600)


def number(value, default=999.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def main() -> int:
    now = datetime.now(timezone.utc)
    required = [SOURCE_HEALTH, COVERAGE, PROCESSING, ENRICHMENT, FEED, ARCHIVE]
    missing = [p.name for p in required if not p.exists()]
    if missing:
        report = {"schema_version": 1, "checked_at": now.isoformat(), "status": "fail", "errors": [f"missing files: {', '.join(missing)}"], "warnings": []}
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    source = load(SOURCE_HEALTH)
    coverage = load(COVERAGE)
    processing = load(PROCESSING)
    enrichment = load(ENRICHMENT)
    feed = load(FEED)
    archive = load(ARCHIVE)

    errors, warnings = [], []
    source_summary = source.get("summary") or {}
    if int(source_summary.get("checks_failed") or 0) > 0:
        errors.append(f"source checks failed: {source_summary.get('checks_failed')}")
    if int(source_summary.get("checks_ok") or 0) < int(source_summary.get("checks_total") or 0):
        errors.append("not all source/query transport checks are healthy")

    if coverage.get("overall_status") != "pass":
        errors.append(f"coverage status is {coverage.get('overall_status')}")
    hard = coverage.get("hard_requirements") or {}
    for key, ok in hard.items():
        if not ok:
            errors.append(f"coverage hard requirement failed: {key}")

    layer = coverage.get("source_layer") or {}
    ishka = layer.get("ishka_direct_checks") or {}
    ano = layer.get("aviation_news_online_direct_checks") or {}
    bing = layer.get("bing_discovery_checks") or {}
    if ishka.get("ok") != ishka.get("total"):
        errors.append(f"Ishka direct feeds unhealthy: {ishka.get('ok')}/{ishka.get('total')}")
    if ano.get("ok") != ano.get("total"):
        errors.append(f"Aviation News Online categories unhealthy: {ano.get('ok')}/{ano.get('total')}")
    if number(bing.get("transport_ratio"), 0.0) < 0.90:
        errors.append(f"Bing discovery transport ratio below 90%: {bing.get('transport_ratio')}")

    recall = ((coverage.get("known_ishka_recall") or {}).get("recall"))
    if recall is not None and number(recall, 0.0) < 1.0:
        errors.append(f"known Ishka baseline recall below 100%: {recall}")

    feed_stories = feed.get("stories") or []
    archive_stories = archive.get("stories") or []
    if not feed_stories:
        errors.append("current news feed is empty")
    if len(archive_stories) < len(feed_stories):
        errors.append("archive contains fewer stories than current feed")

    generated_age = age_hours(feed.get("generated_at"), now)
    if generated_age is None:
        errors.append("news_feed generated_at is invalid")
    elif generated_age > 3.0:
        errors.append(f"news feed is stale: {generated_age:.1f}h old")

    newest = max((parse_dt(x.get("published_at")) for x in feed_stories if parse_dt(x.get("published_at"))), default=None)
    newest_age = None if newest is None else max(0.0, (now - newest).total_seconds() / 3600)
    if newest_age is None:
        errors.append("no valid published_at in current feed")
    elif newest_age > 96:
        warnings.append(f"newest story is {newest_age:.1f}h old; verify weekend/holiday versus ingestion gap")

    if processing.get("status") not in {"pass", "warn"}:
        errors.append(f"Step 2 processing status is {processing.get('status')}")
    if enrichment.get("status") not in {"pass", "warn"}:
        errors.append(f"Step 3 enrichment status is {enrichment.get('status')}")

    current_total = max(1, int(enrichment.get("current_enriched_stories") or len(feed_stories) or 1))
    fallback = int(enrichment.get("fallback_story_count") or 0)
    fallback_ratio = fallback / current_total
    if fallback_ratio > 0.50:
        warnings.append(f"DeepSeek backlog high: {fallback}/{current_total} current stories ({fallback_ratio:.1%}) still use fallback enrichment")
    elif fallback_ratio > 0.20:
        warnings.append(f"DeepSeek backlog remains: {fallback}/{current_total} current stories ({fallback_ratio:.1%})")

    if int(enrichment.get("deepseek_failed_batches") or 0) > 0:
        warnings.append(f"latest Step 3 run had {enrichment.get('deepseek_failed_batches')} failed DeepSeek batches")
    if int(enrichment.get("backfill_failed_batches") or 0) > 0:
        warnings.append(f"latest Step 5 backfill had {enrichment.get('backfill_failed_batches')} failed DeepSeek batches")

    source_ages = layer.get("newest_record_age_days") or {}
    ishka_age = number(source_ages.get("Ishka Airfinance"))
    ano_age = number(source_ages.get("Aviation News Online"))
    reuters_age = number(source_ages.get("Reuters"))
    flightglobal_age = number(source_ages.get("Flightglobal"))
    if ishka_age > 2:
        warnings.append(f"Ishka newest stored record is {source_ages.get('Ishka Airfinance')} days old")
    if ano_age > 3:
        warnings.append(f"Aviation News Online newest stored record is {source_ages.get('Aviation News Online')} days old")
    if reuters_age > 10:
        warnings.append(f"Reuters discovery newest record is {source_ages.get('Reuters')} days old")
    if flightglobal_age > 14:
        warnings.append(f"FlightGlobal discovery newest record is {source_ages.get('Flightglobal')} days old")

    status = "fail" if errors else ("degraded" if warnings else "healthy")
    report = {
        "schema_version": 1,
        "checked_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": status,
        "publish_allowed": not errors,
        "source_checks": {
            "ok": source_summary.get("checks_ok"),
            "total": source_summary.get("checks_total"),
            "failed": source_summary.get("checks_failed"),
        },
        "coverage": {
            "status": coverage.get("overall_status"),
            "ishka_baseline_recall": recall,
            "ishka_direct": ishka,
            "aviation_news_online_direct": ano,
            "bing_discovery": bing,
        },
        "freshness": {
            "feed_generated_age_hours": None if generated_age is None else round(generated_age, 2),
            "newest_story_age_hours": None if newest_age is None else round(newest_age, 2),
            "source_newest_record_age_days": source_ages,
        },
        "enrichment": {
            "deepseek_configured": enrichment.get("deepseek_configured"),
            "current_stories": current_total,
            "deepseek_stories": enrichment.get("deepseek_story_count"),
            "fallback_stories": fallback,
            "fallback_ratio": round(fallback_ratio, 4),
            "archive_deepseek_stories": enrichment.get("archive_deepseek_story_count"),
            "archive_fallback_stories": enrichment.get("archive_fallback_story_count"),
        },
        "feed": {
            "current_stories": len(feed_stories),
            "archive_stories": len(archive_stories),
            "critical_stories": (feed.get("stats") or {}).get("critical_stories"),
            "priority_entity_stories": (feed.get("stats") or {}).get("priority_entity_stories"),
            "macro_related_stories": (feed.get("stats") or {}).get("macro_related_stories"),
        },
        "errors": errors,
        "warnings": warnings,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
