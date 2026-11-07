#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_aspects_js.py — 分析系列 A1-7：每張卡的「面向」（位置、風險、成本；A2a 加趨勢）——js/card-analysis.js 的 aspects／strip／detail／cell

跑法：python -m unittest discover -s scripts -p "test_*.py"
用無頭 Chrome／Edge 開 scripts/test_aspects.html（不連網、不讀資料檔），把結果 JSON 倒出來逐題檢查。
★ 找不到瀏覽器時這一組是【紅】的，不是跳過。要指定瀏覽器：環境變數 IW_BROWSER=完整路徑。

釘住的事（規格在 docs/ANALYSIS.md「呈現原則」；對照組見 docs/CHANGELOG.md A1-7）：
  1. 每張卡三個面向、順序固定；每個面向都有狀態詞、這次是哪個數字（或為什麼沒有狀態）、規則。
  2. 位置沿用卡片的燈號：跟 js/indicators.js 的輸出一字不差（30 組合成輸入），狀態詞永遠不是「偏便宜／偏貴」。
  3. 風險＝1 年波動相對 5 年（±兩成）；成本＝存摺價差、條塊溢價、換匯價差跟自己的中位數比（±一成），
     折溢價跟自己歷史的第 25／75 百分位比、看「確定」口徑；不滿 20 個交易日是「資料不足（累積中 n/20）」。
  4. 「不適用」（指數、股票、加密貨幣、商品的成本）不是「資料不足」：卡片不畫、總覽表寫灰字。
  5. 匯率卡的位置只掛燈號；細節裡多一行「5 年位置：…（見換匯助手）」並連過去。
  6. 圖示一定有面向名稱與狀態詞並列、有文字替代；細節一定印規則；每個數字下面一行白話、不是空的。
  7. 檔案讀不到寫「分析資料暫時讀不到」；任何輸出沒有帶方向的交易字眼、沒有「幾個面向怎樣」的計數。
  A2a（趨勢；對照組見 scripts/mutations_pending/a2a_mutations.py 的 W 系列）：
  8. 面向變成四個，趨勢排最後；現價高於 200 日均線且 12 個月價格報酬為正＝向上、都相反＝向下、其他＝持平；用色點不用天氣。
  9. 200 日均線至少 200 點、12 個月報酬至少 253 點，任一不夠就是資料不足（寫出只有幾天）；條塊固定資料不足。
  10. 這次的數字：一位小數、正負號、寫明不含息與資料日期；規則句以「規則：」開頭；兩個名詞在名詞解釋裡，RSI 補一句定位。
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAGE = os.path.join(HERE, "test_aspects.html")
sys.path.insert(0, HERE)
from test_plain_js import run_page   # noqa: E402  同一套開頁面、倒結果的做法


class TestAspectsJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_page(PAGE)
        cls.by_name = dict((r["name"], r) for r in cls.report.get("results", []))

    def case(self, name):
        self.assertIn(name, self.by_name, "測試頁裡沒有這一題：%s" % name)
        r = self.by_name[name]
        self.assertTrue(r["ok"], "%s：%s" % (name, "；".join(r["problems"])))

    def test_page_really_ran(self):
        self.assertTrue(self.report.get("loaded"), "測試頁沒有載到 indicators／plain／glossary／card-analysis 其中一支")
        self.assertEqual(self.report.get("total"), 27)

    def test_aspects_order_word_number_rule(self):
        self.case("aspects_come_in_a_fixed_order_each_with_word_number_and_rule")

    def test_position_is_the_same_lamp_as_the_card(self):
        """對照組：位置面向的字眼被改成偏貴（拿區間百分位去套匯率位置那一套）→ 這一條會紅。"""
        self.case("position_is_the_same_lamp_as_the_card_word_for_word")

    def test_position_without_data_says_why(self):
        self.case("position_without_data_says_why")

    def test_risk_compares_one_year_to_five_years(self):
        """對照組：風險的門檻改壞 → 這一條會紅。"""
        self.case("risk_compares_one_year_to_five_years")

    def test_cost_compares_spreads_to_their_own_median(self):
        """對照組：人民幣那一本拿到台幣那一本的價差 → 這一條會紅。"""
        self.case("cost_compares_spreads_to_their_own_median")

    def test_bar_cost_waits_for_twenty_days(self):
        self.case("bar_cost_uses_the_one_kilo_row_and_waits_for_twenty_days")

    def test_premium_uses_own_quartiles_and_the_official_row(self):
        """對照組：折溢價用錯規則（拿去跟中位數比一成）→ 這一條會紅。"""
        self.case("premium_uses_own_quartiles_and_the_official_row")

    def test_not_applicable_is_not_insufficient(self):
        """對照組：不適用被寫成資料不足 → 這一條會紅。"""
        self.case("not_applicable_is_not_the_same_as_insufficient")

    def test_not_applicable_still_lists_the_fixed_costs(self):
        """A1-7 驗收裁決：不適用是沒有動態成本，不是沒有成本。對照組：不適用的細節不列靜態費用 → 這一條會紅。"""
        self.case("not_applicable_still_lists_the_fixed_costs")

    def test_fx_cards_keep_the_lamp_and_link_to_the_helper(self):
        """對照組：匯率卡的位置換成 5 年百分位那一套、或把連結拿掉 → 這一條會紅。"""
        self.case("fx_cards_keep_the_lamp_and_link_the_five_year_position_to_the_helper")

    def test_every_chip_has_icon_name_and_word(self):
        """對照組：圖示沒有狀態詞 → 這一條會紅。"""
        self.case("every_chip_has_icon_name_and_word_and_controls_its_own_detail")

    def test_every_detail_prints_rule_and_plain_lines(self):
        """對照組：圖示沒有規則、或某一行白話是空的 → 這一條會紅。"""
        self.case("every_detail_prints_the_number_the_rule_and_a_plain_line")

    def test_unreadable_files_say_so(self):
        self.case("unreadable_files_say_analysis_is_temporarily_unavailable")

    def test_overview_cells(self):
        self.case("overview_cells_show_icon_and_word_or_grey_text")

    def test_no_trading_words_no_counting(self):
        """對照組：出現禁用詞、或在圖示列印出「幾個面向怎樣」→ 這一條會紅。"""
        self.case("no_trading_words_no_counting_and_only_known_words")

    def test_fold_rows_get_a_plain_line(self):
        self.case("fold_rows_get_a_plain_line_when_the_plain_layer_is_loaded")

    # ---- A2a：趨勢
    def test_trend_up(self):
        """對照組：三態判斷反過來、或趨勢的圖示換成天氣 → 這一條會紅。"""
        self.case("trend_up_when_above_the_200_day_average_and_the_year_is_positive")

    def test_trend_down(self):
        self.case("trend_down_when_below_the_average_and_the_year_is_negative")

    def test_trend_flat(self):
        """對照組：持平的條件改成「任一個相反就算向下」→ 這一條會紅。"""
        self.case("trend_is_flat_when_the_two_facts_disagree_or_sit_on_the_line")

    def test_trend_199_and_200_points(self):
        """對照組：200 日均線的門檻改成 199 → 這一條會紅。"""
        self.case("trend_needs_200_points_for_the_average")

    def test_trend_252_and_253_points(self):
        """對照組：12 個月報酬的門檻少一天（253 → 252）→ 這一條會紅。"""
        self.case("trend_needs_253_points_for_the_twelve_month_return")

    def test_trend_bar_and_failures(self):
        """對照組：條塊不固定資料不足 → 這一條會紅。"""
        self.case("trend_bar_and_failures_say_why_instead_of_guessing")

    def test_trend_because_format(self):
        """對照組：這次的數字不寫「不含息」→ 這一條會紅。"""
        self.case("trend_because_has_one_decimal_signs_and_the_data_date")

    def test_trend_rule(self):
        self.case("trend_rule_starts_with_rule_and_is_printed_everywhere")

    def test_trend_glossary(self):
        """對照組：名詞解釋少了「200 日均線」、或 RSI 少了定位那一句 → 這一條會紅。"""
        self.case("trend_terms_are_in_the_glossary_and_rsi_is_only_a_description")

    def test_trend_sentences(self):
        self.case("trend_sentences_say_insufficient_instead_of_nothing")

    def test_trend_no_trading_words(self):
        self.case("trend_has_no_trading_words_and_no_counting")


