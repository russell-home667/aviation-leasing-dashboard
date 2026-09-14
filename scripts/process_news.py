#!/usr/bin/env python3
"""Step 2: clean, cluster, classify and enrich aviation-leasing news.

Input:  data/news_raw.json (Step 1 broad candidate pool)
Output: data/news.json (rolling current feed)
        data/news_archive.json (120-day clustered archive)
        data/news_processing_status.json

DeepSeek is used only on candidate metadata/headline/summary already collected in Step 1.
No paid article body is fetched here.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
RAW_PATH = ROOT / "data" / "news_raw.json"
OUT_PATH = ROOT / "data" / "news.json"
ARCHIVE_PATH = ROOT / "data" / "news_archive.json"
STATUS_PATH = ROOT / "data" / "news_processing_status.json"

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
CHAT_API = "https://api.deepseek.com/chat/completions"
CURRENT_DAYS = int(os.getenv("NEWS_CURRENT_DAYS", "7"))
ARCHIVE_DAYS = int(os.getenv("NEWS_ARCHIVE_DAYS", "120"))
MAX_AI_CANDIDATES = int(os.getenv("NEWS_MAX_AI_CANDIDATES", "360"))
BATCH_SIZE = int(os.getenv("NEWS_AI_BATCH_SIZE", "45"))

CATEGORIES = [
    "Leasing & Trading",
    "Aircraft & OEM",
    "Airlines & Credit",
    "Financing & Capital Markets",
    "Engines & MRO",
    "Values & Lease Rates",
    "Legal & Regulatory",
    "Macro & Geopolitics",
]

CORE_SOURCES = {"Ishka Airfinance", "Aviation News Online", "Reuters", "Flightglobal", "FlightGlobal"}
SOURCE_RANK = {
    "Reuters": 100,
    "Ishka Airfinance": 98,
    "Flightglobal": 96,
    "FlightGlobal": 96,
    "Aviation News Online": 92,
    "Bloomberg": 90,
    "Financial Times": 90,
    "Wall Street Journal": 90,
    "CNBC": 84,
    "Associated Press": 84,
}

STOP = {
    "a","an","the","and","or","of","to","for","in","on","at","by","from","with","as","is","are",
    "was","were","be","been","being","its","it","this","that","these","those","after","before",
    "will","would","could","may","might","into","over","under","new","more","says","said",
}

ENTITY_PATTERNS = [
    re.compile(r"\b(?:Airbus|Boeing|Embraer|COMAC|ATR)\b", re.I),
    re.compile(r"\b(?:AerCap|Avolon|BOC Aviation|SMBC Aviation Capital|Dubai Aerospace Enterprise|DAE|CDB Aviation|CALC|Air Lease|Aviation Capital Group|ACG|Aircastle|Castlelake|Azorra|TrueNoord|Nordic Aviation Capital|NAC|Macquarie AirFinance|ORIX Aviation|Jackson Square Aviation|JSA|Griffin Global Asset Management|SKY Leasing)\b", re.I),
    re.compile(r"\b(?:Pratt\s*&\s*Whitney|Pratt Whitney|GE Aerospace|CFM International|Rolls-Royce|Safran|MTU)\b", re.I),
    re.compile(r"\b(?:A220(?:-\d{3})?|A3(?:19|20|21|30|40|50)(?:neo)?(?:-\d{3,4})?|A380(?:-\d{3})?|7(?:37|47|67|77|87)(?:-\d{3,4}| MAX ?\d+)?|E(?:170|175|190|195)(?:-E2)?|CRJ(?:200|700|900|1000)|C909|C919|C929|ARJ21|ATR ?(?:42|72)(?:-\d{3})?)\b", re.I),
    re.compile(r"\b(?:PW1\d{3}G|PW1100G|PW1500G|LEAP-1[ABC]|CFM56|GEnx(?:-\dB)?|GE9X|Trent ?(?:7000|1000|XWB|700|800|900))\b", re.I),
]

KEYWORD_WEIGHTS = {
    "Leasing & Trading": {
        "lease": 5, "leasing": 6, "lessor": 6, "sale and leaseback": 8, "sale-leaseback": 8,
        "slb": 7, "aircraft sale": 5, "portfolio": 3, "trading": 4, "placement": 3,
        "remarketing": 5, "lease deal": 6, "lease agreement": 6,
    },
    "Aircraft & OEM": {
        "airbus": 5, "boeing": 5, "embraer": 5, "comac": 5, "atr": 3, "aircraft order": 7,
        "orders": 2, "delivery": 4, "deliveries": 4, "production": 3, "backlog": 3, "certification": 3,
        "a320": 3, "a321": 3, "a350": 3, "737": 3, "777": 3, "787": 3, "e2": 3, "c919": 3,
    },
    "Airlines & Credit": {
        "airline": 4, "carrier": 3, "bankruptcy": 8, "insolvency": 8, "restructuring": 7, "default": 7,
        "liquidity": 5, "credit": 5, "profit": 3, "loss": 3, "results": 2, "traffic": 2,
        "capacity": 2, "fleet": 3, "grounding": 4, "suspends": 4,
    },
    "Financing & Capital Markets": {
        "financing": 7, "finance": 5, "bond": 5, "abs": 7, "securit": 7, "loan": 5, "debt": 5,
        "jolco": 8, "jol": 6, "pdp": 7, "capital markets": 8, "refinanc": 6, "warehouse": 4,
        "credit facility": 6, "ipo": 4, "funding": 4,
    },
    "Engines & MRO": {
        "engine": 5, "mro": 7, "maintenance": 6, "overhaul": 6, "shop visit": 7, "pratt": 6,
        "pw1100": 7, "gtf": 7, "cfm": 5, "leap": 6, "rolls-royce": 5, "trent": 5, "safran": 4,
        "spare engine": 7, "parts": 3, "repair": 5,
    },
    "Values & Lease Rates": {
        "lease rate": 9, "lease rates": 9, "aircraft value": 9, "aircraft values": 9, "valuation": 7,
        "market value": 8, "base value": 8, "residual value": 8, "apprais": 7, "rental": 5,
        "half-life": 4, "full-life": 4,
    },
    "Legal & Regulatory": {
        "regulat": 6, "legal": 5, "court": 5, "lawsuit": 6, "litigation": 6, "sanction": 6,
        "cape town convention": 8, "repossession": 8, "repossess": 8, "law": 4, "compliance": 5,
        "antitrust": 5, "faa": 4, "easa": 4, "authority": 2,
    },
    "Macro & Geopolitics": {
        "war": 6, "conflict": 6, "geopolit": 7, "tariff": 6, "oil price": 6, "jet fuel": 7,
        "interest rate": 6, "rates": 2, "inflation": 5, "gdp": 4, "trade war": 7, "airspace": 5,
        "middle east": 3, "russia": 3, "ukraine": 3, "iran": 3,
    },
}

AVIATION_TERMS = (
    "aircraft","airline","aviation","lessor","lease","leasing","airbus","boeing","embraer","comac",
    "engine","mro","airport","flight","fleet","aercap","avolon","boc aviation","smbc","air lease",
    "a320","a321","a350","737","777","787","c919","e190","e195","atr",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def norm(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def tokens(value: str) -> set[str]:
    return {x for x in norm(value).split() if len(x) > 1 and x not in STOP}


def jaccard(a: str, b: str) -> float:
    x, y = tokens(a), tokens(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def title_similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    jac = jaccard(a, b)
    return max(seq, jac, (seq + jac) / 2)


def extract_entities(*texts: str) -> list[str]:
    text = " | ".join(clean_text(x) for x in texts if x)
    found = []
    for pattern in ENTITY_PATTERNS:
        for m in pattern.finditer(text):
            value = clean_text(m.group(0))
            key = value.lower()
            if key not in {x.lower() for x in found}:
                found.append(value)
    return found[:16]


def fallback_category(item: dict[str, Any]) -> str:
    text = " ".join([
        item.get("title", ""),
        item.get("summary", ""),
        " ".join(item.get("categories_raw") or []),
        item.get("source_channel", ""),
    ]).lower()
    best_cat, best_score = "Airlines & Credit", -1
    for cat, weights in KEYWORD_WEIGHTS.items():
        score = 0
        for kw, weight in weights.items():
            if kw in text:
                score += weight
        if score > best_score:
            best_cat, best_score = cat, score
    if best_score <= 0:
        if item.get("source_channel", "").startswith("ishka_"):
            return "Leasing & Trading"
        if item.get("source_channel") in {"aviationnews_engine", "aviationnews_maintenance"}:
            return "Engines & MRO"
        if item.get("source_channel") in {"aviationnews_legal", "aviationnews_regulatory"}:
            return "Legal & Regulatory"
    return best_cat


def source_rank(name: str) -> int:
    return SOURCE_RANK.get(name, 60)


def importance_seed(item: dict[str, Any]) -> int:
    text = (item.get("title", "") + " " + item.get("summary", "")).lower()
    score = 42 + max(0, source_rank(item.get("source", "")) - 60) // 4
    if item.get("source") in CORE_SOURCES:
        score += 8
    strong = (
        "bankruptcy","insolvency","default","repossession","sale and leaseback","sale-leaseback",
        "aircraft order","orders","delivery","financing","abs","jolco","lease rate","aircraft value",
        "gtf","grounding","sanction","merger","acquisition",
    )
    score += min(25, sum(4 for kw in strong if kw in text))
    return max(35, min(88, score))


def item_dt(item: dict[str, Any]) -> datetime | None:
    return parse_dt(item.get("published_at")) or parse_dt(item.get("fetched_at"))


def raw_candidate_priority(item: dict[str, Any], now: datetime) -> float:
    dt = item_dt(item) or now - timedelta(days=30)
    age_h = max(0.0, (now - dt).total_seconds() / 3600)
    recency = max(0.0, 48.0 - age_h) / 6.0
    text = (item.get("title", "") + " " + item.get("summary", "")).lower()
    aviation = sum(1 for kw in AVIATION_TERMS if kw in text)
    return source_rank(item.get("source", "")) + recency + min(18, aviation * 2)


def choose_recent_items(raw_items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=CURRENT_DAYS, hours=4)
    recent = []
    seen = set()
    for item in raw_items:
        dt = item_dt(item)
        if not dt or dt < cutoff:
            continue
        title, url = clean_text(item.get("title")), clean_text(item.get("url"))
        if not title or not url:
            continue
        key = item.get("id") or hashlib.sha1((norm(title) + "|" + url).encode()).hexdigest()[:24]
        if key in seen:
            continue
        seen.add(key)
        x = dict(item)
        x["_candidate_key"] = key
        x["_dt"] = dt
        x["_entities"] = extract_entities(title, item.get("summary", ""))
        recent.append(x)
    recent.sort(key=lambda x: raw_candidate_priority(x, now), reverse=True)
    if len(recent) <= MAX_AI_CANDIDATES:
        return recent

    core = [x for x in recent if x.get("source") in CORE_SOURCES]
    noncore = [x for x in recent if x.get("source") not in CORE_SOURCES]
    selected = core[:MAX_AI_CANDIDATES]
    selected_keys = {x["_candidate_key"] for x in selected}
    for x in noncore:
        if len(selected) >= MAX_AI_CANDIDATES:
            break
        if x["_candidate_key"] not in selected_keys:
            selected.append(x)
            selected_keys.add(x["_candidate_key"])
    selected.sort(key=lambda x: x["_dt"], reverse=True)
    return selected


def compact_candidate(item: dict[str, Any], cid: str) -> dict[str, Any]:
    return {
        "id": cid,
        "title": clean_text(item.get("title"))[:300],
        "source": clean_text(item.get("source"))[:100],
        "published_at": iso(item_dt(item)),
        "summary": clean_text(item.get("summary"))[:500],
        "categories_raw": (item.get("categories_raw") or [])[:8],
        "region_raw": clean_text(item.get("region_raw"))[:80],
        "entities_hint": item.get("_entities", [])[:10],
    }


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}


def call_deepseek(api_key: str, batch: list[dict[str, Any]], mode: str = "normal") -> dict[str, Any]:
    system = """You are the news editor for an aircraft-leasing front-office market-intelligence dashboard.
