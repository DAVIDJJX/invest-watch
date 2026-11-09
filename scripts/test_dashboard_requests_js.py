#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_dashboard_requests_js.py — 分析系列 A1-7：真的開瀏覽器量儀表板的請求數，並檢查每張卡的圖示列

做法：這支測試自己起一個只聽 127.0.0.1 的小伺服器（把倉庫當網站根目錄、每個請求記一筆），
用無頭 Chrome／Edge 開 scripts/test_dashboard_live.html——那一頁把真正的 index.html 放進 iframe，
等圖示列補完，回報瀏覽器自己記的每一個請求與「首屏畫完」的時間標記。兩邊的紀錄（瀏覽器的、伺服器的）都要對得上。
不連外網：所有請求都打本機；台銀、證交所、Yahoo 一個請求都沒有。
★ 找不到瀏覽器時這一組是【紅】的，不是跳過。要指定瀏覽器：環境變數 IW_BROWSER=完整路徑。

釘住的事（對照組見 docs/CHANGELOG.md A1-7）：
  1. 首屏畫完之前的請求＝7 個靜態檔＋行情、報告各 1 個＋每張卡最多 1 個日線檔（現在 14 張卡，所以是 23 個）；一個都不多。
     G8B：國際換算估算不是卡、不另外抓；台銀卡切到估算序列時抓估算的檔代替自己的（TestDashboardWhenGoldIsEstimated）。
     某個標的這一輪抓不到（例如台銀擋下連線、沒有日線）時少幾個就是幾個，照 data/latest.json 算；永遠不超過上限（A1-4）。
  2. 首屏畫完之後最多再 5 個：js/plain.js、js/glossary.js、data/analysis 的 risk／cost／fx。點開卡片、展開「分析」不會再多抓。
  3. 每張卡在標題列與展開區之間有一條圖示列（不在標題列裡面）；每個圖示有圖示、面向名稱、狀態詞、文字替代，規則與數字在點開的細節裡。
  4. 位置的狀態詞跟卡片名稱旁邊的燈號一字不差；條塊沒有燈號就是「資料不足」。卡片上不畫「不適用」。
  5. 指標格的標題變成可以點的名詞，標題的字與下面的小字都沒變。
  6. 分析檔抓不到：圖示寫「暫時讀不到」、下面寫「分析資料暫時讀不到」；白話層載不到：整條寫「分析資料暫時讀不到」。首屏不受影響。

