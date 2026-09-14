#!/usr/bin/env python3
"""Backfill recent public Aviation News Online listing metadata for Step 1."""
from __future__ import annotations

import json
import urllib.parse
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

from update_news_raw import Fetcher, dedupe, iso, merge_existing, parse_aviation_news_online

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "news_sources.json"
OUTPUT_PATH = ROOT / "data" / "news_raw.json"
REPORT_PATH = ROOT / "data" / "news_backfill_report.json"


def paged_url(url: str, page: int) -> str:
    p = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(p.query, keep_blank_values=True))
    query["page"] = str(page)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(query), p.fragment))


def parse_day(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    days = int(config.get("history_backfill_days", 120))
    max_pages = int(config.get("aviationnews_backfill_max_pages", 12))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    fetcher = Fetcher(config.get("user_agent", "AviationLeasingDashboardNewsBot/1.1"), int(config.get("request_timeout_seconds", 20)))

    collected = []
    source_reports = []
    for src in [s for s in config.get("sources", []) if s.get("kind") == "aviationnews_html"]:
        source_items = []
        seen_page_signatures = set()
        pages_fetched = 0
        stop_reason = "max_pages"
        errors = []
        for page in range(1, max_pages + 1):
            url = paged_url(src["url"], page)
            response, error = fetcher.get(url, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5")
            if response is None:
                errors.append({"page": page, "url": url, "error": error})
                stop_reason = "fetch_error"
                break
            try:
                records = parse_aviation_news_online(response.text, response.url, src["id"])
            except Exception as exc:
                errors.append({"page": page, "url": url, "error": f"ParseError: {type(exc).__name__}: {exc}"})
                stop_reason = "parse_error"
                break
            pages_fetched += 1
            signature = tuple(sorted(item.get("url", "") for item in records))
            if not records:
                stop_reason = "empty_page"
                break
            if signature in seen_page_signatures:
                stop_reason = "repeated_page"
                break
            seen_page_signatures.add(signature)
            source_items.extend(records)
            dated = [parse_day(item.get("published_date")) for item in records]
            dated = [d for d in dated if d is not None]
            if dated and min(dated) < cutoff:
                stop_reason = "cutoff_reached"
                break

        source_items = dedupe(source_items)
        in_window = [item for item in source_items if (parse_day(item.get("published_date")) is None or parse_day(item.get("published_date")) >= cutoff)]
        collected.extend(in_window)
        dates = sorted([d for d in (parse_day(item.get("published_date")) for item in in_window) if d is not None])
        source_reports.append({
            "source_id": src["id"],
            "pages_fetched": pages_fetched,
            "records_parsed": len(source_items),
            "records_within_window": len(in_window),
            "earliest_date": dates[0].isoformat() if dates else None,
            "latest_date": dates[-1].isoformat() if dates else None,
            "stop_reason": stop_reason,
            "errors": errors,
        })

    collected = dedupe(collected)
    retired = set(config.get("retired_source_channels", []))
    merged = merge_existing(collected, int(config.get("retention_days", 120)), retired)
    by_source = Counter(item.get("source", "Unknown") for item in merged)
    by_channel = Counter(item.get("source_channel", "Unknown") for item in merged)
    reuters_count = sum(1 for item in merged if item.get("source") == "Reuters")
    payload = {
        "schema_version": 1,
        "generated_at": iso(datetime.now(timezone.utc)),
        "retention_days": int(config.get("retention_days", 120)),
        "metadata_only": True,
        "stats": {
            "current_batch_unique": len(collected),
            "stored_total": len(merged),
            "reuters_discovered_via_bing": reuters_count,
            "by_source": dict(sorted(by_source.items())),
            "by_channel": dict(sorted(by_channel.items())),
        },
        "items": merged,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = {
        "schema_version": 1,
        "generated_at": iso(datetime.now(timezone.utc)),
        "backfill_days": days,
        "cutoff_date": cutoff.isoformat(),
        "source": "Aviation News Online public category listings",
        "records_collected_unique": len(collected),
        "stored_total_after_merge": len(merged),
        "sources": source_reports,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
