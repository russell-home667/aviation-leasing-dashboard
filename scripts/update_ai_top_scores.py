#!/usr/bin/env python3
"""Step 10: Calculate top-level AI Bubble Score and AI Breakdown Score.

Inputs:
  data/ai_bubble/derived_indicators/latest.json
  data/ai_bubble/market_liquidity/latest.json
  data/ai_bubble/hyperscalers/latest.json

Outputs:
  data/ai_bubble/top_scores/latest.json
  data/ai_bubble/top_scores/history.csv

The two scores intentionally answer different questions:
- Bubble Score: how stretched/excessive the AI investment/valuation cycle is.
- Breakdown Score: whether the boom is actually entering a self-reinforcing unwind.

Thresholds are heuristic_v1 and will be calibrated in Step 12.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BJT = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "ai_bubble"
OUT = DATA / "top_scores"
OUT.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(x)))


def linear(value: float, low: float, high: float) -> float:
    if high == low:
        return 0.0
    return clamp((float(value) - low) / (high - low) * 100.0)


def weighted(parts: dict[str, tuple[float, float]]) -> float:
    num = sum(score * weight for score, weight in parts.values())
    den = sum(weight for _, weight in parts.values())
    return round(num / den, 1) if den else 0.0


def bubble_label(score: float) -> str:
    if score < 30:
        return "normal"
    if score < 50:
        return "elevated"
    if score < 70:
        return "high"
    if score < 85:
        return "bubble"
    return "extreme"


def breakdown_label(score: float) -> str:
    if score < 25:
        return "stable"
    if score < 45:
        return "watch"
    if score < 65:
        return "deteriorating"
    if score < 80:
        return "breakdown"
    return "severe"


def determine_regime(
    bubble: float,
    breakdown: float,
    cloud_growth: float,
    compute_strength: float,
    nvidia_strength: float,
) -> tuple[str, str]:
    """Map current conditions into the four regimes locked in Step 1.

    Fundamental Divergence is reserved for weakening monetization / compute
    fundamentals while excess investment remains elevated. Strong demand with a
    high Bubble Score is still Speculative Expansion, even if cash-flow pressure
    is already visible.
    """
    if breakdown >= 65:
        return (
            "Bubble Breakdown",
            "Market/credit/fundamental unwind has reached multi-pillar breakdown territory.",
        )

    fundamentals_weakening = (
        cloud_growth < 30.0
        or compute_strength < 50.0
        or nvidia_strength < 45.0
    )
    if bubble >= 50 and fundamentals_weakening:
        return (
            "Fundamental Divergence",
            "Investment excess remains elevated while monetization or compute fundamentals are weakening.",
        )

    if bubble >= 50:
        return (
            "Speculative Expansion",
            "Investment/capital-return excess is elevated, but cloud monetization and compute demand remain strong enough that breakdown is not confirmed.",
        )

    return (
        "Healthy Expansion",
        "Investment growth is not yet extreme relative to monetization, cash return and market conditions.",
    )


def market_breakdown_score(market: dict) -> tuple[float, dict]:
    """Trend/drawdown stress from NDX, SOX and NVDA.

    Drawdown is deliberately combined with distance from 200DMA. A large drawdown
    while still above the 200DMA is treated as a warning, not confirmed breakdown.
    """
    cfg = {
        "ndx": {"drawdown_full": 25.0, "below_200_full": -15.0},
        "sox": {"drawdown_full": 35.0, "below_200_full": -20.0},
        "nvda": {"drawdown_full": 40.0, "below_200_full": -25.0},
    }
    detail = {}
    scores = []
    for key, c in cfg.items():
        x = market.get(key, {})
        dd = abs(min(0.0, float(x.get("ath_drawdown_pct") or 0.0)))
        dist = float(x.get("distance_from_200dma_pct") or 0.0)
        drawdown_score = clamp(dd / c["drawdown_full"] * 100.0)
        trend_score = 0.0 if dist >= 0 else clamp(abs(dist) / abs(c["below_200_full"]) * 100.0)
        score = 0.55 * drawdown_score + 0.45 * trend_score
        detail[key] = {
            "ath_drawdown_pct": round(-dd, 2),
            "distance_from_200dma_pct": round(dist, 2),
            "drawdown_stress": round(drawdown_score, 1),
            "trend_stress": round(trend_score, 1),
            "combined_stress": round(score, 1),
        }
        scores.append(score)
    return round(sum(scores) / len(scores), 1), detail


def append_history(payload: dict) -> None:
    path = OUT / "history.csv"
    row = {
        "date_bjt": datetime.now(BJT).date().isoformat(),
        "generated_at_bjt": payload["generated_at_bjt"],
        "bubble_score": payload["bubble_score"]["score_0_100"],
        "bubble_label": payload["bubble_score"]["label"],
        "breakdown_score": payload["breakdown_score"]["score_0_100"],
        "breakdown_label": payload["breakdown_score"]["label"],
        "regime": payload["regime"],
        "coverage_pct": payload["coverage_pct"],
    }
    rows = []
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    rows = [r for r in rows if r.get("date_bjt") != row["date_bjt"]]
    rows.append({k: str(v) for k, v in row.items()})
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    derived = load_json(DATA / "derived_indicators" / "latest.json")
    market_doc = load_json(DATA / "market_liquidity" / "latest.json")
    hyper = load_json(DATA / "hyperscalers" / "latest.json")

    if derived.get("errors") or market_doc.get("errors") or hyper.get("errors"):
        raise SystemExit("Upstream AI data contains errors; refusing to publish top scores")

    d = derived["indicators"]
    market = market_doc["indicators"]
    h5 = hyper["h5_latest_complete"]

    # ---------------- Bubble Score ----------------
    inv_monet = float(d["investment_monetization_gap"]["risk_score_0_100"])
    inv_cash = float(d["investment_cash_flow_gap"]["risk_score_0_100"])
    price_fund = float(d["price_fundamental_gap"]["risk_score_0_100"])
    market_heat = float(d["price_fundamental_gap"]["market_heat_score"])

    capex_intensity = linear(float(h5["capex_revenue_pct"]), 15.0, 45.0)
    da_burden = linear(float(h5["da_revenue_pct"]), 5.0, 15.0)
    capital_burden = round(0.7 * capex_intensity + 0.3 * da_burden, 1)

    bubble_parts = {
        "investment_monetization_gap": (inv_monet, 0.30),
        "investment_cash_flow_gap": (inv_cash, 0.25),
        "market_heat": (market_heat, 0.20),
        "capital_burden": (capital_burden, 0.15),
        "price_fundamental_excess": (price_fund, 0.10),
    }
    bubble = weighted(bubble_parts)

    # ---------------- Breakdown Score ----------------
    liquidity = float(d["liquidity_stress_score"]["score_0_100"])
    nvidia_strength = float(d["nvidia_demand_quality_score"]["score_0_100"])
    compute_strength = float(d["compute_demand_score"]["score_0_100"])
    fundamental_deterioration = round(
        0.55 * (100.0 - compute_strength) + 0.45 * (100.0 - nvidia_strength), 1
    )

    market_breakdown, market_breakdown_detail = market_breakdown_score(market)

    fcf_yoy = float(h5["fcf_yoy_pct"])
    cashflow_deterioration = clamp(-fcf_yoy)

    cloud_growth = float(d["investment_monetization_gap"]["cloud_monetization_median_yoy_pct"])
    monetization_deterioration = clamp((30.0 - cloud_growth) / 30.0 * 100.0)

    breakdown_parts = {
        "liquidity_stress": (liquidity, 0.25),
        "market_trend_breakdown": (market_breakdown, 0.30),
        "fundamental_deterioration": (fundamental_deterioration, 0.25),
        "cashflow_deterioration": (cashflow_deterioration, 0.10),
        "monetization_deterioration": (monetization_deterioration, 0.10),
    }
    breakdown = weighted(breakdown_parts)

    compute_coverage = float(d["compute_demand_score"].get("coverage_pct", 100.0))
    overall_coverage = round(100.0 - (100.0 - compute_coverage) * 0.25, 1)

    regime, regime_detail = determine_regime(
        bubble=bubble,
        breakdown=breakdown,
        cloud_growth=cloud_growth,
        compute_strength=compute_strength,
        nvidia_strength=nvidia_strength,
    )
    generated = datetime.now(BJT).isoformat(timespec="seconds")

    payload = {
        "module": "AI Bubble Monitor - Top Scores",
        "step": 10,
        "timezone": "Asia/Shanghai",
        "generated_at_bjt": generated,
        "methodology_version": "heuristic_v1_pre_backtest",
        "calibration_note": "Weights and thresholds are deliberately transparent but provisional. Step 12 will backtest/calibrate them; current scores are monitoring signals, not probabilities.",
        "bubble_score": {
            "score_0_100": bubble,
            "label": bubble_label(bubble),
            "question": "How stretched/excessive is the AI investment and valuation cycle?",
            "components": {
                k: {"score_0_100": round(v[0], 1), "weight_pct": round(v[1] * 100)}
                for k, v in bubble_parts.items()
            },
            "capital_burden_detail": {
                "h5_capex_revenue_pct": round(float(h5["capex_revenue_pct"]), 2),
                "h5_da_revenue_pct": round(float(h5["da_revenue_pct"]), 2),
                "capex_intensity_score": round(capex_intensity, 1),
                "da_burden_score": round(da_burden, 1),
            },
        },
        "breakdown_score": {
            "score_0_100": breakdown,
            "label": breakdown_label(breakdown),
            "question": "Has the AI boom started a self-reinforcing fundamental/market/credit unwind?",
            "components": {
                k: {"score_0_100": round(v[0], 1), "weight_pct": round(v[1] * 100)}
                for k, v in breakdown_parts.items()
            },
            "market_breakdown_detail": market_breakdown_detail,
            "compute_demand_input_coverage_pct": compute_coverage,
        },
        "regime": regime,
        "regime_detail": regime_detail,
        "regime_framework": [
            "Healthy Expansion",
            "Speculative Expansion",
            "Fundamental Divergence",
            "Bubble Breakdown",
        ],
        "coverage_pct": overall_coverage,
        "interpretation": {
            "bubble_high_breakdown_low": "Excess/overinvestment is elevated, but demand, market trend and financing have not jointly confirmed a bust.",
            "bubble_high_breakdown_high": "A stretched cycle is also showing multi-pillar unwind confirmation; this is the dangerous combination.",
            "current": (
                "Capital-return divergence is elevated while compute demand remains strong and liquidity/market breakdown confirmation is limited."
                if bubble >= 50 and breakdown < 45
                else "Read Bubble Score and Breakdown Score separately; neither is a probability forecast."
            ),
        },
        "thresholds": {
            "bubble": {"0-30": "normal", "30-50": "elevated", "50-70": "high", "70-85": "bubble", "85-100": "extreme"},
            "breakdown": {"0-25": "stable", "25-45": "watch", "45-65": "deteriorating", "65-80": "breakdown", "80-100": "severe"},
        },
        "source_generated_at": {
            "derived_indicators": derived.get("generated_at_bjt"),
            "market_liquidity": market_doc.get("generated_at_bjt"),
            "hyperscalers": hyper.get("generated_at_bjt"),
        },
        "errors": {},
    }

    (OUT / "latest.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    append_history(payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
