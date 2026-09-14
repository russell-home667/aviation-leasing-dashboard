#!/usr/bin/env python3
import argparse
import csv
import io
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf
from bs4 import BeautifulSoup
from curl_cffi import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "gold_xauusd.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

PAIR_ID = "68"
YAHOO_TICKER = "XAUUSD=X"
STOOQ_SYMBOL = "xauusd"
INVESTING_URL = "https://www.investing.com/currencies/xau-usd"
INVESTING_HISTORICAL_URL = "https://www.investing.com/currencies/xau-usd-historical-data"
STOOQ_URL = "https://stooq.com/q/?s=xauusd"
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
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    m = re.search(r"(?:19|20)\d{2}-\d{2}-\d{2}", s)
    return m.group(0) if m else None


def extract_investing_rows(html):
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


def fetch_stooq_history(full=False):
    d1 = "19700101" if full else (date.today() - timedelta(days=800)).strftime("%Y%m%d")
    d2 = date.today().strftime("%Y%m%d")
    urls = [
        f"https://stooq.com/q/d/l/?s={STOOQ_SYMBOL}&d1={d1}&d2={d2}&i=d",
        f"https://stooq.com/q/d/l/?s={STOOQ_SYMBOL}&i=d",
    ]
    errors = []
    for url in urls:
        try:
            resp = requests.get(url, headers={"user-agent": UA, "accept": "text/csv,*/*"}, impersonate="chrome", timeout=45)
            text = resp.text.strip()
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            if not text.lower().startswith("date,"):
                raise RuntimeError(f"unexpected body: {text[:120]!r}")
            rows = []
            for item in csv.DictReader(io.StringIO(text)):
                d = parse_date(item.get("Date"))
                close = clean_number(item.get("Close"))
                if not d or close is None or close < 100:
                    continue
                rec = {"date": d, "value": round(close, 2)}
                for col, key in (("Open", "open"), ("High", "high"), ("Low", "low")):
                    val = clean_number(item.get(col))
                    if val is not None:
                        rec[key] = round(val, 2)
                rows.append(rec)
            if len(rows) >= 20:
                print(f"Stooq XAUUSD history rows: {len(rows)}")
                return rows
            raise RuntimeError(f"only {len(rows)} valid rows")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError(" | ".join(errors))


def fetch_stooq_quote():
    url = f"https://stooq.com/q/l/?s={STOOQ_SYMBOL}&f=sd2t2ohlcv&h&e=csv"
    try:
        resp = requests.get(url, headers={"user-agent": UA, "accept": "text/csv,*/*"}, impersonate="chrome", timeout=20)
        text = resp.text.strip()
        if resp.status_code != 200 or not text.lower().startswith("symbol,"):
            raise RuntimeError(f"HTTP {resp.status_code}; body={text[:120]!r}")
        items = list(csv.DictReader(io.StringIO(text)))
        if not items:
            raise RuntimeError("empty quote CSV")
        item = items[0]
        price = clean_number(item.get("Close"))
        d = parse_date(item.get("Date"))
        tm = (item.get("Time") or "").strip()
        if not price or price < 100:
            raise RuntimeError("invalid price")
        # Stooq exposes the provider timestamp; retrieval time is stored separately in updated_at_bjt.
        stamp = f"{d}T{tm}" if d and tm else datetime.now(TZ_BJT).isoformat(timespec="seconds")
        return round(price, 2), stamp, "STOOQ_QUOTE"
    except Exception as exc:
        print(f"Stooq quote warning: {exc}")
        return None, None, None


def fetch_yahoo_history(full=False):
    ticker = yf.Ticker(YAHOO_TICKER)
    hist = ticker.history(period="max" if full else "2y", interval="1d", auto_adjust=False)
    if hist is None or hist.empty:
        raise RuntimeError(f"Yahoo {YAHOO_TICKER} returned no daily history")
    rows = []
    for idx, row in hist.iterrows():
        close = clean_number(row.get("Close"))
        if close is None or close < 100:
            continue
        rec = {"date": idx.date().isoformat(), "value": round(close, 2)}
        for source_col, key in (("Open", "open"), ("High", "high"), ("Low", "low")):
            val = clean_number(row.get(source_col))
            if val is not None:
                rec[key] = round(val, 2)
        rows.append(rec)
    if len(rows) < 20:
        raise RuntimeError(f"Yahoo {YAHOO_TICKER} history unexpectedly short: {len(rows)}")
    return rows


def investing_headers(host):
    return {
        "user-agent": UA,
        "accept-language": "en-GB,en;q=0.9",
        "accept": "text/html, */*; q=0.01",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "x-requested-with": "XMLHttpRequest",
        "origin": host,
        "referer": host + "/currencies/xau-usd-historical-data",
    }


