from pathlib import Path

path = Path("index.html")
text = path.read_text(encoding="utf-8")

market_start = text.find('function marketChartOption')
bai_start = text.find('function baiChartOption')
status_start = text.find('function setDatasetStatus')
if min(market_start, bai_start, status_start) < 0 or not (market_start < bai_start < status_start):
    raise SystemExit('Chart function boundaries not found; refusing unsafe patch')

market_block = text[market_start:bai_start]
if 'dataZoom: commonDataZoom("filter")' not in market_block:
    raise SystemExit('Reference Market Prices is not using visible-range filtering')

bai_block = text[bai_start:status_start]
old = 'dataZoom: commonDataZoom(),'
new = 'dataZoom: commonDataZoom("filter"),'
if new not in bai_block:
    if old not in bai_block:
        raise SystemExit('BAI dataZoom call not found')
    bai_block = bai_block.replace(old, new, 1)
    text = text[:bai_start] + bai_block + text[status_start:]

path.write_text(text, encoding="utf-8")
print('All aviation dashboard time-series charts now autoscale Y axes to the visible X range.')
