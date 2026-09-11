#!/usr/bin/env python3
"""Update Step 5 market/liquidity data for the AI Bubble Monitor.

Outputs live under data/ai_bubble/market_liquidity/ and are deliberately
separate from the aviation-leasing dashboard data.

Groups:
  market  - NDX, SOX, NVDA, QQQ, RSP and derived QQQ/RSP
  vix     - official Cboe VIX daily history
  macro   - FRED DFII10 (10Y real yield)
  credit  - FRED HY OAS, IG OAS, plus BAA10Y long-history backtest proxy
  all     - all of the above

FRED_API_KEY is optional. When absent, the script uses FRED's public CSV
endpoint; when present, it uses the official FRED JSON API.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

BJT = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ai_bubble" / "market_liquidity"
OUT.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "AI-Bubble-Monitor/1.0 (github.com/russell-home667/aviation-leasing-dashboard)",
    "Accept": "text/csv,application/json,text/plain,*/*",
}

MARKET = {
    "ndx": {"ticker": "^NDX", "name": "Nasdaq-100", "start": "1985-01-01", "unit": "index"},
    "sox": {"ticker": "^SOX", "name": "PHLX Semiconductor Index", "start": "1994-01-01", "unit": "index"},
    "nvda": {"ticker": "NVDA", "name": "NVIDIA", "start": "1999-01-22", "unit": "USD"},
    "qqq": {"ticker": "QQQ", "name": "Invesco QQQ", "start": "1999-03-10", "unit": "USD"},
    "rsp": {"ticker": "RSP", "name": "Invesco S&P 500 Equal Weight ETF", "start": "2003-04-24", "unit": "USD"},
}

FRED = {
    "dfii10": {
        "series": "DFII10",
        "name": "10-Year Treasury Inflation-Indexed Security, Constant Maturity",
        "unit": "%",
        "source": "Federal Reserve / FRED",
    },
    "hy_oas": {
        "series": "BAMLH0A0HYM2",
        "name": "ICE BofA US High Yield Index Option-Adjusted Spread",
        "unit": "%",
        "source": "ICE BofA / FRED",
    },
    "ig_oas": {
        "series": "BAMLC0A0CM",
        "name": "ICE BofA US Corporate Index Option-Adjusted Spread",
        "unit": "%",
        "source": "ICE BofA / FRED",
    },
    "baa10y_proxy": {
        "series": "BAA10Y",
        "name": "Moody's Seasoned Baa Corporate Bond Yield Relative to 10-Year Treasury",
        "unit": "%",
        "source": "Moody's / Federal Reserve / FRED",
    },
}

CBOE_VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"


def now_bjt() -> str:
    return datetime.now(BJT).isoformat(timespec="seconds")


def request_with_retry(url: str, *, params=None, timeout: int = 30, attempts: int = 3) -> requests.Response:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i + 1 < attempts:
                time.sleep(2 ** i)
    raise RuntimeError(f"GET failed after {attempts} attempts: {url}: {last}")


def read_csv_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df


def atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    out = df.copy()
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    out.to_csv(tmp, index=False)
    tmp.replace(path)


def merge_by_date(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        combined = new.copy()
    else:
        combined = pd.concat([existing, new], ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"], errors="coerce")
    combined = combined.dropna(subset=["date"]).sort_values("date")
    combined = combined.drop_duplicates(subset=["date"], keep="last")
    return combined.reset_index(drop=True)


def yahoo_download(key: str, meta: dict) -> pd.DataFrame:
    path = OUT / f"{key}.csv"
    existing = read_csv_if_exists(path)
    if not existing.empty:
        latest = pd.to_datetime(existing["date"]).max()
        start = (latest - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
    else:
        start = meta["start"]

    raw = yf.download(
        meta["ticker"],
        start=start,
        end=None,
        auto_adjust=False,
        progress=False,
        threads=False,
        timeout=30,
    )
    if raw.empty:
        raise RuntimeError(f"Yahoo Finance returned no rows for {meta['ticker']}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.reset_index()
    date_col = "Date" if "Date" in raw.columns else raw.columns[0]
    rename = {
        date_col: "date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Adj Close": "adj_close",
        "Volume": "volume",
    }
    raw = raw.rename(columns=rename)
    keep = [c for c in ["date", "open", "high", "low", "close", "adj_close", "volume"] if c in raw.columns]
    raw = raw[keep].copy()
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce").dt.tz_localize(None)
    for c in ["open", "high", "low", "close", "adj_close", "volume"]:
        if c in raw.columns:
            raw[c] = pd.to_numeric(raw[c], errors="coerce")
    raw = raw.dropna(subset=["date", "close"])
    raw["source"] = "Yahoo Finance"
    raw["source_symbol"] = meta["ticker"]
    raw["fetched_at_bjt"] = now_bjt()
    raw["status"] = "confirmed"

    combined = merge_by_date(existing, raw)
    atomic_write_csv(combined, path)
    return combined


def build_qqq_rsp() -> pd.DataFrame:
    qqq = read_csv_if_exists(OUT / "qqq.csv")
    rsp = read_csv_if_exists(OUT / "rsp.csv")
    if qqq.empty or rsp.empty:
        raise RuntimeError("QQQ and RSP are required before QQQ/RSP can be calculated")
    q = qqq[["date", "close"]].rename(columns={"close": "qqq_close"})
    r = rsp[["date", "close"]].rename(columns={"close": "rsp_close"})
    df = pd.merge(q, r, on="date", how="inner").sort_values("date")
    df["close"] = df["qqq_close"] / df["rsp_close"]
    df["source"] = "Derived from Yahoo Finance QQQ and RSP"
    df["source_symbol"] = "QQQ/RSP"
    df["fetched_at_bjt"] = now_bjt()
    df["status"] = "confirmed"
    atomic_write_csv(df, OUT / "qqq_rsp.csv")
    return df


def update_vix() -> pd.DataFrame:
    r = request_with_retry(CBOE_VIX_URL)
    from io import StringIO

    raw = pd.read_csv(StringIO(r.text))
    raw.columns = [str(c).strip().upper() for c in raw.columns]
    if "DATE" not in raw.columns or "CLOSE" not in raw.columns:
        raise RuntimeError(f"Unexpected Cboe VIX columns: {list(raw.columns)}")
    df = raw.rename(columns={"DATE": "date", "OPEN": "open", "HIGH": "high", "LOW": "low", "CLOSE": "close"})
    keep = [c for c in ["date", "open", "high", "low", "close"] if c in df.columns]
    df = df[keep].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ["open", "high", "low", "close"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date")
    df["source"] = "Cboe Global Markets"
    df["source_symbol"] = "VIX"
    df["fetched_at_bjt"] = now_bjt()
    df["status"] = "confirmed"
    atomic_write_csv(df, OUT / "vix.csv")
    return df


def fred_download(key: str, meta: dict) -> pd.DataFrame:
    series = meta["series"]
    api_key = os.getenv("FRED_API_KEY", "").strip()
    if api_key:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "asc",
        }
        payload = request_with_retry(url, params=params).json()
        rows = payload.get("observations", [])
        df = pd.DataFrame({"date": [x.get("date") for x in rows], "value": [x.get("value") for x in rows]})
        endpoint = "FRED API"
    else:
        url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
        from io import StringIO

        text = request_with_retry(url, params={"id": series}).text
        raw = pd.read_csv(StringIO(text))
        if "DATE" in raw.columns:
            date_col = "DATE"
        elif "observation_date" in raw.columns:
            date_col = "observation_date"
        else:
            date_col = raw.columns[0]
        value_col = series if series in raw.columns else raw.columns[-1]
        df = raw[[date_col, value_col]].rename(columns={date_col: "date", value_col: "value"})
        endpoint = "FRED public CSV"

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"].replace(".", np.nan), errors="coerce")
    df = df.dropna(subset=["date", "value"]).sort_values("date")
    df["source"] = meta["source"]
    df["source_symbol"] = series
    df["endpoint"] = endpoint
    df["fetched_at_bjt"] = now_bjt()
    df["status"] = "confirmed"
    atomic_write_csv(df, OUT / f"{key}.csv")
    return df


def pct_change(close: pd.Series, periods: int) -> Optional[float]:
    s = pd.to_numeric(close, errors="coerce").dropna()
    if len(s) <= periods:
        return None
    old = float(s.iloc[-periods - 1])
    new = float(s.iloc[-1])
    if old == 0:
        return None
    return (new / old - 1.0) * 100.0


def summary_for_price(path: Path, *, unit: str, name: str, source_url: str) -> Optional[dict]:
    df = read_csv_if_exists(path)
    if df.empty or "close" not in df.columns:
        return None
    df = df.dropna(subset=["date", "close"]).sort_values("date")
    s = pd.to_numeric(df["close"], errors="coerce").dropna()
    if s.empty:
        return None
    last_idx = s.index[-1]
    last = float(s.loc[last_idx])
    date = pd.to_datetime(df.loc[last_idx, "date"])
    sma20 = float(s.rolling(20).mean().iloc[-1]) if len(s) >= 20 else None
    sma50 = float(s.rolling(50).mean().iloc[-1]) if len(s) >= 50 else None
    sma200 = float(s.rolling(200).mean().iloc[-1]) if len(s) >= 200 else None
    ath = float(s.max())
    age_days = (pd.Timestamp.now(tz=BJT).tz_localize(None).normalize() - date.normalize()).days
    status = "fresh" if age_days <= 4 else "stale"
    return {
        "name": name,
        "observation_date": date.strftime("%Y-%m-%d"),
        "value": round(last, 6),
        "unit": unit,
        "change_1d_pct": _round(pct_change(s, 1)),
        "change_5d_pct": _round(pct_change(s, 5)),
        "change_20d_pct": _round(pct_change(s, 20)),
        "sma20": _round(sma20, 6),
        "sma50": _round(sma50, 6),
        "sma200": _round(sma200, 6),
        "distance_from_200dma_pct": _round((last / sma200 - 1) * 100, 4) if sma200 else None,
        "ath_drawdown_pct": _round((last / ath - 1) * 100, 4) if ath else None,
        "source": str(df.iloc[-1].get("source", "")),
        "source_symbol": str(df.iloc[-1].get("source_symbol", "")),
        "source_url": source_url,
        "fetched_at_bjt": str(df.iloc[-1].get("fetched_at_bjt", "")),
        "status": status,
    }


def summary_for_fred(path: Path, *, name: str, unit: str, source_url: str) -> Optional[dict]:
    df = read_csv_if_exists(path)
    if df.empty or "value" not in df.columns:
        return None
    df = df.dropna(subset=["date", "value"]).sort_values("date")
    s = pd.to_numeric(df["value"], errors="coerce").dropna()
    if s.empty:
        return None
    idx = s.index[-1]
    date = pd.to_datetime(df.loc[idx, "date"])
    age_days = (pd.Timestamp.now(tz=BJT).tz_localize(None).normalize() - date.normalize()).days
    return {
        "name": name,
        "observation_date": date.strftime("%Y-%m-%d"),
        "value": round(float(s.loc[idx]), 6),
        "unit": unit,
        "change_1obs": _round(float(s.iloc[-1] - s.iloc[-2]), 6) if len(s) >= 2 else None,
        "change_5obs": _round(float(s.iloc[-1] - s.iloc[-6]), 6) if len(s) >= 6 else None,
        "change_20obs": _round(float(s.iloc[-1] - s.iloc[-21]), 6) if len(s) >= 21 else None,
        "source": str(df.iloc[-1].get("source", "")),
        "source_symbol": str(df.iloc[-1].get("source_symbol", "")),
        "source_url": source_url,
        "fetched_at_bjt": str(df.iloc[-1].get("fetched_at_bjt", "")),
        "status": "fresh" if age_days <= 5 else "stale",
    }


def _round(value, digits: int = 4):
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def write_latest(errors: Dict[str, str]) -> None:
    indicators: Dict[str, dict] = {}
    source_urls = {
        "ndx": "https://finance.yahoo.com/quote/%5ENDX/history/",
        "sox": "https://finance.yahoo.com/quote/%5ESOX/history/",
        "nvda": "https://finance.yahoo.com/quote/NVDA/history/",
        "qqq_rsp": "https://finance.yahoo.com/",
        "vix": "https://www.cboe.com/tradable_products/vix/vix_historical_data/",
    }
    price_defs = {
        "ndx": ("Nasdaq-100", "index"),
        "sox": ("PHLX Semiconductor Index", "index"),
        "nvda": ("NVIDIA", "USD"),
        "qqq_rsp": ("QQQ / RSP concentration ratio", "ratio"),
        "vix": ("Cboe VIX", "index"),
    }
    for key, (name, unit) in price_defs.items():
        x = summary_for_price(OUT / f"{key}.csv", unit=unit, name=name, source_url=source_urls[key])
        if x:
            indicators[key] = x

    for key in ["dfii10", "hy_oas", "ig_oas", "baa10y_proxy"]:
        meta = FRED[key]
        x = summary_for_fred(
            OUT / f"{key}.csv",
            name=meta["name"],
            unit=meta["unit"],
            source_url=f"https://fred.stlouisfed.org/series/{meta['series']}",
        )
        if x:
            indicators[key] = x

    payload = {
        "module": "AI Bubble Monitor - Market & Liquidity",
        "step": 5,
        "timezone": "Asia/Shanghai",
        "generated_at_bjt": now_bjt(),
        "indicators": indicators,
        "errors": errors,
        "notes": {
            "hy_ig_history": "ICE BofA FRED series may be license-limited to recent history; BAA10Y is stored as a long-history credit-stress proxy for later backtests.",
            "qqq_rsp": "Derived daily from QQQ close divided by RSP close; higher values indicate stronger mega-cap/tech concentration relative to equal-weight S&P 500.",
        },
    }
    tmp = OUT / "latest.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUT / "latest.json")


def run_group(group: str) -> Dict[str, str]:
    errors: Dict[str, str] = {}

    if group in {"all", "market"}:
        for key, meta in MARKET.items():
            try:
                yahoo_download(key, meta)
                print(f"OK market {key}")
            except Exception as exc:  # noqa: BLE001
                errors[key] = str(exc)
                print(f"ERROR market {key}: {exc}", file=sys.stderr)
        try:
            build_qqq_rsp()
            print("OK derived qqq_rsp")
        except Exception as exc:  # noqa: BLE001
            errors["qqq_rsp"] = str(exc)
            print(f"ERROR qqq_rsp: {exc}", file=sys.stderr)

    if group in {"all", "vix"}:
        try:
            update_vix()
            print("OK vix")
        except Exception as exc:  # noqa: BLE001
            errors["vix"] = str(exc)
            print(f"ERROR vix: {exc}", file=sys.stderr)

    if group in {"all", "macro"}:
        try:
            fred_download("dfii10", FRED["dfii10"])
            print("OK fred dfii10")
        except Exception as exc:  # noqa: BLE001
            errors["dfii10"] = str(exc)
            print(f"ERROR dfii10: {exc}", file=sys.stderr)

    if group in {"all", "credit"}:
        for key in ["hy_oas", "ig_oas", "baa10y_proxy"]:
            try:
                fred_download(key, FRED[key])
                print(f"OK fred {key}")
            except Exception as exc:  # noqa: BLE001
                errors[key] = str(exc)
                print(f"ERROR {key}: {exc}", file=sys.stderr)

    write_latest(errors)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", choices=["all", "market", "vix", "macro", "credit"], default="all")
    args = parser.parse_args()
    errors = run_group(args.group)
    # Preserve prior good data on partial provider failures, but make a total
    # group failure visible to Actions by returning non-zero.
    expected = {
        "market": {"ndx", "sox", "nvda", "qqq", "rsp", "qqq_rsp"},
        "vix": {"vix"},
        "macro": {"dfii10"},
        "credit": {"hy_oas", "ig_oas", "baa10y_proxy"},
    }
    if args.group != "all" and expected[args.group].issubset(errors.keys()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
