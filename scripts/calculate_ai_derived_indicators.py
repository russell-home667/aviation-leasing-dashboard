#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import pandas as pd

BJT = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data" / "ai_bubble"
OUT = BASE / "derived_indicators"
OUT.mkdir(parents=True, exist_ok=True)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(x)))


def scale(x, lo, hi, invert=False):
    if x is None or hi == lo:
        return None
    s = clamp((float(x) - lo) / (hi - lo) * 100.0)
    return 100.0 - s if invert else s


def weighted(parts):
    vals = [(v, w) for v, w in parts if v is not None]
    if not vals:
        return None, 0.0
    den = sum(w for _, w in vals)
    return sum(v * w for v, w in vals) / den, den


def risk_label(x):
    if x is None: return "unavailable"
    if x < 30: return "low"
    if x < 50: return "elevated"
    if x < 70: return "high"
    return "severe"


def strength_label(x):
    if x is None: return "unavailable"
    if x < 30: return "weak"
    if x < 50: return "soft"
    if x < 70: return "healthy"
    return "strong"


def company_capex_growth(latest_q):
    tickers = ["msft", "googl", "amzn", "orcl"]
    q = int(latest_q[-1])
    y = int(latest_q[:4])
    prior = f"{y-1}Q{q}"
    cur_sum = 0.0; prev_sum = 0.0; cur_n = 0; prev_n = 0
    detail = {}
    for t in tickers:
        df = pd.read_csv(BASE / "hyperscalers" / f"{t}.csv")
        cur = df[df["calendar_quarter"] == latest_q]
        old = df[df["calendar_quarter"] == prior]
        if not cur.empty:
            v = float(cur.iloc[-1]["cash_capex_usd"]) / 1e9
            cur_sum += v; cur_n += 1; detail[t.upper()] = {"current_capex_usd_bn": round(v,3)}
        if not old.empty:
            v = float(old.iloc[-1]["cash_capex_usd"]) / 1e9
            prev_sum += v; prev_n += 1; detail.setdefault(t.upper(), {})["prior_capex_usd_bn"] = round(v,3)
    growth = ((cur_sum / prev_sum) - 1) * 100 if cur_n == 4 and prev_n == 4 and prev_sum else None
    return growth, cur_sum, prev_sum, detail


