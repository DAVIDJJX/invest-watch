#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_slots.py — 停點 7-1：時段「被告知」而不是「自己猜」、暫定點、結束碼

跑法：
    python -m unittest discover -s scripts -p "test_*.py" -v

這一組全部離線：需要外部資料的地方都用假的回應替換掉；
呼叫 fetch_data.py 當子程序的測試，都是在它連網之前就該被擋下來的情況。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import fetch_data as fd                                      # noqa: E402
import merge_latest as ml                                    # noqa: E402
import dispatch_args as da                                   # noqa: E402

TPE = timezone(timedelta(hours=8))


def run_script(*args):
    """跑 scripts/ 底下的腳本當子程序，回傳 (結束碼, stdout+stderr)。"""
    p = subprocess.run([sys.executable] + list(args), cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    return p.returncode, p.stdout.decode("utf-8", "replace")


# ==========================================================================
# 1. 時段不猜：--slot 必填、本機只能 light
# ==========================================================================

class TestSlotIsToldNotGuessed(unittest.TestCase):

    def test_time_based_guessing_is_gone(self):
        """整支程式不可以再有「用時間猜時段」的函式。

        對照組：把猜時段的函式加回去、--slot 改回選填，這一條和下一條會紅。
        """
        self.assertFalse(hasattr(fd, "guess_slot"))
        with open(os.path.join(HERE, "fetch_data.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("def guess_slot", src)

    def test_slot_is_required(self):
        rc, out = run_script("scripts/fetch_data.py", "--source", "cloud")
        self.assertNotEqual(rc, 0)
        self.assertIn("--slot", out)
        self.assertIn("required", out)

    def test_local_source_only_accepts_light(self):
        """本機不產報告，沒有資格宣告時段 → 非 light 一律結束碼 2、什麼都不跑。

        對照組：拿掉 fetch_data.py 裡那個 if source == "local" 的檢查，這一條會紅。
        """
        for bad in ("morning", "midmorning", "close", "review", "manual"):
            rc, out = run_script("scripts/fetch_data.py",
                                 "--source", "local", "--slot", bad)
            self.assertEqual(rc, 2, "slot=%s 應該被擋，實際 rc=%d\n%s" % (bad, rc, out))
            self.assertIn("只接受 --slot light", out)
            self.assertNotIn("已寫出分片", out, "被擋的那一輪不可以寫任何檔")

    def test_all_slot_names_are_the_new_ones(self):
        self.assertEqual(fd.ALL_SLOTS, ("light", "morning", "midmorning", "close",
                                         "review", "manual"))
        self.assertNotIn("midday", fd.SLOT_LABEL, "新的時段名裡不該再有 midday")
        for s in fd.ALL_SLOTS:
            self.assertIn(s, fd.SLOT_LABEL)


class TestDispatchArgs(unittest.TestCase):
    """workflow 第一步：合法組合只有 slot=light⇔mode=light、其餘⇔mode=full。"""

    def test_valid_combinations(self):
        self.assertEqual(da.resolve("workflow_dispatch", "light", "light"), ("light", "light"))
        for s in ("morning", "midmorning", "close", "review", "manual"):
            self.assertEqual(da.resolve("workflow_dispatch", "full", s), ("full", s))

    def test_schedule_is_always_light_and_never_reports(self):
        """備援 cron 從來不準時，所以永遠只做 light、不產報告——不管它想傳什麼。"""
        self.assertEqual(da.resolve("schedule"), ("light", "light"))
        self.assertEqual(da.resolve("schedule", "full", "morning"), ("light", "light"))

    def test_invalid_combinations_are_rejected(self):
        bad = [("workflow_dispatch", "light", "morning"),
               ("workflow_dispatch", "full", "light"),
               ("workflow_dispatch", "full", "midday"),
               ("workflow_dispatch", "", "morning"),
               ("workflow_dispatch", "full", ""),
               ("push", "light", "light")]
        for ev, m, s in bad:
            with self.assertRaises(ValueError, msg="%s/%s/%s 應該被拒絕" % (ev, m, s)):
                da.resolve(ev, m, s)

    def test_cli_exit_code_is_2_on_invalid(self):
        rc, out = run_script("scripts/dispatch_args.py",
                             "--event", "workflow_dispatch", "--mode", "light", "--slot", "morning")
        self.assertEqual(rc, 2)
        self.assertIn("::error::", out)
        rc, out = run_script("scripts/dispatch_args.py",
                             "--event", "workflow_dispatch", "--mode", "full", "--slot", "review")
        self.assertEqual(rc, 0)
        self.assertIn("mode=full", out)
        self.assertIn("slot=review", out)


# ==========================================================================
# 2. 暫定點（provisional）與同一天的等級規則
# ==========================================================================

class TestMergePointsGrades(unittest.TestCase):
    """同一天：低等級不能蓋高等級；定案的點進來要拿掉 provisional。

    對照組：把 merge_points 裡的等級檢查拿掉，第一條會紅。
    """

    def test_intraday_cannot_overwrite_official(self):
        old = [{"d": "2026-09-09", "c": 100.0, "dateSource": "official"}]
        new = [{"d": "2026-09-09", "c": 101.0, "dateSource": "intraday", "provisional": True}]
        got = fd.merge_points(old, new)
        self.assertEqual(got[0]["c"], 100.0)
        self.assertEqual(got[0]["dateSource"], "official")
        self.assertNotIn("provisional", got[0])

    def test_official_overwrites_intraday_and_clears_provisional(self):
        old = [{"d": "2026-09-09", "c": 101.0, "dateSource": "intraday", "provisional": True}]
        new = [{"d": "2026-09-09", "c": 100.0, "dateSource": "official"}]
        got = fd.merge_points(old, new)
        self.assertEqual(got[0]["c"], 100.0)
        self.assertEqual(got[0]["dateSource"], "official")
        self.assertNotIn("provisional", got[0], "定案之後不可以還掛著暫定標記")

    def test_close_realtime_then_official(self):
        old = [{"d": "2026-09-09", "c": 100.5, "dateSource": "close-realtime"}]
        new = [{"d": "2026-09-09", "c": 100.0, "dateSource": "official"}]
        self.assertEqual(fd.merge_points(old, new)[0]["dateSource"], "official")
        # 反過來：官方已定案，收盤後即時價不能再蓋回去
        self.assertEqual(fd.merge_points(new, old)[0]["dateSource"], "official")

    def test_same_grade_newer_wins(self):
        old = [{"d": "2026-09-09", "c": 1.0, "dateSource": "intraday", "provisional": True}]
        new = [{"d": "2026-09-09", "c": 2.0, "dateSource": "intraday", "provisional": True}]
        got = fd.merge_points(old, new)
        self.assertEqual(got[0]["c"], 2.0)
        self.assertTrue(got[0]["provisional"])

    def test_untagged_legacy_points_can_be_upgraded(self):
        old = [{"d": "2026-09-09", "c": 1.0}]
        new = [{"d": "2026-09-09", "c": 2.0, "dateSource": "official"}]
        self.assertEqual(fd.merge_points(old, new)[0]["c"], 2.0)


class TestYahooDatesAndProvisional(unittest.TestCase):
    """Yahoo 的 K 棒：日期用交易所當地日期；只有進行中的那根才 provisional。

    對照組：把 parse_yahoo_chart 的 gmtoffset 換回 UTC，test_exchange_local_date 會紅。
    """

    @staticmethod
    def chart(ts, closes, gmtoffset, price, session_end):
        return {"chart": {"result": [{
            "meta": {"gmtoffset": gmtoffset, "regularMarketPrice": price,
                     "currency": "USD",
                     "currentTradingPeriod": {"regular": {"end": session_end}}},
            "timestamp": ts,
            "indicators": {"quote": [{"close": closes}]},
        }], "error": None}}

    def test_exchange_local_date_not_utc(self):
        """時間戳要挑「UTC 日期 ≠ 交易所當地日期」的，測試才分辨得出來。

        2026-09-09 02:00 UTC = 美東（EDT, UTC-4）2026-09-08 22:00。
        正確答案是 09-08（交易所當地）；錯用 UTC 會得到 09-09；錯用台北會得到 09-09 10:00 → 09-09。
        """
        t = int(datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc).timestamp())
        j = self.chart([t], [100.0], -4 * 3600, 100.0, t + 3600)
        now = t + 7200                      # 交易所已收盤
        _, _, pts = fd.parse_yahoo_chart(j, now_epoch=now)
        self.assertEqual(pts[-1]["d"], "2026-09-08", "要用交易所當地日期，不是 UTC 或台北")
        self.assertEqual(pts[-1]["dateSource"], "yahoo")
        self.assertNotIn("provisional", pts[-1])

    def test_taiwan_listed_uses_taipei_date(self):
        """上櫃 00679B 走 Yahoo（gmtoffset +8）：2026-09-08 20:00 UTC = 台北 09-09 04:00。"""
        t = int(datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc).timestamp())
        j = self.chart([t], [25.0], 8 * 3600, 25.0, t + 3600)
        _, _, pts = fd.parse_yahoo_chart(j, now_epoch=t + 7200)
        self.assertEqual(pts[-1]["d"], "2026-09-09")

    def test_in_progress_bar_is_provisional(self):
        t = int(datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc).timestamp())   # 09:30 ET 開盤
        end = int(datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc).timestamp())  # 16:00 ET 收盤
        j = self.chart([t], [None], -4 * 3600, 101.5, end)
        _, _, pts = fd.parse_yahoo_chart(j, now_epoch=t + 3600)                   # 10:30 ET，盤中
        self.assertEqual(pts[-1]["d"], "2026-09-08")
        self.assertTrue(pts[-1].get("provisional"))
        self.assertEqual(pts[-1]["dateSource"], "intraday")
        self.assertEqual(pts[-1]["c"], 101.5)

    def test_bar_is_final_after_session_end(self):
        t = int(datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc).timestamp())
        j = self.chart([t], [102.0], -4 * 3600, 102.0, end)
        _, _, pts = fd.parse_yahoo_chart(j, now_epoch=end + 60)                   # 收盤後一分鐘
        self.assertNotIn("provisional", pts[-1])
        self.assertEqual(pts[-1]["dateSource"], "yahoo")

    def test_btc_today_bar_stays_provisional_until_utc_midnight(self):
        """比特幣：Yahoo 把它的一天定成 UTC 00:00~23:59，台北 08:00 才換日。"""
        day0 = int(datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc).timestamp())
        end = day0 + 86399
        j = self.chart([day0], [None], 0, 79000.0, end)
        _, _, pts = fd.parse_yahoo_chart(j, now_epoch=day0 + 9 * 3600)   # UTC 09:00 = 台北 17:00
        self.assertTrue(pts[-1].get("provisional"))
        _, _, pts2 = fd.parse_yahoo_chart(j, now_epoch=day0 + 86400 + 60)   # 隔天 UTC 00:01
        self.assertNotIn("provisional", pts2[-1])

    def test_yesterday_bar_seen_in_taipei_daytime_is_final(self):
        """台北白天抓到的 NVDA 是前一個美股交易日的完成 bar，不是 provisional。"""
        t = int(datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc).timestamp())    # 09-08 美股
        end_next = int(datetime(2026, 9, 9, 20, 0, tzinfo=timezone.utc).timestamp())
        j = self.chart([t], [100.0], -4 * 3600, 100.0, end_next)
        now = int(datetime(2026, 9, 9, 7, 30, tzinfo=timezone.utc).timestamp())    # 台北 09-09 15:30
        _, _, pts = fd.parse_yahoo_chart(j, now_epoch=now)
        self.assertEqual(pts[-1]["d"], "2026-09-08")
        self.assertNotIn("provisional", pts[-1])