自己跑一次看量測結果：python scripts/test_dashboard_requests_js.py --measure
"""
import datetime
import functools
import html
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from test_freshness_js import find_browser   # noqa: E402  同一套找瀏覽器的規則

NOW = "2026-09-30T16:30:00+08:00"            # 只影響「今天星期幾」的標示，資料一個字都不會變
STATIC = ["css/style.css", "lib/chart.umd.min.js", "js/indicators.js", "js/charts.js", "js/freshness.js", "js/card-analysis.js", "js/app.js"]
DEFERRED = ["js/plain.js", "js/glossary.js", "data/analysis/risk.json", "data/analysis/cost.json", "data/analysis/fx.json"]
AFTER_MAX = 5                                # 首屏畫完之後最多再幾個請求（裁決）
VOCAB = {"position": ["相對低檔區", "中性", "相對高檔區", "資料不足", "暫時讀不到"],
         "risk": ["平靜", "正常", "劇烈", "資料不足", "暫時讀不到"],
         "cost": ["便宜", "正常", "偏貴", "資料不足", "暫時讀不到"],
         "trend": ["向上", "持平", "向下", "資料不足", "暫時讀不到"]}      # 卡片上不畫「不適用」
JUDGEMENT = ["買" + "進", "賣" + "出", "建" + "議", "總" + "分", "分" + "數", "偏" + "買", "偏" + "賣", "主" + "力"]
PRICE_NAMES = ["本行" + "賣" + "出", "本行買入", "即期" + "賣" + "出", "即期買入"]
COUNTING = re.compile(r"[0-9０-９一二兩三四五六七八九十]+\s*(個|項)\s*(面向|指標|燈號|圖示)")


def card_ids(latest):
    """G8B：國際換算估算（estimateOf 有值）不是一張卡。儀表板上的卡＝latest 裡其餘的每一項。"""
    return [aid for aid, a in latest["assets"].items() if not a.get("estimateOf")]


def card_view(aid, latest, now):
    """跟 js/freshness.js 的 estimateView 同一條規則：台銀黃金這一輪失敗、落後超過門檻、估算是好的 → 這張卡改用估算（抓估算的日線）。
    回 (要抓的日線檔的 id, 算首屏用的那一份)。落後幾個交易日用 scripts/analyze.py 的 trading_days_behind（前後端同一條，另有測試釘住）。"""
    import analyze as A
    from datetime import datetime as _dt
    a = latest["assets"][aid]
    est = next((e for e in latest["assets"].values() if e.get("estimateOf") == aid), None)
    gate = (est or {}).get("switchAfterTradingDays")
    if a.get("type") == "bot_gold" and est and a.get("status") != "ok" and est.get("status") == "ok" \
            and isinstance(gate, (int, float)) and (est.get("points") or 0) > 1:
        behind = A.trading_days_behind((a.get("lastGood") or {}).get("date"), _dt.fromisoformat(now).date())
        if behind is not None and behind > gate:
            return est["id"], est
    return aid, a


def expected_first_screen(latest=None, now=NOW):
    """照 js/app.js 的 loadHistories 同一條規則，從 data/latest.json 算出首屏該抓哪些資料檔：每張卡最多 1 個日線檔
    （G8B：估算不是卡、不另外抓；切到估算序列的台銀卡抓估算的檔來代替自己的）。"""
    if latest is None:
        with open(os.path.join(ROOT, "data", "latest.json"), encoding="utf-8") as fh:
            latest = json.load(fh)
    ids, files = [], []
    for aid in card_ids(latest):
        fid, v = card_view(aid, latest, now)
        pts = v.get("points") or 0
        if v.get("status") == "ok" and (pts > 1 or (v.get("type") == "bot_gold_bar" and pts >= 1)):
            ids.append(aid)
            files.append(fid)
    data = ["data/latest.json", "data/report-latest.json"] + ["data/history/%s.json" % i for i in files]
    return latest, ids, data


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):                                   # 測試輸出不要被存取紀錄洗掉
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].lstrip("/")
        with self.server.lock:
            self.server.seen.append(path)
        if any(path.startswith(b) for b in self.server.block):
            self.send_error(404, "blocked by the test")
            return
        if path in self.server.override:                            # G8B：這個路徑改送測試準備的內容（不動倉庫裡的檔）
            body = self.server.override[path]
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class Site(object):
    """只聽 127.0.0.1 的小伺服器；block 裡的路徑開頭一律回 404（模擬檔案抓不到）；override：路徑 → 改送的內容（bytes）。"""

    def __init__(self, block=(), override=None):
        self.block = tuple(block)
        self.override = dict(override or {})

    def __enter__(self):
        handler = functools.partial(_Handler, directory=ROOT)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.httpd.seen, self.httpd.block, self.httpd.lock = [], self.block, threading.Lock()
        self.httpd.override = self.override
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=10)

    @property
    def seen(self):
        with self.httpd.lock:
            return list(self.httpd.seen)


def measure(block=(), open_card="tw00646", override=None, now=NOW):
    """開一次儀表板，回 (瀏覽器那一頁的報告, 伺服器看到的請求路徑清單)。"""
    browser = find_browser()
    if not browser:
        raise AssertionError("找不到 Edge／Chrome，儀表板的請求數量測沒辦法跑（這一組刻意不跳過）。IW_BROWSER 可指定路徑。")
    profile = tempfile.mkdtemp(prefix="iw-browser-")
    try:
        with Site(block, override) as site:
            url = "%s/scripts/test_dashboard_live.html?now=%s&open=%s" % (site.base, now.replace("+", "%2B").replace(":", "%3A"), open_card)
            p = subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run", "--window-size=1300,2600",
                                "--user-data-dir=" + profile, "--virtual-time-budget=30000", "--dump-dom", url],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=180)
            dom = p.stdout.decode("utf-8", "replace")
            seen = site.seen
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    found = re.findall(r'<pre id="result">(.*?)</pre>', dom, flags=re.S)
    if not found or not found[-1].strip():
        raise AssertionError("量測頁沒有產出結果（瀏覽器：%s）。DOM 開頭：%s" % (browser, dom[:300]))
    return json.loads(html.unescape(found[-1])), seen


def split_by_mark(report):
    mark = report.get("mark")
    before = sorted(r["path"] for r in report["resources"] if r["start"] < mark)
    after = sorted(r["path"] for r in report["resources"] if r["start"] >= mark)
    return before, after


def site_requests(seen):
    """伺服器看到的請求裡，屬於儀表板自己的那些（量測頁、index.html、favicon 不算）。"""
    skip = ("scripts/test_dashboard_live.html", "index.html", "favicon.ico")
    return [p for p in seen if p not in skip]


class TestDashboardRequests(unittest.TestCase):
    """一切正常時：首屏 23、之後 5；圖示列、名詞、摺疊區。"""

    @classmethod
    def setUpClass(cls):
        cls.latest, cls.history_ids, cls.data_files = expected_first_screen()
        cls.report, cls.seen = measure()

    def test_page_really_ran(self):
        r = self.report
        self.assertEqual(r.get("problems"), [])
        self.assertFalse(r.get("timedOut"), "等不到圖示列補完")
        self.assertTrue(r.get("loaded"))
        self.assertEqual(r.get("dashboardDone"), "done")
        self.assertIsNotNone(r.get("mark"), "儀表板沒有留下「首屏畫完」的時間標記")

    def test_first_screen_requests_are_exactly_the_static_files_and_one_file_per_asset(self):
        """對照組：首屏請求數超過 23（把白話層掛回 index.html、或 boot 就抓分析檔）→ 這一條會紅。"""
        before, _after = split_by_mark(self.report)
        self.assertEqual(before, sorted(STATIC + self.data_files), "首屏畫完之前多了或少了請求")
        self.assertEqual(len(STATIC), 7)
        self.assertEqual(len(before), 9 + len(self.history_ids))
        tags = self.report["staticTags"]                                             # 量的時候圖示列已經補完：原本 7 個標籤，後來才多出兩支
        self.assertEqual([t for t in tags if t in STATIC], STATIC)
        self.assertEqual(sorted(tags), sorted(STATIC + DEFERRED[:2]))
        ceiling = len(STATIC) + 2 + len(card_ids(self.latest))                     # 規則的上限：每張卡都有日線檔的時候（G8B：估算不是卡）
        self.assertLessEqual(len(before), ceiling, "首屏畫完之前超過「靜態檔＋行情、報告＋每個標的 1 個日線檔」")
        if len(card_ids(self.latest)) == 14:                                        # 現在的監控清單：14 張卡（G8B：估算兩項不算）
            self.assertEqual(ceiling, 23)
            if len(self.history_ids) == 14:                                          # 台銀擋住的那天少幾個日線檔，照資料算（上面兩條）；全部抓得到才要剛好 23
                self.assertEqual(len(before), 23, "首屏畫完之前應該剛好 23 個請求")

    def test_at_most_five_more_requests_after_the_first_screen(self):
        """對照組：首屏之後超過上限（多抓一個 decompose.json）→ 這一條會紅。"""
        _before, after = split_by_mark(self.report)
        self.assertLessEqual(len(after), AFTER_MAX, "首屏畫完之後的請求超過 %d 個：%s" % (AFTER_MAX, after))
        self.assertEqual(after, sorted(DEFERRED))
        self.assertEqual(AFTER_MAX, 5)

    def test_opening_a_card_and_its_analysis_fold_fetches_nothing_more(self):
        self.assertEqual(self.report["resourcesAfterInteraction"], len(self.report["resources"]))
        self.assertEqual(len(site_requests(self.seen)), len(self.report["resources"]))

    def test_server_saw_the_same_requests_in_the_same_order(self):
        """第二份獨立的紀錄：伺服器收到的順序——延後的那五個，全部排在首屏那 23 個之後。"""
        seen = site_requests(self.seen)
        self.assertEqual(sorted(seen), sorted(STATIC + self.data_files + DEFERRED))
        first_deferred = min(seen.index(p) for p in DEFERRED)
        last_first_screen = max(seen.index(p) for p in STATIC + self.data_files)
        self.assertLess(last_first_screen, first_deferred)
        for p in seen:
            self.assertFalse(p.startswith("data/history-long"), p)
            self.assertNotIn("decompose", p)

    def test_every_card_has_a_strip_between_head_and_body(self):
        cards = self.report["cards"]
        self.assertEqual(sorted(c["id"] for c in cards), sorted(card_ids(self.latest)))   # G8B：估算不是卡
        for c in cards:
            self.assertTrue(c["hasStrip"], c["id"])
            self.assertTrue(c["stripBetweenHeadAndBody"], "%s：圖示列要在標題列與展開區之間" % c["id"])
            self.assertFalse(c["stripInsideHead"], "%s：標題列整塊是按鈕，圖示不可以放在裡面" % c["id"])
            self.assertFalse(c["waiting"], "%s：圖示列還停在「讀取中」" % c["id"])
            self.assertIsNone(c["missing"], "%s：一切正常時不該出現「分析資料暫時讀不到」" % c["id"])
            self.assertGreaterEqual(len(c["chips"]), 2, c["id"])
            self.assertEqual(c["chips"][0]["aspect"], "position", c["id"])

    def test_every_chip_has_icon_name_word_and_a_detail_with_a_rule(self):
        """對照組：圖示沒有狀態詞、圖示沒有規則 → 這一條會紅。"""
        for c in self.report["cards"]:
            details = dict((d["id"], d) for d in c["details"])
            self.assertEqual(len(details), len(c["chips"]), c["id"])
            for chip in c["chips"]:
                tag = "%s/%s" % (c["id"], chip["aspect"])
                self.assertTrue(chip["icon"], tag + " 沒有圖示")
                self.assertTrue(chip["iconLabel"], tag + " 的圖示沒有文字替代")
                self.assertTrue(chip["name"], tag + " 沒有面向名稱")
                self.assertTrue(chip["word"], tag + " 圖示旁邊沒有狀態詞")
                self.assertIn(chip["word"], VOCAB[chip["aspect"]], tag)
                self.assertTrue(chip["label"].startswith("%s：%s" % (chip["name"], chip["word"])), tag)
                self.assertEqual(chip["expanded"], "false", tag)
                d = details.get(chip["controls"])
                self.assertIsNotNone(d, tag + " 找不到它的細節")
                self.assertEqual(d["aspect"], chip["aspect"], tag)
                self.assertTrue(d["hidden"], tag + " 的細節預設要收合")
                self.assertTrue(d["because"], tag + " 沒有寫這次是哪個數字")
                self.assertTrue((d["rule"] or "").startswith("規則："), tag + " 沒有印規則")
                self.assertEqual(d["emptyPlain"], 0, tag + " 有一行白話是空的")
                if chip["state"] in ("low", "mid", "high"):
                    self.assertGreaterEqual(d["facts"], 1, tag + " 有狀態卻沒有列數字")

    def test_position_chip_is_the_lamp_word_for_word(self):
        """對照組：位置面向的字眼被改成偏貴 → 這一條會紅。"""
        seen_lamps = 0
        for c in self.report["cards"]:
            pos = c["chips"][0]
            self.assertIn(pos["word"], ("相對低檔區", "中性", "相對高檔區", "資料不足"), c["id"])
            if c["lamp"] is None:
                self.assertEqual(pos["word"], "資料不足", "%s 沒有燈號，位置應該是資料不足" % c["id"])
                continue
            seen_lamps += 1
            self.assertEqual(pos["word"], c["lamp"], "%s：位置的狀態詞跟名稱旁邊的燈號不一樣" % c["id"])
        self.assertGreaterEqual(seen_lamps, 10)
        bar = [c for c in self.report["cards"] if c["id"] == "gold_bar"]
        if bar:
            self.assertIsNone(bar[0]["lamp"])                                        # 條塊維持不掛燈號

    def test_not_applicable_is_never_drawn_on_a_card_and_fx_cards_link_to_the_helper(self):
        for c in self.report["cards"]:
            for chip in c["chips"]:
                self.assertNotEqual(chip["word"], "不適用", c["id"])
                self.assertNotEqual(chip["state"], "none", c["id"])
        by_id = dict((c["id"], c) for c in self.report["cards"])
        for aid, a in ((i, self.latest["assets"][i]) for i in card_ids(self.latest)):   # G8B：估算不是卡
            if a.get("type") in ("finmind_fx", "bot_fx"):
                pos = [d for d in by_id[aid]["details"] if d["aspect"] == "position"][0]
                self.assertTrue(any(x.startswith("5 年位置：") and x.endswith("（見換匯助手）") for x in pos["extra"]), aid)
                self.assertIn("analysis.html#fx", pos["links"], aid)
            else:
                for d in by_id[aid]["details"]:
                    if d["aspect"] == "position":
                        self.assertEqual(d["extra"], [], aid)

    def test_no_trading_words_and_no_counting_on_the_strips(self):
        """對照組：出現禁用詞 → 這一條會紅。"""
        text = self.report["glanceText"]
        for e in PRICE_NAMES:
            text = text.replace(e, "")
        self.assertEqual([w for w in JUDGEMENT if w in text], [])
        self.assertIsNone(COUNTING.search(text))
        self.assertTrue(COUNTING.search("3 個面向偏貴"))

    def test_clicking_a_chip_opens_only_its_own_detail(self):
        click = self.report["click"]
        n = len(self.report["cards"][[c["id"] for c in self.report["cards"]].index("tw00646")]["chips"])
        closed = ",".join(["false"] * n) + "|" + ",".join(["h"] * n)
        self.assertEqual(click["before"], ",".join(["false"] * n))
        self.assertEqual(click["afterFirst"], ",".join(["true"] + ["false"] * (n - 1)) + "|" + ",".join(["v"] + ["h"] * (n - 1)))
        self.assertEqual(click["afterSecond"], ",".join(["false", "true"] + ["false"] * (n - 2)) + "|" + ",".join(["h", "v"] + ["h"] * (n - 2)))
        self.assertEqual(click["afterClose"], closed)
        self.assertFalse(click["cardOpenedByChip"], "點圖示不可以順便把整張卡展開")

    def test_metric_titles_become_clickable_terms_with_the_same_words(self):
        m = self.report["metrics"]
        self.assertTrue(m["opened"])
        self.assertEqual(m["titles"], ["RSI (14)", "現價 vs MA20", "現價 vs MA60", "近 10 日漲跌", "年化波動 (30日)", "歷史資料點"])
        self.assertEqual(m["terms"], ["RSI", "移動平均", "移動平均", "近 10 日漲跌", "年化波動"])
        self.assertEqual(m["pending"], 0)
        self.assertEqual(m["rangeTerm"], "區間位置｜52 週區間位置")
        self.assertTrue(m["definition"].startswith("RSI："), m["definition"])
        self.assertTrue(m["cardStillOpen"], "點名詞不可以把卡片收起來")
        self.assertTrue(m["definitionClosed"])
        self.assertEqual(len(m["notes"]), 6)                                          # 既有的小字一個都沒少
        self.assertIn("0～100，越高代表近期漲多", m["notes"])
        self.assertIn("僅供了解價格起伏大小", m["notes"])
        self.assertIn("本站保留最近 400 個交易日", m["notes"])

    def test_analysis_fold_still_works_and_gets_plain_lines(self):
        fold = self.report["fold"]
        self.assertEqual(fold["hint"], "風險、相關、成本的完整數字")
        self.assertGreaterEqual(fold["rows"], 7)
        self.assertGreaterEqual(fold["plainLines"], 5)
        self.assertEqual(fold["emptyPlain"], 0)
        self.assertNotIn("分析資料暫時讀不到", fold["text"])


class TestDashboardWhenAnalysisIsUnreachable(unittest.TestCase):
    """三個分析檔都抓不到：首屏照常，圖示照實寫「暫時讀不到」。"""

    @classmethod
    def setUpClass(cls):
        cls.latest, cls.history_ids, cls.data_files = expected_first_screen()
        cls.report, cls.seen = measure(block=("data/analysis/",))

    def test_first_screen_is_untouched(self):
        self.assertFalse(self.report.get("timedOut"))
        self.assertEqual(self.report.get("dashboardDone"), "done")
        seen = site_requests(self.seen)
        self.assertEqual(sorted(seen), sorted(STATIC + self.data_files + DEFERRED))
        self.assertLess(max(seen.index(p) for p in STATIC + self.data_files), min(seen.index(p) for p in DEFERRED))

    def test_chips_say_temporarily_unavailable(self):
        for c in self.report["cards"]:
            self.assertEqual(c["missing"], "分析資料暫時讀不到", c["id"])
            words = dict((chip["aspect"], chip) for chip in c["chips"])
            self.assertEqual(words["risk"]["word"], "暫時讀不到", c["id"])
            self.assertEqual(words["risk"]["state"], "error", c["id"])
            self.assertEqual(words["cost"]["word"], "暫時讀不到", c["id"])
            if c["lamp"] is not None:
                self.assertEqual(words["position"]["word"], c["lamp"], c["id"])       # 位置不靠分析檔

    def test_fold_says_so_too(self):
        self.assertIn("分析資料暫時讀不到", self.report["fold"]["text"])
        self.assertNotIn("這個標的目前沒有分析項目", self.report["fold"]["text"])


EST_NOW = "2026-11-09T10:00:00+08:00"          # 星期一；假資料裡台銀最後一筆牌告是 10-28，落後 8 個交易日


def estimate_fixture():
    """G8B：台銀黃金被擋、有國際換算估算時的儀表板（假資料，只送給這一組；倉庫裡的檔不動）。
    從倉庫現在的 latest.json 出發，把台銀兩項改成「這一輪失敗、上次成功是 10-28」，再加上兩項估算與它們的日線。數字一看就是假的。"""
    with open(os.path.join(ROOT, "data", "latest.json"), encoding="utf-8") as fh:
        latest = json.load(fh)
    assets = dict((k, v) for k, v in latest["assets"].items() if not v.get("estimateOf"))
    hist = {}
    for gid, eid, base in (("gold_twd", "gold_twd_est", 3000.0), ("gold_cny", "gold_cny_est", 600.0)):
        g = dict(assets[gid])
        g.update({"status": "error", "price": None, "error": "台銀要求瀏覽器驗證（回傳驗證頁），程式過不了，本輪不再重試。",
                  "errorAt": "2026-11-09T09:30:00+08:00", "spark": None, "points": None, "date": None,
                  "lastGood": {"price": base, "bars": None, "date": "2026-10-28", "fetchedAt": "2026-10-28T17:00:00+08:00", "quoteTime": "2026/10/28 16:50"}})
        assets[gid] = g
        pts, d = [], datetime.date(2025, 9, 24)
        while d <= datetime.date(2026, 11, 9):
            if d.weekday() < 5:
                pts.append({"d": d.isoformat(), "c": round(base + len(pts) % 37, 4), "dateSource": "estimate-backfill"})
            d += datetime.timedelta(days=1)
        pts[-1]["dateSource"] = "estimate"
        hist[eid] = {"id": eid, "points": pts}
        assets[eid] = {"id": eid, "name": g["name"] + "國際換算估算", "shortName": eid, "group": "貴金屬", "type": "gold_est",
                       "estimateOf": gid, "currency": g.get("currency"), "unit": g.get("unit"), "decimals": g.get("decimals"),
                       "priceLabel": "國際換算估算", "status": "ok", "price": pts[-1]["c"], "prevClose": pts[-2]["c"],
                       "change": round(pts[-1]["c"] - pts[-2]["c"], 4), "changePct": 0.1, "date": pts[-1]["d"], "points": len(pts),
                       "spark": [p["c"] for p in pts[-40:]], "freshness": "fresh", "switchAfterTradingDays": 1, "typicalErrorPct": 0.6,
                       "backfillThrough": pts[-2]["d"], "estimate": {"note": "金價取 11-09 09:55、匯率取 11-09 09:58（台北時間）"}}
    latest["assets"] = assets
    s = latest.get("summary") or {}
    bad = [k for k, v in assets.items() if v.get("status") != "ok"]
    latest["summary"] = dict(s, ok=len(assets) - len(bad), error=len(bad), errorIds=bad, total=len(assets))
    files = {"data/latest.json": json.dumps(latest, ensure_ascii=False).encode("utf-8")}
    for eid, h in hist.items():
        files["data/history/%s.json" % eid] = json.dumps(h, ensure_ascii=False).encode("utf-8")
    return latest, files


class TestDashboardWhenGoldIsEstimated(unittest.TestCase):
    """G8B（規格 3.3、Cowork 裁決 5(a)）：台銀黃金被擋、落後超過 1 個交易日時——
    估算不自成一張卡；台銀卡的主數字寫「沿用 <日期> 牌告」、「更新失敗」照舊、下面一行標「國際換算估算」；
    走勢線、燈號、展開與圖示列的位置改用估算序列並寫出來；首屏仍是每張卡最多 1 個日線檔（抓估算的檔代替台銀的），上限 23 不變；
    狀態列的計數不算估算那兩項。"""

    @classmethod
    def setUpClass(cls):
        cls.latest, files = estimate_fixture()
        _l, cls.history_ids, cls.data_files = expected_first_screen(cls.latest, EST_NOW)
        cls.report, cls.seen = measure(override=files, now=EST_NOW, open_card="gold_twd")
        cls.cards = dict((c["id"], c) for c in cls.report["cards"])

    def test_page_ran(self):
        self.assertEqual(self.report.get("problems"), [])
        self.assertFalse(self.report.get("timedOut"))
        self.assertEqual(self.report.get("dashboardDone"), "done")

    def test_estimates_are_not_cards(self):
        self.assertEqual(sorted(self.cards), sorted(card_ids(self.latest)))
        self.assertNotIn("gold_twd_est", self.cards)
        self.assertNotIn("gold_cny_est", self.cards)
        self.assertEqual(len(self.cards), 14)

    def test_first_screen_swaps_the_gold_files_and_keeps_the_ceiling(self):
        """對照組：估算兩項也各抓一個日線檔（首屏 25）→ 這一條會紅。"""
        before, _after = split_by_mark(self.report)
        self.assertEqual(before, sorted(STATIC + self.data_files))
        for gid, eid in (("gold_twd", "gold_twd_est"), ("gold_cny", "gold_cny_est")):
            self.assertIn("data/history/%s.json" % eid, before)
            self.assertNotIn("data/history/%s.json" % gid, before)
        self.assertLessEqual(len(before), len(STATIC) + 2 + len(card_ids(self.latest)))
        self.assertEqual(len(STATIC) + 2 + len(card_ids(self.latest)), 23)

    def test_gold_card_says_carried_and_shows_the_estimate_line(self):
        """對照組：拿掉「國際換算估算」字樣、或主數字不寫沿用 → 這一條會紅。"""
        for gid in ("gold_twd", "gold_cny"):
            c = self.cards[gid]
            self.assertIn("沿用 10-28 牌告", c["headText"], gid)
            self.assertIn("2026/10/28 16:50 掛牌", c["headText"], gid)
            self.assertIn("更新失敗", c["headText"], gid)
            self.assertIn("這一項資料更新失敗", c["headText"], gid)              # 原本的錯誤說明還在（DOM 只增不減）
            self.assertTrue(c["estLine"], gid)
            self.assertTrue(c["estLine"].startswith("國際換算估算"), gid)
            self.assertIn("不是台銀牌告", c["estLine"], gid)
            self.assertIn("金價取 11-09 09:55、匯率取 11-09 09:58", c["estLine"], gid)
            self.assertIn("改用國際換算估算的序列", c["estLine"], gid)
            self.assertIn("2026-10-28", c["estLine"], gid)
        self.assertIn("條塊不做估算", self.cards["gold_bar"]["estLine"] or "")

    def test_lamp_and_position_follow_the_estimate_series_and_say_so(self):
        for gid in ("gold_twd", "gold_cny"):
            c = self.cards[gid]
            self.assertIsNotNone(c["lamp"], gid)
            pos = c["chips"][0]
            self.assertEqual(pos["word"], c["lamp"], gid)
            d = [x for x in c["details"] if x["aspect"] == "position"][0]
            self.assertIn("國際換算估算", d["dates"] or "", gid)

    def test_status_counts_leave_the_estimates_out(self):
        ok_cards = len([i for i in card_ids(self.latest) if self.latest["assets"][i].get("status") == "ok"])
        self.assertIn("%d 項成功" % ok_cards, self.report["statusText"])
        self.assertNotIn("估算", self.report["statusText"])

    def test_no_trading_words_on_the_gold_cards(self):
        for gid in ("gold_twd", "gold_cny", "gold_bar"):
            text = self.cards[gid]["headText"] or ""
            for e in PRICE_NAMES:
                text = text.replace(e, "")
            self.assertEqual([w for w in JUDGEMENT if w in text], [], gid)


class TestDashboardWhenThePlainLayerIsUnreachable(unittest.TestCase):
    """白話層（js/plain.js）載不到：整條圖示列寫「分析資料暫時讀不到」，其餘照常。"""

    @classmethod
    def setUpClass(cls):
        cls.report, cls.seen = measure(block=("js/plain.js",))

    def test_strips_say_unavailable_and_nothing_else_breaks(self):
        r = self.report
        self.assertFalse(r.get("timedOut"))
        self.assertEqual(r.get("dashboardDone"), "done")
        for c in r["cards"]:
            self.assertEqual(c["chips"], [], c["id"])
            self.assertEqual(c["missing"], "分析資料暫時讀不到", c["id"])
            self.assertFalse(c["waiting"], c["id"])
        self.assertGreaterEqual(len([c for c in r["cards"] if c["lamp"] in ("相對低檔區", "中性", "相對高檔區")]), 10)   # 燈號照常
        self.assertTrue(r["metrics"]["opened"])
        self.assertEqual(r["metrics"]["terms"], ["RSI", "移動平均", "移動平均", "近 10 日漲跌", "年化波動"])          # 名詞解釋還在
        self.assertGreaterEqual(r["fold"]["rows"], 7)                                  # 摺疊區照常，只是沒有白話
        self.assertEqual(r["fold"]["plainLines"], 0)


def print_measurement():
    latest, ids, data = expected_first_screen()
    report, seen = measure()
    before, after = split_by_mark(report)
    static = [p for p in before if p in STATIC]
    jsons = [p for p in before if p not in STATIC]
    print("儀表板請求數量測（無頭瀏覽器實開；資料是倉庫裡現在的檔案）")
    print("  首屏畫完之前：%d 個請求＝靜態檔 %d 個＋資料檔 %d 個（行情 1、報告 1、日線 %d）"
          % (len(before), len(static), len(jsons), len([p for p in jsons if p.startswith("data/history/")])))
    for p in before:
        print("    " + p)
    print("  首屏畫完之後：%d 個請求（上限 %d）" % (len(after), AFTER_MAX))
    for p in after:
        print("    " + p)
    print("  點開一張卡、展開「分析」之後：總數 %d（沒有再多抓）" % report["resourcesAfterInteraction"])
    order = site_requests(seen)
    print("  伺服器收到的順序：延後的第一個排第 %d，首屏的最後一個排第 %d（共 %d 個）"
          % (min(order.index(p) for p in DEFERRED) + 1, max(order.index(p) for p in STATIC + data) + 1, len(order)))
    print("  對外連線：0（全部打 127.0.0.1）")


if __name__ == "__main__":
    if "--measure" in sys.argv:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print_measurement()
        sys.exit(0)
    unittest.main()
