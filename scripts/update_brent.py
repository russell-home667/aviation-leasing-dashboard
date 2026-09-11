import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf


# =========================
# File paths
# =========================

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "market_data.json"


# =========================
# Load existing JSON
# =========================

with open(DATA_FILE, "r", encoding="utf-8") as f:
    market = json.load(f)


# =========================
# Download Brent data
# Yahoo Finance: BZ=F
# =========================

print("Downloading Brent data from Yahoo Finance...")

ticker = yf.Ticker("BZ=F")

history = ticker.history(
    period="1y",
    interval="1d",
    auto_adjust=False,
    actions=False
)

if history.empty:
    raise RuntimeError("Yahoo Finance returned no Brent data.")

history = history.dropna(subset=["Close"])


# =========================
# Avoid using an unfinished
# current trading day
# =========================

now_ny = datetime.now(
    ZoneInfo("America/New_York")
)

# Use today's bar only after
# the US trading day is safely finished.
if now_ny.hour >= 18:
    max_date = now_ny.date()
else:
    max_date = (
        now_ny.date()
        - timedelta(days=1)
    )


# =========================
# Convert to JSON records
# =========================

fetched_records = []

for index, row in history.iterrows():

    date = index.date()

    if date > max_date:
        continue

    value = float(row["Close"])

    fetched_records.append({
        "date": date.isoformat(),
        "value": round(value, 2)
    })


if not fetched_records:
    raise RuntimeError(
        "No completed Brent daily bars found."
    )


# =========================
# Merge / initialize history
# =========================

old_status = (
    market
    .get("brent", {})
    .get("status")
)

old_records = (
    market
    .get("brent", {})
    .get("data", [])
)


# First LIVE run:
# remove all previous demo Brent data.
if old_status != "LIVE":

    merged_records = fetched_records

else:

    records_by_date = {
        row["date"]: row
        for row in old_records
    }

    for row in fetched_records:
        records_by_date[row["date"]] = row

    merged_records = sorted(
        records_by_date.values(),
        key=lambda x: x["date"]
    )


# =========================
# Detect whether data changed
# =========================

latest = merged_records[-1]

data_changed = (
    old_status != "LIVE"
    or merged_records != old_records
)


if not data_changed:

    print(
        "No new Brent data. "
        f"Latest remains "
        f"{latest['date']} = "
        f"{latest['value']}"
    )

    sys.exit(0)


# =========================
# Update JSON
# =========================

market["brent"]["name"] = "Brent Crude"
market["brent"]["ticker"] = "BZ=F"
market["brent"]["unit"] = "USD/bbl"

market["brent"]["source"] = (
    "Yahoo Finance"
)

market["brent"]["source_url"] = (
    "https://finance.yahoo.com/quote/BZ=F/"
)

market["brent"]["frequency"] = "Daily"

market["brent"]["price_field"] = (
    "Daily Close"
)

market["brent"]["status"] = "LIVE"

market["brent"]["data"] = merged_records


market["updated_at"] = datetime.now(
    ZoneInfo("Asia/Shanghai")
).isoformat(
    timespec="seconds"
)


# =========================
# Save JSON
# =========================

with open(
    DATA_FILE,
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        market,
        f,
        ensure_ascii=False,
        indent=2
    )

    f.write("\n")


print("Brent update successful.")

print(
    "Latest completed observation:",
    latest["date"],
    latest["value"],
    "USD/bbl"
)
