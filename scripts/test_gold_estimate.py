#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_gold_estimate.py — G8B：台銀黃金被擋 → 國際換算估算序列＋本機停止硬打

全部離線：Yahoo 的回應用假的、台銀的「驗證頁」用假的，一個真的連線都沒有（台銀 0 次）。
數字一看就是假的（金價 2000、匯率 30、人民幣 7、台銀 3000）。只有「校正係數用倉庫的歷史檔重算＝寫死的值」那一條讀倉庫裡的公開牌價。

釘住的事（規格 G8B、Cowork 裁決 01_Cowork裁決_覆述八件.md）：
  1. 估算＝GC=F ÷ 每盎司公克數（analyze.py 那一份）× 同一時刻的匯率 × 寫死的校正係數；校正係數用倉庫資料重算＝寫死的值。
  2. 金價與匯率的報價時刻相差超過上限：這一輪不算、沿用上一次（不算失敗）；連上一次都沒有才標失敗。
  3. 點的日期＝報價時刻的台北日期（不是執行時間）；兩者不同天或週末不寫點；15:30 那一輪定案、其餘是暫定點。
  4. 第一次跑只讀倉庫的檔回補（標 estimate-backfill）；估算只寫 *_est 自己的檔，台銀那三個檔一個 byte 都不動。
  5. 新的日期來源都有登記等級：定案點蓋得過暫定點與回補點，暫定點蓋不過定案點。
  6. 停止硬打：連續被驗證頁擋 3 輪之後，只在週一～五 09:00 以後的第一輪試一次、週末不試；成功歸零、連線失敗不算。
  7. 錯誤訊息不分雲端或筆電：「台銀要求瀏覽器驗證…程式過不了」。
  8. 估算不自成一列：analyze.py 的趨勢、report.py 的報告都不替估算各出一筆；台銀落後超過門檻時趨勢改用估算序列並照實寫。
  9. 同一個常數只有一份：前後端的交易日規則同一組日期表、文件與頁尾的數字跟 assets.json 一樣。
