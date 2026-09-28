#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_fxplan_js.py — 分析系列 A1-6：換匯助手的私人設定與本月試算（js/fxplan.js，預算池模型）

跑法：python -m unittest discover -s scripts -p "test_*.py"
用無頭 Chrome／Edge 開 scripts/test_fxplan.html（不連網、不讀資料檔），把結果 JSON 倒出來逐題檢查。
★ 找不到瀏覽器時這一組是【紅】的，不是跳過。要指定瀏覽器：環境變數 IW_BROWSER=完整路徑。

釘住的事（對照組見 docs/CHANGELOG.md A1-6）：
  1. 預算池＝從計畫起始月累積的預算 − 這段期間已經換掉的台幣；計畫開始以前的紀錄不從池子扣。
  2. 規則額度＝規則比例 × 池子（不是 × 月預算）；固定分批＝一個月預算。兩種都算。
  3. 保底（有期限才有）＝池子 ÷ 剩餘月數；有目標總額時再跟目標那一半比，取較大者。
  4. 本月額度＝max(預設做法的額度, 保底)，不得超過池子。
  5. 本月額度用月初的池子算；本月已換的從額度裡扣一次——換完之後不會再叫你把剩下的池子乘一次比例。
  6. 計畫起始月必填；每一筆已換紀錄的匯率必填；設定檔有錯就不算。
  7. 設定檔剛好七個欄位，沒有任何日期戳；算出來的結果不沿用設定檔的鍵名。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "test_fxplan.html")
sys.path.insert(0, HERE)
from test_plain_js import run_page   # noqa: E402  同一套開頁面的做法


class TestFxPlanJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_page(PAGE)
        cls.by_name = dict((r["name"], r) for r in cls.report.get("results", []))

    def case(self, name):
        self.assertIn(name, self.by_name, "測試頁裡沒有這一題：%s" % name)
        r = self.by_name[name]
        self.assertTrue(r["ok"], "%s：%s" % (name, "；".join(r["problems"])))

    def test_page_really_ran(self):
        self.assertTrue(self.report.get("loaded"), "測試頁沒有載到 js/fxplan.js")
        self.assertEqual(self.report.get("total"), 9)

    def test_pool_accumulates(self):
        """對照組：池子不累積（永遠只有一個月預算）→ 這一條會紅。"""
        self.case("pool_accumulates_budget_since_the_start_month_minus_what_was_spent")

    def test_rule_quota_is_ratio_times_pool(self):
        """對照組：規則比例乘月預算而不是乘池子 → 這一條會紅。"""
        self.case("rule_quota_is_the_ratio_times_the_pool_not_times_the_monthly_budget")

    def test_floor_and_the_larger_one_wins(self):
        """對照組：把保底拿掉 → 這一條會紅。"""
        self.case("floor_is_pool_over_months_left_or_the_goal_based_one_whichever_is_larger")

    def test_quota_never_exceeds_the_pool(self):
        """對照組：本月額度可以超過池子 → 這一條會紅。"""
        self.case("this_months_quota_never_exceeds_the_pool")

    def test_this_months_records_come_off_once(self):
        """對照組：用扣掉本月已換之後的池子再乘一次比例 → 這一條會紅。"""
        self.case("this_months_records_come_off_once_and_the_quota_does_not_compound")

    def test_no_deadline_no_floor(self):
        self.case("no_deadline_means_no_floor_and_missing_data_is_said_out_loud")

    def test_start_month_and_rate_are_required(self):
        """對照組：匯率不填也放行、或計畫起始月不填也放行 → 這一條會紅。"""
        self.case("validation_requires_the_start_month_and_a_rate_on_every_record")

    def test_months_between(self):
        self.case("months_between_counts_both_ends")

    def test_form_round_trip(self):
        self.case("form_round_trip_keeps_values_and_the_file_has_no_date_stamp")


if __name__ == "__main__":
    unittest.main()
