#!/usr/bin/env python3
"""Apply official-source overrides to the hyperscaler quarterly database.

This is intentionally separate from the automated Yahoo fetcher. When Yahoo's
quarterly statement feed lags an already-published filing, add the confirmed
quarter to official_overrides.csv. The override replaces the same ticker/period
in the stored company CSV and then rebuilds H5 aggregates/latest.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import update_ai_hyperscaler_financials as base

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ai_bubble" / "hyperscalers"
OVERRIDES = OUT / "official_overrides.csv"


def main() -> None:
    if not OVERRIDES.exists():
        print("No official override file; nothing to do.")
        return

    ov = pd.read_csv(OVERRIDES)
    if ov.empty:
        print("Official override file is empty; nothing to do.")
        return

    for c in ["revenue_usd", "cfo_usd", "cash_capex_usd", "da_usd"]:
        ov[c] = pd.to_numeric(ov[c], errors="coerce")
    ov["period_end"] = pd.to_datetime(ov["period_end"], errors="coerce")
    ov["fcf_usd"] = ov["cfo_usd"] - ov["cash_capex_usd"]
    ov["capex_revenue_pct"] = ov["cash_capex_usd"] / ov["revenue_usd"] * 100
    ov["fcf_margin_pct"] = ov["fcf_usd"] / ov["revenue_usd"] * 100
    ov["source_symbol"] = ov["ticker"]
    ov["fetched_at_bjt"] = base.now_bjt()

    frames = {}
    applied = []
    for company in base.COMPANIES:
        path = OUT / f"{company.ticker.lower()}.csv"
        if not path.exists():
            raise SystemExit(f"Missing base company file: {path}")
        df = pd.read_csv(path)
        df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce")
        add = ov[ov["ticker"] == company.ticker].copy()
        if not add.empty:
            # Align to stored schema. Missing extra columns are created as NA; official fields replace same period.
            for col in df.columns:
                if col not in add.columns:
                    add[col] = pd.NA
            for col in add.columns:
                if col not in df.columns:
                    df[col] = pd.NA
            add = add[df.columns]
            df = pd.concat([df, add], ignore_index=True)
            df = df.sort_values("period_end").drop_duplicates(["ticker", "period_end"], keep="last")
            applied.extend([f"{company.ticker}:{d.strftime('%Y-%m-%d')}" for d in add["period_end"]])
        base.write_csv(df, path)
        frames[company.ticker] = df

    h5 = base.build_h5(frames)
    base.write_csv(h5, OUT / "h5_quarterly.csv")
    latest = base.build_latest(frames, h5, {})
    latest["official_overrides_applied"] = applied
    latest["source_policy"]["override_policy"] = (
        "Confirmed company filing/IR observations replace lagging vendor rows for the same ticker and period."
    )
    (OUT / "latest.json").write_text(json.dumps(latest, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Applied official overrides:", ", ".join(applied) if applied else "none")
    complete = h5[h5["is_complete_h5"] == True]  # noqa: E712
    if not complete.empty:
        r = complete.iloc[-1]
        print(
            f"Latest complete H5 {r['calendar_quarter']}: Revenue ${r['revenue_usd']/1e9:.1f}bn, "
            f"Cash CapEx ${r['cash_capex_usd']/1e9:.1f}bn, FCF ${r['fcf_usd']/1e9:.1f}bn"
        )


if __name__ == "__main__":
    main()
