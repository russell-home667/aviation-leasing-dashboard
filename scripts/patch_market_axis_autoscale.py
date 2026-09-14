from pathlib import Path

path = Path("index.html")
text = path.read_text(encoding="utf-8")

old = '''  function commonDataZoom() {
    return [
      { type: "inside", filterMode: "none", zoomOnMouseWheel: true, moveOnMouseMove: true, moveOnMouseWheel: false },
      {
        type: "slider", height: 18, bottom: 9,
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

new = '''  function commonDataZoom() {
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

if old not in text:
    if 'filterMode: "filter"' in text:
        print("Autoscale patch already applied")
        raise SystemExit(0)
    raise SystemExit("Expected commonDataZoom block not found; refusing unsafe patch")

path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Updated Reference Market Prices dataZoom to filter visible data for dynamic Y-axis scaling")
