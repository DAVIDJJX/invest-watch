#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_analysis_page_js.py — 分析系列 A1 的暫時檢視頁（analysis-debug.html ＋ js/analysis-debug.js）

作法比照 test_freshness_js.py：用機器上的 Edge／Chrome 無頭模式開 scripts/test_analysis_page.html
（不連網、不讀資料檔，把假的 status／risk／decompose 塞進 render()），把 DOM 倒出來逐題檢查。
★ 找不到瀏覽器時這一組是【紅】的，不是跳過。要指定瀏覽器：環境變數 IW_BROWSER=完整路徑。

釘住的事：
  1. 最上面顯示分析上次執行時間；有錯要紅字列出——分析壞了不能讓人以為數字是新的。
  2. 風險表每個標的一列；資料不足要寫出來；10 年視窗不夠要寫幾時才夠；相關矩陣有畫出來。
  3. 每一個日期欄都非空、長得像日期。
  4. 拆解的「現在這一刻」、摘要、逐月表都有畫出來，資料標籤「估算」有顯示。
  5. 頁面上沒有任何投資判斷用語（頁尾那句固定聲明是唯一例外）。
  A1-7（小白呈現）：
  6. 最上面是一行分析狀態，接著是總覽表：每個標的一列、順序跟儀表板一樣；每一格的狀態跟卡片的面向是同一份，位置就是燈號。
  7. 總覽表可以照面向排序（同狀態照名稱、沒有狀態的永遠排最後）、可以回到預設；點一列展開數字、規則、白話。
  8. 總覽表不顯示任何計數或加總。
  9. 既有各區每個數字下面一行白話（不是空的）；相關矩陣每個標的一句；逐日／逐月明細只在摘要寫白話；表頭的名詞可以點、點了不會觸發排序。
  10. 試算結果有圖示列（風險、最像的三檔）。
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAGE = os.path.join(HERE, "test_analysis_page.html")
sys.path.insert(0, HERE)

from test_freshness_js import find_browser   # noqa: E402  同一份瀏覽器搜尋規則


def run_page():
    browser = find_browser()
    if not browser:
        raise AssertionError("找不到 Edge／Chrome，js/analysis-debug.js 的測試沒辦法跑（這一組刻意不跳過）。")
    profile = tempfile.mkdtemp(prefix="iw-browser-")
    try:
        url = "file:///" + PAGE.replace("\\", "/").lstrip("/")
        p = subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run",
                            "--user-data-dir=" + profile, "--virtual-time-budget=3000", "--dump-dom", url],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
        dom = p.stdout.decode("utf-8", "replace")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    found = re.findall(r'<pre id="result">(.*?)</pre>', dom, flags=re.S)
    if not found:
        raise AssertionError("測試頁沒有產出結果（瀏覽器：%s）。DOM 開頭：%s" % (browser, dom[:300]))
    try:
        return json.loads(html.unescape(found[-1]))
    except ValueError:
        raise AssertionError("測試頁的結果不是 JSON：%s" % found[-1][:300])


class TestAnalysisDebugJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_page()
        cls.by_name = dict((r["name"], r) for r in cls.report.get("results", []))

    def case(self, name):
        self.assertIn(name, self.by_name, "測試頁裡沒有這一題：%s" % name)
        r = self.by_name[name]
        self.assertTrue(r["ok"], "%s：%s" % (name, "；".join(r["problems"])))

    def test_page_really_ran(self):
        self.assertTrue(self.report.get("loaded"), "測試頁沒有載到 js/analysis-debug.js")
        self.assertEqual(self.report.get("total"), 38)

    # --- A1-6：換匯助手（一眼看懂、位置、成本、規則表、歷史模擬、私人設定） ---
    def test_fx_glance_row(self):
        """對照組：一眼看懂那一列不印狀態詞或不印規則 → 這一條會紅。"""
        self.case("fx_glance_row_has_icon_word_number_and_rule_in_every_cell")

    def test_fx_never_counts_aspects(self):
        """對照組：在一眼看懂那一列加一句「幾個面向怎樣」→ 這一條會紅。"""
        self.case("fx_never_counts_aspects")

    def test_fx_position_and_cost_tables(self):
        self.case("fx_position_and_cost_tables_have_numbers_dates_and_plain_lines")

    def test_fx_rule_table(self):
        """對照組：規則表少畫一檔 → 這一條會紅。"""
        self.case("fx_rule_table_marks_the_current_bucket")

    def test_fx_backtest_summary(self):
        """對照組：把視窗重疊那一句拿掉 → 這一條會紅。"""
        self.case("fx_backtest_summary_shows_decision_overlap_note_and_pure_b")

    def test_fx_fixed_notes(self):
        self.case("fx_fixed_notes_are_always_there")

    def test_fx_unset_state(self):
        self.case("fx_unset_state_shows_no_amounts_and_no_form")

    def test_fx_set_state_prints_both_numbers(self):
        """對照組：只印本月額度、不印「比例 × 月預算」與「保底」兩個數字 → 這一條會紅。"""
        self.case("fx_set_state_prints_both_numbers_and_the_larger_one")

    def test_fx_plan_keys_and_amounts_stay_out_of_attributes(self):
        """對照組：表單欄位改用設定檔的鍵名當 id、或把金額塞進 data- 屬性 → 這一條會紅。"""
        self.case("fx_plan_keys_and_amounts_never_reach_ids_or_attributes")

    def test_fx_form_round_trip(self):
        self.case("fx_form_reads_back_and_skips_blank_rows")

    def test_status_on_top(self):
        self.case("status_shows_last_run_and_errors_on_top")

    # --- A1-7：總覽表與全頁的白話 ---
    def test_overview_status_line(self):
        self.case("overview_status_line_is_on_top_and_loud_when_broken")

    def test_overview_rows_in_dashboard_order(self):
        """對照組：預設順序不照儀表板的分組 → 這一條會紅。"""
        self.case("overview_has_one_row_per_asset_in_dashboard_order")

    def test_overview_cells_are_the_card_aspects(self):
        """對照組：位置面向的字眼被改成偏貴、或不適用被寫成資料不足 → 這一條會紅。"""
        self.case("overview_cells_are_the_same_aspects_as_the_cards")

    def test_overview_sorting(self):
        """對照組：排序時把「資料不足」排到前面 → 這一條會紅。"""
        self.case("overview_sorts_by_aspect_keeps_blanks_last_and_resets")

    def test_overview_row_expands(self):
        """對照組：圖示沒有規則 → 這一條會紅。"""
        self.case("overview_row_expands_into_rule_numbers_and_plain_lines")

    def test_overview_never_counts(self):
        """對照組：總覽表顯示計數 → 這一條會紅。"""
        self.case("overview_never_counts_or_totals")

    def test_every_section_has_plain_lines(self):
        """對照組：白話模板輸出空字串 → 這一條會紅。"""
        self.case("every_section_explains_its_numbers_in_plain_words")

    def test_table_headers_have_terms(self):
        """對照組：點表頭裡的名詞也會觸發排序 → 這一條會紅。"""
        self.case("table_headers_have_clickable_terms_without_breaking_sorting")

    def test_correlation_one_sentence_per_asset(self):
        self.case("correlation_has_one_sentence_per_asset_not_per_cell")

    def test_adhoc_glance_row(self):
        self.case("adhoc_detail_gets_a_glance_row_and_plain_lines")

    def test_matrix_cells_and_colors(self):
        """對照組：矩陣少畫一列 → 這一條會紅。"""
        self.case("correlation_matrix_has_ids_squared_cells_with_numbers_first")

    def test_risk_table_sorting(self):
        self.case("risk_table_sorts_and_puts_insufficient_last")

    def test_adhoc_index_state(self):
        self.case("adhoc_index_state_lists_symbols_with_summary")

    def test_adhoc_fallback_list_and_empty_states(self):
        self.case("adhoc_no_index_fallback_lists_latest_file_per_symbol")

    def test_adhoc_detail(self):
        self.case("adhoc_detail_shows_facts_labels_and_tax_note")

    def test_risk_table(self):
        self.case("risk_table_has_one_row_per_asset")

    def test_correlation_matrix(self):
        self.case("correlation_matrix_rendered")

    def test_date_cells(self):
        """對照組：把 dateCell 的日期拿掉 → 這一條會紅。"""
        self.case("every_date_cell_is_non_empty_and_looks_like_a_date")

    def test_decompose(self):
        self.case("decompose_rendered_with_live_row_and_monthly_rows")

    def test_no_judgement_words(self):
        """對照組：在檢視頁塞一個判斷用語 → 這一條會紅。"""
        self.case("no_judgement_words_on_the_page")

    def test_missing_status_is_loud(self):
        self.case("missing_status_is_loud")

    # --- A1-2：成本、集中度 ---
    def test_cost_section(self):
        self.case("cost_section_rendered")

    def test_concentration_three_facts(self):
        """對照組：把 other 混進同向計算、或把門檻比較改壞 → 這一條會紅。"""
        self.case("concentration_three_facts_with_fake_profile")

    def test_concentration_unset(self):
        self.case("concentration_unset_state")

    def test_validation(self):
        self.case("validation_blocks_bad_numbers_and_only_warns_on_sum")

    def test_profile_key_names_never_reach_the_dom(self):
        """對照組：表單欄位改用設定檔的鍵名當 id → 這一條會紅。"""
        self.case("profile_key_names_never_reach_the_dom")

    def test_storage_and_concentration_loaded(self):
        self.case("storage_exposes_loadFile_and_saveFile")


