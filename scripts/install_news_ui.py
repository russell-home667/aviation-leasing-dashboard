#!/usr/bin/env python3
"""Idempotently install the Step-3 news blocks into the existing dashboard homepage."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
CSS_MARK = "/* STEP3_NEWS_CSS */"
HTML_MARK = "<!-- STEP3_NEWS_HTML -->"
JS_MARK = "// STEP3_NEWS_JS"

CSS = r'''
    /* STEP3_NEWS_CSS */
    .news-dashboard { margin-top: 18px; display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(340px, .85fr); gap: 18px; }
    .news-card { background: linear-gradient(180deg, rgba(11,28,47,.96), rgba(7,20,35,.94)); border: 1px solid rgba(70,193,255,.27); border-radius: 14px; overflow: hidden; }
    .news-card.critical-panel { border-color: rgba(255,113,141,.25); }
    .news-panel-head { padding: 17px 19px 12px; display:flex; justify-content:space-between; align-items:flex-start; gap:14px; border-bottom:1px solid rgba(89,151,190,.12); }
    .news-panel-kicker { color:#66c8ff; font-size:10px; font-weight:800; letter-spacing:1px; margin-bottom:5px; }
    .critical-panel .news-panel-kicker { color:#ff8ea4; }
    .news-panel-title { margin:0; font-size:18px; }
    .news-panel-sub { color:#66859b; font-size:10px; margin-top:5px; line-height:1.45; }
    .news-more { color:#8fd9ff; text-decoration:none; font-size:11px; padding:6px 9px; border:1px solid rgba(70,193,255,.24); border-radius:7px; white-space:nowrap; }
    .news-list { padding: 4px 10px 9px; }
    .news-row { display:grid; grid-template-columns:44px 1fr; gap:10px; padding:10px 7px; border-bottom:1px solid rgba(89,151,190,.10); }
    .news-row:last-child { border-bottom:0; }
    .news-score { height:39px; border-radius:8px; display:flex; align-items:center; justify-content:center; font-size:16px; font-weight:800; background:rgba(70,193,255,.08); border:1px solid rgba(70,193,255,.20); }
    .critical-panel .news-score { border-color:rgba(255,113,141,.25); background:rgba(130,31,50,.10); }
    .news-tags { display:flex; flex-wrap:wrap; gap:4px; margin-bottom:4px; }
    .news-tag { color:#7f9fb3; font-size:8px; border:1px solid rgba(94,158,197,.20); border-radius:999px; padding:2px 5px; }
    .news-tag.priority { color:#ffd77c; border-color:rgba(255,210,100,.27); }
    .news-tag.macro { color:#b6a9ff; border-color:rgba(168,143,255,.26); }
    .news-tag.critical { color:#ff9db0; border-color:rgba(255,113,141,.32); }
    .news-title-link { color:#dceef8; text-decoration:none; font-size:12px; font-weight:700; line-height:1.4; }
    .news-title-link:hover { color:#74d1ff; }
    .news-summary { color:#809fb2; font-size:10px; line-height:1.45; margin-top:4px; }
    .news-meta { color:#55758a; font-size:8px; margin-top:5px; }
    .news-empty { color:#66859b; font-size:11px; padding:24px 12px; text-align:center; }
    @media (max-width: 1050px) { .news-dashboard { grid-template-columns: 1fr; } }
'''

HTML = r'''
  <!-- STEP3_NEWS_HTML -->
  <div class="news-dashboard">
    <section class="news-card">
      <div class="news-panel-head">
        <div>
          <div class="news-panel-kicker">AIRCRAFT LEASING NEWS FEED</div>
          <h2 class="news-panel-title">过去7天重要新闻</h2>
          <div class="news-panel-sub">按航空租赁重要性排序 · 重点客户/潜在客户与宏观传导优先</div>
        </div>
        <a class="news-more" href="news.html">More →</a>
      </div>
      <div class="news-list" id="homeNewsFeed"><div class="news-empty">Loading news…</div></div>
    </section>

    <section class="news-card critical-panel">
      <div class="news-panel-head">
        <div>
          <div class="news-panel-kicker">CRITICAL LEASING NEWS</div>
          <h2 class="news-panel-title">需要优先关注</h2>
          <div class="news-panel-sub">重大客户事件 · 信用/回收风险 · 系统性宏观或供给冲击</div>
        </div>
        <a class="news-more" href="news.html?critical=1">More →</a>
      </div>
      <div class="news-list" id="homeCriticalNews"><div class="news-empty">Loading critical news…</div></div>
    </section>
  </div>
'''

JS = r'''
  // STEP3_NEWS_JS
  function newsEsc(value) {
    return String(value ?? "").replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
  }

  function newsTime(value) {
    if (!value) return "--";
    try {
      return new Date(value).toLocaleString("zh-CN", { timeZone:"Asia/Shanghai", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false });
    } catch { return value; }
  }

  function newsTag(text, cls="") {
    return `<span class="news-tag ${cls}">${newsEsc(text)}</span>`;
  }

  function newsRow(story, criticalPanel=false) {
    const url = story.primary_url || story.sources?.[0]?.url || "#";
    const tags = [newsTag(story.category || "News")];
    if (story.critical) tags.push(newsTag("CRITICAL", "critical"));
    (story.priority_matches || []).slice(0,2).forEach(x => tags.push(newsTag(`重点 · ${x.name}`, "priority")));
    (story.macro_tags || []).slice(0,1).forEach(x => tags.push(newsTag(x, "macro")));
    return `<div class="news-row">
      <div class="news-score">${newsEsc(story.importance_score ?? "--")}</div>
      <div>
        <div class="news-tags">${tags.join("")}</div>
        <a class="news-title-link" href="${newsEsc(url)}" target="_blank" rel="noopener">${newsEsc(story.title)}</a>
        <div class="news-summary">${newsEsc(story.summary_zh || "")}</div>
        <div class="news-meta">${newsEsc(story.primary_source || "")} · ${newsTime(story.published_at)}${criticalPanel && story.critical_reason_zh ? ` · ${newsEsc(story.critical_reason_zh)}` : ""}</div>
      </div>
    </div>`;
  }

  async function loadNews() {
    const feedEl = document.getElementById("homeNewsFeed");
    const criticalEl = document.getElementById("homeCriticalNews");
    if (!feedEl || !criticalEl) return;
    try {
      const response = await fetch(`data/news_feed.json?t=${Date.now()}`, { cache:"no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const stories = data.stories || [];
      const byId = Object.fromEntries(stories.map(x => [x.id, x]));
      const feedIds = data.homepage?.feed_story_ids || stories.slice(0,10).map(x => x.id);
      const criticalIds = data.homepage?.critical_story_ids || stories.filter(x => x.critical).slice(0,10).map(x => x.id);
      const feed = feedIds.map(id => byId[id]).filter(Boolean).slice(0,10);
      const critical = criticalIds.map(id => byId[id]).filter(Boolean).slice(0,10);
      feedEl.innerHTML = feed.length ? feed.map(x => newsRow(x, false)).join("") : `<div class="news-empty">暂无新闻。</div>`;
      criticalEl.innerHTML = critical.length ? critical.map(x => newsRow(x, true)).join("") : `<div class="news-empty">过去7天暂无 Critical 新闻。</div>`;
    } catch (error) {
      console.error("News data error:", error);
      feedEl.innerHTML = `<div class="news-empty">新闻数据暂不可用。</div>`;
      criticalEl.innerHTML = `<div class="news-empty">Critical 新闻数据暂不可用。</div>`;
    }
  }
'''


def main() -> int:
    text = INDEX.read_text(encoding="utf-8")
    original = text

    if CSS_MARK not in text:
        if "  </style>" not in text:
            raise SystemExit("Could not find </style> insertion point")
        text = text.replace("  </style>", CSS + "\n  </style>", 1)

    if HTML_MARK not in text:
        marker = "  <div class=\"footer\">"
        if marker not in text:
            raise SystemExit("Could not find footer insertion point")
        text = text.replace(marker, HTML + "\n" + marker, 1)

    if JS_MARK not in text:
        marker = "  loadData();"
        if marker not in text:
            raise SystemExit("Could not find loadData() insertion point")
        text = text.replace(marker, JS + "\n  loadNews();\n  setInterval(loadNews, 300000);\n\n" + marker, 1)

    if text != original:
        INDEX.write_text(text, encoding="utf-8")
        print("Installed Step 3 news UI into index.html")
    else:
        print("Step 3 news UI already installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
