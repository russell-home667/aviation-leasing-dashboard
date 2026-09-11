#!/usr/bin/env python3
"""Apply Step-12 calibrated label bands without changing live score numerics.

The long-history proxy showed that the old 25-point Watch threshold was too
sensitive. Numeric component weights remain unchanged; only Breakdown severity
bands and methodology metadata are calibrated. The script also keeps the web
Dashboard methodology label synchronized with the calibrated model.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data" / "ai_bubble"
TOP = BASE / "top_scores"
BACKTEST = BASE / "backtest_calibration"
DASHBOARD = ROOT / "ai-bubble" / "index.html"

CALIBRATED_BANDS = {
    "0-45": "stable",
    "45-55": "watch",
    "55-65": "deteriorating",
    "65-80": "breakdown",
    "80-100": "severe",
}


def label(score: float) -> str:
    if score < 45:
        return "stable"
    if score < 55:
        return "watch"
    if score < 65:
        return "deteriorating"
    if score < 80:
        return "breakdown"
    return "severe"


def sync_dashboard() -> None:
    if not DASHBOARD.exists():
        return
    html = DASHBOARD.read_text(encoding="utf-8")
    html = html.replace(
        '<span class="pill">Methodology · heuristic v1 / pre-backtest</span>',
        '<span class="pill">Methodology · hybrid v2 / backtest-calibrated</span>'
    )
    if 'href="backtest.html"' not in html:
        html = html.replace(
            '<a class="navbtn" href="../">← Aviation Dashboard</a>',
            '<a class="navbtn" href="../">← Aviation Dashboard</a><a class="navbtn" href="backtest.html">Historical Backtest</a>'
        )
    html = html.replace(
        'Current weights and thresholds are heuristic v1 and will be historically calibrated in Step 12.',
        'Breakdown severity bands are historically calibrated with a 1999-present proxy backtest; Bubble Score remains a transparent modern-era heuristic because comparable 2000-era CapEx/cloud/GPU data do not exist.'
    )
    DASHBOARD.write_text(html, encoding="utf-8")


def main() -> None:
    p = TOP / "latest.json"
    if not p.exists():
        raise SystemExit("top_scores/latest.json missing")
    d = json.loads(p.read_text(encoding="utf-8"))
    score = float(d["breakdown_score"]["score_0_100"])
    d["breakdown_score"]["label"] = label(score)
    d["methodology_version"] = "hybrid_v2_backtest_calibrated"
    d["calibration_note"] = (
        "Step-12 long-history proxy backtest calibrated Breakdown severity bands. "
        "Numeric score weights are unchanged; the 25-point Watch threshold was removed "
        "because it generated excessive normal-period alerts. Bubble Score remains a "
        "transparent modern-era heuristic because comparable 2000-era CapEx/cloud/GPU data do not exist."
    )
    d.setdefault("thresholds", {})["breakdown"] = CALIBRATED_BANDS
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    hp = TOP / "history.csv"
    if hp.exists():
        with hp.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        if rows:
            rows[-1]["breakdown_label"] = label(float(rows[-1]["breakdown_score"]))
            with hp.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=rows[0].keys())
                w.writeheader(); w.writerows(rows)

    bp = BACKTEST / "latest.json"
    if bp.exists():
        b = json.loads(bp.read_text(encoding="utf-8"))
        b["calibration_conclusion"] = {
            "status": "hybrid_v2_applied",
            "live_score_bands_retained": False,
            "old_breakdown_bands": {
                "0-25": "stable", "25-45": "watch", "45-65": "deteriorating",
                "65-80": "breakdown", "80-100": "severe"
            },
            "calibrated_breakdown_bands": CALIBRATED_BANDS,
            "reason": (
                "In the long-history proxy, 25+ occurred in 71.9% of normal/control months, "
                "while 45+ occurred in 25.5% of normal months versus 74.2% of crisis months. "
                "The calibrated bands therefore use 45 as the first alert threshold, retain "
                "65 as confirmed Breakdown, and 80 as Severe."
            ),
            "bubble_score_note": (
                "Bubble Score numeric weights/bands are not statistically re-fit to 2000 because "
                "its core CapEx/monetization/GPU inputs are modern-only."
            ),
        }
        bp.write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")

    sync_dashboard()
    print(f"Applied calibrated Breakdown label: {score:.1f} -> {label(score)}")


if __name__ == "__main__":
    main()