class TestTwseIntradayPoint(unittest.TestCase):
    """證交所即時價：13:30 前是盤中暫定；13:30 後是收盤價（PLAN 7.4）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-twse-")
        self._orig = fd.HIST_DIR
        fd.HIST_DIR = self.tmp
        # 60 個舊點，讓 light 模式不去抓 Yahoo 回補、也不抓月檔
        base = datetime(2026, 6, 1)
        pts = [{"d": (base + timedelta(days=i)).strftime("%Y-%m-%d"), "c": 100.0 + i,
                "dateSource": "official"} for i in range(60)]
        with open(os.path.join(self.tmp, "tw2330.json"), "w", encoding="utf-8") as fh:
            json.dump({"points": pts}, fh)

    def tearDown(self):
        fd.HIST_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_twse(self, time_str):
        asset = {"id": "tw2330", "name": "台積電 2330", "symbol": "2330",
                 "type": "twse_stock", "currency": "TWD", "unit": "元",
                 "decimals": 0, "priceLabel": "成交價"}
        ctx = {"light": True,
               "twseRealtime": {"2330": {"price": 2410.0, "date": "2026-09-09",
                                         "time": time_str, "open": 2400.0,
                                         "high": 2420.0, "low": 2395.0}}}
        merged, quote = fd.handle_twse(None, asset, ctx)
        return [p for p in merged if p["d"] == "2026-09-09"][0]

    def test_before_close_is_provisional(self):
        p = self.run_twse("10:15:00")
        self.assertEqual(p["dateSource"], "intraday")
        self.assertTrue(p.get("provisional"))

    def test_after_close_is_close_realtime(self):
        p = self.run_twse("13:30:00")
        self.assertEqual(p["dateSource"], "close-realtime")
        self.assertNotIn("provisional", p)


# ==========================================================================
# 3. latest.json 的時段只聽雲端；暫定點留過夜要警告
# ==========================================================================

class MergeSandbox(unittest.TestCase):
    """把 merge_latest 讀的路徑全部換成暫存目錄。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-merge-")
        os.makedirs(os.path.join(self.tmp, "sources"))
        os.makedirs(os.path.join(self.tmp, "history"))
        self._orig = (ml.ASSETS_FILE, ml.SOURCES_DIR, ml.HIST_DIR)
        ml.ASSETS_FILE = os.path.join(self.tmp, "assets.json")
        ml.SOURCES_DIR = os.path.join(self.tmp, "sources")
        ml.HIST_DIR = os.path.join(self.tmp, "history")
        with open(ml.ASSETS_FILE, "w", encoding="utf-8") as fh:
            json.dump({"assets": [
                {"id": "nvda", "name": "NVIDIA", "type": "yahoo", "owner": "cloud",
                 "symbol": "NVDA"},
                {"id": "gold_twd", "name": "黃金存摺", "type": "bot_gold", "owner": "local",
                 "symbol": "TWD"},
            ]}, fh)
        self.schedule = {"graceMinutes": 30,
                         "cloud": {"full": {"days": "0-6", "at": ["15:20"]}},
                         "local": {"full": {"days": "0-6", "at": ["10:05"]}}}

    def tearDown(self):
        ml.ASSETS_FILE, ml.SOURCES_DIR, ml.HIST_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def shard(self, source, run_at, slot, mode, assets):
        with open(os.path.join(self.tmp, "sources", "%s.json" % source), "w",
                  encoding="utf-8") as fh:
            json.dump({"source": source, "runAt": run_at, "slot": slot,
                       "slotLabel": fd.SLOT_LABEL.get(slot, slot), "mode": mode,
                       "requests": 1, "count": len(assets), "assets": assets}, fh)

    def history(self, aid, points):
        with open(os.path.join(self.tmp, "history", "%s.json" % aid), "w",
                  encoding="utf-8") as fh:
            json.dump({"points": points}, fh)