class TestPageWiring(unittest.TestCase):
    """檔案有沒有接對線：只看原始碼，不開瀏覽器。"""

    def read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_analysis_page_loads_the_scripts_and_has_the_fixed_footer(self):
        src = self.read("analysis.html")
        self.assertIn("js/analysis.js", src)
        for dep in ("js/lock.js", "js/storage.js", "js/concentration.js"):
            self.assertIn(dep, src)
            self.assertLess(src.index(dep), src.index("js/analysis.js"))
        for sec in ("overview", "status", "fx", "risk", "cost", "decompose", "concentration", "adhoc"):
            self.assertIn('id="%s"' % sec, src)
        self.assertLess(src.index('id="overview"'), src.index('id="status"'))                   # 總覽（最上面一行就是分析狀態）在最上面
        self.assertLess(src.index('id="status"'), src.index('id="fx"'))
        self.assertIn("以上為量化整理，未經回測驗證，不構成投資建議。", src)
        self.assertIn('<a href="analysis.html" class="active">分析</a>', src)
        self.assertNotIn("暫時", src)

    def test_every_page_nav_has_the_analysis_tab(self):
        for page in ("index.html", "history.html", "analysis.html", "records.html", "settings.html"):
            self.assertIn('href="analysis.html"', self.read(page), page)

    def test_old_debug_url_redirects_instead_of_404(self):
        src = self.read("analysis-debug.html")
        self.assertIn('http-equiv="refresh"', src)
        self.assertIn("url=analysis.html", src)
        self.assertIn("location.replace('analysis.html')", src)
        self.assertIn("此頁已搬到分析分頁", src)
        self.assertNotIn("<script src=", src)                                                    # 不載任何程式，只跳轉

    def test_dashboard_first_screen_loads_seven_static_files_and_defers_the_rest(self):
        """A1-7：分析的三個檔與兩支程式要等首屏畫完才抓。對照組：boot 就抓 risk.json、或把白話層掛回 index.html → 這一條會紅。
        這裡只看原始碼的結構；真的開瀏覽器量請求數的是 test_dashboard_requests_js.py。"""
        src = self.read("index.html")
        tags = re.findall(r'<(?:link rel="stylesheet"|script src=)', src)
        self.assertEqual(len(tags), 7, "首屏靜態檔應為 7 個（css 1、Chart.js 1、js 5），得到 %d" % len(tags))
        self.assertIn("js/card-analysis.js", src)
        self.assertLess(src.index("js/card-analysis.js"), src.index("js/app.js"))
        app = self.read("js/app.js")
        boot = app[app.index("function boot()"):app.index("if (document.readyState === 'loading')")]
        self.assertNotIn("analysis", boot.lower(), "boot() 不可以自己碰任何分析檔")
        self.assertNotIn("loadDeferred", boot)
        self.assertNotIn("loadScript", boot)
        self.assertIn("loadHistories(latest).then(function () { afterFirstScreen(latest); });", boot)   # 日線都到了才算首屏畫完
        histories = app[app.index("function loadHistories("):app.index("/* 頂部那條")]
        self.assertNotIn("analysis", histories.lower())
        self.assertNotIn("loadDeferred", histories)
        self.assertIn("return Promise.all(jobs);", histories)
        self.assertEqual(app.count("fetchJSON('data/analysis/"), 3)                             # risk、cost、fx，而且都在 loadAnalysis 裡
        loader = app[app.index("  function loadAnalysis()"):app.index("  function loadDeferred()")]
        for name in ("risk", "cost", "fx"):
            self.assertIn("fetchJSON('data/analysis/%s.json')" % name, loader)
        self.assertNotIn("fetchJSON('data/analysis/decompose", app)                             # 儀表板永遠不抓拆解與週線長歷史
        self.assertNotIn("fetchJSON('data/history-long", app)
        # 會去抓那五個檔的只有 loadDeferred()；叫它的只有兩個地方：首屏畫完之後、以及使用者自己展開「分析」
        self.assertEqual(app.count("loadDeferred()"), 3)                                        # 定義 1 ＋ 呼叫 2
        after = app[app.index("  function afterFirstScreen("):app.index("  /* ---------------------------------------------------------- 分析（A1-5）")]
        self.assertIn("return loadDeferred().then(", after)
        wire = app[app.index("  function wireAnalysis("):app.index("  function toggleCard(")]
        self.assertIn("loadDeferred().then(", wire)
        self.assertEqual(app.count("afterFirstScreen(latest)"), 3)                              # 定義 1 ＋ 呼叫 2：開頁一次、按「重新讀取」一次，都接在 loadHistories 後面
        self.assertEqual(app.count("loadHistories(latest).then(function () { afterFirstScreen(latest); });"), 2)

    def test_analysis_page_has_the_overview_styles_for_phones(self):
        """A1-7：總覽表在手機上整張橫向捲動、第一欄固定。"""
        src = self.read("analysis.html")
        self.assertIn(".ana-page .ov-wrap { overflow-x: auto;", src)
        self.assertRegex(src, r"td\.ov-name \{ position: sticky; left: 0;")
        self.assertIn(".ana-page .ov-detail-grid { position: sticky;", src)

    def test_analysis_page_reads_fx_json_and_saves_the_plan_without_numbers_in_the_message(self):
        """A1-6：分析分頁多讀一檔 fx.json；私人設定存檔的 commit 訊息是固定的幾個字，不帶任何數字。"""
        src = self.read("js/analysis.js")
        boot = src[src.index("  function boot()"):src.index("  window.AnalysisDebug = ")]
        self.assertEqual(boot.count("fetchJSON('data/analysis/"), 6)
        self.assertIn("fetchJSON('data/analysis/fx.json')", boot)
        self.assertIn("loadMarket()", boot)                                                     # A1-7：總覽表多讀行情與每個標的的日線
        market = src[src.index("  function loadMarket()"):src.index("  function boot()")]
        self.assertIn("fetchJSON('data/latest.json')", market)
        self.assertIn("fetchJSON('data/history/' + id + '.json')", market)
        self.assertNotIn("history-long", market)
        wire = src[src.index("  function wireFx("):src.index("  function boot()")]
        self.assertIn("window.Storage.testConnection()", wire)
        self.assertLess(wire.index("window.Storage.testConnection()"), wire.index("window.Storage.saveFile("))
        self.assertIn("saveFile(F.FILE, plan, 'fx-plan 更新')", wire)
        self.assertIn("window.Lock.gate(", wire)
        self.assertNotIn("toISOString", wire)                                                   # 訊息裡連時間戳都不放

    def test_docs_no_longer_call_it_a_temporary_page(self):
        for rel in ("README.md", "docs/ANALYSIS.md", "index.html"):
            self.assertNotIn("暫時頁", self.read(rel), rel)
            self.assertNotIn("暫時檢視頁", self.read(rel), rel)

    def test_app_js_overseas_note_keys_on_type_not_group(self):
        src = self.read("js/app.js")
        self.assertIn("a.type === 'yahoo'", src)
        self.assertNotIn("a.group === '海外'\n          ?", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
