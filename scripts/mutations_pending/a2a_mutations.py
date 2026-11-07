# -*- coding: utf-8 -*-
"""
a2a_mutations.py — 分析系列 A2a（趨勢面：200 日均線＋12 個月價格報酬）的突變對照（第二包起門檻在 scripts/analyze.py）。

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
ANALYZE = ["scripts/test_analyze.py"]

P_ = "js/plain.js"
CA_ = "js/card-analysis.js"
G_ = "js/glossary.js"
AN_ = "scripts/analyze.py"

# 第二包（Codex 第 1 輪採納後）：數字改在 scripts/analyze.py 的 15:30 那一輪算、放在 risk.json 的 trend；前端只讀。
# 門檻與「排除盤中點」的突變改打在 analyze.py；前端的突變打在讀 JSON 與三態、白話、規則句上。
M = [
    ("W01", "200 日均線的門檻 200 改成 199（Python 的常數）", AN_,
     "TREND_MA_DAYS = 200 ", "TREND_MA_DAYS = 199 ", ANALYZE + PLAIN),
    ("W02", "12 個月報酬的門檻少一天（253 點改成 252 點就算）", AN_,
     "    if days >= TREND_RET_DAYS + 1:", "    if days >= TREND_RET_DAYS:", ANALYZE),
    ("W03", "三態判斷反過來（向上與向下對調）", P_,
     "    var key = t.above && t.ret12mPct > 0 ? 'high' : (!t.above && t.ret12mPct < 0 ? 'low' : 'mid');",
     "    var key = t.above && t.ret12mPct > 0 ? 'low' : (!t.above && t.ret12mPct < 0 ? 'high' : 'mid');", ASPECTS + PLAIN),
    ("W04", "持平的條件改壞：任一個相反就算向下", P_,
     "    var key = t.above && t.ret12mPct > 0 ? 'high' : (!t.above && t.ret12mPct < 0 ? 'low' : 'mid');",
     "    var key = t.above && t.ret12mPct > 0 ? 'high' : (!t.above || t.ret12mPct < 0 ? 'low' : 'mid');", ASPECTS),
    ("W05", "資料不足不顯示：JSON 寫資料不足也硬給一個狀態", P_,
     "    if (!ok) return insufficient('trend', t.reason || NA, rule);", "    if (false) return insufficient('trend', t.reason || NA, rule);", ASPECTS),
    ("W06", "面向順序少了趨勢", CA_,
     "  var ASPECT_ORDER = ['position', 'risk', 'cost', 'trend'];", "  var ASPECT_ORDER = ['position', 'risk', 'cost'];", ASPECTS),
    ("W07", "趨勢的圖示從色點改成天氣", P_,
     "    trend:      { name: '趨勢', icon: 'dot', words:", "    trend:      { name: '趨勢', words:", ASPECTS + PLAIN),
    ("W08", "名詞解釋少了「200 日均線」", G_,
     "    '200 日均線': '最近 200 個交易日收盤價的平均。", "    '200 日均線（拿掉）': '最近 200 個交易日收盤價的平均。", ASPECTS + PLAIN + ANALYZE),
    ("W09", "條塊不固定資料不足（拿本站自己累積的點去算）", AN_,
     "        if a.get(\"type\") == \"bot_gold_bar\":", "        if False:", ANALYZE),
    ("W10", "這次的數字不寫「不含息」", P_,
     "'（不含息，資料到 '", "'（資料到 '", ASPECTS),
    ("W11", "RSI 少了定位那一句", G_,
     "它只描述最近的漲跌。本站只拿它描述最近漲跌，不作為判斷依據。'", "它只描述最近的漲跌。'", ASPECTS + PLAIN),
    ("W12", "規則句不以「規則：」開頭", P_,
     "    return '規則：現價高於 '", "    return '現價高於 '", ASPECTS + PLAIN),
    ("W13", "儀表板的佔位字沒有跟著加趨勢", "js/app.js",
     "位置、風險、成本、趨勢：讀取中…", "位置、風險、成本：讀取中…", ASPECTS),
    ("W14", "12 個月報酬的白話不寫不含配息", P_,
     "'；只算價格，不含配息。';", "'。';", ASPECTS + PLAIN),
    ("W15", "盤中的暫定點也算進去（不排除 provisional）", AN_,
     "    rows = [p for p in (points or []) if not p.get(\"provisional\") and", "    rows = [p for p in (points or []) if", ANALYZE),
    ("W16", "前端自己算（拿日線重算均線，不只讀 JSON）", CA_,
     "    var t = ctx.risk && ctx.risk.trend && ctx.risk.trend[asset.id];",
     "    var t = ctx.risk && ctx.risk.trend && ctx.risk.trend[asset.id]; void (global.Indicators && global.Indicators.ma(global.Indicators.closes(ctx.points || []), 200));",
     ASPECTS),
    ("W17", "risk.json 讀不到寫成資料不足（不是暫時讀不到）", CA_,
     "    if (ctx.risk === null) { out.state = Pl.unavailable('trend'); return out; }",
     "    if (ctx.risk === null) { out.state = Pl.insufficient('trend', '資料不足'); return out; }", ASPECTS),
    ("W18", "文件的門檻寫錯一個數字（跟常數對不上）", "docs/ANALYSIS.md",
     "序列不到 200 點算不出均線、不到 253 點算不出 12 個月報酬", "序列不到 200 點算不出均線、不到 252 點算不出 12 個月報酬", ANALYZE),
    ("W19", "天數寫死，不看 JSON 的 maDays", P_,
     "    return v === 0 ? '跟 ' + maDays + ' 日均線差不到 0.1%' : '比 ' + maDays + ' 日均線'",
     "    return v === 0 ? '跟 ' + maDays + ' 日均線差不到 0.1%' : '比 ' + 200 + ' 日均線'", ASPECTS),
]

MUTATIONS = M
BASELINES = [("aspects", ASPECTS), ("plain", PLAIN), ("analyze", ANALYZE)]
