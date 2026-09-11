#!/usr/bin/env python3
"""Build standardized quarterly hyperscaler financials for the AI Bubble Monitor.

Step 6 scope:
- Microsoft, Alphabet, Amazon, Meta, Oracle
- Revenue
- Operating cash flow (CFO)
- Cash CapEx (cash paid to acquire PP&E)
- Depreciation & amortization (D&A)
- Derived FCF, CapEx/Revenue, FCF margin

Source: SEC EDGAR Company Facts API (standard us-gaap concepts only).

Important accounting convention:
Cash-flow facts in 10-Q filings are usually year-to-date. This script converts
those YTD facts into stand-alone fiscal quarters. Q4 is derived as FY minus Q3
YTD. Revenue uses stand-alone quarterly facts when available; Q4 is FY minus
Q1-Q3.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

BJT = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ai_bubble" / "hyperscalers"
OUT.mkdir(parents=True, exist_ok=True)

SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "AI-Bubble-Monitor/1.0 (github.com/russell-home667/aviation-leasing-dashboard; contact via GitHub)",
)
HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
}


@dataclass(frozen=True)
class Company:
    ticker: str
    name: str
    cik: int
    fy_end_month: int
    fy_end_day: int


COMPANIES = [
    Company("MSFT", "Microsoft", 789019, 6, 30),
    Company("GOOGL", "Alphabet", 1652044, 12, 31),
    Company("AMZN", "Amazon", 1018724, 12, 31),
    Company("META", "Meta", 1326801, 12, 31),
    Company("ORCL", "Oracle", 1341439, 5, 31),
]

# Concept aliases are ordered from preferred/current standard tag to fallbacks.
CONCEPTS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "cfo": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "cash_capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForAdditionsToPropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "da": [
        "DepreciationDepletionAndAmortization",
        "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
        "DepreciationDepletionAndAmortizationAndAccretionNet",
        "Depreciation",
    ],
}

FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A"}
SEC_BASE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


def now_bjt() -> str:
    return datetime.now(BJT).isoformat(timespec="seconds")


def get_json(url: str, attempts: int = 4) -> dict:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=HEADERS, timeout=45)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i + 1 < attempts:
                time.sleep(1.5 * (2**i))
    raise RuntimeError(f"SEC request failed after {attempts} attempts: {url}: {last}")


def safe_num(x) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def expected_period_end(company: Company, fiscal_year: int, quarter: str) -> pd.Timestamp:
    fy_end = pd.Timestamp(year=fiscal_year, month=company.fy_end_month, day=company.fy_end_day)
    months_back = {"Q1": 9, "Q2": 6, "Q3": 3, "Q4": 0, "FY": 0}[quarter]
    return fy_end - pd.DateOffset(months=months_back)


def fiscal_year_start(company: Company, fiscal_year: int) -> pd.Timestamp:
    prev_end = expected_period_end(company, fiscal_year - 1, "FY")
    return prev_end + pd.Timedelta(days=1)


def collect_concept_rows(companyfacts: dict, aliases: Iterable[str]) -> pd.DataFrame:
    facts = companyfacts.get("facts", {}).get("us-gaap", {})
    rows: List[dict] = []
    for priority, concept in enumerate(aliases):
        obj = facts.get(concept)
        if not obj:
            continue
        units = obj.get("units", {})
        # Flow metrics here are USD amounts. Prefer USD; tolerate USDm only if ever present.
        candidates = units.get("USD", [])
        for r in candidates:
            if r.get("form") not in FORMS:
                continue
            if not r.get("start") or not r.get("end"):
                continue
            value = safe_num(r.get("val"))
            if value is None:
                continue
            start = pd.to_datetime(r.get("start"), errors="coerce")
            end = pd.to_datetime(r.get("end"), errors="coerce")
            filed = pd.to_datetime(r.get("filed"), errors="coerce")
            if pd.isna(start) or pd.isna(end):
                continue
            rows.append(
                {
                    "concept": concept,
                    "concept_priority": priority,
                    "start": start,
                    "end": end,
                    "filed": filed,
                    "fy": r.get("fy"),
                    "fp": r.get("fp"),
                    "form": r.get("form"),
                    "accn": r.get("accn"),
                    "frame": r.get("frame"),
                    "value": value,
                    "duration_days": int((end - start).days) + 1,
                }
            )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def choose_fact(
    df: pd.DataFrame,
    *,
    company: Company,
    fiscal_year: int,
    quarter: str,
    mode: str,
) -> Optional[dict]:
    """Choose the best SEC fact for a fiscal period.

    mode='quarter': target a stand-alone ~3 month duration (revenue).
    mode='ytd': target fiscal-year-to-date duration (CFO/CapEx/D&A).
    mode='annual': target full fiscal year.
    """
    if df.empty:
        return None
    expected_end = expected_period_end(company, fiscal_year, quarter)
    fy_start = fiscal_year_start(company, fiscal_year)

    qnum = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 4}[quarter]
    if mode == "quarter":
        target_days = 91
    elif mode == "ytd":
        target_days = int((expected_end - fy_start).days) + 1
    elif mode == "annual":
        target_days = int((expected_period_end(company, fiscal_year, "FY") - fy_start).days) + 1
    else:
        raise ValueError(mode)

    x = df.copy()
    x["end_diff"] = (x["end"] - expected_end).abs().dt.days
    # 52/53-week calendars and weekend fiscal closes can drift; 21 days is deliberately conservative.
    x = x[x["end_diff"] <= 21]
    if x.empty:
        return None

    x["duration_diff"] = (x["duration_days"] - target_days).abs()
    if mode == "quarter":
        # Exclude obvious YTD/annual periods when looking for a stand-alone income-statement quarter.
        x = x[(x["duration_days"] >= 55) & (x["duration_days"] <= 125)]
    elif mode == "annual":
        x = x[(x["duration_days"] >= 300) & (x["duration_days"] <= 400)]
    else:
        tolerance = max(35, int(target_days * 0.20))
        x = x[x["duration_diff"] <= tolerance]
    if x.empty:
        return None

    # Prefer expected SEC form/fiscal-period metadata, but don't require it because comparative
    # facts can be refiled with a later filing's fiscal focus.
    expected_fp = "FY" if quarter in {"Q4", "FY"} and mode == "annual" else quarter
    x["fp_penalty"] = x["fp"].apply(lambda z: 0 if str(z) == expected_fp else 6)
    x["fy_penalty"] = x["fy"].apply(lambda z: 0 if str(z) == str(fiscal_year) else 4)
    x["form_penalty"] = x["form"].apply(
        lambda z: 0
        if ((mode == "annual" and str(z).startswith("10-K")) or (mode != "annual" and str(z).startswith("10-Q")))
        else 5
    )
    x["score"] = (
        x["end_diff"] * 5
        + x["duration_diff"]
        + x["concept_priority"] * 3
        + x["fp_penalty"]
        + x["fy_penalty"]
        + x["form_penalty"]
    )
    x = x.sort_values(["score", "filed"], ascending=[True, False])
    row = x.iloc[0].to_dict()
    row["target_days"] = target_days
    return row


def fiscal_year_bounds(company: Company, all_metric_dfs: Dict[str, pd.DataFrame]) -> Tuple[int, int]:
    ends: List[pd.Timestamp] = []
    for df in all_metric_dfs.values():
        if not df.empty:
            ends.extend(pd.to_datetime(df["end"], errors="coerce").dropna().tolist())
    if not ends:
        raise RuntimeError(f"No usable SEC facts for {company.ticker}")
    min_year = min(x.year for x in ends)
    max_year = max(x.year for x in ends) + 1
    # XBRL coverage is generally useful from 2009 onward; start at 2010 for cleaner quarterly history.
    return max(2010, min_year), min(datetime.now().year + 1, max_year)


def derive_company(company: Company, companyfacts: dict) -> pd.DataFrame:
    facts = {metric: collect_concept_rows(companyfacts, aliases) for metric, aliases in CONCEPTS.items()}
    start_fy, end_fy = fiscal_year_bounds(company, facts)
    rows: List[dict] = []

    for fy in range(start_fy, end_fy + 1):
        # Revenue: three stand-alone quarters + annual, derive Q4.
        rev_q: Dict[str, Optional[dict]] = {
            q: choose_fact(facts["revenue"], company=company, fiscal_year=fy, quarter=q, mode="quarter")
            for q in ("Q1", "Q2", "Q3")
        }
        rev_fy = choose_fact(facts["revenue"], company=company, fiscal_year=fy, quarter="FY", mode="annual")

        # Cash-flow metrics: Q1/Q2/Q3 are YTD in 10-Q; annual is FY.
        cumul: Dict[str, Dict[str, Optional[dict]]] = {}
        for metric in ("cfo", "cash_capex", "da"):
            cumul[metric] = {
                q: choose_fact(facts[metric], company=company, fiscal_year=fy, quarter=q, mode="ytd")
                for q in ("Q1", "Q2", "Q3")
            }
            cumul[metric]["FY"] = choose_fact(
                facts[metric], company=company, fiscal_year=fy, quarter="FY", mode="annual"
            )

        def val(r: Optional[dict]) -> Optional[float]:
            return None if r is None else safe_num(r.get("value"))

        rev_values = {q: val(rev_q[q]) for q in ("Q1", "Q2", "Q3")}
        rev_values["Q4"] = None
        if rev_fy is not None and all(rev_values[q] is not None for q in ("Q1", "Q2", "Q3")):
            rev_values["Q4"] = val(rev_fy) - sum(float(rev_values[q]) for q in ("Q1", "Q2", "Q3"))

        metric_quarters: Dict[str, Dict[str, Optional[float]]] = {}
        for metric in ("cfo", "cash_capex", "da"):
            y1, y2, y3, annual = (val(cumul[metric][k]) for k in ("Q1", "Q2", "Q3", "FY"))
            qv = {"Q1": y1, "Q2": None, "Q3": None, "Q4": None}
            if y1 is not None and y2 is not None:
                qv["Q2"] = y2 - y1
            if y2 is not None and y3 is not None:
                qv["Q3"] = y3 - y2
            if y3 is not None and annual is not None:
                qv["Q4"] = annual - y3
            metric_quarters[metric] = qv

        for q in ("Q1", "Q2", "Q3", "Q4"):
            revenue = rev_values.get(q)
            cfo = metric_quarters["cfo"].get(q)
            capex = metric_quarters["cash_capex"].get(q)
            da = metric_quarters["da"].get(q)
            # Skip periods with no useful core data.
            if revenue is None and cfo is None and capex is None:
                continue
            period_end = expected_period_end(company, fy, q)
            calendar_quarter = f"{period_end.year}Q{period_end.quarter}"
            fcf = cfo - capex if cfo is not None and capex is not None else None
            capex_rev = (capex / revenue * 100.0) if capex is not None and revenue not in (None, 0) else None
            fcf_margin = (fcf / revenue * 100.0) if fcf is not None and revenue not in (None, 0) else None

            # Best available filing date/accession for traceability.
            support: List[dict] = []
            if q in rev_q and rev_q.get(q):
                support.append(rev_q[q])
            if q == "Q4" and rev_fy:
                support.append(rev_fy)
            if q == "Q1":
                for m in cumul.values():
                    if m.get("Q1"):
                        support.append(m["Q1"])
            elif q == "Q2":
                for m in cumul.values():
                    if m.get("Q2"):
                        support.append(m["Q2"])
            elif q == "Q3":
                for m in cumul.values():
                    if m.get("Q3"):
                        support.append(m["Q3"])
            else:
                for m in cumul.values():
                    if m.get("FY"):
                        support.append(m["FY"])
            filed_dates = [pd.to_datetime(r.get("filed"), errors="coerce") for r in support]
            filed_dates = [x for x in filed_dates if not pd.isna(x)]
            latest_filed = max(filed_dates).strftime("%Y-%m-%d") if filed_dates else None
            accessions = sorted({str(r.get("accn")) for r in support if r.get("accn")})

            rows.append(
                {
                    "ticker": company.ticker,
                    "company": company.name,
                    "fiscal_year": fy,
                    "fiscal_quarter": q,
                    "period_end": period_end.strftime("%Y-%m-%d"),
                    "calendar_quarter": calendar_quarter,
                    "revenue_usd": revenue,
                    "cfo_usd": cfo,
                    "cash_capex_usd": capex,
                    "da_usd": da,
                    "fcf_usd": fcf,
                    "capex_revenue_pct": capex_rev,
                    "fcf_margin_pct": fcf_margin,
                    "latest_supporting_filing": latest_filed,
                    "supporting_accessions": ";".join(accessions),
                    "source": "SEC EDGAR Company Facts",
                    "fetched_at_bjt": now_bjt(),
                }
            )

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError(f"No quarterly rows derived for {company.ticker}")
    for col in ["revenue_usd", "cfo_usd", "cash_capex_usd", "da_usd", "fcf_usd"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # Remove clearly invalid derived negative CapEx caused by taxonomy switches/selection errors.
    df.loc[df["cash_capex_usd"] < 0, ["cash_capex_usd", "fcf_usd", "capex_revenue_pct", "fcf_margin_pct"]] = pd.NA
    return df.sort_values(["fiscal_year", "fiscal_quarter"]).reset_index(drop=True)


def write_csv(df: pd.DataFrame, path: Path) -> None:
    out = df.copy()
    # Dollar fields are stored as whole dollars; ratios retain precision.
    for c in ["revenue_usd", "cfo_usd", "cash_capex_usd", "da_usd", "fcf_usd"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").round(0)
    out.to_csv(path, index=False)


def build_h5(company_frames: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    combined = pd.concat(company_frames.values(), ignore_index=True)
    combined["period_end"] = pd.to_datetime(combined["period_end"], errors="coerce")
    # Require core Revenue/CFO/CapEx for inclusion in standardized H5 totals.
    core = combined.dropna(subset=["revenue_usd", "cfo_usd", "cash_capex_usd"]).copy()
    grouped: List[dict] = []
    for cq, g in core.groupby("calendar_quarter", sort=True):
        tickers = sorted(g["ticker"].unique().tolist())
        revenue = float(g["revenue_usd"].sum())
        cfo = float(g["cfo_usd"].sum())
        capex = float(g["cash_capex_usd"].sum())
        fcf = cfo - capex
        da_values = pd.to_numeric(g["da_usd"], errors="coerce")
        da_sum = float(da_values.sum()) if da_values.notna().any() else None
        row = {
            "calendar_quarter": cq,
            "period_end_max": g["period_end"].max().strftime("%Y-%m-%d"),
            "company_count": len(tickers),
            "companies": ";".join(tickers),
            "is_complete_h5": len(tickers) == 5,
            "revenue_usd": revenue,
            "cfo_usd": cfo,
            "cash_capex_usd": capex,
            "da_usd": da_sum,
            "fcf_usd": fcf,
            "capex_revenue_pct": capex / revenue * 100 if revenue else None,
            "fcf_margin_pct": fcf / revenue * 100 if revenue else None,
            "da_revenue_pct": da_sum / revenue * 100 if revenue and da_sum is not None else None,
        }
        grouped.append(row)
    h5 = pd.DataFrame(grouped).sort_values("calendar_quarter").reset_index(drop=True)
    # YoY calculations only on complete H5 quarters to avoid false changes caused by missing companies.
    for col, outcol in [
        ("cash_capex_usd", "cash_capex_yoy_pct"),
        ("revenue_usd", "revenue_yoy_pct"),
        ("fcf_usd", "fcf_yoy_pct"),
    ]:
        h5[outcol] = pd.NA
        complete_idx = h5.index[h5["is_complete_h5"]].tolist()
        lookup = {h5.loc[i, "calendar_quarter"]: i for i in complete_idx}
        for i in complete_idx:
            cq = str(h5.loc[i, "calendar_quarter"])
            year, q = int(cq[:4]), cq[-2:]
            prev = f"{year-1}{q}"
            j = lookup.get(prev)
            if j is None:
                continue
            old = safe_num(h5.loc[j, col])
            new = safe_num(h5.loc[i, col])
            if old not in (None, 0) and new is not None:
                h5.loc[i, outcol] = (new / old - 1.0) * 100.0
    h5["source"] = "Derived from SEC EDGAR Company Facts"
    h5["generated_at_bjt"] = now_bjt()
    return h5


def latest_summary(company_frames: Dict[str, pd.DataFrame], h5: pd.DataFrame, errors: Dict[str, str]) -> dict:
    companies = {}
    for ticker, df in company_frames.items():
        x = df.dropna(subset=["revenue_usd", "cfo_usd", "cash_capex_usd"]).copy()
        if x.empty:
            continue
        x["period_end"] = pd.to_datetime(x["period_end"], errors="coerce")
        r = x.sort_values("period_end").iloc[-1]
        companies[ticker] = {
            "company": r["company"],
            "fiscal_year": int(r["fiscal_year"]),
            "fiscal_quarter": r["fiscal_quarter"],
            "period_end": r["period_end"].strftime("%Y-%m-%d"),
            "calendar_quarter": r["calendar_quarter"],
            "revenue_usd_bn": round(float(r["revenue_usd"]) / 1e9, 3),
            "cfo_usd_bn": round(float(r["cfo_usd"]) / 1e9, 3),
            "cash_capex_usd_bn": round(float(r["cash_capex_usd"]) / 1e9, 3),
            "da_usd_bn": round(float(r["da_usd"]) / 1e9, 3) if pd.notna(r["da_usd"]) else None,
            "fcf_usd_bn": round(float(r["fcf_usd"]) / 1e9, 3) if pd.notna(r["fcf_usd"]) else None,
            "capex_revenue_pct": round(float(r["capex_revenue_pct"]), 3) if pd.notna(r["capex_revenue_pct"]) else None,
            "fcf_margin_pct": round(float(r["fcf_margin_pct"]), 3) if pd.notna(r["fcf_margin_pct"]) else None,
            "latest_supporting_filing": r["latest_supporting_filing"],
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
            "fcf_usd_bn": round(float(r["fcf_usd"]) / 1e9, 3),
            "da_usd_bn": round(float(r["da_usd"]) / 1e9, 3) if pd.notna(r["da_usd"]) else None,
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
        "accounting_policy": {
            "cash_capex": "Cash paid to acquire PP&E using standardized SEC us-gaap facts; company-specific reported CapEx definitions are not mixed into H5 totals.",
            "fcf": "CFO - standardized Cash CapEx",
            "quarterly_cashflow": "10-Q YTD cash-flow facts are differenced into stand-alone quarters; Q4 = FY - Q3 YTD.",
            "h5_alignment": "Companies are grouped by calendar quarter containing each fiscal period end; MSFT and ORCL fiscal calendars differ from calendar-year companies.",
        },
        "companies": companies,
        "h5_latest_complete": h5_latest,
        "errors": errors,
    }


def main() -> None:
    frames: Dict[str, pd.DataFrame] = {}
    errors: Dict[str, str] = {}

    for company in COMPANIES:
        print(f"=== {company.ticker}: fetching SEC Company Facts ===")
        try:
            payload = get_json(SEC_BASE.format(cik=company.cik))
            df = derive_company(company, payload)
            write_csv(df, OUT / f"{company.ticker.lower()}.csv")
            frames[company.ticker] = df
            core_rows = df.dropna(subset=["revenue_usd", "cfo_usd", "cash_capex_usd"])
            print(f"{company.ticker}: {len(df)} quarterly rows, {len(core_rows)} with full core metrics")
        except Exception as exc:  # noqa: BLE001
            errors[company.ticker] = str(exc)
            print(f"ERROR {company.ticker}: {exc}")
        time.sleep(0.15)  # polite SEC pacing, far below SEC request-rate limits

    if len(frames) < 5:
        raise SystemExit(f"Only {len(frames)}/5 companies were successfully processed: {errors}")

    h5 = build_h5(frames)
    write_csv(h5, OUT / "h5_quarterly.csv")
    summary = latest_summary(frames, h5, errors)
    (OUT / "latest.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    latest_complete = h5[h5["is_complete_h5"] == True]  # noqa: E712
    print(f"H5 rows: {len(h5)}; complete H5 quarters: {len(latest_complete)}")
    if not latest_complete.empty:
        r = latest_complete.iloc[-1]
        print(
            f"Latest complete H5 {r['calendar_quarter']}: "
            f"Revenue ${r['revenue_usd']/1e9:.1f}bn, CapEx ${r['cash_capex_usd']/1e9:.1f}bn, "
            f"FCF ${r['fcf_usd']/1e9:.1f}bn"
        )
    print(f"Wrote {OUT / 'latest.json'}")


if __name__ == "__main__":
    main()
