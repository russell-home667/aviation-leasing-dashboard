#!/usr/bin/env python3
"""Validate Step-1 news-source coverage without accessing any paid article body."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from update_news_raw import normalize_title

ROOT = Path(__file__).resolve().parents[1]
RAW_PATH = ROOT / "data" / "news_raw.json"
HEALTH_PATH = ROOT / "data" / "news_source_health.json"
BASELINE_PATH = ROOT / "data" / "news_validation_baseline.json"
OUTPUT_PATH = ROOT / "data" / "news_coverage.json"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def best_title_match(expected: str, candidates: list[dict]) -> tuple[dict | None, float]:
    target = normalize_title(expected)
    exact = [item for item in candidates if normalize_title(item.get("title", "")) == target]
    if exact:
        return exact[0], 1.0
    best, score = None, 0.0
    for item in candidates:
        s = SequenceMatcher(None, target, normalize_title(item.get("title", ""))).ratio()
        if s > score:
            best, score = item, s
    return best, score


def newest_age_days(items: list[dict], source_name: str) -> float | None:
    dates = [parse_iso(item.get("published_at")) for item in items if item.get("source", "").lower() == source_name.lower()]
    dates = [d for d in dates if d is not None]
    if not dates:
        return None
    return round((datetime.now(timezone.utc) - max(dates)).total_seconds() / 86400, 2)


def normalize_health_semantics(health: dict) -> None:
    """A Bing query with zero hits is healthy if transport/parsing worked.

    Direct feeds/categories are different: zero records is a content-health failure.
    This corrects the original Step-1 health metric, which incorrectly counted an
    empty but successful Bing query as a failed source check.
    """
    checks = health.get("checks", [])
    for row in checks:
        transport_ok = row.get("http_status") == 200 and not row.get("error")
        has_records = int(row.get("records") or 0) > 0
        row["transport_ok"] = transport_ok
        row["has_records"] = has_records
        if row.get("kind") == "rss_search":
            row["ok"] = transport_ok
        else:
            row["ok"] = transport_ok and has_records
    ok_count = sum(1 for row in checks if row.get("ok"))
    summary = health.setdefault("summary", {})
    summary["checks_total"] = len(checks)
    summary["checks_ok"] = ok_count
    summary["checks_failed"] = len(checks) - ok_count
    summary["semantic_note"] = "For rss_search, HTTP/parse success is healthy even when a query currently returns zero results; has_records is tracked separately."


def main() -> int:
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    health = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
    normalize_health_semantics(health)
    HEALTH_PATH.write_text(json.dumps(health, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    items = raw.get("items", [])
    checks = health.get("checks", [])

    ishka_items = [item for item in items if item.get("source") == "Ishka Airfinance"]
    baseline_results = []
    hits = 0
    for expected in baseline.get("expected_items", []):
        same_date = [item for item in ishka_items if item.get("published_date") == expected.get("published_date")]
        match, score = best_title_match(expected["title"], same_date or ishka_items)
        found = bool(match and score >= 0.94)
        hits += int(found)
        baseline_results.append({
            "published_date": expected.get("published_date"),
            "expected_title": expected["title"],
            "found": found,
            "similarity": round(score, 3),
            "matched_title": match.get("title") if found and match else None,
            "matched_url": match.get("url") if found and match else None,
        })

    expected_total = len(baseline_results)
    recall = round(hits / expected_total, 4) if expected_total else None

    ishka_checks = [c for c in checks if c.get("source_id", "").startswith("ishka_")]
    aviationnews_checks = [c for c in checks if c.get("source_id", "").startswith("aviationnews_")]
    bing_checks = [c for c in checks if c.get("kind") == "rss_search"]

    def content_ok(rows: list[dict]) -> int:
        return sum(1 for c in rows if c.get("http_status") == 200 and not c.get("error") and int(c.get("records") or 0) > 0)

    def transport_ok(rows: list[dict]) -> int:
        return sum(1 for c in rows if c.get("http_status") == 200 and not c.get("error"))

    source_counts = raw.get("stats", {}).get("by_source", {})
    ishka_ok = bool(ishka_checks) and content_ok(ishka_checks) == len(ishka_checks)
    aviationnews_ok = bool(aviationnews_checks) and content_ok(aviationnews_checks) == len(aviationnews_checks)
    bing_transport_ratio = round(transport_ok(bing_checks) / len(bing_checks), 4) if bing_checks else 0.0
    reuters_count = int(source_counts.get("Reuters", 0))
    flightglobal_count = int(source_counts.get("Flightglobal", 0)) + int(source_counts.get("FlightGlobal", 0))

    hard_requirements = {
        "ishka_all_direct_feeds_return_records": ishka_ok,
        "aviation_news_online_all_direct_categories_return_records": aviationnews_ok,
        "bing_transport_success_at_least_90pct": bing_transport_ratio >= 0.90,
        "known_ishka_baseline_recall_100pct": recall == 1.0,
        "reuters_discovery_present": reuters_count > 0,
        "flightglobal_discovery_present": flightglobal_count > 0,
    }

    warnings = []
    freshness = {
        "Ishka Airfinance": newest_age_days(items, "Ishka Airfinance"),
        "Aviation News Online": newest_age_days(items, "Aviation News Online"),
        "Reuters": newest_age_days(items, "Reuters"),
        "Flightglobal": newest_age_days(items, "Flightglobal"),
    }
    for source, age in freshness.items():
        if age is None:
            warnings.append(f"No dated records stored for {source}.")
        elif age > 14 and source in {"Ishka Airfinance", "Aviation News Online"}:
            warnings.append(f"Newest {source} record is {age} days old; check source freshness.")
        elif age > 30 and source in {"Reuters", "Flightglobal"}:
            warnings.append(f"Newest discovered {source} record is {age} days old; check discovery queries.")

    failed = [name for name, ok in hard_requirements.items() if not ok]
    report = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "overall_status": "pass" if not failed else "fail",
        "hard_requirements": hard_requirements,
        "failed_requirements": failed,
        "warnings": warnings,
        "known_ishka_recall": {
            "hits": hits,
            "expected": expected_total,
            "recall": recall,
            "items": baseline_results,
        },
        "source_layer": {
            "ishka_direct_checks": {"ok": content_ok(ishka_checks), "total": len(ishka_checks)},
            "aviation_news_online_direct_checks": {"ok": content_ok(aviationnews_checks), "total": len(aviationnews_checks)},
            "bing_discovery_checks": {"transport_ok": transport_ok(bing_checks), "total": len(bing_checks), "transport_ratio": bing_transport_ratio},
            "stored_source_counts": {
                "Ishka Airfinance": int(source_counts.get("Ishka Airfinance", 0)),
                "Aviation News Online": int(source_counts.get("Aviation News Online", 0)),
                "Reuters": reuters_count,
                "Flightglobal": flightglobal_count,
            },
            "newest_record_age_days": freshness,
        },
    }
    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "overall_status": report["overall_status"],
        "ishka_recall": recall,
        "failed_requirements": failed,
        "warnings": warnings,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
