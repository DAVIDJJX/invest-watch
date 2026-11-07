#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_theme.py — U1 柔和配色（分析系列 A1-4）：預設淺色、深色保留可以切換、只改顏色。

跑法：py -3.12 -m unittest discover -s scripts -p "test_*.py"
前半是讀檔就能查的（css/style.css、js/charts.js、五個頁面）；後半用無頭 Chrome／Edge 真的開一頁看切換。
★ 找不到瀏覽器時後半是【紅】的，不是跳過。要指定瀏覽器：環境變數 IW_BROWSER=完整路徑。

釘住的事：
  1. :root（淺色）的值就是規格的色票（--high 照 A1-4 的裁決改成 #a84f18）。
  2. html[data-theme="dark"] 的前 18 個跟 A1-4 以前的 :root 一字不差；兩組的變數名稱一模一樣（少一個就紅）。
  3. 淺色底下，文字、三態、漲跌的顏色在白卡、頁面底、側欄底上的對比都 ≥ 4.5（WCAG 2.x 的算法）；
     錯誤訊息的字在它的淡紅底上、金色按鈕上的字、圓餅七色與 MA60 在白卡上也都 ≥ 4.5。
  4. js/charts.js 不寫任何色碼，用到的變數兩組都有。
  5. 五個頁面 head 都有同一段主題腳本、在樣式表之前；theme-color 的兩個值跟兩組的 --bg 一樣。
  6. 瀏覽器裡：沒選過＝淺色；存了 dark＝深色；別的分頁換了（storage 事件）＝跟著換並發 iw:theme；圖表讀到的顏色跟著主題。
"""
import html
import io
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
sys.path.insert(0, HERE)
from test_freshness_js import find_browser   # noqa: E402  同一套找瀏覽器的規則

PAGES = ["index.html", "analysis.html", "history.html", "settings.html", "records.html"]

# 規格的淺色色票（A1-4，Cowork 2026-10-12）；--high 照 A1-4 的裁決（2026-11-05）改成 #a84f18
LIGHT = {"--bg": "#f1f5f8", "--bg-soft": "#e7f0f4", "--card": "#ffffff", "--card-hover": "#f9fbfc", "--line": "#dce5ea",
         "--text": "#2b3e4e", "--text-dim": "#55666f", "--text-faint": "#5f6e79",
         "--up": "#c0392b", "--down": "#1a7340", "--flat": "#5f6e79",
         "--gold": "#8a6410", "--accent": "#3f6279", "--warn": "#8f5c0e", "--error": "#c0392b",
         "--low": "#1a7340", "--mid": "#5f6e79", "--high": "#a84f18"}

# A1-4 以前 css/style.css 的 :root（深色），一字不差：搬到 html[data-theme="dark"] 之後要原樣出現、順序也一樣
DARK_BEFORE = """  --bg:          #0e1420;
  --bg-soft:     #151d2c;
  --card:        #182132;
  --card-hover:  #1e2a3e;
  --line:        #263349;
  --text:        #e6edf7;
  --text-dim:    #8fa2bd;
  --text-faint:  #64748b;

  --up:          #ff6b6b;   /* 台股習慣：漲用紅 */
  --down:        #4ade80;   /* 跌用綠 */
  --flat:        #8fa2bd;

  --gold:        #e5b567;
  --accent:      #5b9cf8;
  --warn:        #f7b955;
  --error:       #ff5f56;

  --low:         #4ade80;   /* 相對低檔區 */
  --mid:         #8fa2bd;   /* 中性 */
  --high:        #ff9f43;   /* 相對高檔區 */
