# -*- coding: utf-8 -*-
"""
a14_mutations.py — 分析系列 A1-4 的突變對照（00646 官方淨值回補、近一年、U1 柔和配色）。

格式跟 scripts/mutations/autopilot_mutations.py 一樣：每一筆 (編號, 白話說明, 檔案, 原文, 改成, [unittest 的參數…])，
原文必須在那個檔裡剛好出現一次（錨點）。

為什麼放在這裡、不在 scripts/mutations/：A1-4 是自動駕駛跑的，scripts/mutations/ 在自動駕駛期間動不得（第一層）。
這一份先 commit 在這裡，Codex 審 PR 時看得到；驗收機不會跑它（驗收機只讀 scripts/mutations/autopilot_mutations.py）。
下一個一般模式的階段再併進 scripts/mutations/（編號用 U，跟現有的 M／N／Q／R 不撞）。

本機怎麼跑（突變執行器讀另一份定義檔）：
    py -3.12 scripts/mutations/run_mutations.py --defs scripts/mutations_pending/a14_mutations.py --out <倉庫外>/a14.json --copy <倉庫外>/mutcopy
"""

BACKFILL = ["scripts/test_backfill_nav.py"]
YEAR = ["scripts/test_premium_year_js.py"]
THEME = ["scripts/test_theme.py"]
DASH = ["scripts/test_dashboard_requests_js.py"]