"""
import contextlib
import datetime
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import analyze as A        # noqa: E402
import fetch_data as fd    # noqa: E402
import report as R         # noqa: E402

TPE = datetime.timezone(datetime.timedelta(hours=8))
with open(os.path.join(ROOT, "data", "assets.json"), encoding="utf-8") as _fh:
    CFG = json.load(_fh)
GE = CFG["goldEstimate"]
EST = dict((a["id"], a) for a in CFG["assets"] if a.get("type") == "gold_est")


def epoch(y, mo, d, h, mi):
    return int(datetime.datetime(y, mo, d, h, mi, tzinfo=TPE).timestamp())


def yahoo(symbol, price, at):
    return {"chart": {"result": [{"meta": {"symbol": symbol, "regularMarketPrice": price, "regularMarketTime": at,
                                           "currency": "USD"}}], "error": None}}


def weekdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def write_hist(folder, aid, points):
    with open(os.path.join(folder, "%s.json" % aid), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"id": aid, "points": points}, fh, ensure_ascii=False)


class FakeYahoo(object):
    """只回 Yahoo 的假報價；同一個代號要問幾次都記下來。任何不是 Yahoo 的網址都算失敗（台銀 0 次）。"""

    def __init__(self, quotes):
        self.quotes, self.urls, self.blocked, self.count = dict(quotes), [], {}, 0

    def get(self, url, **kw):
        self.urls.append(url)
        if "query1.finance.yahoo.com" not in url:
            raise AssertionError("估算只准問 Yahoo：%s" % url)
        sym = url.split("/chart/")[1].split("?")[0].replace("%3D", "=")
        price, at = self.quotes[sym]
        return yahoo(sym, price, at)


class Sandbox(object):
    """暫存資料夾裡的 data/：assets.json 是倉庫那一份（估算的設定與校正係數照用），歷史檔是假的。fetch_data 的路徑全部指過來。"""

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-g8b-")
        self.data = os.path.join(self.tmp, "data")
        self.hist = os.path.join(self.data, "history")
        self.src = os.path.join(self.data, "sources")
        os.makedirs(self.hist)
        os.makedirs(self.src)
        shutil.copyfile(os.path.join(ROOT, "data", "assets.json"), os.path.join(self.data, "assets.json"))
        days = weekdays(datetime.date(2025, 9, 22), datetime.date(2026, 11, 3))
        write_hist(self.hist, "gold_intl", [{"d": d, "c": 2000.0 + (i % 7), "dateSource": "yahoo"} for i, d in enumerate(days)])
        write_hist(self.hist, "fx_usd", [{"d": d, "spotBuy": 29.95, "spotSell": 30.05, "c": 30.05, "dateSource": "finmind"} for d in days])
        write_hist(self.hist, "fx_cny", [{"d": d, "spotBuy": 4.27, "spotSell": 4.31, "c": 4.31, "dateSource": "finmind"} for d in days])
        for aid, base in (("gold_twd", 3000.0), ("gold_cny", 700.0)):
            write_hist(self.hist, aid, [{"d": d, "buy": base - 30, "sell": base, "c": base, "dateSource": "chart"}
                                        for d in weekdays(datetime.date(2026, 9, 1), datetime.date(2026, 10, 28))])
        write_hist(self.hist, "gold_bar", [{"d": "2026-10-28", "g1000": 3000000.0, "c": 3000.0, "dateSource": "quote"}])
        self.patches = [mock.patch.object(fd, "DATA_DIR", self.data), mock.patch.object(fd, "HIST_DIR", self.hist),
                        mock.patch.object(fd, "ASSETS_FILE", os.path.join(self.data, "assets.json")),
                        mock.patch.object(fd, "LATEST_FILE", os.path.join(self.data, "latest.json")),
                        mock.patch.object(fd, "SOURCES_DIR", self.src)]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def raw(self, aid):
        with open(os.path.join(self.hist, "%s.json" % aid), "rb") as fh:
            return fh.read()

    def shard(self, source):
        with open(os.path.join(self.src, "%s.json" % source), encoding="utf-8") as fh:
            return json.load(fh)


def run_main(argv, fake_get=None, now=None):
    """跑一次 fetch_data.main()（輸出吞掉）。fake_get 換掉 Fetcher.get；now 固定「現在」。回結束碼。"""
    patches = [mock.patch.object(sys, "argv", ["fetch_data.py"] + argv)]
    if fake_get is not None:
        patches.append(mock.patch.object(fd.Fetcher, "get", fake_get))
    if now is not None:
        patches.append(mock.patch.object(fd, "now_tpe", lambda: now))
    with contextlib.ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        st.enter_context(contextlib.redirect_stdout(io.StringIO()))
        return fd.main()


# ============================================================ 1. 設定與校正係數

class TestConfig(unittest.TestCase):
    def test_estimate_assets_are_cloud_gold_est_without_a_yahoo_symbol(self):
        """估算兩項：owner cloud、type gold_est、估算的是存在的台銀黃金存摺；不給 yahooSymbol、type 不是 yahoo（不然會被納入週線長歷史與風險總表）。"""
        ids = dict((a["id"], a) for a in CFG["assets"])
        self.assertEqual(sorted(EST), ["gold_cny_est", "gold_twd_est"])
        for eid, e in EST.items():
            self.assertEqual((e["owner"], e["type"], e["group"], e["assetClass"]), ("cloud", "gold_est", "貴金屬", "gold_tw"), eid)
            self.assertNotIn("yahooSymbol", e)
            self.assertNotIn("cadence", e)
            self.assertEqual(ids[e["estimateOf"]]["type"], "bot_gold", eid)
            self.assertEqual(e["currency"], ids[e["estimateOf"]]["currency"])
            self.assertEqual(e["decimals"], ids[e["estimateOf"]]["decimals"])
            self.assertIn("國際換算估算", e["name"])
            self.assertEqual(e["priceLabel"], "國際換算估算")
        self.assertEqual([t["id"] for t in A.long_targets(list(EST.values())) if t["id"] in EST], [])   # 不進週線長歷史

    def test_calibration_recomputed_from_the_repo_equals_the_constant(self):
        """校正係數寫死在 assets.json；用倉庫的歷史檔重算要一樣（規格：附來源與日期、不在每輪重算）。對照組：改掉寫死的值 → 紅。"""
        for eid, e in EST.items():
            cfg = fd.estimate_config(e, GE)
            k, frm, to, n = fd.estimate_calibration(cfg, fd.load_history(e["estimateOf"]))
            self.assertAlmostEqual(k, cfg["calibration"], places=6, msg=eid)
            self.assertEqual((frm, to, n), (GE["calibrationFrom"], GE["calibrationTo"], GE["calibrationDays"]), eid)

    def test_grams_per_ounce_is_the_one_in_analyze(self):
        """裁決 8：沿用 analyze.py 的 31.1035，估算不另外寫一個。"""
        self.assertEqual(fd.grams_per_troy_ounce(), A.GRAMS_PER_TROY_OUNCE)
        src = inspect.getsource(fd)
        self.assertNotIn("31.1034768", src)
        self.assertNotIn("31.1035", src.replace("GRAMS_PER_TROY_OUNCE", ""))

    def test_date_sources_are_registered_with_grades(self):
        """沒登記的 dateSource 是 0 級、蓋不過任何舊點（錯得無聲）。"""
        for k in ("estimate", "estimate-intraday", "estimate-backfill"):
            self.assertIn(k, fd.DATE_SOURCE)
            self.assertIn(k, fd.DATE_SOURCE_GRADE)
        g = fd.DATE_SOURCE_GRADE
        self.assertGreater(g["estimate"], g["estimate-intraday"])
        self.assertGreater(g["estimate"], g["estimate-backfill"])


# ============================================================ 2. 同一時刻、日期、定案

class TestEstimateNow(unittest.TestCase):
    CFG = {"maxSkewMinutes": 60, "calibration": 1.01}

    def test_formula_and_same_moment(self):
        g = {"price": 2000.0, "at": epoch(2026, 11, 4, 10, 0)}
        x = {"price": 30.0, "at": epoch(2026, 11, 4, 10, 2)}
        price, pt, note = fd.gold_estimate_now(self.CFG, g, x, "light", 31.1035)
        self.assertAlmostEqual(price, 2000.0 / 31.1035 * 30.0 * 1.01, places=9)
        self.assertEqual((pt["d"], pt["dateSource"], pt.get("provisional")), ("2026-11-04", "estimate-intraday", True))
        self.assertEqual((pt["gAt"], pt["fxAt"]), ("10:00", "10:02"))
        self.assertIn("金價取 11-04 10:00、匯率取 11-04 10:02（台北時間）", note)

    def test_review_slot_writes_the_final_point(self):
        g = {"price": 2000.0, "at": epoch(2026, 11, 4, 15, 30)}
        _p, pt, _n = fd.gold_estimate_now(self.CFG, g, dict(g, price=30.0), "review", 31.1035)
        self.assertEqual(pt["dateSource"], "estimate")
        self.assertNotIn("provisional", pt)

    def test_skew_over_the_limit_is_not_computed(self):
        """對照組：拿掉時刻差檢查（用前一天的匯率配今天的金價）→ 紅。"""
        g = {"price": 2000.0, "at": epoch(2026, 11, 4, 10, 0)}
        yesterday_fx = {"price": 30.0, "at": epoch(2026, 11, 3, 10, 0)}
        with self.assertRaises(fd.EstimateSkew):
            fd.gold_estimate_now(self.CFG, g, yesterday_fx, "light", 31.1035)
        with self.assertRaises(fd.EstimateSkew):
            fd.gold_estimate_now(self.CFG, g, {"price": 30.0, "at": g["at"] - 61 * 60}, "light", 31.1035)
        fd.gold_estimate_now(self.CFG, g, {"price": 30.0, "at": g["at"] - 60 * 60}, "light", 31.1035)   # 剛好 60 分鐘：還算

    def test_the_date_comes_from_the_quotes_not_the_clock(self):
        """點的日期是報價時刻的台北日期（2026-11-04），不是跑程式的那一天。對照組：用執行時間當日期 → 紅。"""
        g = {"price": 2000.0, "at": epoch(2026, 11, 4, 23, 50)}
        x = {"price": 30.0, "at": epoch(2026, 11, 4, 23, 55)}
        _p, pt, _n = fd.gold_estimate_now(self.CFG, g, x, "light", 31.1035)
        self.assertEqual(pt["d"], "2026-11-04")
        self.assertNotEqual(pt["d"], fd.now_tpe().strftime("%Y-%m-%d"))

    def test_different_days_or_weekend_write_no_point(self):
        g = {"price": 2000.0, "at": epoch(2026, 11, 4, 23, 50)}
        x = {"price": 30.0, "at": epoch(2026, 11, 5, 0, 10)}
        price, pt, note = fd.gold_estimate_now(self.CFG, g, x, "light", 31.1035)
        self.assertIsNone(pt)
        self.assertIn("不寫點", note)
        sat = {"price": 2000.0, "at": epoch(2026, 11, 7, 6, 0)}
        price, pt, note = fd.gold_estimate_now(self.CFG, sat, dict(sat, price=30.0), "review", 31.1035)
        self.assertIsNone(pt)
        self.assertIn("週末", note)
        self.assertGreater(price, 0)                                 # 現價照樣更新


# ============================================================ 3. handler：回補、只寫自己的檔、沿用

class TestHandler(unittest.TestCase):
    def ctx(self, slot="light", prev=None):
        return {"slot": slot, "goldEstimate": GE, "prevAssets": prev or {}, "light": slot == "light"}

    def quotes(self, at, gc=2000.0):
        return {"GC=F": (gc, at), "TWD=X": (30.0, at), "CNY=X": (7.0, at)}

    def test_first_run_backfills_from_the_repo_files_and_marks_every_point(self):
        with Sandbox():
            f = FakeYahoo(self.quotes(epoch(2026, 11, 4, 15, 30)))
            pts, q = fd.handle_gold_est(f, EST["gold_twd_est"], self.ctx("review"))
            back = [p for p in pts if p["dateSource"] == "estimate-backfill"]
            self.assertGreater(len(back), 200)
            self.assertGreaterEqual(back[0]["d"], GE["backfillFrom"])
            self.assertEqual(back[-1]["d"], "2026-11-03")
            for p in back:
                self.assertIn("g", p)
                self.assertIn("fx", p)
            self.assertEqual((pts[-1]["d"], pts[-1]["dateSource"]), ("2026-11-04", "estimate"))
            k = EST["gold_twd_est"]["estimate"]["calibration"]
            self.assertAlmostEqual(q["price"], 2000.0 / A.GRAMS_PER_TROY_OUNCE * 30.0 * k, places=4)
            self.assertEqual(q["estimateOf"], "gold_twd")
            self.assertEqual(q["sourceLabel"], "國際換算估算（不是台銀牌告）")
            self.assertEqual((q["switchAfterTradingDays"], q["typicalErrorPct"]), (GE["switchAfterTradingDays"], GE["typicalErrorPct"]))
            self.assertEqual(q["estimate"]["calibration"], k)
            self.assertEqual(q["backfillThrough"], "2026-11-03")
            self.assertIn("回補", q["historyNote"])
            self.assertEqual(len(f.urls), 2)                         # GC=F 與 TWD=X 各一次

    def test_both_estimates_share_one_gold_quote(self):
        with Sandbox():
            f, ctx = FakeYahoo(self.quotes(epoch(2026, 11, 4, 10, 0))), self.ctx()
            fd.handle_gold_est(f, EST["gold_twd_est"], ctx)
            fd.handle_gold_est(f, EST["gold_cny_est"], ctx)
            self.assertEqual(len(f.urls), 3)                         # GC=F 只問一次＝兩項同一時刻的金價
            self.assertEqual(sum(1 for u in f.urls if "GC%3DF" in u or "GC=F" in u), 1)

    def test_skew_carries_the_previous_estimate_or_fails_if_there_is_none(self):
        with Sandbox():
            at = epoch(2026, 11, 4, 10, 0)
            q = {"GC=F": (2000.0, at), "TWD=X": (30.0, at - 3 * 3600), "CNY=X": (7.0, at)}
            with self.assertRaises(fd.SkipAsset):
                fd.handle_gold_est(FakeYahoo(q), EST["gold_twd_est"], self.ctx(prev={"gold_twd_est": {"status": "ok", "price": 1.0}}))
            with self.assertRaises(fd.FetchError):
                fd.handle_gold_est(FakeYahoo(q), EST["gold_twd_est"], self.ctx())

    def test_final_point_beats_the_intraday_one_and_not_the_other_way(self):
        old = [{"d": "2026-11-04", "c": 1.0, "dateSource": "estimate-backfill"}]
        fin = fd.merge_points(old, [{"d": "2026-11-04", "c": 2.0, "dateSource": "estimate"}])
        self.assertEqual(fin[-1]["c"], 2.0)
        still = fd.merge_points(fin, [{"d": "2026-11-04", "c": 3.0, "dateSource": "estimate-intraday", "provisional": True}])
        self.assertEqual((still[-1]["c"], still[-1]["dateSource"]), (2.0, "estimate"))

    def test_estimate_points_can_never_be_saved_into_the_bot_files(self):
        """鐵則：估算不冒充台銀牌告。對照組：拿掉 save_history 的檢查 → 紅。"""
        with Sandbox() as sb:
            before = sb.raw("gold_twd")
            bot = dict((a["id"], a) for a in CFG["assets"])["gold_twd"]
            with self.assertRaises(fd.FetchError):
                fd.save_history(bot, [{"d": "2026-11-04", "c": 1.0, "dateSource": "estimate"}])
            with self.assertRaises(fd.FetchError):
                fd.save_history(bot, [{"d": "2026-11-04", "c": 1.0, "dateSource": "estimate-backfill"}])
            self.assertEqual(sb.raw("gold_twd"), before)
            fd.save_history(EST["gold_twd_est"], [{"d": "2026-11-04", "c": 1.0, "dateSource": "estimate"}])


class TestFailedEstimatesStayAttached(unittest.TestCase):
    """Codex 第 1 輪：估算失敗那一輪、雲端還沒抓過的那幾輪，紀錄也要帶 estimateOf——不然前端、分析頁、報告會把它當成獨立的標的列出來。"""

    def test_error_quote_keeps_estimate_of(self):
        """對照組：error_quote 不帶 estimateOf → 紅。"""
        q = fd.error_quote(EST["gold_twd_est"], "Yahoo 沒有回應", {})
        self.assertEqual((q["status"], q["estimateOf"]), ("error", "gold_twd"))
        bot = dict((a["id"], a) for a in CFG["assets"])["gold_twd"]
        self.assertNotIn("estimateOf", fd.error_quote(bot, "x", {}))

    def test_merge_stub_keeps_estimate_of(self):
        """對照組：merge_latest 的替代紀錄不帶 estimateOf → 紅。"""
        import merge_latest as ML
        q = ML.stub_quote(EST["gold_cny_est"], "雲端的資料檔裡沒有這一項", datetime.datetime(2026, 11, 9, 9, 0, tzinfo=TPE))
        self.assertEqual((q["status"], q["estimateOf"]), ("error", "gold_cny"))

    def test_a_cloud_run_where_yahoo_fails_still_marks_the_estimate(self):
        with Sandbox() as sb:
            def failing_get(self, url, **kw):
                raise fd.FetchError("Yahoo 回應 429（請求過於頻繁）")
            rc = run_main(["--source", "cloud", "--slot", "light", "--light", "--only", "gold_twd_est,gold_cny_est"], failing_get)
            self.assertEqual(rc, 2)                                                  # 有標的失敗：照實回 2
            for eid, gid in (("gold_twd_est", "gold_twd"), ("gold_cny_est", "gold_cny")):
                e = sb.shard("cloud")["assets"][eid]
                self.assertEqual((e["status"], e["estimateOf"]), ("error", gid))
                self.assertIn("429", e["error"])


class TestCloudRunWhileTheLaptopIsOff(unittest.TestCase):
    """規格第 5 節的「筆電關機 24 小時」（模擬）：只有雲端在跑。估算每一輪在動、台銀三個檔沒動、一個台銀請求都沒有。"""

    def test_cloud_runs_move_the_estimate_and_never_touch_the_bot_files(self):
        with Sandbox() as sb:
            bot_before = dict((aid, sb.raw(aid)) for aid in ("gold_twd", "gold_cny", "gold_bar"))
            seen = []

            def fake_get_for(quotes):
                def fake_get(self, url, **kw):
                    seen.append(url)
                    return FakeYahoo(quotes).get(url)
                return fake_get
            only = ["--source", "cloud", "--only", "gold_twd_est,gold_cny_est"]
            t1 = epoch(2026, 11, 4, 15, 30)
            rc = run_main(only + ["--slot", "review"], fake_get_for({"GC=F": (2000.0, t1), "TWD=X": (30.0, t1), "CNY=X": (7.0, t1)}))
            self.assertEqual(rc, 0)
            first = sb.shard("cloud")["assets"]["gold_twd_est"]
            t2 = epoch(2026, 11, 5, 9, 10)
            run_main(only + ["--slot", "light", "--light"], fake_get_for({"GC=F": (2010.0, t2), "TWD=X": (30.0, t2), "CNY=X": (7.0, t2)}))
            second = sb.shard("cloud")["assets"]["gold_twd_est"]
            self.assertGreater(second["price"], first["price"])                     # 估算在動
            self.assertEqual(second["estimateOf"], "gold_twd")
            est_pts = json.loads(sb.raw("gold_twd_est").decode("utf-8"))["points"]
            self.assertEqual(est_pts[-2]["d"], "2026-11-04")
            self.assertEqual(est_pts[-2]["dateSource"], "estimate")
            self.assertEqual((est_pts[-1]["d"], est_pts[-1]["dateSource"]), ("2026-11-05", "estimate-intraday"))
            for aid, raw in bot_before.items():
                self.assertEqual(sb.raw(aid), raw, "%s 的歷史檔被動到了" % aid)   # 台銀三個檔一個 byte 都不動
            self.assertEqual([u for u in seen if "bot.com.tw" in u], [])
            self.assertTrue(all("query1.finance.yahoo.com" in u for u in seen))


# ============================================================ 4. 停止硬打

class TestBackoffRules(unittest.TestCase):
    MON_0859 = datetime.datetime(2026, 11, 9, 8, 59, tzinfo=TPE)
    MON_0900 = datetime.datetime(2026, 11, 9, 9, 0, tzinfo=TPE)
    SAT = datetime.datetime(2026, 11, 14, 10, 0, tzinfo=TPE)

    def test_below_the_threshold_always_tries(self):
        self.assertEqual(fd.bot_backoff_decision({}, self.SAT), (True, None))
        self.assertEqual(fd.bot_backoff_decision({"blockedRuns": fd.BOT_BACKOFF_AFTER - 1}, self.MON_0859), (True, None))

    def test_after_three_blocks_only_once_per_business_day_after_nine(self):
        """對照組：拿掉降頻（被擋之後仍每輪打）→ 紅。"""
        st = {"blockedRuns": fd.BOT_BACKOFF_AFTER}
        ok, why = fd.bot_backoff_decision(st, self.MON_0859)
        self.assertFalse(ok)
        self.assertIn("09:00 以後再試一次", why)
        self.assertIn("這一輪沒有連台銀", why)
        self.assertEqual(fd.bot_backoff_decision(st, self.MON_0900), (True, None))
        ok, why = fd.bot_backoff_decision(dict(st, lastTryDate="2026-11-09"), self.MON_0900 + datetime.timedelta(hours=3))
        self.assertFalse(ok)
        self.assertIn("今天已經試過一次", why)

    def test_weekends_are_skipped_in_backoff(self):
        ok, why = fd.bot_backoff_decision({"blockedRuns": 9}, self.SAT)
        self.assertFalse(ok)
        self.assertIn("週六、週日不試", why)

    def test_after_success_resets_and_other_errors_do_not_count(self):
        """對照組：成功不歸零 → 紅。"""
        st = {"blockedRuns": 5, "since": "x", "lastTryDate": "2026-11-06"}
        self.assertEqual(fd.bot_backoff_after(st, self.MON_0900, True, False, True),
                         {"blockedRuns": 0, "lastTryAt": fd.iso(self.MON_0900), "lastTryDate": "2026-11-09"})
        other = fd.bot_backoff_after(st, self.MON_0900, True, False, False)
        self.assertEqual(other["blockedRuns"], 5)
        self.assertEqual(other["lastTryDate"], "2026-11-09")
        self.assertEqual(fd.bot_backoff_after(st, self.MON_0900, False, False, False), st)       # 沒試：原樣
        self.assertEqual(fd.bot_backoff_after({}, self.MON_0900, True, True, False)["blockedRuns"], 1)

    def test_the_numbers_live_in_one_place(self):
        self.assertEqual((fd.BOT_BACKOFF_AFTER, fd.BOT_BACKOFF_RETRY_AT), (3, "09:00"))
        self.assertEqual(fd.BOT_HOST, "rate.bot.com.tw")


class TestLocalRunsBackOff(unittest.TestCase):
    """本機連續幾輪被台銀的驗證頁擋下：第 4 輪起不再連台銀，平日 09:00 以後每天試一次，週末不試（排程照舊叫這支程式）。"""

    def test_sequence_of_local_runs(self):
        with Sandbox() as sb:
            with open(os.path.join(sb.src, "local.json"), "w", encoding="utf-8") as fh:
                json.dump({"source": "local", "assets": dict(
                    (aid, {"id": aid, "status": "ok", "price": 3000.0, "date": "2026-10-28", "fetchedAt": "2026-10-28T17:00:00+08:00",
                           "quoteTime": "2026/10/28 16:50", "type": t})
                    for aid, t in (("gold_twd", "bot_gold"), ("gold_cny", "bot_gold"), ("gold_bar", "bot_gold_bar")))}, fh)
            hits = []

            def blocking_get(self, url, **kw):
                host = url.split("/")[2]
                if host in self.blocked:
                    raise fd.FetchError(self.blocked[host])
                hits.append(url)
                self.count += 1
                self.blocked[host] = "台銀要求瀏覽器驗證（回傳驗證頁），程式過不了，本輪不再重試。"
                raise fd.FetchError(self.blocked[host])

            def at(day, h, m):
                return datetime.datetime(2026, 11, day, h, m, tzinfo=TPE)
            local = ["--source", "local", "--slot", "light"]
            for n, when in enumerate((at(9, 9, 0), at(9, 9, 30), at(9, 10, 0)), start=1):
                before = len(hits)
                run_main(local, blocking_get, when)
                self.assertEqual(len(hits) - before, 1, "第 %d 輪應該試一次" % n)
                self.assertEqual(sb.shard("local")["botBackoff"]["blockedRuns"], n)
                lg = sb.shard("local")["assets"]["gold_twd"]["lastGood"]
                self.assertEqual((lg["price"], lg["date"], lg["quoteTime"]), (3000.0, "2026-10-28", "2026/10/28 16:50"))   # 卡片寫「沿用」用
            for when in (at(9, 10, 30), at(10, 8, 30)):                         # 同一天已經試過；隔天 09:00 以前
                before = len(hits)
                run_main(local, blocking_get, when)
                self.assertEqual(len(hits), before, "降頻中：%s 這一輪不該連台銀" % when)
                a = sb.shard("local")["assets"]["gold_twd"]
                self.assertTrue(a.get("carriedOver"))
                self.assertIn("這一輪沒有連台銀", a.get("carriedReason") or "")
                self.assertIn("lastGood", a)                                    # 上次成功的牌價還在（卡片寫「沿用」）
            before = len(hits)
            run_main(local, blocking_get, at(10, 9, 0))                           # 隔天 09:00：試一次
            self.assertEqual(len(hits) - before, 1)
            self.assertEqual(sb.shard("local")["botBackoff"]["blockedRuns"], 4)
            for when in (at(10, 9, 30), at(14, 10, 0), at(15, 10, 5)):           # 同一天第二輪、週六、週日
                before = len(hits)
                run_main(local, blocking_get, when)
                self.assertEqual(len(hits), before, when)
            self.assertEqual(sb.shard("local")["botBackoff"]["lastTryDate"], "2026-11-10")


class TestBlockMessage(unittest.TestCase):
    def test_message_does_not_blame_the_cloud(self):
        """錯誤訊息不分雲端或筆電（規格 3.5）。"""
        class Resp(object):
            status_code = 200
            content = b"<title>Challenge Validation</title>"
        f = fd.Fetcher(verbose=False)
        f.session.get = lambda url, headers=None, timeout=None: Resp()
        with mock.patch.object(fd.time, "sleep", lambda s: None):
            with self.assertRaises(fd.FetchError) as cm:
                f.get("https://rate.bot.com.tw/gold?Lang=zh-TW")
        msg = str(cm.exception)
        self.assertIn("台銀要求瀏覽器驗證", msg)
        self.assertIn("程式過不了", msg)
        self.assertNotIn("雲端主機", msg)
        self.assertIn(fd.BOT_HOST, f.blocked)
        self.assertNotIn("雲端主機的 IP 常被這樣阻擋", inspect.getsource(fd))


# ============================================================ 5. 估算不自成一列；趨勢照實寫算在哪條序列

class TestTrendAndReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-g8b-trend-")
        hist = os.path.join(self.tmp, "history")
        os.makedirs(hist)
        days = weekdays(datetime.date(2025, 9, 1), datetime.date(2026, 11, 9))
        write_hist(hist, "gold_twd", [{"d": d, "c": 3000.0 + i, "dateSource": "chart"} for i, d in enumerate(days) if d <= "2026-10-28"])
        write_hist(hist, "gold_twd_est", [{"d": d, "c": 2000.0 + i, "dateSource": "estimate-backfill"} for i, d in enumerate(days)])
        assets = os.path.join(self.tmp, "assets.json")
        with open(assets, "w", encoding="utf-8") as fh:
            json.dump({"goldEstimate": {"switchAfterTradingDays": 1}, "assets": []}, fh)
        self.paths = {"history": hist, "assets": assets}
        self.assets = [{"id": "gold_twd", "type": "bot_gold"}, {"id": "gold_twd_est", "type": "gold_est", "estimateOf": "gold_twd"}]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_trend_switches_to_the_estimate_and_says_so(self):
        """對照組：拿掉「算在估算序列上」的標記、或替估算自己也算一筆 → 紅。"""
        out = A.build_trend(self.assets, self.paths, [], now=datetime.datetime(2026, 11, 9, 16, 0, tzinfo=TPE))
        self.assertNotIn("gold_twd_est", out)
        t = out["gold_twd"]
        self.assertEqual((t["series"], t["seriesId"], t["label"], t["botThrough"]), ("estimate", "gold_twd_est", "國際換算估算", "2026-10-28"))
        self.assertIn("2026-10-28 之後沒有新牌告", t["seriesNote"])
        self.assertIn("不是台銀牌告", t["seriesNote"])
        self.assertEqual(t["through"], "2026-11-09")

    def test_an_unreadable_estimate_file_is_an_error_not_a_silent_fallback(self):
        """Codex 第 1 輪：估算的檔在、卻讀不懂＝這一筆寫 error、記進 problems，不悄悄退回台銀的舊序列。對照組：吞掉讀檔錯誤 → 紅。"""
        with open(os.path.join(self.paths["history"], "gold_twd_est.json"), "w", encoding="utf-8") as fh:
            fh.write("{ 這不是 JSON")
        problems = []
        out = A.build_trend(self.assets, self.paths, problems, now=datetime.datetime(2026, 11, 9, 16, 0, tzinfo=TPE))
        t = out["gold_twd"]
        self.assertTrue(t.get("error"))
        self.assertIn("gold_twd_est.json", t["error"])
        self.assertEqual((t["series"], t["ma200"], t["days"]), ("estimate", None, None))
        self.assertEqual(len(problems), 1)
        self.assertIn("gold_twd", problems[0])

    def test_no_estimate_yet_falls_back_to_the_bot_series_with_its_own_date(self):
        os.remove(os.path.join(self.paths["history"], "gold_twd_est.json"))
        problems = []
        out = A.build_trend(self.assets, self.paths, problems, now=datetime.datetime(2026, 11, 9, 16, 0, tzinfo=TPE))
        self.assertNotIn("series", out["gold_twd"])
        self.assertEqual(out["gold_twd"]["through"], "2026-10-28")                  # 日期照實寫台銀的最後一天
        self.assertEqual(problems, [])

    def test_within_the_gate_the_trend_stays_on_the_bot_series(self):
        out = A.build_trend(self.assets, self.paths, [], now=datetime.datetime(2026, 10, 29, 16, 0, tzinfo=TPE))
        self.assertNotIn("series", out["gold_twd"])
        self.assertEqual(out["gold_twd"]["through"], "2026-10-28")

    def test_report_leaves_the_estimates_out(self):
        """對照組：generate 不濾掉估算 → 報告多兩列（失敗警告、觀察、燈號表、排行、快照）→ 紅。"""
        latest = {"assets": {"gold_twd": {"id": "gold_twd"}, "gold_twd_est": {"id": "gold_twd_est", "estimateOf": "gold_twd"}}}
        self.assertEqual(sorted(R.without_estimates(latest)["assets"]), ["gold_twd"])
        self.assertEqual(sorted(latest["assets"]), ["gold_twd", "gold_twd_est"])                 # 不改到原本那一份
        src = inspect.getsource(R.generate)
        self.assertIn("latest = without_estimates(", src)

    def test_trading_days_rule_is_the_same_as_the_front_end(self):
        """前後端同一組日期表：讀 scripts/test_freshness.html 裡那一段，餵 analyze.trading_days_behind。"""
        with open(os.path.join(HERE, "test_freshness.html"), encoding="utf-8") as fh:
            page = fh.read()
        block = page.split("/* TRADING-DAY-VECTORS-BEGIN */", 1)[1].split("/* TRADING-DAY-VECTORS-END */", 1)[0]
        vectors = json.loads(block[block.index("["):block.rindex("]") + 1])
        self.assertGreaterEqual(len(vectors), 8)
        for last, now, want in vectors:
            today = datetime.datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(TPE).date()
            self.assertEqual(A.trading_days_behind(last, today), want, (last, now))


# ============================================================ 6. 常數只有一份（文件、頁尾跟 assets.json 一樣）

class TestOneSourceOfTruth(unittest.TestCase):
    def read(self, rel):
        with open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8") as fh:
            return fh.read()

    def test_footer_sentence_and_its_number(self):
        page = self.read("index.html")
        pct = ("%g" % GE["typicalErrorPct"])
        self.assertIn("估算＝國際金價換算，再用台銀被擋前的本行賣出價校正，所以對應的是本行賣出價，不代表本行買入價；"
                      "平常與台銀差約 %s%%，金價大波動時可能差好幾 %%；不是台銀牌告，實際交易以台銀為準。" % pct, page)

    def test_docs_quote_the_config(self):
        doc = self.read("docs/ANALYSIS.md")
        for k in ("calibrationFrom", "calibrationTo"):
            self.assertIn(GE[k], doc, k)
        for eid, e in EST.items():
            self.assertIn("%.6f" % e["estimate"]["calibration"], doc, eid)
            self.assertIn(e["estimate"]["fxSymbol"], doc, eid)
        self.assertIn("%d 分鐘" % GE["maxSkewMinutes"], doc)
        self.assertIn("%d 個交易日" % GE["switchAfterTradingDays"], doc)
        self.assertIn("約 %g%%" % GE["typicalErrorPct"], doc)
        self.assertIn("%d 輪" % fd.BOT_BACKOFF_AFTER, doc)
        self.assertIn(fd.BOT_BACKOFF_RETRY_AT, doc)
        self.assertIn(str(A.GRAMS_PER_TROY_OUNCE), doc)
        self.assertIn("estimate-backfill", doc)

    def test_the_front_end_reads_the_copied_numbers_not_its_own(self):
        app = self.read("js/app.js")
        fr = self.read("js/freshness.js")
        self.assertIn("est.switchAfterTradingDays", fr)
        self.assertIn("typicalErrorPct", app)
        self.assertNotIn("0.6", re.sub(r"//.*|/\*[\s\S]*?\*/", "", app.split("function estimatePlainHtml", 1)[1].split("function ", 1)[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
