#!/usr/bin/env python3
import argparse
import csv
import io
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "gold_xauusd.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

TZ_BJT = ZoneInfo("Asia/Shanghai")
XAUS_HISTORY_URL = "https://xaus.com/api/v1/history"
XAUS_SPOT_URL = "https://xaus.com/api/v1/spot?compact=1"
GOLD_API_SPOT_URL = "https://api.gold-api.com/price/XAU"

# Public mirror maintained from the World Bank Commodity Markets (Pink Sheet).
# Its README explicitly documents that 1960-present values come from the World Bank.
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
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def request_text(url: str, timeout: int = 45):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; aviation-leasing-dashboard/1.0; personal research)",
        "Accept": "text/csv,text/plain,*/*",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def load_existing():
    if not OUT.exists():
        return {}
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fetch_world_bank_monthly():
    """Return World Bank Pink Sheet monthly gold averages for 1990-01 through 2021-09.

    The ingestion endpoint is the datasets/gold-prices public mirror. Its documented
    source for 1960-present is World Bank Commodity Markets (Pink Sheet). Values are
    monthly averages in USD per troy ounce. We preserve that frequency explicitly and
    do not interpolate the data into synthetic daily observations.
    """
    text = request_text(WORLD_BANK_GOLD_CSV, 60)
    reader = csv.DictReader(io.StringIO(text))
    rows = []

    for rec in reader:
        ym = str(rec.get("Date") or "").strip()
        raw = rec.get("Price")
        if not ym or raw is None:
            continue
        if ym < WORLD_BANK_START_MONTH or ym > WORLD_BANK_END_MONTH:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value <= 100:
            continue
        rows.append(
            {
                "date": f"{ym}-01",
                "period": ym,
                "value": round(value, 2),
                "frequency": "monthly_average",
                "source": "World Bank Commodity Markets (Pink Sheet)",
            }
        )

    rows.sort(key=lambda x: x["date"])
    expected_min = (2021 - 1990) * 12 + 9
    if len(rows) < expected_min:
        raise RuntimeError(f"World Bank gold history unexpectedly short: {len(rows)} rows")
    if rows[0]["date"] != "1990-01-01":
        raise RuntimeError(f"World Bank gold history starts at {rows[0]['date']}, expected 1990-01-01")
    return rows


def fetch_xaus_history():
    payload = request_json(XAUS_HISTORY_URL, 45)
    points = payload.get("points") or []
    rows = []
    for p in points:
        d = p.get("d")
        close = p.get("c")
        if not d or close is None:
            continue
        try:
            close = float(close)
        except (TypeError, ValueError):
            continue
        if close <= 100:
            continue
        rec = {
            "date": str(d)[:10],
            "value": round(close, 2),
            "frequency": "daily",
            "source": "XAUS Gold Data API",
        }
        if p.get("h") is not None:
            rec["high"] = round(float(p["h"]), 2)
        if p.get("l") is not None:
            rec["low"] = round(float(p["l"]), 2)
        rows.append(rec)
    rows.sort(key=lambda x: x["date"])
    if len(rows) < 20:
        raise RuntimeError(f"XAUS history unexpectedly short: {len(rows)} rows")
    return rows, payload.get("data_state") or {}


def fetch_live_quote():
    errors = []

    # Primary: XAUS keyless XAU/USD spot endpoint.
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

    # Secondary: keyless real-time XAU endpoint. It is only used if XAUS is unavailable.
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
    parser.add_argument(
        "--full",
        action="store_true",
        help="Refresh World Bank long history as well as the current XAUS daily history",
    )
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
            print(
                f"World Bank Pink Sheet monthly gold: {len(world_bank_rows)} rows, "
                f"{world_bank_rows[0]['date']} -> {world_bank_rows[-1]['date']}"
            )
        except Exception as exc:
            world_bank_error = str(exc)
            print(f"World Bank history warning: {exc}")

    history_rows = []
    history_state = {}
    history_error = None
    try:
        history_rows, history_state = fetch_xaus_history()
        print(f"XAUS daily history: {len(history_rows)} rows, {history_rows[0]['date']} -> {history_rows[-1]['date']}")
    except Exception as exc:
        history_error = str(exc)
        print(f"XAUS history warning: {exc}")

    by_date = {}
    # Ordering matters: freshly fetched authoritative segment data override older stored rows.
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

    try:
        quote = fetch_live_quote()
    except Exception as exc:
        if not existing.get("latest_quote"):
            raise
        quote = dict(existing["latest_quote"])
        quote["quote_status"] = "STALE_STORED_QUOTE"
        quote["fallback_reason"] = str(exc)
        print(f"Quote warning: {exc}; preserving prior stored quote")

    coverage_has_world_bank = merged[0]["date"] <= "1990-01-01"
    combined_source = (
        "World Bank Pink Sheet + XAUS Gold Data API"
        if coverage_has_world_bank
        else "XAUS Gold Data API"
    )

    payload = {
        "name": "Gold Spot / US Dollar",
        "ticker": "XAU/USD",
        "unit": "USD/oz",
        "instrument_type": "spot",
        "source": combined_source,
        "source_url": "https://xaus.com/api/",
        "historical_source": "World Bank Commodity Markets (Pink Sheet)",
        "historical_source_url": WORLD_BANK_SOURCE_URL,
        "historical_ingest": "datasets/gold-prices mirror of World Bank monthly Gold series",
        "historical_ingest_url": WORLD_BANK_MIRROR_URL,
        "current_source": "XAUS Gold Data API",
        "current_source_url": "https://xaus.com/api/",
        "live_fallback_source": "gold-api.com",
        "live_fallback_url": GOLD_API_SPOT_URL,
        "frequency": "Monthly average 1990-01 to 2021-09; daily XAU/USD spot from 2021-09-13 onward; live updater checks every 30 minutes Monday-Saturday",
        "price_field": "USD per troy ounce; World Bank monthly average for long history, XAUS daily/spot for recent history",
        "status": "LIVE" if quote.get("quote_status") in {"XAUS_FRESH", "GOLD_API_REALTIME"} else "STALE",
        "history_scope": "World Bank Pink Sheet monthly gold averages from 1990-01 through 2021-09, then XAUS XAU/USD daily spot history from 2021-09-13 onward; no synthetic interpolation",
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
                "source": "XAUS Gold Data API",
            },
        ] if coverage_has_world_bank else [
            {
                "start": merged[0]["date"],
                "end": merged[-1]["date"],
                "frequency": "daily",
                "source": "XAUS Gold Data API",
            }
        ],
        "history_start": merged[0]["date"],
        "history_end": merged[-1]["date"],
        "observation_count": len(merged),
        "history_data_state": history_state,
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
        f"({len(merged)} rows), latest={payload['latest_quote']['price']} "
        f"via {payload['latest_quote'].get('source')}"
    )


if __name__ == "__main__":
    main()
