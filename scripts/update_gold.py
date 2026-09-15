#!/usr/bin/env python3
import argparse
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "gold_xauusd.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

TZ_BJT = ZoneInfo("Asia/Shanghai")
YAHOO_DAILY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD%3DX?range=1y&interval=1d&events=history"
YAHOO_GOLD_PAGE = "https://finance.yahoo.com/quote/XAUUSD%3DX/history/"
GOLDPRICE_BARS_BASE = "https://api.goldprice.dev/v1/bars"
GOLDPRICE_DOCS = "https://goldprice.dev/docs/historical"
XAUS_SPOT_URL = "https://xaus.com/api/v1/spot?compact=1"
GOLD_API_SPOT_URL = "https://api.gold-api.com/price/XAU"

# Public mirror maintained from the World Bank Commodity Markets (Pink Sheet).
WORLD_BANK_GOLD_CSV = "https://raw.githubusercontent.com/datasets/gold-prices/main/data/monthly.csv"
WORLD_BANK_SOURCE_URL = "https://www.worldbank.org/en/research/commodity-markets"
WORLD_BANK_MIRROR_URL = "https://github.com/datasets/gold-prices"
WORLD_BANK_START_MONTH = "1990-01"
WORLD_BANK_END_MONTH = "2021-09"


def request_json(url: str, timeout: int = 30):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; aviation-leasing-dashboard/1.0; personal research)",
        "Accept": "application/json",
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def request_text(url: str, timeout: int = 45):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; aviation-leasing-dashboard/1.0; personal research)",
        "Accept": "text/csv,text/plain,*/*",
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


def load_existing():
    if not OUT.exists():
        return {}
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fetch_world_bank_monthly():
    text = request_text(WORLD_BANK_GOLD_CSV, 60)
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for rec in reader:
        ym = str(rec.get("Date") or "").strip()
        raw = rec.get("Price")
        if not ym or raw is None or ym < WORLD_BANK_START_MONTH or ym > WORLD_BANK_END_MONTH:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value <= 100:
            continue
        rows.append({
            "date": f"{ym}-01",
            "period": ym,
            "value": round(value, 2),
            "frequency": "monthly_average",
            "source": "World Bank Commodity Markets (Pink Sheet)",
        })
    rows.sort(key=lambda x: x["date"])
    expected_min = (2021 - 1990) * 12 + 9
    if len(rows) < expected_min:
        raise RuntimeError(f"World Bank gold history unexpectedly short: {len(rows)} rows")
    if rows[0]["date"] != "1990-01-01":
        raise RuntimeError(f"World Bank gold history starts at {rows[0]['date']}, expected 1990-01-01")
    return rows


def validate_daily_rows(rows, provider: str, min_rows: int):
    if len(rows) < min_rows:
        raise RuntimeError(f"{provider} daily history unexpectedly short: {len(rows)} rows")
    rows.sort(key=lambda x: x["date"])
    latest = datetime.fromisoformat(rows[-1]["date"]).date()
    if latest < (datetime.now(timezone.utc).date() - timedelta(days=10)):
        raise RuntimeError(f"{provider} latest daily bar is stale: {latest}")
    prev = None
    for row in rows:
        value = float(row["value"])
        if not 100 < value < 20000:
            raise RuntimeError(f"{provider} implausible gold value on {row['date']}: {value}")
        if prev is not None and abs(value / prev - 1) > 0.25:
            raise RuntimeError(f"{provider} implausible daily jump near {row['date']}: {prev} -> {value}")
        prev = value
    return rows


def fetch_yahoo_daily():
    payload = request_json(YAHOO_DAILY_URL, 35)
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"Yahoo chart error: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError("Yahoo chart returned no result for XAUUSD=X")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote_sets = ((result.get("indicators") or {}).get("quote") or [])
    if not quote_sets:
        raise RuntimeError("Yahoo chart returned no quote array")
    q = quote_sets[0]
    opens, highs, lows, closes = q.get("open") or [], q.get("high") or [], q.get("low") or [], q.get("close") or []
    today_utc = datetime.now(timezone.utc).date()
    rows = []
    for i, ts in enumerate(timestamps):
        if i >= len(closes) or closes[i] is None:
            continue
        d = datetime.fromtimestamp(int(ts), timezone.utc).date()
        if d >= today_utc:
            continue
        try:
            close = float(closes[i])
        except (TypeError, ValueError):
            continue
        if close <= 100:
            continue
        row = {
            "date": d.isoformat(),
            "value": round(close, 2),
            "frequency": "daily",
            "source": "Yahoo Finance XAUUSD=X",
        }
        for name, arr in (("open", opens), ("high", highs), ("low", lows)):
            if i < len(arr) and arr[i] is not None:
                try:
                    row[name] = round(float(arr[i]), 2)
                except (TypeError, ValueError):
                    pass
        rows.append(row)
    return validate_daily_rows(rows, "Yahoo Finance XAUUSD=X", 100)


