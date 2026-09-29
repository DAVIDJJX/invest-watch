#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_indicators_js.py — 分析系列 A1-5 的「既有功能不變」證據：燈號與指標（js/indicators.js）釘住

scripts/fixtures/indicators_signal.json 是在 A1-5 動任何前端程式【之前】、用 main 上的 js/indicators.js
對 scripts/test_indicators.html 裡的合成序列算出來的；這裡每次重算一次、逐值比對（先做 fixture 再改程式，順序不能反）。
對照組：把 indicators.js 的燈號門檻改壞 → 這一條會紅。
★ 找不到瀏覽器時這一組是【紅】的，不是跳過。
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
PAGE = os.path.join(HERE, "test_indicators.html")
FIXTURE = os.path.join(HERE, "fixtures", "indicators_signal.json")
sys.path.insert(0, HERE)
from test_freshness_js import find_browser   # noqa: E402


def run_page():
    browser = find_browser()
    if not browser:
        raise AssertionError("找不到 Edge／Chrome，js/indicators.js 的釘住測試沒辦法跑（這一組刻意不跳過）。")
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


class TestIndicatorsPinned(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_page()
        with open(FIXTURE, encoding="utf-8") as fh:
            cls.fixture = json.load(fh)

    def test_page_really_ran(self):
        self.assertTrue(self.report.get("loaded"), "測試頁沒有載到 js/indicators.js")
        self.assertEqual(len(self.report.get("cases") or {}), 30)

    def test_signals_and_indicators_match_the_fixture_made_from_main(self):
        cases = self.report["cases"]
        expected = self.fixture["cases"]
        self.assertEqual(sorted(cases), sorted(expected))
        diffs = []
        for key in sorted(expected):
            if cases[key] != expected[key]:
                diffs.append("%s：現在 %s / fixture %s" % (key, json.dumps(cases[key].get("signal"), ensure_ascii=False),
                                                          json.dumps(expected[key].get("signal"), ensure_ascii=False)))
        self.assertEqual(diffs, [], "燈號或指標的輸出跟改程式之前不一樣：\n" + "\n".join(diffs[:5]))

    def test_fixture_covers_all_three_levels(self):
        levels = set(str(v.get("signal", {}).get("level")) for v in self.fixture["cases"].values())
        self.assertEqual(levels, {"low", "mid", "high"})


class TestWindowLabel(unittest.TestCase):
    """A1-6：滿 227 個交易日才叫「52 週」，否則照實寫「近 n 個交易日（資料自 <起日>）」。
    門檻只有一個數字（Indicators.FULL_YEAR_DAYS），區間位置的 full、卡片標題、規則句都看它。
    對照組：門檻改成 10 → 只有 12 天資料的卡也變成 52 週 → 這一組會紅（上面釘住的 fixture 也會紅）。"""

    @classmethod
    def setUpClass(cls):
        cls.labels = run_page().get("labels") or {}

    def test_page_computed_the_labels(self):
        self.assertIsNone(self.labels.get("error"), "測試頁算視窗名稱時出錯：%s" % self.labels.get("error"))
        self.assertEqual(self.labels.get("threshold"), 227)

    def test_short_history_is_never_called_52_weeks(self):
        x = self.labels["n12"]
        self.assertEqual((x["days"], x["full"]), (12, False))
        self.assertEqual(x["name"], "近 12 個交易日")
        self.assertEqual(x["since"], "資料自 2025-09-01")
        self.assertEqual(x["label"], "近 12 個交易日（資料自 2025-09-01）")
        self.assertTrue(x["rule"].startswith("位於近 12 個交易日（資料自 2025-09-01）區間第 "), x["rule"])
        self.assertNotIn("52 週", x["label"] + x["rule"])

    def test_threshold_is_exactly_227(self):
        below, at = self.labels["n226"], self.labels["n227"]
        self.assertEqual((below["days"], below["full"], below["name"]), (226, False, "近 226 個交易日"))
        self.assertEqual((at["days"], at["full"], at["name"], at["since"], at["label"]), (227, True, "52 週", "", "52 週"))
        self.assertTrue(at["rule"].startswith("位於52 週區間第 "), at["rule"])

    def test_long_history_uses_the_last_252_days_and_their_first_date(self):
        x = self.labels["n400"]
        self.assertEqual((x["days"], x["full"], x["name"]), (252, True, "52 週"))
        self.assertEqual(x["firstPoint"], "2025-09-01")
        self.assertEqual(x["first"], "2026-01-27")                          # 400 點的第 149 點（往回數 252 點）：2025-09-01 ＋ 148 天
        self.assertEqual(self.labels["n252"]["first"], "2025-09-01")

    def test_display_rule_keeps_the_same_verdict_as_the_pinned_signal(self):
        """畫面上的規則句只改視窗名稱；箭頭後面的判定（低於 25%／落在中間／高於 75%）跟釘住的燈號一字不差。"""
        for key in ("n12", "n226", "n227", "n252", "n400"):
            x = self.labels[key]
            self.assertEqual(x["rule"].split("→")[1], x["pinnedRule"].split("→")[1], key)
            self.assertEqual(x["rule"].split("區間第")[1], x["pinnedRule"].split("區間第")[1], key)

    def test_no_data_says_so(self):
        none = self.labels["none"]
        self.assertEqual((none["name"], none["label"], none["start"]), ("", "", None))
        self.assertEqual(none["rule"], "歷史資料還不夠算出區間位置")

    def test_app_js_takes_the_name_from_indicators_and_has_no_threshold_of_its_own(self):
        with open(os.path.join(ROOT, "js", "app.js"), encoding="utf-8") as fh:
            app = fh.read()
        block = app[app.index("function rangeHtml("):app.index("/*\n   * 實體條塊的展開內容")]
        self.assertIn("I.windowName(r)", block)
        self.assertIn("I.windowSince(r, first)", block)
        self.assertIn("I.signalRule(r, first)", block)
        self.assertNotIn("r.full ? '52 週'", app)                            # 舊的寫法（自己判斷、寫「近 N 個月」）不可以回來
        self.assertNotIn("個月'", block)
        self.assertNotIn("227", app)
        self.assertNotIn("* 0.9", app)
        with open(os.path.join(ROOT, "js", "indicators.js"), encoding="utf-8") as fh:
            ind = fh.read()
        self.assertEqual(ind.count("var FULL_YEAR_DAYS = 227;"), 1)
        self.assertNotIn("TRADING_DAYS_YEAR * 0.9", ind)                    # 門檻只有一個數字


if __name__ == "__main__":
    unittest.main()
