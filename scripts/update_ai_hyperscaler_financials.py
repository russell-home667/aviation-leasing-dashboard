#!/usr/bin/env python3
"""Build standardized quarterly hyperscaler financials for the AI Bubble Monitor.

Step 6 scope:
- Microsoft, Alphabet, Amazon, Meta, Oracle
- Revenue
- Operating cash flow (CFO)
- Cash CapEx
- Depreciation & amortization (D&A)
- Derived FCF, CapEx/Revenue, FCF margin

Primary automated source in GitHub Actions: Yahoo Finance quarterly financial
statements via yfinance. SEC Company Facts remains the preferred official
validation source, but data.sec.gov currently returns HTTP 403 from GitHub-hosted
Azure runners even with a compliant identifying User-Agent.

Each run merges new quarters into our own CSV history, so historical observations
already collected are retained even if the upstream provider exposes a rolling
window.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

BJT = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ai_bubble" / "hyperscalers"
OUT.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Company:
    ticker: str
    name: str
    fy_end_month: int
    fy_end_day: int


COMPANIES = [
    Company("MSFT", "Microsoft", 6, 30),
    Company("GOOGL", "Alphabet", 12, 31),
    Company("AMZN", "Amazon", 12, 31),
    Company("META", "Meta", 12, 31),
    Company("ORCL", "Oracle", 5, 31),
]

ALIASES = {
    "revenue": ["TotalRevenue", "OperatingRevenue"],
    "cfo": ["OperatingCashFlow", "TotalCashFromOperatingActivities"],
    "capex": ["CapitalExpenditure", "CapitalExpenditures"],
    "da": [
        "DepreciationAndAmortization",
        "DepreciationAmortizationDepletion",
        "ReconciledDepreciation",
        "Depreciation",
    ],
}


def now_bjt() -> str:
    return datetime.now(BJT).isoformat(timespec="seconds")


def safe_float(v) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def expected_period_end(company: Company, fiscal_year: int, quarter: str) -> pd.Timestamp:
    fy_end = pd.Timestamp(year=fiscal_year, month=company.fy_end_month, day=company.fy_end_day)
    months_back = {"Q1": 9, "Q2": 6, "Q3": 3, "Q4": 0}[quarter]
    return fy_end - pd.DateOffset(months=months_back)


def infer_fiscal_period(company: Company, period_end: pd.Timestamp) -> tuple[int, str]:
    candidates = []
    for fy in range(period_end.year - 1, period_end.year + 2):
        for q in ("Q1", "Q2", "Q3", "Q4"):
            exp = expected_period_end(company, fy, q)
            diff = abs((period_end.normalize() - exp.normalize()).days)
            candidates.append((diff, fy, q))
    diff, fy, q = min(candidates)
    if diff > 25:
        # This should only happen for unusual 52/53-week reporting calendars.
        return period_end.year, f"Q{period_end.quarter}"
    return fy, q


def get_statement_row(df: pd.DataFrame, aliases: Iterable[str]) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    for name in aliases:
        if name in df.index:
            s = pd.to_numeric(df.loc[name], errors="coerce")
            if isinstance(s, pd.Series):
                return s
    return pd.Series(dtype="float64")


def merge_history(existing_path: Path, fresh: pd.DataFrame) -> pd.DataFrame:
    if existing_path.exists():
        old = pd.read_csv(existing_path)
        combined = pd.concat([old, fresh], ignore_index=True, sort=False)
    else:
        combined = fresh.copy()
    combined["period_end"] = pd.to_datetime(combined["period_end"], errors="coerce")
    combined = combined.dropna(subset=["period_end"]).sort_values("period_end")
    # Latest fetch wins for the same company/period.
    combined = combined.drop_duplicates(subset=["ticker", "period_end"], keep="last")
    return combined.reset_index(drop=True)


def fetch_company(company: Company) -> pd.DataFrame:
    t = yf.Ticker(company.ticker)
    income = t.get_income_stmt(freq="quarterly", pretty=False)
    cash = t.get_cash_flow(freq="quarterly", pretty=False)
    if income is None or income.empty:
        raise RuntimeError(f"No quarterly income statement returned for {company.ticker}")
    if cash is None or cash.empty:
        raise RuntimeError(f"No quarterly cash-flow statement returned for {company.ticker}")

    revenue_s = get_statement_row(income, ALIASES["revenue"])
    cfo_s = get_statement_row(cash, ALIASES["cfo"])
    capex_s = get_statement_row(cash, ALIASES["capex"])
    da_s = get_statement_row(cash, ALIASES["da"])

    dates = sorted(
        {
            pd.Timestamp(x).tz_localize(None)
            for x in list(income.columns) + list(cash.columns)
            if not pd.isna(pd.Timestamp(x))
        }
    )
    rows = []
    for dt in dates:
        rev = safe_float(revenue_s.get(dt)) if not revenue_s.empty else None
        cfo = safe_float(cfo_s.get(dt)) if not cfo_s.empty else None
        raw_capex = safe_float(capex_s.get(dt)) if not capex_s.empty else None
        da = safe_float(da_s.get(dt)) if not da_s.empty else None
        if rev is None and cfo is None and raw_capex is None:
            continue

        # Yahoo cash-flow statements normally report CapitalExpenditure as a negative outflow.
        # Normalize Cash CapEx to a positive amount spent.
        capex = abs(raw_capex) if raw_capex is not None else None
        fcf = cfo - capex if cfo is not None and capex is not None else None
        fy, fq = infer_fiscal_period(company, dt)
        cq = f"{dt.year}Q{dt.quarter}"
        rows.append(
            {
                "ticker": company.ticker,
                "company": company.name,
                "fiscal_year": fy,
                "fiscal_quarter": fq,
                "period_end": dt.strftime("%Y-%m-%d"),
                "calendar_quarter": cq,
                "revenue_usd": rev,
                "cfo_usd": cfo,
                "cash_capex_usd": capex,
                "da_usd": da,
                "fcf_usd": fcf,
                "capex_revenue_pct": capex / rev * 100 if capex is not None and rev not in (None, 0) else None,
                "fcf_margin_pct": fcf / rev * 100 if fcf is not None and rev not in (None, 0) else None,
                "source": "Yahoo Finance quarterly financial statements",
                "source_symbol": company.ticker,
                "fetched_at_bjt": now_bjt(),
            }
        )
    if not rows:
        raise RuntimeError(f"No usable quarterly rows returned for {company.ticker}")
    return pd.DataFrame(rows)


def write_csv(df: pd.DataFrame, path: Path) -> None:
    out = df.copy()
    if "period_end" in out.columns:
        out["period_end"] = pd.to_datetime(out["period_end"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in ["revenue_usd", "cfo_usd", "cash_capex_usd", "da_usd", "fcf_usd"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").round(0)
    out.to_csv(path, index=False)


def build_h5(frames: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    all_rows = pd.concat(frames.values(), ignore_index=True)
    core = all_rows.dropna(subset=["revenue_usd", "cfo_usd", "cash_capex_usd"]).copy()
    result = []
    for cq, g in core.groupby("calendar_quarter", sort=True):
        # One row per company per calendar quarter; if upstream ever supplies duplicates, keep latest period end.
        g = g.sort_values("period_end").drop_duplicates("ticker", keep="last")
        tickers = sorted(g["ticker"].tolist())
        revenue = float(pd.to_numeric(g["revenue_usd"]).sum())
        cfo = float(pd.to_numeric(g["cfo_usd"]).sum())
        capex = float(pd.to_numeric(g["cash_capex_usd"]).sum())
        fcf = cfo - capex
        da_series = pd.to_numeric(g["da_usd"], errors="coerce")
        da = float(da_series.sum()) if da_series.notna().any() else None
        result.append(
            {
                "calendar_quarter": cq,
                "company_count": len(tickers),
                "companies": ";".join(tickers),
                "is_complete_h5": len(tickers) == 5,
                "revenue_usd": revenue,
                "cfo_usd": cfo,
                "cash_capex_usd": capex,
                "da_usd": da,
                "fcf_usd": fcf,
                "capex_revenue_pct": capex / revenue * 100 if revenue else None,
                "fcf_margin_pct": fcf / revenue * 100 if revenue else None,
                "da_revenue_pct": da / revenue * 100 if revenue and da is not None else None,
            }
        )
    h5 = pd.DataFrame(result).sort_values("calendar_quarter").reset_index(drop=True)

    complete = h5["is_complete_h5"] == True  # noqa: E712
    for metric, outcol in [
        ("cash_capex_usd", "cash_capex_yoy_pct"),
        ("revenue_usd", "revenue_yoy_pct"),
        ("fcf_usd", "fcf_yoy_pct"),
    ]:
        h5[outcol] = pd.NA
        lookup = {str(h5.loc[i, "calendar_quarter"]): i for i in h5.index[complete]}
        for i in h5.index[complete]:
            cq = str(h5.loc[i, "calendar_quarter"])
            prev = f"{int(cq[:4]) - 1}{cq[-2:]}"
            j = lookup.get(prev)
            if j is None:
                continue
            old = safe_float(h5.loc[j, metric])
            new = safe_float(h5.loc[i, metric])
            if old not in (None, 0) and new is not None:
                h5.loc[i, outcol] = (new / old - 1) * 100

    h5["source"] = "Derived from Yahoo Finance quarterly financial statements"
    h5["generated_at_bjt"] = now_bjt()
    return h5


def build_latest(frames: Dict[str, pd.DataFrame], h5: pd.DataFrame, errors: Dict[str, str]) -> dict:
    companies = {}
    for ticker, df in frames.items():
        x = df.dropna(subset=["revenue_usd", "cfo_usd", "cash_capex_usd"]).copy()
        if x.empty:
            continue
        x["period_end"] = pd.to_datetime(x["period_end"], errors="coerce")
        r = x.sort_values("period_end").iloc[-1]
        companies[ticker] = {
            "company": r["company"],
            "fiscal_year": int(r["fiscal_year"]),
            "fiscal_quarter": str(r["fiscal_quarter"]),
            "period_end": r["period_end"].strftime("%Y-%m-%d"),
            "calendar_quarter": r["calendar_quarter"],
            "revenue_usd_bn": round(float(r["revenue_usd"]) / 1e9, 3),
            "cfo_usd_bn": round(float(r["cfo_usd"]) / 1e9, 3),
            "cash_capex_usd_bn": round(float(r["cash_capex_usd"]) / 1e9, 3),
            "da_usd_bn": round(float(r["da_usd"]) / 1e9, 3) if pd.notna(r["da_usd"]) else None,
            "fcf_usd_bn": round(float(r["fcf_usd"]) / 1e9, 3),
            "capex_revenue_pct": round(float(r["capex_revenue_pct"]), 3),
            "fcf_margin_pct": round(float(r["fcf_margin_pct"]), 3),
        }

    complete = h5[h5["is_complete_h5"] == True].copy()  # noqa: E712
    h5_latest = None
    if not complete.empty:
        r = complete.iloc[-1]
        h5_latest = {
            "calendar_quarter": r["calendar_quarter"],
            "revenue_usd_bn": round(float(r["revenue_usd"]) / 1e9, 3),
            "cfo_usd_bn": round(float(r["cfo_usd"]) / 1e9, 3),
            "cash_capex_usd_bn": round(float(r["cash_capex_usd"]) / 1e9, 3),
            "da_usd_bn": round(float(r["da_usd"]) / 1e9, 3) if pd.notna(r["da_usd"]) else None,
            "fcf_usd_bn": round(float(r["fcf_usd"]) / 1e9, 3),
            "capex_revenue_pct": round(float(r["capex_revenue_pct"]), 3),
            "fcf_margin_pct": round(float(r["fcf_margin_pct"]), 3),
            "da_revenue_pct": round(float(r["da_revenue_pct"]), 3) if pd.notna(r["da_revenue_pct"]) else None,
            "cash_capex_yoy_pct": round(float(r["cash_capex_yoy_pct"]), 3) if pd.notna(r["cash_capex_yoy_pct"]) else None,
            "revenue_yoy_pct": round(float(r["revenue_yoy_pct"]), 3) if pd.notna(r["revenue_yoy_pct"]) else None,
            "fcf_yoy_pct": round(float(r["fcf_yoy_pct"]), 3) if pd.notna(r["fcf_yoy_pct"]) else None,
        }

    return {
        "module": "AI Bubble Monitor - Hyperscaler Financials",
        "step": 6,
        "timezone": "Asia/Shanghai",
        "generated_at_bjt": now_bjt(),
        "source_policy": {
            "automated_source": "Yahoo Finance quarterly financial statements via yfinance",
            "official_validation_source": "SEC EDGAR / company investor relations",
            "sec_github_actions_note": "data.sec.gov currently returns HTTP 403 from GitHub-hosted Azure runners; official sources will be used for validation/backfill rather than unattended runner fetches.",
        },
        "accounting_policy": {
            "cash_capex": "Absolute value of quarterly CapitalExpenditure cash-flow line; normalized as positive cash spent.",
            "fcf": "Operating Cash Flow - Cash CapEx",
            "h5_alignment": "Companies grouped by calendar quarter containing each fiscal period end; MSFT and ORCL fiscal calendars differ from calendar-year companies.",
        },
        "companies": companies,
        "h5_latest_complete": h5_latest,
        "errors": errors,
    }


def main() -> None:
    frames: Dict[str, pd.DataFrame] = {}
    errors: Dict[str, str] = {}

    for company in COMPANIES:
        print(f"=== {company.ticker}: fetching quarterly fundamentals ===")
        try:
            fresh = fetch_company(company)
            path = OUT / f"{company.ticker.lower()}.csv"
            merged = merge_history(path, fresh)
            write_csv(merged, path)
            frames[company.ticker] = merged
            core = merged.dropna(subset=["revenue_usd", "cfo_usd", "cash_capex_usd"])
            print(f"{company.ticker}: {len(merged)} stored quarters, {len(core)} complete core quarters")
        except Exception as exc:  # noqa: BLE001
            errors[company.ticker] = str(exc)
            print(f"ERROR {company.ticker}: {exc}")

    if len(frames) != 5:
        raise SystemExit(f"Only {len(frames)}/5 companies were processed: {errors}")

    h5 = build_h5(frames)
    write_csv(h5, OUT / "h5_quarterly.csv")
    latest = build_latest(frames, h5, errors)
    (OUT / "latest.json").write_text(json.dumps(latest, indent=2, ensure_ascii=False), encoding="utf-8")

    complete = h5[h5["is_complete_h5"] == True]  # noqa: E712
    print(f"H5 calendar quarters: {len(h5)}; complete H5 quarters: {len(complete)}")
    if not complete.empty:
        r = complete.iloc[-1]
        print(
            f"Latest complete {r['calendar_quarter']}: Revenue ${r['revenue_usd']/1e9:.1f}bn, "
            f"Cash CapEx ${r['cash_capex_usd']/1e9:.1f}bn, FCF ${r['fcf_usd']/1e9:.1f}bn"
        )


if __name__ == "__main__":
    main()
