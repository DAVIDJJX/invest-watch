#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_history_dates.py — 歷史點的日期一律取自來源，不准用「現在」

跑法：
    python -m unittest discover -s scripts -p "test_*.py" -v

為什麼要有這一組？
    2026-09-05（週六）出過事：台銀週末不掛牌，抓到的還是週五 19:52 那一筆，
    但寫歷史時用 now_tpe() 當日期，於是憑空多了一個週六的點，值跟週五一模一樣。
    走勢圖上就會有一根不存在的 K 線。

    這跟「台股估算價不可以寫進 history」是同一條原則：
    歷史檔裡的每一天，都必須是那一天真的存在的報價。

這一組測試完全不連網路：需要外部資料的地方都用假的回應替換掉。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data as fd                                      # noqa: E402


def ymd(dt):
    return dt.strftime("%Y-%m-%d")


class HistDirSandbox(unittest.TestCase):
    """把 data/history 換成暫存目錄，測試絕對不要碰到真實資料。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-hist-")
        self._orig_dir = fd.HIST_DIR
        fd.HIST_DIR = self.tmp

    def tearDown(self):
        fd.HIST_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self, asset_id, points):
        with open(os.path.join(self.tmp, "%s.json" % asset_id), "w",
                  encoding="utf-8") as fh:
            json.dump({"points": points}, fh)


class TestDateOfBotQuote(unittest.TestCase):
    """台銀掛牌時間的日期解析。讀不出來一定要回 None，不能亂猜一個日期。"""

    def test_parses(self):
        self.assertEqual(fd.date_of_bot_quote("2026/09/04 19:52"), "2026-09-04")
        self.assertEqual(fd.date_of_bot_quote("2026/9/4 15:14"), "2026-09-04")
        self.assertEqual(fd.date_of_bot_quote("  2026/09/04 15:14 "), "2026-09-04")

    def test_returns_none_when_unreadable(self):
        for bad in (None, "", "掛牌時間讀不到", "09/04 19:52", 12345, {}):
            self.assertIsNone(fd.date_of_bot_quote(bad), repr(bad))


class TestGoldPassbookDate(HistDirSandbox):
    """① 掛牌時間是前一天、今天才執行 → 不可以產生「今天」的點。

    這裡刻意用「相對於現在的前一天」當掛牌時間，而不是寫死 2026/09/04：
    寫死的話，萬一哪天真的在那一天跑測試，對照組就抓不到錯了。
    情境等同於規格說的「掛牌時間是週五、在週六執行」。
    """

    def run_gold(self, quote_time):
        asset = {"id": "gold_twd", "name": "黃金存摺（台幣）", "symbol": "TWD",
                 "type": "bot_gold", "currency": "TWD", "unit": "元 / 公克",
                 "decimals": 0, "priceLabel": "本行賣出"}
        ctx = {"light": True, "goldPageBuy": 4517.0, "goldPageSell": 4570.0,
               "goldQuoteTime": quote_time}
        # light=True 且主頁有牌價時不會發出任何請求，所以 f 傳 None 就夠
        return fd.handle_bot_gold(None, asset, ctx)

    def test_point_is_dated_by_the_quote_not_by_today(self):
        yesterday = ymd(fd.now_tpe() - timedelta(days=1))
        quote_time = yesterday.replace("-", "/") + " 19:52"
        self.seed("gold_twd", [{"d": yesterday, "c": 4570.0, "dateSource": "quote"}])

        points, quote = self.run_gold(quote_time)
        days = [p["d"] for p in points]

        self.assertEqual(days, [yesterday],
                         "只該有掛牌那一天，實際是 %s" % days)
        self.assertNotIn(ymd(fd.now_tpe()), days,
                         "憑空生出了『今天』的點——這正是要修掉的 bug")
        self.assertEqual(points[-1]["dateSource"], "quote")
        self.assertEqual(quote["date"], yesterday)

    def test_same_day_twice_updates_instead_of_adding(self):
        """② 同一天抓兩次 → 只有一筆，值是後抓到的。"""
        yesterday = ymd(fd.now_tpe() - timedelta(days=1))
        quote_time = yesterday.replace("-", "/") + " 19:52"
        self.seed("gold_twd", [{"d": yesterday, "c": 4500.0, "sell": 4500.0,
                                "dateSource": "quote"}])

        points, _ = self.run_gold(quote_time)
        self.assertEqual(len(points), 1, "同一天不可以變成兩筆")
        self.assertEqual(points[0]["d"], yesterday)
        self.assertEqual(points[0]["c"], 4570.0, "值要換成後抓到的")

    def test_no_quote_time_means_no_history_and_a_visible_note(self):
        """③ 來源沒有時間戳 → 不寫歷史，而且該項要有明確標記。

        沒有掛牌時間就不知道這筆牌價屬於哪一天。這時 light 模式沒有東西可寫，
        會退回去抓走勢表；這裡把走勢表換成假的，確認寫進歷史的是走勢表自己的
        日期，主頁那筆沒有被硬塞成今天。
        """
        chart_day = ymd(fd.now_tpe() - timedelta(days=3))
        calls = []

        def fake_chart(f, currency, light=False):
            calls.append(currency)
            return [{"d": chart_day, "buy": 4500.0, "sell": 4510.0,
                     "c": 4510.0, "dateSource": "chart"}]

        orig = fd.fetch_bot_gold
        fd.fetch_bot_gold = fake_chart
        try:
            points, quote = self.run_gold(None)
        finally:
            fd.fetch_bot_gold = orig

        self.assertEqual(calls, ["TWD"], "沒有掛牌時間時應該退回去抓走勢表")
        self.assertEqual([p["d"] for p in points], [chart_day])
        self.assertNotIn(ymd(fd.now_tpe()), [p["d"] for p in points])
        self.assertIn("掛牌時間", quote.get("historyNote") or "",
                      "要在該項標記原因，不能安靜地跳過")


class TestGoldBarDate(HistDirSandbox):

    BARS = [{"spec": s, "buy": 1.0, "sell": v} for s, v in
            (("1 公斤", 4604070.0), ("500 公克", 2305859.0), ("250 公克", 1155100.0),
             ("100 公克", 463349.0), ("金鑽 1 台兩", 173944.0))]

    def run_bar(self, quote_time):
        asset = {"id": "gold_bar", "name": "台銀實體黃金條塊", "symbol": "BAR",
                 "type": "bot_gold_bar", "currency": "TWD", "unit": "元",
                 "decimals": 0, "priceLabel": "本行賣出"}
        orig = fd.fetch_gold_bars
        fd.fetch_gold_bars = lambda f: (list(self.BARS), quote_time)
        try:
            return fd.handle_bot_gold_bar(None, asset, {"light": False})
        finally:
            fd.fetch_gold_bars = orig

    def test_dated_by_quote_time(self):
        yesterday = ymd(fd.now_tpe() - timedelta(days=1))
        to_save, quote = self.run_bar(yesterday.replace("-", "/") + " 15:14")
        self.assertEqual([p["d"] for p in to_save], [yesterday])
        self.assertEqual(to_save[-1]["dateSource"], "quote")
        self.assertEqual(quote["date"], yesterday)

    def test_no_quote_time_writes_nothing(self):
        old_day = ymd(fd.now_tpe() - timedelta(days=5))
        self.seed("gold_bar", [{"d": old_day, "c": 4600.0, "dateSource": "quote"}])
        to_save, quote = self.run_bar(None)
        self.assertIsNone(to_save, "沒有掛牌時間就不可以寫檔")
        self.assertIn("掛牌時間", quote.get("historyNote") or "")
        # 卡片照樣看得到既有的走勢，只是不新增
        self.assertTrue(quote["hasHistory"] is False or quote["points"] >= 1)


class TestFxHistoryDate(HistDirSandbox):
    """台銀當日匯率 CSV【沒有資料日期欄】（2026-09-05 實測），
    所以它只能當現價，不能當某一天的歷史。歷史一律取自有資料日期的 L6M 檔。"""

    DAY = {"USD": {"cashBuy": 31.23, "spotBuy": 31.555,
                   "cashSell": 31.9, "spotSell": 31.705}}

    def run_fx(self, light, seeded=None, l6m=None):
        asset = {"id": "fx_usd", "name": "美元 / 台幣", "symbol": "USD",
                 "type": "bot_fx", "currency": "TWD", "unit": "台幣 / 1 美元",
                 "decimals": 3, "priceLabel": "即期賣出"}
        if seeded:
            self.seed("fx_usd", seeded)
        calls = []

        def fake_l6m(f, code):
            calls.append(code)
            return list(l6m or [])

        orig = fd.fetch_bot_fx_history
        fd.fetch_bot_fx_history = fake_l6m
        try:
            pts, q = fd.handle_bot_fx(None, asset,
                                      {"light": light, "fxDay": dict(self.DAY)})
            return pts, q, calls
        finally:
            fd.fetch_bot_fx_history = orig

    def test_full_run_uses_the_dated_csv(self):
        d1 = ymd(fd.now_tpe() - timedelta(days=2))
        d2 = ymd(fd.now_tpe() - timedelta(days=1))
        pts, q, calls = self.run_fx(
            light=False,
            l6m=[{"d": d1, "c": 31.6, "dateSource": "csv"},
                 {"d": d2, "c": 31.705, "dateSource": "csv"}])
        self.assertEqual(calls, ["USD"])
        self.assertEqual([p["d"] for p in pts], [d1, d2])
        self.assertNotIn(ymd(fd.now_tpe()), [p["d"] for p in pts],
                         "當日 CSV 沒有日期，不可以被寫成『今天』的歷史點")
        self.assertTrue(all(p["dateSource"] == "csv" for p in pts))
        # 現價照樣顯示（現價本來就不需要日期）
        self.assertEqual(q["price"], 31.705)

    def test_light_run_adds_nothing_and_says_why(self):
        old_day = ymd(fd.now_tpe() - timedelta(days=1))
        pts, q, calls = self.run_fx(
            light=True,
            seeded=[{"d": old_day, "c": 31.6, "dateSource": "csv"}])
        self.assertEqual(calls, [], "輕量更新不該重抓歷史")
        self.assertEqual([p["d"] for p in pts], [old_day], "不可以多出一天")
        self.assertIn("資料日期", q.get("historyNote") or "")

    def test_light_run_still_backfills_when_there_is_no_history_at_all(self):
        d1 = ymd(fd.now_tpe() - timedelta(days=1))
        pts, q, calls = self.run_fx(
            light=True, l6m=[{"d": d1, "c": 31.7, "dateSource": "csv"}])
        self.assertEqual(calls, ["USD"], "一筆歷史都沒有時，輕量模式也要補一次")
        self.assertEqual([p["d"] for p in pts], [d1])


class TestMergePoints(unittest.TestCase):

    def test_same_day_is_updated_not_appended(self):
        old = [{"d": "2026-09-03", "c": 1.0}, {"d": "2026-09-04", "c": 2.0}]
        new = [{"d": "2026-09-04", "c": 2.5}]
        got = fd.merge_points(old, new)
        self.assertEqual([p["d"] for p in got], ["2026-09-03", "2026-09-04"])
        self.assertEqual(got[-1]["c"], 2.5)

    def test_point_without_a_date_is_dropped(self):
        """沒有日期的點一律丟掉——這是最後一道防線。"""
        got = fd.merge_points([], [{"c": 1.0}, {"d": None, "c": 2.0},
                                   {"d": "2026-09-04", "c": 3.0}])
        self.assertEqual([p["d"] for p in got], ["2026-09-04"])


class TestEveryPointIsTagged(unittest.TestCase):
    """每一個產生歷史點的地方都要標 dateSource，否則稽核會有破口。"""

    def test_no_untagged_point_creation_in_source(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fetch_data.py")
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        bad = []
        for i, line in enumerate(lines):
            if '"d":' not in line:
                continue
            chunk = "".join(lines[i:i + 8])
            if "dateSource" not in chunk:
                bad.append("%d: %s" % (i + 1, line.strip()))
        self.assertEqual(bad, [], "這些地方產生歷史點卻沒有標 dateSource：\n" +
                         "\n".join(bad))

    def test_source_never_dates_a_point_by_now(self):
        """整支程式不可以再出現「用現在當歷史日期」。"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fetch_data.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn('"d": now_tpe()', src)
        self.assertNotIn('now_tpe().strftime("%Y-%m-%d")', src)



