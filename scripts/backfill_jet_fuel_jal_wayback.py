import calendar
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "market_data.json"

WAYBACK_PREFIX = "https://web.archive.org/web/20251231000000id_/"
JAL_BASE = "https://press.jal.co.jp"
GAP_START = date(2010, 7, 28)
GAP_END = date(2026, 1, 25)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; aviation-leasing-dashboard/1.0; personal research)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

MONTH_MAP = {
    **{name.lower(): i for i, name in enumerate(calendar.month_name) if name},
    **{name.lower(): i for i, name in enumerate(calendar.month_abbr) if name},
}
MONTH_RE = "(?:" + "|".join(sorted((re.escape(x) for x in MONTH_MAP), key=len, reverse=True)) + ")"

# These archive pages span the years needed for the gap. Cargo pages recover
# one-month averages; fare pages add two-month period averages when no monthly
# cargo disclosure is available.
INDEX_URLS = []
INDEX_URLS += [f"{JAL_BASE}/en/cargo/index_{n}.html" for n in range(4, 13)]
INDEX_URLS += [f"{JAL_BASE}/en/fares/index_{n}.html" for n in range(7, 15)]


def month_end(year: int, month: int) -> str:
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def clean_text(html: str) -> str:
    return " ".join(
        BeautifulSoup(html, "html.parser")
        .get_text(" ", strip=True)
        .replace("\xa0", " ")
        .split()
    )


def month_num(name: str):
    return MONTH_MAP.get(name.strip().lower().rstrip("."))


def unwrap_wayback_href(href: str, base_original: str):
    if not href:
        return None
    # Absolute Wayback-rewritten link:
    # /web/TIMESTAMPid_/https://press.jal.co.jp/en/release/...
    m = re.search(r"/web/[^/]+/(https?://.+)$", href)
    if m:
        href = m.group(1)
    if href.startswith("//"):
        href = "https:" + href
    full = urljoin(base_original, href)
    parts = urlsplit(full)
    if parts.netloc not in {"press.jal.co.jp", "www.jal.com", "jal.com"}:
        return None
    # The historical articles we need have /en/release/YYYYMM/....html URLs.
    if "/en/release/" not in parts.path or not parts.path.lower().endswith(".html"):
        return None
    # Canonicalize to press.jal.co.jp, which is what Wayback generally stored.
    return "https://press.jal.co.jp" + parts.path


def wayback_url(original: str):
    return WAYBACK_PREFIX + original


def collect_release_links(session: requests.Session):
    candidates = {}
    for original in INDEX_URLS:
        url = wayback_url(original)
        try:
            r = session.get(url, timeout=30, allow_redirects=True)
            print(f"JAL archive index {original}: HTTP {r.status_code}, {len(r.content)} bytes")
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            found = 0
            for a in soup.find_all("a", href=True):
                title = " ".join(a.get_text(" ", strip=True).split())
                title_l = title.lower()
                if "fuel surcharge" not in title_l:
                    continue
                release = unwrap_wayback_href(a.get("href", ""), original)
                if not release:
                    continue
                m = re.search(r"/en/release/(20\d{2})\d{2}/", release)
                if not m:
                    continue
                year = int(m.group(1))
                if 2010 <= year <= 2026:
                    candidates[release] = title
                    found += 1
            print(f"  fuel-surcharge release links: {found}")
        except Exception as exc:
            print(f"  index warning: {exc}")
    print(f"Unique archived JAL release candidates: {len(candidates)}")
    return candidates


MONTHLY_PATTERNS = [
    re.compile(
        rf"average fuel price of Singapore kerosene(?:-type jet fuel)?\s+"
        rf"(?:for )?(?:the )?month of\s+({MONTH_RE})(?:\s+(?:in\s+)?(20\d{{2}}))?\s+"
        rf"was\s+(?:US\$|USD\s*\$?|USD)?\s*([0-9]+(?:\.[0-9]+)?)\s+per barrel",
        re.I,
    ),
    re.compile(
        rf"average (?:fuel )?price of Singapore kerosene(?:-type jet fuel)?\s+"
        rf"(?:for )?(?:the )?month of\s+({MONTH_RE})(?:\s+(?:in\s+)?(20\d{{2}}))?"
        rf"(?:[^.]){{0,80}}?(?:US\$|USD\s*\$?)\s*([0-9]+(?:\.[0-9]+)?)",
        re.I,
    ),
]

