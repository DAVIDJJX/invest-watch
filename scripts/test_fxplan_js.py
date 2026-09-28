#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_fxplan_js.py — 分析系列 A1-6：換匯助手的私人設定與本月試算（js/fxplan.js）

跑法：python -m unittest discover -s scripts -p "test_*.py"
用無頭 Chrome／Edge 開 scripts/test_fxplan.html（不連網、不讀資料檔），把結果 JSON 倒出來逐題檢查。
★ 找不到瀏覽器時這一組是【紅】的，不是跳過。要指定瀏覽器：環境變數 IW_BROWSER=完整路徑。

釘住的事（對照組見 docs/CHANGELOG.md A1-6）：
  1. 兩種做法都算：A 固定分批＝月預算 × 100%；B＝月預算 × 規則表那一檔的比例。
  2. 期限保底＝（目標總額 − 已換人民幣）÷ 剩餘月數，已換人民幣是各筆台幣 ÷ 匯率的加總；
     本月額度取預設做法與保底的較大者，兩個數字都留著。沒有目標或沒有期限就沒有保底。
  3. 本月的保底以月初為準；本月已換的從本月額度裡扣一次，不會扣兩次，也不會變成負的。
  4. 每一筆已換紀錄的匯率必填；設定檔有錯就不算。
  5. 設定檔剛好六個欄位，沒有任何日期戳。
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
        self.assertEqual(self.report.get("total"), 7)

    def test_both_methods_are_computed(self):
        """對照組：B 的比例乘錯（用 100% 當比例）→ 這一條會紅。"""
        self.case("rule_ratio_times_budget_and_fixed_installment_are_both_computed")

    def test_floor_and_the_larger_one_wins(self):
        """對照組：把保底拿掉 → 這一條會紅。"""
        self.case("floor_is_remaining_divided_by_months_left_and_the_larger_one_wins")

    def test_no_target_no_floor(self):
        self.case("no_target_or_no_deadline_means_no_floor")

    def test_this_months_records_come_off_once(self):
        self.case("this_months_records_come_off_the_quota_once")

    def test_rate_is_required(self):
        """對照組：匯率不填也放行 → 這一條會紅。"""
        self.case("validation_requires_a_rate_on_every_record")

    def test_months_between(self):
        self.case("months_between_counts_both_ends")

    def test_form_round_trip(self):
        self.case("form_round_trip_keeps_values_and_the_file_has_no_date_stamp")


if __name__ == "__main__":
    unittest.main()
