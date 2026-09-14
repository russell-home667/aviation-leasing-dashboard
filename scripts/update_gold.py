#!/usr/bin/env python3
import argparse
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


def request_json(url: str, timeout: int = 30):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; aviation-leasing-dashboard/1.0; personal research)",
        "Accept": "application/json",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def load_existing():
    if not OUT.exists():
        return {}
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {}


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
        rec = {"date": str(d)[:10], "value": round(close, 2)}
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
    parser.add_argument("--full", action="store_true", help="Retained for workflow compatibility; XAUS returns its maximum daily history")
    parser.parse_args()

    now_bjt = datetime.now(TZ_BJT)
    existing = load_existing()
    old_rows = existing.get("data", []) if isinstance(existing, dict) else []

    history_rows = []
    history_state = {}
    history_error = None
    try:
        history_rows, history_state = fetch_xaus_history()
        print(f"XAUS daily history: {len(history_rows)} rows, {history_rows[0]['date']} -> {history_rows[-1]['date']}")
    except Exception as exc:
        history_error = str(exc)
        print(f"History warning: {exc}")

    by_date = {}
    for row in old_rows + history_rows:
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
        raise RuntimeError("No XAU/USD daily history available")

    try:
        quote = fetch_live_quote()
    except Exception as exc:
        if not existing.get("latest_quote"):
            raise
        quote = dict(existing["latest_quote"])
        quote["quote_status"] = "STALE_STORED_QUOTE"
        quote["fallback_reason"] = str(exc)
        print(f"Quote warning: {exc}; preserving prior stored quote")

    payload = {
        "name": "Gold Spot / US Dollar",
        "ticker": "XAU/USD",
        "unit": "USD/oz",
        "instrument_type": "spot",
        "source": "XAUS Gold Data API",
        "source_url": "https://xaus.com/api/",
        "live_fallback_source": "gold-api.com",
        "live_fallback_url": GOLD_API_SPOT_URL,
        "frequency": "Daily historical series; updater checks every 30 minutes Monday-Saturday",
        "price_field": "XAU/USD spot price",
        "status": "LIVE" if quote.get("quote_status") in {"XAUS_FRESH", "GOLD_API_REALTIME"} else "STALE",
        "history_scope": "Up to five years of XAU/USD daily closes from XAUS; no gold-futures substitution and no synthetic interpolation",
        "history_start": merged[0]["date"],
        "history_end": merged[-1]["date"],
        "observation_count": len(merged),
        "history_data_state": history_state,
        "history_fetch_warning": history_error,
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
