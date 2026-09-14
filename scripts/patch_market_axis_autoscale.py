from pathlib import Path

path = Path("index.html")
text = path.read_text(encoding="utf-8")

old_zoom = '''  function commonDataZoom() {
    return [
      {
        type: "inside",
        filterMode: "filter",
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
        moveOnMouseWheel: false
      },
      {
        type: "slider", filterMode: "filter", height: 18, bottom: 9,
        borderColor: "rgba(70, 193, 255, 0.10)",
        backgroundColor: "rgba(4, 12, 20, 0.30)",
        fillerColor: "rgba(70, 193, 255, 0.13)",
        handleStyle: { borderColor: "#46c1ff" },
        textStyle: { color: "#647f92" },
        showDetail: false
      }
    ];
  }
'''

new_zoom = '''  function commonDataZoom(filterMode = "none") {
    return [
      {
        type: "inside",
        filterMode,
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
        moveOnMouseWheel: false
      },
      {
        type: "slider", filterMode, height: 18, bottom: 9,
        borderColor: "rgba(70, 193, 255, 0.10)",
        backgroundColor: "rgba(4, 12, 20, 0.30)",
        fillerColor: "rgba(70, 193, 255, 0.13)",
        handleStyle: { borderColor: "#46c1ff" },
        textStyle: { color: "#647f92" },
        showDetail: false
      }
    ];
  }
'''

if old_zoom in text:
    text = text.replace(old_zoom, new_zoom, 1)
elif 'function commonDataZoom(filterMode = "none")' not in text:
    raise SystemExit("Expected commonDataZoom block not found; refusing unsafe patch")

market_anchor = '''  function marketChartOption(brentSeries, jetSeries, goldSeries) {'''
bai_anchor = '''  function baiChartOption(seriesData) {'''
market_start = text.find(market_anchor)
bai_start = text.find(bai_anchor)
if market_start < 0 or bai_start < 0 or bai_start <= market_start:
    raise SystemExit("Chart option boundaries not found")

market_block = text[market_start:bai_start]
if 'dataZoom: commonDataZoom("filter"),' not in market_block:
    if 'dataZoom: commonDataZoom(),' not in market_block:
        raise SystemExit("Market dataZoom call not found")
    market_block = market_block.replace('dataZoom: commonDataZoom(),', 'dataZoom: commonDataZoom("filter"),', 1)
    text = text[:market_start] + market_block + text[bai_start:]

path.write_text(text, encoding="utf-8")
print("Scoped dynamic Y-axis autoscaling to Reference Market Prices only; other charts keep filterMode=none")
