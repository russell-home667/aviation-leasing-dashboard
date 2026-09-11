from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_FILE = ROOT / "index.html"

text = INDEX_FILE.read_text(encoding="utf-8")

replacements = {
    '<div class="chart-subtitle" id="jetSubtitle">Jet fuel · USD/bbl</div>':
        '<div class="chart-subtitle" id="jetSubtitle">FOB Singapore indicative mid · USD/bbl</div>',
    '<div class="chart-subtitle" id="baiSubtitle">Baltic Air Freight Index</div>':
        '<div class="chart-subtitle" id="baiSubtitle">Weekly headline index · TAC Index / Baltic Exchange</div>',
    'Data source: Yahoo Finance · CME / alternative source pending · TAC Index':
        'Data source: Yahoo Finance · Public FOB Singapore jet/kerosene archive · TAC Index',
    '全球原油基准 · ICE Brent front-month reference':
        'Brent Crude Oil Last Day Financial Futures · Yahoo Finance BZ=F',
    '''return new Date(isoString).toLocaleString("zh-CN", {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false
      });''':
        '''return new Date(isoString).toLocaleString("zh-CN", {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false,
        timeZone: "Asia/Shanghai"
      });''',
    '''    const latestItem = data[data.length - 1];
    const latestValue = Number(latestItem.value);
    states[key].latest = series[series.length - 1][0];

    setPriceValue(config.valueId, latestValue, config.currency);
    updateChange(config.changeId, getChange(data), config.changePeriod);
    document.getElementById(config.dateId).textContent = latestItem.date || "--";
    document.getElementById(config.frequencyId).textContent = dataset?.frequency || config.defaultFrequency;
    document.getElementById(config.sourceId).textContent = dataset?.source || "--";
    setDatasetStatus(config.statusId, dataset?.status || "--");''':
        '''    const latestItem = data[data.length - 1];
    const latestQuote = key === "brent" ? dataset?.latest_quote : null;
    const quotePrice = Number(latestQuote?.price);
    const hasQuote = Number.isFinite(quotePrice) && latestQuote?.timestamp;
    const latestValue = hasQuote ? quotePrice : Number(latestItem.value);
    states[key].latest = series[series.length - 1][0];

    setPriceValue(config.valueId, latestValue, config.currency);

    if (hasQuote) {
      const previousClose = Number(latestItem.value);
      const quoteChange = Number.isFinite(previousClose) && previousClose !== 0
        ? (quotePrice / previousClose - 1) * 100
        : null;
      updateChange(config.changeId, quoteChange, config.changePeriod);
      document.getElementById(config.dateId).textContent = `${formatUpdatedAt(latestQuote.timestamp)} 北京时间`;
      document.getElementById(config.frequencyId).textContent = dataset?.quote_frequency || "1m source bars / 5m polling";
      document.getElementById(config.sourceId).textContent = `${dataset?.source || "Yahoo Finance"} · delayed quote`;
      setDatasetStatus(config.statusId, latestQuote?.quote_status || "DELAYED");
    } else {
      updateChange(config.changeId, getChange(data), config.changePeriod);
      document.getElementById(config.dateId).textContent = latestItem.date || "--";
      document.getElementById(config.frequencyId).textContent = dataset?.frequency || config.defaultFrequency;
      document.getElementById(config.sourceId).textContent = dataset?.source || "--";
      setDatasetStatus(config.statusId, dataset?.status || "--");
    }''',
    '''  loadData();

  window.addEventListener("resize", () => {''':
        '''  loadData();
  // Keep an open dashboard fresh without requiring a manual browser refresh.
  setInterval(loadData, 60000);

  window.addEventListener("resize", () => {''',
}

changed = False
for old, new in replacements.items():
    if old in text and old != new:
        text = text.replace(old, new)
        changed = True

if changed:
    INDEX_FILE.write_text(text, encoding="utf-8")
    print("Dashboard metadata / Beijing-time quote display updated.")
else:
    print("Dashboard metadata already current.")
