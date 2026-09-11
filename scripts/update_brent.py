import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "market_data.json"

with open(DATA_FILE, "r", encoding="utf-8") as f:
    market = json.load(f)

print("Downloading full Brent daily history from Yahoo Finance...")

ticker = yf.Ticker("BZ=F")
history = ticker.history(
    period="max",
    interval="1d",
    auto_adjust=False,
    actions=False,
)

if history.empty:
    raise RuntimeError("Yahoo Finance returned no Brent data.")

history = history.dropna(subset=["Close"])

now_ny = datetime.now(ZoneInfo("America/New_York"))
if now_ny.hour >= 18:
    max_date = now_ny.date()
else:
    max_date = now_ny.date() - timedelta(days=1)

fetched_records = []
for index, row in history.iterrows():
    date = index.date()
    if date > max_date:
        continue
    fetched_records.append({
        "date": date.isoformat(),
        "value": round(float(row["Close"]), 2),
    })

if not fetched_records:
    raise RuntimeError("No completed Brent daily bars found.")

old_section = market.get("brent", {})
old_records = old_section.get("data", [])
records_by_date = {
    row["date"]: row
    for row in old_records
    if row.get("date") and row.get("value") is not None
}
for row in fetched_records:
    records_by_date[row["date"]] = row

merged_records = sorted(records_by_date.values(), key=lambda x: x["date"])
latest = merged_records[-1]
earliest = merged_records[0]

market.setdefault("brent", {})
market["brent"].update({
    "name": "Brent Crude",
    "ticker": "BZ=F",
    "unit": "USD/bbl",
    "source": "Yahoo Finance",
    "source_url": "https://finance.yahoo.com/quote/BZ=F/",
    "frequency": "Daily",
    "price_field": "Daily Close",
    "status": "LIVE",
    "history_scope": "Maximum daily history available from Yahoo Finance BZ=F",
    "data": merged_records,
})

market["updated_at"] = datetime.now(
    ZoneInfo("Asia/Shanghai")
).isoformat(timespec="seconds")

new_records = market["brent"]["data"]
if new_records == old_records:
    print(
        "No Brent history change. "
        f"Coverage remains {earliest['date']} to {latest['date']} "
        f"({len(new_records)} rows)."
    )
    sys.exit(0)

with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(market, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("Brent history update successful.")
print(f"History rows: {len(merged_records)}")
print(f"Earliest observation: {earliest['date']} = {earliest['value']} USD/bbl")
print(f"Latest completed observation: {latest['date']} = {latest['value']} USD/bbl")
