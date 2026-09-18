#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_freshness_js.py — 停點 8-4：卡片要自己說「這是舊資料」（js/freshness.js）

跑法：
    python -m unittest discover -s scripts -p "test_*.py" -v

前端是純 JavaScript、倉庫裡沒有 Node，所以這裡用機器上本來就有的瀏覽器：
用無頭模式的 Edge／Chrome 開 scripts/test_freshness.html（不連網、不讀資料檔，
拿寫死的假標的與假「現在」去問 js/freshness.js），把執行完的 DOM 倒出來逐題檢查。

★ 找不到瀏覽器時這一組是【紅】的，不是跳過——「測試沒跑」不可以看起來像「測試過了」。
  要指定瀏覽器：設環境變數 IW_BROWSER=完整路徑。

釘住的事（對照組見 docs/CHANGELOG.md 8-4）：
  1. freshness=stale → 黃色，寫出沿用的是什麼時候抓的、牌價是哪一天的；回到 fresh 就消失。
  2. freshness=error → 紅色，而且不顯示價格。
  3. carriedOver → 一行小字，不是警告。
  4. 台銀與台股在週六日、牌價日期是剛過去的星期五 → 中性灰，不是黃色；
     判斷看的是【牌價日期】不是最後抓取時間，星期幾一律用台北時間算。
  5. 牌價比星期五更舊、海外標的、星期一 → 不適用週末那個理由，照樣黃色。
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
PAGE = os.path.join(HERE, "test_freshness.html")

CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def find_browser():
    forced = os.environ.get("IW_BROWSER")
    if forced:
        return forced if os.path.exists(forced) else None
    for p in CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def run_page():
    browser = find_browser()
    if not browser:
        raise AssertionError("找不到 Edge／Chrome，js/freshness.js 的測試沒辦法跑。"
                             "請安裝其中一個，或用 IW_BROWSER 指定路徑。（這一組刻意不跳過）")
    profile = tempfile.mkdtemp(prefix="iw-browser-")
    try:
        url = "file:///" + PAGE.replace("\\", "/").lstrip("/")
        p = subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run",
                            "--user-data-dir=" + profile, "--virtual-time-budget=3000",
                            "--dump-dom", url],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
        dom = p.stdout.decode("utf-8", "replace")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    found = re.findall(r'<pre id="result">(.*?)</pre>', dom, flags=re.S)
    if not found:
        raise AssertionError("測試頁沒有產出結果（瀏覽器：%s）。DOM 開頭：%s" % (browser, dom[:300]))
    text = html.unescape(found[-1])              # 取最後一個：頁面上方的說明文字裡也可能提到這個標籤
    try:
        return json.loads(text)
    except ValueError:
        raise AssertionError("測試頁的結果不是 JSON：%s" % text[:300])


class TestFreshnessJs(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.report = run_page()                  # 整組只開一次瀏覽器
        cls.by_name = dict((r["name"], r) for r in cls.report.get("results", []))

    def case(self, name):
        self.assertIn(name, self.by_name, "測試頁裡沒有這一題：%s" % name)
        r = self.by_name[name]
        self.assertTrue(r["ok"], "%s：%s" % (name, "；".join(r["problems"])))

    def test_page_really_ran(self):
        """頁面要真的載到 freshness.js、題數要對——「一題都沒跑」不可以算通過。"""
        self.assertTrue(self.report.get("loaded"), "測試頁沒有載到 js/freshness.js")
        self.assertEqual(self.report.get("total"), 16)
        self.assertEqual(len(self.by_name), 16)

    # --- 黃、紅、小字 ---
    def test_fresh_has_no_badge(self):
        self.case("fresh_has_no_badge")

    def test_stale_is_yellow_and_says_since_when(self):
        """對照組：把 freshness.js 裡的 'stale' 判斷改壞 → 這一條會紅。"""
        self.case("stale_is_yellow_and_says_since_when")

    def test_back_to_fresh_clears_the_badge(self):
        self.case("back_to_fresh_clears_the_badge")

    def test_freshness_error_is_red_and_hides_the_price(self):
        self.case("freshness_error_is_red_and_hides_the_price")

    def test_failed_fetch_hides_price_without_a_second_badge(self):
        self.case("failed_fetch_hides_price_without_a_second_badge")

    def test_carried_over_is_small_text_with_the_reason(self):
        self.case("carried_over_is_small_text_with_the_reason")

    def test_carried_over_without_a_reason_says_when_it_was_fetched(self):
        self.case("carried_over_without_a_reason_says_when_it_was_fetched")

    # --- 週末的灰色 ---
    def test_saturday_with_fridays_quote_is_grey_not_yellow(self):
        self.case("saturday_with_fridays_quote_is_grey_not_yellow")

    def test_sunday_still_grey_even_if_the_last_fetch_was_on_saturday(self):
        """判斷看的是牌價日期，不是最後抓取時間。"""
        self.case("sunday_still_grey_even_if_the_last_fetch_was_on_saturday")

    def test_weekend_and_fresh_is_grey_too(self):
        self.case("weekend_and_fresh_is_grey_too_so_the_yellow_date_note_is_suppressed")

    def test_taiwan_stocks_say_market_closed(self):
        self.case("taiwan_stocks_say_market_closed")

    def test_finmind_fx_counts_as_bank_of_taiwan(self):
        self.case("finmind_fx_counts_as_bank_of_taiwan_and_uses_the_quote_date")

    # --- 週末那個理由不適用的時候 ---
    def test_weekend_but_the_quote_is_older_than_friday_is_yellow(self):
        """對照組：拿掉「牌價日期必須是星期五」→ 這一條會紅。"""
        self.case("weekend_but_the_quote_is_older_than_friday_is_yellow")

    def test_overseas_assets_never_get_the_weekend_excuse(self):
        self.case("overseas_assets_never_get_the_weekend_excuse")

    def test_monday_is_not_the_weekend(self):
        self.case("monday_is_not_the_weekend")

    def test_weekday_is_judged_in_taipei_not_in_the_browsers_timezone(self):
        """對照組：拿掉 +8 小時的換算 → 這一條會紅。"""
        self.case("weekday_is_judged_in_taipei_not_in_the_browsers_timezone")


class TestCardIsWiredToFreshness(unittest.TestCase):
    """函式寫對了但卡片沒去叫它，等於沒做。這兩條只看原始碼，不開瀏覽器。"""

    def read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_app_js_asks_freshness_js_and_does_not_judge_by_itself(self):
        src = self.read("js/app.js")
        self.assertIn("window.Freshness.classify(", src)
        self.assertIn("fr.badge", src)
        self.assertIn("fr.hidePrice", src)
        self.assertIn("fr.suppressDateNote", src)
        self.assertNotIn("lastSuccessAt", src,
                         "卡片不可以自己拿 lastSuccessAt 去算新舊——規則只有 schedule.json 那一份")

    def test_index_html_loads_freshness_js_before_app_js(self):
        src = self.read("index.html")
        self.assertIn("js/freshness.js", src)
        self.assertLess(src.index("js/freshness.js"), src.index("js/app.js"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
