#!/usr/bin/env python3
import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from curl_cffi import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "gold_xauusd.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

PAIR_ID = "68"  # Investing.com XAU/USD Gold Spot / US Dollar; not Gold Futures.
CANONICAL_URL = "https://www.investing.com/currencies/xau-usd"
CANONICAL_HISTORICAL_URL = "https://www.investing.com/currencies/xau-usd-historical-data"
REGIONAL_HOSTS = ["https://uk.investing.com", "https://au.investing.com", "https://ca.investing.com"]
TZ_BJT = ZoneInfo("Asia/Shanghai")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def clean_number(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.\-]", "", str(v).replace(",", "").strip())
    if not s or s in {"-", "."}:
        return None
    try:
        return float(s)
    except Exception:
        return None


def parse_date(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{9,13}(?:\.0+)?", s):
        ts = float(s)
        if ts > 10_000_000_000:
            ts /= 1000
        return datetime.utcfromtimestamp(ts).date().isoformat()
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    m = re.search(r"(?:19|20)\d{2}-\d{2}-\d{2}", s)
    return m.group(0) if m else None


def extract_html_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table#curr_table") or soup.select_one("table.historicalTbl")
    if not table:
        return []
    out = []
    for tr in table.select("tbody tr"):
        td = tr.find_all("td")
        if len(td) < 2:
            continue
        d = parse_date(td[0].get_text(" ", strip=True))
        price = clean_number(td[1].get("data-real-value") or td[1].get_text(" ", strip=True))
        if not d or price is None:
            continue
        rec = {"date": d, "value": round(price, 2)}
        if len(td) >= 5:
            for idx, key in ((2, "open"), (3, "high"), (4, "low")):
                n = clean_number(td[idx].get("data-real-value") or td[idx].get_text(" ", strip=True))
                if n is not None:
                    rec[key] = round(n, 2)
        out.append(rec)
    return out


class InvestingFetcher:
    def __init__(self):
        self.sessions = {host: requests.Session(impersonate="chrome") for host in REGIONAL_HOSTS}

    @staticmethod
    def headers(host):
        hist = host + "/currencies/xau-usd-historical-data"
        return {
            "user-agent": UA,
            "accept-language": "en-GB,en;q=0.9",
            "accept": "text/html, */*; q=0.01",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
            "origin": host,
            "referer": hist,
        }

    def fetch_from_host(self, host, start, end):
        payload = {
            "curr_id": PAIR_ID,
            "smlID": "12345678",
            "header": "XAU/USD Historical Data",
            "st_date": start.strftime("%m/%d/%Y"),
            "end_date": end.strftime("%m/%d/%Y"),
            "interval_sec": "Daily",
            "sort_col": "date",
            "sort_ord": "DESC",
            "action": "historical_data",
        }
        resp = self.sessions[host].post(
            host + "/instruments/HistoricalDataAjax",
            headers=self.headers(host),
            data=payload,
            timeout=75,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return extract_html_rows(resp.text)

    def fetch_period(self, start, end):
        errors = []
        for host in REGIONAL_HOSTS:
            try:
                rows = self.fetch_from_host(host, start, end)
                print(f"Investing.com {host}: {len(rows)} rows for {start} -> {end}")
                return rows, host
            except Exception as exc:
                errors.append(f"{host}: {exc}")
                time.sleep(0.6)
        raise RuntimeError(" | ".join(errors))


FETCHER = InvestingFetcher()


def fetch_full_history():
    # Investing.com's endpoint caps a response at roughly 2.5 years. Use overlapping
    # two-year requests and let the date-key merge deduplicate them.
    cur = date(1970, 1, 1)
    today = date.today()
    rows = []
    used_hosts = set()
    while cur <= today:
        end = min(date(cur.year + 1, 12, 31), today)
        try:
            chunk, host = FETCHER.fetch_period(cur, end)
            rows.extend(chunk)
            used_hosts.add(host)
        except Exception as exc:
            print(f"Chunk {cur}->{end} failed: {exc}", file=sys.stderr)
        cur = date(cur.year + 2, 1, 1)
        time.sleep(0.35)
    return rows, sorted(used_hosts)


def fetch_latest_window():
    return FETCHER.fetch_period(date.today() - timedelta(days=450), date.today())


def fetch_page_quote():
    for host in REGIONAL_HOSTS:
        try:
            resp = requests.get(
                host + "/currencies/xau-usd",
                headers={"user-agent": UA, "accept-language": "en-GB,en;q=0.9"},
                impersonate="chrome",
                timeout=20,
            )
            if resp.status_code != 200:
                continue
            node = BeautifulSoup(resp.text, "html.parser").select_one('[data-test="instrument-price-last"]')
            if node:
                value = clean_number(node.get_text(" ", strip=True))
                if value and value > 100:
                    return value, host
        except Exception:
            pass
    return None, None


def load_existing():
    if not OUT.exists():
        return {}
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    existing = load_existing()
    old_rows = existing.get("data", []) if isinstance(existing, dict) else []
    used_hosts = set(existing.get("retrieval_hosts", [])) if isinstance(existing, dict) else set()

    if args.full or not old_rows:
        fetched, hosts = fetch_full_history()
        used_hosts.update(hosts)
    else:
        fetched, host = fetch_latest_window()
        if host:
            used_hosts.add(host)

    by_date = {}
    for row in old_rows + fetched:
        if isinstance(row, dict) and row.get("date") and row.get("value") is not None:
            by_date[row["date"]] = row
    merged = sorted(by_date.values(), key=lambda x: x["date"])
    if not merged:
        raise RuntimeError("No XAU/USD history available after merge")

    live, live_host = fetch_page_quote()
    if live_host:
        used_hosts.add(live_host)
    latest_daily = merged[-1]
    now_bjt = datetime.now(TZ_BJT)
    latest_price = round(live if live is not None else float(latest_daily["value"]), 2)

    payload = {
        "name": "Gold Spot / US Dollar",
        "ticker": "XAU/USD",
        "unit": "USD/oz",
        "instrument_id": PAIR_ID,
        "source": "Investing.com",
        "source_url": CANONICAL_URL,
        "historical_source_url": CANONICAL_HISTORICAL_URL,
        "retrieval_hosts": sorted(used_hosts),
        "frequency": "Daily historical series; updater checks every 30 minutes Monday-Saturday",
        "price_field": "XAU/USD spot price",
        "status": "LIVE" if live is not None else "DAILY_CLOSE",
        "history_scope": "Maximum Investing.com XAU/USD daily history discovered by two-year overlapping pagination; no synthetic weekend/interpolated rows",
        "history_start": merged[0]["date"],
        "history_end": merged[-1]["date"],
        "observation_count": len(merged),
        "latest_quote": {
            "price": latest_price,
            "timestamp": now_bjt.isoformat(timespec="seconds"),
            "quote_status": "INVESTING_PAGE" if live is not None else "LATEST_DAILY_CLOSE",
            "observation_date": latest_daily["date"],
        },
        "data": merged,
        "updated_at_bjt": now_bjt.isoformat(timespec="seconds"),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Gold updated: {payload['history_start']} -> {payload['history_end']} ({len(merged)} rows), latest={latest_price}")


if __name__ == "__main__":
    main()