class TestAspectsWiring(unittest.TestCase):
    """檔案有沒有接對線：只看原始碼，不開瀏覽器。"""

    def read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_card_analysis_stays_pure(self):
        src = self.read("js/card-analysis.js")
        self.assertNotIn("fetch(", src)
        self.assertNotIn("localStorage", src)
        self.assertNotIn("document.", src)                                                      # 不碰 DOM：只回 HTML 字串
        self.assertNotIn("addEventListener", src)

    def test_position_never_recomputes_the_lamp(self):
        """位置的狀態只准從 Indicators.computeAll 的 signal 來：card-analysis.js 與 plain.js 裡都沒有自己的 25／75 判斷。"""
        ca = self.read("js/card-analysis.js")
        block = ca[ca.index("  function positionAspect("):ca.index("  /* 匯率卡的位置只掛 52 週燈號")]
        self.assertIn("I.computeAll(points, asset.price)", block)
        self.assertIn("Pl.lampState(ind.signal, I.signalRule(r, first))", block)
        self.assertNotIn("< 25", block)
        self.assertNotIn("> 75", block)
        self.assertNotIn("fxPositionState", block)                                              # 匯率位置那一套（偏便宜／偏貴）不准用在位置上
        plain = self.read("js/plain.js")
        lamp = plain[plain.index("  function lampState("):plain.index("  /* 匯率位置（換匯助手）")]
        self.assertIn("word: sig.label", lamp)                                                  # 字直接拿燈號的，不查自己的表
        self.assertNotIn("percentile", lamp)

    def test_no_function_adds_aspects_up(self):
        for rel in ("js/card-analysis.js", "js/plain.js"):
            src = self.read(rel)
            for bad in ("score", "tally", "countStates", "sumStates"):
                self.assertNotIn(bad, src, "%s 裡出現 %s" % (rel, bad))

    def test_trend_is_wired_after_the_other_three(self):
        """A2a：趨勢排最後、用的是現成的 trendAspect；儀表板的佔位字跟著改；趨勢只用已經載入的日線。"""
        ca = self.read("js/card-analysis.js")
        self.assertIn("var ASPECT_ORDER = ['position', 'risk', 'cost', 'trend'];", ca)
        self.assertIn("trend: trendAspect", ca)
        block = ca[ca.index("  function trendAspect("):ca.index("  var BUILDERS = ")]
        self.assertIn("Pl.trendState(series, price, through)", block)
        self.assertIsNone(re.search(r"(?<![A-Za-z0-9])(200|252|253)(?![0-9])", block))      # 門檻只在 js/plain.js 一份（ma200 這種名字不算）
        app = self.read("js/app.js")
        self.assertIn("位置、風險、成本、趨勢：讀取中…", app)
        self.assertNotIn("位置、風險、成本：讀取中", app)

    def test_both_pages_use_the_same_aspects(self):
        app = self.read("js/app.js")
        self.assertIn("CA.strip(a, CA.aspects(a, {", app)
        ana = self.read("js/analysis.js")
        self.assertIn("CA.aspects(latest.assets[id], {", ana)
        self.assertIn("CA.cell(x)", ana)
        self.assertIn("CA.detail(x)", ana)
        page = self.read("analysis.html")
        for dep in ("js/indicators.js", "js/freshness.js", "js/plain.js", "js/glossary.js", "js/card-analysis.js"):
            self.assertIn(dep, page)
            self.assertLess(page.index(dep), page.index("js/analysis.js"))


if __name__ == "__main__":
    unittest.main()
