#!/usr/bin/env python3
"""Canonicalize NVIDIA fiscal period-end dates to official disclosure dates."""
from pathlib import Path
import json
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ai_bubble" / "compute_fundamentals"
CSV = OUT / "nvda_quarterly.csv"
OV = OUT / "nvda_official_overrides.csv"
LATEST = OUT / "latest.json"

if not CSV.exists() or not OV.exists():
    raise SystemExit(0)

df = pd.read_csv(CSV)
ov = pd.read_csv(OV)
df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce")
ov["period_end"] = pd.to_datetime(ov["period_end"], errors="coerce")

for d in ov["period_end"].dropna():
    delta = (df["period_end"] - d).abs().dt.days
    if len(delta) and delta.min() <= 10:
        idx = delta.idxmin()
        df.at[idx, "period_end"] = d

df = df.drop_duplicates(subset=["period_end"], keep="last").sort_values("period_end")
df["period_end"] = df["period_end"].dt.strftime("%Y-%m-%d")
df.to_csv(CSV, index=False)

if LATEST.exists() and not df.empty:
    payload = json.loads(LATEST.read_text(encoding="utf-8"))
    if payload.get("nvda") is not None:
        payload["nvda"]["period_end"] = str(df.iloc[-1]["period_end"])
    LATEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