class TestCleanupScript(HistDirSandbox):
    """清理腳本：甲類要被列出來、乙類只列不刪、比特幣的週末點絕對不能碰。"""

    def setUp(self):
        HistDirSandbox.setUp(self)
        import cleanup_fake_history_points as cl
        self.cl = cl

    def test_classifies_weekend_and_flat_days(self):
        # 2026-09-04 週五、09-05 週六、09-07 週一、09-08 週二
        self.seed("gold_twd", [
            {"d": "2026-09-03", "c": 4549.0},     # 週四
            {"d": "2026-09-04", "c": 4570.0},     # 週五
            {"d": "2026-09-05", "c": 4570.0},     # 週六 → 甲類
            {"d": "2026-09-07", "c": 4580.0},     # 週一，值有變 → 都不是
            {"d": "2026-09-08", "c": 4580.0},     # 週二，值沒變 → 乙類
        ])
        a, b = self.cl.scan({"id": "gold_twd", "type": "bot_gold"})
        self.assertEqual([r[0] for r in a], ["2026-09-05"])
        self.assertEqual([r[0] for r in b], ["2026-09-08"])

    def test_only_bank_of_taiwan_assets_are_scanned(self):
        """比特幣週末真的有交易，它不可以進掃描範圍。"""
        ids = [a["id"] for a in self.cl.load_assets()]
        self.assertIn("gold_twd", ids)
        self.assertIn("fx_usd", ids)
        self.assertNotIn("btc", ids, "比特幣的週末點是真的，絕對不能碰")
        self.assertNotIn("twii", ids)

    def test_delete_a_removes_only_class_a(self):
        self.seed("fx_usd", [
            {"d": "2026-09-04", "c": 31.705},     # 週五
            {"d": "2026-09-05", "c": 31.705},     # 週六 → 甲類
            {"d": "2026-09-08", "c": 31.705},     # 週二、值沒變 → 乙類
        ])
        a, b = self.cl.scan({"id": "fx_usd", "type": "bot_fx"})
        drop = {r[0] for r in a}
        kept = [p for p in fd.load_history("fx_usd") if p["d"] not in drop]
        self.assertEqual([p["d"] for p in kept], ["2026-09-04", "2026-09-08"],
                         "乙類不可以被一起刪掉")

    def test_class_b_before_first_run_cannot_be_a_fake_point(self):
        """這個 bug 只會產生日期 >= 第一次執行那天的點。

        更早的點來自台銀官方帶日期的走勢表與 CSV，值連兩天一樣只是正常現象。
        """
        self.assertLess("2026-06-05", self.cl.FIRST_RUN_DATE)
        self.assertEqual(self.cl.FIRST_RUN_DATE, "2026-08-31")

if __name__ == "__main__":
    unittest.main(verbosity=2)
