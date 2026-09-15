#!/usr/bin/env python3
"""Patch the aviation-news analyzer so one DeepSeek pass does final classification + enrichment.

Step 2 remains deterministic: metadata cleanup, de-duplication, Python clustering and a
provisional category. Step 3 uses this module to ask DeepSeek once for the final category,
Chinese summary, lessor relevance and risk/materiality fields. Existing deterministic
priority, critical and ranking rules remain authoritative after the model response.
"""
from __future__ import annotations

import json
from typing import Any

ALLOWED_CATEGORIES = [
    "Leasing & Trading",
    "Aircraft & OEM",
    "Airlines & Credit",
    "Financing & Capital Markets",
    "Engines & MRO",
    "Values & Lease Rates",
    "Legal & Regulatory",
    "Macro & Geopolitics",
]


def install(an) -> None:
    """Install unified classification/enrichment behavior into analyze_news."""
    if getattr(an, "_unified_news_ai_installed", False):
        return

    original_post = an.requests.post
    original_sanitize = an.sanitize_ai
    original_enrich = an.enrich_story

    # Preserve the most recent AI category when a story is reused without another API call.
    prior_category_by_id: dict[str, str] = {}
    try:
        prior = json.loads(an.OUT_ARCHIVE.read_text(encoding="utf-8")) if an.OUT_ARCHIVE.exists() else {}
        for story in prior.get("stories", []) or []:
            sid = an.clean(story.get("id"))
            category = an.clean(story.get("category"))
            if sid and category in ALLOWED_CATEGORIES:
                prior_category_by_id[sid] = category
    except Exception:
        prior_category_by_id = {}

    def capped_post(*args, **kwargs):
        requested = kwargs.get("timeout", 35)
        try:
            requested = float(requested)
        except Exception:
            requested = 35
        kwargs["timeout"] = min(requested, 35)
        return original_post(*args, **kwargs)

    def unified_call_ai(key: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        system = """You are the senior market-intelligence editor for an aircraft leasing front office.
Use ONLY the supplied headline, public metadata snippets and structured fields. Never invent facts,
numbers, counterparties or conclusions. The supplied category is only a deterministic provisional
category; choose the FINAL category yourself from the eight allowed categories.

The user cares especially about lessee credit, aircraft supply/demand, lease rates and values,
engine/MRO constraints, financing conditions, repossession/legal risk and macro/geopolitical
transmission into aviation leasing.

For every supplied story:
1) assign exactly one FINAL allowed category;
2) write a concise factual Chinese summary (normally 35-90 Chinese characters);
3) write one concise Chinese sentence explaining why it matters to an aircraft lessor;
4) score market_materiality, lessor_relevance and macro_relevance from 0-100;
5) mark critical_candidate only for developments that could require prompt front-office attention;
6) identify macro tags, key entities and aircraft types only when supported by input.
Return JSON only. Do not drop any supplied story ID."""
        prompt = {
            "allowed_categories": ALLOWED_CATEGORIES,
            "scoring_guide": {
                "market_materiality": "How consequential the development is for aviation/airlines regardless of this user's portfolio.",
                "lessor_relevance": "Direct relevance to leasing, lessee credit, asset value, financing, engine/MRO, supply, remarketing or recoverability.",
                "macro_relevance": "How strongly rates/funding, fuel, FX, trade, geopolitics, airspace, demand, regulation or OEM supply create market-wide leasing effects.",
                "critical_candidate": "True only for potentially urgent/material developments such as bankruptcy/default/restructuring, major grounding/engine crisis, sanctions/airspace shock, major OEM production shock, major financing/lease transaction, or similarly material news.",
            },
            "allowed_macro_tags": [
                "Rates & Funding", "Fuel & Energy", "Geopolitics & Airspace",
                "Trade & Supply Chain", "Macro Demand", "OEM Supply", "Regulation & Recovery",
            ],
            "return": {
                "stories": [{
                    "id": "exact supplied id",
                    "category": "exactly one allowed category",
                    "summary_zh": "Chinese factual summary",
                    "why_it_matters_zh": "Chinese lessor implication",
                    "market_materiality": 0,
                    "lessor_relevance": 0,
                    "macro_relevance": 0,
                    "critical_candidate": False,
                    "critical_reason_zh": "short Chinese reason or empty string",
                    "macro_tags": ["allowed tag"],
                    "key_entities": ["supported entity"],
                    "aircraft_tags": ["supported aircraft type"],
                }]
            },
            "stories": candidates,
        }
        body = {
            "model": an.MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 8000,
        }
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        errors = []
        for attempt in range(2):
            try:
                response = an.requests.post(an.CHAT_API, headers=headers, json=body, timeout=100)
                response.raise_for_status()
                content = (((response.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                if not content:
                    raise ValueError("empty DeepSeek content")
                return an.parse_json(content)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                if attempt == 0:
                    an.time.sleep(2)
        raise RuntimeError("; ".join(errors))

    def unified_sanitize(row: dict[str, Any], fallback: dict[str, Any], allowed_macro_tags: set[str]) -> dict[str, Any]:
        result = original_sanitize(row, fallback, allowed_macro_tags)
        category = an.clean(row.get("category"))
        if category in ALLOWED_CATEGORIES:
            result["category"] = category
        return result

    def unified_enrich(story: dict[str, Any], analysis: dict[str, Any], config: dict[str, Any], raw: dict[str, dict[str, Any]], now):
        # Use the current AI category, otherwise preserve the prior AI category for reused stories.
        working = dict(story)
        category = an.clean(analysis.get("category"))
        if category not in ALLOWED_CATEGORIES:
            category = prior_category_by_id.get(an.clean(story.get("id")), "")
        if category in ALLOWED_CATEGORIES:
            working["category"] = category

        enriched = original_enrich(working, analysis, config, raw, now)
        enriched["category"] = working.get("category")

        # Fingerprint the deterministic Step-2 input, not the AI-corrected category. This keeps
        # unchanged stories reusable and prevents category correction from causing daily re-calls.
        enriched["analysis_fingerprint"] = an.story_fingerprint(story)
        return enriched

    an.requests.post = capped_post
    an.time.sleep = lambda _seconds: None
    an.call_ai = unified_call_ai
    an.sanitize_ai = unified_sanitize
    an.enrich_story = unified_enrich
    an._unified_news_ai_installed = True
