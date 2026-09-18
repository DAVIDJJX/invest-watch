#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_finmind_fx.py — 停點 8-0：匯率改由雲端經 FinMind 抓（台銀每日匯率）

跑法：
    python -m unittest discover -s scripts -p "test_*.py" -v

這一組【完全不連網路】：FinMind 的回應是寫死的假 JSON，Fetcher 換成只會記網址的替身；
歷史檔全部寫在暫存目錄，碰不到真實的 data/history/。

釘住的事（每一條都有對照組，見 docs/CHANGELOG.md 8-0）：
  1. 歷史點的日期只認回應自己的 date 欄，不是「現在」；新點標 dateSource=finmind。
  2. msg 不是 success（或 status 不是 200）→ 失敗，而且【一個點都不准寫】。
     FinMind 出錯時 HTTP 照樣是 200，這個判斷是唯一的防線。
  3. 回應是空陣列 ≠ 錯誤 ≠ 空的歷史：舊的點一個都不能少，卡片顯示最後一筆的日期。
  4. 純追加：來源重送舊日期（就算數字不同）也不准改寫既有的點——台銀 CSV 時期的
     dateSource=csv 要原封不動留著。
  5. 輕量更新不重抓（cadence=full）；但上一次沒有結果或是失敗的就要抓。
  6. 每個幣別一個請求、從歷史最後一天問起、完全不碰 rate.bot.com.tw。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import fetch_data as fd                                      # noqa: E402

ASSET = {"id": "fx_usd", "name": "美元 / 台幣", "type": "finmind_fx", "symbol": "USD",
         "currency": "TWD", "unit": "台幣 / 1 美元", "decimals": 3, "priceLabel": "即期賣出"}


def day(n):
    """n 天前的日期字串。用相對日期而不是寫死：寫死的話哪天真的在那一天跑測試，
    「日期不可以是今天」這種斷言就分辨不出對錯了（跟 test_history_dates.py 同一個理由）。"""
    return (fd.now_tpe() - timedelta(days=n)).strftime("%Y-%m-%d")


def row(d, sell, code="USD", **over):
    r = {"date": d, "currency": code,
         "cash_buy": round(sell - 0.45, 3), "cash_sell": round(sell + 0.22, 3),
         "spot_buy": round(sell - 0.1, 3), "spot_sell": sell}
    r.update(over)
    return r


def ok(rows):
    return {"msg": "success", "status": 200, "data": rows}


class FakeFetcher:
    """不連網的 Fetcher 替身：記下每一個被要求的網址，回傳寫死的 JSON。"""

    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.payload