class TestLatestSlotComesFromCloudOnly(MergeSandbox):
    """對照組：把 merge_latest 改回「取 runAt 較新的分片」，第一條會紅。"""

    def test_newer_local_shard_does_not_set_the_slot(self):
        now = datetime(2026, 9, 9, 22, 10, tzinfo=TPE)
        self.shard("cloud", "2026-09-09T15:30:40+08:00", "review", "full",
                   {"nvda": {"id": "nvda", "status": "ok", "price": 1.0,
                             "lastSuccessAt": "2026-09-09T15:30:40+08:00"}})
        self.shard("local", "2026-09-09T22:02:56+08:00", "light", "full",
                   {"gold_twd": {"id": "gold_twd", "status": "ok", "price": 2.0,
                                 "lastSuccessAt": "2026-09-09T22:02:56+08:00"}})
        latest, _ = ml.merge(now=now, schedule=self.schedule)
        self.assertEqual(latest["slot"], "review", "時段只能聽雲端，本機的 light 不算")
        self.assertEqual(latest["slotLabel"], fd.SLOT_LABEL["review"])
        # 但資料新鮮度還是兩邊一起看：updatedAt 取較新的那一邊
        self.assertEqual(latest["updatedAt"], "2026-09-09T22:02:56+08:00")

    def test_missing_cloud_shard_is_unknown_not_guessed(self):
        now = datetime(2026, 9, 9, 22, 10, tzinfo=TPE)
        self.shard("local", "2026-09-09T22:02:56+08:00", "light", "full",
                   {"gold_twd": {"id": "gold_twd", "status": "ok", "price": 2.0,
                                 "lastSuccessAt": "2026-09-09T22:02:56+08:00"}})
        latest, _ = ml.merge(now=now, schedule=self.schedule)
        self.assertEqual(latest["slot"], "unknown")
        self.assertEqual(latest["slotLabel"], "（雲端尚未回報時段）")
        self.assertEqual(latest["updatedAt"], "2026-09-09T22:02:56+08:00")