Analyze ONLY the supplied candidate metadata. Never invent an event, source, date, URL, company, aircraft type or fact.
First remove candidates that are not genuinely about civil aviation, airlines, commercial aircraft, engines/MRO, aircraft leasing/trading, aviation finance/capital markets, aircraft values/lease rates, aviation law/regulation, or macro/geopolitical developments with a clear aviation impact.
Then merge candidates that describe the SAME underlying event. Different publishers covering the same event must become one story and all matching candidate IDs must be retained.
Classify every retained event into exactly one allowed category. Write concise Chinese summary and concise Chinese 'why it matters for lessors'. Importance is 0-100 from the viewpoint of aircraft lessors/front-office teams.
Do not drop a materially important aviation event simply because only one source covers it. Return valid JSON only."""

    prompt = {
        "allowed_categories": CATEGORIES,
        "rules": {
            "same_event": "Use headline, named companies, aircraft/engine type, transaction, geography and timing. Do not merge merely because two stories mention the same airline or OEM.",
            "importance_guide": {
                "90-100": "market-moving / major lessor, airline credit, OEM, sanctions, large order/financing, systemic engine issue",
                "75-89": "important transaction, fleet/order/delivery, funding, material operating/credit development",
                "60-74": "useful market intelligence",
                "below_60": "routine/minor; may still be retained if clearly aviation-relevant"
            }
        },
        "return_shape": {
            "stories": [{
                "title": "English headline, concise and factual",
                "summary_zh": "1-2 concise Chinese sentences",
                "why_it_matters_zh": "1 concise Chinese sentence focused on lessors",
                "category": "one exact allowed category",
                "importance_score": 75,
                "region": "short region/country label",
                "entities": ["company / airline / lessor / OEM / aircraft / engine"],
                "candidate_ids": ["c001", "c002"]
            }]
        },
        "candidates": batch,
    }
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 9000,
    }
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    last = None
    for attempt in range(3):
        try:
            r = requests.post(CHAT_API, headers=headers, json=body, timeout=180)
            r.raise_for_status()
            content = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content", "")
            if not content:
                raise ValueError("empty DeepSeek content")
            return extract_json(content)
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"DeepSeek failed after 3 attempts: {last}")


def source_obj(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": clean_text(item.get("source")) or "Unknown",
        "url": clean_text(item.get("url")),
        "published_at": iso(item_dt(item)),
        "source_channel": clean_text(item.get("source_channel")),
        "discovered_via": clean_text(item.get("discovered_via")),
    }


def story_id(title: str, published_at: str | None, entities: list[str]) -> str:
    day = (published_at or "")[:10]
    anchor = "|".join(sorted(norm(x) for x in entities[:4]))
    base = norm(title) + "|" + day + "|" + anchor
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:18]


def ai_story_to_record(story: dict[str, Any], lookup: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    ids = []
    for cid in story.get("candidate_ids") or []:
        cid = str(cid)
        if cid in lookup and cid not in ids:
            ids.append(cid)
    if not ids:
        return None
    items = [lookup[x] for x in ids]
    items.sort(key=lambda x: (source_rank(x.get("source", "")), item_dt(x) or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    primary = items[0]
    category = story.get("category")
    if category not in CATEGORIES:
        category = fallback_category(primary)
    try:
        importance = max(0, min(100, int(story.get("importance_score", importance_seed(primary)))))
    except Exception:
        importance = importance_seed(primary)

    sources, seen_urls = [], set()
    for item in items:
        src = source_obj(item)
        if not src["url"] or src["url"] in seen_urls:
            continue
        seen_urls.add(src["url"])
        sources.append(src)
    entities = []
    for x in (story.get("entities") or []) + [z for item in items for z in item.get("_entities", [])]:
        x = clean_text(x)
        if x and x.lower() not in {e.lower() for e in entities}:
            entities.append(x)
    entities = entities[:16]
    published = max((item_dt(x) for x in items if item_dt(x)), default=now)
    title = clean_text(story.get("title")) or clean_text(primary.get("title"))
    summary_zh = clean_text(story.get("summary_zh"))[:500]
    why = clean_text(story.get("why_it_matters_zh"))[:400]
    region = clean_text(story.get("region")) or clean_text(primary.get("region_raw")) or "Global/Unspecified"
    record = {
        "id": story_id(title, iso(published), entities),
        "title": title,
        "summary_zh": summary_zh,
        "why_it_matters_zh": why,
        "category": category,
        "importance_score": importance,
        "region": region,
        "entities": entities,
        "published_at": iso(published),
        "updated_at": iso(now),
        "primary_source": sources[0]["name"] if sources else clean_text(primary.get("source")),
        "primary_url": sources[0]["url"] if sources else clean_text(primary.get("url")),
        "source_count": len(sources),
        "sources": sources,
        "raw_ids": [x.get("_candidate_key") for x in items if x.get("_candidate_key")],
        "analysis_mode": "deepseek",
    }
    return record


def fallback_story(item: dict[str, Any], now: datetime, reason: str = "rules") -> dict[str, Any]:
    published = item_dt(item) or now
    entities = item.get("_entities") or extract_entities(item.get("title", ""), item.get("summary", ""))
    title = clean_text(item.get("title"))
    summary = clean_text(item.get("summary"))
    return {
        "id": story_id(title, iso(published), entities),
        "title": title,
        "summary_zh": "",
        "why_it_matters_zh": "",
        "category": fallback_category(item),
        "importance_score": importance_seed(item),
        "region": clean_text(item.get("region_raw")) or "Global/Unspecified",
        "entities": entities[:16],
        "published_at": iso(published),
        "updated_at": iso(now),
        "primary_source": clean_text(item.get("source")) or "Unknown",
        "primary_url": clean_text(item.get("url")),
        "source_count": 1,
        "sources": [source_obj(item)],
        "raw_ids": [item.get("_candidate_key")] if item.get("_candidate_key") else [],
        "analysis_mode": reason,
        "source_excerpt": summary[:350],
    }


def entity_keys(story: dict[str, Any]) -> set[str]:
    return {norm(x) for x in story.get("entities") or [] if norm(x)}


def story_dt(story: dict[str, Any]) -> datetime | None:
    return parse_dt(story.get("published_at"))


def same_event(a: dict[str, Any], b: dict[str, Any]) -> bool:
    urls_a = {x.get("url") for x in a.get("sources") or [] if x.get("url")}
    urls_b = {x.get("url") for x in b.get("sources") or [] if x.get("url")}
    if urls_a & urls_b:
        return True
    raw_a, raw_b = set(a.get("raw_ids") or []), set(b.get("raw_ids") or [])
    if raw_a & raw_b:
        return True
    da, db = story_dt(a), story_dt(b)
    if da and db and abs((da - db).total_seconds()) > 72 * 3600:
        return False
    sim = title_similarity(a.get("title", ""), b.get("title", ""))
    if sim >= 0.82:
        return True
    ent_overlap = entity_keys(a) & entity_keys(b)
    if sim >= 0.64 and ent_overlap:
        return True
    if sim >= 0.56 and len(ent_overlap) >= 2:
        return True
    return False


def merge_two(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    sa = (int(a.get("importance_score", 0)), 1 if a.get("analysis_mode") == "deepseek" else 0, len(a.get("sources") or []))
    sb = (int(b.get("importance_score", 0)), 1 if b.get("analysis_mode") == "deepseek" else 0, len(b.get("sources") or []))
    base, other = (dict(a), b) if sa >= sb else (dict(b), a)

    sources, seen = [], set()
    for src in (base.get("sources") or []) + (other.get("sources") or []):
        url = src.get("url")
        key = url or (src.get("name"), src.get("published_at"))
        if key in seen:
            continue
        seen.add(key)
        sources.append(src)
    sources.sort(key=lambda x: source_rank(x.get("name", "")), reverse=True)
    base["sources"] = sources[:8]
    base["source_count"] = len(base["sources"])
    if sources:
        base["primary_source"] = sources[0].get("name") or base.get("primary_source")
        base["primary_url"] = sources[0].get("url") or base.get("primary_url")

    entities = []
    for x in (base.get("entities") or []) + (other.get("entities") or []):
        x = clean_text(x)
        if x and x.lower() not in {e.lower() for e in entities}:
            entities.append(x)
    base["entities"] = entities[:16]
    base["raw_ids"] = list(dict.fromkeys((base.get("raw_ids") or []) + (other.get("raw_ids") or [])))[:30]
    base["importance_score"] = max(int(a.get("importance_score", 0)), int(b.get("importance_score", 0)))
    dates = [x for x in [story_dt(a), story_dt(b)] if x]
    if dates:
        base["published_at"] = iso(max(dates))
    updates = [parse_dt(x.get("updated_at")) for x in (a, b)]
    updates = [x for x in updates if x]
    if updates:
        base["updated_at"] = iso(max(updates))
    base["id"] = story_id(base.get("title", ""), base.get("published_at"), base.get("entities") or [])
    return base


def consolidate(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        stories,
        key=lambda s: (int(s.get("importance_score", 0)), story_dt(s) or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for story in ordered:
        match_idx = next((i for i, existing in enumerate(out) if same_event(story, existing)), None)
        if match_idx is None:
            out.append(story)
        else:
            out[match_idx] = merge_two(out[match_idx], story)
    return out


def load_archive() -> list[dict[str, Any]]:
    if not ARCHIVE_PATH.exists():
        return []
    try:
        data = json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
        return data.get("stories", []) if isinstance(data, dict) else []
    except Exception:
        return []


def analyze_recent(api_key: str, candidates: list[dict[str, Any]], now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    numbered = []
    for i, item in enumerate(candidates, 1):
        cid = f"c{i:04d}"
        lookup[cid] = item
        numbered.append((cid, item))

    ai_records: list[dict[str, Any]] = []
    referenced: set[str] = set()
    calls, errors = 0, []
    for start in range(0, len(numbered), BATCH_SIZE):
        batch_rows = numbered[start:start + BATCH_SIZE]
        compact = [compact_candidate(item, cid) for cid, item in batch_rows]
        try:
            data = call_deepseek(api_key, compact)
            calls += 1
            for s in data.get("stories") or []:
                record = ai_story_to_record(s, lookup, now)
                if record:
                    ai_records.append(record)
                    for cid in s.get("candidate_ids") or []:
                        if str(cid) in lookup:
                            referenced.add(str(cid))
        except Exception as exc:
            errors.append(f"batch_{start // BATCH_SIZE + 1}: {type(exc).__name__}: {exc}")
            for cid, item in batch_rows:
                if item.get("source") in CORE_SOURCES:
                    ai_records.append(fallback_story(item, now, reason="fallback_after_ai_error"))
                    referenced.add(cid)

    for cid, item in numbered:
        if cid not in referenced and item.get("source") in CORE_SOURCES:
            ai_records.append(fallback_story(item, now, reason="core_source_safety_net"))
            referenced.add(cid)

    return consolidate(ai_records), {
        "deepseek_calls": calls,
        "deepseek_errors": errors,
        "referenced_candidates": len(referenced),
        "unreferenced_candidates": max(0, len(candidates) - len(referenced)),
    }


def fallback_all(candidates: list[dict[str, Any]], now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stories = []
    for item in candidates:
        text = (item.get("title", "") + " " + item.get("summary", "")).lower()
        clearly_aviation = any(term in text for term in AVIATION_TERMS)
        if item.get("source") in CORE_SOURCES or clearly_aviation:
            stories.append(fallback_story(item, now, reason="fallback_no_deepseek_key"))
    return consolidate(stories), {
        "deepseek_calls": 0,
        "deepseek_errors": ["DEEPSEEK_API_KEY is missing; rules fallback used."],
        "referenced_candidates": len(stories),
        "unreferenced_candidates": max(0, len(candidates) - len(stories)),
    }


def payload(categories: list[str], stories: list[dict[str, Any]], now: datetime, stats: dict[str, Any], model: str, mode: str) -> dict[str, Any]:
    by_category = Counter(s.get("category", "Unknown") for s in stories)
    by_source = Counter()
    for s in stories:
        for src in s.get("sources") or []:
            by_source[src.get("name", "Unknown")] += 1
    enriched = sum(1 for s in stories if s.get("analysis_mode") == "deepseek")
    return {
        "schema_version": 2,
        "generated_at": iso(now),
        "model": model,
        "analysis_mode": mode,
        "categories": categories,
        "stats": {
            **stats,
            "stories": len(stories),
            "deepseek_enriched_stories": enriched,
            "by_category": dict(sorted(by_category.items())),
            "source_mentions": dict(sorted(by_source.items())),
        },
        "stories": stories,
    }


def main() -> int:
    now = utcnow()
    if not RAW_PATH.exists():
        raise SystemExit("data/news_raw.json missing")
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    raw_items = raw.get("items", []) if isinstance(raw, dict) else []
    candidates = choose_recent_items(raw_items, now)

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if api_key:
        current_stories, analysis_stats = analyze_recent(api_key, candidates, now)
        mode = "deepseek"
    else:
        current_stories, analysis_stats = fallback_all(candidates, now)
        mode = "fallback_rules"

    current_cutoff = now - timedelta(days=CURRENT_DAYS)
    current_stories = [
        s for s in current_stories
        if (story_dt(s) or datetime.min.replace(tzinfo=timezone.utc)) >= current_cutoff
        and s.get("category") in CATEGORIES
    ]
    current_stories.sort(
        key=lambda s: (int(s.get("importance_score", 0)), story_dt(s) or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )

    existing_archive = load_archive()
    archive_cutoff = now - timedelta(days=ARCHIVE_DAYS)
    archive_pool = []
    for s in current_stories + existing_archive:
        d = story_dt(s)
        if d and d >= archive_cutoff and s.get("category") in CATEGORIES:
            archive_pool.append(s)
    archive_stories = consolidate(archive_pool)
    archive_stories.sort(key=lambda s: story_dt(s) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    common_stats = {
        "raw_pool_total": len(raw_items),
        "recent_candidates_selected": len(candidates),
        "core_candidates_selected": sum(1 for x in candidates if x.get("source") in CORE_SOURCES),
        **analysis_stats,
    }
    OUT_PATH.write_text(
        json.dumps(payload(CATEGORIES, current_stories, now, common_stats, MODEL if api_key else "rules-fallback", mode), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    archive_stats = {
        "archive_retention_days": ARCHIVE_DAYS,
        "current_feed_stories": len(current_stories),
        "existing_archive_input": len(existing_archive),
    }
    ARCHIVE_PATH.write_text(
        json.dumps(payload(CATEGORIES, archive_stories, now, archive_stats, MODEL if api_key else "rules-fallback", mode), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    status = {
        "schema_version": 1,
        "checked_at": iso(now),
        "status": "pass" if current_stories else "warn",
        "analysis_mode": mode,
        "deepseek_configured": bool(api_key),
        "categories": CATEGORIES,
        "raw_pool_total": len(raw_items),
        "recent_candidates_selected": len(candidates),
        "current_stories": len(current_stories),
        "archive_stories": len(archive_stories),
        "deepseek_calls": analysis_stats.get("deepseek_calls", 0),
        "deepseek_errors": analysis_stats.get("deepseek_errors", []),
        "empty_chinese_enrichment_count": sum(
            1 for s in current_stories if not s.get("summary_zh") or not s.get("why_it_matters_zh")
        ),
    }
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
