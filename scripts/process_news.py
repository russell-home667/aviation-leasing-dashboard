#!/usr/bin/env python3
"""Step 2: clean, de-duplicate, event-cluster and classify aviation-leasing news.

Consumes Step-1 metadata only. It never fetches paid article bodies.
Outputs:
  data/news.json                    rolling 7-day processed feed
  data/news_archive.json            rolling processed archive
  data/news_processing_status.json  processing/AI health

Design goals:
- preserve coverage from Ishka / Aviation News Online / Reuters / FlightGlobal;
- merge different publishers covering the same underlying event;
- classify every retained event into one of eight fixed categories;
- use DeepSeek for semantic decisions, with deterministic fallbacks;
- process only new raw records after the first successful run so hourly jobs stay cheap.
"""
from __future__ import annotations

import hashlib
import json
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
MAX_NEW_AI_EVENTS = int(os.getenv("NEWS_MAX_AI_CANDIDATES", "360"))
BATCH_SIZE = int(os.getenv("NEWS_AI_BATCH_SIZE", "30"))

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
    "FlightGlobal": 96,
    "Flightglobal": 96,
    "Aviation News Online": 92,
    "Bloomberg": 90,
    "Financial Times": 90,
    "Wall Street Journal": 90,
    "Associated Press": 86,
    "CNBC": 84,
}
STOP = {
    "a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "at", "by", "from", "with",
    "as", "is", "are", "was", "were", "be", "been", "being", "its", "it", "this", "that", "these",
    "those", "after", "before", "will", "would", "could", "may", "might", "into", "over", "under",
    "new", "more", "says", "said",
}

ENTITY_PATTERNS = [
    re.compile(r"\b(?:Airbus|Boeing|Embraer|COMAC|ATR)\b", re.I),
    re.compile(r"\b(?:AerCap|Avolon|BOC Aviation|SMBC Aviation Capital|Dubai Aerospace Enterprise|DAE|CDB Aviation|CALC|Air Lease|Aviation Capital Group|ACG|Aircastle|Castlelake|Azorra|TrueNoord|Nordic Aviation Capital|NAC|Macquarie AirFinance|ORIX Aviation|Jackson Square Aviation|JSA|Griffin Global Asset Management|SKY Leasing)\b", re.I),
    re.compile(r"\b(?:Pratt\s*&\s*Whitney|Pratt Whitney|GE Aerospace|CFM International|Rolls-Royce|Safran|MTU)\b", re.I),
    re.compile(r"\b(?:A220(?:-\d{3})?|A3(?:19|20|21|30|40|50)(?:neo)?(?:-\d{3,4})?|A380(?:-\d{3})?|7(?:37|47|67|77|87)(?:-\d{3,4}| MAX ?\d+)?|E(?:170|175|190|195)(?:-E2)?|CRJ(?:200|700|900|1000)|C909|C919|C929|ARJ21|ATR ?(?:42|72)(?:-\d{3})?)\b", re.I),
    re.compile(r"\b(?:PW1100G|PW1500G|LEAP-1[ABC]|CFM56|GEnx(?:-\dB)?|GE9X|Trent ?(?:7000|1000|XWB|700|800|900))\b", re.I),
]