def main():
    market = load_json(BASE / "market_liquidity" / "latest.json")
    hyper = load_json(BASE / "hyperscalers" / "latest.json")
    compute = load_json(BASE / "compute_fundamentals" / "latest.json")
    gpu = load_json(BASE / "gpu_rental_prices" / "latest.json")
    ind = market["indicators"]
    h5 = hyper["h5_latest_complete"]
    nv = compute["nvda"]
    ts = compute["tsmc"]
    latest_q = h5["calendar_quarter"]

    # 1) Investment–Monetization Gap (H4 excludes Meta because it has no directly comparable cloud segment).
    h4_growth, h4_cur, h4_prev, h4_detail = company_capex_growth(latest_q)
    cloud = pd.read_csv(BASE / "cloud_monetization" / "official_quarterly.csv")
    cq = cloud[cloud["calendar_quarter"] == latest_q]
    cloud_vals = [float(x) for x in cq["growth_yoy_pct"].dropna().tolist()]
    cloud_median = median(cloud_vals) if len(cloud_vals) == 4 else None
    im_gap = h4_growth - cloud_median if h4_growth is not None and cloud_median is not None else None
    im_risk = scale(im_gap, -20, 60) if im_gap is not None else None

    # 2) Investment–Cash Flow Gap.
    ic_gap = float(h5["cash_capex_yoy_pct"]) - float(h5["fcf_yoy_pct"])
    ic_risk = scale(ic_gap, 0, 200)

    # 3) Price–Fundamental Gap. Positive means prices/market heat are ahead of operating fundamentals.
    market_heat, _ = weighted([
        (scale(ind["ndx"]["distance_from_200dma_pct"], -10, 25), 0.25),
        (scale(ind["sox"]["distance_from_200dma_pct"], -15, 35), 0.25),
        (scale(ind["nvda"]["distance_from_200dma_pct"], -20, 50), 0.25),
        (scale(ind["qqq_rsp"]["distance_from_200dma_pct"], -5, 15), 0.25),
    ])
    fundamental_heat, _ = weighted([
        (scale(nv["data_center_yoy_pct"], 0, 150), 0.45),
        (scale(nv["data_center_qoq_pct"], -10, 30), 0.20),
        (scale(ts["rolling_3m_yoy_pct"], -20, 60), 0.35),
    ])
    pf_gap = market_heat - fundamental_heat if market_heat is not None and fundamental_heat is not None else None
    pf_risk = scale(pf_gap, -30, 40) if pf_gap is not None else None

    # 4) Liquidity Stress Score. Higher = tighter/riskier financing conditions.
    liquidity, _ = weighted([
        (scale(ind["dfii10"]["value"], 1.0, 4.0), 0.20),
        (scale(ind["dfii10"]["change_20obs"], -0.25, 0.75), 0.10),
        (scale(ind["hy_oas"]["value"], 2.0, 8.0), 0.25),
        (scale(ind["hy_oas"]["change_20obs"], -0.25, 1.50), 0.20),
        (scale(ind["vix"]["value"], 12.0, 40.0), 0.15),
        (scale(ind["vix"]["change_20d_pct"], -20.0, 100.0), 0.10),
    ])

    # 5) NVIDIA Demand Quality Score. Higher = better demand quality.
    nv_quality, _ = weighted([
        (scale(nv["data_center_yoy_pct"], 0, 150), 0.30),
        (scale(nv["data_center_qoq_pct"], -10, 30), 0.15),
        (scale(nv["gross_margin_pct"], 55, 80), 0.20),
        (scale(nv["inventory_days"], 60, 150, invert=True), 0.20),
        (scale(nv["dso_days"], 35, 80, invert=True), 0.15),
    ])

    # 6) Compute Demand Score. GPU component is included only when a same-source 30D history exists.
    gpu30 = gpu.get("composite", {}).get("change_30d_pct")
    compute_demand, coverage_weight = weighted([
        (scale(nv["data_center_yoy_pct"], 0, 150), 0.40),
        (scale(ts["rolling_3m_yoy_pct"], -20, 60), 0.30),
        (scale(gpu30, -40, 20), 0.30),
    ])
    coverage_pct = coverage_weight * 100

    result = {
        "module": "AI Bubble Monitor - Derived Indicators",
        "step": 9,
        "timezone": "Asia/Shanghai",
        "generated_at_bjt": datetime.now(BJT).isoformat(timespec="seconds"),
        "methodology_version": "heuristic_v1_pre_backtest",
        "calibration_note": "Thresholds are provisional heuristics. Step 12 will backtest/calibrate them against historical stress periods.",
        "latest_complete_quarter": latest_q,
        "indicators": {
            "investment_monetization_gap": {
                "value_ppt": round(im_gap, 2) if im_gap is not None else None,
                "h4_cash_capex_yoy_pct": round(h4_growth, 2) if h4_growth is not None else None,
                "cloud_monetization_median_yoy_pct": round(cloud_median, 2) if cloud_median is not None else None,
                "risk_score_0_100": round(im_risk, 1) if im_risk is not None else None,
                "risk_label": risk_label(im_risk),
                "interpretation": "Positive values mean cloud-infrastructure cash CapEx is growing faster than median cloud monetization growth.",
                "h4_current_capex_usd_bn": round(h4_cur,3),
                "h4_prior_year_capex_usd_bn": round(h4_prev,3),
                "h4_detail": h4_detail,
                "cloud_components": cq[["company","metric","growth_yoy_pct"]].to_dict("records"),
            },
            "investment_cash_flow_gap": {
                "value_ppt": round(ic_gap, 2),
                "h5_cash_capex_yoy_pct": h5["cash_capex_yoy_pct"],
                "h5_fcf_yoy_pct": h5["fcf_yoy_pct"],
                "risk_score_0_100": round(ic_risk,1),
                "risk_label": risk_label(ic_risk),
            },
            "price_fundamental_gap": {
                "value_score_points": round(pf_gap,1) if pf_gap is not None else None,
                "market_heat_score": round(market_heat,1) if market_heat is not None else None,
                "fundamental_heat_score": round(fundamental_heat,1) if fundamental_heat is not None else None,
                "risk_score_0_100": round(pf_risk,1) if pf_risk is not None else None,
                "risk_label": risk_label(pf_risk),
                "interpretation": "Positive gap = price/market heat ahead of fundamentals; negative gap = fundamentals stronger than price heat.",
            },
            "liquidity_stress_score": {
                "score_0_100": round(liquidity,1),
                "risk_label": risk_label(liquidity),
            },
            "nvidia_demand_quality_score": {
                "score_0_100": round(nv_quality,1),
                "strength_label": strength_label(nv_quality),
            },
            "compute_demand_score": {
                "score_0_100": round(compute_demand,1),
                "strength_label": strength_label(compute_demand),
                "coverage_pct": round(coverage_pct,0),
                "gpu_30d_component_available": gpu30 is not None,
                "note": "GPU weight is automatically excluded and remaining weights renormalized until 30 days of same-source GPU history exist.",
            },
        },
        "source_generated_at": {
            "market_liquidity": market.get("generated_at_bjt"),
            "hyperscalers": hyper.get("generated_at_bjt"),
            "compute_fundamentals": compute.get("generated_at_bjt"),
            "gpu_rental_prices": gpu.get("generated_at_bjt"),
        },
        "errors": {},
    }

    (OUT / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    row = {
        "date_bjt": datetime.now(BJT).date().isoformat(),
        "generated_at_bjt": result["generated_at_bjt"],
        "investment_monetization_gap_ppt": result["indicators"]["investment_monetization_gap"]["value_ppt"],
        "investment_cash_flow_gap_ppt": result["indicators"]["investment_cash_flow_gap"]["value_ppt"],
        "price_fundamental_gap_points": result["indicators"]["price_fundamental_gap"]["value_score_points"],
        "liquidity_stress_score": result["indicators"]["liquidity_stress_score"]["score_0_100"],
        "nvidia_demand_quality_score": result["indicators"]["nvidia_demand_quality_score"]["score_0_100"],
        "compute_demand_score": result["indicators"]["compute_demand_score"]["score_0_100"],
        "compute_coverage_pct": result["indicators"]["compute_demand_score"]["coverage_pct"],
    }
    hp = OUT / "history.csv"
    if hp.exists():
        hist = pd.read_csv(hp)
        hist = hist[hist["date_bjt"] != row["date_bjt"]]
        hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    else:
        hist = pd.DataFrame([row])
    hist.sort_values("date_bjt").to_csv(hp, index=False)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