"""
TEXT_VARS = ["--text", "--text-dim", "--text-faint", "--up", "--down", "--flat", "--gold", "--accent", "--warn", "--error",
             "--low", "--mid", "--high"]
BACKS = ["--card", "--bg", "--bg-soft"]             # 白卡、頁面底、側欄底
PIE_AND_LINES = ["--pie-tw", "--pie-us", "--pie-bond", "--pie-gold", "--pie-fund", "--pie-cash", "--pie-other", "--pie-none", "--chart-ma60"]


def read(rel):
    with io.open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def block(css, selector):
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, flags=re.S)
    return m.group(1) if m else None


def variables(body):
    out = {}
    for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body or ""):
        out[m.group(1)] = m.group(2).strip()
    return out


def rgb(color, under=(255, 255, 255)):
    """#rrggbb 或 rgba(r, g, b, a)（疊在 under 上）→ (r, g, b) 0～255。"""
    c = color.strip().lower()
    m = re.match(r"^#([0-9a-f]{6})$", c)
    if m:
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = re.match(r"^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$", c)
    if m:
        r, g, b = (float(m.group(i)) for i in (1, 2, 3))
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        return tuple(a * x + (1 - a) * u for x, u in zip((r, g, b), under))
    raise ValueError("看不懂的顏色：%s" % color)