def fetch_investing_recent():
    start = date.today() - timedelta(days=450)
    end = date.today()
    errors = []
    for host in REGIONAL_HOSTS:
        try:
            session = requests.Session(impersonate="chrome")
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
            resp = session.post(host + "/instruments/HistoricalDataAjax", headers=investing_headers(host), data=payload, timeout=40)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            rows = extract_investing_rows(resp.text)
            if rows:
                return rows, host
        except Exception as exc:
            errors.append(f"{host}: {exc}")
            time.sleep(0.3)
    raise RuntimeError(" | ".join(errors))


def fetch_investing_page_quote():
    for host in REGIONAL_HOSTS:
        try:
            resp = requests.get(host + "/currencies/xau-usd", headers={"user-agent": UA}, impersonate="chrome", timeout=15)
            if resp.status_code != 200:
                continue
            node = BeautifulSoup(resp.text, "html.parser").select_one('[data-test="instrument-price-last"]')
            if node:
                price = clean_number(node.get_text(" ", strip=True))
                if price and price > 100:
                    return round(price, 2), host
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
    sources = []
    fetched = []
    want_full = args.full or not old_rows

    # 1) Same source intended by the AI-bubble monitor.
    try:
        inv_rows, inv_host = fetch_investing_recent()
        fetched.extend(inv_rows)
        sources.append(f"Investing.com ({inv_host})")
        print(f"Investing recent rows: {len(inv_rows)}")
    except Exception as exc:
        print(f"Investing.com unavailable: {exc}")

    # 2) Same XAU/USD spot instrument from Stooq; never substitute GC=F futures.
    try:
        stooq_rows = fetch_stooq_history(full=want_full)
        fetched.extend(stooq_rows)
        sources.append("Stooq XAUUSD")
    except Exception as exc:
        print(f"Stooq XAUUSD history warning: {exc}")

    # 3) Legacy Yahoo XAUUSD=X only if Yahoo still serves it.
    try:
        yh_rows = fetch_yahoo_history(full=want_full)
        fetched.extend(yh_rows)
        sources.append("Yahoo Finance XAUUSD=X")
        print(f"Yahoo XAU/USD rows: {len(yh_rows)}")
    except Exception as exc:
        print(f"Yahoo XAU/USD history warning: {exc}")

    by_date = {}
    for row in old_rows + fetched:
        if isinstance(row, dict) and row.get("date") and row.get("value") is not None:
            by_date[row["date"]] = row
    merged = sorted(by_date.values(), key=lambda x: x["date"])
    if not merged:
        raise RuntimeError("No XAU/USD spot history available from Investing.com, Stooq, or Yahoo Finance")

    now_bjt = datetime.now(TZ_BJT)
    quote_price, quote_ts, quote_status = fetch_stooq_quote()
    quote_source = "Stooq XAUUSD"
    inv_live, inv_host = fetch_investing_page_quote()
    if inv_live is not None:
        quote_price = inv_live
        quote_ts = now_bjt.isoformat(timespec="seconds")
        quote_status = "INVESTING_PAGE"
        quote_source = f"Investing.com ({inv_host})"
    if quote_price is None:
        quote_price = float(merged[-1]["value"])
        quote_ts = now_bjt.isoformat(timespec="seconds")
        quote_status = "LATEST_DAILY_CLOSE"
        quote_source = sources[-1] if sources else "stored history"

    payload = {
        "name": "Gold Spot / US Dollar",
        "ticker": "XAU/USD",
        "unit": "USD/oz",
        "instrument_id": PAIR_ID,
        "source": " + ".join(dict.fromkeys(sources)) if sources else "XAU/USD stored history",
        "source_url": INVESTING_URL,
        "secondary_source_url": STOOQ_URL,
        "historical_source_url": INVESTING_HISTORICAL_URL,
        "frequency": "Daily historical series; updater checks every 30 minutes Monday-Saturday",
        "price_field": "XAU/USD spot price",
        "status": "LIVE" if quote_status in {"INVESTING_PAGE", "STOOQ_QUOTE"} else "DAILY_CLOSE",
        "history_scope": "XAU/USD spot daily history; no Gold Futures substitution and no synthetic weekend/interpolated rows",
        "history_start": merged[0]["date"],
        "history_end": merged[-1]["date"],
        "observation_count": len(merged),
        "latest_quote": {
            "price": round(float(quote_price), 2),
            "timestamp": quote_ts,
            "retrieved_at_bjt": now_bjt.isoformat(timespec="seconds"),
            "quote_status": quote_status,
            "source": quote_source,
            "observation_date": merged[-1]["date"],
        },
        "data": merged,
        "updated_at_bjt": now_bjt.isoformat(timespec="seconds"),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Gold updated: {payload['history_start']} -> {payload['history_end']} ({len(merged)} rows), latest={payload['latest_quote']['price']} via {quote_status}")


if __name__ == "__main__":
    main()
