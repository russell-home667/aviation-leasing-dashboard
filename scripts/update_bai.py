import json
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "market_data.json"

CHART_URL = "https://api.tacindex.com/freight/chart/BAI00/?index=BAI"
SUMMARY_URL = "https://api.tacindex.com/route/BAI00/?currency=USD&index=BAI"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://dashboard.tacindex.com/",
    "Origin": "https://dashboard.tacindex.com",
}


def fetch_json(session, url):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def to_date(timestamp_ms):
    return datetime.fromtimestamp(float(timestamp_ms) / 1000, tz=timezone.utc).date().isoformat()


def main():
    market = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    session = requests.Session()
    session.headers.update(HEADERS)

    raw_history = fetch_json(session, CHART_URL)
    if not isinstance(raw_history, list) or not raw_history:
        raise RuntimeError("BAI00 history endpoint returned no data")

    history_by_date = {}
    for row in raw_history:
        if not isinstance(row, list) or len(row) < 2:
            continue
        try:
            date = to_date(row[0])
            value = float(row[1])
        except (TypeError, ValueError, OverflowError):
            continue
        history_by_date[date] = value

    if not history_by_date:
        raise RuntimeError("No valid BAI00 observations parsed")

    data = [
        {"date": date, "value": value}
        for date, value in sorted(history_by_date.items())
    ]

    # Fetch the public route summary as a cross-check and to capture the newest observation.
    try:
        summary = fetch_json(session, SUMMARY_URL)
        index_rows = summary.get("index") or []
        if index_rows:
            latest = index_rows[0]
            latest_date = latest.get("date")
            latest_price = latest.get("price")
            if latest_date and latest_price is not None:
                history_by_date[str(latest_date)] = float(latest_price)
                data = [
                    {"date": date, "value": value}
                    for date, value in sorted(history_by_date.items())
                ]
    except Exception as exc:
        print(f"BAI00 summary cross-check failed, keeping chart history: {exc}")

    previous = market.get("bai00", {})
    new_section = {
        "name": "Baltic Air Freight Index",
        "ticker": "BAI00",
        "unit": "Index",
        "source": "TAC Index / Baltic Exchange",
        "frequency": "Weekly",
        "data": data,
        "source_url": "https://dashboard.tacindex.com/detail?route=BAI00&routeType=basket",
        "api_source_url": CHART_URL,
        "price_field": "BAI00 weekly index value",
        "status": "LIVE",
        "note": "Baltic Air Freight Index calculated by TAC Index under Baltic Exchange governance; public endpoint availability may change.",
    }

    market["bai00"] = new_section
    market["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    DATA_FILE.write_text(
        json.dumps(market, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    latest = data[-1]
    earliest = data[0]
    print(f"BAI00 history rows: {len(data)}")
    print(f"Earliest observation: {earliest['date']} = {earliest['value']}")
    print(f"Latest observation: {latest['date']} = {latest['value']}")
    print(f"Data changed: {previous != new_section}")


if __name__ == "__main__":
    main()
