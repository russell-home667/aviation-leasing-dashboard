#!/usr/bin/env python3
import argparse
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
INVESTING_URL = "https://www.investing.com/currencies/xau-usd"
INVESTING_HISTORICAL_URL = "https://www.investing.com/currencies/xau-usd-historical-data"
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
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %y"):
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


def fetch_yahoo_history(full=False):
    ticker = yf.Ticker(YAHOO_TICKER)
    period = "max" if full else "2y"
    hist = ticker.history(period=period, interval="1d", auto_adjust=False)
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


def fetch_yahoo_quote():
    ticker = yf.Ticker(YAHOO_TICKER)
    try:
        intraday = ticker.history(period="5d", interval="1m", auto_adjust=False, prepost=True)
        if intraday is not None and not intraday.empty:
            last = intraday.iloc[-1]
            price = clean_number(last.get("Close"))
            if price and price > 100:
                ts = intraday.index[-1]
                try:
                    ts_bjt = ts.tz_convert(TZ_BJT)
                except Exception:
                    ts_bjt = datetime.now(TZ_BJT)
                return round(price, 2), ts_bjt.isoformat(timespec="seconds"), "YAHOO_1M"
    except Exception as exc:
        print(f"Yahoo 1m quote warning: {exc}")
    return None, None, None


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
            resp = requests.get(
                host + "/currencies/xau-usd",
                headers={"user-agent": UA, "accept-language": "en-GB,en;q=0.9"},
                impersonate="chrome",
                timeout=15,
            )
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

    # Prefer the same XAU/USD spot definition used by the AI-bubble monitor.
    # Investing.com is attempted for recent observations, but GitHub-hosted IPs can be blocked.
    try:
        inv_rows, inv_host = fetch_investing_recent()
        fetched.extend(inv_rows)
        sources.append(f"Investing.com ({inv_host})")
        print(f"Investing recent rows: {len(inv_rows)}")
    except Exception as exc:
        print(f"Investing.com unavailable, using Yahoo XAU/USD fallback: {exc}")

    # Yahoo XAUUSD=X is the resilient same-instrument fallback and supplies long history.
    try:
        yh_rows = fetch_yahoo_history(full=(args.full or not old_rows))
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
        raise RuntimeError("No XAU/USD spot history available from Investing.com or Yahoo Finance")

    now_bjt = datetime.now(TZ_BJT)
    quote_price, quote_ts, quote_status = fetch_yahoo_quote()
    quote_source = "Yahoo Finance XAUUSD=X"
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
        "historical_source_url": INVESTING_HISTORICAL_URL,
        "fallback_ticker": YAHOO_TICKER,
        "frequency": "Daily historical series; updater checks every 30 minutes Monday-Saturday",
        "price_field": "XAU/USD spot price",
        "status": "LIVE" if quote_status in {"INVESTING_PAGE", "YAHOO_1M"} else "DAILY_CLOSE",
        "history_scope": "XAU/USD spot daily history; no futures substitution and no synthetic weekend/interpolated rows",
        "history_start": merged[0]["date"],
        "history_end": merged[-1]["date"],
        "observation_count": len(merged),
        "latest_quote": {
            "price": round(float(quote_price), 2),
            "timestamp": quote_ts,
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
