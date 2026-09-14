#!/usr/bin/env python3
"""Validate Step-2 processed aviation-news payloads."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEWS = ROOT / "data" / "news.json"
ARCHIVE = ROOT / "data" / "news_archive.json"
STATUS = ROOT / "data" / "news_processing_status.json"

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


def validate_payload(path: Path, current: bool) -> tuple[list[str], list[str], dict]:
    errors, warnings = [], []
    if not path.exists():
        return [f"{path.name} missing"], [], {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"{path.name} invalid JSON: {exc}"], [], {}

    if data.get("schema_version") != 2:
        errors.append(f"{path.name}: schema_version must be 2")
    if data.get("categories") != EXPECTED:
        errors.append(f"{path.name}: categories do not match the agreed 8-category taxonomy")

    stories = data.get("stories")
    if not isinstance(stories, list):
        errors.append(f"{path.name}: stories must be a list")
        return errors, warnings, data

    now = datetime.now(timezone.utc)
    min_dt = now - timedelta(days=8 if current else 122)
    seen_story_ids, url_owner = set(), {}
    enriched = 0

    for i, s in enumerate(stories):
        tag = f"{path.name} story[{i}]"
        sid = s.get("id")
        if not sid:
            errors.append(f"{tag}: missing id")
        elif sid in seen_story_ids:
            errors.append(f"{tag}: duplicate story id {sid}")
        else:
            seen_story_ids.add(sid)

        if not s.get("title"):
            errors.append(f"{tag}: missing title")
        if s.get("category") not in EXPECTED:
            errors.append(f"{tag}: invalid category {s.get('category')!r}")
        try:
            imp = int(s.get("importance_score"))
            if imp < 0 or imp > 100:
                raise ValueError
        except Exception:
            errors.append(f"{tag}: importance_score must be 0..100")

        d = parse_dt(s.get("published_at"))
        if not d:
            errors.append(f"{tag}: bad published_at")
        elif d < min_dt:
            errors.append(f"{tag}: published_at outside retention window")

        sources = s.get("sources")
        if not isinstance(sources, list) or not sources:
            errors.append(f"{tag}: sources missing")
        else:
            urls = []
            for src in sources:
                url = src.get("url")
                if not url:
                    errors.append(f"{tag}: source missing url")
                    continue
                urls.append(url)
                owner = url_owner.get(url)
                if owner and owner != sid:
                    errors.append(f"{tag}: source URL already belongs to another cluster: {url}")
                else:
                    url_owner[url] = sid
            if len(urls) != len(set(urls)):
                errors.append(f"{tag}: duplicate source URL inside story")

        if s.get("source_count") != len(sources or []):
            errors.append(f"{tag}: source_count mismatch")

        if s.get("analysis_mode") == "deepseek":
            enriched += 1
            if not s.get("summary_zh"):
                errors.append(f"{tag}: DeepSeek story missing summary_zh")
            if not s.get("why_it_matters_zh"):
                errors.append(f"{tag}: DeepSeek story missing why_it_matters_zh")
        elif not s.get("summary_zh") or not s.get("why_it_matters_zh"):
            warnings.append(f"{tag}: fallback story has no Chinese enrichment")

    if current and not stories:
        errors.append(f"{path.name}: current feed is empty")
    if current and data.get("analysis_mode") == "deepseek" and enriched == 0:
        errors.append(f"{path.name}: DeepSeek mode set but no DeepSeek-enriched stories present")

    return errors, warnings, data


def main() -> int:
    errors, warnings = [], []
    e, w, news = validate_payload(NEWS, current=True)
    errors.extend(e); warnings.extend(w)
    e, w, archive = validate_payload(ARCHIVE, current=False)
    errors.extend(e); warnings.extend(w)

    if STATUS.exists():
        try:
            st = json.loads(STATUS.read_text(encoding="utf-8"))
            if st.get("categories") != EXPECTED:
                errors.append("news_processing_status.json: category taxonomy mismatch")
        except Exception as exc:
            errors.append(f"news_processing_status.json invalid JSON: {exc}")
    else:
        errors.append("news_processing_status.json missing")

    report = {
        "status": "pass" if not errors else "fail",
        "current_stories": len(news.get("stories", [])) if isinstance(news, dict) else 0,
        "archive_stories": len(archive.get("stories", [])) if isinstance(archive, dict) else 0,
        "analysis_mode": news.get("analysis_mode") if isinstance(news, dict) else None,
        "errors": errors,
        "warnings_count": len(warnings),
        "warnings_sample": warnings[:10],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
