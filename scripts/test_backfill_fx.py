#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_backfill_fx.py — 匯率日線回補（scripts/backfill_fx_history.py）的離線測試，完全不連網

釘住的事（對照組見 docs/CHANGELOG.md A1-6）：
  1. 只增不改：只新增比現有第一天更早的日期；現有的點一個欄位都不動，連沒有 dateSource 的舊點也不補標。
  2. 重疊區逐日比對；任何一筆差超過 0.3% 就列進 over（主程式看到就不寫檔）。
  3. 新補的點標 dateSource=finmind；補完只留最近 400 點。
"""
import copy
import os
import sys
import unittest
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import backfill_fx_history as B   # noqa: E402
import fetch_data as fd           # noqa: E402


def days(start, n):
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def rows(start, n, buy=4.7, sell=4.75):
    return [{"date": d, "spotBuy": buy, "spotSell": sell, "cashBuy": 4.6, "cashSell": 4.8} for d in days(start, n)]


OLD = ([{"d": d, "spotBuy": 4.7, "spotSell": 4.75, "c": 4.75} for d in days("2026-03-02", 3)] +          # 最早的點沒有 dateSource
       [{"d": d, "spotBuy": 4.7, "spotSell": 4.75, "c": 4.75, "dateSource": "csv"} for d in days("2026-03-05", 3)])


class TestPlan(unittest.TestCase):
    def test_only_older_dates_are_added_and_tagged_finmind(self):
        added, report = B.plan_backfill(OLD, rows("2026-02-20", 20))
        self.assertEqual([p["d"] for p in added], days("2026-02-20", 10))                       # 2/20～3/1，比現有第一天早
        self.assertTrue(all(p["dateSource"] == "finmind" and p["c"] == p["spotSell"] for p in added))
        self.assertEqual((report["compared"], report["same"], report["over"]), (6, 6, []))
        self.assertEqual(report["firstExisting"], "2026-03-02")
        self.assertEqual(report["onlyFinmindInsideOurRange"], days("2026-03-08", 4))            # 區間內只有來源有的：不補，只記

    def test_existing_points_are_never_touched(self):
        """對照組：把重疊的日期也拿來覆寫 → 這一條會紅。"""
        before = copy.deepcopy(OLD)
        added, _ = B.plan_backfill(OLD, rows("2026-02-20", 20, buy=4.7, sell=4.75))
        merged = B.apply_backfill(OLD, added)
        self.assertEqual(merged[-len(OLD):], before)                                            # 原本的六個點原封不動
        self.assertNotIn("dateSource", merged[-len(OLD)])                                       # 沒有 dateSource 的舊點不補標
        self.assertEqual(OLD, before)
        self.assertEqual([p["d"] for p in merged], sorted(p["d"] for p in merged))

    def test_overlap_difference_over_threshold_is_reported(self):
        """對照組：門檻檢查拿掉 → 這一條會紅。"""
        bad = rows("2026-03-02", 6)
        bad[2]["spotSell"] = 4.75 * 1.004                                                       # 差 0.4%
        bad[4]["spotBuy"] = 4.7 * 1.002                                                         # 差 0.2%，不一致但沒過門檻
        _, report = B.plan_backfill(OLD, bad)
        self.assertEqual(len(report["diffs"]), 2)
        self.assertEqual([(r["d"], r["field"]) for r in report["over"]], [("2026-03-04", "即期賣出")])
        self.assertEqual(report["same"], 4)

    def test_result_is_capped_at_the_history_limit(self):
        old = [{"d": d, "spotBuy": 4.7, "spotSell": 4.75, "c": 4.75, "dateSource": "finmind"} for d in days("2026-03-02", 145)]
        added, _ = B.plan_backfill(old, rows("2025-01-01", 600))
        merged = B.apply_backfill(old, added)
        self.assertEqual(len(merged), fd.MAX_POINTS)
        self.assertEqual(merged[-1]["d"], old[-1]["d"])
        self.assertEqual(merged[-145:], old)

    def test_empty_history_takes_everything(self):
        added, report = B.plan_backfill([], rows("2026-01-01", 5))
        self.assertEqual(len(added), 5)
        self.assertIsNone(report["firstExisting"])


if __name__ == "__main__":
    unittest.main()
