#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_schedule_util.py — 過期判定的單元測試

跑法：
    python -m unittest discover -s scripts -p "test_*.py" -v
    或
    python scripts/test_schedule_util.py

全部用假的 now，不依賴真實時間，也不連網路。

測試用的日期（2026 年 9 月）：
    09-04 週五 / 09-05 週六 / 09-06 週日 / 09-07 週一
"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedule_util as su                                    # noqa: E402


# 測試自己帶一份排程，不讀 data/schedule.json——
# 這樣改真實排程時測試不會莫名其妙紅掉，測的是邏輯不是設定。
SCHEDULE = {
    "timezone": "Asia/Taipei",
    "graceMinutes": 30,
    "cloud": {
        "full": {"days": "0-6", "at": ["10:17", "13:17", "15:17"]}
    },
    "local": {
        "full": {"days": "0-6", "at": ["10:05", "13:05", "15:05"]},
        "light": {"days": "1-5", "from": "09:00", "to": "17:00", "everyMinutes": 30}
    },
    "reports": {"morning": "10:35", "midday": "13:35", "close": "15:35"}
}

FRI, SAT, SUN, MON = "2026-09-04", "2026-09-05", "2026-09-06", "2026-09-07"


def t(s):
    """'2026-09-04 11:20' → 帶台北時區的 datetime。"""
    return datetime.fromisoformat(s).replace(tzinfo=su.TPE)


