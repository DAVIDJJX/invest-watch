#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_premium_year_js.py — 分析系列 A1-4：00646 的成本圖示看近一年（cost.json 的 officialYear）

跑法：py -3.12 -m unittest discover -s scripts -p "test_*.py"
用無頭 Chrome／Edge 開 scripts/test_premium_year.html（不連網、不讀資料檔），把結果 JSON 倒出來逐題檢查。
★ 找不到瀏覽器時這一組是【紅】的，不是跳過。要指定瀏覽器：環境變數 IW_BROWSER=完整路徑。

釘住的事（規格在 docs/ANALYSIS.md 5.5）：
  1. 有近一年那一組（最後 252 個有確定折溢價的交易日）就看它判定；沒有就照舊看可用的全部（舊的 cost.json 照樣畫得出來）。
  2. 規則那一行與這次的數字寫出視窗（252 個交易日、起訖）。
  3. 細節並列兩組，可用的全部那一列寫明「不參與上面的狀態」；近一年不到 20 列寫累積中。
  4. 卡片的分析摺疊區寫「近一年 n 個交易日中位數」。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "test_premium_year.html")
sys.path.insert(0, HERE)
from test_plain_js import run_page   # noqa: E402  同一套開頁面、倒結果的做法


class TestPremiumYearJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_page(PAGE)
        cls.by_name = dict((r["name"], r) for r in cls.report.get("results", []))

    def case(self, name):
        self.assertIn(name, self.by_name, "測試頁裡沒有這一題：%s" % name)
        r = self.by_name[name]
        self.assertTrue(r["ok"], "%s：%s" % (name, "；".join(r["problems"])))

    def test_page_really_ran(self):
        self.assertTrue(self.report.get("loaded"), "測試頁沒有載入 js/card-analysis.js 等")
        self.assertEqual(self.report.get("total"), 5)

    def test_the_icon_looks_at_the_last_year(self):
        """對照組：圖示改回看可用的全部 → 這一條會紅。"""
        self.case("the_icon_looks_at_the_last_year")

    def test_the_rule_and_the_number_name_the_window(self):
        self.case("the_rule_and_the_number_name_the_window")

    def test_the_detail_lists_both_groups(self):
        self.case("the_detail_lists_both_groups_and_only_the_year_decides")

    def test_the_year_waits_for_twenty_rows(self):
        self.case("the_year_waits_for_twenty_rows")

    def test_the_fold_shows_the_last_year(self):
        """對照組：摺疊區改回看可用的全部 → 這一條會紅。"""
        self.case("the_fold_shows_the_last_year")


if __name__ == "__main__":
    unittest.main()
