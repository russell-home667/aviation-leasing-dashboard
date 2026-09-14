#!/usr/bin/env python3
"""Fetch metadata-only aviation-leasing news from approved public/RSS sources."""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "news_sources.json"
OUTPUT_PATH = ROOT / "data" / "news_raw.json"
HEALTH_PATH = ROOT / "data" / "news_source_health.json"
DATE_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\b", re.I)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = BeautifulSoup(html.unescape(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def truncate(value: str, limit: int = 600) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def normalize_title(value: str) -> str:
    value = strip_html(value).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()


def canonical_url(value: str) -> str:
    if not value:
        return ""
    try:
        p = urllib.parse.urlsplit(value)
        pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        pairs = [(k, v) for k, v in pairs if not k.lower().startswith("utm_") and k.lower() not in {"ref", "source", "campaign", "mc_cid", "mc_eid"}]
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, urllib.parse.urlencode(pairs, doseq=True), ""))
    except Exception:
        return value


def unwrap_bing_url(value: str) -> str:
    if not value:
        return value
    try:
        p = urllib.parse.urlsplit(value)
        if "bing.com" not in p.netloc.lower():
            return value
        qs = urllib.parse.parse_qs(p.query)
        for key in ("url", "u", "r"):
            for candidate in qs.get(key, []):
                candidate = urllib.parse.unquote(candidate)
                if candidate.startswith(("http://", "https://")):
                    return candidate
    except Exception:
        pass
    return value


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = strip_html(value)
    try:
        dt = parsedate_to_datetime(value)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except Exception:
        pass
    match = DATE_RE.search(value)
    if match:
        try:
            return datetime.strptime(f"{match.group(1)} {match.group(2)} {match.group(3)}", "%d %B %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def localname(tag: str) -> str:
    return tag.split("}")[-1].split(":")[-1].lower()


def node_text(node: ET.Element, names: tuple[str, ...]) -> str:
    wanted = set(names)
    for child in list(node):
        if localname(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def stable_id(item: dict[str, Any]) -> str:
    base = canonical_url(item.get("url", "")) or "|".join([normalize_title(item.get("title", "")), item.get("published_at") or "", item.get("source") or ""])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def make_item(*, title: str, url: str, source: str, source_channel: str, published_at: datetime | None = None, summary: str = "", author: str = "", categories: list[str] | None = None, region: str = "", discovered_via: str = "direct", discovery_query: str = "", rights_mode: str = "metadata_only") -> dict[str, Any] | None:
    title = strip_html(title)
    if len(title) < 5:
        return None
    url = canonical_url(unwrap_bing_url(url))
    item = {
        "title": title,
        "source": source,
        "source_channel": source_channel,
        "published_at": iso(published_at),
        "published_date": published_at.date().isoformat() if published_at else None,
        "url": url,
        "summary": truncate(strip_html(summary)),
        "author": strip_html(author),
        "categories_raw": sorted({strip_html(x) for x in (categories or []) if strip_html(x)}),
        "region_raw": strip_html(region),
        "discovered_via": discovered_via,
        "discovery_query": discovery_query,
        "rights_mode": rights_mode,
        "fetched_at": iso(utcnow()),
    }
    item["id"] = stable_id(item)
    return item


class Fetcher:
    def __init__(self, user_agent: str, timeout: int) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.8", "Cache-Control": "no-cache"})

    def get(self, url: str, accept: str = "*/*") -> tuple[requests.Response | None, str | None]:
        last_error = None
        for attempt in range(3):
            try:
                r = self.session.get(url, timeout=self.timeout, headers={"Accept": accept}, allow_redirects=True)
                if r.status_code >= 500 and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r, None
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        return None, last_error


def parse_rss(xml_text: str, *, source_name: str, source_channel: str, rights_mode: str, discovered_via: str = "direct", discovery_query: str = "") -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    output = []
    for node in [n for n in root.iter() if localname(n.tag) in {"item", "entry"}]:
        title = node_text(node, ("title",))
        link = node_text(node, ("link", "url"))
        if not link:
            for child in list(node):
                if localname(child.tag) == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        description = node_text(node, ("description", "summary", "content"))
        date_text = node_text(node, ("pubdate", "published", "updated", "date"))
        author = node_text(node, ("author", "creator"))
        categories = [(child.text or "").strip() for child in list(node) if localname(child.tag) == "category" and (child.text or "").strip()]
        publisher = ""
        for child in list(node):
            if localname(child.tag) == "source" and (child.text or "").strip():
                publisher = (child.text or "").strip()
                break
        actual_source, actual_channel, actual_discovered = source_name, source_channel, discovered_via
        if source_channel == "bing_news_rss":
            pub = strip_html(publisher)
            host = urllib.parse.urlsplit(unwrap_bing_url(link)).netloc.lower()
            if "reuters.com" in host or "reuters" in pub.lower():
                actual_source, actual_channel = "Reuters", "reuters_via_bing"
            elif pub:
                actual_source = pub
            actual_discovered = "Bing News"
        item = make_item(title=title, url=link, source=actual_source, source_channel=actual_channel, published_at=parse_date(date_text), summary=description, author=author, categories=categories, discovered_via=actual_discovered, discovery_query=discovery_query, rights_mode=rights_mode)
        if item:
            output.append(item)
    return output


def extract_container(anchor: Any) -> Any:
    current, best = anchor, anchor.parent
    for _ in range(7):
        current = getattr(current, "parent", None)
        if current is None:
            break
        text = current.get_text(" ", strip=True)
        if DATE_RE.search(text):
            best = current
            if len(text) < 1800:
                return current
    return best


def parse_aviation_news_online(html_text: str, base_url: str, source_channel: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    output, seen_urls = [], set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if "/article/" not in href:
            continue
        url = urllib.parse.urljoin(base_url, href)
        key = canonical_url(url)
        title = anchor.get_text(" ", strip=True)
        if key in seen_urls or len(title) < 8:
            continue
        seen_urls.add(key)
        container = extract_container(anchor)
        block_text = container.get_text(" ", strip=True) if container else ""
        published = parse_date(block_text)
        author = ""
        m = re.search(r"\bBy\s+([^|]{2,80}?)(?=\s+\d{1,2}(?:st|nd|rd|th)?\s+|$)", block_text)
        if m:
            author = m.group(1).strip()
        categories = []
        if container:
            for a in container.find_all("a", href=True):
                txt, ahref = a.get_text(" ", strip=True), a.get("href", "")
                if "/category/" in ahref and txt and txt.lower() not in {"home", "all news"}:
                    categories.append(txt)
        region_candidates = {"Americas", "Asia/Pacific", "Europe", "Middle East/Africa", "Oceania", "Africa", "Latin America", "Southeast Asia", "Middle East"}
        region = next((cat for cat in categories if cat in region_candidates), "")
        paragraphs = []
        if container:
            for p in container.find_all("p"):
                text = p.get_text(" ", strip=True)
                if len(text) >= 30 and title.lower() not in text.lower():
                    paragraphs.append(text)
        summary = max(paragraphs, key=len) if paragraphs else block_text.replace(title, " ")
        item = make_item(title=title, url=url, source="Aviation News Online", source_channel=source_channel, published_at=published, summary=summary, author=author, categories=categories, region=region, rights_mode="public_listing_metadata")
        if item:
            output.append(item)
    return output


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for item in items:
        url_key = canonical_url(item.get("url", ""))
        key = "url:" + url_key if url_key else "title:" + normalize_title(item.get("title", "")) + "|" + (item.get("published_date") or "")
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
            continue
        old_score = sum(bool(existing.get(k)) for k in ("published_at", "summary", "author", "region_raw")) + len(existing.get("categories_raw", []))
        new_score = sum(bool(item.get(k)) for k in ("published_at", "summary", "author", "region_raw")) + len(item.get("categories_raw", []))
        if new_score > old_score:
            by_key[key] = item
    return list(by_key.values())


def merge_existing(new_items: list[dict[str, Any]], retention_days: int, retired_source_channels: set[str] | None = None) -> list[dict[str, Any]]:
    retired_source_channels = retired_source_channels or set()
    existing = []
    if OUTPUT_PATH.exists():
        try:
            payload = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            existing = payload.get("items", []) if isinstance(payload, dict) else []
            existing = [item for item in existing if item.get("source_channel") not in retired_source_channels]
        except Exception:
            pass
    all_items = dedupe(existing + new_items)
    cutoff = (utcnow() - timedelta(days=retention_days)).date()
    retained = []
    for item in all_items:
        d = item.get("published_date")
        if not d:
            retained.append(item)
            continue
        try:
            if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff:
                retained.append(item)
        except ValueError:
            retained.append(item)
    return sorted(retained, key=lambda x: (x.get("published_at") or "", x.get("title") or ""), reverse=True)


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    fetcher = Fetcher(config.get("user_agent", "AviationLeasingDashboardNewsBot/1.0"), int(config.get("request_timeout_seconds", 20)))
    fetched_at = utcnow()
    collected, health = [], []

    for src in config.get("sources", []):
        started = time.time()
        response, error = fetcher.get(src["url"], "application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.5")
        records = []
        if response is not None:
            try:
                if src["kind"] == "rss":
                    records = parse_rss(response.text, source_name=src["name"], source_channel=src["id"], rights_mode=src.get("rights_mode", "metadata_only"))
                elif src["kind"] == "aviationnews_html":
                    records = parse_aviation_news_online(response.text, response.url, src["id"])
                else:
                    raise ValueError(f"Unsupported source kind: {src['kind']}")
            except Exception as exc:
                error = f"ParseError: {type(exc).__name__}: {exc}"
        collected.extend(records)
        health.append({"source_id": src["id"], "name": src["name"], "kind": src["kind"], "url": src["url"], "ok": response is not None and error is None and len(records) > 0, "http_status": response.status_code if response is not None else None, "records": len(records), "error": error, "elapsed_seconds": round(time.time() - started, 2), "checked_at": iso(utcnow())})

    bing = config.get("bing_news", {})
    for query in bing.get("queries", []):
        started = time.time()
        url = bing.get("base_url", "https://www.bing.com/news/search") + "?" + urllib.parse.urlencode({"q": query, "format": "rss", "mkt": "en-US"})
        response, error = fetcher.get(url, "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.5")
        records = []
        if response is not None:
            try:
                records = parse_rss(response.text, source_name="Bing News", source_channel="bing_news_rss", rights_mode="search_result_metadata", discovered_via="Bing News", discovery_query=query)
            except Exception as exc:
                error = f"ParseError: {type(exc).__name__}: {exc}"
        collected.extend(records)
        health.append({"source_id": "bing:" + query, "name": "Bing News", "kind": "rss_search", "url": url, "ok": response is not None and error is None and len(records) > 0, "http_status": response.status_code if response is not None else None, "records": len(records), "error": error, "elapsed_seconds": round(time.time() - started, 2), "checked_at": iso(utcnow())})

    current_batch = dedupe(collected)
    retired = set(config.get("retired_source_channels", []))
    merged = merge_existing(current_batch, int(config.get("retention_days", 45)), retired)
    by_source = Counter(item.get("source", "Unknown") for item in merged)
    by_channel = Counter(item.get("source_channel", "Unknown") for item in merged)
    reuters_count = sum(1 for item in merged if item.get("source") == "Reuters")
    OUTPUT_PATH.write_text(json.dumps({"schema_version": 1, "generated_at": iso(fetched_at), "retention_days": int(config.get("retention_days", 45)), "metadata_only": True, "stats": {"current_batch_unique": len(current_batch), "stored_total": len(merged), "reuters_discovered_via_bing": reuters_count, "by_source": dict(sorted(by_source.items())), "by_channel": dict(sorted(by_channel.items()))}, "items": merged}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok_count = sum(1 for h in health if h["ok"])
    HEALTH_PATH.write_text(json.dumps({"schema_version": 1, "checked_at": iso(fetched_at), "summary": {"checks_total": len(health), "checks_ok": ok_count, "checks_failed": len(health) - ok_count, "records_before_cross_source_dedupe": len(collected), "current_batch_unique": len(current_batch)}, "checks": health}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Fetched {len(collected)} records; {len(current_batch)} unique in this run; {len(merged)} stored.")
    print(f"Source checks: {ok_count}/{len(health)} passed. Reuters via Bing: {reuters_count} stored.")
    core = [h for h in health if h["source_id"].startswith(("ishka_", "aviationnews_", "flightglobal_"))]
    if core and not any(h["ok"] for h in core):
        print("ERROR: all direct core news sources failed.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
