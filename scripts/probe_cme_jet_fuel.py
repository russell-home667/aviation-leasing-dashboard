import requests
from datetime import datetime, timezone

URL = (
    "https://www.cmegroup.com/markets/energy/refined-products/"
    "singapore-jet-kerosene-swap-futures.settlements.html"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

print("=" * 60)
print("CME Singapore Jet Kerosene connectivity test")
print("UTC time:", datetime.now(timezone.utc).isoformat())
print("URL:", URL)
print("=" * 60)

try:
    response = requests.get(
        URL,
        headers=HEADERS,
        timeout=30,
    )
except Exception as exc:
    raise RuntimeError(
        f"ERROR: Could not connect to CME: {exc}"
    ) from exc

print("HTTP status:", response.status_code)
print("Content-Type:", response.headers.get("Content-Type"))
print("Downloaded bytes:", len(response.content))

if response.status_code != 200:
    raise RuntimeError(
        f"ERROR: CME returned HTTP {response.status_code}"
    )

html = response.text

checks = [
    "Singapore Jet Kerosene",
    "Settlement",
    "settle",
    "productId",
    "CmeWS",
    "tradeDate",
]

print()
print("Keywords found in raw CME HTML:")

for word in checks:
    found = word.lower() in html.lower()
    print(f"  {word}: {found}")

print()
print("SUCCESS: GitHub Actions can access the CME page.")
print(
    "This test does NOT write anything to market_data.json."
)