def hhmm(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None


class TestDays(unittest.TestCase):
    """cron 星期寫法的解析。0=週日，跟 Python 的 weekday() 差一位，最容易錯。"""

    def test_parse(self):
        self.assertEqual(su.parse_days("0-6"), set(range(7)))
        self.assertEqual(su.parse_days("1-5"), {1, 2, 3, 4, 5})
        self.assertEqual(su.parse_days("1,3,5"), {1, 3, 5})
        self.assertEqual(su.parse_days("*"), set(range(7)))
        self.assertEqual(su.parse_days("5-1"), {5, 6, 0, 1})   # 跨週末

    def test_cron_dow(self):
        self.assertEqual(su.cron_dow(t(FRI + " 00:00").date()), 5)   # 週五
        self.assertEqual(su.cron_dow(t(SAT + " 00:00").date()), 6)   # 週六
        self.assertEqual(su.cron_dow(t(SUN + " 00:00").date()), 0)   # 週日
        self.assertEqual(su.cron_dow(t(MON + " 00:00").date()), 1)   # 週一


class TestExpectedTimes(unittest.TestCase):

    def test_local_full_weekday(self):
        got = su.expected_times("local", "full", t(FRI + " 00:00").date(), SCHEDULE)
        self.assertEqual([hhmm(x) for x in got],
                         [FRI + " 10:05", FRI + " 13:05", FRI + " 15:05"])

    def test_local_full_runs_on_weekend_too(self):
        """full 的 days 是 0-6，週末照跑——這跟 Windows 排程的實際設定一致。"""
        got = su.expected_times("local", "full", t(SAT + " 00:00").date(), SCHEDULE)
        self.assertEqual([hhmm(x) for x in got],
                         [SAT + " 10:05", SAT + " 13:05", SAT + " 15:05"])

    def test_local_light_is_weekday_only(self):
        """light 的 days 是 1-5，週末只剩 full 那三個點。"""
        wk = su.expected_times("local", "light", t(FRI + " 00:00").date(), SCHEDULE)
        we = su.expected_times("local", "light", t(SAT + " 00:00").date(), SCHEDULE)
        self.assertEqual(len(we), 3)
        self.assertEqual([hhmm(x) for x in we],
                         [SAT + " 10:05", SAT + " 13:05", SAT + " 15:05"])
        # 平日：09:00~17:00 每 30 分 = 17 個點，加上 full 的 10:05/13:05/15:05
        self.assertEqual(len(wk), 17 + 3)
        self.assertEqual(hhmm(wk[0]), FRI + " 09:00")
        self.assertEqual(hhmm(wk[-1]), FRI + " 17:00")   # 含結束時間

    def test_cloud_has_no_light_yet(self):
        """雲端的盤中輕量排程要等停點 5 才加，現在 light 跟 full 應該一樣。"""
        full = su.expected_times("cloud", "full", t(FRI + " 00:00").date(), SCHEDULE)
        light = su.expected_times("cloud", "light", t(FRI + " 00:00").date(), SCHEDULE)
        self.assertEqual(full, light)
        self.assertEqual([hhmm(x) for x in full],
                         [FRI + " 10:17", FRI + " 13:17", FRI + " 15:17"])


class TestCadence(unittest.TestCase):

    def test_default(self):
        # local 有 light 排程 → 預設 light；cloud 目前沒有 → 預設 full
        self.assertEqual(su.default_cadence("local", SCHEDULE), "light")
        self.assertEqual(su.default_cadence("cloud", SCHEDULE), "full")

    def test_asset_override(self):
        gold_bar = {"id": "gold_bar", "owner": "local", "cadence": "full"}
        gold_twd = {"id": "gold_twd", "owner": "local"}
        nvda = {"id": "nvda", "owner": "cloud"}
        self.assertEqual(su.cadence_of(gold_bar, SCHEDULE), "full")
        self.assertEqual(su.cadence_of(gold_twd, SCHEDULE), "light")
        self.assertEqual(su.cadence_of(nvda, SCHEDULE), "full")

    def test_real_assets_json_matches(self):
        """真實的 data/assets.json 只有 gold_bar 需要寫 cadence。"""
        import json
        path = os.path.join(su.ROOT, "data", "assets.json")
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        overridden = [a["id"] for a in cfg["assets"] if a.get("cadence")]
        self.assertEqual(overridden, ["gold_bar"])


class TestFourScenarios(unittest.TestCase):
    """規格裡指定的四個情境。

    注意 ① 和 ② 的 lastDue 跟原始需求寫的不一樣，原因記在各自的測試裡：
    full 的 days 是 0-6（週末照跑），所以週末與週一早上的「上一個排定點」
    落在週末，不是週五。這是排程設定造成的，不是演算法的問題。
    """

    def check(self, owner, cadence, now, last_success, expect_verdict, expect_due):
        verdict, last_due, _ = su.freshness(last_success, now, owner, cadence, SCHEDULE)
        self.assertEqual(hhmm(last_due), expect_due,
                         "lastDue 不對（now=%s）" % hhmm(now))
        self.assertEqual(verdict, expect_verdict,
                         "判定不對（now=%s, lastSuccess=%s, lastDue=%s）"
                         % (hhmm(now), last_success, hhmm(last_due)))

    # --- ① 週六 22:00 看黃金存摺 -----------------------------------------
    def test_1_saturday_night_gold_passbook(self):
        """需求寫 lastDue=週五 17:00；實際是週六 15:05，因為 full 週末也跑。

        結論一樣是 fresh：週六 15:05 那一輪抓到了，資料就是新的。
        """
        self.check("local", "light", t(SAT + " 22:00"),
                   SAT + "T15:05:30+08:00", "fresh", SAT + " 15:05")

    def test_1b_saturday_night_but_laptop_slept_all_weekend(self):
        """同一個時間點，但筆電整個週末沒醒 → stale。

        這是 ① 跟原始需求真正會分歧的地方：需求預期 fresh，實際是 stale。
        因為週六 10:05/13:05/15:05 三次排程都沒跑到，資料確實比排程落後。
        """
        self.check("local", "light", t(SAT + " 22:00"),
                   FRI + "T17:00:20+08:00", "stale", SAT + " 15:05")

    # --- ② 週一 09:30 看實體條塊 -----------------------------------------
    def test_2_monday_morning_gold_bar(self):
        """需求寫 lastDue=週五 15:05；實際是週日 15:05，一樣因為 full 週末也跑。

        重點在於這裡【沒有】用「距今幾分鐘」去比：實體條塊上次成功是
        週日 15:05，距今 18 小時半，用舊規則一定被判過期，用排定點就正常。
        """
        self.check("local", "full", t(MON + " 09:30"),
                   SUN + "T15:05:40+08:00", "fresh", SUN + " 15:05")

    def test_2b_monday_morning_gold_bar_laptop_slept_all_weekend(self):
        self.check("local", "full", t(MON + " 09:30"),
                   FRI + "T15:05:40+08:00", "stale", SUN + " 15:05")

    # --- ③ 週一 10:40 看實體條塊、筆電從週五就沒醒 -----------------------
    def test_3_monday_1040_gold_bar_stale(self):
        """完全符合需求：lastDue=週一 10:05 → stale。"""
        self.check("local", "full", t(MON + " 10:40"),
                   FRI + "T15:05:40+08:00", "stale", MON + " 10:05")

    def test_3b_same_moment_but_it_did_run(self):
        """對照組：同一時刻，如果 10:05 真的跑到了就該是 fresh。"""
        self.check("local", "full", t(MON + " 10:40"),
                   MON + "T10:05:30+08:00", "fresh", MON + " 10:05")

    # --- ④ 平日 11:20 黃金存摺、筆電 09:30 後睡著 ------------------------
    def test_4_weekday_1120_gold_passbook_stale(self):
        """完全符合需求：lastDue=11:00，09:30 已經超過 30 分寬限 → stale。"""
        self.check("local", "light", t(FRI + " 11:20"),
                   FRI + "T09:30:12+08:00", "stale", FRI + " 11:00")

    def test_4b_same_moment_but_it_did_run(self):
        self.check("local", "light", t(FRI + " 11:20"),
                   FRI + "T11:00:15+08:00", "fresh", FRI + " 11:00")


class TestFreshnessEdges(unittest.TestCase):

    def test_never_succeeded_is_error_not_stale(self):
        """從來沒抓成功過不是「舊」，是「壞」——不能拿舊資料假裝是新的。"""
        verdict, _, _ = su.freshness(None, t(FRI + " 11:20"), "local", "light", SCHEDULE)
        self.assertEqual(verdict, "error")

    def test_grace_boundary_is_inclusive(self):
        """剛好落在寬限邊界上算 fresh（規格寫的是 >=）。"""
        # lastDue = 11:00，寬限 30 分 → 10:30 整算 fresh，10:29 算 stale
        self.check_at(FRI + "T10:30:00+08:00", "fresh")
        self.check_at(FRI + "T10:29:59+08:00", "stale")

    def check_at(self, last_success, expect):
        verdict, _, _ = su.freshness(last_success, t(FRI + " 11:20"),
                                     "local", "light", SCHEDULE)
        self.assertEqual(verdict, expect, "lastSuccess=%s" % last_success)

    def test_naive_timestamp_treated_as_taipei(self):
        """沒有時區的舊資料要當台北時間。

        這個案例是刻意挑的：09:30 當台北時間是 stale，當成 UTC 會變成當天 17:30，
        反而比 lastDue 還新 → fresh。也就是說「把 naive 當 UTC」這個錯誤
        會把過期的資料說成是新的，正好違反誠實鐵則。
        必須用會分辨出兩者的案例來測，否則測了等於沒測。
        """
        verdict, _, _ = su.freshness("2026-09-04T09:30:00", t(FRI + " 11:20"),
                                     "local", "light", SCHEDULE)
        self.assertEqual(verdict, "stale")

    def test_bad_lastsuccess_type_is_error_not_crash(self):
        """lastSuccessAt 型別怪怪的（數字、字典、亂碼字串）要當成沒成功過。

        直接拿去比大小會丟 TypeError，那會讓 merge 整支掛掉、latest.json 產不出來，
        網站上 13 張卡片全部消失——只因為某一項的時間欄位型別不對。
        """
        for bad in (1767200000, {"t": 1}, ["x"], "不是時間", "", True):
            verdict, _, _ = su.freshness(bad, t(FRI + " 11:20"),
                                         "local", "light", SCHEDULE)
            self.assertEqual(verdict, "error", "輸入 %r 應該判成 error" % (bad,))

    def test_no_schedule_block_means_no_due_time(self):
        """排程完全沒有的 owner 算不出排定點。

        依規格這會回 fresh；merge_latest.py 另外會把這種「設定漏了」的情況
        改標成 error 並印警告，見 test_merge_latest 那邊。
        """
        empty = {"graceMinutes": 30, "nobody": {}}
        verdict, last_due, next_due = su.freshness(
            FRI + "T11:00:00+08:00", t(FRI + " 11:20"), "nobody", "full", empty)
        self.assertIsNone(last_due)
        self.assertIsNone(next_due)
        self.assertEqual(verdict, "fresh")
        self.assertFalse(su.has_schedule("nobody", empty))
        self.assertTrue(su.has_schedule("local", SCHEDULE))

    def test_lookback_reaches_a_whole_week(self):
        """往回要找得夠遠：一週只跑一天的排程，也要找得到上一次。

        cron 的星期寫法最稀疏就是「每週只跑一天」，間隔剛好 7 天，
        所以 7 天的上限剛好夠用、也不會把真的排程漏掉。
        這個測試把下限釘住：改成 6 天就會壞。
        """
        weekly = {"graceMinutes": 30,
                  "weekly": {"full": {"days": "1", "at": ["10:05"]}}}   # 只有週一
        # 週一 09:00，今天的 10:05 還沒到 → 要往回找到「上週一」的 10:05
        got = su.last_expected("weekly", "full", t(MON + " 09:00"), weekly)
        self.assertEqual(hhmm(got), "2026-08-31 10:05")
        self.assertEqual((t(MON + " 09:00").date() - got.date()).days, 7)

    def test_lookahead_reaches_a_whole_week(self):
        weekly = {"graceMinutes": 30,
                  "weekly": {"full": {"days": "1", "at": ["10:05"]}}}
        got = su.next_expected("weekly", "full", t(MON + " 10:30"), weekly)
        self.assertEqual(hhmm(got), "2026-09-14 10:05")   # 下週一

    def test_next_expected(self):
        self.assertEqual(hhmm(su.next_expected("local", "light", t(FRI + " 11:20"), SCHEDULE)),
                         FRI + " 11:30")
        # 週五 17:00 之後：light 收工，下一個是週六的 full 10:05
        self.assertEqual(hhmm(su.next_expected("local", "light", t(FRI + " 17:30"), SCHEDULE)),
                         SAT + " 10:05")

    def test_last_expected_includes_now(self):
        """now 剛好落在排定點上時，那個點算「已到期」。"""
        self.assertEqual(hhmm(su.last_expected("local", "light", t(FRI + " 11:00"), SCHEDULE)),
                         FRI + " 11:00")


class TestConfigIsActuallyRead(unittest.TestCase):
    """這一組刻意用【跟正式設定不一樣】的值。

    如果測試用的排程跟 data/schedule.json 長得一模一樣，那麼「函式根本沒讀傳進去的
    設定、偷偷去讀正式檔案」這種錯誤會完全測不出來——兩邊答案剛好相同。
    所以這裡的 graceMinutes 用 5、full 的 days 用 1-5（正式是 30 和 0-6）。
    """

    ODD = {
        "graceMinutes": 5,
        "acme": {
            "full": {"days": "1-5", "at": ["08:00"]},
            "light": {"days": "3", "from": "20:00", "to": "21:00", "everyMinutes": 60},
        },
    }

    def test_grace_comes_from_the_given_schedule(self):
        """寬限要用傳進來的 5 分鐘，不是正式檔案的 30 分鐘。"""
        self.assertEqual(su.grace_minutes(self.ODD), 5)
        # lastDue = 週五 08:00，寬限 5 分 → 07:55 算 fresh、07:54 算 stale。
        # 如果偷讀到正式設定的 30 分，07:54 會變成 fresh，這個測試就會抓到。
        for stamp, expect in ((FRI + "T07:55:00+08:00", "fresh"),
                              (FRI + "T07:54:00+08:00", "stale")):
            verdict, last_due, _ = su.freshness(stamp, t(FRI + " 09:00"),
                                                "acme", "full", self.ODD)
            self.assertEqual(hhmm(last_due), FRI + " 08:00")
            self.assertEqual(verdict, expect, "lastSuccess=%s" % stamp)

    def test_full_block_honours_its_own_days(self):
        """full 也要看自己的 days。

        正式設定的 full 是 0-6（每天），所以「full 完全不看 days」這個錯誤
        在正式設定下看不出來。這裡用 1-5，週六就必須是空的。
        """
        fri = su.expected_times("acme", "full", t(FRI + " 00:00").date(), self.ODD)
        sat = su.expected_times("acme", "full", t(SAT + " 00:00").date(), self.ODD)
        self.assertEqual([hhmm(x) for x in fri], [FRI + " 08:00"])
        self.assertEqual(sat, [])

    def test_light_block_honours_its_own_days_and_step(self):
        # light 只有週三，週五應該只剩 full 的 08:00
        fri = su.expected_times("acme", "light", t(FRI + " 00:00").date(), self.ODD)
        self.assertEqual([hhmm(x) for x in fri], [FRI + " 08:00"])
        wed = su.expected_times("acme", "light", t("2026-09-02 00:00").date(), self.ODD)
        self.assertEqual([hhmm(x) for x in wed],
                         ["2026-09-02 08:00", "2026-09-02 20:00", "2026-09-02 21:00"])


class TestCadenceValidation(unittest.TestCase):
    """cadence 寫錯字時要退回「比較嚴格」的那一邊。

    full 一天只有 3 個排定點、light 有 20 個，所以 full 是寬鬆的那一邊。
    寫錯字時如果悄悄退回 full，會把真正過期的資料判成 fresh——失敗的方向錯了。
    """

    def test_typo_falls_back_to_owner_default_not_full(self):
        typo = {"id": "x", "owner": "local", "cadence": "Light"}
        self.assertFalse(su.cadence_is_valid(typo))
        self.assertEqual(su.cadence_of(typo, SCHEDULE), "light")   # 不是 full

    def test_valid_values_pass_through(self):
        for v in ("full", "light"):
            a = {"id": "x", "owner": "local", "cadence": v}
            self.assertTrue(su.cadence_is_valid(a))
            self.assertEqual(su.cadence_of(a, SCHEDULE), v)
        self.assertTrue(su.cadence_is_valid({"id": "x", "owner": "local"}))

    def test_typo_does_not_turn_stale_into_fresh(self):
        """實際驗一次方向：10:06 抓到、12:00 來看。

        light（正確）：lastDue=12:00，寬限後 11:30，10:06 落後 → stale
        full（寫錯字若退回這裡）：lastDue=10:05，寬限後 09:35，10:06 → fresh
        """
        stamp = FRI + "T10:06:00+08:00"
        now = t(FRI + " 12:00")
        self.assertEqual(su.freshness(stamp, now, "local", "light", SCHEDULE)[0], "stale")
        self.assertEqual(su.freshness(stamp, now, "local", "full", SCHEDULE)[0], "fresh")
        # 寫錯字的標的要走 light（stale），不是 full（fresh）
        typo = {"id": "x", "owner": "local", "cadence": "Light"}
        cad = su.cadence_of(typo, SCHEDULE)
        self.assertEqual(su.freshness(stamp, now, "local", cad, SCHEDULE)[0], "stale")


if __name__ == "__main__":
    unittest.main(verbosity=2)
