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
BASE = "https://press.jal.co.jp"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; aviation-leasing-dashboard/1.0; personal research)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

MONTHS = {
    name.lower(): i
    for i, name in enumerate(calendar.month_name)
    if name
}


def month_end(year: int, month: int) -> str:
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def normalize_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).replace("\xa0", " ").split())


def release_year_from_url(url: str):
    m = re.search(r"/release/(20\d{2})\d{2}/", url)
    return int(m.group(1)) if m else None


def collect_release_links(session, category: str, max_pages: int):
    links = {}
    for page in range(1, max_pages + 1):
        url = f"{BASE}/en/{category}/" if page == 1 else f"{BASE}/en/{category}/index_{page}.html"
        try:
            r = session.get(url, timeout=20)
        except Exception as exc:
            print(f"Index fetch warning {url}: {exc}")
            continue
        if r.status_code == 404:
            if page > 3:
                break
            continue
        if r.status_code != 200:
            print(f"Index fetch {url}: HTTP {r.status_code}")
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        found = 0
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "/en/release/" not in href:
                continue
            full = urljoin(url, href)
            title = " ".join(a.get_text(" ", strip=True).split())
            year = release_year_from_url(full)
            if not year or year < 2009 or year > 2026:
                continue
            title_lower = title.lower()
            # The JAL archive contains many releases. Limit the expensive detail-page
            # requests to fuel-surcharge related items.
            if "fuel surcharge" not in title_lower and "surcharge" not in title_lower:
                continue
            links[full] = title
            found += 1
        print(f"JAL {category} index page {page}: {found} candidate releases")
    return links


MONTHLY_PATTERNS = [
    re.compile(
        r"average fuel price of Singapore kerosene(?:-type jet fuel)?\s+"
        r"(?:for the month of|in)\s+([A-Za-z]+)\s+(?:in\s+)?(20\d{2})\s+"
        r"was\s+US\$\s*([0-9]+(?:\.[0-9]+)?)\s+per barrel",
        re.I,
    ),
    re.compile(
        r"average fuel price of Singapore kerosene(?:-type jet fuel)?\s+"
        r"(?:for the month of|in)\s+([A-Za-z]+)\s+(?:in\s+)?(20\d{2})\s+"
        r"was\s+US\$\s*([0-9]+(?:\.[0-9]+)?)",
        re.I,
    ),
]

BIMONTHLY_PATTERN = re.compile(
    r"price of Singapore kerosene-type jet fuel\s+during the two-month period of\s+"
    r"([A-Za-z]+)(?:\s+(20\d{2}))?\s+and\s+"
    r"([A-Za-z]+)(?:\s+(20\d{2}))?\s+averaged\s+"
    r"USD\s*\$?\s*([0-9]+(?:\.[0-9]+)?)\s+per barrel",
    re.I,
)

# Some 2025-2026 pages say "based on the two-month average" and omit "during".
BIMONTHLY_PATTERN_ALT = re.compile(
    r"price of Singapore kerosene-type jet fuel\s+during the two-month period of\s+"
    r"([A-Za-z]+)(?:\s+(20\d{2}))?\s+and\s+"
    r"([A-Za-z]+)(?:\s+(20\d{2}))?\s+averaged\s+USD\s+"
    r"([0-9]+(?:\.[0-9]+)?)",
    re.I,
)


def parse_release(text: str, source_url: str):
    results = []

    for pattern in MONTHLY_PATTERNS:
        for m in pattern.finditer(text):
            month_name, year_s, value_s = m.groups()
            month = MONTHS.get(month_name.lower())
            if not month:
                continue
            year = int(year_s)
            value = float(value_s)
            if 20 <= value <= 300:
                results.append({
                    "date": month_end(year, month),
                    "value": round(value, 2),
                    "observation_type": "monthly_average",
                    "period_start": date(year, month, 1).isoformat(),
                    "period_end": month_end(year, month),
                    "source": "Japan Airlines disclosed Singapore kerosene monthly average",
                    "source_url": source_url,
                })

    for pattern in (BIMONTHLY_PATTERN, BIMONTHLY_PATTERN_ALT):
        for m in pattern.finditer(text):
            m1_name, y1_s, m2_name, y2_s, value_s = m.groups()
            m1 = MONTHS.get(m1_name.lower())
            m2 = MONTHS.get(m2_name.lower())
            if not m1 or not m2:
                continue

            y1 = int(y1_s) if y1_s else None
            y2 = int(y2_s) if y2_s else None
            if y1 is None and y2 is None:
                continue
            if y1 is None:
                y1 = y2 - 1 if m1 > m2 else y2
            if y2 is None:
                y2 = y1 + 1 if m2 < m1 else y1

            value = float(value_s)
            if not 20 <= value <= 300:
                continue

            results.append({
                "date": month_end(y2, m2),
                "value": round(value, 2),
                "observation_type": "two_month_average",
                "period_start": date(y1, m1, 1).isoformat(),
                "period_end": month_end(y2, m2),
                "source": "Japan Airlines disclosed Singapore kerosene two-month average",
                "source_url": source_url,
            })

    return results


