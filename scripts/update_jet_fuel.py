import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "market_data.json"

# Public reference pages. These are indicative FOB Singapore jet/kerosene
# archive levels, not a licensed Platts/CME settlement feed.
SOURCE_URLS = [
    "https://alghafmarine.com/prices/history/jet-sg/?window=all",
    "https://alghafmarine.com/prices/history/jet-sg/",
    "https://commodityscope.com/prices/singapore-jet-kerosene",
]

# Historical backfill assembled from the public FOB Singapore archive.
# It gives the dashboard a useful history even if the source website later
# limits the number of rows exposed in its HTML. Live scrape values overwrite
# these seed values whenever the same date is found online.
SEED_HISTORY = [
    {"date": "2026-01-26", "value": 85.4},
    {"date": "2026-01-28", "value": 85.7},
    {"date": "2026-01-29", "value": 87.6},
    {"date": "2026-02-02", "value": 85.9},
    {"date": "2026-02-04", "value": 86.7},
    {"date": "2026-02-05", "value": 87.1},
    {"date": "2026-02-06", "value": 87.7},
    {"date": "2026-02-09", "value": 86.5},
    {"date": "2026-02-10", "value": 87.5},
    {"date": "2026-02-11", "value": 88.5},
    {"date": "2026-02-12", "value": 88.6},
    {"date": "2026-02-13", "value": 86.4},
    {"date": "2026-02-16", "value": 85.8},
    {"date": "2026-02-19", "value": 90.9},
    {"date": "2026-02-20", "value": 92.5},
    {"date": "2026-02-23", "value": 91.8},
    {"date": "2026-02-24", "value": 92.7},
    {"date": "2026-02-25", "value": 92.5},
    {"date": "2026-02-26", "value": 92.7},
    {"date": "2026-02-27", "value": 93.6},
    {"date": "2026-03-02", "value": 116.5},
    {"date": "2026-03-03", "value": 130.2},
    {"date": "2026-03-04", "value": 231.4},
    {"date": "2026-03-05", "value": 201.2},
    {"date": "2026-03-06", "value": 160.6},
    {"date": "2026-03-09", "value": 188.2},
    {"date": "2026-03-10", "value": 143.0},
    {"date": "2026-03-11", "value": 157.3},
    {"date": "2026-03-12", "value": 209.5},
    {"date": "2026-03-13", "value": 199.5},
    {"date": "2026-03-16", "value": 201.4},
    {"date": "2026-03-17", "value": 201.3},
    {"date": "2026-03-18", "value": 201.1},
    {"date": "2026-03-19", "value": 227.4},
    {"date": "2026-03-20", "value": 222.5},
    {"date": "2026-03-23", "value": 234.3},
    {"date": "2026-03-24", "value": 204.6},
    {"date": "2026-03-25", "value": 182.2},
    {"date": "2026-03-26", "value": 200.5},
    {"date": "2026-03-27", "value": 224.9},
    {"date": "2026-03-30", "value": 242.7},
    {"date": "2026-03-31", "value": 218.4},
    {"date": "2026-04-01", "value": 218.6},
    {"date": "2026-04-06", "value": 228.8},
    {"date": "2026-04-07", "value": 229.2},
    {"date": "2026-04-08", "value": 195.3},
    {"date": "2026-04-09", "value": 216.4},
    {"date": "2026-04-10", "value": 212.6},
    {"date": "2026-04-13", "value": 216.0},
    {"date": "2026-04-16", "value": 205.4},
    {"date": "2026-04-17", "value": 208.8},
    {"date": "2026-04-20", "value": 186.5},
    {"date": "2026-04-21", "value": 177.8},
    {"date": "2026-04-22", "value": 184.3},
    {"date": "2026-04-23", "value": 178.7},
    {"date": "2026-04-24", "value": 183.0},
    {"date": "2026-04-27", "value": 185.1},
    {"date": "2026-04-28", "value": 174.2},
    {"date": "2026-04-29", "value": 176.5},
    {"date": "2026-04-30", "value": 186.4},
    {"date": "2026-05-05", "value": 165.8},
    {"date": "2026-05-06", "value": 157.3},
    {"date": "2026-05-07", "value": 148.3},
    {"date": "2026-05-08", "value": 150.9},
    {"date": "2026-05-11", "value": 153.4},
    {"date": "2026-05-12", "value": 158.2},
    {"date": "2026-05-13", "value": 159.8},
    {"date": "2026-05-14", "value": 151.9},
    {"date": "2026-05-15", "value": 155.3},
    {"date": "2026-05-18", "value": 160.1},
    {"date": "2026-05-19", "value": 162.4},
    {"date": "2026-05-20", "value": 160.2},
    {"date": "2026-05-22", "value": 147.0},
    {"date": "2026-05-25", "value": 135.7},
    {"date": "2026-05-26", "value": 136.2},
    {"date": "2026-05-28", "value": 133.2},
    {"date": "2026-05-29", "value": 128.1},
    {"date": "2026-06-02", "value": 138.8},
    {"date": "2026-06-03", "value": 148.7},
    {"date": "2026-06-04", "value": 148.0},
    {"date": "2026-06-05", "value": 140.7},
    {"date": "2026-06-08", "value": 147.2},
    {"date": "2026-06-09", "value": 140.1},
    {"date": "2026-06-10", "value": 138.0},
    {"date": "2026-06-11", "value": 138.9},
    {"date": "2026-06-12", "value": 127.3},
    {"date": "2026-06-16", "value": 115.6},
    {"date": "2026-06-17", "value": 115.8},
    {"date": "2026-06-18", "value": 112.1},
    {"date": "2026-06-19", "value": 112.2},
    {"date": "2026-06-22", "value": 114.5},
    {"date": "2026-06-23", "value": 110.6},
    {"date": "2026-06-24", "value": 111.1},
    {"date": "2026-06-25", "value": 111.0},
    {"date": "2026-06-26", "value": 110.3},
    {"date": "2026-07-06", "value": 114.7},
    {"date": "2026-07-07", "value": 116.5},
    {"date": "2026-07-10", "value": 122.7},
    {"date": "2026-07-20", "value": 147.5},
    {"date": "2026-07-21", "value": 149.4},
    {"date": "2026-07-29", "value": 150.8},
    {"date": "2026-07-31", "value": 153.3},
    {"date": "2026-08-03", "value": 149.3},
    {"date": "2026-08-04", "value": 140.3},
    {"date": "2026-08-05", "value": 134.9},
    {"date": "2026-08-06", "value": 137.0},
    {"date": "2026-08-07", "value": 143.7},
    {"date": "2026-08-11", "value": 154.3},
    {"date": "2026-08-12", "value": 152.1},
    {"date": "2026-08-13", "value": 151.6},
    {"date": "2026-08-14", "value": 152.1},
    {"date": "2026-08-17", "value": 152.3},
    {"date": "2026-08-18", "value": 155.7},
    {"date": "2026-08-19", "value": 156.6},
    {"date": "2026-08-20", "value": 155.0},
    {"date": "2026-08-21", "value": 154.0},
    {"date": "2026-08-24", "value": 153.9},
    {"date": "2026-08-25", "value": 147.0},
    {"date": "2026-08-26", "value": 139.2},
    {"date": "2026-08-27", "value": 142.9},
    {"date": "2026-08-28", "value": 146.7},
    {"date": "2026-08-31", "value": 150.0},
    {"date": "2026-09-01", "value": 154.74},
    {"date": "2026-09-02", "value": 159.31},
    {"date": "2026-09-03", "value": 159.58},
    {"date": "2026-09-04", "value": 159.31},
    {"date": "2026-09-07", "value": 160.13},
    {"date": "2026-09-08", "value": 163.75},
    {"date": "2026-09-09", "value": 163.19},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; aviation-leasing-dashboard/1.0)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def parse_date(text):
    text = " ".join(text.replace(",", " ").split())
    formats = [
        "%Y-%m-%d",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d %Y",
        "%b %d %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_number(text):
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None
    try:
        value = float(match.group(0))
    except ValueError:
        return None
    if 20 <= value <= 500:
        return round(value, 2)
    return None


def extract_rows_from_html(html):
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
            cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            date = parse_date(cells[0])
            if not date:
                continue

            value = None
            if mid_index is not None and mid_index < len(cells):
                value = parse_number(cells[mid_index])
            if value is None:
                # Alghaf format: Date | Mid | Change | Unit
                value = parse_number(cells[1])
            if value is not None:
                rows[date] = value

    # Also catch common embedded JSON shapes used by chart components.
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
            value = parse_number(number)
            if value is not None:
                rows[date] = value

    return rows


def fetch_public_history():
    merged = {}
    successes = []
    session = requests.Session()
    session.headers.update(HEADERS)

    for url in SOURCE_URLS:
        try:
            response = session.get(url, timeout=30, allow_redirects=True)
            print(f"Jet fuel source {url}: HTTP {response.status_code}, {len(response.content)} bytes")
            if response.status_code != 200:
                continue
            rows = extract_rows_from_html(response.text)
            print(f"  parsed {len(rows)} dated rows")
            if rows:
                # Later sources are preferred, so they overwrite earlier values.
                merged.update(rows)
                successes.append(url)
        except Exception as exc:
            print(f"  warning: {exc}")

    return merged, successes


with open(DATA_FILE, "r", encoding="utf-8") as f:
    market = json.load(f)

old_jet = market.get("jet_fuel", {})
old_records = old_jet.get("data", [])
old_by_date = {
    row.get("date"): row.get("value")
    for row in old_records
    if row.get("date") and row.get("value") is not None
}

records_by_date = {
    row["date"]: row["value"]
    for row in SEED_HISTORY
}

# Preserve any previously captured real history beyond the seed.
if old_jet.get("status") == "LIVE":
    records_by_date.update(old_by_date)

fetched, successful_sources = fetch_public_history()
records_by_date.update(fetched)

records = [
    {"date": date, "value": round(float(value), 2)}
    for date, value in sorted(records_by_date.items())
]

if len(records) < 100:
    raise RuntimeError(f"Jet fuel history unexpectedly short: {len(records)} rows")

latest = records[-1]
market.setdefault("jet_fuel", {})
market["jet_fuel"].update({
    "name": "Singapore Jet Kerosene",
    "ticker": "FOB Singapore Mid",
    "unit": "USD/bbl",
    "source": "Public FOB Singapore jet/kerosene archive",
    "source_url": successful_sources[-1] if successful_sources else SOURCE_URLS[0],
    "frequency": "Daily / published observations",
    "price_field": "Indicative FOB Singapore Mid",
    "status": "LIVE" if successful_sources else "BACKFILL_ONLY",
    "note": "Indicative public market reference; not a licensed Platts/CME settlement feed.",
    "data": records,
})

market["updated_at"] = datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")

new_jet = market["jet_fuel"]
changed = new_jet != old_jet

with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(market, f, ensure_ascii=False, indent=2)
    f.write("\n")

print(f"Singapore Jet Kerosene history rows: {len(records)}")
print(f"Latest observation: {latest['date']} = {latest['value']} USD/bbl")
print(f"Online source success: {bool(successful_sources)}")
print(f"Data changed: {changed}")

if not successful_sources:
    print("WARNING: live source unavailable; historical backfill retained.")