# Passenger surcharge releases often publish an exact two-month Singapore
# kerosene average. Keep it as a two-month observation rather than inventing
# two monthly values.
TWO_MONTH_PATTERNS = [
    re.compile(
        rf"(?:two-month|2-month) average (?:fuel )?price of Singapore kerosene(?:-type jet fuel)?"
        rf"(?:[^.]){{0,120}}?({MONTH_RE})(?:\s+(20\d{{2}}))?\s+(?:and|to|-)\s+"
        rf"({MONTH_RE})(?:\s+(20\d{{2}}))?(?:[^.]){{0,100}}?"
        rf"(?:US\$|USD\s*\$?)\s*([0-9]+(?:\.[0-9]+)?)",
        re.I,
    ),
    re.compile(
        rf"price of Singapore kerosene(?:-type jet fuel)?\s+during the two-month period of\s+"
        rf"({MONTH_RE})(?:\s+(20\d{{2}}))?\s+and\s+({MONTH_RE})(?:\s+(20\d{{2}}))?\s+"
        rf"averaged\s+(?:US\$|USD\s*\$?)\s*([0-9]+(?:\.[0-9]+)?)",
        re.I,
    ),
]


def release_date_from_text(text: str, url: str):
    # Page text usually contains a date like "Sep 08,2015".
    for fmt_re, fmt in [
        (r"\b([A-Za-z]{3})\s+(\d{1,2}),\s*(20\d{2})\b", "%b %d %Y"),
        (r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(20\d{2})\b", "%B %d %Y"),
    ]:
        m = re.search(fmt_re, text)
        if m:
            try:
                return datetime.strptime(" ".join(m.groups()), fmt).date()
            except ValueError:
                pass
    m = re.search(r"/en/release/(20\d{2})(\d{2})/", url)
    if m:
        return date(int(m.group(1)), int(m.group(2)), 15)
    return None


def infer_year(month: int, article_date: date | None):
    if article_date is None:
        return None
    # The disclosed benchmark period normally precedes the article by 1-3 months.
    return article_date.year - 1 if month > article_date.month else article_date.year


def parse_release(html: str, original_url: str):
    text = clean_text(html)
    text_l = text.lower()
    if "singapore kerosene" not in text_l:
        return []
    article_date = release_date_from_text(text, original_url)
    rows = []

    for pattern in MONTHLY_PATTERNS:
        for m in pattern.finditer(text):
            month = month_num(m.group(1))
            explicit_year = int(m.group(2)) if m.group(2) else None
            year = explicit_year or infer_year(month, article_date)
            value = float(m.group(3))
            if not year or not month or not (15 <= value <= 350):
                continue
            end = date.fromisoformat(month_end(year, month))
            if GAP_START <= end <= GAP_END:
                rows.append({
                    "date": end.isoformat(),
                    "value": round(value, 2),
                    "observation_type": "monthly_average",
                    "period_start": date(year, month, 1).isoformat(),
                    "period_end": end.isoformat(),
                    "source": "Japan Airlines official Singapore kerosene monthly average (Wayback copy)",
                    "source_url": original_url,
                })

    for pattern in TWO_MONTH_PATTERNS:
        for m in pattern.finditer(text):
            m1 = month_num(m.group(1))
            y1 = int(m.group(2)) if m.group(2) else None
            m2 = month_num(m.group(3))
            y2 = int(m.group(4)) if m.group(4) else None
            value = float(m.group(5))
            if not m1 or not m2 or not (15 <= value <= 350):
                continue
            if y1 is None and y2 is None:
                y2 = infer_year(m2, article_date)
                if y2 is None:
                    continue
                y1 = y2 - 1 if m1 > m2 else y2
            elif y1 is None:
                y1 = y2 - 1 if m1 > m2 else y2
            elif y2 is None:
                y2 = y1 + 1 if m2 < m1 else y1
            start = date(y1, m1, 1)
            end = date.fromisoformat(month_end(y2, m2))
            if GAP_START <= end <= GAP_END:
                rows.append({
                    "date": end.isoformat(),
                    "value": round(value, 2),
                    "observation_type": "two_month_average",
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "source": "Japan Airlines official Singapore kerosene two-month average (Wayback copy)",
                    "source_url": original_url,
                })

    # Within one release, prefer a monthly value if the same end date appears twice.
    priority = {"two_month_average": 1, "monthly_average": 2}
    by_date = {}
    for row in rows:
        old = by_date.get(row["date"])
        if old is None or priority[row["observation_type"]] > priority[old["observation_type"]]:
            by_date[row["date"]] = row
    return list(by_date.values())


def crawl():
    session = requests.Session()
    session.headers.update(HEADERS)
    links = collect_release_links(session)
    rows = []
    parsed_pages = 0
    for i, (original, title) in enumerate(sorted(links.items()), start=1):
        try:
            r = session.get(wayback_url(original), timeout=30, allow_redirects=True)
            if r.status_code != 200:
                print(f"[{i}/{len(links)}] HTTP {r.status_code}: {original}")
                continue
            parsed = parse_release(r.text, original)
            if parsed:
                parsed_pages += 1
                rows.extend(parsed)
                print(f"[{i}/{len(links)}] parsed {len(parsed)}: {title}")
        except Exception as exc:
            print(f"[{i}/{len(links)}] warning {original}: {exc}")

    priority = {"two_month_average": 1, "monthly_average": 2}
    by_date = {}
    conflicts = []
    for row in rows:
        old = by_date.get(row["date"])
        if old is None:
            by_date[row["date"]] = row
            continue
        if old["observation_type"] == row["observation_type"] and abs(old["value"] - row["value"]) > 0.03:
            conflicts.append((row["date"], old["value"], row["value"]))
            continue
        if priority[row["observation_type"]] > priority[old["observation_type"]]:
            by_date[row["date"]] = row

    for item in conflicts[:20]:
        print(f"WARNING conflicting JAL value {item[0]}: {item[1]} vs {item[2]}")
    print(f"Archived JAL pages with extracted prices: {parsed_pages}")
    return [by_date[d] for d in sorted(by_date)]


with open(DATA_FILE, "r", encoding="utf-8") as f:
    market = json.load(f)

rows = crawl()
if len(rows) < 30:
    raise RuntimeError(f"Wayback JAL backfill unexpectedly short: {len(rows)} observations")

jet = market.setdefault("jet_fuel", {})
existing = {
    row["date"]: {"date": row["date"], "value": float(row["value"])}
    for row in jet.get("data", [])
    if row.get("date") and row.get("value") is not None
}

added = []
# Prefer exact monthly rows over period-average rows when adding new dates.
rows_sorted = sorted(rows, key=lambda r: (r["date"], 0 if r["observation_type"] == "monthly_average" else 1))
for row in rows_sorted:
    if row["date"] not in existing:
        existing[row["date"]] = {"date": row["date"], "value": row["value"]}
        added.append(row)

jet["data"] = [existing[d] for d in sorted(existing)]
monthly = [r for r in rows if r["observation_type"] == "monthly_average"]
two_month = [r for r in rows if r["observation_type"] == "two_month_average"]
jet["jal_wayback_history_backfill"] = {
    "source": "Japan Airlines official fuel-surcharge releases recovered through Internet Archive",
    "method": "Extract Singapore kerosene monthly averages and, where useful, explicitly published two-month averages from archived official JAL releases",
    "target_gap": f"{GAP_START.isoformat()} to {GAP_END.isoformat()}",
    "from": rows[0]["period_start"],
    "to": rows[-1]["period_end"],
    "parsed_observations": len(rows),
    "monthly_average_observations": len(monthly),
    "two_month_average_observations": len(two_month),
    "new_gap_fill_points_added": len(added),
    "caveat": "Period averages are retained as period averages and are not expanded or interpolated into synthetic daily values. Existing higher-frequency observations are never overwritten.",
    "observations": rows,
}
jet["source"] = "Public Singapore jet archive + U.S. EIA legacy series + ANA/JAL official period averages"
jet["note"] = (
    "Long history contains frequency/source breaks. Daily EIA/current data are preserved. "
    "The 2010-2026 gap is filled where possible with official ANA and JAL Singapore kerosene period averages; no synthetic interpolation is used."
)
market["updated_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")

with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(market, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("Wayback JAL Singapore kerosene backfill complete.")
print(f"Recovered observations: {len(rows)}")
print(f"  Monthly averages: {len(monthly)}")
print(f"  Two-month averages: {len(two_month)}")
print(f"Coverage: {rows[0]['period_start']} -> {rows[-1]['period_end']}")
print(f"New gap-fill points inserted: {len(added)}")
print(f"Total jet_fuel.data rows now: {len(jet['data'])}")
