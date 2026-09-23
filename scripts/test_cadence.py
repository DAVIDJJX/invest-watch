#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_cadence.py — 分析系列 A1-1：assets.json 的 cadence=full 對所有 type 一律有效

以前只有實體條塊與 FinMind 匯率各自在 handler 裡寫「盤中輕量更新不重抓」；
國際金價 gold_intl（type=yahoo、cadence=full）加入後，改成 fetch_data.py 主流程統一判斷：
  * --light 且 cadence=full 且上一次成功 → 跳過、沿用上次結果（carriedOver）
  * 上一次失敗或從來沒抓過 → 照抓（一次失敗不能一直錯到下一個完整更新）
  * 完整更新、或 cadence 不是 full → 照抓
把 skip_for_cadence 改壞，這裡一定要紅（對照組見 docs/CHANGELOG.md 分析系列 A1-1）。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fetch_data as fd   # noqa: E402

FULL = {"id": "x", "type": "yahoo", "cadence": "full"}
NORMAL = {"id": "y", "type": "yahoo"}
OK_PREV = {"status": "ok", "price": 1.0}
BAD_PREV = {"status": "error", "price": None}


class TestSkipForCadence(unittest.TestCase):
    def test_light_and_full_and_last_time_ok_skips(self):
        self.assertTrue(fd.skip_for_cadence(FULL, True, OK_PREV))

    def test_light_and_full_but_last_time_failed_fetches_again(self):
        self.assertIsNone(fd.skip_for_cadence(FULL, True, BAD_PREV))

    def test_light_and_full_but_never_fetched_fetches(self):
        self.assertIsNone(fd.skip_for_cadence(FULL, True, None))

    def test_full_update_never_skips(self):
        self.assertIsNone(fd.skip_for_cadence(FULL, False, OK_PREV))

    def test_assets_without_cadence_follow_the_light_schedule(self):
        self.assertIsNone(fd.skip_for_cadence(NORMAL, True, OK_PREV))

    def test_explicit_light_cadence_is_not_skipped(self):
        self.assertIsNone(fd.skip_for_cadence(dict(NORMAL, cadence="light"), True, OK_PREV))

    def test_reason_text_says_why(self):
        self.assertIn("cadence=full", fd.skip_for_cadence(FULL, True, OK_PREV))


class TestRealAssets(unittest.TestCase):
    """健康檢查：真的 assets.json 裡 gold_intl 是 type=yahoo、cadence=full、owner=cloud、group 貴金屬。"""

    def setUp(self):
        with open(os.path.join(ROOT, "data", "assets.json"), encoding="utf-8") as fh:
            self.assets = {a["id"]: a for a in json.load(fh)["assets"]}

    def test_gold_intl_is_configured_as_ruled(self):
        a = self.assets["gold_intl"]
        self.assertEqual((a["type"], a["cadence"], a["owner"], a["group"], a["assetClass"], a["symbol"]),
                         ("yahoo", "full", "cloud", "貴金屬", "commodity", "GC=F"))
        self.assertIn("盎司", a["unit"])

    def test_every_asset_has_an_asset_class_from_the_allowed_set(self):
        allowed = {"stock", "index", "index_etf", "bond_etf", "commodity", "crypto", "fx", "gold_tw"}
        for aid, a in self.assets.items():
            self.assertIn(a.get("assetClass"), allowed, aid)

    def test_asset_classes_match_the_ruling(self):
        want = {"gold_twd": "gold_tw", "gold_cny": "gold_tw", "gold_bar": "gold_tw", "gold_intl": "commodity",
                "twii": "index", "gspc": "index", "tw2330": "stock", "nvda": "stock",
                "tw00646": "index_etf", "tw00679b": "bond_etf", "wti": "commodity", "btc": "crypto",
                "fx_usd": "fx", "fx_cny": "fx"}
        self.assertEqual({k: v["assetClass"] for k, v in self.assets.items()}, want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