def fetch_goldprice_daily_fallback():
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=29)
    url = (
        f"{GOLDPRICE_BARS_BASE}?symbol=XAU-USD-SPOT&interval=1d"
        f"&from={start.isoformat()}&to={today.isoformat()}&limit=100"
    )
    payload = request_json(url, 35)
    bars = payload.get("bars") or []
    rows = []
    for bar in bars:
        if bar.get("is_closed") is False:
            continue
        d = str(bar.get("bar_start") or "")[:10]
        close = bar.get("close")
        if not d or close is None or d >= today.isoformat():
            continue
        try:
            close = float(close)
        except (TypeError, ValueError):
            continue
        if close <= 100:
            continue
        row = {
            "date": d,
            "value": round(close, 2),
            "frequency": "daily",
            "source": "goldprice.dev XAU-USD-SPOT",
        }
        for source_key, target_key in (("open", "open"), ("high", "high"), ("low", "low")):
            if bar.get(source_key) is not None:
                try:
                    row[target_key] = round(float(bar[source_key]), 2)
                except (TypeError, ValueError):
                    pass
        rows.append(row)
    return validate_daily_rows(rows, "goldprice.dev XAU-USD-SPOT", 10)


def fetch_daily_history():
    errors = []
    try:
        rows = fetch_yahoo_daily()
        return rows, "Yahoo Finance XAUUSD=X", False, None
    except Exception as exc:
        errors.append(f"Yahoo Finance: {exc}")
    try:
        rows = fetch_goldprice_daily_fallback()
        return rows, "goldprice.dev XAU-USD-SPOT", True, " | ".join(errors)
    except Exception as exc:
        errors.append(f"goldprice.dev: {exc}")
    raise RuntimeError("No reliable XAU/USD daily history source available: " + " | ".join(errors))


