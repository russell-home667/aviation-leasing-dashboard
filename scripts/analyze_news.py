#!/usr/bin/env python3
"""Step 3: enrich, score and rank clustered aviation-leasing news.

Inputs are Step-2 metadata plus public article text/excerpts and snippets already
collected upstream. This script never bypasses login or paywall controls.

Outputs:
- data/news_feed.json: ranked/enriched rolling 7-day feed
- data/news_feed_archive.json: ranked/enriched 120-day archive
- data/news_enrichment_status.json: Step-3 AI/scoring health

DeepSeek writes concise Chinese summaries and semantic relevance judgments.
Final importance and Critical flags are deterministic so explicit customer /
potential-customer priorities always win even if model output varies.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
STEP2_CURRENT = ROOT / "data" / "news.json"
STEP2_ARCHIVE = ROOT / "data" / "news_archive.json"
RAW_PATH = ROOT / "data" / "news_raw.json"
CONFIG_PATH = ROOT / "data" / "news_priority_entities.json"
OUT_CURRENT = ROOT / "data" / "news_feed.json"
OUT_ARCHIVE = ROOT / "data" / "news_feed_archive.json"
STATUS_PATH = ROOT / "data" / "news_enrichment_status.json"

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
CHAT_API = "https://api.deepseek.com/chat/completions"
BATCH_SIZE = max(1, int(os.getenv("NEWS_STEP3_BATCH_SIZE", "16")))
MAX_AI_STORIES = max(1, int(os.getenv("NEWS_STEP3_MAX_AI_STORIES", "260")))

SOURCE_RANK = {
    "Reuters": 100,
    "Ishka Airfinance": 98,
    "FlightGlobal": 96,
    "Flightglobal": 96,
    "Aviation News Online": 92,
    "Bloomberg": 90,
    "Financial Times": 90,
    "Wall Street Journal": 90,
    "Associated Press": 86,
    "CNBC": 84,
}

CATEGORY_LESSOR_BASE = {
    "Leasing & Trading": 92,
    "Aircraft & OEM": 82,
    "Airlines & Credit": 78,
    "Financing & Capital Markets": 90,
    "Engines & MRO": 86,
    "Values & Lease Rates": 94,
    "Legal & Regulatory": 84,
    "Macro & Geopolitics": 82,
}

SEVERE_TERMS = (
    "bankruptcy", "chapter 11", "insolvency", "default", "payment default",
    "restructuring", "repossession", "repossess", "sanction", "grounding",
    "grounded", "production halt", "delivery halt", "airspace closure",
    "war", "conflict", "credit downgrade", "liquidity crisis",
)
DIRECT_LESSOR_TERMS = (
    "lease", "leasing", "lessor", "sale and leaseback", "sale-leaseback", "slb",
    "lease rate", "aircraft value", "valuation", "residual value", "remarketing",
    "repossession", "aircraft financing", "financing", "refinancing", "jolco",
    "asset-backed", "abs", "pdp", "warehouse facility",
)
AIRCRAFT_RE = re.compile(
    r"\b(?:A220(?:-\d{3})?|A3(?:19|20|21|30|40|50)(?:neo)?(?:-\d{3,4})?|"
    r"A380(?:-\d{3})?|7(?:37|47|67|77|87)(?:-\d{3,4}| MAX ?\d+)?|"
    r"E(?:170|175|190|195)(?:-E2)?|CRJ(?:200|700|900|1000)|C909|C919|C929|"
    r"ARJ21|ATR ?(?:42|72)(?:-\d{3})?)\b",
    re.I,
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dtparse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def clamp(value: Any, lo: int = 0, hi: int = 100, default: int = 0) -> int:
    try:
        n = int(round(float(value)))
    except Exception:
        n = default
    return max(lo, min(hi, n))


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def story_fingerprint(story: dict[str, Any]) -> str:
    material = {
        "title": clean(story.get("title")),
        "category": clean(story.get("category")),
        "region": clean(story.get("region")),
        "entities": sorted(clean(x) for x in (story.get("entities") or []) if clean(x)),
        "raw_ids": sorted(str(x) for x in (story.get("raw_ids") or [])),
        "sources": sorted(clean(x.get("name")) + "|" + clean(x.get("url")) for x in (story.get("sources") or [])),
    }
    return hashlib.sha1(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


def raw_lookup() -> dict[str, dict[str, Any]]:
    data = load_json(RAW_PATH, {})
    items = data.get("items", []) if isinstance(data, dict) else []
    out = {}
    for item in items:
        rid = clean(item.get("id"))
        if rid:
            out[rid] = item
    return out


CONTENT_BASIS_RANK = {
    "headline_only": 0,
    "rss_summary": 1,
    "article_excerpt": 2,
    "full_text": 3,
}


def analysis_content_basis(story: dict[str, Any], raw: dict[str, dict[str, Any]]) -> str:
    best = "headline_only"
    for rid in story.get("raw_ids") or []:
        item = raw.get(str(rid))
        if not item:
            continue
        basis = clean(item.get("content_basis"))
        if basis not in CONTENT_BASIS_RANK:
            if clean(item.get("public_article_text")):
                basis = "article_excerpt"
            elif len(clean(item.get("summary"))) >= 80:
                basis = "rss_summary"
            else:
                basis = "headline_only"
        if CONTENT_BASIS_RANK[basis] > CONTENT_BASIS_RANK[best]:
            best = basis
    return best


def metadata_snippets(story: dict[str, Any], raw: dict[str, dict[str, Any]]) -> list[str]:
    snippets = []
    seen = set()
    char_budget = 6800

    # Prefer a publicly accessible article body/excerpt when Step 1.5 obtained one.
    article_candidates = []
    for rid in story.get("raw_ids") or []:
        item = raw.get(str(rid))
        if not item:
            continue
        text = clean(item.get("public_article_text"))
        if text:
            article_candidates.append(text)
    if article_candidates:
        article = max(article_candidates, key=len)[:5600]
        snippets.append(article)
        seen.add(article.lower())
        char_budget -= len(article)

    # Add short RSS/listing metadata as corroborating context.
    for rid in story.get("raw_ids") or []:
        item = raw.get(str(rid))
        if not item:
            continue
        for value in (item.get("summary"), item.get("description"), item.get("snippet")):
            text = clean(value)
            if not text or text.lower() in seen:
                continue
            remaining = min(650, max(0, char_budget))
            if remaining <= 0:
                break
            snippets.append(text[:remaining])
            seen.add(text.lower())
            char_budget -= min(len(text), remaining)
        if len(snippets) >= 4 or char_budget <= 0:
            break
    return snippets[:4]


def boundary_alias_match(text: str, alias: str) -> bool:
    haystack = text.lower()
    needle = clean(alias).lower()
    if not needle:
        return False
    if re.search(r"[\u3400-\u9fff]", needle) or " " in needle or "-" in needle or "'" in needle:
        return needle in haystack
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack, re.I) is not None


def story_text(story: dict[str, Any], raw: dict[str, dict[str, Any]]) -> str:
    parts = [clean(story.get("title")), clean(story.get("category")), clean(story.get("region"))]
    parts.extend(clean(x) for x in (story.get("entities") or []))
    parts.extend(metadata_snippets(story, raw))
    return " | ".join(x for x in parts if x)


def priority_matches(text: str, config: dict[str, Any]) -> list[dict[str, str]]:
    hits = []
    for item in config.get("entities") or []:
        aliases = item.get("aliases") or [item.get("canonical")]
        if any(boundary_alias_match(text, str(alias)) for alias in aliases if alias):
            hits.append({"name": clean(item.get("canonical")), "type": clean(item.get("type"))})
    out = []
    seen = set()
    for hit in hits:
        if hit["name"] and hit["name"].lower() not in seen:
            seen.add(hit["name"].lower())
            out.append(hit)
    return out


def macro_tags(text: str, config: dict[str, Any]) -> list[str]:
    haystack = text.lower()
    tags = []
    for topic in config.get("macro_topics") or []:
        if any(clean(term).lower() in haystack for term in (topic.get("terms") or []) if clean(term)):
            tag = clean(topic.get("tag"))
            if tag:
                tags.append(tag)
    return list(dict.fromkeys(tags))


def has_any(text: str, terms: list[str] | tuple[str, ...]) -> bool:
    haystack = text.lower()
    return any(clean(term).lower() in haystack for term in terms if clean(term))


def extract_aircraft(text: str, entities: list[str] | None = None) -> list[str]:
    values = []
    for m in AIRCRAFT_RE.finditer(text):
        value = clean(m.group(0)).upper().replace(" ", "")
        if value not in values:
            values.append(value)
    for entity in entities or []:
        for m in AIRCRAFT_RE.finditer(clean(entity)):
            value = clean(m.group(0)).upper().replace(" ", "")
            if value not in values:
                values.append(value)
    return values[:12]


def source_quality(story: dict[str, Any]) -> int:
    names = [clean(x.get("name")) for x in (story.get("sources") or [])]
    if not names:
        names = [clean(story.get("primary_source"))]
    return max([SOURCE_RANK.get(x, 65) for x in names if x] or [60])


def recency_score(story: dict[str, Any], now: datetime) -> int:
    date = dtparse(story.get("published_at"))
    if not date:
        return 45
    days = max(0.0, (now - date).total_seconds() / 86400)
    return clamp(100 - days * 7.5, 35, 100, 50)


def fallback_ai(story: dict[str, Any], text: str, priority: list[dict[str, str]], macros: list[str]) -> dict[str, Any]:
    category = clean(story.get("category"))
    materiality = clamp(story.get("importance_score"), 35, 90, 55)
    lessor = CATEGORY_LESSOR_BASE.get(category, 72)
    if has_any(text, SEVERE_TERMS):
        materiality = max(materiality, 88)
        lessor = max(lessor, 88)
    if has_any(text, DIRECT_LESSOR_TERMS):
        lessor = max(lessor, 91)
    macro = 82 if category == "Macro & Geopolitics" else (72 if macros else 30)
    if priority:
        materiality = max(materiality, 78)
    why_templates = {
        "Leasing & Trading": "直接影响飞机交易、再营销、租金或出租人竞争格局。",
        "Aircraft & OEM": "可能改变新机供给、交付节奏、存量飞机稀缺性与资产价值。",
        "Airlines & Credit": "需要关注承租人信用、现金流、机队调整及潜在租约风险。",
        "Financing & Capital Markets": "可能影响航空资产融资成本、资金可得性和交易定价。",
        "Engines & MRO": "可能影响在翼时间、维修储备、备发需求和飞机可用率。",
        "Values & Lease Rates": "直接关系飞机市场价值、残值和租赁收益水平。",
        "Legal & Regulatory": "可能改变回收、合规、跨境执行或交易结构风险。",
        "Macro & Geopolitics": "可能通过资金成本、燃油、需求、空域或制裁传导至航空租赁市场。",
    }
    return {
        "summary_zh": f"根据公开标题与元数据：{clean(story.get('title'))}",
        "why_it_matters_zh": why_templates.get(category, "可能影响航空公司经营与航空资产风险收益。"),
        "market_materiality": materiality,
        "lessor_relevance": lessor,
        "macro_relevance": macro,
        "critical_candidate": bool(has_any(text, SEVERE_TERMS)),
        "critical_reason_zh": "涉及信用、资产回收、停飞、制裁或重大供给冲击。" if has_any(text, SEVERE_TERMS) else "",
        "macro_tags": macros,
        "key_entities": [x["name"] for x in priority],
        "aircraft_tags": extract_aircraft(text, story.get("entities") or []),
        "ai_mode": "fallback_rules",
    }


def parse_json(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    try:
        obj = json.loads(value)
    except Exception:
        match = re.search(r"\{.*\}", value, re.S)
        if not match:
            raise ValueError("no JSON object in DeepSeek response")
        obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("DeepSeek response is not an object")
    return obj


def call_ai(key: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    system = """You are the senior market-intelligence editor for an aircraft leasing front office.
Use ONLY the supplied headline, public article text/excerpts, RSS snippets and structured fields. Never invent facts,
numbers, counterparties or conclusions. The user cares especially about lessee credit, aircraft
supply/demand, lease rates and values, engine/MRO constraints, financing conditions, repossession /
legal risk and macro/geopolitical transmission into aviation leasing.

