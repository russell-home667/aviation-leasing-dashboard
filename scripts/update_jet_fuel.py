import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "market_data.json"

CURRENT_SOURCE_URLS = [
    "https://alghafmarine.com/prices/history/jet-sg/?window=all",
    "https://alghafmarine.com/prices/history/jet-sg/",
    "https://commodityscope.com/prices/singapore-jet-kerosene",
]

# Historical EIA series previously published as
# "Singapore Kerosene-Type Jet Fuel Spot Price FOB".
LEGACY_EIA_URLS = [
    "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=rjetsin5&f=D",
    "http://tonto.eia.doe.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=rjetsin5&f=D",
    "https://web.archive.org/web/20141231000000id_/http://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=rjetsin5&f=D",
    "https://web.archive.org/web/20101231000000id_/http://tonto.eia.doe.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=rjetsin5&f=D",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; aviation-leasing-dashboard/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def parse_date(text):
    text = " ".join(text.replace(",", " ").split())
    for fmt in (
        "%Y-%m-%d",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d %Y",
        "%b %d %Y",
    ):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_number(text, minimum=1, maximum=1000):
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None
    try:
        value = float(match.group(0))
    except ValueError:
        return None
    if minimum <= value <= maximum:
        return value
    return None


def extract_current_rows(html):
    rows = {}
    soup = BeautifulSoup(html, "html.parser")

    for table in soup.find_all("table"):
        headers = [
            " ".join(cell.get_text(" ", strip=True).lower().split())
            for cell in table.find_all("th")
        ]
        mid_index = None
        for i, header in enumerate(headers):
            if header == "mid" or "average mid" in header:
                mid_index = i
                break

        for tr in table.find_all("tr"):
            cells = [
                " ".join(td.get_text(" ", strip=True).split())
                for td in tr.find_all(["td", "th"])
            ]
            if len(cells) < 2:
                continue
            date = parse_date(cells[0])
            if not date:
                continue
            value = None
            if mid_index is not None and mid_index < len(cells):
                value = parse_number(cells[mid_index], 20, 500)
            if value is None:
                value = parse_number(cells[1], 20, 500)
            if value is not None:
                rows[date] = round(value, 2)

    patterns = [
        r'["\']date["\']\s*:\s*["\'](\d{4}-\d{2}-\d{2})["\'][^{}]{0,180}?["\'](?:mid|value|price)["\']\s*:\s*["\']?([0-9]+(?:\.[0-9]+)?)',
        r'["\'](?:mid|value|price)["\']\s*:\s*["\']?([0-9]+(?:\.[0-9]+)?)["\']?[^{}]{0,180}?["\']date["\']\s*:\s*["\'](\d{4}-\d{2}-\d{2})["\']',
    ]
    for i, pattern in enumerate(patterns):
        for match in re.finditer(pattern, html, flags=re.I):
            if i == 0:
                date, number = match.group(1), match.group(2)
            else:
                number, date = match.group(1), match.group(2)
            value = parse_number(number, 20, 500)
            if value is not None:
                rows[date] = round(value, 2)

    return rows


def parse_week_start(label):
    clean = " ".join(label.replace("\xa0", " ").split())
    match = re.search(
        r"(\d{4})\s+([A-Za-z]{3})-\s*(\d{1,2})\s+to\s+([A-Za-z]{3})-\s*(\d{1,2})",
        clean,
        flags=re.I,
    )
    if not match:
        return None
    year = int(match.group(1))
    month = match.group(2).title()
    day = int(match.group(3))
    try:
        return datetime.strptime(f"{year} {month} {day}", "%Y %b %d").date()
    except ValueError:
        return None


def extract_legacy_eia_rows(html):
    lower = html.lower()
    if "singapore" not in lower or "jet fuel" not in lower:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    page_text = " ".join(soup.stripped_strings).lower()
    raw_rows = []

    for tr in soup.find_all("tr"):
        cells = [
            " ".join(td.get_text(" ", strip=True).split())
            for td in tr.find_all(["td", "th"])
        ]
        if len(cells) < 2:
            continue
        start = parse_week_start(cells[0])
        if not start:
            continue
        for offset, cell in enumerate(cells[1:6]):
            value = parse_number(cell, 0.01, 1000)
            if value is not None:
                raw_rows.append((start + timedelta(days=offset), value))

    if not raw_rows:
        return {}

    if "cents" in page_text and "gallon" in page_text:
        converter = lambda x: x * 42.0 / 100.0
        unit_note = "cents/US gallon converted to USD/bbl"
    elif "dollars per gallon" in page_text:
        converter = lambda x: x * 42.0
        unit_note = "USD/US gallon converted to USD/bbl"
    else:
        sample = sorted(value for _, value in raw_rows)[len(raw_rows) // 2]
        if sample > 10:
            converter = lambda x: x * 42.0 / 100.0
            unit_note = "inferred cents/US gallon converted to USD/bbl"
        else:
            converter = lambda x: x * 42.0
            unit_note = "inferred USD/US gallon converted to USD/bbl"

    rows = {}
    for date, raw_value in raw_rows:
        value = round(converter(raw_value), 2)
        if 10 <= value <= 500:
            rows[date.isoformat()] = value

    return rows, unit_note


def fetch_current_history(session):
    merged = {}
    successes = []
    for url in CURRENT_SOURCE_URLS:
        try:
            response = session.get(url, timeout=30, allow_redirects=True)
            print(f"Current jet source {url}: HTTP {response.status_code}, {len(response.content)} bytes")
            if response.status_code != 200:
                continue
            rows = extract_current_rows(response.text)
            print(f"  parsed {len(rows)} current dated rows")
            if rows:
                merged.update(rows)
                successes.append(url)
        except Exception as exc:
            print(f"  warning: {exc}")
    return merged, successes


def fetch_legacy_history(session):
    best_rows = {}
    best_url = None
    unit_note = None
    for url in LEGACY_EIA_URLS:
        try:
            response = session.get(url, timeout=45, allow_redirects=True)
            print(f"Legacy EIA source {url}: HTTP {response.status_code}, {len(response.content)} bytes")
            if response.status_code != 200:
                continue
            parsed = extract_legacy_eia_rows(response.text)
            if not parsed:
                print("  no valid Singapore EIA rows parsed")
                continue
            rows, parsed_unit_note = parsed
            print(f"  parsed {len(rows)} legacy EIA dated rows")
            if len(rows) > len(best_rows):
                best_rows = rows
                best_url = url
                unit_note = parsed_unit_note
        except Exception as exc:
            print(f"  warning: {exc}")
    return best_rows, best_url, unit_note


with open(DATA_FILE, "r", encoding="utf-8") as f:
    market = json.load(f)

old_jet = market.get("jet_fuel", {})
old_records = old_jet.get("data", [])
records_by_date = {
    row["date"]: float(row["value"])
    for row in old_records
    if row.get("date") and row.get("value") is not None
}

session = requests.Session()
session.headers.update(HEADERS)

legacy_rows, legacy_url, legacy_unit_note = fetch_legacy_history(session)
for date, value in legacy_rows.items():
    records_by_date.setdefault(date, value)

current_rows, current_successes = fetch_current_history(session)
records_by_date.update(current_rows)

records = [
    {"date": date, "value": round(float(value), 2)}
    for date, value in sorted(records_by_date.items())
]

if len(records) < 100:
    raise RuntimeError(f"Jet fuel history unexpectedly short: {len(records)} rows")

latest = records[-1]
earliest = records[0]

history_segments = []
if legacy_rows:
    legacy_dates = sorted(legacy_rows)
    history_segments.append({
        "source": "U.S. EIA legacy Singapore Kerosene-Type Jet Fuel Spot Price FOB",
        "source_url": legacy_url,
        "from": legacy_dates[0],
        "to": legacy_dates[-1],
        "observations": len(legacy_rows),
        "conversion": legacy_unit_note,
        "note": "Legacy historical benchmark; methodology/source differs from the current public indicative archive.",
    })
if current_rows:
    current_dates = sorted(current_rows)
    history_segments.append({
        "source": "Public FOB Singapore jet/kerosene archive",
        "source_url": current_successes[-1] if current_successes else CURRENT_SOURCE_URLS[0],
        "from": current_dates[0],
        "to": current_dates[-1],
        "observations": len(current_rows),
        "note": "Current public indicative FOB Singapore mid; not a licensed Platts/CME settlement feed.",
    })

market.setdefault("jet_fuel", {})
market["jet_fuel"].update({
    "name": "Singapore Jet Kerosene",
    "ticker": "FOB Singapore Mid",
    "unit": "USD/bbl",
    "source": "Public FOB Singapore archive + legacy U.S. EIA Singapore series" if legacy_rows else "Public FOB Singapore jet/kerosene archive",
    "source_url": current_successes[-1] if current_successes else CURRENT_SOURCE_URLS[0],
    "frequency": "Daily / published observations",
    "price_field": "Indicative / historical FOB Singapore jet kerosene reference",
    "status": "LIVE" if current_successes else old_jet.get("status", "BACKFILL_ONLY"),
    "note": "Long history may contain source/methodology breaks. Legacy EIA observations and current indicative archive are explicitly separated in history_segments.",
    "history_segments": history_segments,
    "data": records,
})

market["updated_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")

with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(market, f, ensure_ascii=False, indent=2)
    f.write("\n")

print(f"Singapore Jet Kerosene total history rows: {len(records)}")
print(f"Earliest observation: {earliest['date']} = {earliest['value']} USD/bbl")
print(f"Latest observation: {latest['date']} = {latest['value']} USD/bbl")
print(f"Legacy EIA rows added/available: {len(legacy_rows)}")
print(f"Current online rows parsed: {len(current_rows)}")
