import calendar
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "market_data.json"
ARCHIVE_URL = "https://www.anacargo.jp/mt/en/news/int/fuel/"
CANONICAL_PREFIX = "https://www.anacargo.jp/en/news/int/fuel/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; aviation-leasing-dashboard/1.0; personal research)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

MONTH_MAP = {
    **{name.lower(): i for i, name in enumerate(calendar.month_name) if name},
    **{name.lower(): i for i, name in enumerate(calendar.month_abbr) if name},
}
MONTH_RE = "(?:" + "|".join(sorted((re.escape(x) for x in MONTH_MAP), key=len, reverse=True)) + ")"
GAP_START = date(2010, 7, 28)
GAP_END = date(2026, 1, 25)


def month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def clean_text(html):
    return " ".join(
        BeautifulSoup(html, "html.parser").get_text(" ", strip=True).replace("\xa0", " ").split()
    )


def page_date(text):
    for y, m, d in re.findall(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b", text):
        try:
            x = date(int(y), int(m), int(d))
            if 2010 <= x.year <= 2020:
                return x
        except ValueError:
            pass
    return None


def month_number(name):
    return MONTH_MAP.get(name.lower().rstrip("."))


def infer_year(month, published):
    if published is None:
        return None
    return published.year - 1 if month > published.month else published.year


def canonicalize(href):
    full = urljoin(ARCHIVE_URL, href)
    full = full.replace(
        "https://www.anacargo.jp/mt/en/news/int/fuel/",
        CANONICAL_PREFIX,
    )
    # Strip fragments/query strings so duplicate old links collapse cleanly.
    parts = urlsplit(full)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def collect_detail_links(session):
    r = session.get(ARCHIVE_URL, timeout=30)
    print(f"ANA legacy index: HTTP {r.status_code}, {len(r.content)} bytes")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    links = {}
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        full = canonicalize(href)
        path = urlsplit(full).path
        if not path.startswith("/en/news/int/fuel/"):
            continue
        if not path.lower().endswith(".html"):
            continue
        if full == CANONICAL_PREFIX.rstrip("/") + ".html":
            continue
        # Skip index-like filenames if any.
        name = path.rsplit("/", 1)[-1].lower()
        if name in {"index.html", "index_1.html"}:
            continue
        text = " ".join(a.get_text(" ", strip=True).split())
        links[full] = text
    print(f"ANA legacy canonical article links: {len(links)}")
    return links


PRICE_PATTERNS = [
    re.compile(
        rf"average price of (?:jet fuel|Singapore kerosene)(?:[^.]){{0,140}}?"
        rf"(?:for )?(?:the )?month of\s+({MONTH_RE})(?:\s+(20\d{{2}}))?"
        rf"(?:[^.]){{0,100}}?(?:USD|US\$|USD\$)\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
        re.I,
    ),
    re.compile(
        rf"average (?:fuel )?price of Singapore kerosene(?:[^.]){{0,140}}?"
        rf"\b(?:in|of|for)\s+({MONTH_RE})(?:\s+(20\d{{2}}))?"
        rf"(?:[^.]){{0,100}}?(?:USD|US\$|USD\$)\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
        re.I,
    ),
    re.compile(
        rf"according to the average price of jet fuel for the month of\s+({MONTH_RE})"
        rf"(?:\s+(20\d{{2}}))?\s*,?\s*(?:USD|US\$|USD\$)?\s*\$?\s*"
        rf"([0-9]+(?:\.[0-9]+)?)",
        re.I,
    ),
]


def parse_detail(html, url):
    text = clean_text(html)
    if "fuel surcharge" not in text.lower():
        return []
    published = page_date(text)
    found = []
    for pattern in PRICE_PATTERNS:
        for m in pattern.finditer(text):
            month = month_number(m.group(1))
            explicit_year = int(m.group(2)) if m.group(2) else None
            year = explicit_year or infer_year(month, published)
            value = float(m.group(3))
            if not year or not month or not (15 <= value <= 350):
                continue
            end = date.fromisoformat(month_end(year, month))
            if not (GAP_START <= end <= GAP_END):
                continue
            found.append({
                "date": end.isoformat(),
                "value": round(value, 2),
                "observation_type": "monthly_average",
                "period_start": date(year, month, 1).isoformat(),
                "period_end": end.isoformat(),
                "source": "ANA Cargo legacy fuel-surcharge archive",
                "source_url": url,
            })
    by_date = {}
    for row in found:
        by_date[row["date"]] = row
    return list(by_date.values())


def crawl():
    session = requests.Session()
    session.headers.update(HEADERS)
    links = collect_detail_links(session)
    rows = []
    ok = 0
    http200 = 0
    for i, (url, title) in enumerate(sorted(links.items()), start=1):
        try:
            r = session.get(url, timeout=20)
            if r.status_code != 200:
                continue
            http200 += 1
            parsed = parse_detail(r.text, url)
            if parsed:
                ok += 1
                rows.extend(parsed)
                print(f"[{i}/{len(links)}] parsed {len(parsed)}: {title or url}")
        except Exception as exc:
            print(f"[{i}/{len(links)}] warning: {exc}")
    by_date = {}
    for row in rows:
        old = by_date.get(row["date"])
        if old and abs(old["value"] - row["value"]) > 0.02:
            print(f"WARNING conflicting legacy ANA price {row['date']}: {old['value']} vs {row['value']}")
            continue
        by_date[row["date"]] = row
    result = [by_date[d] for d in sorted(by_date)]
    print(f"Legacy article pages HTTP 200: {http200}")
    print(f"Legacy article pages with parsed prices: {ok}")
    return result


with open(DATA_FILE, "r", encoding="utf-8") as f:
    market = json.load(f)

rows = crawl()
if len(rows) < 25:
    raise RuntimeError(f"Legacy ANA backfill unexpectedly short: {len(rows)} observations")

jet = market.setdefault("jet_fuel", {})
existing = {
    row["date"]: {"date": row["date"], "value": float(row["value"])}
    for row in jet.get("data", [])
    if row.get("date") and row.get("value") is not None
}

added = []
for row in rows:
    if row["date"] not in existing:
        existing[row["date"]] = {"date": row["date"], "value": row["value"]}
        added.append(row)

jet["data"] = [existing[d] for d in sorted(existing)]
jet["ana_legacy_history_backfill"] = {
    "source": "ANA Cargo legacy official fuel-surcharge archive",
    "method": "Singapore kerosene / jet fuel monthly averages explicitly printed in official ANA Cargo surcharge notices",
    "frequency": "Monthly where an official notice exists",
    "from": rows[0]["period_start"],
    "to": rows[-1]["period_end"],
    "parsed_observations": len(rows),
    "new_gap_fill_points_added": len(added),
    "caveat": "Official monthly period averages; not reconstructed daily Platts assessments.",
    "observations": rows,
}
jet["source"] = "Public Singapore jet archive + U.S. EIA legacy series + ANA Cargo official averages"
jet["note"] = (
    "Long history contains source/frequency breaks. Daily EIA/current observations are retained; "
    "official ANA Cargo monthly Singapore kerosene averages fill much of the intervening period."
)
market["updated_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")

with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(market, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("ANA legacy history backfill complete.")
print(f"Parsed monthly observations: {len(rows)}")
print(f"Coverage: {rows[0]['period_start']} -> {rows[-1]['period_end']}")
print(f"New gap points added: {len(added)}")
print(f"Total jet_fuel.data rows now: {len(jet['data'])}")
