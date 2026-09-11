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
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

print("=" * 70)
print("CME Singapore Jet Kerosene diagnostic test")
print("UTC time:", datetime.now(timezone.utc).isoformat())
print("URL:", URL)
print("=" * 70)

session = requests.Session()
session.headers.update(HEADERS)

try:
    response = session.get(
        URL,
        timeout=30,
        allow_redirects=True,
    )
except Exception as exc:
    raise RuntimeError(
        f"ERROR: Could not connect to CME: {exc}"
    ) from exc

print()
print("HTTP status:", response.status_code)
print("Final URL:", response.url)
print("Content-Type:", response.headers.get("Content-Type"))
print("Server:", response.headers.get("Server"))
print("Downloaded bytes:", len(response.content))

print()
print("=" * 70)
print("CME RESPONSE BODY")
print("=" * 70)

body = response.text

# Print enough of the error response for diagnosis.
print(body[:3000])

print()
print("=" * 70)

if response.status_code != 200:
    raise RuntimeError(
        f"ERROR: CME returned HTTP {response.status_code}. "
        "See response body above."
    )

print("SUCCESS: CME page is accessible.")
