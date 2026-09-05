#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_publish.py — publish.py 的擁有權計算測試

跑法：
    python -m unittest discover -s scripts -p "test_*.py" -v

這裡只測「我這一輪產生哪些檔案」這件事，因為競態重試整段沒辦法用
單純的單元測試涵蓋（要真的有 git 遠端才會被拒），那部分在
scripts/test_race_recovery.py 用兩個 clone 加一個 bare repo 實測。

為什麼擁有權清單值得單獨測？
    publish.py 重試時的做法是「保留我的檔案 → 工作區整個回到遠端狀態 →
    把我的放回去」。漏列一個自己的檔案，那個檔案這一輪的成果就會被丟掉；
    多列一個別人的檔案，就會把對方剛推上來的內容還原掉。
    這份清單是整套競態處理正確性的根。
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import publish                                              # noqa: E402

TPE = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 5, 10, 30, tzinfo=TPE)


def load_assets():
    with open(publish.ASSETS_FILE, encoding="utf-8") as fh:
        return [a for a in json.load(fh)["assets"] if a.get("enabled", True)]


class TestOwnedPaths(unittest.TestCase):

    def test_shard_is_always_mine(self):
        for src in ("cloud", "local"):
            self.assertIn("data/sources/%s.json" % src,
                          publish.owned_paths(src, ["merge"], NOW))

    def test_never_claims_the_other_shard(self):
        """最重要的一條：絕對不可以把對方的分片列進自己的清單。

        列進去的話，重試時就會用我手上那份過期的副本蓋掉對方剛推上來的，
        正是這次改架構要消滅的問題。
        """
        self.assertNotIn("data/sources/local.json",
                         publish.owned_paths("cloud", ["merge"], NOW))
        self.assertNotIn("data/sources/cloud.json",
                         publish.owned_paths("local", ["merge"], NOW))

    def test_history_split_follows_assets_json(self):
        """歷史檔的歸屬完全照 assets.json 的 owner，不寫死 id。"""
        assets = load_assets()
        for src in ("cloud", "local"):
            mine = set(publish.owned_paths(src, ["merge"], NOW))
            for a in assets:
                path = "data/history/%s.json" % a["id"]
                owner = a.get("owner") or "cloud"
                if owner == src:
                    self.assertIn(path, mine, "%s 應該屬於 %s" % (a["id"], src))
                else:
                    self.assertNotIn(path, mine, "%s 不該屬於 %s" % (a["id"], src))

    def test_history_is_a_partition(self):
        """兩邊的歷史檔要互斥、而且合起來剛好涵蓋全部——不重不漏。"""
        assets = load_assets()
        c = {p for p in publish.owned_paths("cloud", ["merge"], NOW)
             if p.startswith("data/history/")}
        l = {p for p in publish.owned_paths("local", ["merge"], NOW)
             if p.startswith("data/history/")}
        self.assertEqual(c & l, set(), "同一個歷史檔不能兩邊都認領")
        self.assertEqual(c | l,
                         {"data/history/%s.json" % a["id"] for a in assets},
                         "所有啟用中的標的都要有人負責")

    def test_latest_json_belongs_to_both(self):
        """latest.json 是衍生檔，兩邊都會重算後提交，所以兩邊都要列。

        這不會造成互相覆蓋：重試時它是【重算】出來的，
        用的是對方剛推上來的分片 + 我自己的分片。
        """
        self.assertIn("data/latest.json", publish.owned_paths("cloud", ["merge"], NOW))
        self.assertIn("data/latest.json", publish.owned_paths("local", ["merge"], NOW))

    def test_report_files_only_when_report_was_run(self):
        """沒跑報告就不能把報告檔列成自己的。

        本機不產報告。如果本機把 report-latest.json 列進清單，
        重試時就會把雲端剛產生的報告用本機那份舊的蓋回去。
        """
        no_report = publish.owned_paths("local", ["merge"], NOW)
        self.assertNotIn("data/report-latest.json", no_report)
        self.assertFalse([p for p in no_report if p.startswith("data/archive/")])

        with_report = publish.owned_paths("cloud", ["merge", "report:morning"], NOW)
        self.assertIn("data/report-latest.json", with_report)
        self.assertIn("data/archive/2026-09-05/morning.json", with_report)
        self.assertIn("data/archive/2026-09-05/snapshot.json", with_report)
        self.assertIn("data/archive/index.json", with_report)

    def test_report_path_uses_the_given_slot(self):
        for slot in ("morning", "midday", "close", "manual"):
            p = publish.owned_paths("cloud", ["report:" + slot], NOW)
            self.assertIn("data/archive/2026-09-05/%s.json" % slot, p)

    def test_paths_are_repo_relative_with_forward_slashes(self):
        """一律用倉庫相對路徑加正斜線：git 指令在 Windows 上也吃這種寫法。"""
        for p in publish.owned_paths("cloud", ["merge", "report:close"], NOW):
            self.assertFalse(os.path.isabs(p), p)
            self.assertNotIn("\\", p, p)
            self.assertTrue(p.startswith("data/"), p)


class TestRebuildParsing(unittest.TestCase):

    def test_accepts_valid(self):
        self.assertEqual(publish.parse_rebuild("merge"), ["merge"])
        self.assertEqual(publish.parse_rebuild("merge,report:close"),
                         ["merge", "report:close"])
        self.assertEqual(publish.parse_rebuild(""), [])

    def test_rejects_typos(self):
        """打錯字要直接報錯，不要安靜地少跑一個步驟。"""
        for bad in ("Merge", "report", "report:evening", "merge,repot:close"):
            with self.assertRaises(SystemExit, msg="「%s」應該被拒絕" % bad):
                publish.parse_rebuild(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
