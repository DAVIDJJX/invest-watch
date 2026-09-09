#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_reports.py — 停點 7-2：四個時段各一份、manual 分流、新舊名字相容

跑法：
    python -m unittest discover -s scripts -p "test_*.py" -v

輸入用倉庫裡真實的 data/latest.json（公開市場資料，唯讀）；
所有寫入（archive／index／report-latest／report-manual）全部導到暫存目錄，
測試絕對不會碰到真實的報告檔。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import report as rp                                          # noqa: E402


def load_real_latest():
    with open(os.path.join(ROOT, "data", "latest.json"), encoding="utf-8") as fh:
        return json.load(fh)


class ReportSandbox(unittest.TestCase):
    """把 report.py 的所有寫入路徑換成暫存目錄；歷史檔照樣讀真的（唯讀）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-report-")
        self._orig = (rp.ARCHIVE_DIR, rp.INDEX_FILE, rp.REPORT_LATEST, rp.REPORT_MANUAL)
        rp.ARCHIVE_DIR = os.path.join(self.tmp, "archive")
        rp.INDEX_FILE = os.path.join(rp.ARCHIVE_DIR, "index.json")
        rp.REPORT_LATEST = os.path.join(self.tmp, "report-latest.json")
        rp.REPORT_MANUAL = os.path.join(self.tmp, "report-manual.json")
        os.makedirs(rp.ARCHIVE_DIR)
        self.latest = load_real_latest()

    def tearDown(self):
        rp.ARCHIVE_DIR, rp.INDEX_FILE, rp.REPORT_LATEST, rp.REPORT_MANUAL = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)


class TestFourSlotsPlusManual(ReportSandbox):

    def test_every_slot_produces_its_own_archive_file(self):
        for slot in rp.REPORT_SLOTS + ("manual",):
            r = rp.generate(slot, self.latest)
            path = os.path.join(rp.ARCHIVE_DIR, r["date"], "%s.json" % slot)
            self.assertTrue(os.path.isfile(path), "%s 沒有寫進 archive" % slot)
            self.assertEqual(r["slot"], slot)
            self.assertEqual(r["title"], rp.SLOT_TITLE[slot])
            self.assertIn("producedBy", r)
            self.assertEqual(r["producedBy"]["host"], "local")

    def test_manual_writes_report_manual_and_leaves_report_latest_alone(self):
        """對照組：把 generate() 裡那行改回一律寫 REPORT_LATEST，這一條會紅。"""
        rp.generate("review", self.latest)
        self.assertEqual(self.read(rp.REPORT_LATEST)["slot"], "review")
        self.assertFalse(os.path.exists(rp.REPORT_MANUAL))

        rp.generate("manual", self.latest)
        self.assertEqual(self.read(rp.REPORT_MANUAL)["slot"], "manual")
        self.assertEqual(self.read(rp.REPORT_LATEST)["slot"], "review",
                         "手動報告不可以把首頁的最新報告換掉")
        # 但 manual 照樣進 archive，歷史頁才看得到
        r = self.read(rp.REPORT_LATEST)
        self.assertTrue(os.path.isfile(os.path.join(rp.ARCHIVE_DIR, r["date"], "manual.json")))

    def test_scheduled_slots_all_update_report_latest(self):
        for slot in rp.REPORT_SLOTS:
            rp.generate(slot, self.latest)
            self.assertEqual(self.read(rp.REPORT_LATEST)["slot"], slot)


class TestSlotContents(ReportSandbox):

    def ids(self, slot):
        return [s["id"] for s in rp.generate(slot, self.latest)["sections"]]

    def test_close_is_short_and_review_is_deep(self):
        close_ids = self.ids("close")
        review_ids = self.ids("review")
        self.assertLess(len(close_ids), len(review_ids), "收盤快報要比盤後檢討短")
        self.assertIn("tonight", review_ids, "盤後要有「今晚觀察」")
        self.assertNotIn("tonight", close_ids)
        # 盤後才有燈號一覽與漲跌排行（用 type 判，不綁 id 名稱）
        review_types = [s["type"] for s in rp.generate("review", self.latest)["sections"]]
        close_types = [s["type"] for s in rp.generate("close", self.latest)["sections"]]
        self.assertIn("table", review_types)
        self.assertNotIn("table", close_types)

    def test_midmorning_has_intraday_sections(self):
        ids = self.ids("midmorning")
        self.assertIn("tw", ids)
        self.assertIn("bot", ids)

    def test_tonight_section_is_facts_only(self):
        sec = rp.build_tonight_section(self.latest)
        self.assertEqual(sec["type"], "text")
        text = " ".join(sec["paragraphs"])
        self.assertIn("21:30", text)
        for banned in ("建議", "看漲", "看跌", "應該買", "應該賣"):
            self.assertNotIn(banned, text, "今晚觀察只能列事實，不能給建議")


class TestOldAndNewSlotNames(ReportSandbox):
    """舊 archive 用 midday，新的用 midmorning／review；讀舊檔的地方要兩種都認得。"""

    def test_slot_title_has_no_midday(self):
        self.assertNotIn("midday", rp.SLOT_TITLE)
        self.assertEqual(rp.REPORT_SLOTS, ("morning", "midmorning", "close", "review"))

    def test_cli_choices_come_from_report_slots(self):
        """CLI 的 --slot 選項必須由 REPORT_SLOTS 推出來，不能另外手寫一份。

        以前這條是真的把 report.py 當子程序跑 --slot midday 看它報錯；但做突變對照時
        若有人把 midday 加回選項，那一跑就會把【真實的】archive 寫壞。
        改成看原始碼：只要選項是從 REPORT_SLOTS 算出來的，midday 就不可能混進去。
        """
        with open(os.path.join(HERE, "report.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('choices=list(REPORT_SLOTS) + ["manual"]', src)
        self.assertNotIn('"midday"', src.split("SLOT_TITLE")[0],
                         "SLOT_TITLE 之前不該再出現 midday 這個名字")

    def test_index_sorts_old_and_new_names_together(self):
        """對照組：把 update_index 的排序表改回只有舊名字，這一條會紅。"""
        market = {"twTradingDay": True}
        fake = {"generatedAtText": "2026-09-09 15:30", "generatedAt": "x", "dataStatus": {}}
        for slot in ("manual", "review", "close", "midday", "midmorning", "morning"):
            rp.update_index("2026-09-09", slot, fake, market)
        entry = self.read(rp.INDEX_FILE)["days"][0]
        self.assertEqual(entry["slots"],
                         ["morning", "midmorning", "midday", "close", "review", "manual"])

    def test_previous_close_report_reads_both_generations(self):
        """昨天若是舊格式（只有 midday）也要找得到；新格式優先 review。"""
        def write_day(date, slots):
            d = os.path.join(rp.ARCHIVE_DIR, date)
            os.makedirs(d, exist_ok=True)
            for s in slots:
                rp.write_json(os.path.join(d, "%s.json" % s), {"slot": s, "date": date})
        rp.write_json(rp.INDEX_FILE, {"days": [
            {"date": "2026-09-09", "slots": ["morning"]},
            {"date": "2026-09-08", "slots": ["morning", "midday"]},      # 舊格式的一天
        ]})
        write_day("2026-09-08", ["morning", "midday"])
        got = rp.previous_close_report("2026-09-09")
        self.assertEqual((got["date"], got["slot"]), ("2026-09-08", "midday"))

        rp.write_json(rp.INDEX_FILE, {"days": [
            {"date": "2026-09-10", "slots": ["morning"]},
            {"date": "2026-09-09", "slots": ["morning", "close", "review"]},
        ]})
        write_day("2026-09-09", ["morning", "close", "review"])
        got = rp.previous_close_report("2026-09-10")
        self.assertEqual((got["date"], got["slot"]), ("2026-09-09", "review"),
                         "新格式要優先拿盤後檢討，不是收盤快報")


if __name__ == "__main__":
    unittest.main(verbosity=2)
