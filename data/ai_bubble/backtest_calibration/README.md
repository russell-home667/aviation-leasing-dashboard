# AI Bubble Monitor — Historical Backtest & Calibration

Step 12 completed on 2026-09-11 (Beijing Time).

- Long-history proxy window: 1999-present.
- Full modern Bubble Score is **not** reconstructed back to 2000 because comparable Hyperscaler AI CapEx, cloud monetization, and GPU rental histories do not exist.
- Historical Breakdown Proxy uses market trend/drawdown, long-history liquidity/credit stress, and TSMC semiconductor-demand deterioration.
- Calibrated Breakdown bands: `<45 stable`, `45-55 watch`, `55-65 deteriorating`, `65-80 breakdown`, `>=80 severe`.
- Current live scores are stored in `../top_scores/latest.json`.
- Full event results are stored in `latest.json`, `event_summary.csv`, and `monthly_proxy_history.csv`.