class TestOverdueProvisionalWarning(MergeSandbox):
    """暫定點的日期 <= 前天才警告；今天／昨天的不警告；台銀不看。"""

    def test_flags_only_points_older_than_yesterday(self):
        now = datetime(2026, 9, 10, 16, 0, tzinfo=TPE)
        self.history("nvda", [
            {"d": "2026-09-07", "c": 1.0, "dateSource": "intraday", "provisional": True},  # 前三天 → 警告
            {"d": "2026-09-08", "c": 1.0, "dateSource": "intraday", "provisional": True},  # 前天 → 警告
            {"d": "2026-09-09", "c": 1.0, "dateSource": "intraday", "provisional": True},  # 昨天 → 不警告
            {"d": "2026-09-10", "c": 1.0, "dateSource": "intraday", "provisional": True},  # 今天 → 不警告
        ])
        self.history("gold_twd", [
            {"d": "2026-09-01", "c": 1.0, "dateSource": "intraday", "provisional": True},  # 台銀不看
        ])
        with open(ml.ASSETS_FILE, encoding="utf-8") as fh:
            enabled = json.load(fh)["assets"]
        msgs = ml.overdue_provisional_points(enabled, now)
        self.assertEqual(len(msgs), 1)
        self.assertIn("nvda", msgs[0])
        self.assertIn("2026-09-07", msgs[0])
        self.assertIn("2026-09-08", msgs[0])
        self.assertNotIn("2026-09-09", msgs[0])
        self.assertNotIn("gold_twd", " ".join(msgs))

    def test_no_provisional_no_warning(self):
        now = datetime(2026, 9, 10, 16, 0, tzinfo=TPE)
        self.history("nvda", [{"d": "2026-09-07", "c": 1.0, "dateSource": "yahoo"}])
        with open(ml.ASSETS_FILE, encoding="utf-8") as fh:
            enabled = json.load(fh)["assets"]
        self.assertEqual(ml.overdue_provisional_points(enabled, now), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