For each story:
1) write a concise factual Chinese summary (normally 35-90 Chinese characters);
2) write one concise Chinese sentence explaining why it matters to an aircraft lessor;
3) score market_materiality, lessor_relevance and macro_relevance from 0-100;
4) mark critical_candidate only for developments that could require prompt front-office attention;
5) identify macro tags, key entities and aircraft types only when supported by input.
Return JSON only. Do not drop any supplied story ID."""
    prompt = {
        "scoring_guide": {
            "market_materiality": "How consequential the development is for aviation/airlines regardless of this user's portfolio.",
            "lessor_relevance": "Direct relevance to leasing, lessee credit, asset value, financing, engine/MRO, supply, remarketing or recoverability.",
            "macro_relevance": "How strongly rates/funding, fuel, FX, trade, geopolitics, airspace, demand, regulation or OEM supply create market-wide leasing effects.",
            "critical_candidate": "True only for potentially urgent/material developments such as bankruptcy/default/restructuring, major grounding/engine crisis, sanctions/airspace shock, major OEM production shock, major financing/lease transaction, or similarly material news."
        },
        "allowed_macro_tags": [
            "Rates & Funding", "Fuel & Energy", "Geopolitics & Airspace",
            "Trade & Supply Chain", "Macro Demand", "OEM Supply", "Regulation & Recovery"
        ],
        "return": {
            "stories": [{
                "id": "exact supplied id",
                "summary_zh": "Chinese factual summary",
                "why_it_matters_zh": "Chinese lessor implication",
                "market_materiality": 0,
                "lessor_relevance": 0,
                "macro_relevance": 0,
                "critical_candidate": False,
                "critical_reason_zh": "short Chinese reason or empty string",
                "macro_tags": ["allowed tag"],
                "key_entities": ["supported entity"],
                "aircraft_tags": ["supported aircraft type"]
            }]
        },
        "stories": candidates,
    }
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 7500,
    }
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    errors = []
    for attempt in range(2):
        try:
            response = requests.post(CHAT_API, headers=headers, json=body, timeout=100)
            response.raise_for_status()
            content = (((response.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
            if not content:
                raise ValueError("empty DeepSeek content")
            return parse_json(content)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt == 0:
                time.sleep(2)
    raise RuntimeError("; ".join(errors))


def ai_candidate(story: dict[str, Any], raw: dict[str, dict[str, Any]], priority: list[dict[str, str]], macros: list[str]) -> dict[str, Any]:
    return {
        "id": clean(story.get("id")),
        "title": clean(story.get("title"))[:320],
        "category": clean(story.get("category")),
        "region": clean(story.get("region")),
        "published_at": clean(story.get("published_at")),
        "entities": [clean(x) for x in (story.get("entities") or []) if clean(x)][:16],
        "sources": [clean(x.get("name")) for x in (story.get("sources") or []) if clean(x.get("name"))][:8],
        "metadata_snippets": metadata_snippets(story, raw),
        "content_basis": analysis_content_basis(story, raw),
        "priority_entity_matches": [x["name"] for x in priority],
        "deterministic_macro_tags": macros,
    }


def sanitize_ai(row: dict[str, Any], fallback: dict[str, Any], allowed_macro_tags: set[str]) -> dict[str, Any]:
    result = dict(fallback)
    summary = clean(row.get("summary_zh"))
    why = clean(row.get("why_it_matters_zh"))
    if summary:
        result["summary_zh"] = summary[:220]
    if why:
        result["why_it_matters_zh"] = why[:240]
    result["market_materiality"] = clamp(row.get("market_materiality"), 0, 100, fallback["market_materiality"])
    result["lessor_relevance"] = clamp(row.get("lessor_relevance"), 0, 100, fallback["lessor_relevance"])
    result["macro_relevance"] = clamp(row.get("macro_relevance"), 0, 100, fallback["macro_relevance"])
    result["critical_candidate"] = bool(row.get("critical_candidate"))
    result["critical_reason_zh"] = clean(row.get("critical_reason_zh"))[:180]
    tags = [clean(x) for x in (row.get("macro_tags") or []) if clean(x) in allowed_macro_tags]
    result["macro_tags"] = list(dict.fromkeys(tags + list(fallback.get("macro_tags") or [])))
    result["key_entities"] = list(dict.fromkeys(
        [clean(x) for x in (row.get("key_entities") or []) if clean(x)] + list(fallback.get("key_entities") or [])
    ))[:16]
    result["aircraft_tags"] = list(dict.fromkeys(
        [clean(x).upper().replace(" ", "") for x in (row.get("aircraft_tags") or []) if clean(x)] + list(fallback.get("aircraft_tags") or [])
    ))[:12]
    result["ai_mode"] = "deepseek"
    return result


def score_story(story: dict[str, Any], analysis: dict[str, Any], priority: list[dict[str, str]], macros: list[str], text: str, config: dict[str, Any], now: datetime) -> tuple[int, dict[str, Any]]:
    materiality = clamp(analysis.get("market_materiality"), 0, 100, 55)
    lessor = clamp(analysis.get("lessor_relevance"), 0, 100, CATEGORY_LESSOR_BASE.get(story.get("category"), 72))
    macro = clamp(analysis.get("macro_relevance"), 0, 100, 30)
    source = source_quality(story)
    recency = recency_score(story, now)
    multi = min(100, 55 + max(0, int(story.get("source_count") or 1) - 1) * 15)

    base = round(materiality * 0.32 + lessor * 0.34 + source * 0.16 + recency * 0.12 + multi * 0.06)
    bonuses = 0
    floors = []

    severe = has_any(text, SEVERE_TERMS)
    direct_lessor = has_any(text, DIRECT_LESSOR_TERMS)
    material_event = has_any(text, config.get("material_event_terms") or [])
    all_macro_tags = list(dict.fromkeys(list(macros) + list(analysis.get("macro_tags") or [])))
    macro_related = bool(all_macro_tags) and (story.get("category") == "Macro & Geopolitics" or macro >= 55 or lessor >= 70)
    systemic_macro = macro_related and (macro >= 76 or len(all_macro_tags) >= 2) and lessor >= 62

    if direct_lessor:
        bonuses += 7
    if severe:
        bonuses += 10

    if priority:
        bonuses += int(config.get("priority_entity_boost", 22))
        floors.append(int(config.get("priority_entity_floor", 86)))
        if len(priority) >= 2:
            bonuses += int(config.get("multiple_priority_entity_bonus", 4))
        if material_event or severe or lessor >= 84 or materiality >= 82:
            floors.append(int(config.get("priority_material_event_floor", 92)))

    if macro_related:
        bonuses += 12
        floors.append(int(config.get("macro_relevance_floor", 83)))
    if systemic_macro:
        bonuses += 4
        floors.append(int(config.get("systemic_macro_floor", 88)))

    score = min(100, base + bonuses)
    if floors:
        score = max(score, max(floors))

    components = {
        "market_materiality": materiality,
        "lessor_relevance": lessor,
        "macro_relevance": macro,
        "source_quality": source,
        "recency": recency,
        "multi_source": multi,
        "weighted_base": base,
        "bonuses": bonuses,
        "applied_floor": max(floors) if floors else None,
        "priority_entity": bool(priority),
        "priority_material_event": bool(priority and (material_event or severe or lessor >= 84 or materiality >= 82)),
        "macro_related": macro_related,
        "systemic_macro": systemic_macro,
        "severe_event": severe,
        "direct_lessor_event": direct_lessor,
    }
    return clamp(score), components


def critical_decision(score: int, analysis: dict[str, Any], priority: list[dict[str, str]], components: dict[str, Any]) -> tuple[bool, str]:
    severe = bool(components.get("severe_event"))
    priority_material = bool(components.get("priority_material_event"))
    systemic_macro = bool(components.get("systemic_macro"))
    ai_candidate = bool(analysis.get("critical_candidate"))

    critical = (
        (score >= 92 and (severe or priority_material or systemic_macro))
        or (score >= 90 and ai_candidate and (components.get("direct_lessor_event") or severe or systemic_macro))
        or score >= 97
    )
    if not critical:
        return False, ""

    reason = clean(analysis.get("critical_reason_zh"))
    if reason:
        return True, reason[:180]
    if severe:
        return True, "涉及破产/违约、停飞、制裁、回收或其他可能需要及时关注的重大风险。"
    if priority_material and priority:
        return True, f"重点客户/潜在客户 {priority[0]['name']} 出现重大经营、机队、融资或交易事件。"
    if systemic_macro:
        return True, "宏观或地缘冲击可能系统性传导至航空融资、资产价值、需求或运营风险。"
    return True, "对航空租赁市场具有较高即时重要性。"


def enrich_story(story: dict[str, Any], analysis: dict[str, Any], config: dict[str, Any], raw: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any]:
    out = dict(story)
    text = story_text(story, raw)
    priority = priority_matches(text, config)
    deterministic_macro = macro_tags(text, config)
    score, components = score_story(story, analysis, priority, deterministic_macro, text, config, now)
    critical, critical_reason = critical_decision(score, analysis, priority, components)

    out["summary_zh"] = clean(analysis.get("summary_zh"))
    out["why_it_matters_zh"] = clean(analysis.get("why_it_matters_zh"))
    out["importance_score"] = score
    out["priority_matches"] = priority
    out["macro_tags"] = list(dict.fromkeys(deterministic_macro + list(analysis.get("macro_tags") or [])))
    out["aircraft_tags"] = list(dict.fromkeys(
        extract_aircraft(text, story.get("entities") or []) + list(analysis.get("aircraft_tags") or [])
    ))[:12]
    out["critical"] = critical
    out["critical_reason_zh"] = critical_reason
    out["score_components"] = components
    out["step3_ai_mode"] = clean(analysis.get("ai_mode")) or "unknown"
    basis = analysis_content_basis(story, raw)
    out["content_basis"] = basis
    out["analysis_reused"] = bool(analysis.get("_reused"))
    out["analysis_input_basis"] = "historical_reuse" if out["analysis_reused"] else basis
    out["analysis_fingerprint"] = story_fingerprint(story)
    out["step3_updated_at"] = iso(now)
    return out


def main() -> int:
    now = now_utc()
    step2_current = load_json(STEP2_CURRENT, {})
    step2_archive = load_json(STEP2_ARCHIVE, {})
    config = load_json(CONFIG_PATH, {})
    raw = raw_lookup()

    if not isinstance(step2_current, dict) or not isinstance(step2_current.get("stories"), list):
        raise SystemExit("data/news.json missing or invalid")
    if not isinstance(step2_archive, dict) or not isinstance(step2_archive.get("stories"), list):
        raise SystemExit("data/news_archive.json missing or invalid")
    if not isinstance(config, dict) or not config.get("entities"):
        raise SystemExit("data/news_priority_entities.json missing or invalid")

    prior_payload = load_json(OUT_ARCHIVE, {})
    prior_by_id = {
        str(story.get("id")): story for story in (prior_payload.get("stories") or [])
        if isinstance(story, dict) and story.get("id")
    }

    archive_stories = step2_archive.get("stories") or []
    allowed_macro = {clean(x.get("tag")) for x in (config.get("macro_topics") or []) if clean(x.get("tag"))}
    analyses: dict[str, dict[str, Any]] = {}
    pending: list[tuple[dict[str, Any], list[dict[str, str]], list[str], str]] = []
    reused = 0

    for story in archive_stories:
        sid = clean(story.get("id"))
        if not sid:
            continue
        text = story_text(story, raw)
        priority = priority_matches(text, config)
        macros = macro_tags(text, config)
        fallback = fallback_ai(story, text, priority, macros)
        prior = prior_by_id.get(sid)
        fingerprint = story_fingerprint(story)
        current_basis = analysis_content_basis(story, raw)
        prior_basis = clean(prior.get("content_basis")) if prior else ""
        content_improved = CONTENT_BASIS_RANK.get(current_basis, 0) > CONTENT_BASIS_RANK.get(prior_basis, -1)
        if (
            prior
            and not content_improved
            and prior.get("analysis_fingerprint") == fingerprint
            and clean(prior.get("summary_zh"))
            and clean(prior.get("why_it_matters_zh"))
            and isinstance(prior.get("score_components"), dict)
        ):
            analyses[sid] = {
                "summary_zh": clean(prior.get("summary_zh")),
                "why_it_matters_zh": clean(prior.get("why_it_matters_zh")),
                "market_materiality": clamp((prior.get("score_components") or {}).get("market_materiality"), 0, 100, fallback["market_materiality"]),
                "lessor_relevance": clamp((prior.get("score_components") or {}).get("lessor_relevance"), 0, 100, fallback["lessor_relevance"]),
                "macro_relevance": clamp((prior.get("score_components") or {}).get("macro_relevance"), 0, 100, fallback["macro_relevance"]),
                "critical_candidate": bool(prior.get("critical")),
                "critical_reason_zh": clean(prior.get("critical_reason_zh")),
                "macro_tags": list(prior.get("macro_tags") or []),
                "key_entities": [x.get("name") for x in (prior.get("priority_matches") or []) if isinstance(x, dict) and x.get("name")],
                "aircraft_tags": list(prior.get("aircraft_tags") or []),
                "ai_mode": clean(prior.get("step3_ai_mode")) or "reused",
                "_reused": True,
            }
            reused += 1
        else:
            analyses[sid] = fallback
            pending.append((story, priority, macros, text))

    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    calls = 0
    failed_batches = 0
    errors = []
    submitted = 0
    deepseek_updated = 0

    if pending and key:
        current_ids = {str(story.get("id")) for story in (step2_current.get("stories") or [])}
        pending.sort(
            key=lambda row: (
                1 if clean(row[0].get("id")) in current_ids else 0,
                int(row[0].get("importance_score") or 0),
                dtparse(row[0].get("published_at")) or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
        ai_pending = pending[:MAX_AI_STORIES]
        for start in range(0, len(ai_pending), BATCH_SIZE):
            batch = ai_pending[start:start + BATCH_SIZE]
            candidates = [ai_candidate(story, raw, priority, macros) for story, priority, macros, _ in batch]
            submitted += len(batch)
            try:
                data = call_ai(key, candidates)
                calls += 1
                returned = {}
                for row in data.get("stories") or []:
                    rid = clean(row.get("id"))
                    if rid:
                        returned[rid] = row
                for story, priority, macros, text in batch:
                    sid = clean(story.get("id"))
                    fallback = fallback_ai(story, text, priority, macros)
                    row = returned.get(sid)
                    if row:
                        analyses[sid] = sanitize_ai(row, fallback, allowed_macro)
                        deepseek_updated += 1
                    else:
                        errors.append(f"batch_{start // BATCH_SIZE + 1}: missing story id {sid} in DeepSeek response")
            except Exception as exc:
                failed_batches += 1
                errors.append(f"batch_{start // BATCH_SIZE + 1}: {type(exc).__name__}: {exc}")

    enriched_archive = []
    for story in archive_stories:
        sid = clean(story.get("id"))
        if sid:
            enriched_archive.append(enrich_story(story, analyses[sid], config, raw, now))

    enriched_by_id = {str(story.get("id")): story for story in enriched_archive}
    current = [
        enriched_by_id[str(story.get("id"))] for story in (step2_current.get("stories") or [])
        if str(story.get("id")) in enriched_by_id
    ]

    sort_key = lambda story: (
        int(story.get("importance_score") or 0),
        dtparse(story.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    current.sort(key=sort_key, reverse=True)
    enriched_archive.sort(key=sort_key, reverse=True)
    critical = sorted([story for story in current if story.get("critical")], key=sort_key, reverse=True)

    homepage = {
        "feed_story_ids": [story["id"] for story in current[:10]],
        "critical_story_ids": [story["id"] for story in critical[:10]],
        "feed_limit": 10,
        "critical_limit": 10,
    }

    category_counts = Counter(clean(story.get("category")) for story in current)
    priority_counts = Counter(
        item.get("name") for story in current for item in (story.get("priority_matches") or [])
        if isinstance(item, dict) and item.get("name")
    )
    stats = {
        "stories": len(current),
        "critical_stories": len(critical),
        "priority_entity_stories": sum(1 for story in current if story.get("priority_matches")),
        "macro_related_stories": sum(1 for story in current if (story.get("score_components") or {}).get("macro_related")),
        "by_category": dict(sorted(category_counts.items())),
        "priority_mentions": dict(priority_counts.most_common()),
        "step3_reused_ai": reused,
        "step3_pending_before_ai": len(pending),
        "step3_ai_submitted": submitted,
        "step3_deepseek_updated": deepseek_updated,
        "step3_deepseek_calls": calls,
        "step3_failed_batches": failed_batches,
        "content_basis_counts": dict(Counter(story.get("content_basis") or "unknown" for story in current)),
        "historical_reuse_stories": sum(1 for story in current if story.get("analysis_reused")),
    }

    common = {
        "schema_version": 3,
        "generated_at": iso(now),
        "model": MODEL if key else "rules-fallback",
        "analysis_mode": "deepseek+deterministic_priority" if key else "deterministic_priority_fallback",
        "categories": step2_current.get("categories") or [],
        "ranking_policy": {
            "priority_entity_floor": int(config.get("priority_entity_floor", 86)),
            "priority_material_event_floor": int(config.get("priority_material_event_floor", 92)),
            "macro_relevance_floor": int(config.get("macro_relevance_floor", 83)),
            "systemic_macro_floor": int(config.get("systemic_macro_floor", 88)),
            "critical_score_threshold": int(config.get("critical_score_threshold", 92)),
        },
    }

    current_payload = {**common, "stats": stats, "homepage": homepage, "stories": current}
    archive_payload = {
        **common,
        "stats": {
            **stats,
            "stories": len(enriched_archive),
            "archive_retention_days": (step2_archive.get("stats") or {}).get("archive_retention_days", 120),
        },
        "stories": enriched_archive,
    }

    OUT_CURRENT.write_text(json.dumps(current_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    OUT_ARCHIVE.write_text(json.dumps(archive_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    status = {
        "schema_version": 3,
        "checked_at": iso(now),
        "status": "pass" if current else "warn",
        "deepseek_configured": bool(key),
        "model": MODEL if key else None,
        "step2_current_stories": len(step2_current.get("stories") or []),
        "step2_archive_stories": len(archive_stories),
        "current_enriched_stories": len(current),
        "archive_enriched_stories": len(enriched_archive),
        "priority_entity_stories": stats["priority_entity_stories"],
        "macro_related_stories": stats["macro_related_stories"],
        "critical_stories": len(critical),
        "reused_ai_stories": reused,
        "pending_before_ai": len(pending),
        "ai_submitted": submitted,
        "deepseek_updated": deepseek_updated,
        "deepseek_calls": calls,
        "deepseek_failed_batches": failed_batches,
        "deepseek_errors": errors[:30],
        "fallback_story_count": sum(1 for story in current if story.get("step3_ai_mode") != "deepseek"),
        "deepseek_story_count": sum(1 for story in current if story.get("step3_ai_mode") == "deepseek"),
        "content_basis_counts": dict(Counter(story.get("content_basis") or "unknown" for story in current)),
        "historical_reuse_story_count": sum(1 for story in current if story.get("analysis_reused")),
    }
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