class HistSandbox(unittest.TestCase):
    """把 fetch_data 的歷史目錄換成暫存目錄。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-finmind-")
        self._orig = fd.HIST_DIR
        fd.HIST_DIR = self.tmp

    def tearDown(self):
        fd.HIST_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self, points):
        fd.save_history(ASSET, points)

    def raw(self):
        with open(fd.hist_path(ASSET["id"]), "rb") as fh:
            return fh.read()

    def run_like_main(self, payload, light=False, prev=None):
        """照 fetch_data.main() 對每個標的做的那三步走一遍：
        呼叫 handler → 沒丟例外且 points 不是 None 才 save_history。
        「失敗時一個點都不准寫」要驗到存檔這一步才算數——handler 本身從來不寫檔，
        只驗 handler 的話，就算把 msg 的判斷拿掉，歷史檔也「看起來」沒被動過。
        """
        f = FakeFetcher(payload)
        ctx = {"light": light, "prevAssets": prev or {}}
        points, quote = fd.handle_finmind_fx(f, ASSET, ctx)
        if points is not None:
            fd.save_history(ASSET, points)
        return points, quote, f


# ==========================================================================
# 1. 解析：什麼算成功、什麼算失敗
# ==========================================================================

class TestParseFinMindFx(unittest.TestCase):

    def test_rows_come_back_sorted_and_dated_by_the_api(self):
        got = fd.parse_finmind_fx(ok([row(day(1), 31.9), row(day(3), 31.6), row(day(2), 31.75)]),
                                  "USD")
        self.assertEqual([r["date"] for r in got], [day(3), day(2), day(1)])
        self.assertEqual(got[-1]["spotSell"], 31.9)
        self.assertEqual(got[-1]["cashSell"], 32.12)

    def test_message_other_than_success_is_a_failure_even_with_data(self):
        """對照組：把 parse_finmind_fx 裡 msg 的判斷改壞，這一條會紅。

        注意這份假回應【故意帶著看起來正常的資料列】：真的出錯時不保證 data 是空的，
        判斷一壞，這幾列就會被當成真的匯率寫進歷史。
        """
        bad = {"msg": "Requests reach the upper limit.", "status": 200,
               "data": [row(day(1), 31.9)]}
        with self.assertRaises(fd.FetchError):
            fd.parse_finmind_fx(bad, "USD")

    def test_status_other_than_200_is_a_failure(self):
        bad = {"msg": "success", "status": 402, "data": [row(day(1), 31.9)]}
        with self.assertRaises(fd.FetchError):
            fd.parse_finmind_fx(bad, "USD")

    def test_missing_data_array_is_a_failure(self):
        for j in ({"msg": "success", "status": 200},
                  {"msg": "success", "status": 200, "data": {"oops": 1}},
                  ["not", "a", "dict"], None):
            with self.assertRaises(fd.FetchError):
                fd.parse_finmind_fx(j, "USD")

    def test_empty_data_is_not_an_error(self):
        self.assertEqual(fd.parse_finmind_fx(ok([]), "USD"), [])

    def test_unusable_rows_are_dropped(self):
        rows = [row(day(2), 31.7),
                row("", 31.8),                          # 沒有日期
                row("2026/09/01", 31.8),                # 日期格式不對
                row(day(1), 31.9, spot_sell=None),      # 沒有即期賣出
                row(day(1), 4.7, code="CNY"),           # 別的幣別
                row(day(4), 31.5, cash_buy=-1, cash_sell=0)]   # 沒有現金牌價
        got = fd.parse_finmind_fx(ok(rows), "USD")
        self.assertEqual([r["date"] for r in got], [day(4), day(2)])
        self.assertIsNone(got[0]["cashBuy"], "0 或負數是『沒有這個牌價』，不是價格")
        self.assertIsNone(got[0]["cashSell"])


# ==========================================================================
# 2. handler：日期、純追加、失敗與空回應
# ==========================================================================

class TestHandleFinMindFx(HistSandbox):

    # 台銀 CSV 時期留下來的歷史：最前面一個連 dateSource 都沒有的老點，其餘是 csv
    def legacy(self):
        return [{"d": day(6), "spotBuy": 31.4, "spotSell": 31.5, "c": 31.5},
                {"d": day(5), "spotBuy": 31.5, "spotSell": 31.6, "c": 31.6, "dateSource": "csv"},
                {"d": day(4), "spotBuy": 31.59, "spotSell": 31.69, "c": 31.69, "dateSource": "csv"}]

    def test_new_days_are_appended_dated_by_the_api_and_tagged_finmind(self):
        self.seed(self.legacy())
        payload = ok([row(day(4), 31.69), row(day(3), 31.75), row(day(2), 31.9)])
        pts, q, _ = self.run_like_main(payload)

        saved = fd.load_history(ASSET["id"])
        self.assertEqual([p["d"] for p in saved], [day(6), day(5), day(4), day(3), day(2)])
        self.assertNotIn(day(0), [p["d"] for p in saved], "來源沒給今天，就不可以出現今天的點")
        self.assertEqual([p.get("dateSource") for p in saved[-2:]], ["finmind", "finmind"])
        self.assertEqual(saved[-1]["c"], 31.9, "主價 c＝即期賣出")
        self.assertEqual(saved[-1]["spotBuy"], 31.8)
        self.assertNotIn("cashBuy", saved[-1], "歷史點的形狀不變：現金牌價只放在卡片，不進歷史")

        # 卡片：價格、買賣價、日期全部出自同一列
        self.assertEqual(q["status"], "ok")
        self.assertEqual(q["type"], "finmind_fx")
        self.assertEqual(q["date"], day(2))
        self.assertEqual(q["price"], 31.9)
        self.assertEqual(q["spotSell"], 31.9)
        self.assertEqual(q["cashSell"], 32.12)
        self.assertEqual(q["prevClose"], 31.75)
        self.assertEqual(q["sourceLabel"], "台銀每日匯率（經 FinMind）")
        self.assertIsNone(q["historyNote"])

    def test_todays_row_is_accepted_when_the_api_itself_says_today(self):
        self.seed(self.legacy())
        pts, q, _ = self.run_like_main(ok([row(day(4), 31.69), row(day(0), 32.0)]))
        self.assertEqual(q["date"], day(0))
        self.assertEqual(fd.load_history(ASSET["id"])[-1]["dateSource"], "finmind")

    def test_failure_message_writes_nothing_at_all(self):
        """對照組：把 msg 的判斷改壞 → 這一條必須紅（假回應裡的那一列會被寫進歷史）。"""
        self.seed(self.legacy())
        before = self.raw()
        bad = {"msg": "Token is expired.", "status": 200, "data": [row(day(1), 99.999)]}
        with self.assertRaises(fd.FetchError):
            self.run_like_main(bad)
        self.assertEqual(self.raw(), before, "來源說失敗時，歷史檔一個位元組都不可以動")

    def test_empty_response_keeps_every_old_point_and_says_why(self):
        """今天那一筆還沒發布：不是錯誤，更不是「歷史變成空的」。

        對照組：把 handler 最後改成「不管有沒有新點都回傳 new_pts」→ 這一條必須紅
        （main 會拿空陣列去存檔，整份歷史就被清空了）。
        """
        self.seed(self.legacy())
        before = self.raw()
        pts, q, _ = self.run_like_main(ok([]))
        self.assertIsNone(pts, "沒有新點就不要碰歷史檔")
        self.assertEqual(self.raw(), before)
        self.assertEqual(q["status"], "ok")
        self.assertEqual(q["date"], day(4), "卡片要顯示最後一筆的日期，不准顯示成今天")
        self.assertEqual(q["price"], 31.69)
        self.assertTrue(q["historyNote"], "沒有新增歷史點時要說明原因")
        for k in ("spotBuy", "spotSell", "cashBuy", "cashSell", "historyNote", "sourceLabel"):
            self.assertIn(k, q, "前端與相容性檢查都靠這些鍵，值可以是 None，鍵不能少")

    def test_old_days_are_never_rewritten_even_if_the_source_disagrees(self):
        """對照組：拿掉「只收比最後一天新的日期」那個判斷 → 這一條必須紅。"""
        old = self.legacy()
        self.seed(old)
        payload = ok([row(day(6), 30.0), row(day(5), 30.1), row(day(4), 30.2),
                      row(day(3), 31.75)])
        pts, q, _ = self.run_like_main(payload)
        saved = fd.load_history(ASSET["id"])
        self.assertEqual(saved[:3], old, "舊的點（含沒有 dateSource 的老點與 csv 點）要原封不動")
        self.assertEqual(saved[-1]["d"], day(3))
        self.assertTrue(any(day(4) in w for w in q.get("warnings") or []),
                        "來源對最後一天的說法跟本站不同時要提醒，但不覆寫")

    def test_light_run_is_skipped_when_the_previous_result_was_ok(self):
        """對照組：拿掉 handler 開頭的輕量跳過 → 這一條必須紅（盤中每 30 分鐘都去敲 FinMind）。"""
        self.seed(self.legacy())
        f = FakeFetcher(ok([]))
        with self.assertRaises(fd.SkipAsset):
            fd.handle_finmind_fx(f, ASSET, {"light": True,
                                            "prevAssets": {"fx_usd": {"status": "ok"}}})
        self.assertEqual(f.urls, [], "被跳過的那一輪不可以發出任何請求")

    def test_light_run_still_fetches_when_there_is_nothing_to_carry_over(self):
        """剛搬到雲端時雲端分片裡還沒有這一項；上一次若是失敗的也一樣要抓。"""
        self.seed(self.legacy())
        for prev in ({}, {"fx_usd": {"status": "error"}}):
            pts, q, f = self.run_like_main(ok([row(day(4), 31.69)]), light=True, prev=prev)
            self.assertEqual(len(f.urls), 1)
            self.assertEqual(q["status"], "ok")

    def test_one_request_per_currency_from_the_last_known_day_and_never_the_bank(self):
        self.seed(self.legacy())
        pts, q, f = self.run_like_main(ok([row(day(4), 31.69)]))
        self.assertEqual(len(f.urls), 1, "每個幣別每一輪只准一個請求")
        url = f.urls[0]
        self.assertTrue(url.startswith("https://api.finmindtrade.com/"), url)
        self.assertIn("dataset=TaiwanExchangeRate", url)
        self.assertIn("data_id=USD", url)
        self.assertIn("start_date=%s" % day(4), url, "只問歷史最後一天起的資料，不是整段重抓")
        self.assertNotIn("rate.bot.com.tw", url, "匯率不可以再碰台銀")

    def test_first_run_without_any_history_backfills_about_half_a_year(self):
        pts, q, f = self.run_like_main(ok([row(day(3), 31.75), row(day(2), 31.9)]))
        self.assertIn("start_date=%s" % day(fd.FINMIND_BACKFILL_DAYS), f.urls[0])
        self.assertEqual([p["d"] for p in fd.load_history(ASSET["id"])], [day(3), day(2)])

    def test_no_history_and_no_data_is_an_error(self):
        with self.assertRaises(fd.FetchError):
            self.run_like_main(ok([]))


# ==========================================================================
# 3. 登記：dateSource 等級、type、設定檔、其他模組
# ==========================================================================

class TestFinMindIsRegisteredEverywhere(unittest.TestCase):

    def test_finmind_is_a_documented_and_graded_date_source(self):
        """對照組：把 DATE_SOURCE_GRADE 裡的 finmind 那一行刪掉 → 這一條必須紅。

        沒登記的 dateSource 會被 merge_points 當成 0 級：新點永遠蓋不過任何舊點，
        盤中價卻可以蓋掉它——圖表看起來完全正常，沒有人看得出來。
        """
        self.assertIn("finmind", fd.DATE_SOURCE)
        self.assertIn("finmind", fd.DATE_SOURCE_GRADE)
        self.assertEqual(fd.DATE_SOURCE_GRADE["finmind"], fd.DATE_SOURCE_GRADE["csv"],
                         "經 FinMind 拿到的就是台銀每日牌價，跟台銀 CSV 同一級")
        merged = fd.merge_points(
            [{"d": "2026-09-16", "c": 31.94, "dateSource": "finmind"}],
            [{"d": "2026-09-16", "c": 31.0, "dateSource": "intraday", "provisional": True}])
        self.assertEqual(merged[0]["c"], 31.94, "盤中價不可以蓋掉每日牌價")

    def test_handler_is_registered(self):
        self.assertIs(fd.HANDLERS.get("finmind_fx"), fd.handle_finmind_fx)

    def test_real_assets_json_points_both_fx_assets_at_finmind_on_the_cloud(self):
        with open(os.path.join(ROOT, "data", "assets.json"), encoding="utf-8") as fh:
            assets = {a["id"]: a for a in json.load(fh)["assets"]}
        for aid, code in (("fx_usd", "USD"), ("fx_cny", "CNY")):
            a = assets[aid]
            self.assertEqual(a["type"], "finmind_fx")
            self.assertEqual(a["symbol"], code)
            self.assertEqual(a["owner"], "cloud", "台銀擋雲端；FinMind 不擋，所以歸雲端")
            self.assertEqual(a.get("cadence"), "full", "一天一筆，只在完整更新抓")

    def test_other_modules_treat_the_new_type_as_bank_of_taiwan_daily_data(self):
        """每日牌價是定案值、週末不掛牌：這兩個清單少了新 type，規則就無聲地漏掉匯率。"""
        import merge_latest as ml
        import cleanup_fake_history_points as cl
        self.assertIn("finmind_fx", ml.BOT_TYPES)
        self.assertIn("finmind_fx", cl.BOT_TYPES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
