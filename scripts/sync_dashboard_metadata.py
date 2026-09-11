from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_FILE = ROOT / "index.html"

text = INDEX_FILE.read_text(encoding="utf-8")

replacements = {
    '<div class="chart-subtitle" id="jetSubtitle">Jet fuel · USD/bbl</div>':
        '<div class="chart-subtitle" id="jetSubtitle">FOB Singapore indicative mid · USD/bbl</div>',
    'Data source: Yahoo Finance · CME / alternative source pending · TAC Index':
        'Data source: Yahoo Finance · Public FOB Singapore jet/kerosene archive · TAC Index',
}

changed = False
for old, new in replacements.items():
    if old in text:
        text = text.replace(old, new)
        changed = True

if changed:
    INDEX_FILE.write_text(text, encoding="utf-8")
    print("Dashboard source labels updated.")
else:
    print("Dashboard source labels already current.")