def fetch_live_quote():
    errors = []
    try:
        payload = request_json(XAUS_SPOT_URL, 25)
        price = payload.get("spot_usd_oz")
        if price is None:
            price = (payload.get("xau") or {}).get("price")
        price = float(price)
        state = payload.get("data_state") or {}
        status = str(state.get("status") or "fresh").lower()
        if price > 100 and status != "unavailable":
            timestamp = payload.get("updated_at") or state.get("as_of") or datetime.now(TZ_BJT).isoformat(timespec="seconds")
            return {
                "price": round(price, 2),
                "timestamp": timestamp,
                "quote_status": "XAUS_FRESH" if status == "fresh" else "XAUS_STALE",
                "source": "XAUS Gold Data API",
                "source_state": state,
            }
    except Exception as exc:
        errors.append(f"XAUS: {exc}")

    try:
        payload = request_json(GOLD_API_SPOT_URL, 25)
        price = float(payload.get("price"))
        if price > 100:
            timestamp = payload.get("updatedAt") or payload.get("updated_at") or datetime.now(TZ_BJT).isoformat(timespec="seconds")
            return {
                "price": round(price, 2),
                "timestamp": timestamp,
                "quote_status": "GOLD_API_REALTIME",
                "source": "gold-api.com",
                "source_state": {"status": "fresh"},
            }
    except Exception as exc:
        errors.append(f"gold-api.com: {exc}")
    raise RuntimeError("No XAU/USD spot quote available: " + " | ".join(errors))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Refresh World Bank long history as well as recent daily XAU/USD history")
    args = parser.parse_args()

    now_bjt = datetime.now(TZ_BJT)
    existing = load_existing()
    old_rows = existing.get("data", []) if isinstance(existing, dict) else []

    existing_dates = [str(x.get("date"))[:10] for x in old_rows if isinstance(x, dict) and x.get("date")]
    has_1990_history = bool(existing_dates) and min(existing_dates) <= "1990-01-01"

    world_bank_rows = []
    world_bank_error = None
    if args.full or not has_1990_history:
        try:
            world_bank_rows = fetch_world_bank_monthly()
            print(f"World Bank Pink Sheet monthly gold: {len(world_bank_rows)} rows, {world_bank_rows[0]['date']} -> {world_bank_rows[-1]['date']}")
        except Exception as exc:
            world_bank_error = str(exc)
            print(f"World Bank history warning: {exc}")

    try:
        quote = fetch_live_quote()
    except Exception as exc:
        if not existing.get("latest_quote"):
            raise
        quote = dict(existing["latest_quote"])
        quote["quote_status"] = "STALE_STORED_QUOTE"
        quote["fallback_reason"] = str(exc)
        print(f"Quote warning: {exc}; preserving prior stored quote")

    history_rows = []
    daily_source = "retained stored history"
    daily_fallback_used = False
    history_error = None
    try:
        history_rows, daily_source, daily_fallback_used, primary_warning = fetch_daily_history()
        history_error = primary_warning
        print(f"Daily gold history via {daily_source}: {len(history_rows)} rows, {history_rows[0]['date']} -> {history_rows[-1]['date']}")
        if quote.get("quote_status") != "STALE_STORED_QUOTE" and history_rows:
            live_price = float(quote["price"])
            last_close = float(history_rows[-1]["value"])
            divergence = abs(live_price / last_close - 1)
            if divergence > 0.12:
                raise RuntimeError(
                    f"daily close/live quote divergence {divergence:.1%} exceeds 12% sanity threshold "
                    f"({last_close} vs {live_price})"
                )
    except Exception as exc:
        history_error = (history_error + " | " if history_error else "") + str(exc)
        history_rows = []
        daily_source = "retained stored history"
        daily_fallback_used = False
        print(f"Daily history warning: {exc}; preserving stored daily history")

    by_date = {}
    for row in old_rows + world_bank_rows + history_rows:
        if not isinstance(row, dict) or not row.get("date") or row.get("value") is None:
            continue
        try:
            value = float(row["value"])
        except (TypeError, ValueError):
            continue
        if value <= 100:
            continue
        clean = dict(row)
        clean["date"] = str(row["date"])[:10]
        clean["value"] = round(value, 2)
        by_date[clean["date"]] = clean

    merged = [by_date[d] for d in sorted(by_date)]
    if not merged:
        raise RuntimeError("No XAU/USD history available")

    coverage_has_world_bank = merged[0]["date"] <= "1990-01-01"
    combined_source = "World Bank Pink Sheet + Yahoo Finance XAUUSD=X" if coverage_has_world_bank else "Yahoo Finance XAUUSD=X"

    payload = {
        "name": "Gold Spot / US Dollar",
        "ticker": "XAU/USD",
        "unit": "USD/oz",
        "instrument_type": "spot",
        "source": combined_source,
        "source_url": YAHOO_GOLD_PAGE,
        "historical_source": "World Bank Commodity Markets (Pink Sheet)",
        "historical_source_url": WORLD_BANK_SOURCE_URL,
        "historical_ingest": "datasets/gold-prices mirror of World Bank monthly Gold series",
        "historical_ingest_url": WORLD_BANK_MIRROR_URL,
        "daily_source": daily_source,
        "daily_primary_source": "Yahoo Finance XAUUSD=X",
        "daily_primary_url": YAHOO_GOLD_PAGE,
        "daily_fallback_source": "goldprice.dev XAU-USD-SPOT",
        "daily_fallback_url": GOLDPRICE_DOCS,
        "daily_fallback_used": daily_fallback_used,
        "live_primary_source": "XAUS Gold Data API",
        "live_primary_url": "https://xaus.com/api/",
        "live_fallback_source": "gold-api.com",
        "live_fallback_url": GOLD_API_SPOT_URL,
        "frequency": "Monthly average 1990-01 to 2021-09; completed daily XAU/USD spot bars thereafter; live quote checks every 30 minutes Monday-Saturday",
        "price_field": "USD per troy ounce; World Bank monthly average for long history; Yahoo Finance XAUUSD=X completed daily close for ongoing daily refresh",
        "status": "LIVE" if quote.get("quote_status") in {"XAUS_FRESH", "GOLD_API_REALTIME"} else "STALE",
        "history_scope": "World Bank Pink Sheet monthly gold averages from 1990-01 through 2021-09; stored daily spot history thereafter, with recent dates refreshed from Yahoo Finance XAUUSD=X and goldprice.dev used only as a fallback; no synthetic interpolation",
        "history_segments": [
            {
                "start": "1990-01-01",
                "end": "2021-09-01",
                "frequency": "monthly_average",
                "source": "World Bank Commodity Markets (Pink Sheet)",
                "ingest": "datasets/gold-prices mirror",
            },
            {
                "start": "2021-09-13",
                "end": merged[-1]["date"],
                "frequency": "daily",
                "source": "Stored XAU/USD spot history; recent dates refreshed by Yahoo Finance XAUUSD=X",
            },
        ] if coverage_has_world_bank else [
            {
                "start": merged[0]["date"],
                "end": merged[-1]["date"],
                "frequency": "daily",
                "source": "Stored XAU/USD spot history; recent dates refreshed by Yahoo Finance XAUUSD=X",
            }
        ],
        "history_start": merged[0]["date"],
        "history_end": merged[-1]["date"],
        "observation_count": len(merged),
        "history_data_state": {
            "daily_source": daily_source,
            "fallback_used": daily_fallback_used,
        },
        "history_fetch_warning": history_error,
        "world_bank_fetch_warning": world_bank_error,
        "latest_quote": {
            **quote,
            "retrieved_at_bjt": now_bjt.isoformat(timespec="seconds"),
            "observation_date": merged[-1]["date"],
        },
        "data": merged,
        "updated_at_bjt": now_bjt.isoformat(timespec="seconds"),
    }

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Gold updated: {payload['history_start']} -> {payload['history_end']} "
        f"({len(merged)} rows), daily={daily_source}, latest={payload['latest_quote']['price']} "
        f"via {payload['latest_quote'].get('source')}"
    )


if __name__ == "__main__":
    main()
