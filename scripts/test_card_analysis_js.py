#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_card_analysis_js.py — 分析系列 A1-5：儀表板卡片裡的「分析」摺疊區（js/card-analysis.js）

跑法：python -m unittest discover -s scripts -p "test_*.py"
用無頭 Chrome／Edge 開 scripts/test_card_analysis.html（不連網、不讀資料檔），把結果 JSON 倒出來逐題檢查。
★ 找不到瀏覽器時這一組是【紅】的，不是跳過。要指定瀏覽器：環境變數 IW_BROWSER=完整路徑。

釘住的事（對照組見 docs/CHANGELOG.md A1-5）：
  1. 展開後有風險、相關、成本三組事實；每一列都有四種資料標籤之一與資料日期。
  2. 「目前距高點」橫條的刻度是 0 到該標的歷史最大回檔；週線日期與日線報價日期並列印出。
  3. 最同向＝r 最高；最不相關＝|r| 最接近 0；反向最強只在 r < −0.3 時顯示。
  4. 沒有週線長歷史的卡只列成本項；什麼都沒有的卡寫「這個標的目前沒有分析項目」。
  5. 畫出來的文字沒有判斷用語、沒有任何加總後的單一數字。
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
PAGE = os.path.join(HERE, "test_card_analysis.html")
sys.path.insert(0, HERE)
from test_freshness_js import find_browser   # noqa: E402  同一套找瀏覽器的規則


def run_page():
    browser = find_browser()
    if not browser:
        raise AssertionError("找不到 Edge／Chrome，js/card-analysis.js 的測試沒辦法跑（這一組刻意不跳過）。IW_BROWSER 可指定路徑。")
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
    return json.loads(html.unescape(found[-1]))


class TestCardAnalysisJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_page()
        cls.by_name = dict((r["name"], r) for r in cls.report.get("results", []))

    def case(self, name):
        self.assertIn(name, self.by_name, "測試頁裡沒有這一題：%s" % name)
        r = self.by_name[name]
        self.assertTrue(r["ok"], "%s：%s" % (name, "；".join(r["problems"])))

    def test_page_really_ran(self):
        self.assertTrue(self.report.get("loaded"), "測試頁沒有載到 js/card-analysis.js")
        self.assertEqual(self.report.get("total"), 7)

    def test_expanded_card_has_content(self):
        self.case("expanded_card_has_risk_correlation_and_cost")

    def test_labels_and_dates_non_empty(self):
        """對照組：把資料標籤拿掉 → 這一條會紅。"""
        self.case("every_row_has_a_label_and_a_date")

    def test_drawdown_bar_scale(self):
        """對照組：橫條刻度改錯 → 這一條會紅。"""
        self.case("drawdown_bar_scaled_to_max_drawdown_with_both_dates")

    def test_correlation_rules(self):
        self.case("correlation_most_aligned_least_related_and_strongest_inverse")

    def test_cards_without_long_history(self):
        self.case("cards_without_long_history_show_only_cost_or_nothing")

    def test_premium_median_rule(self):
        self.case("premium_median_shown_only_after_twenty_days")

    def test_no_judgement_words(self):
        """對照組：把數字換成一個假的加總數字 → 這一條會紅。"""
        self.case("no_judgement_words_or_totals_in_rendered_text")


if __name__ == "__main__":
    unittest.main()
