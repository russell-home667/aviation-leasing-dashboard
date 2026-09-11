import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "market_data.json"
SYMBOL = "BZ=F"
URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
    f"{quote(SYMBOL, safe='')}?range=1d&interval=1m&includePrePost=true"
)

request = Request(
    URL,
    headers={
        "User-Agent": "Mozilla/5.0 (compatible; aviation-leasing-dashboard/1.0)",
        "Accept": "application/json,text/plain,*/*",
    },
)

with urlopen(request, timeout=30) as response:
    payload = json.loads(response.read().decode("utf-8"))

result = (payload.get("chart", {}).get("result") or [None])[0]
if not result:
    raise RuntimeError("Yahoo Finance returned no BZ=F chart result.")

timestamps = result.get("timestamp") or []
quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
closes = quotes.get("close") or []

latest_timestamp = None
latest_price = None
for ts, close in zip(timestamps, closes):
    if close is None:
        continue
    latest_timestamp = int(ts)
    latest_price = round(float(close), 4)

if latest_timestamp is None or latest_price is None:
    raise RuntimeError("Yahoo Finance returned no valid 1-minute BZ=F bars.")

beijing = ZoneInfo("Asia/Shanghai")
quote_time = datetime.fromtimestamp(latest_timestamp, tz=beijing)
quote_iso = quote_time.isoformat(timespec="seconds")

market = json.loads(DATA_FILE.read_text(encoding="utf-8"))
brent = market.setdefault("brent", {})
old_quote = brent.get("latest_quote") or {}

new_quote = {
    "price": latest_price,
    "timestamp": quote_iso,
    "bar_interval": "1m",
    "poll_frequency": "5 minutes",
    "source": "Yahoo Finance BZ=F",
    "quote_status": "DELAYED",
    "note": "Yahoo Finance labels BZ=F as a delayed quote. The source provides 1-minute bars; GitHub Actions checks them every 5 minutes, which is GitHub's shortest scheduled interval.",
}

if old_quote == new_quote:
    print(f"No new Brent quote. Latest remains {quote_iso} = {latest_price}")
    sys.exit(0)

brent["latest_quote"] = new_quote
brent["quote_frequency"] = "1-minute source bars / 5-minute GitHub polling"
brent["quote_timezone"] = "Asia/Shanghai (Beijing Time)"
market["updated_at"] = datetime.now(beijing).isoformat(timespec="seconds")

DATA_FILE.write_text(
    json.dumps(market, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print("Brent quote update successful.")
print(f"Latest 1-minute source bar: {quote_iso} = {latest_price} USD/bbl")
print("Source status: Yahoo Finance delayed quote")
