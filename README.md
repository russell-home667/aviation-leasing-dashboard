# aviation-leasing-dashboard
Aircraft Leasing Market Intelligence Dashboard

Current reference-market view combines Brent Crude, Singapore Jet Kerosene and XAU/USD spot gold in one interactive chart, with BAI00 shown separately.

## News ingestion — Step 1

The repository now has a metadata-only aviation-leasing news ingestion layer. It does not store subscriber credentials or republish paid article bodies.

- **Ishka Airfinance:** official RSS only. The collector reads Latest News, five sector feeds and all eleven official regional feeds, then de-duplicates overlapping article URLs.
- **Aviation News Online:** public category-listing metadata from Leasing, Finance, Airline, Engine, Maintenance, Legal, Regulatory and People. A one-time/history workflow backfills the most recent 45 days of public listings; the rolling archive retains up to 120 days as new runs accumulate.
- **FlightGlobal:** discovered through targeted Bing News RSS searches because the direct FlightGlobal RSS endpoint returned HTTP 403 from GitHub-hosted runners.
- **Reuters:** discovered through targeted Bing News RSS searches; Reuters pages are not directly scraped.
- **Bing News:** broad discovery layer covering leasing terms, major lessors, finance structures, airline distress, engines, values/lease rates and targeted Reuters/FlightGlobal searches.

`Update Aviation Leasing News` runs hourly and writes `data/news_raw.json`, `data/news_source_health.json` and `data/news_coverage.json`. Coverage validation includes a ten-headline Ishka regression baseline and separates transport health from a search query simply returning zero current hits.

Step 2 will perform cross-publisher event clustering, semantic de-duplication and the final news taxonomy; therefore `news_raw.json` intentionally remains a broad candidate pool.