KEYWORD_WEIGHTS = {
    "Leasing & Trading": {
        "lease": 5, "leasing": 6, "lessor": 6, "sale and leaseback": 9, "sale-leaseback": 9,
        "slb": 8, "aircraft sale": 5, "portfolio": 3, "trading": 4, "remarketing": 6,
    },
    "Aircraft & OEM": {
        "airbus": 5, "boeing": 5, "embraer": 5, "comac": 5, "aircraft order": 8, "orders": 2,
        "delivery": 4, "deliveries": 4, "production": 4, "backlog": 3, "certification": 4,
        "a320": 3, "a321": 3, "a350": 3, "737": 3, "777": 3, "787": 3, "e2": 3, "c919": 3,
    },
    "Airlines & Credit": {
        "airline": 4, "carrier": 3, "bankruptcy": 9, "insolvency": 9, "restructuring": 8, "default": 8,
        "liquidity": 6, "credit": 5, "profit": 3, "loss": 3, "results": 2, "traffic": 2,
        "capacity": 2, "fleet": 3, "grounding": 5,
    },
    "Financing & Capital Markets": {
        "financing": 8, "finance": 5, "bond": 6, "abs": 8, "securit": 8, "loan": 6, "debt": 6,
        "jolco": 9, "jol": 7, "pdp": 8, "capital markets": 9, "refinanc": 7, "warehouse": 5,
        "credit facility": 7, "funding": 4,
    },
    "Engines & MRO": {
        "engine": 5, "mro": 8, "maintenance": 7, "overhaul": 7, "shop visit": 8, "pratt": 6,
        "pw1100": 8, "gtf": 8, "cfm": 5, "leap": 6, "rolls-royce": 5, "trent": 5,
        "spare engine": 8, "repair": 5,
    },
    "Values & Lease Rates": {
        "lease rate": 10, "lease rates": 10, "aircraft value": 10, "aircraft values": 10,
        "valuation": 8, "market value": 9, "base value": 9, "residual value": 9, "apprais": 8,
        "half-life": 5, "full-life": 5,
    },
    "Legal & Regulatory": {
        "regulat": 7, "legal": 6, "court": 6, "lawsuit": 7, "litigation": 7, "sanction": 7,
        "cape town convention": 9, "repossession": 9, "repossess": 9, "compliance": 6,
        "antitrust": 6, "faa": 4, "easa": 4,
    },
    "Macro & Geopolitics": {
        "war": 7, "conflict": 7, "geopolit": 8, "tariff": 7, "oil price": 7, "jet fuel": 8,
        "interest rate": 7, "inflation": 6, "gdp": 4, "trade war": 8, "airspace": 6,
        "russia": 3, "ukraine": 3, "iran": 3,
    },
}

AVIATION_TERMS = (
    "aircraft", "airline", "aviation", "lessor", "lease", "leasing", "airbus", "boeing", "embraer",
    "comac", "engine", "mro", "fleet", "aercap", "avolon", "boc aviation", "smbc", "air lease",
    "a320", "a321", "a350", "737", "777", "787", "c919", "e190", "e195", "atr",
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
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def norm(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).split())


def wordset(value: str) -> set[str]:
    return {w for w in norm(value).split() if len(w) > 1 and w not in STOP}


def similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    x, y = wordset(a), wordset(b)
    jac = len(x & y) / len(x | y) if x and y else 0.0
    return max(seq, jac, (seq + jac) / 2)


def source_rank(name: str) -> int:
    return SOURCE_RANK.get(clean(name), 60)


def item_dt(item: dict[str, Any]) -> datetime | None:
    return parse_dt(item.get("published_at")) or parse_dt(item.get("fetched_at"))


def extract_entities(*texts: str) -> list[str]:
    text = " | ".join(clean(x) for x in texts if x)
    out: list[str] = []
    for pat in ENTITY_PATTERNS:
        for m in pat.finditer(text):
            v = clean(m.group(0))
            if v and v.lower() not in {x.lower() for x in out}:
                out.append(v)
    return out[:16]


def fallback_category_text(text: str, source_channel: str = "") -> str:
    low = clean(text).lower()
    scores: dict[str, int] = {}
    for cat, weights in KEYWORD_WEIGHTS.items():
        scores[cat] = sum(weight for kw, weight in weights.items() if kw in low)
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        if source_channel in {"aviationnews_engine", "aviationnews_maintenance"}:
            return "Engines & MRO"
        if source_channel in {"aviationnews_legal", "aviationnews_regulatory"}:
            return "Legal & Regulatory"
        if source_channel.startswith("ishka_"):
            return "Leasing & Trading"
        return "Airlines & Credit"
    return best


def seed_importance(text: str, sources: list[dict[str, Any]]) -> int:
    low = clean(text).lower()
    top_rank = max([source_rank(s.get("name", "")) for s in sources] or [60])
    score = 45 + max(0, top_rank - 60) // 4
    strong = (
        "bankruptcy", "insolvency", "default", "repossession", "sale and leaseback", "sale-leaseback",
        "aircraft order", "financing", "abs", "jolco", "lease rate", "aircraft value", "gtf",
        "grounding", "sanction", "merger", "acquisition",
    )
    score += min(24, sum(4 for kw in strong if kw in low))
    return max(35, min(88, score))


def source_obj(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": clean(item.get("source")) or "Unknown",
        "url": clean(item.get("url")),
        "published_at": iso(item_dt(item)),
        "source_channel": clean(item.get("source_channel")),
        "discovered_via": clean(item.get("discovered_via")),
    }


