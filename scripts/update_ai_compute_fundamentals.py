#!/usr/bin/env python3
"""Step 7: AI compute-demand fundamentals for the AI Bubble Monitor.

Outputs:
  data/ai_bubble/compute_fundamentals/nvda_quarterly.csv
  data/ai_bubble/compute_fundamentals/tsmc_monthly.csv
  data/ai_bubble/compute_fundamentals/latest.json

NVIDIA standard statement data are fetched from Yahoo Finance/yfinance for
unattended automation. Official NVIDIA disclosures in
nvda_official_overrides.csv take precedence for Data Center revenue and other
confirmed observations.

TSMC monthly revenue is fetched directly from TSMC Investor Relations.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

BJT = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ai_bubble" / "compute_fundamentals"
OUT.mkdir(parents=True, exist_ok=True)
OVERRIDES = OUT / "nvda_official_overrides.csv"
HEADERS = {
    "User-Agent": "Mozilla/5.0 AI-Bubble-Monitor/1.0",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
}
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def now_bjt() -> str:
    return datetime.now(BJT).isoformat(timespec="seconds")


def nkey(x: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def fnum(x: object) -> Optional[float]:
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except Exception:
        return None


def request(url: str, attempts: int = 3) -> requests.Response:
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r
        except Exception as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(2 ** i)
    raise RuntimeError(f"GET failed: {url}: {last}")


def row_series(df: pd.DataFrame, names: list[str]) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=float)
    norm = {nkey(i): i for i in df.index}
    for name in names:
        k = nkey(name)
        if k in norm:
            return pd.to_numeric(df.loc[norm[k]], errors="coerce")
    for name in names:
        k = nkey(name)
        for nk, original in norm.items():
            if k in nk or nk in k:
                return pd.to_numeric(df.loc[original], errors="coerce")
    return pd.Series(dtype=float)


def series_map(s: pd.Series) -> dict[pd.Timestamp, float]:
    out: dict[pd.Timestamp, float] = {}
    for c, v in s.items():
        d = pd.to_datetime(c, errors="coerce")
        val = fnum(v)
        if pd.notna(d) and val is not None:
            out[pd.Timestamp(d).tz_localize(None).normalize()] = val
    return out


def merge_history(path: Path, fresh: pd.DataFrame, key: str) -> pd.DataFrame:
    if path.exists():
        old = pd.read_csv(path)
        combined = pd.concat([old, fresh], ignore_index=True, sort=False)
    else:
        combined = fresh.copy()
    combined[key] = pd.to_datetime(combined[key], errors="coerce")
    combined = combined.dropna(subset=[key]).sort_values(key)
    combined = combined.drop_duplicates(subset=[key], keep="last").reset_index(drop=True)
    return combined


def apply_nvda_overrides(df: pd.DataFrame) -> pd.DataFrame:
    if not OVERRIDES.exists():
        return df
    ov = pd.read_csv(OVERRIDES)
    ov["period_end"] = pd.to_datetime(ov["period_end"], errors="coerce")
    df = df.copy()
    df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce")
    numeric_cols = [
        "fiscal_year", "data_center_revenue_usd_bn", "total_revenue_usd_bn",
        "gross_margin_pct", "inventory_usd_bn", "accounts_receivable_usd_bn", "dso_days",
    ]
    for _, r in ov.iterrows():
        d = r["period_end"]
        if pd.isna(d):
            continue
        if df.empty:
            idx = None
        else:
            delta = (df["period_end"] - d).abs().dt.days
            idx = delta.idxmin() if len(delta) and delta.min() <= 10 else None
        if idx is None:
            new = {c: None for c in df.columns}
            new["period_end"] = d
            df = pd.concat([df, pd.DataFrame([new])], ignore_index=True)
            idx = df.index[-1]
        for col in numeric_cols + ["fiscal_quarter", "source", "source_url"]:
            if col in r.index and pd.notna(r[col]) and str(r[col]).strip() != "":
                df.at[idx, col] = r[col]
        # If official total revenue and gross margin exist, derive COGS when needed.
        rev = fnum(df.at[idx, "total_revenue_usd_bn"]) if "total_revenue_usd_bn" in df.columns else None
        gm = fnum(df.at[idx, "gross_margin_pct"]) if "gross_margin_pct" in df.columns else None
        if rev is not None and gm is not None:
            df.at[idx, "cost_of_revenue_usd_bn"] = rev * (1.0 - gm / 100.0)
    return df.sort_values("period_end").reset_index(drop=True)


def update_nvda() -> pd.DataFrame:
    t = yf.Ticker("NVDA")
    income = t.quarterly_income_stmt
    balance = t.quarterly_balance_sheet
    rev = series_map(row_series(income, ["Total Revenue", "Operating Revenue"]))
    cogs = series_map(row_series(income, ["Cost Of Revenue", "Reconciled Cost Of Revenue"]))
    gp = series_map(row_series(income, ["Gross Profit"]))
    inv = series_map(row_series(balance, ["Inventory", "Inventories"]))
    ar = series_map(row_series(balance, ["Accounts Receivable", "Receivables", "Net Receivables"]))
    dates = sorted(set(rev) | set(cogs) | set(gp) | set(inv) | set(ar))
    rows = []
    fetched = now_bjt()
    for d in dates:
        revenue = rev.get(d)
        cost = cogs.get(d)
        gross = gp.get(d)
        gm = None
        if revenue and gross is not None:
            gm = gross / revenue * 100
        elif revenue and cost is not None:
            gm = (1 - cost / revenue) * 100
        rows.append({
            "period_end": d,
            "fiscal_year": None,
            "fiscal_quarter": None,
            "total_revenue_usd_bn": revenue / 1e9 if revenue is not None else None,
            "cost_of_revenue_usd_bn": cost / 1e9 if cost is not None else None,
            "gross_margin_pct": gm,
            "inventory_usd_bn": inv.get(d) / 1e9 if inv.get(d) is not None else None,
            "accounts_receivable_usd_bn": ar.get(d) / 1e9 if ar.get(d) is not None else None,
            "data_center_revenue_usd_bn": None,
            "data_center_yoy_pct": None,
            "data_center_qoq_pct": None,
            "inventory_days": None,
            "dso_days": None,
            "source": "Yahoo Finance quarterly statements",
            "source_url": "https://finance.yahoo.com/quote/NVDA/financials/",
            "fetched_at_bjt": fetched,
        })
    fresh = pd.DataFrame(rows)
    path = OUT / "nvda_quarterly.csv"
    df = merge_history(path, fresh, "period_end") if not fresh.empty else (pd.read_csv(path) if path.exists() else pd.DataFrame())
    df = apply_nvda_overrides(df)
    df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce")
    # Recompute ratios across the full stored history.
    df = df.sort_values("period_end").reset_index(drop=True)
    for i in range(len(df)):
        cur = df.loc[i]
        if i > 0:
            prev = df.loc[i - 1]
            days = (cur["period_end"] - prev["period_end"]).days
            if not 60 <= days <= 120:
                days = 91.25
        else:
            prev = None
            days = 91.25
        cost = fnum(cur.get("cost_of_revenue_usd_bn"))
        cinv = fnum(cur.get("inventory_usd_bn"))
        pinv = fnum(prev.get("inventory_usd_bn")) if prev is not None else None
        if cost and cinv is not None:
            avg_inv = (cinv + pinv) / 2 if pinv is not None else cinv
            df.at[i, "inventory_days"] = avg_inv / cost * days
        if pd.isna(cur.get("dso_days")):
            revenue = fnum(cur.get("total_revenue_usd_bn"))
            car = fnum(cur.get("accounts_receivable_usd_bn"))
            par = fnum(prev.get("accounts_receivable_usd_bn")) if prev is not None else None
            if revenue and car is not None:
                avg_ar = (car + par) / 2 if par is not None else car
                df.at[i, "dso_days"] = avg_ar / revenue * days
    # Data Center growth is based only on official observations.
    official_idx = [i for i in df.index if fnum(df.at[i, "data_center_revenue_usd_bn"]) is not None]
    for pos, i in enumerate(official_idx):
        cur_dc = fnum(df.at[i, "data_center_revenue_usd_bn"])
        if pos > 0:
            p = official_idx[pos - 1]
            prev_dc = fnum(df.at[p, "data_center_revenue_usd_bn"])
            if cur_dc is not None and prev_dc:
                df.at[i, "data_center_qoq_pct"] = (cur_dc / prev_dc - 1) * 100
        fy = fnum(df.at[i, "fiscal_year"])
        fq = str(df.at[i, "fiscal_quarter"])
        if fy is not None:
            matches = df[(pd.to_numeric(df["fiscal_year"], errors="coerce") == fy - 1) & (df["fiscal_quarter"].astype(str) == fq)]
            if not matches.empty:
                old = fnum(matches.iloc[-1].get("data_center_revenue_usd_bn"))
                if cur_dc is not None and old:
                    df.at[i, "data_center_yoy_pct"] = (cur_dc / old - 1) * 100
    # Round presentation fields and persist.
    for c in ["total_revenue_usd_bn", "cost_of_revenue_usd_bn", "data_center_revenue_usd_bn", "inventory_usd_bn", "accounts_receivable_usd_bn"]:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").round(3)
    for c in ["gross_margin_pct", "data_center_yoy_pct", "data_center_qoq_pct", "inventory_days", "dso_days"]:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").round(2)
    out = df.copy()
    out["period_end"] = out["period_end"].dt.strftime("%Y-%m-%d")
    out.to_csv(path, index=False)
    return df


def clean_month(v: object) -> Optional[int]:
    s = re.sub(r"[^a-z]", "", str(v).lower())
    return MONTHS.get(s)


def parse_tsmc_year(year: int) -> pd.DataFrame:
    url = f"https://investor.tsmc.com/english/monthly-revenue/{year}"
    html = request(url).text
    tables = pd.read_html(StringIO(html))
    best = None
    for table in tables:
        t = table.copy()
        if isinstance(t.columns, pd.MultiIndex):
            t.columns = [" ".join(str(x) for x in col if str(x) != "nan").strip() for col in t.columns]
        else:
            t.columns = [str(c) for c in t.columns]
        if t.shape[1] < 3:
            continue
        months = t.iloc[:, 0].map(clean_month)
        if months.notna().sum() >= 6:
            best = t
            break
    if best is None:
        raise RuntimeError(f"Could not identify TSMC monthly table for {year}")
    rows = []
    for _, r in best.iterrows():
        m = clean_month(r.iloc[0])
        if not m:
            continue
        rev_raw = str(r.iloc[1]).replace(",", "").strip()
        yoy_raw = str(r.iloc[2]).replace("%", "").strip()
        revenue = pd.to_numeric(rev_raw, errors="coerce")
        yoy = pd.to_numeric(yoy_raw, errors="coerce")
        if pd.isna(revenue):
            continue
        rows.append({
            "month": f"{year}-{m:02d}",
            "revenue_twd_mn": float(revenue),
            "yoy_pct_reported": float(yoy) if pd.notna(yoy) else None,
            "source": "TSMC Investor Relations",
            "source_url": url,
            "fetched_at_bjt": now_bjt(),
        })
    return pd.DataFrame(rows)


def update_tsmc() -> pd.DataFrame:
    path = OUT / "tsmc_monthly.csv"
    now_year = datetime.now(BJT).year
    if path.exists():
        old = pd.read_csv(path)
        years = [now_year - 1, now_year]
    else:
        old = pd.DataFrame()
        years = list(range(1999, now_year + 1))
    parts = []
    errors = []
    for year in years:
        try:
            parts.append(parse_tsmc_year(year))
        except Exception as exc:
            errors.append(f"{year}: {exc}")
    if parts:
        fresh = pd.concat(parts, ignore_index=True)
        if old.empty:
            df = fresh
        else:
            df = pd.concat([old, fresh], ignore_index=True, sort=False)
        df = df.drop_duplicates(subset=["month"], keep="last")
    elif not old.empty:
        df = old
    else:
        raise RuntimeError("No TSMC data available: " + "; ".join(errors))
    df["period"] = pd.PeriodIndex(df["month"], freq="M")
    df = df.sort_values("period").reset_index(drop=True)
    revenue = pd.to_numeric(df["revenue_twd_mn"], errors="coerce")
    rolling = revenue.rolling(3, min_periods=3).sum()
    df["rolling_3m_yoy_pct"] = ((rolling / rolling.shift(12)) - 1) * 100
    df["rolling_3m_yoy_pct"] = df["rolling_3m_yoy_pct"].round(2)
    df = df.drop(columns=["period"])
    df.to_csv(path, index=False)
    if errors:
        print("TSMC partial-year warnings:", errors)
    return df


def build_latest(errors: dict) -> dict:
    payload = {
        "module": "AI Bubble Monitor - Compute Fundamentals",
        "step": 7,
        "timezone": "Asia/Shanghai",
        "generated_at_bjt": now_bjt(),
        "nvda": None,
        "tsmc": None,
        "errors": errors,
    }
    nvpath = OUT / "nvda_quarterly.csv"
    if nvpath.exists():
        n = pd.read_csv(nvpath)
        n["period_end"] = pd.to_datetime(n["period_end"], errors="coerce")
        n = n.dropna(subset=["period_end"]).sort_values("period_end")
        if not n.empty:
            r = n.iloc[-1]
            keys = [
                "period_end", "fiscal_year", "fiscal_quarter", "total_revenue_usd_bn",
                "data_center_revenue_usd_bn", "data_center_yoy_pct", "data_center_qoq_pct",
                "gross_margin_pct", "inventory_usd_bn", "inventory_days",
                "accounts_receivable_usd_bn", "dso_days", "source", "source_url",
            ]
            payload["nvda"] = {k: (r[k] if k in r and pd.notna(r[k]) else None) for k in keys}
            payload["nvda"]["period_end"] = r["period_end"].strftime("%Y-%m-%d")
    tpath = OUT / "tsmc_monthly.csv"
    if tpath.exists():
        t = pd.read_csv(tpath).sort_values("month")
        if not t.empty:
            r = t.iloc[-1]
            payload["tsmc"] = {
                "month": str(r["month"]),
                "revenue_twd_mn": fnum(r.get("revenue_twd_mn")),
                "yoy_pct_reported": fnum(r.get("yoy_pct_reported")),
                "rolling_3m_yoy_pct": fnum(r.get("rolling_3m_yoy_pct")),
                "source": r.get("source"),
                "source_url": r.get("source_url"),
            }
    (OUT / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", choices=["all", "nvda", "tsmc"], default="all")
    args = ap.parse_args()
    errors: dict[str, str] = {}
    if args.group in ("all", "nvda"):
        try:
            n = update_nvda()
            print(f"NVDA: {len(n)} stored quarters")
        except Exception as exc:
            errors["nvda"] = str(exc)
            print("NVDA ERROR:", exc)
    if args.group in ("all", "tsmc"):
        try:
            t = update_tsmc()
            print(f"TSMC: {len(t)} stored months")
        except Exception as exc:
            errors["tsmc"] = str(exc)
            print("TSMC ERROR:", exc)
    payload = build_latest(errors)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
