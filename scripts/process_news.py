#!/usr/bin/env python3
"""Step 2: clean, de-duplicate, cluster and classify aviation-leasing news.

Input: Step-1 metadata only (`data/news_raw.json`). No paid article body is fetched.
Outputs:
- data/news.json: processed rolling 7-day feed
- data/news_archive.json: clustered archive retained up to 120 days
- data/news_processing_status.json: processing and AI health

Step 2 deliberately keeps AI output small: semantic event grouping + one of eight
fixed categories. Chinese summaries / final importance logic belong to Step 3.
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
RAW_PATH = ROOT / "data/news_raw.json"
NEWS_PATH = ROOT / "data/news.json"
ARCHIVE_PATH = ROOT / "data/news_archive.json"
STATUS_PATH = ROOT / "data/news_processing_status.json"

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
CHAT_API = "https://api.deepseek.com/chat/completions"
CURRENT_DAYS = int(os.getenv("NEWS_CURRENT_DAYS", "7"))
ARCHIVE_DAYS = int(os.getenv("NEWS_ARCHIVE_DAYS", "120"))
BATCH_SIZE = int(os.getenv("NEWS_AI_BATCH_SIZE", "30"))
MAX_NEW_CLUSTERS = int(os.getenv("NEWS_MAX_AI_CANDIDATES", "360"))

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
    "a","an","the","and","or","of","to","for","in","on","at","by","from","with","as","is","are",
    "was","were","be","been","being","its","it","this","that","these","those","after","before","will",
    "would","could","may","might","into","over","under","new","more","says","said",
}
AVIATION_TERMS = (
    "aircraft","airline","aviation","lessor","lease","leasing","airbus","boeing","embraer","comac","engine",
    "mro","fleet","aercap","avolon","boc aviation","smbc","air lease","a320","a321","a350","737","777",
    "787","c919","e190","e195","atr",
)
ENTITY_PATTERNS = [
    re.compile(r"\b(?:Airbus|Boeing|Embraer|COMAC|ATR)\b", re.I),
    re.compile(r"\b(?:AerCap|Avolon|BOC Aviation|SMBC Aviation Capital|Dubai Aerospace Enterprise|DAE|CDB Aviation|CALC|Air Lease|Aviation Capital Group|ACG|Aircastle|Castlelake|Azorra|TrueNoord|Nordic Aviation Capital|NAC|Macquarie AirFinance|ORIX Aviation|Jackson Square Aviation|JSA|Griffin Global Asset Management|SKY Leasing)\b", re.I),
    re.compile(r"\b(?:Pratt\s*&\s*Whitney|Pratt Whitney|GE Aerospace|CFM International|Rolls-Royce|Safran|MTU)\b", re.I),
    re.compile(r"\b(?:A220(?:-\d{3})?|A3(?:19|20|21|30|40|50)(?:neo)?(?:-\d{3,4})?|A380(?:-\d{3})?|7(?:37|47|67|77|87)(?:-\d{3,4}| MAX ?\d+)?|E(?:170|175|190|195)(?:-E2)?|CRJ(?:200|700|900|1000)|C909|C919|C929|ARJ21|ATR ?(?:42|72)(?:-\d{3})?)\b", re.I),
    re.compile(r"\b(?:PW1100G|PW1500G|LEAP-1[ABC]|CFM56|GEnx(?:-\dB)?|GE9X|Trent ?(?:7000|1000|XWB|700|800|900))\b", re.I),
]
KEYWORDS = {
    "Leasing & Trading": ["lease","leasing","lessor","sale and leaseback","sale-leaseback","slb","remarketing","trading","portfolio"],
    "Aircraft & OEM": ["airbus","boeing","embraer","comac","aircraft order","orders","delivery","deliveries","production","backlog","certification","a320","a321","a350","737","777","787","c919"],
    "Airlines & Credit": ["airline","carrier","bankruptcy","insolvency","restructuring","default","liquidity","credit","profit","loss","traffic","capacity","fleet","grounding"],
    "Financing & Capital Markets": ["financing","finance","bond","abs","securit","loan","debt","jolco","pdp","capital markets","refinanc","warehouse","credit facility","funding"],
    "Engines & MRO": ["engine","mro","maintenance","overhaul","shop visit","pratt","pw1100","gtf","cfm","leap","rolls-royce","trent","spare engine","repair"],
    "Values & Lease Rates": ["lease rate","aircraft value","valuation","market value","base value","residual value","apprais","half-life","full-life"],
    "Legal & Regulatory": ["regulat","legal","court","lawsuit","litigation","sanction","cape town convention","repossession","repossess","compliance","antitrust","faa","easa"],
    "Macro & Geopolitics": ["war","conflict","geopolit","tariff","oil price","jet fuel","interest rate","inflation","gdp","trade war","airspace","russia","ukraine","iran"],
}


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


def clean(v: Any) -> str:
    return " ".join(str(v or "").split())


def norm(v: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", clean(v).lower()).split())


def words(v: str) -> set[str]:
    return {x for x in norm(v).split() if len(x) > 1 and x not in STOP}


def sim(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    x, y = words(a), words(b)
    jac = len(x & y) / len(x | y) if x and y else 0.0
    return max(seq, jac, (seq + jac) / 2)


def source_rank(name: str) -> int:
    return SOURCE_RANK.get(clean(name), 60)


def raw_dt(item: dict[str, Any]) -> datetime | None:
    return dtparse(item.get("published_at")) or dtparse(item.get("fetched_at"))


def entities(*texts: str) -> list[str]:
    text = " | ".join(clean(x) for x in texts if x)
    out: list[str] = []
    for pat in ENTITY_PATTERNS:
        for m in pat.finditer(text):
            v = clean(m.group(0))
            if v and v.lower() not in {x.lower() for x in out}:
                out.append(v)
    return out[:16]


def fallback_category(text: str, channel: str = "") -> str:
    low = clean(text).lower()
    scores = {cat: sum(1 for kw in kws if kw in low) for cat, kws in KEYWORDS.items()}
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        if channel in {"aviationnews_engine", "aviationnews_maintenance"}: return "Engines & MRO"
        if channel in {"aviationnews_legal", "aviationnews_regulatory"}: return "Legal & Regulatory"
        if channel.startswith("ishka_"): return "Leasing & Trading"
        return "Airlines & Credit"
    return best


def provisional_importance(text: str, source_names: list[str]) -> int:
    score = 45 + max(0, max([source_rank(x) for x in source_names] or [60]) - 60) // 4
    strong = ("bankruptcy","insolvency","default","repossession","sale and leaseback","aircraft order","financing","abs","jolco","lease rate","aircraft value","gtf","grounding","sanction","merger","acquisition")
    score += min(24, sum(4 for kw in strong if kw in clean(text).lower()))
    return max(35, min(88, score))


def prepare(raw_items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=CURRENT_DAYS, hours=4)
    out, seen = [], set()
    for item in raw_items:
        d = raw_dt(item)
        if not d or d < cutoff: continue
        title, url = clean(item.get("title")), clean(item.get("url"))
        if not title or not url: continue
        rid = clean(item.get("id")) or hashlib.sha1((norm(title) + "|" + url).encode()).hexdigest()[:24]
        if rid in seen: continue
        seen.add(rid)
        x = dict(item); x["_rid"] = rid; x["_dt"] = d; x["_entities"] = entities(title, item.get("summary", "")); out.append(x)
    out.sort(key=lambda x: x["_dt"], reverse=True)
    return out


def precluster(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for item in items:
        hit = None
        for c in clusters:
            if abs((item["_dt"] - c["latest_dt"]).total_seconds()) > 72 * 3600: continue
            if clean(item.get("url")) in c["urls"] or sim(item.get("title", ""), c["rep"].get("title", "")) >= 0.88:
                hit = c; break
        if hit is None:
            clusters.append({"items": [item], "latest_dt": item["_dt"], "rep": item, "urls": {clean(item.get("url"))}})
        else:
            hit["items"].append(item); hit["urls"].add(clean(item.get("url"))); hit["latest_dt"] = max(hit["latest_dt"], item["_dt"])
            if source_rank(item.get("source", "")) > source_rank(hit["rep"].get("source", "")): hit["rep"] = item
    return clusters


def load_archive() -> list[dict[str, Any]]:
    if not ARCHIVE_PATH.exists(): return []
    try:
        d = json.loads(ARCHIVE_PATH.read_text(encoding="utf-8")); return d.get("stories", []) if isinstance(d, dict) else []
    except Exception: return []


def processed_ids(stories: list[dict[str, Any]]) -> set[str]:
    return {str(r) for s in stories for r in (s.get("raw_ids") or []) if r}


def cluster_for_ai(c: dict[str, Any], pid: str) -> dict[str, Any]:
    rep = c["rep"]
    return {
        "id": pid,
        "title": clean(rep.get("title"))[:280],
        "summary": clean(rep.get("summary"))[:300],
        "published_at": iso(c["latest_dt"]),
        "sources": sorted({clean(x.get("source")) for x in c["items"] if clean(x.get("source"))}),
        "raw_categories": sorted({z for x in c["items"] for z in (x.get("categories_raw") or [])})[:8],
        "region": clean(rep.get("region_raw"))[:70],
        "entities": sorted({z for x in c["items"] for z in x.get("_entities", [])})[:10],
    }


def parse_json(text: str) -> dict[str, Any]:
    t = str(text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t); t = re.sub(r"\s*```$", "", t)
    try: obj = json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if not m: raise ValueError("no JSON object in DeepSeek response")
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict): raise ValueError("DeepSeek response is not object")
    return obj


def call_ai(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    system = """You edit an aircraft-leasing news feed. Use ONLY the supplied metadata. Do not invent facts. Remove clearly irrelevant candidates. Merge IDs only if they are the SAME underlying event, not merely the same company. Assign exactly one allowed category to every retained event. Return compact JSON only."""
    prompt = {
        "allowed_categories": CATEGORIES,
        "return": {"stories": [{"title": "concise factual English headline", "category": "exact allowed category", "event_ids": ["p0001", "p0002"], "region": "short region/country", "entities": ["named entity"]}]},
        "candidates": rows,
    }
    body = {"model": MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}], "response_format": {"type": "json_object"}, "max_tokens": 5000}
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    errors = []
    for attempt in range(2):
        try:
            r = requests.post(CHAT_API, headers=headers, json=body, timeout=90); r.raise_for_status()
            content = (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
            if not content: raise ValueError("empty DeepSeek content")
            return parse_json(content)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt == 0: time.sleep(2)
    raise RuntimeError("; ".join(errors))


def src_obj(item: dict[str, Any]) -> dict[str, Any]:
    return {"name": clean(item.get("source")) or "Unknown", "url": clean(item.get("url")), "published_at": iso(raw_dt(item)), "source_channel": clean(item.get("source_channel")), "discovered_via": clean(item.get("discovered_via"))}


def make_id(raw_ids: list[str], title: str, published: str | None) -> str:
    base = "|".join(sorted(raw_ids)) if raw_ids else norm(title) + "|" + (published or "")[:10]
    return hashlib.sha1(base.encode()).hexdigest()[:18]


def from_cluster(c: dict[str, Any], now: datetime, mode: str) -> dict[str, Any]:
    items = sorted(c["items"], key=lambda x: source_rank(x.get("source", "")), reverse=True); rep = items[0]
    sources, seen = [], set()
    for x in items:
        s = src_obj(x)
        if s["url"] and s["url"] not in seen: seen.add(s["url"]); sources.append(s)
    raw_ids = [x["_rid"] for x in items]; text = " ".join(clean(x.get("title")) + " " + clean(x.get("summary")) for x in items)
    ents = []
    for x in items:
        for e in x.get("_entities", []):
            if e.lower() not in {q.lower() for q in ents}: ents.append(e)
    pub = max(x["_dt"] for x in items); names = [s["name"] for s in sources]
    return {"id": make_id(raw_ids, rep.get("title", ""), iso(pub)), "title": clean(rep.get("title")), "summary_zh": "", "why_it_matters_zh": "", "category": fallback_category(text, clean(rep.get("source_channel"))), "importance_score": provisional_importance(text, names), "region": clean(rep.get("region_raw")) or "Global/Unspecified", "entities": ents[:16], "published_at": iso(pub), "updated_at": iso(now), "primary_source": sources[0]["name"] if sources else clean(rep.get("source")), "primary_url": sources[0]["url"] if sources else clean(rep.get("url")), "source_count": len(sources), "sources": sources[:8], "raw_ids": raw_ids, "analysis_mode": mode}


def from_ai(row: dict[str, Any], lookup: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    pids = []
    for pid in row.get("event_ids") or []:
        p = str(pid)
        if p in lookup and p not in pids: pids.append(p)
    if not pids: return None
    items = [x for p in pids for x in lookup[p]["items"]]; items.sort(key=lambda x: source_rank(x.get("source", "")), reverse=True)
    sources, seen = [], set()
    for x in items:
        s = src_obj(x)
        if s["url"] and s["url"] not in seen: seen.add(s["url"]); sources.append(s)
    raw_ids = list(dict.fromkeys(x["_rid"] for x in items)); pub = max(x["_dt"] for x in items)
    title = clean(row.get("title")) or clean(items[0].get("title")); text = " ".join(clean(x.get("title")) + " " + clean(x.get("summary")) for x in items)
    cat = row.get("category") if row.get("category") in CATEGORIES else fallback_category(text, clean(items[0].get("source_channel")))
    ents = []
    for e in (row.get("entities") or []) + [e for x in items for e in x.get("_entities", [])]:
        e = clean(e)
        if e and e.lower() not in {q.lower() for q in ents}: ents.append(e)
    return {"id": make_id(raw_ids, title, iso(pub)), "title": title, "summary_zh": "", "why_it_matters_zh": "", "category": cat, "importance_score": provisional_importance(text, [s["name"] for s in sources]), "region": clean(row.get("region")) or clean(items[0].get("region_raw")) or "Global/Unspecified", "entities": ents[:16], "published_at": iso(pub), "updated_at": iso(now), "primary_source": sources[0]["name"] if sources else clean(items[0].get("source")), "primary_url": sources[0]["url"] if sources else clean(items[0].get("url")), "source_count": len(sources), "sources": sources[:8], "raw_ids": raw_ids, "analysis_mode": "deepseek"}


def story_dt(s: dict[str, Any]) -> datetime | None: return dtparse(s.get("published_at"))
def entset(s: dict[str, Any]) -> set[str]: return {norm(x) for x in (s.get("entities") or []) if norm(x)}


def same_event(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if {x.get("url") for x in a.get("sources", []) if x.get("url")} & {x.get("url") for x in b.get("sources", []) if x.get("url")}: return True
    if set(a.get("raw_ids") or []) & set(b.get("raw_ids") or []): return True
    da, db = story_dt(a), story_dt(b)
    if da and db and abs((da-db).total_seconds()) > 72*3600: return False
    score = sim(a.get("title", ""), b.get("title", "")); common = entset(a) & entset(b)
    return score >= .82 or (score >= .64 and bool(common)) or (score >= .56 and len(common) >= 2)


def merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    ranka = (1 if a.get("analysis_mode") == "deepseek" else 0, int(a.get("importance_score",0)), len(a.get("sources") or [])); rankb = (1 if b.get("analysis_mode") == "deepseek" else 0, int(b.get("importance_score",0)), len(b.get("sources") or []))
    base, other = (dict(a), b) if ranka >= rankb else (dict(b), a); stable = a.get("id") or b.get("id")
    sources, seen = [], set()
    for s in (base.get("sources") or []) + (other.get("sources") or []):
        if s.get("url") and s["url"] not in seen: seen.add(s["url"]); sources.append(s)
    sources.sort(key=lambda s: source_rank(s.get("name","")), reverse=True); base["sources"] = sources[:8]; base["source_count"] = len(base["sources"])
    if base["sources"]: base["primary_source"] = base["sources"][0].get("name"); base["primary_url"] = base["sources"][0].get("url")
    base["raw_ids"] = list(dict.fromkeys((base.get("raw_ids") or []) + (other.get("raw_ids") or [])))
    es = []
    for e in (base.get("entities") or []) + (other.get("entities") or []):
        e = clean(e)
        if e and e.lower() not in {q.lower() for q in es}: es.append(e)
    base["entities"] = es[:16]; base["importance_score"] = max(int(a.get("importance_score",0)), int(b.get("importance_score",0)))
    dates = [d for d in (story_dt(a), story_dt(b)) if d]
    if dates: base["published_at"] = iso(max(dates))
    base["id"] = stable or make_id(base["raw_ids"], base.get("title",""), base.get("published_at")); return base


def consolidate(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in sorted(stories, key=lambda z: (story_dt(z) or datetime.min.replace(tzinfo=timezone.utc), int(z.get("importance_score",0))), reverse=True):
        idx = next((i for i,x in enumerate(out) if same_event(s,x)), None)
        if idx is None: out.append(s)
        else: out[idx] = merge(out[idx], s)
    return out


def process_new(key: str, clusters: list[dict[str, Any]], now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    numbered = [(f"p{i:04d}", c) for i,c in enumerate(clusters,1)]; lookup = dict(numbered); stories = []; accounted: set[str] = set(); calls=0; fails=0; errors=[]
    for start in range(0, len(numbered), BATCH_SIZE):
        batch = numbered[start:start+BATCH_SIZE]
        try:
            data = call_ai(key, [cluster_for_ai(c,p) for p,c in batch]); calls += 1
            for row in data.get("stories") or []:
                s = from_ai(row, lookup, now)
                if s:
                    stories.append(s); accounted.update(str(x) for x in (row.get("event_ids") or []) if str(x) in lookup)
        except Exception as exc:
            fails += 1; errors.append(f"batch_{start//BATCH_SIZE+1}: {type(exc).__name__}: {exc}")
            for p,c in batch: stories.append(from_cluster(c,now,"fallback_after_ai_error")); accounted.add(p)
    for p,c in numbered:
        if p not in accounted and any(x.get("source") in CORE_SOURCES for x in c["items"]): stories.append(from_cluster(c,now,"core_source_safety_net")); accounted.add(p)
    return consolidate(stories), {"deepseek_calls": calls, "deepseek_failed_batches": fails, "deepseek_errors": errors, "clusters_submitted": len(clusters), "clusters_retained_or_accounted": len(accounted)}


def fallback_process(clusters: list[dict[str, Any]], now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stories=[]
    for c in clusters:
        text=" ".join(clean(x.get("title"))+" "+clean(x.get("summary")) for x in c["items"]).lower()
        if any(x.get("source") in CORE_SOURCES for x in c["items"]) or any(t in text for t in AVIATION_TERMS): stories.append(from_cluster(c,now,"fallback_no_deepseek_key"))
    return consolidate(stories), {"deepseek_calls":0,"deepseek_failed_batches":0,"deepseek_errors":["DEEPSEEK_API_KEY missing; deterministic fallback used."],"clusters_submitted":len(clusters),"clusters_retained_or_accounted":len(stories)}


def payload(stories: list[dict[str, Any]], now: datetime, model: str, mode: str, stats: dict[str, Any]) -> dict[str, Any]:
    cats=Counter(s.get("category","Unknown") for s in stories); sources=Counter(x.get("name","Unknown") for s in stories for x in (s.get("sources") or []))
    return {"schema_version":2,"generated_at":iso(now),"model":model,"analysis_mode":mode,"categories":CATEGORIES,"stats":{**stats,"stories":len(stories),"by_category":dict(sorted(cats.items())),"source_mentions":dict(sorted(sources.items()))},"stories":stories}


def main() -> int:
    now=now_utc()
    if not RAW_PATH.exists(): raise SystemExit("data/news_raw.json missing")
    raw=json.loads(RAW_PATH.read_text(encoding="utf-8")); raw_items=raw.get("items",[]) if isinstance(raw,dict) else []
    recent=prepare(raw_items,now); existing=load_archive(); done=processed_ids(existing); new_items=[x for x in recent if x["_rid"] not in done]; clusters=precluster(new_items)
    if len(clusters)>MAX_NEW_CLUSTERS:
        core=[c for c in clusters if any(x.get("source") in CORE_SOURCES for x in c["items"])]; non=[c for c in clusters if c not in core]; clusters=core+non[:max(0,MAX_NEW_CLUSTERS-len(core))]
    key=os.getenv("DEEPSEEK_API_KEY","").strip()
    if clusters:
        if key: new_stories,ai=process_new(key,clusters,now); mode="deepseek"
        else: new_stories,ai=fallback_process(clusters,now); mode="fallback_rules"
    else:
        new_stories=[]; ai={"deepseek_calls":0,"deepseek_failed_batches":0,"deepseek_errors":[],"clusters_submitted":0,"clusters_retained_or_accounted":0}; mode="incremental_no_new_items"
    acut=now-timedelta(days=ARCHIVE_DAYS); archive=consolidate([s for s in existing+new_stories if story_dt(s) and story_dt(s)>=acut and s.get("category") in CATEGORIES]); archive.sort(key=lambda s:story_dt(s) or datetime.min.replace(tzinfo=timezone.utc),reverse=True)
    ccut=now-timedelta(days=CURRENT_DAYS); current=[s for s in archive if story_dt(s) and story_dt(s)>=ccut]; current.sort(key=lambda s:(int(s.get("importance_score",0)),story_dt(s) or datetime.min.replace(tzinfo=timezone.utc)),reverse=True)
    stats={"raw_pool_total":len(raw_items),"recent_raw_items":len(recent),"already_processed_raw_items":sum(1 for x in recent if x["_rid"] in done),"new_raw_items":len(new_items),"new_preclusters":len(clusters),**ai}
    NEWS_PATH.write_text(json.dumps(payload(current,now,MODEL if key else "rules-fallback",mode,stats),ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    ARCHIVE_PATH.write_text(json.dumps(payload(archive,now,MODEL if key else "rules-fallback",mode,{"archive_retention_days":ARCHIVE_DAYS,**stats}),ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    status={"schema_version":2,"checked_at":iso(now),"status":"pass" if current else "warn","analysis_mode":mode,"deepseek_configured":bool(key),"categories":CATEGORIES,"raw_pool_total":len(raw_items),"recent_raw_items":len(recent),"new_raw_items":len(new_items),"new_preclusters":len(clusters),"current_stories":len(current),"archive_stories":len(archive),**ai,"fallback_story_count":sum(1 for s in current if s.get("analysis_mode")!="deepseek"),"deepseek_story_count":sum(1 for s in current if s.get("analysis_mode")=="deepseek")}
    STATUS_PATH.write_text(json.dumps(status,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); print(json.dumps(status,ensure_ascii=False,indent=2)); return 0

if __name__ == "__main__": raise SystemExit(main())