def prepare_recent(raw_items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=CURRENT_DAYS, hours=4)
    out, seen = [], set()
    for item in raw_items:
        dt = item_dt(item)
        if not dt or dt < cutoff:
            continue
        title, url = clean(item.get("title")), clean(item.get("url"))
        if not title or not url:
            continue
        rid = clean(item.get("id")) or hashlib.sha1((norm(title) + "|" + url).encode()).hexdigest()[:24]
        if rid in seen:
            continue
        seen.add(rid)
        x = dict(item)
        x["_rid"] = rid
        x["_dt"] = dt
        x["_entities"] = extract_entities(title, item.get("summary", ""))
        out.append(x)
    out.sort(key=lambda x: x["_dt"], reverse=True)
    return out


def precluster(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cheap conservative de-dupe before AI. Never merges merely on one shared company."""
    clusters: list[dict[str, Any]] = []
    for item in items:
        matched = None
        for c in clusters:
            if abs((item["_dt"] - c["latest_dt"]).total_seconds()) > 72 * 3600:
                continue
            if clean(item.get("url")) in c["urls"]:
                matched = c
                break
            if similarity(item.get("title", ""), c["representative"].get("title", "")) >= 0.88:
                matched = c
                break
        if matched is None:
            clusters.append({"items": [item], "latest_dt": item["_dt"], "representative": item, "urls": {clean(item.get("url"))}})
        else:
            matched["items"].append(item)
            matched["urls"].add(clean(item.get("url")))
            matched["latest_dt"] = max(matched["latest_dt"], item["_dt"])
            if source_rank(item.get("source", "")) > source_rank(matched["representative"].get("source", "")):
                matched["representative"] = item
    return clusters


def load_archive() -> list[dict[str, Any]]:
    if not ARCHIVE_PATH.exists():
        return []
    try:
        data = json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
        return data.get("stories", []) if isinstance(data, dict) else []
    except Exception:
        return []


def processed_raw_ids(stories: list[dict[str, Any]]) -> set[str]:
    return {str(rid) for s in stories for rid in (s.get("raw_ids") or []) if rid}


def cluster_payload(cluster: dict[str, Any], pid: str) -> dict[str, Any]:
    rep = cluster["representative"]
    return {
        "id": pid,
        "title": clean(rep.get("title"))[:300],
        "summary": clean(rep.get("summary"))[:450],
        "published_at": iso(cluster["latest_dt"]),
        "sources": sorted({clean(x.get("source")) for x in cluster["items"] if clean(x.get("source"))}),
        "categories_raw": sorted({c for x in cluster["items"] for c in (x.get("categories_raw") or [])})[:10],
        "region_raw": clean(rep.get("region_raw"))[:80],
        "entities_hint": sorted({e for x in cluster["items"] for e in x.get("_entities", [])})[:12],
    }


def extract_json(text: str) -> dict[str, Any]:
    text = clean(text)
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("No JSON object found")
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("DeepSeek output is not a JSON object")
    return obj


def deepseek_once(api_key: str, rows: list[dict[str, Any]], thinking: bool) -> dict[str, Any]:
    system = (
        "You are the news editor for an aircraft-leasing front-office dashboard. Use ONLY supplied metadata. "
        "Never invent facts. Remove clearly irrelevant items. Merge IDs only when they describe the same underlying event, "
        "not merely because they mention the same company. Every retained event must have exactly one allowed category. "
        "Return JSON only. Chinese fields are useful but must remain concise."
    )
    request_obj = {
        "allowed_categories": CATEGORIES,
        "output": {"stories": [{
            "title": "concise factual English headline",
            "summary_zh": "1 concise Chinese sentence; empty allowed if metadata is insufficient",
            "why_it_matters_zh": "1 concise Chinese sentence for aircraft lessors; empty allowed if uncertain",
            "category": "one exact allowed category",
            "importance_score": 70,
            "region": "short region/country label",
            "entities": ["named companies / aircraft / engines"],
            "event_ids": ["p001", "p002"]
        }]},
        "candidates": rows,
    }
    body: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(request_obj, ensure_ascii=False)}],
        "response_format": {"type": "json_object"},
        "max_tokens": 10000 if thinking else 7500,
    }
    if thinking:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = "high"
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    r = requests.post(CHAT_API, headers=headers, json=body, timeout=180)
    r.raise_for_status()
    msg = ((r.json().get("choices") or [{}])[0].get("message") or {})
    content = msg.get("content") or ""
    if not clean(content):
        raise ValueError("empty DeepSeek content")
    return extract_json(content)


def call_deepseek(api_key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    errors = []
    for attempt, thinking in enumerate((True, False, False), 1):
        try:
            return deepseek_once(api_key, rows, thinking=thinking)
        except Exception as exc:
            errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            if attempt < 3:
                time.sleep(2 * attempt)
    raise RuntimeError("; ".join(errors))


def story_id(raw_ids: list[str], title: str, published_at: str | None) -> str:
    base = "|".join(sorted(raw_ids)) if raw_ids else norm(title) + "|" + (published_at or "")[:10]
    return hashlib.sha1(base.encode()).hexdigest()[:18]


def fallback_from_cluster(cluster: dict[str, Any], now: datetime, mode: str) -> dict[str, Any]:
    rep = cluster["representative"]
    items = sorted(cluster["items"], key=lambda x: source_rank(x.get("source", "")), reverse=True)
    sources, seen = [], set()
    for item in items:
        src = source_obj(item)
        if src["url"] and src["url"] not in seen:
            seen.add(src["url"]); sources.append(src)
    text = " ".join(clean(x.get("title")) + " " + clean(x.get("summary")) for x in items)
    raw_ids = [x["_rid"] for x in items]
    entities = []
    for x in items:
        for e in x.get("_entities", []):
            if e.lower() not in {z.lower() for z in entities}:
                entities.append(e)
    published = max(x["_dt"] for x in items)
    return {
        "id": story_id(raw_ids, rep.get("title", ""), iso(published)),
        "title": clean(rep.get("title")), "summary_zh": "", "why_it_matters_zh": "",
        "category": fallback_category_text(text, clean(rep.get("source_channel"))),
        "importance_score": seed_importance(text, sources),
        "region": clean(rep.get("region_raw")) or "Global/Unspecified",
        "entities": entities[:16], "published_at": iso(published), "updated_at": iso(now),
        "primary_source": sources[0]["name"] if sources else clean(rep.get("source")),
        "primary_url": sources[0]["url"] if sources else clean(rep.get("url")),
        "source_count": len(sources), "sources": sources[:8], "raw_ids": raw_ids, "analysis_mode": mode,
    }


def ai_story(story: dict[str, Any], lookup: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    pids = []
    for pid in story.get("event_ids") or []:
        pid = str(pid)
        if pid in lookup and pid not in pids:
            pids.append(pid)
    if not pids:
        return None
    all_items = [x for pid in pids for x in lookup[pid]["items"]]
    all_items.sort(key=lambda x: source_rank(x.get("source", "")), reverse=True)
    sources, seen = [], set()
    for item in all_items:
        src = source_obj(item)
        if src["url"] and src["url"] not in seen:
            seen.add(src["url"]); sources.append(src)
    raw_ids = list(dict.fromkeys(x["_rid"] for x in all_items))
    entities = []
    for e in (story.get("entities") or []) + [e for x in all_items for e in x.get("_entities", [])]:
        e = clean(e)
        if e and e.lower() not in {z.lower() for z in entities}:
            entities.append(e)
    category = story.get("category") if story.get("category") in CATEGORIES else fallback_category_text(" ".join(x.get("title", "") for x in all_items))
    try:
        importance = max(0, min(100, int(story.get("importance_score", 60))))
    except Exception:
        importance = 60
    published = max(x["_dt"] for x in all_items)
    title = clean(story.get("title")) or clean(all_items[0].get("title"))
    return {
        "id": story_id(raw_ids, title, iso(published)), "title": title,
        "summary_zh": clean(story.get("summary_zh"))[:500], "why_it_matters_zh": clean(story.get("why_it_matters_zh"))[:400],
        "category": category, "importance_score": importance,
        "region": clean(story.get("region")) or clean(all_items[0].get("region_raw")) or "Global/Unspecified",
        "entities": entities[:16], "published_at": iso(published), "updated_at": iso(now),
        "primary_source": sources[0]["name"] if sources else clean(all_items[0].get("source")),
        "primary_url": sources[0]["url"] if sources else clean(all_items[0].get("url")),
        "source_count": len(sources), "sources": sources[:8], "raw_ids": raw_ids, "analysis_mode": "deepseek",
    }


def story_dt(story: dict[str, Any]) -> datetime | None:
    return parse_dt(story.get("published_at"))


def entity_keys(story: dict[str, Any]) -> set[str]:
    return {norm(x) for x in (story.get("entities") or []) if norm(x)}


def same_event(a: dict[str, Any], b: dict[str, Any]) -> bool:
    urls_a = {x.get("url") for x in a.get("sources") or [] if x.get("url")}
    urls_b = {x.get("url") for x in b.get("sources") or [] if x.get("url")}
    if urls_a & urls_b or set(a.get("raw_ids") or []) & set(b.get("raw_ids") or []):
        return True
    da, db = story_dt(a), story_dt(b)
    if da and db and abs((da - db).total_seconds()) > 72 * 3600:
        return False
    sim = similarity(a.get("title", ""), b.get("title", ""))
    overlap = entity_keys(a) & entity_keys(b)
    return sim >= 0.82 or (sim >= 0.64 and bool(overlap)) or (sim >= 0.56 and len(overlap) >= 2)


def merge_story(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    score_a = (1 if a.get("analysis_mode") == "deepseek" else 0, int(a.get("importance_score", 0)), len(a.get("sources") or []))
    score_b = (1 if b.get("analysis_mode") == "deepseek" else 0, int(b.get("importance_score", 0)), len(b.get("sources") or []))
    base, other = (dict(a), b) if score_a >= score_b else (dict(b), a)
    old_id = a.get("id") or b.get("id")
    sources, seen = [], set()
    for src in (base.get("sources") or []) + (other.get("sources") or []):
        url = src.get("url")
        if url and url not in seen:
            seen.add(url); sources.append(src)
    sources.sort(key=lambda x: source_rank(x.get("name", "")), reverse=True)
    base["sources"] = sources[:8]; base["source_count"] = len(base["sources"])
    if base["sources"]:
        base["primary_source"] = base["sources"][0].get("name"); base["primary_url"] = base["sources"][0].get("url")
    entities = []
    for e in (base.get("entities") or []) + (other.get("entities") or []):
        e = clean(e)
        if e and e.lower() not in {z.lower() for z in entities}:
            entities.append(e)
    base["entities"] = entities[:16]
    base["raw_ids"] = list(dict.fromkeys((base.get("raw_ids") or []) + (other.get("raw_ids") or [])))
    base["importance_score"] = max(int(a.get("importance_score", 0)), int(b.get("importance_score", 0)))
    dates = [d for d in (story_dt(a), story_dt(b)) if d]
    if dates: base["published_at"] = iso(max(dates))
    base["id"] = old_id or story_id(base["raw_ids"], base.get("title", ""), base.get("published_at"))
    return base


def consolidate(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(stories, key=lambda s: (story_dt(s) or datetime.min.replace(tzinfo=timezone.utc), int(s.get("importance_score", 0))), reverse=True)
    out: list[dict[str, Any]] = []
    for s in ordered:
        idx = next((i for i, existing in enumerate(out) if same_event(s, existing)), None)
        if idx is None: out.append(s)
        else: out[idx] = merge_story(out[idx], s)
    return out


def analyze_clusters(api_key: str, clusters: list[dict[str, Any]], now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    numbered = [(f"p{i:04d}", c) for i, c in enumerate(clusters, 1)]
    lookup = dict(numbered)
    stories: list[dict[str, Any]] = []; success_calls = 0; failed_batches = 0; errors: list[str] = []; returned: set[str] = set()
    for start in range(0, len(numbered), BATCH_SIZE):
        batch = numbered[start:start + BATCH_SIZE]
        try:
            data = call_deepseek(api_key, [cluster_payload(c, pid) for pid, c in batch]); success_calls += 1
            for row in data.get("stories") or []:
                record = ai_story(row, lookup, now)
                if record:
                    stories.append(record); returned.update(str(x) for x in (row.get("event_ids") or []) if str(x) in lookup)
        except Exception as exc:
            failed_batches += 1; errors.append(f"batch_{start // BATCH_SIZE + 1}: {type(exc).__name__}: {exc}")
            for pid, c in batch:
                stories.append(fallback_from_cluster(c, now, "fallback_after_ai_error")); returned.add(pid)
    for pid, c in numbered:
        if pid not in returned and any(x.get("source") in CORE_SOURCES for x in c["items"]):
            stories.append(fallback_from_cluster(c, now, "core_source_safety_net")); returned.add(pid)
    return consolidate(stories), {"deepseek_calls": success_calls, "deepseek_failed_batches": failed_batches, "deepseek_errors": errors, "clusters_submitted": len(clusters), "clusters_retained_or_accounted": len(returned)}


def fallback_clusters(clusters: list[dict[str, Any]], now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stories = []
    for c in clusters:
        text = " ".join(clean(x.get("title")) + " " + clean(x.get("summary")) for x in c["items"]).lower()
        if any(x.get("source") in CORE_SOURCES for x in c["items"]) or any(t in text for t in AVIATION_TERMS):
            stories.append(fallback_from_cluster(c, now, "fallback_no_deepseek_key"))
    return consolidate(stories), {"deepseek_calls": 0, "deepseek_failed_batches": 0, "deepseek_errors": ["DEEPSEEK_API_KEY missing; deterministic fallback used."], "clusters_submitted": len(clusters), "clusters_retained_or_accounted": len(stories)}


def make_payload(stories: list[dict[str, Any]], now: datetime, model: str, mode: str, stats: dict[str, Any]) -> dict[str, Any]:
    by_category = Counter(s.get("category", "Unknown") for s in stories)
    source_mentions = Counter(src.get("name", "Unknown") for s in stories for src in (s.get("sources") or []))
    return {"schema_version": 2, "generated_at": iso(now), "model": model, "analysis_mode": mode, "categories": CATEGORIES, "stats": {**stats, "stories": len(stories), "by_category": dict(sorted(by_category.items())), "source_mentions": dict(sorted(source_mentions.items()))}, "stories": stories}


def main() -> int:
    now = utcnow()
    if not RAW_PATH.exists(): raise SystemExit("data/news_raw.json missing")
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8")); raw_items = raw.get("items", []) if isinstance(raw, dict) else []
    recent_items = prepare_recent(raw_items, now)
    existing_archive = load_archive(); already = processed_raw_ids(existing_archive)
    new_items = [x for x in recent_items if x["_rid"] not in already]; new_clusters = precluster(new_items)

    if len(new_clusters) > MAX_NEW_AI_EVENTS:
        core_clusters = [c for c in new_clusters if any(x.get("source") in CORE_SOURCES for x in c["items"])]
        noncore = [c for c in new_clusters if c not in core_clusters]
        new_clusters = core_clusters + noncore[:max(0, MAX_NEW_AI_EVENTS - len(core_clusters))]

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if new_clusters:
        if api_key: new_stories, ai_stats, mode = *analyze_clusters(api_key, new_clusters, now), "deepseek"
        else: new_stories, ai_stats, mode = *fallback_clusters(new_clusters, now), "fallback_rules"
    else:
        new_stories = []; ai_stats = {"deepseek_calls": 0, "deepseek_failed_batches": 0, "deepseek_errors": [], "clusters_submitted": 0, "clusters_retained_or_accounted": 0}; mode = "incremental_no_new_items"

    archive_cutoff = now - timedelta(days=ARCHIVE_DAYS)
    archive_pool = [s for s in existing_archive + new_stories if story_dt(s) and story_dt(s) >= archive_cutoff and s.get("category") in CATEGORIES]
    archive_stories = consolidate(archive_pool); archive_stories.sort(key=lambda s: story_dt(s) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    current_cutoff = now - timedelta(days=CURRENT_DAYS)
    current_stories = [s for s in archive_stories if story_dt(s) and story_dt(s) >= current_cutoff]
    current_stories.sort(key=lambda s: (int(s.get("importance_score", 0)), story_dt(s) or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

    common = {"raw_pool_total": len(raw_items), "recent_raw_items": len(recent_items), "already_processed_raw_items": len([x for x in recent_items if x["_rid"] in already]), "new_raw_items": len(new_items), "new_preclusters": len(new_clusters), **ai_stats}
    OUT_PATH.write_text(json.dumps(make_payload(current_stories, now, MODEL if api_key else "rules-fallback", mode, common), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ARCHIVE_PATH.write_text(json.dumps(make_payload(archive_stories, now, MODEL if api_key else "rules-fallback", mode, {"archive_retention_days": ARCHIVE_DAYS, **common}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    status_level = "pass"
    if api_key and ai_stats["deepseek_calls"] == 0 and new_clusters: status_level = "warn"
    elif ai_stats["deepseek_failed_batches"] > ai_stats["deepseek_calls"] and new_clusters: status_level = "warn"
    status = {"schema_version": 2, "checked_at": iso(now), "status": status_level, "analysis_mode": mode, "deepseek_configured": bool(api_key), "categories": CATEGORIES, "raw_pool_total": len(raw_items), "recent_raw_items": len(recent_items), "new_raw_items": len(new_items), "new_preclusters": len(new_clusters), "current_stories": len(current_stories), "archive_stories": len(archive_stories), **ai_stats, "fallback_story_count": sum(1 for s in current_stories if s.get("analysis_mode") != "deepseek"), "deepseek_story_count": sum(1 for s in current_stories if s.get("analysis_mode") == "deepseek")}
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=2)); return 0


if __name__ == "__main__": raise SystemExit(main())
