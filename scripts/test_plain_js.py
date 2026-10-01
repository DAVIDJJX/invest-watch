#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_plain_js.py — 分析系列 A1-6／A1-7：「一眼看懂」的翻譯層（js/plain.js）與名詞解釋（js/glossary.js）

跑法：python -m unittest discover -s scripts -p "test_*.py"
用無頭 Chrome／Edge 開 scripts/test_plain.html（不連網、不讀資料檔），把結果 JSON 倒出來逐題檢查。
★ 找不到瀏覽器時這一組是【紅】的，不是跳過。要指定瀏覽器：環境變數 IW_BROWSER=完整路徑。

釘住的事（規格在 docs/ANALYSIS.md「呈現原則」；對照組見 docs/CHANGELOG.md A1-6、A1-7）：
  1. 三態的門檻跟印出來的規則句一致（匯率位置 40／60、成本 ±一成、風險 ±兩成）。
  2. 圖示旁邊一定有狀態詞；圖示旁邊一定印規則與這次的數字；資料不足時也一樣。
  3. 白話模板遇到資料不足輸出「資料不足」，不是空字串。
  4. 任何輸出都沒有帶方向的交易字眼，也沒有「幾個面向怎樣」的計數；plain.js 沒有加總用的函式。
  5. 名詞解釋有規格點名的詞；點一下展開、再點一下收起。
  6. 歷史模擬的那一句結論完全由數字決定：勝率、中位數、跟一次價差比、贏得多的視窗是不是同一次事件、預設哪一種。
  A1-7：
  7. 位置的狀態詞就是卡片燈號的字（相對低檔區／中性／相對高檔區），不重算、不換字；「偏便宜／偏貴」只給匯率位置與估值。
  8. 折溢價跟自己歷史的第 25／75 百分位比，不跟中位數比；不滿 20 個交易日是「資料不足（累積中 n/20）」。
  9. 「資料不足」「不適用」「暫時讀不到」是三種不同的狀態。
  10. 每一種白話模板至少三組測試（正常、極端、資料不足），輸出逐字比對。
  11. 先畫、後載入名詞解釋的地方（儀表板的指標格）能換成可以點的按鈕；點名詞不會觸發外面的點擊。
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
PAGE = os.path.join(HERE, "test_plain.html")
sys.path.insert(0, HERE)
from test_freshness_js import find_browser   # noqa: E402  同一套找瀏覽器的規則


def run_page(page=PAGE):
    browser = find_browser()
    if not browser:
        raise AssertionError("找不到 Edge／Chrome，%s 沒辦法跑（這一組刻意不跳過）。IW_BROWSER 可指定路徑。" % os.path.basename(page))
    profile = tempfile.mkdtemp(prefix="iw-browser-")
    try:
        url = "file:///" + page.replace("\\", "/").lstrip("/")
        p = subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run",
                            "--user-data-dir=" + profile, "--virtual-time-budget=3000", "--dump-dom", url],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
        dom = p.stdout.decode("utf-8", "replace")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    found = re.findall(r'<pre id="result">(.*?)</pre>', dom, flags=re.S)
    if not found:
        raise AssertionError("測試頁沒有產出結果（瀏覽器：%s）。DOM 開頭：%s" % (browser, dom[:300]))
    return json.loads(html.unescape(found[-1]))


class TestPlainJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_page()
        cls.by_name = dict((r["name"], r) for r in cls.report.get("results", []))

    def case(self, name):
        self.assertIn(name, self.by_name, "測試頁裡沒有這一題：%s" % name)
        r = self.by_name[name]
        self.assertTrue(r["ok"], "%s：%s" % (name, "；".join(r["problems"])))

    def test_page_really_ran(self):
        self.assertTrue(self.report.get("loaded"), "測試頁沒有載到 js/plain.js 或 js/glossary.js")
        self.assertEqual(self.report.get("total"), 12)

    def test_three_states_follow_the_rules(self):
        """對照組：匯率位置的門檻改壞（60 改成 80）→ 這一條會紅。"""
        self.case("three_states_follow_the_printed_rules")

    def test_position_state_is_the_pinned_lamp_word_for_word(self):
        """A1-7。對照組：位置面向的字眼被改成偏貴（或換成匯率位置那一套）→ 這一條會紅。"""
        self.case("position_state_is_the_pinned_lamp_word_for_word")

    def test_premium_state_uses_own_quartiles(self):
        """A1-7。對照組：折溢價用錯規則（拿去跟中位數比一成）→ 這一條會紅。"""
        self.case("premium_state_uses_own_quartiles_not_the_median")

    def test_insufficient_and_not_applicable_are_different(self):
        """A1-7。對照組：不適用被寫成資料不足 → 這一條會紅。"""
        self.case("insufficient_not_applicable_and_unavailable_are_three_different_things")

    def test_icon_always_has_its_word(self):
        """對照組：badge 不印狀態詞 → 這一條會紅。"""
        self.case("every_icon_has_its_word_next_to_it")

    def test_icon_always_prints_its_rule(self):
        """對照組：badge 不印規則 → 這一條會紅。"""
        self.case("every_icon_prints_its_rule_and_this_times_number")

    def test_insufficient_data_is_said_out_loud(self):
        """對照組：資料不足時回空字串 → 這一條會紅。"""
        self.case("plain_sentences_say_insufficient_instead_of_nothing")

    def test_every_template_has_three_kinds_of_cases(self):
        """A1-7：每一種白話模板至少三組（正常、極端、資料不足），輸出逐字比對。對照組：某個模板輸出空字串 → 這一條會紅。"""
        self.case("every_template_has_normal_extreme_and_insufficient_cases")

    def test_backtest_story_follows_the_numbers(self):
        """對照組：不管資料、一律說「同一次事件」→ 這一條會紅。"""
        self.case("backtest_story_follows_the_numbers")

    def test_no_trading_words_no_counting(self):
        """對照組：狀態詞換成帶方向的字眼、或印出「幾個面向怎樣」→ 這一條會紅。"""
        self.case("no_trading_words_and_no_counting_anywhere")

    def test_glossary(self):
        self.case("glossary_has_the_required_terms_and_opens_on_click")

    def test_glossary_upgrades_placeholders(self):
        """A1-7：儀表板先畫、名詞解釋後到——先留記號的字要能換成按鈕；點名詞不往外傳。"""
        self.case("glossary_upgrades_placeholders_and_keeps_clicks_to_itself")


class TestPlainWiring(unittest.TestCase):
    """檔案有沒有接對線：只看原始碼，不開瀏覽器。"""

    def read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_analysis_page_loads_the_three_new_scripts_before_analysis_js(self):
        src = self.read("analysis.html")
        for dep in ("js/plain.js", "js/glossary.js", "js/fxplan.js"):
            self.assertIn(dep, src)
            self.assertLess(src.index(dep), src.index("js/analysis.js"))
        self.assertIn('<section id="fx"></section>', src)
        self.assertLess(src.index('id="status"'), src.index('id="fx"'))                          # 分析狀態永遠在最上面
        self.assertLess(src.index('id="fx"'), src.index('id="risk"'))

    def test_dashboard_loads_them_only_after_the_first_screen(self):
        """儀表板首屏維持 7 個靜態檔：index.html 裡沒有白話層與名詞解釋的標籤；A1-7 起由 js/app.js 在首屏畫完之後才載入這兩支。
        換匯設定（fxplan.js）儀表板永遠不載。真的量請求數的測試在 test_dashboard_requests_js.py。"""
        src = self.read("index.html")
        for dep in ("js/plain.js", "js/glossary.js", "js/fxplan.js"):
            self.assertNotIn(dep, src)
        app = self.read("js/app.js")
        loader = app[app.index("  function loadDeferredScripts()"):app.index("  function loadAnalysis()")]
        self.assertIn("loadScript('js/plain.js')", loader)
        self.assertIn("loadScript('js/glossary.js')", loader)
        self.assertEqual(app.count("loadScript('js/"), 2)                                        # 首屏之後才載入的程式只有這兩支
        self.assertNotIn("fxplan", app)

    def test_pure_layers_never_fetch_or_log(self):
        call = re.compile(r"console\s*\.\s*\w+\s*\(")                                          # 真的呼叫才算；註解裡提到這個字不算
        for rel in ("js/plain.js", "js/glossary.js", "js/fxplan.js"):
            src = self.read(rel)
            self.assertNotIn("fetch(", src, rel)
            self.assertIsNone(call.search(src), rel)
            self.assertNotIn("localStorage", src, rel)
        self.assertIsNone(call.search(self.read("js/analysis.js")))
        self.assertTrue(call.search("x; console.log(1)"))                                       # 檢查式自己要抓得到東西

    def test_styles_for_the_glance_row_exist(self):
        css = self.read("css/style.css")
        for sel in (".glance-row", ".glance-word", ".glance-rule", ".plain-line", "button.term[data-term]", ".term-def",
                    ".card-glance", "button.asp", ".asp-word", ".asp-rule", ".asp-detail", ".asp-cell"):   # 後面六個是 A1-7 的圖示列
            self.assertIn(sel, css)


if __name__ == "__main__":
    unittest.main()
