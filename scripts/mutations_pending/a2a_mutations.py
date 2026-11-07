# -*- coding: utf-8 -*-
"""
a2a_mutations.py — 分析系列 A2a（趨勢面：200 日均線＋12 個月價格報酬）的突變對照。

格式跟 scripts/mutations/autopilot_mutations.py 一樣：每一筆 (編號, 白話說明, 檔案, 原文, 改成, [unittest 的參數…])，
原文必須在那個檔裡剛好出現一次（錨點）。編號用 W，跟現有的 M／N／Q／R／U／V 不撞。

為什麼放在這裡、不在 scripts/mutations/：A2a 是雲端施工、筆電接收模式收包，接收模式不收第一層（scripts/mutations/）的改動。
這一份先 commit 在這裡，Codex 審 PR 時看得到；驗收機不會跑它（驗收機只讀 scripts/mutations/autopilot_mutations.py）。
下一個一般模式的階段再併進 scripts/mutations/（內容一字不改）。

怎麼跑（突變執行器讀另一份定義檔；要瀏覽器）：
    python scripts/mutations/run_mutations.py --defs scripts/mutations_pending/a2a_mutations.py --out <倉庫外>/a2a.json --browser <Chrome 或 Edge>
"""

ASPECTS = ["scripts/test_aspects_js.py"]
PLAIN = ["scripts/test_plain_js.py"]

P_ = "js/plain.js"
CA_ = "js/card-analysis.js"
G_ = "js/glossary.js"

M = [
    ("W01", "200 日均線的門檻 200 改成 199", P_,
     "  var TREND_MA_DAYS = 200;", "  var TREND_MA_DAYS = 199;", ASPECTS + PLAIN),
    ("W02", "12 個月報酬的門檻少一天（253 點改成 252 點就算）", P_,
     "  var TREND_RET_DAYS = 252;", "  var TREND_RET_DAYS = 251;", ASPECTS + PLAIN),
    ("W03", "三態判斷反過來（向上與向下對調）", P_,
     "    var key = t.above && t.ret12mPct > 0 ? 'high' : (!t.above && t.ret12mPct < 0 ? 'low' : 'mid');",
     "    var key = t.above && t.ret12mPct > 0 ? 'low' : (!t.above && t.ret12mPct < 0 ? 'high' : 'mid');", ASPECTS + PLAIN),
    ("W04", "持平的條件改壞：任一個相反就算向下", P_,
     "    var key = t.above && t.ret12mPct > 0 ? 'high' : (!t.above && t.ret12mPct < 0 ? 'low' : 'mid');",
     "    var key = t.above && t.ret12mPct > 0 ? 'high' : (!t.above || t.ret12mPct < 0 ? 'low' : 'mid');", ASPECTS),
    ("W05", "資料不足不顯示：資料不夠也硬算一個狀態", P_,
     "    if (!t.ok) return insufficient('trend', trendShort(t.n));", "    if (false) return insufficient('trend', trendShort(t.n));", ASPECTS),
    ("W06", "面向順序少了趨勢", CA_,
     "  var ASPECT_ORDER = ['position', 'risk', 'cost', 'trend'];", "  var ASPECT_ORDER = ['position', 'risk', 'cost'];", ASPECTS),
    ("W07", "趨勢的圖示從色點改成天氣", P_,
     "    trend:      { name: '趨勢', icon: 'dot', words:", "    trend:      { name: '趨勢', words:", ASPECTS + PLAIN),
    ("W08", "名詞解釋少了「200 日均線」", G_,
     "    '200 日均線': '最近 200 個交易日收盤價的平均。", "    '200 日均線（拿掉）': '最近 200 個交易日收盤價的平均。", ASPECTS + PLAIN),
    ("W09", "條塊不固定資料不足（拿本站自己累積的點去算）", CA_,
     "    if (asset.type === 'bot_gold_bar') {\n      out.state = Pl.insufficient('trend',",
     "    if (false) {\n      out.state = Pl.insufficient('trend',", ASPECTS),
    ("W10", "這次的數字不寫「不含息」", P_,
     "'（不含息，資料到 '", "'（資料到 '", ASPECTS),
    ("W11", "RSI 少了定位那一句", G_,
     "它只描述最近的漲跌。本站只拿它描述最近漲跌，不作為判斷依據。'", "它只描述最近的漲跌。'", ASPECTS + PLAIN),
    ("W12", "規則句不以「規則：」開頭", P_,
     "  var TREND_RULE = '規則：現價高於 '", "  var TREND_RULE = '現價高於 '", ASPECTS + PLAIN),
    ("W13", "儀表板的佔位字沒有跟著加趨勢", "js/app.js",
     "位置、風險、成本、趨勢：讀取中…", "位置、風險、成本：讀取中…", ASPECTS),
    ("W14", "12 個月報酬的白話不寫不含配息", P_,
     "'；只算價格，不含配息。';", "'。';", ASPECTS + PLAIN),
    ("W15", "更新失敗時拿行情檔的日期當資料日期（不是日線最後一天）", CA_,
     "    var through = (!stale && asset.date) ||", "    var through = asset.date ||", ASPECTS),
]

MUTATIONS = M
BASELINES = [("aspects", ASPECTS), ("plain", PLAIN)]
