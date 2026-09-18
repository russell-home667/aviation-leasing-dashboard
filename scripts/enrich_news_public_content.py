#!/usr/bin/env python3
"""Fetch public article text for high-value aviation-leasing news.

This step enriches data/news_raw.json between metadata discovery and clustering.
It only uses publicly accessible HTML and never attempts to bypass login/paywall
controls. The extracted text is capped before storage and labeled so downstream
AI can distinguish full text, excerpts, RSS summaries and headline-only input.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "news_raw.json"
CONFIG = ROOT / "data" / "news_priority_entities.json"
STATUS = ROOT / "data" / "news_content_enrichment_status.json"

MAX_FETCH = max(1, int(os.getenv("NEWS_PUBLIC_CONTENT_MAX_FETCH", "36")))
MAX_STORED_CHARS = max(1500, int(os.getenv("NEWS_PUBLIC_CONTENT_MAX_CHARS", "6000")))
TIMEOUT = max(5, int(os.getenv("NEWS_PUBLIC_CONTENT_TIMEOUT", "18")))

PAYWALL_DOMAINS = {
    "bloomberg.com", "wsj.com", "ft.com", "economist.com", "nytimes.com",
    "theinformation.com", "barrons.com", "fortune.com", "ishka.com",
    "ishkaairfinance.com",
}
CORE_SOURCES = {
    "Reuters", "FlightGlobal", "Flightglobal", "Aviation News Online",
    "Airbus", "Boeing", "Embraer", "COMAC",
}
ARTICLE_TYPES = {"article", "newsarticle", "reportagenewsarticle"}


def clean(value) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def host(url: str) -> str:
    try:
        value = urlsplit(url).netloc.lower().split(":", 1)[0]
    except Exception:
        return ""
    return value[4:] if value.startswith("www.") else value


def is_paywall_domain(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in PAYWALL_DOMAINS)


def existing_basis(item: dict) -> str:
    if clean(item.get("public_article_text")):
        return clean(item.get("content_basis")) or "article_excerpt"
    if len(clean(item.get("summary"))) >= 80:
        return "rss_summary"
    return "headline_only"


def important_score(item: dict, config: dict) -> int:
    text = (clean(item.get("title")) + " " + clean(item.get("summary"))).lower()
    score = 0
    for entity in config.get("entities") or []:
        aliases = entity.get("aliases") or [entity.get("canonical")]
        if any(clean(alias).lower() in text for alias in aliases if clean(alias)):
            score += 120
            break
    terms = [clean(x).lower() for x in (config.get("material_event_terms") or []) if clean(x)]
    if any(term in text for term in terms):
        score += 90
    if clean(item.get("source")) in CORE_SOURCES:
        score += 45
    if re.search(r"\b(?:lease|leasing|lessor|aircraft order|delivery|engine|mro|bankruptcy|default|financing|repossession|grounding|sanction)\b", text):
        score += 35
    return score


def jsonld_article_text(soup: BeautifulSoup) -> str:
    candidates = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = node.string or node.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        stack = obj if isinstance(obj, list) else [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                stack.extend(cur)
            elif isinstance(cur, dict):
                if "@graph" in cur:
                    stack.append(cur["@graph"])
                typ = cur.get("@type")
                types = {str(x).lower() for x in typ} if isinstance(typ, list) else {str(typ).lower()}
                body = clean(cur.get("articleBody"))
                if body and (types & ARTICLE_TYPES or len(body) >= 800):
                    candidates.append(body)
    return max(candidates, key=len) if candidates else ""


def paragraph_text(soup: BeautifulSoup) -> str:
    containers = soup.find_all("article") or soup.find_all("main") or [soup]
    paragraphs = []
    seen = set()
    for container in containers[:3]:
        for p in container.find_all("p"):
            text = clean(p.get_text(" ", strip=True))
            if len(text) < 45:
                continue
            low = text.lower()
            if any(x in low for x in ("cookie policy", "privacy policy", "sign up for", "subscribe to", "all rights reserved")):
                continue
            if low in seen:
                continue
            seen.add(low)
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    text = jsonld_article_text(soup)
    if len(text) < 800:
        text = paragraph_text(soup)
    return text.strip()


def fetch_public(session: requests.Session, item: dict) -> tuple[str, str, str]:
    url = clean(item.get("url"))
    domain = host(url)
    if not url.startswith(("http://", "https://")):
        return "", "invalid_url", url
    if is_paywall_domain(domain):
        return "", "paywall_skipped", url

    response = session.get(
        url,
        timeout=TIMEOUT,
        allow_redirects=True,
        headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.2"},
    )
    response.raise_for_status()
    ctype = (response.headers.get("content-type") or "").lower()
    if "html" not in ctype:
        return "", "non_html", response.url
    if len(response.content) > 3_500_000:
        return "", "document_too_large", response.url

    text = extract_text(response.text)
    low = text.lower()
    if len(text) < 500:
        return "", "too_short", response.url
    if any(marker in low[:1500] for marker in ("subscribe to continue", "sign in to continue", "already a subscriber")):
        return "", "paywall_detected", response.url
    return text, "ok", response.url


def main() -> int:
    raw = load(RAW, {})
    config = load(CONFIG, {})
    items = raw.get("items") or []
    if not isinstance(items, list):
        raise SystemExit("data/news_raw.json items missing")

    for item in items:
        if isinstance(item, dict):
            item.setdefault("content_basis", existing_basis(item))

    candidates = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if clean(item.get("public_article_text")):
            continue
        score = important_score(item, config)
        if score <= 0:
            continue
        candidates.append((score, clean(item.get("published_at")), item))
    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    selected = candidates[:MAX_FETCH]

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; AviationLeasingDashboardNewsBot/2.0; public-content-enrichment)",
        "Accept-Language": "en-US,en;q=0.9",
    })

    stats = {
        "selected": len(selected), "full_text": 0, "article_excerpt": 0,
        "rss_summary": 0, "headline_only": 0, "fetch_failures": 0,
        "paywall_skipped": 0,
    }
    errors = []
    fetched_at = now_iso()

    for _score, _published, item in selected:
        try:
            text, status, final_url = fetch_public(session, item)
        except Exception as exc:
            text, status, final_url = "", f"{type(exc).__name__}", clean(item.get("url"))
            errors.append(f"{clean(item.get('source'))}: {status}: {clean(item.get('title'))[:100]}")

        if text:
            truncated = len(text) > MAX_STORED_CHARS
            stored = text[:MAX_STORED_CHARS].rstrip()
            basis = "article_excerpt" if truncated else "full_text"
            item["public_article_text"] = stored
            item["content_basis"] = basis
            item["content_characters"] = len(stored)
            item["content_source_url"] = final_url
            item["content_fetched_at"] = fetched_at
            item["content_fetch_status"] = "ok"
            stats[basis] += 1
        else:
            item["content_basis"] = existing_basis(item)
            item["content_fetch_status"] = status
            item["content_fetched_at"] = fetched_at
            if status.startswith("paywall"):
                stats["paywall_skipped"] += 1
            else:
                stats["fetch_failures"] += 1

    for item in items:
        if not isinstance(item, dict):
            continue
        basis = clean(item.get("content_basis")) or existing_basis(item)
        item["content_basis"] = basis
        if basis in {"rss_summary", "headline_only"}:
            stats[basis] += 1

    raw["content_enrichment"] = {
        "generated_at": fetched_at,
        "selected_high_value_stories": len(selected),
        "max_fetch": MAX_FETCH,
        "max_stored_chars": MAX_STORED_CHARS,
        "policy": "public HTML only; no login/paywall bypass; high-value aviation-leasing stories prioritized",
    }
    RAW.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = {
        "schema_version": 1,
        "checked_at": fetched_at,
        "status": "pass",
        "stats": stats,
        "errors": errors[:30],
    }
    STATUS.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
