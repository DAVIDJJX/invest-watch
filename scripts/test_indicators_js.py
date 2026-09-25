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


if __name__ == "__main__":
    unittest.main()
