#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_analysis_debug_js.py — 分析系列 A1 的暫時檢視頁（analysis-debug.html ＋ js/analysis-debug.js）

作法比照 test_freshness_js.py：用機器上的 Edge／Chrome 無頭模式開 scripts/test_analysis_debug.html
（不連網、不讀資料檔，把假的 status／risk／decompose 塞進 render()），把 DOM 倒出來逐題檢查。
★ 找不到瀏覽器時這一組是【紅】的，不是跳過。要指定瀏覽器：環境變數 IW_BROWSER=完整路徑。

釘住的事：
  1. 最上面顯示分析上次執行時間；有錯要紅字列出——分析壞了不能讓人以為數字是新的。
  2. 風險表每個標的一列；資料不足要寫出來；10 年視窗不夠要寫幾時才夠；相關矩陣有畫出來。
  3. 每一個日期欄都非空、長得像日期。
  4. 拆解的「現在這一刻」、摘要、逐月表都有畫出來，資料標籤「估算」有顯示。
  5. 頁面上沒有任何投資判斷用語（頁尾那句固定聲明是唯一例外）。
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
PAGE = os.path.join(HERE, "test_analysis_debug.html")
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
        self.assertEqual(self.report.get("total"), 16)

    def test_status_on_top(self):
        self.case("status_shows_last_run_and_errors_on_top")

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

    def test_debug_page_loads_the_script_and_has_the_fixed_footer(self):
        src = self.read("analysis-debug.html")
        self.assertIn("js/analysis-debug.js", src)
        for dep in ("js/lock.js", "js/storage.js", "js/concentration.js"):
            self.assertIn(dep, src)
            self.assertLess(src.index(dep), src.index("js/analysis-debug.js"))
        self.assertIn('id="adhoc"', src)
        self.assertIn("以上為量化整理，未經回測驗證，不構成投資建議。", src)
        self.assertIn("暫時", src)
        self.assertIn("A4", src)

    def test_index_footer_links_to_the_debug_page(self):
        self.assertIn('href="analysis-debug.html"', self.read("index.html"))

    def test_app_js_overseas_note_keys_on_type_not_group(self):
        src = self.read("js/app.js")
        self.assertIn("a.type === 'yahoo'", src)
        self.assertNotIn("a.group === '海外'\n          ?", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
