#!/usr/bin/env python3
"""Patch the aviation-news analyzer so one DeepSeek pass does final classification + enrichment.

Step 2 remains deterministic: metadata cleanup, de-duplication, Python clustering and a
provisional category. Step 3 asks DeepSeek once for Chinese summary, lessor relevance and
risk/materiality fields. For especially clear stories, conservative Python rules lock the
category so DeepSeek does not spend effort re-classifying it; ambiguous stories still receive
full AI classification. Existing deterministic priority, critical and ranking rules remain
authoritative after the model response.
"""
from __future__ import annotations

import json
import re
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

# These rules are intentionally narrow. A story is category-locked only when exactly one
# category has an explicit high-confidence signal. If two categories match, DeepSeek decides.
HIGH_CONFIDENCE_PATTERNS: dict[str, tuple[str, ...]] = {
    "Leasing & Trading": (
        r"\bsale[- ]and[- ]leaseback\b",
        r"\baircraft leasing deal\b",
        r"\baircraft lease agreement\b",
        r"\baircraft portfolio (?:sale|acquisition|purchase)\b",
        r"\b(?:lease placement|lease extension|remarketing mandate)\b",
    ),
    "Aircraft & OEM": (
        r"\b(?:airbus|boeing|embraer|comac|atr)\b.{0,90}\b(?:firm order|aircraft order|deliver(?:y|ies)|production rate|backlog|certif(?:y|ied|ication))\b",
        r"\b(?:firm order|aircraft order|deliver(?:y|ies)|production rate|backlog|certif(?:y|ied|ication))\b.{0,90}\b(?:airbus|boeing|embraer|comac|atr)\b",
    ),
    "Airlines & Credit": (
        r"\b(?:bankruptcy|chapter 11|insolvency|insolvent|payment default|credit downgrade|liquidity crisis)\b",
        r"\bairline restructuring\b",
    ),
    "Financing & Capital Markets": (
        r"\baircraft financ(?:e|ing)\b",
        r"\baviation (?:abs|asset[- ]backed securiti[sz]ation)\b",
        r"\basset[- ]backed securiti[sz]ation\b",
        r"\bjolco\b",
        r"\bpdp financ(?:e|ing)\b",
        r"\bwarehouse facilit(?:y|ies)\b",
    ),
    "Engines & MRO": (
        r"\b(?:gtf|pw1100g|pw1500g|leap-1[abc]|cfm56|genx|ge9x|trent xwb|trent 1000|trent 7000)\b",
        r"\b(?:engine shop visit|engine overhaul|engine maintenance|spare engine)\b",
        r"\bmro\b",
    ),
    "Values & Lease Rates": (
        r"\b(?:lease rate|lease rates|market value|base value|residual value|aircraft valuation|aircraft appraisal)\b",
    ),
    "Legal & Regulatory": (
        r"\bcape town convention\b",
        r"\b(?:court ruling|lawsuit|litigation|legal dispute)\b",
        r"\b(?:faa|easa) airworthiness directive\b",
    ),
    "Macro & Geopolitics": (
        r"\b(?:airspace closure|airspace closed|geopolitical conflict|trade war)\b",
        r"\b(?:oil price|jet fuel price|interest rate|tariff)\b",
    ),
}


def _classification_text(candidate: dict[str, Any]) -> str:
    parts = [
        str(candidate.get("title") or ""),
        " ".join(str(x) for x in (candidate.get("metadata_snippets") or [])),
        " ".join(str(x) for x in (candidate.get("entities") or [])),
    ]
    return " | ".join(parts).lower()


def high_confidence_category(candidate: dict[str, Any]) -> str | None:
    """Return a deterministic category only when exactly one strong category matches."""
    text = _classification_text(candidate)
    matches: list[str] = []
    for category, patterns in HIGH_CONFIDENCE_PATTERNS.items():
        if any(re.search(pattern, text, re.I) for pattern in patterns):
            matches.append(category)
    if len(matches) != 1:
        return None

    # The Step-2 category must agree unless the strong signal is exceptionally explicit.
    # This extra guard keeps the auto-routing conservative and lets DeepSeek resolve conflicts.
    provisional = str(candidate.get("category") or "").strip()
    strong = matches[0]
    if provisional in ALLOWED_CATEGORIES and provisional != strong:
        return None
    return strong


def install(an) -> None:
    """Install unified classification/enrichment behavior into analyze_news."""
    if getattr(an, "_unified_news_ai_installed", False):
        return

    original_post = an.requests.post
    original_sanitize = an.sanitize_ai
    original_enrich = an.enrich_story

    # Filled for each current DeepSeek request. Locked categories are authoritative even if
    # the model unnecessarily emits a different category in its response.
    locked_category_by_id: dict[str, str] = {}

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
        prepared: list[dict[str, Any]] = []
        locked_category_by_id.clear()
        for candidate in candidates:
            row = dict(candidate)
            sid = an.clean(row.get("id"))
            provisional = an.clean(row.pop("category", ""))
            locked = high_confidence_category({**candidate, "category": provisional})
            if locked:
                row["category_locked"] = True
                row["fixed_category"] = locked
                locked_category_by_id[sid] = locked
            else:
                row["category_locked"] = False
                row["provisional_category"] = provisional
            prepared.append(row)

        locked_count = len(locked_category_by_id)
        print(
            f"[news] classification routing: python_locked={locked_count}, "
            f"deepseek_classify={len(prepared) - locked_count}, total={len(prepared)}"
        )

        system = """You are the senior market-intelligence editor for an aircraft leasing front office.
Use ONLY the supplied headline, public metadata snippets and structured fields. Never invent facts,
numbers, counterparties or conclusions.

Each story has category_locked=true or false.
- If category_locked=true, fixed_category is authoritative. DO NOT spend effort re-classifying it and
  do not change it; focus on summary, importance and lessor analysis.
- If category_locked=false, provisional_category is only a deterministic hint; choose the FINAL
  category yourself from the eight allowed categories.

The user cares especially about lessee credit, aircraft supply/demand, lease rates and values,
engine/MRO constraints, financing conditions, repossession/legal risk and macro/geopolitical
transmission into aviation leasing.

For every supplied story:
1) for unlocked stories only, assign exactly one FINAL allowed category;
2) write a concise factual Chinese summary (normally 35-90 Chinese characters);
3) write one concise Chinese sentence explaining why it matters to an aircraft lessor;
4) score market_materiality, lessor_relevance and macro_relevance from 0-100;
5) mark critical_candidate only for developments that could require prompt front-office attention;
6) identify macro tags, key entities and aircraft types only when supported by input.
Return JSON only. Do not drop any supplied story ID."""
        prompt = {
            "allowed_categories": ALLOWED_CATEGORIES,
            "classification_policy": {
                "category_locked_true": "Do not classify. fixed_category is final and authoritative.",
                "category_locked_false": "Choose the final category; provisional_category is only a hint.",
            },
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
                    "category": "required only when category_locked=false; omit for locked stories",
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
            "stories": prepared,
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
        sid = an.clean(row.get("id"))
        if sid in locked_category_by_id:
            result["category"] = locked_category_by_id[sid]
        else:
            category = an.clean(row.get("category"))
            if category in ALLOWED_CATEGORIES:
                result["category"] = category
        return result

    def unified_enrich(story: dict[str, Any], analysis: dict[str, Any], config: dict[str, Any], raw: dict[str, dict[str, Any]], now):
        # Use the current AI/Python-locked category, otherwise preserve the prior AI category
        # for reused stories.
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