def crawl_jal():
    session = requests.Session()
    session.headers.update(HEADERS)

    candidates = {}
    candidates.update(collect_release_links(session, "cargo", 20))
    candidates.update(collect_release_links(session, "fares", 55))

    print(f"Total JAL fuel-surcharge candidate pages: {len(candidates)}")

    parsed = []
    for i, (url, title) in enumerate(sorted(candidates.items()), start=1):
        try:
            r = session.get(url, timeout=20)
            if r.status_code != 200:
                print(f"[{i}/{len(candidates)}] HTTP {r.status_code}: {url}")
                continue
            text = normalize_text(r.text)
            rows = parse_release(text, url)
            if rows:
                parsed.extend(rows)
                print(f"[{i}/{len(candidates)}] parsed {len(rows)}: {title}")
        except Exception as exc:
            print(f"[{i}/{len(candidates)}] warning {url}: {exc}")

    # Deduplicate: one-month averages are more granular and preferred over
    # two-month averages if they happen to land on the same date.
    priority = {"two_month_average": 1, "monthly_average": 2}
    by_date = {}
    for row in parsed:
        old = by_date.get(row["date"])
        if old is None or priority[row["observation_type"]] > priority[old["observation_type"]]:
            by_date[row["date"]] = row

    return [by_date[d] for d in sorted(by_date)]


with open(DATA_FILE, "r", encoding="utf-8") as f:
    market = json.load(f)

jal_rows = crawl_jal()
if len(jal_rows) < 30:
    raise RuntimeError(f"JAL backfill unexpectedly short: only {len(jal_rows)} observations")

jet = market.setdefault("jet_fuel", {})
old_data = jet.get("data", [])
existing = {
    row["date"]: {"date": row["date"], "value": float(row["value"])}
    for row in old_data
    if row.get("date") and row.get("value") is not None
}

added = []
for row in jal_rows:
    # Never replace an existing daily observation. JAL averages are gap-fillers,
    # not substitutes for a higher-frequency observation already in the database.
    if row["date"] not in existing:
        existing[row["date"]] = {"date": row["date"], "value": row["value"]}
        added.append(row)

jet["data"] = [existing[d] for d in sorted(existing)]

monthly = [r for r in jal_rows if r["observation_type"] == "monthly_average"]
bimonthly = [r for r in jal_rows if r["observation_type"] == "two_month_average"]

jet["jal_history_backfill"] = {
    "source": "Japan Airlines press releases",
    "method": "Officially disclosed Singapore kerosene monthly averages and two-month averages used for JAL fuel-surcharge calculations",
    "frequency": "Monthly where available; two-month average otherwise",
    "from": jal_rows[0]["period_start"],
    "to": jal_rows[-1]["period_end"],
    "parsed_observations": len(jal_rows),
    "monthly_average_observations": len(monthly),
    "two_month_average_observations": len(bimonthly),
    "new_gap_fill_points_added": len(added),
    "caveat": "These are published period averages, not reconstructed daily Platts assessments. Existing daily observations are never overwritten.",
    "observations": jal_rows,
}

jet["note"] = (
    "Long history contains source/frequency breaks. Daily EIA/current observations are retained; "
    "the 2010-2026 gap is supplemented with official JAL-disclosed Singapore kerosene monthly or two-month averages."
)
jet["source"] = "Public Singapore jet archive + U.S. EIA legacy series + JAL disclosed averages"

market["updated_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")

with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(market, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("JAL Singapore kerosene backfill complete.")
print(f"Parsed JAL period averages: {len(jal_rows)}")
print(f"  Monthly averages: {len(monthly)}")
print(f"  Two-month averages: {len(bimonthly)}")
print(f"New points inserted into jet_fuel.data: {len(added)}")
print(f"JAL coverage: {jal_rows[0]['period_start']} -> {jal_rows[-1]['period_end']}")
print(f"Total jet_fuel.data rows now: {len(jet['data'])}")
