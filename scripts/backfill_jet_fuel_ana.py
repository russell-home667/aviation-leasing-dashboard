import calendar
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "market_data.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; aviation-leasing-dashboard/1.0; personal research)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTH_ABBR = {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name}
MONTH_MAP = {**MONTHS, **MONTH_ABBR}
MONTH_RE = "(?:" + "|".join(sorted((re.escape(x) for x in MONTH_MAP), key=len, reverse=True)) + ")"

INDEX_URLS = [
    # Legacy ANA Cargo fuel-surcharge archive. It contains links going back to 2013.
    "https://www.anacargo.jp/mt/en/news/int/fuel/",
    # Older archive years are linked separately.
    "https://www.anacargo.jp/en/news/int/2011/",
    "https://www.anacargo.jp/en/news/int/2012/",
]
INDEX_URLS += [f"https://www.anacargo.jp/en/news/search.php?year={year}" for year in range(2020, 2027)]


def month_end(year: int, month: int) -> str:
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).replace("\xa0", " ").split())


def parse_page_date(text: str):
    # ANA pages usually show YYYY.MM.DD near the top.
    candidates = re.findall(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b", text)
    for y, m, d in candidates:
        try:
            dt = date(int(y), int(m), int(d))
            if 2010 <= dt.year <= 2027:
                return dt
        except ValueError:
            pass
    return None


def infer_year(month: int, page_date: date | None):
    if not page_date:
        return None
    # The benchmark month normally precedes the article by 1-2 months.
    # If the benchmark month number is greater than the article month number,
    # it belongs to the previous calendar year.
    return page_date.year - 1 if month > page_date.month else page_date.year


def month_number(name: str):
    return MONTH_MAP.get(name.strip().lower().rstrip("."))


def parse_ana_release(html: str, url: str):
    text = clean_text(html)
    if "fuel surcharge" not in text.lower():
        return []
    if "singapore kerosene" not in text.lower() and "average price of jet fuel" not in text.lower():
        return []

    page_date = parse_page_date(text)
    rows = []

    # Legacy wording, e.g.
    # "according to the average price of jet fuel for the month of April, USD 85.10"
    legacy_patterns = [
        re.compile(
            rf"average price of (?:jet fuel|Singapore kerosene)(?:[^.]){{0,80}}?"
            rf"(?:for )?the month of\s+({MONTH_RE})(?:\s+(20\d{{2}}))?\s*[,;:]?\s*"
            rf"(?:USD|US\$|USD\$)\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
            re.I,
        ),
        re.compile(
            rf"average (?:fuel )?price of Singapore kerosene(?:[^.]){{0,80}}?"
            rf"(?:for )?the month of\s+({MONTH_RE})(?:\s+(20\d{{2}}))?\s+was\s+"
            rf"(?:USD|US\$|USD\$)?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
            re.I,
        ),
    ]

    for pattern in legacy_patterns:
        for m in pattern.finditer(text):
            month = month_number(m.group(1))
            explicit_year = int(m.group(2)) if m.group(2) else None
            value = float(m.group(3))
            year = explicit_year or infer_year(month, page_date)
            if year and month and 15 <= value <= 350:
                rows.append({
                    "date": month_end(year, month),
                    "value": round(value, 2),
                    "observation_type": "monthly_average",
                    "period_start": date(year, month, 1).isoformat(),
                    "period_end": month_end(year, month),
                    "source": "ANA Cargo disclosed Singapore kerosene monthly average",
                    "source_url": url,
                })

    # Modern wording, e.g.
    # "average price being USD$95.58/bbl for the period of 1May-31May2024"
    # "average price being USD$53.57/bbl for the period of December 1 ~ 31, 2020"
    modern = re.compile(
        r"average price being\s+(?:USD|US\$|USD\$)?\s*\$?\s*([0-9]+(?:\.[0-9]+)?)"
        r"\s*(?:/bbl|per barrel)?\s+for the period of\s+([^.;]{1,100})",
        re.I,
    )
    for m in modern.finditer(text):
        value = float(m.group(1))
        segment = m.group(2)
        month_match = re.search(MONTH_RE, segment, flags=re.I)
        if not month_match:
            continue
        month = month_number(month_match.group(0))
        year_match = re.search(r"20\d{2}", segment)
        year = int(year_match.group(0)) if year_match else infer_year(month, page_date)
        if year and month and 15 <= value <= 350:
            rows.append({
                "date": month_end(year, month),
                "value": round(value, 2),
                "observation_type": "monthly_average",
                "period_start": date(year, month, 1).isoformat(),
                "period_end": month_end(year, month),
                "source": "ANA Cargo disclosed Singapore kerosene monthly average",
                "source_url": url,
            })

    # A few pages use "said average price being USD 58.07/bbl" without a dollar sign.
    # The generic modern pattern above already handles this; deduplicate below.
    by_date = {}
    for row in rows:
        by_date[row["date"]] = row
    return list(by_date.values())


def is_candidate_link(text: str, href: str):
    t = text.lower()
    h = href.lower()
    if "fuel surcharge" not in t and "/fuel/" not in h and "fuel_surcharge" not in h and "fuel-surcharge" not in h:
        return False
    # Prefer ex-Japan / international cargo notices; parser will reject irrelevant pages.
    return True


def collect_links(session: requests.Session):
    links = {}
    for index_url in INDEX_URLS:
        try:
            r = session.get(index_url, timeout=25, allow_redirects=True)
            print(f"ANA index {index_url}: HTTP {r.status_code}, {len(r.content)} bytes")
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            found = 0
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                text = " ".join(a.get_text(" ", strip=True).split())
                if not is_candidate_link(text, href):
                    continue
                full = urljoin(index_url, href)
                if not full.startswith("https://www.anacargo.jp/"):
                    continue
                if full.rstrip("/") == index_url.rstrip("/"):
                    continue
                links[full] = text
                found += 1
            print(f"  candidate detail links: {found}")
        except Exception as exc:
            print(f"  index warning: {exc}")
    return links


def crawl_ana():
    session = requests.Session()
    session.headers.update(HEADERS)
    links = collect_links(session)
    print(f"Unique ANA candidate pages: {len(links)}")

    rows = []
    successes = 0
    for i, (url, title) in enumerate(sorted(links.items()), start=1):
        try:
            r = session.get(url, timeout=25, allow_redirects=True)
            if r.status_code != 200:
                continue
            parsed = parse_ana_release(r.text, url)
            if parsed:
                successes += 1
                rows.extend(parsed)
                print(f"[{i}/{len(links)}] {len(parsed)} price row(s): {title or url}")
        except Exception as exc:
            print(f"[{i}/{len(links)}] detail warning {url}: {exc}")

    # One underlying month can appear in duplicate approval/not-approval notices.
    # Keep one identical official ANA observation for each month.
    by_date = {}
    for row in rows:
        old = by_date.get(row["date"])
        if old is None:
            by_date[row["date"]] = row
        elif abs(old["value"] - row["value"]) > 0.02:
            print(f"WARNING conflicting ANA monthly value {row['date']}: {old['value']} vs {row['value']}")

    result = [by_date[d] for d in sorted(by_date)]
    print(f"ANA pages with parsed values: {successes}")
    return result


with open(DATA_FILE, "r", encoding="utf-8") as f:
    market = json.load(f)

ana_rows = crawl_ana()
if len(ana_rows) < 40:
    raise RuntimeError(f"ANA Singapore kerosene backfill unexpectedly short: {len(ana_rows)} monthly observations")

jet = market.setdefault("jet_fuel", {})
existing = {
    row["date"]: {"date": row["date"], "value": float(row["value"])}
    for row in jet.get("data", [])
    if row.get("date") and row.get("value") is not None
}

added = []
for row in ana_rows:
    # Never overwrite daily observations already captured from EIA or the current archive.
    if row["date"] not in existing:
        existing[row["date"]] = {"date": row["date"], "value": row["value"]}
        added.append(row)

jet["data"] = [existing[d] for d in sorted(existing)]
jet["ana_history_backfill"] = {
    "source": "ANA Cargo official fuel-surcharge notices",
    "method": "Monthly average Singapore kerosene prices explicitly disclosed in ANA Cargo fuel-surcharge notices",
    "frequency": "Monthly observations where an ANA notice is publicly archived",
    "from": ana_rows[0]["period_start"],
    "to": ana_rows[-1]["period_end"],
    "parsed_observations": len(ana_rows),
    "new_gap_fill_points_added": len(added),
    "caveat": "Official period averages, not reconstructed daily Platts assessments. Existing EIA/current daily observations are never overwritten.",
    "observations": ana_rows,
}
jet["source"] = "Public Singapore jet archive + U.S. EIA legacy series + ANA Cargo disclosed monthly averages"
jet["note"] = (
    "Long history contains source/frequency breaks. Daily EIA/current observations are retained; "
    "the 2010-2026 gap is supplemented with official ANA Cargo Singapore kerosene monthly averages where available."
)

market["updated_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")

with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(market, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("ANA Singapore kerosene backfill complete.")
print(f"Parsed official monthly observations: {len(ana_rows)}")
print(f"Coverage: {ana_rows[0]['period_start']} -> {ana_rows[-1]['period_end']}")
print(f"New points added to jet_fuel.data: {len(added)}")
print(f"Total jet_fuel.data rows now: {len(jet['data'])}")