M = [
    # ---- 甲：00646 官方淨值回補（scripts/backfill_nav.py）與 analyze.py 每天只增不改地併進去
    ("U01", "回補：淨值配錯一天（e添富的日期往後挪一天）", "scripts/backfill_nav.py",
     "        navs[d] = v\n", "        navs[(datetime.strptime(d, \"%Y-%m-%d\") + timedelta(days=1)).strftime(\"%Y-%m-%d\")] = v\n", BACKFILL),
    ("U02", "回補：預估混進確定（新列帶了預估的欄位）", "scripts/backfill_nav.py",
     "\"officialTag\": \"確定\", \"officialLabel\": az.LABEL_BACKFILL, \"officialNavSeenOn\": today_str,",
     "\"officialTag\": \"確定\", \"officialLabel\": az.LABEL_BACKFILL, \"officialNavSeenOn\": today_str, \"estPremiumPct\": 0.0, \"estTag\": \"預估\",",
     BACKFILL),
    ("U03", "回補：既有的日期也補（既有列被蓋過）", "scripts/backfill_nav.py",
     "        if d in existing:\n            continue", "        if False:\n            continue", BACKFILL),
    ("U04", "analyze.py：回補的列蓋過既有的列（不是只補沒有的日期）", "scripts/analyze.py",
     "        if d and d not in by_d:\n            by_d[d] = dict(r)", "        if d:\n            by_d[d] = dict(r)", BACKFILL),
    ("U05", "回補：收盤價 0.3% 的比對關掉", "scripts/backfill_nav.py",
     "for d in both if close_diff_pct(hist_closes[d], yahoo_closes[d]) > CLOSE_THRESHOLD_PCT]", "for d in both if False]", BACKFILL),
    ("U06", "回補：官方淨值的比對關掉（既有列跟 e添富對不上也照寫）", "scripts/backfill_nav.py",
     "                if round(mine, NAV_DECIMALS) == round(navs[d], NAV_DECIMALS):", "                if True:", BACKFILL),
    ("U07", "回補的列標籤寫成別的字", "scripts/analyze.py",
     "LABEL_BACKFILL = \"回補\"", "LABEL_BACKFILL = \"單一來源\"", BACKFILL),
    ("U08", "列數上限改回 400", "scripts/analyze.py",
     "NAV_MAX_ROWS = 800 ", "NAV_MAX_ROWS = 400 ", BACKFILL),
    ("U09", "analyze.py：每天重寫檔頭時不保留回補的出處", "scripts/analyze.py",
     "    if backfill:                                                 # A1-4：併進過回補就記下出處；之後每天重寫檔頭時照樣保留\n        head[\"backfill\"] = backfill",
     "    if False:\n        head[\"backfill\"] = backfill", BACKFILL),
    ("U10", "analyze.py：近一年不切最後 252 列（拿可用的全部當近一年）", "scripts/analyze.py",
     "premium_summary(rows, \"officialPremiumPct\", window=FX_DAILY_WINDOW)", "premium_summary(rows, \"officialPremiumPct\", window=None)", BACKFILL),
    ("U11", "回補：白名單多一個主機", "scripts/backfill_nav.py",
     "HOSTS = (\"www.twse.com.tw\", \"query1.finance.yahoo.com\")", "HOSTS = (\"www.twse.com.tw\", \"query1.finance.yahoo.com\", \"mis.twse.com.tw\")", BACKFILL),
    ("U12", "回補：e添富回「安全性考量」頁也照樣發淨值的請求", "scripts/backfill_nav.py",
     "    if twse_blocked(page[:3000]):\n        raise SourceError(\"證交所回「安全性考量」頁", "    if False:\n        raise SourceError(\"證交所回「安全性考量」頁",
     BACKFILL),
    ("U13", "文件的上限跟程式不一樣（docs/ANALYSIS.md 寫回 400）", "docs/ANALYSIS.md",
     "最多留 800 列（", "最多留 400 列（", BACKFILL + ["-k", "docs"]),
    ("U24", "回補：收盤價的來源一律寫 yahoo（取自 data/history、那一點記的是證交所的也照寫 yahoo）", "scripts/backfill_nav.py",
     "\"officialClose\": close_source(d, hist_closes, hist_sources)}", "\"officialClose\": CLOSE_SOURCE}", BACKFILL),
    # ---- Codex 第 1 輪的兩條（P1）
    ("U26", "回補：預設的連線改回自動跟著轉址（下一跳送出之後才查得到主機）", "scripts/backfill_nav.py",
     "timeout=30, allow_redirects=False)", "timeout=30, allow_redirects=True)", BACKFILL),
    ("U27", "回補：回轉址也照樣往下走（不當成來源有問題）", "scripts/backfill_nav.py",
     "        if rec[\"status\"] in REDIRECT_STATUSES:\n", "        if False:\n", BACKFILL),
    ("U28", "analyze.py：回補檔讀不懂就當成沒有這個檔", "scripts/analyze.py",
     "        return [], None, \"讀不懂（%s）\" % e.__class__.__name__", "        return [], None, None", BACKFILL),
    ("U29", "analyze.py：回補檔讀不到不記進這一輪的問題清單", "scripts/analyze.py",
     "            problems.append(\"折溢價 %s：回補檔 %s %s，這一輪沒有併回補的列\" % (aid, where, bf_err))\n", "", BACKFILL),
    # ---- 頁面：成本圖示看近一年
    ("U14", "成本圖示改回看可用的全部（不看近一年）", "js/card-analysis.js",
     "    var s = year || all;", "    var s = all;", YEAR),
    ("U15", "卡片的摺疊區改回看可用的全部", "js/card-analysis.js",
     "var s = (l.tag === '預估' ? p.estimated : (p.officialYear || p.official)) || {};",
     "var s = (l.tag === '預估' ? p.estimated : p.official) || {};", YEAR),
    ("U16", "規則那一行不寫視窗", "js/plain.js",
     "    var rule = premiumRule(d.window);", "    var rule = premiumRule(null);", YEAR),
    # ---- 乙：U1 柔和配色
    ("U17", "淺色的 --text 改成低對比", "css/style.css",
     "  --text:        #2b3e4e;", "  --text:        #b8c2cc;", THEME),
    ("U18", "淺色的 --high 改回規格原本的 #b0541a（側欄底上 4.40）", "css/style.css",
     "  --high:        #a84f18;", "  --high:        #b0541a;", THEME),
    ("U19", "深色那一組少一個變數", "css/style.css",
     "  --pie-none:    #64748b;\n", "", THEME),
    ("U20", "深色那一組的值被改（不再是 A1-4 以前的原樣）", "css/style.css",
     "  --text-dim:    #8fa2bd;\n  --text-faint:  #64748b;\n\n  --up:          #ff6b6b;",
     "  --text-dim:    #8fa2bc;\n  --text-faint:  #64748b;\n\n  --up:          #ff6b6b;", THEME),
    ("U21", "charts.js 寫回一個色碼", "js/charts.js",
     "ma60: '--chart-ma60'", "ma60: '#a78bfa'", THEME),
    ("U22", "主題腳本：存了 dark 也不套深色", "index.html",
     "    if (t === 'dark') root.setAttribute('data-theme', 'dark'); else root.removeAttribute('data-theme');",
     "    if (t === 'never') root.setAttribute('data-theme', 'dark'); else root.removeAttribute('data-theme');", THEME),
    ("U23", "主題腳本：別的分頁換了主題，這一頁不跟著換", "index.html",
     "    if (e.key !== KEY) return;\n    apply(e.newValue);", "    if (e.key !== KEY) return;\n    return;", THEME),
    # ---- 首屏請求數那條既有測試（A1-4 的裁決：台銀擋住的那天照資料算，不再寫死 14 個都抓得到）
    ("U25", "首屏：這一輪抓不到的標的（例如台銀擋住）也去抓日線檔，首屏多出請求", "js/app.js",
     "      if (a.status !== 'ok') return;\n      if (!(a.points > 1) && !(isBar && a.points >= 1)) return;",
     "      if (a.status === 'ok' && !(a.points > 1) && !(isBar && a.points >= 1)) return;", DASH),
]

MUTATIONS = M
BASELINES = [("backfill", BACKFILL), ("year", YEAR), ("theme", THEME), ("dashboard", DASH)]
