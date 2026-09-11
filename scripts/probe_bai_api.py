import re
import requests
from urllib.parse import urljoin

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
}

START_URLS = [
    "https://dashboard.tacindex.com/",
    "https://dashboard.tacindex.com/dashboard",
    "https://dashboard.tacindex.com/detail?route=BAI00&routeType=basket",
]

session = requests.Session()
session.headers.update(HEADERS)

all_js = set()

for url in START_URLS:
    try:
        r = session.get(url, timeout=30)
        print(f"PAGE {url} -> {r.status_code} {len(r.text)} bytes")
        print(r.text[:500].replace("\n", " "))
        for src in re.findall(r'<script[^>]+src=["\']([^"\']+)', r.text, flags=re.I):
            all_js.add(urljoin(url, src))
    except Exception as exc:
        print(f"PAGE ERROR {url}: {exc}")

print("JS FILES", len(all_js))
for js_url in sorted(all_js):
    print("JS", js_url)

api_strings = set()
path_strings = set()
keywords = ["histor", "BAI00", "routeType", "dashboard-api", "series", "chart", "basket"]

for js_url in sorted(all_js):
    try:
        r = session.get(js_url, timeout=30)
        print(f"FETCH JS {js_url} -> {r.status_code} {len(r.text)} bytes")
        text = r.text
        for match in re.findall(r'https?://[^"\'`\\ ]+', text):
            if "tacindex" in match.lower() or "/api/" in match.lower():
                api_strings.add(match[:300])
        for match in re.findall(r'["\'`](/api/[^"\'`]+)', text):
            path_strings.add(match[:300])
        low = text.lower()
        for kw in keywords:
            pos = low.find(kw.lower())
            if pos >= 0:
                snippet = text[max(0, pos-250):pos+500].replace("\n", " ")
                print(f"SNIPPET {kw} in {js_url}: {snippet}")
    except Exception as exc:
        print(f"JS ERROR {js_url}: {exc}")

print("FULL API STRINGS")
for item in sorted(api_strings):
    print(item)

print("API PATH STRINGS")
for item in sorted(path_strings):
    print(item)

for endpoint in [
    "https://dashboard-api.tacindex.com/api/origins",
    "https://dashboard-api.tacindex.com/api/destinations",
]:
    try:
        r = session.get(endpoint, timeout=30, headers={**HEADERS, "Origin": "https://dashboard.tacindex.com", "Referer": "https://dashboard.tacindex.com/"})
        print(f"API {endpoint} -> {r.status_code} {len(r.text)} bytes")
        print(r.text[:2000])
    except Exception as exc:
        print(f"API ERROR {endpoint}: {exc}")
