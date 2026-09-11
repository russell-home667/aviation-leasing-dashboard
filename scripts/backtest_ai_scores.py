#!/usr/bin/env python3
"""Step 12: historical proxy backtest and calibration for AI Bubble Monitor.

Important methodological boundary:
- The *full* live Bubble Score contains modern-only inputs (hyperscaler AI CapEx,
  cloud monetization, GPU rental prices). It is not historically comparable to 2000.
- This script therefore builds a long-history *proxy* using market trend/heat,
  volatility/credit conditions and TSMC semiconductor demand.
- Breakdown Proxy is intended to validate the unwind mechanism and the live score
  bands; Historical Bubble Proxy is a cycle-overheat diagnostic, not a reconstructed
  probability or a replacement for the modern Bubble Score.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

BJT = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data" / "ai_bubble"
MKT = BASE / "market_liquidity"
OUT = BASE / "backtest_calibration"
OUT.mkdir(parents=True, exist_ok=True)


def clamp(x, lo=0.0, hi=100.0):
    if x is None or pd.isna(x):
        return np.nan
    return max(lo, min(hi, float(x)))


def scale(x, lo, hi, invert=False):
    if x is None or pd.isna(x) or hi == lo:
        return np.nan
    s = clamp((float(x) - lo) / (hi - lo) * 100.0)
    return 100.0 - s if invert else s


def weighted(items):
    vals = [(float(v), float(w)) for v, w in items if v is not None and not pd.isna(v)]
    if not vals:
        return np.nan
    den = sum(w for _, w in vals)
    return sum(v * w for v, w in vals) / den


def read_price(name):
    df = pd.read_csv(MKT / f"{name}.csv")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date")
    df[f"{name}_sma200"] = df["close"].rolling(200, min_periods=120).mean()
    df[f"{name}_dist200"] = (df["close"] / df[f"{name}_sma200"] - 1.0) * 100.0
    df[f"{name}_ath"] = df["close"].cummax()
    df[f"{name}_dd"] = (df["close"] / df[f"{name}_ath"] - 1.0) * 100.0
    df[f"{name}_ret252"] = df["close"].pct_change(252) * 100.0
    return df[["date", "close", f"{name}_dist200", f"{name}_dd", f"{name}_ret252"]].rename(columns={"close": f"{name}_close"})


def read_macro(name):
    df = pd.read_csv(MKT / f"{name}.csv")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    value_col = "value" if "value" in df.columns else "close"
    df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
    df = df.dropna(subset=["date", value_col]).sort_values("date")
    df[f"{name}_chg20"] = df[value_col] - df[value_col].shift(20)
    return df[["date", value_col, f"{name}_chg20"]].rename(columns={value_col: name})


def read_vix():
    df = pd.read_csv(MKT / "vix.csv")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date")
    df["vix_chg20_pct"] = df["close"].pct_change(20) * 100.0
    return df[["date", "close", "vix_chg20_pct"]].rename(columns={"close": "vix"})


def month_end(df):
    x = df.copy().sort_values("date").set_index("date")
    return x.resample("ME").last().reset_index()


def market_breakdown_row(r):
    cfg = {
        "ndx": (25.0, -15.0),
        "sox": (35.0, -20.0),
        "nvda": (40.0, -25.0),
    }
    parts = []
    for key, (dd_full, below_full) in cfg.items():
        dd = r.get(f"{key}_dd")
        dist = r.get(f"{key}_dist200")
        if pd.isna(dd) or pd.isna(dist):
            continue
        drawdown = clamp(abs(min(0.0, float(dd))) / dd_full * 100.0)
        trend = 0.0 if float(dist) >= 0 else clamp(abs(float(dist)) / abs(below_full) * 100.0)
        parts.append(0.55 * drawdown + 0.45 * trend)
    return np.nan if not parts else float(np.mean(parts))


def market_heat_row(r):
    return weighted([
        (scale(r.get("ndx_dist200"), -10, 25), 0.25),
        (scale(r.get("sox_dist200"), -15, 35), 0.25),
        (scale(r.get("nvda_dist200"), -20, 50), 0.20),
        (scale(r.get("qqq_rsp_dist200"), -5, 15), 0.15),
        (scale(r.get("ndx_ret252"), -30, 80), 0.15),
    ])


def liquidity_proxy_row(r):
    # Long-history substitute for current HY OAS: BAA10Y + VIX, with real yield
    # included only when available (2003+); weights automatically renormalize.
    return weighted([
        (scale(r.get("baa10y_proxy"), 1.0, 4.5), 0.28),
        (scale(r.get("baa10y_proxy_chg20"), -0.25, 1.5), 0.22),
        (scale(r.get("vix"), 12.0, 40.0), 0.25),
        (scale(r.get("vix_chg20_pct"), -20.0, 100.0), 0.15),
        (scale(r.get("dfii10"), 1.0, 4.0), 0.06),
        (scale(r.get("dfii10_chg20"), -0.25, 0.75), 0.04),
    ])


def crisis_flag(date):
    d = pd.Timestamp(date)
    windows = {
        "Dot-com unwind": ("2000-03-01", "2002-10-31"),
        "GFC": ("2007-10-01", "2009-03-31"),
        "COVID shock": ("2020-02-01", "2020-05-31"),
        "2022 tech/rate selloff": ("2021-11-01", "2022-10-31"),
    }
    for name, (s, e) in windows.items():
        if pd.Timestamp(s) <= d <= pd.Timestamp(e):
            return name
    return "Normal/control"


def summarize_window(df, name, start, end):
    x = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))].copy()
    if x.empty:
        return {"event": name, "start": start, "end": end, "available": False}
    idx_b = x["breakdown_proxy"].idxmax()
    idx_h = x["bubble_proxy"].idxmax()
    return {
        "event": name,
        "start": start,
        "end": end,
        "available": True,
        "max_breakdown_proxy": round(float(x.loc[idx_b, "breakdown_proxy"]), 1),
        "max_breakdown_date": x.loc[idx_b, "date"].date().isoformat(),
        "max_bubble_proxy": round(float(x.loc[idx_h, "bubble_proxy"]), 1),
        "max_bubble_date": x.loc[idx_h, "date"].date().isoformat(),
        "months_breakdown_ge_45": int((x["breakdown_proxy"] >= 45).sum()),
        "months_breakdown_ge_65": int((x["breakdown_proxy"] >= 65).sum()),
        "months_breakdown_ge_80": int((x["breakdown_proxy"] >= 80).sum()),
    }


def main():
    # Daily market series -> monthly observations after computing daily moving stats.
    merged = month_end(read_price("ndx"))
    for name in ["sox", "nvda", "qqq_rsp"]:
        p = MKT / f"{name}.csv"
        if p.exists():
            if name == "qqq_rsp":
                q = pd.read_csv(p)
                q["date"] = pd.to_datetime(q["date"], errors="coerce")
                q["close"] = pd.to_numeric(q["close"], errors="coerce")
                q = q.dropna(subset=["date", "close"]).sort_values("date")
                q["qqq_rsp_sma200"] = q["close"].rolling(200, min_periods=120).mean()
                q["qqq_rsp_dist200"] = (q["close"] / q["qqq_rsp_sma200"] - 1) * 100
                q = q[["date", "qqq_rsp_dist200"]]
                part = month_end(q)
            else:
                part = month_end(read_price(name))
            merged = pd.merge(merged, part, on="date", how="outer")

    # Macro/volatility monthly snapshots.
    for part in [month_end(read_vix()), month_end(read_macro("baa10y_proxy")), month_end(read_macro("dfii10"))]:
        merged = pd.merge(merged, part, on="date", how="outer")

    # TSMC monthly demand, available 1999+.
    ts = pd.read_csv(BASE / "compute_fundamentals" / "tsmc_monthly.csv")
    ts["date"] = pd.to_datetime(ts["month"].astype(str) + "-01", errors="coerce") + pd.offsets.MonthEnd(0)
    ts["tsmc_3m_yoy"] = pd.to_numeric(ts["rolling_3m_yoy_pct"], errors="coerce")
    ts = ts[["date", "tsmc_3m_yoy"]]
    merged = pd.merge(merged, ts, on="date", how="left")

    merged = merged.sort_values("date").reset_index(drop=True)
    # Macro series are business-day/month-end observations; short forward fill only.
    merged = merged.set_index("date").sort_index()
    merged = merged.ffill(limit=2).reset_index()
    merged = merged[merged["date"] >= pd.Timestamp("1999-01-31")].copy()

    merged["market_breakdown_proxy"] = merged.apply(market_breakdown_row, axis=1)
    merged["market_heat_proxy"] = merged.apply(market_heat_row, axis=1)
    merged["liquidity_stress_proxy"] = merged.apply(liquidity_proxy_row, axis=1)
    merged["semiconductor_strength_proxy"] = merged["tsmc_3m_yoy"].apply(lambda x: scale(x, -20, 60))
    merged["semiconductor_deterioration_proxy"] = 100.0 - merged["semiconductor_strength_proxy"]

    # Breakdown mechanism: market trend + financing stress + real semiconductor demand.
    merged["breakdown_proxy"] = merged.apply(lambda r: weighted([
        (r.get("market_breakdown_proxy"), 0.45),
        (r.get("liquidity_stress_proxy"), 0.35),
        (r.get("semiconductor_deterioration_proxy"), 0.20),
    ]), axis=1)

    # Bubble/overheat proxy: deliberately excludes modern CapEx/monetization gap.
    # It asks only whether market/semiconductor cycle conditions look euphoric.
    merged["bubble_proxy"] = merged.apply(lambda r: weighted([
        (r.get("market_heat_proxy"), 0.65),
        (r.get("semiconductor_strength_proxy"), 0.20),
        (scale(r.get("ndx_ret252"), 0, 80), 0.15),
    ]), axis=1)

    merged["event_bucket"] = merged["date"].apply(crisis_flag)

    out_cols = [
        "date", "bubble_proxy", "breakdown_proxy", "market_heat_proxy",
        "market_breakdown_proxy", "liquidity_stress_proxy",
        "semiconductor_strength_proxy", "tsmc_3m_yoy", "ndx_close", "ndx_dd",
        "ndx_dist200", "sox_dd", "sox_dist200", "vix", "baa10y_proxy",
        "dfii10", "event_bucket"
    ]
    available = [c for c in out_cols if c in merged.columns]
    merged[available].to_csv(OUT / "monthly_proxy_history.csv", index=False, date_format="%Y-%m-%d")

    events = [
        ("Dot-com build-up", "1999-01-01", "2000-03-31"),
        ("Dot-com unwind", "2000-03-01", "2002-10-31"),
        ("GFC control", "2007-10-01", "2009-03-31"),
        ("COVID control", "2020-02-01", "2020-05-31"),
        ("2022 tech/rate selloff", "2021-11-01", "2022-10-31"),
        ("AI boom to date", "2023-01-01", "2026-09-30"),
    ]
    event_summary = [summarize_window(merged, *e) for e in events]
    pd.DataFrame(event_summary).to_csv(OUT / "event_summary.csv", index=False)

    valid = merged["breakdown_proxy"].dropna()
    percentiles = {f"p{p}": round(float(np.percentile(valid, p)), 1) for p in [50, 65, 75, 80, 90, 95, 97, 99]}

    crisis = merged[merged["event_bucket"] != "Normal/control"]["breakdown_proxy"].dropna()
    normal = merged[merged["event_bucket"] == "Normal/control"]["breakdown_proxy"].dropna()
    live_bands = [25, 45, 65, 80]
    band_validation = {}
    for t in live_bands:
        band_validation[str(t)] = {
            "normal_months_above_pct": round(float((normal >= t).mean() * 100), 1) if len(normal) else None,
            "crisis_months_above_pct": round(float((crisis >= t).mean() * 100), 1) if len(crisis) else None,
            "historical_percentile_rank_pct": round(float((valid <= t).mean() * 100), 1) if len(valid) else None,
        }

    current_top = json.loads((BASE / "top_scores" / "latest.json").read_text(encoding="utf-8"))
    result = {
        "module": "AI Bubble Monitor - Historical Backtest & Calibration",
        "step": 12,
        "timezone": "Asia/Shanghai",
        "generated_at_bjt": datetime.now(BJT).isoformat(timespec="seconds"),
        "methodology": {
            "full_bubble_score_backtestable_to_2000": False,
            "reason": "Hyperscaler AI CapEx, comparable cloud monetization and GPU rental series are modern-only; reconstructing them for 2000 would create false precision.",
            "breakdown_proxy": "45% market trend/drawdown + 35% long-history liquidity/credit stress + 20% TSMC semiconductor-demand deterioration.",
            "bubble_proxy": "65% market heat + 20% TSMC semiconductor strength + 15% Nasdaq-100 12-month momentum. This is a historical overheat proxy, not the live Bubble Score.",
            "controls": "GFC and COVID are included deliberately to test whether Breakdown identifies unwind mechanics even when the shock is not an AI/technology bubble.",
        },
        "breakdown_proxy_percentiles": percentiles,
        "live_band_validation": band_validation,
        "events": event_summary,
        "current_live_scores": {
            "bubble_score": current_top["bubble_score"]["score_0_100"],
            "breakdown_score": current_top["breakdown_score"]["score_0_100"],
            "regime": current_top["regime"],
        },
        "calibration_conclusion": {
            "status": "hybrid_calibrated",
            "live_score_bands_retained": True,
            "note": "Long-history proxy is used to validate ordering/severity of Breakdown bands. Modern-only Bubble components remain transparent heuristic weights until a longer live history accumulates.",
        },
        "limitations": [
            "The proxy does not reconstruct historical AI/hyperscaler CapEx or cloud monetization.",
            "BAA10Y is used as a long-history credit proxy where licensed HY OAS history is unavailable.",
            "TSMC monthly revenue is a semiconductor-cycle proxy and is not a pure AI-demand measure before the AI era.",
            "COVID is an exogenous shock control: a high Breakdown score does not by itself prove a bubble caused the selloff.",
        ],
        "errors": {},
    }
    (OUT / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