def luminance(c):
    def lin(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


class TestPalette(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = read("css/style.css")
        cls.light = variables(block(cls.css, ":root"))
        cls.dark_body = block(cls.css, 'html[data-theme="dark"]')
        cls.dark = variables(cls.dark_body)

    def test_light_is_the_spec_palette(self):
        """對照組：--high 改回規格原本的 #b0541a、或任何一個色票的值不對 → 這一條會紅。"""
        for k, v in LIGHT.items():
            self.assertEqual(self.light.get(k), v, k)

    def test_dark_is_the_old_root_word_for_word_and_has_every_variable(self):
        """深色那一組＝A1-4 以前的 :root 原樣搬過去；兩組的變數一模一樣。對照組：深色少一個變數 → 這一條會紅。"""
        self.assertIsNotNone(self.dark_body, "找不到 html[data-theme=\"dark\"] 那一組")
        self.assertTrue(self.dark_body.lstrip("\n").startswith(DARK_BEFORE), "深色那一組的前 18 個要跟 A1-4 以前的 :root 一字不差")
        colors = lambda d: set(k for k in d if k not in ("--radius", "--gap"))   # noqa: E731
        self.assertEqual(colors(self.light), set(self.dark), "兩組的顏色變數不一樣")
        self.assertIn("--radius", self.light)
        self.assertNotIn("--radius", self.dark)                                   # 圓角與間距不跟著主題換

    def test_text_and_states_pass_aa_on_every_light_background(self):
        """對照組：--text 改成低對比 → 這一條會紅。"""
        bad = []
        for fg in TEXT_VARS:
            for bg in BACKS:
                r = contrast(rgb(self.light[fg]), rgb(self.light[bg]))
                if r < 4.5:
                    bad.append("%s 在 %s 上 %.2f" % (fg, bg, r))
        self.assertEqual(bad, [])

    def test_the_other_text_colors_pass_aa_too(self):
        card = rgb(self.light["--card"])
        err_box = rgb("rgba(255, 95, 86, .10)", card)                            # .err-box／.rp-err 的淡紅底
        self.assertGreaterEqual(contrast(rgb(self.light["--error-text"]), err_box), 4.5)
        self.assertGreaterEqual(contrast(rgb(self.light["--on-gold"]), rgb(self.light["--gold"])), 4.5)
        for k in PIE_AND_LINES:
            self.assertGreaterEqual(contrast(rgb(self.light[k]), card), 4.5, k)
        self.assertEqual(len(set(self.light[k] for k in PIE_AND_LINES[:7])), 7, "圓餅七色要分得出來")

    def test_formerly_hard_coded_colors_keep_their_dark_values(self):
        """以前寫死在規則裡、或寫死在 charts.js 的顏色，深色的值就是原本的值（深色畫面不變）。"""
        want = {"--head-bg": "rgba(14, 20, 32, 0.94)", "--error-text": "#ffb4ae", "--on-gold": "#1a1408", "--chart-ma60": "#a78bfa",
                "--chart-grid": "rgba(38, 51, 73, .75)", "--chart-fill": "rgba(91, 156, 248, .12)",
                "--pie-tw": "#5b9cf8", "--pie-us": "#a78bfa", "--pie-bond": "#38bdf8", "--pie-gold": "#e5b567",
                "--pie-fund": "#4ade80", "--pie-cash": "#8fa2bd", "--pie-other": "#f7b955", "--pie-none": "#64748b"}
        for k, v in want.items():
            self.assertEqual(self.dark.get(k), v, k)
        rules = self.css.split('html[data-theme="dark"] {', 1)[1].split("\n}", 1)[1]      # 深色那一組之後的所有規則
        for literal in ("#ffb4ae", "#1a1408", "rgba(14, 20, 32"):
            self.assertNotIn(literal, rules, "規則裡還寫死 %s" % literal)


class TestChartsAndPages(unittest.TestCase):
    def test_charts_js_has_no_colors_of_its_own(self):
        """對照組：charts.js 寫回一個色碼 → 這一條會紅。"""
        js = read("js/charts.js")
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", js), [])
        self.assertEqual(re.findall(r"rgba?\(", js), [])
        css = read("css/style.css")
        light, dark = variables(block(css, ":root")), variables(block(css, 'html[data-theme="dark"]'))
        for name in sorted(set(re.findall(r"'(--[a-z0-9-]+)'", js))):
            self.assertIn(name, light, name)
            self.assertIn(name, dark, name)

    def test_every_page_has_the_same_theme_script_before_the_stylesheet(self):
        scripts = {}
        css = read("css/style.css")
        light_bg, dark_bg = variables(block(css, ":root"))["--bg"], variables(block(css, 'html[data-theme="dark"]'))["--bg"]
        for page in PAGES:
            text = read(page)
            head = text.split("</head>", 1)[0]
            m = re.search(r"<script>/\* U1：.*?</script>", head, flags=re.S)
            self.assertIsNotNone(m, "%s 的 head 沒有主題腳本" % page)
            scripts[page] = m.group(0)
            self.assertLess(head.index(m.group(0)), head.index('rel="stylesheet"'), "%s：主題腳本要在樣式表之前" % page)
            self.assertIn('<meta name="theme-color" content="%s">' % light_bg, head, page)
            self.assertIn("'%s' : '%s'" % (dark_bg, light_bg), m.group(0), page)
            self.assertIn("css/style.css?v=20261107-1", head, page)
        self.assertEqual(len(set(scripts.values())), 1, "五個頁面的主題腳本要一字不差")
        self.assertIn("iw-theme", scripts["index.html"])
        self.assertIn('id="theme-light"', read("settings.html"))
        self.assertIn('id="theme-dark"', read("settings.html"))


def theme_script():
    m = re.search(r"<script>(/\* U1：.*?)</script>", read("index.html"), flags=re.S)
    return m.group(1)


PAGE = """<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="theme-color" content="#000000">
<script>try { localStorage.removeItem('iw-theme'); } catch (e) {}</script>
<script>%(script)s</script>
<link rel="stylesheet" href="%(css)s">
</head><body><canvas id="c" width="300" height="150"></canvas><pre id="result"></pre>
<script src="%(charts)s"></script>
<script>
(function () {
  var out = {}, root = document.documentElement;
  function bg() { return getComputedStyle(root).getPropertyValue('--bg').trim(); }
  function meta() { return document.querySelector('meta[name="theme-color"]').getAttribute('content'); }
  out.fresh = { attr: root.getAttribute('data-theme'), bg: bg(), meta: meta(), chartText: window.Charts.COLORS.text, pie: window.Charts.PIE_COLORS['台股'] };
  try { localStorage.setItem('iw-theme', 'dark'); } catch (e) { out.storageError = String(e); }
  eval(document.querySelectorAll('script')[1].textContent);                     // 重新整理頁面的效果：同一段腳本再跑一次
  window.Charts.refreshColors();
  out.dark = { attr: root.getAttribute('data-theme'), bg: bg(), meta: meta(), chartText: window.Charts.COLORS.text, pie: window.Charts.PIE_COLORS['台股'] };
  var fired = 0;
  document.addEventListener('iw:theme', function () { fired++; });
  window.dispatchEvent(new StorageEvent('storage', { key: 'iw-theme', newValue: 'light' }));
  out.otherTab = { attr: root.getAttribute('data-theme'), bg: bg(), meta: meta(), fired: fired, chartText: window.Charts.COLORS.text };
  window.dispatchEvent(new StorageEvent('storage', { key: 'something-else', newValue: 'dark' }));
  out.otherKey = { attr: root.getAttribute('data-theme') };
  window.IwTheme.apply('dark');
  out.applied = { attr: root.getAttribute('data-theme'), meta: meta() };
  window.IwTheme.apply('purple');
  out.unknown = { attr: root.getAttribute('data-theme') };
  try { localStorage.removeItem('iw-theme'); } catch (e) {}
  document.getElementById('result').textContent = JSON.stringify(out);
}());
</script></body></html>
"""


class TestSwitchingInABrowser(unittest.TestCase):
    """真的開一頁：主題腳本是從 index.html 抽出來的那一段（五頁一字不差，上面那一條釘住）。"""

    @classmethod
    def setUpClass(cls):
        browser = find_browser()
        if not browser:
            raise AssertionError("找不到 Edge／Chrome，主題切換沒辦法測（這一組刻意不跳過）。IW_BROWSER 可指定路徑。")
        tmp = tempfile.mkdtemp(prefix="iw-theme-")
        try:
            url = lambda rel: "file:///" + os.path.join(ROOT, rel).replace("\\", "/").lstrip("/")   # noqa: E731
            page = os.path.join(tmp, "theme.html")
            with io.open(page, "w", encoding="utf-8") as fh:
                fh.write(PAGE % {"script": theme_script(), "css": url("css/style.css"), "charts": url("js/charts.js")})
            p = subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run", "--allow-file-access-from-files",
                                "--user-data-dir=" + os.path.join(tmp, "profile"), "--virtual-time-budget=3000", "--dump-dom",
                                "file:///" + page.replace("\\", "/").lstrip("/")],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
            dom = p.stdout.decode("utf-8", "replace")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        found = re.findall(r'<pre id="result">(.*?)</pre>', dom, flags=re.S)
        if not found or not found[-1].strip():
            raise AssertionError("測試頁沒有產出結果（瀏覽器：%s）。DOM 開頭：%s" % (browser, dom[:300]))
        cls.r = json.loads(html.unescape(found[-1]))

    def test_nothing_chosen_means_light(self):
        f = self.r["fresh"]
        self.assertEqual((f["attr"], f["bg"], f["meta"]), (None, "#f1f5f8", "#f1f5f8"))
        self.assertEqual(f["chartText"], "#55666f")                              # 圖表讀到的是淺色的 --text-dim
        self.assertEqual(f["pie"], "#2f6690")

    def test_stored_dark_means_dark_and_the_charts_follow(self):
        self.assertNotIn("storageError", self.r)
        d = self.r["dark"]
        self.assertEqual((d["attr"], d["bg"], d["meta"]), ("dark", "#0e1420", "#0e1420"))
        self.assertEqual(d["chartText"], "#8fa2bd")                              # 深色＝以前寫死在 charts.js 的那個值
        self.assertEqual(d["pie"], "#5b9cf8")

    def test_another_tab_switching_is_followed_and_redraws(self):
        o = self.r["otherTab"]
        self.assertEqual((o["attr"], o["bg"], o["meta"]), (None, "#f1f5f8", "#f1f5f8"))
        self.assertGreaterEqual(o["fired"], 1)                                   # 測試頁把主題腳本跑了兩次（模擬重新整理），所以聽了兩次；真的頁面只跑一次
        self.assertEqual(o["chartText"], "#55666f")                              # iw:theme → charts.js 重讀顏色
        self.assertEqual(self.r["otherKey"]["attr"], None)                       # 別的鍵不理
        self.assertEqual(self.r["applied"], {"attr": "dark", "meta": "#0e1420"})
        self.assertEqual(self.r["unknown"]["attr"], None)                        # 不認得的值＝淺色


if __name__ == "__main__":
    unittest.main()
