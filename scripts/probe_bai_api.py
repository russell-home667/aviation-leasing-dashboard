import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://dashboard.tacindex.com/",
    "Origin": "https://dashboard.tacindex.com",
}

session = requests.Session()
session.headers.update(HEADERS)

URLS = [
    "https://api.tacindex.com/bai/",
    "https://api.tacindex.com/route/?filter=ticker",
    "https://api.tacindex.com/route/BAI00/?currency=USD&index=BAI",
    "https://api.tacindex.com/freight/chart/BAI00/?index=BAI",
    "https://api.tacindex.com/freight/chart/BAI00/?index=BAI&currency=USD",
]

for url in URLS:
    try:
        r = session.get(url, timeout=30)
        print("=" * 100)
        print(f"GET {url}")
        print("STATUS", r.status_code)
        print("CONTENT-TYPE", r.headers.get("content-type"))
        print("LENGTH", len(r.text))
        print(r.text[:20000])
    except Exception as exc:
        print("=" * 100)
        print(f"ERROR {url}: {exc}")
