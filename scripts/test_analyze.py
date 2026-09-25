#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_analyze.py — scripts/analyze.py 的離線測試（完全不連網）

釘住的事（把 analyze.py 的判準改壞，這裡一定要紅；對照組見 docs/CHANGELOG.md 分析系列 A1-1）：
  1. Yahoo 週線只收「宣告 1wk、實際間距 6～8 天、已完成」的 K 棒；range=max 那種月線回應整批不收。
  2. 匯率週線每個 ISO 週只取最後一個營業日；FinMind 的 -1 哨兵列不進來。
  3. 補抓規則：沒有檔案→整段；最後一根早於最近一個完成週的週一→從它往前 21 天補；否則這週不發請求（每週只抓一次）。
  4. 波動、最大回檔、目前回檔、相關矩陣的算法；視窗不夠就「資料不足」；對角線不是 1 要喊出來。
  5. 拆解：盎司→公克用 31.1035；同一天／前一日兩個口徑；00646 = S&P × 匯率 + 殘差。
  6. 主機白名單：台銀連線都不發；不在名單的主機也不發。
  7. status.json：錯誤要寫進去、不含本機路徑；結束碼 0／2；dry-run 不寫檔。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, time as dtime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import analyze as A       # noqa: E402
import fetch_data as fd   # noqa: E402
import net_policy         # noqa: E402

DAY = 86400
NOW = datetime(2026, 9, 24, 15, 35, tzinfo=A.TPE)          # 測試裡的「現在」：2026-09-24（四）15:35
NOW_EPOCH = NOW.timestamp()


def monday(dstr):
    return A.monday_of(dstr).strftime("%Y-%m-%d")


def weekly_body(n=60, start="2025-07-07", gran="1wk", step_days=7, closes=None, gmtoffset=-14400,
                include_open_week=False, dup_tail=False, null_at=(), first_trade=None, currency="USD", name=None):
    """假的 Yahoo chart 回應：n 根週棒，從 start（星期一）起、每 step_days 一根，時間戳＝當地 09:30。"""
    t0 = A.epoch_of_day(start) + 9 * 3600 + 1800 - gmtoffset
    ts = [t0 + i * step_days * DAY for i in range(n)]
    cs = list(closes) if closes else [100.0 + i for i in range(n)]
    for i in null_at:
        cs[i] = None
    if include_open_week:                      # 最後補一根「起始＋7 天 > 現在」的
        ts.append(int(NOW_EPOCH) - 2 * DAY)
        cs.append(999.0)
    if dup_tail:                               # 尾端多一個跟前一根同值、只差兩天的即時點
        ts.append(ts[-1] + 2 * DAY)
        cs.append(cs[-1])
    return {"chart": {"error": None, "result": [{
        "meta": {"dataGranularity": gran, "gmtoffset": gmtoffset, "currency": currency, "exchangeName": "SNP",
                 "firstTradeDate": first_trade, "longName": name},
        "timestamp": ts, "indicators": {"quote": [{"close": cs}]}}]}}


def pts(start="2016-01-04", n=200, closes=None):
    """合成的週線 [{d, c}]，從 start 起每 7 天一根。"""
    d0 = A.parse_day(start)
    cs = list(closes) if closes else [100.0 * (1.001 ** i) for i in range(n)]
    return [{"d": (d0 + timedelta(days=7 * i)).strftime("%Y-%m-%d"), "c": cs[i], "dateSource": "yahoo-week"}
            for i in range(len(cs))]


def fx_rows(days):
    """FinMind 每日列（已解析格式）。"""
    return [{"date": d, "spotBuy": 31.4, "spotSell": 31.5, "cashBuy": 31.2, "cashSell": 31.7} for d in days]


# --------------------------------------------------------------------------
# 1. Yahoo 週線的三道檢查
# --------------------------------------------------------------------------

class TestYahooWeekly(unittest.TestCase):
    def test_good_weekly_bars_are_accepted_with_exchange_local_dates(self):
        pts_, info = A.parse_yahoo_weekly(weekly_body(n=60), NOW_EPOCH)
        self.assertEqual(len(pts_), 60)
        self.assertEqual(pts_[0]["d"], "2025-07-07")                        # 週一、交易所當地日期
        self.assertEqual(pts_[-1]["dateSource"], "yahoo-week")
        self.assertEqual(info["granularity"], "1wk")

    def test_monthly_response_is_rejected_even_with_http_200(self):
        with self.assertRaises(A.AnalyzeError) as cm:
            A.parse_yahoo_weekly(weekly_body(n=60, gran="1mo", step_days=30), NOW_EPOCH)
        self.assertIn("1mo", str(cm.exception))

    def test_declared_weekly_but_monthly_spacing_is_rejected(self):
        with self.assertRaises(A.AnalyzeError) as cm:
            A.parse_yahoo_weekly(weekly_body(n=60, gran="1wk", step_days=30), NOW_EPOCH)
        self.assertIn("間距", str(cm.exception))

    def test_in_progress_week_is_dropped(self):
        pts_, info = A.parse_yahoo_weekly(weekly_body(n=60, include_open_week=True), NOW_EPOCH)
        self.assertEqual(len(pts_), 60)
        self.assertEqual(info["droppedInProgress"], 1)
        self.assertNotIn(999.0, [p["c"] for p in pts_])

    def test_completed_week_boundary_is_start_plus_seven_days(self):
        # 起始＋7 天 == 現在 → 算完成；起始＋7 天 == 現在＋1 秒 → 進行中
        body = weekly_body(n=1, start="2025-07-07")
        t = body["chart"]["result"][0]["timestamp"][0]
        body["chart"]["result"][0]["timestamp"] = [t - 7 * DAY, t]      # 兩根：前一根一定完成
        body["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [1.0, 2.0]
        done, _ = A.parse_yahoo_weekly(body, now_epoch=t + 7 * DAY)
        self.assertEqual(len(done), 2)
        open_, _ = A.parse_yahoo_weekly(body, now_epoch=t + 7 * DAY - 1)
        self.assertEqual(len(open_), 1)

    def test_null_closes_are_skipped_and_duplicate_tail_removed(self):
        pts_, _ = A.parse_yahoo_weekly(weekly_body(n=60, null_at=(3, 4), dup_tail=True), NOW_EPOCH)
        self.assertEqual(len(pts_), 58)

    def test_error_body_and_empty_result_are_rejected(self):
        with self.assertRaises(A.AnalyzeError):
            A.parse_yahoo_weekly({"chart": {"result": None, "error": {"code": "Not Found"}}}, NOW_EPOCH)
        with self.assertRaises(A.AnalyzeError):
            A.parse_yahoo_weekly({"chart": {"result": [], "error": None}}, NOW_EPOCH)

    def test_dates_before_1970_do_not_crash(self):
        self.assertEqual(A.local_day(-1325583000), "1927-12-30")
        _, info = A.parse_yahoo_weekly(weekly_body(n=60, first_trade=-1325583000), NOW_EPOCH)
        self.assertEqual(info["firstTradeDate"], "1927-12-30")

    def test_symbol_comes_from_assets_json_rules(self):
        self.assertEqual(A.yahoo_symbol({"type": "yahoo", "symbol": "GC=F"}), "GC=F")
        self.assertEqual(A.yahoo_symbol({"type": "twse_stock", "symbol": "00679B", "yahooSymbol": "00679B.TWO"}), "00679B.TWO")
        self.assertIsNone(A.yahoo_symbol({"type": "finmind_fx", "symbol": "USD"}))

    def test_weekly_url_uses_period1_period2_not_range_max(self):
        seen = {}

        class F(object):
            def get(self, url, **kw):
                seen["url"] = url
                return weekly_body(n=60)

        A.fetch_yahoo_weekly(F(), "GC=F", 0, int(NOW_EPOCH), NOW_EPOCH)
        self.assertIn("period1=0&period2=%d&interval=1wk" % int(NOW_EPOCH), seen["url"])
        self.assertNotIn("range=", seen["url"])
        self.assertIn("GC%3DF", seen["url"])


# --------------------------------------------------------------------------
# 2. 匯率週線、3. 補抓規則、合併
# --------------------------------------------------------------------------

class TestFxWeeklyAndPlan(unittest.TestCase):
    def test_one_row_per_iso_week_last_business_day(self):
        days = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",      # W38
                "2026-09-21", "2026-09-22"]                                                # W39（週二為止）
        wk = A.weekly_from_daily_fx(fx_rows(days))
        self.assertEqual([p["d"] for p in wk], ["2026-09-18", "2026-09-22"])
        self.assertEqual(wk[0]["dateSource"], "finmind")
        self.assertEqual(wk[0]["c"], 31.5)

    def test_current_week_is_not_stored_for_fx(self):
        days = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23"]
        wk = A.weekly_from_daily_fx(fx_rows(days), today=NOW.date())         # NOW 是 9/24（四），那一週還沒走完
        self.assertEqual([p["d"] for p in wk], ["2026-09-18"])
        wk2 = A.weekly_from_daily_fx(fx_rows(days), today=A.parse_day("2026-09-28"))
        self.assertEqual([p["d"] for p in wk2], ["2026-09-18", "2026-09-23"])

    def test_sentinel_rows_never_get_in(self):
        rows = fx_rows(["2026-09-14", "2026-09-15"])
        rows[1]["spotSell"] = None                       # parse_finmind_fx 對 -1 就是給 None
        wk = A.weekly_from_daily_fx(rows)
        self.assertEqual([p["d"] for p in wk], ["2026-09-14"])
        # 真的 -1 走 fetch_data 的解析器：整列被丟掉
        j = {"msg": "success", "status": 200, "data": [
            {"date": "2006-01-02", "currency": "USD", "spot_buy": -1, "spot_sell": -1, "cash_buy": -1, "cash_sell": -1},
            {"date": "2006-01-03", "currency": "USD", "spot_buy": 32.7, "spot_sell": 32.8, "cash_buy": 32.5, "cash_sell": 33.0}]}
        parsed = fd.parse_finmind_fx(j, "USD")
        self.assertEqual([r["date"] for r in parsed], ["2006-01-03"])

    def test_refetch_plan(self):
        today = NOW.date()                                                                     # 2026-09-24 星期四
        self.assertEqual(A.last_completed_week_monday(today), A.parse_day("2026-09-14"))
        self.assertEqual(A.last_completed_week_monday(A.parse_day("2026-09-21")), A.parse_day("2026-09-14"))   # 週一當天
        self.assertEqual(A.last_completed_week_monday(A.parse_day("2026-09-27")), A.parse_day("2026-09-14"))   # 週日還是同一週
        self.assertEqual(A.refetch_plan([], today)[0], "full")
        have_last_week = [{"d": "2026-09-14", "c": 1}]                                         # 上週的週棒已經在 → 這週不抓
        self.assertEqual(A.refetch_plan(have_last_week, today)[0], "skip")
        tuesday_bar = [{"d": "2026-09-15", "c": 1}]                                            # 週一休市、週棒落在週二也算有
        self.assertEqual(A.refetch_plan(tuesday_bar, today)[0], "skip")
        fx_friday = [{"d": "2026-09-18", "c": 1}]                                              # 匯率是該週最後一個營業日
        self.assertEqual(A.refetch_plan(fx_friday, today)[0], "skip")
        two_weeks_old = [{"d": "2026-09-07", "c": 1}]
        mode, start, _ = A.refetch_plan(two_weeks_old, today)
        self.assertEqual(mode, "incremental")
        self.assertEqual(start, "2026-08-17")                                                  # 往前 21 天
        eight_days = [{"d": (today - timedelta(days=8)).strftime("%Y-%m-%d"), "c": 1}]        # 舊規則會抓的情況，現在不抓
        self.assertEqual(A.refetch_plan(eight_days, today)[0], "skip")

    def test_fourteen_daily_reviews_fetch_exactly_twice(self):
        """對照組：把判斷改壞成每天抓 → 這一條會紅。連續 14 天每天 15:30 跑一次 review，只有兩個週一該發請求。"""
        w = World()
        try:
            w.write("assets.json", ASSETS)
            d0 = A.parse_day("2026-09-07")                                                     # 星期一；存檔停在兩週前
            w.write("history-long/gspc.json", {"id": "gspc", "points": [{"d": (d0 - timedelta(days=14)).strftime("%Y-%m-%d"), "c": 1.0, "dateSource": "x"}]})
            calls = []

            def fake_fetch(f, symbol, p1, p2, now_epoch=None):
                now_day = datetime.fromtimestamp(now_epoch, tz=A.TPE).date()
                due = A.last_completed_week_monday(now_day)
                calls.append(now_day.strftime("%Y-%m-%d"))
                out = [{"d": (due - timedelta(days=7 * k)).strftime("%Y-%m-%d"), "c": 2.0, "dateSource": "x"} for k in (3, 2, 1, 0)]
                return out, {"droppedInProgress": 1, "dominantWeekday": "Mon", "firstTradeDate": None}
            target = {"id": "gspc", "kind": "yahoo", "symbol": "^GSPC", "asset": {"id": "gspc", "name": "S&P 500", "currency": "USD", "unit": "點"}}
            orig = A.fetch_yahoo_weekly
            A.fetch_yahoo_weekly = fake_fetch
            try:
                for i in range(14):
                    now = datetime.combine(d0 + timedelta(days=i), dtime(15, 30), tzinfo=A.TPE)
                    status = {"longHistory": {}, "warnings": []}
                    A.update_long_history(target, None, now, status, paths=w.paths)
            finally:
                A.fetch_yahoo_weekly = orig
            self.assertEqual(calls, ["2026-09-07", "2026-09-14"])                             # 只有兩個週一
            last = json.load(io.open(os.path.join(w.data, "history-long", "gspc.json"), encoding="utf-8"))["points"][-1]["d"]
            self.assertEqual(last, "2026-09-07")                                               # 第二次抓到的是 9/7 那一週
        finally:
            w.close()

    def test_merge_keeps_one_point_per_week_and_prefers_new(self):
        old = [{"d": "2026-09-07", "c": 1}, {"d": "2026-09-14", "c": 2}]
        new = [{"d": "2026-09-14", "c": 2.5}, {"d": "2026-09-21", "c": 3}]
        m = A.merge_weekly(old, new)
        self.assertEqual([(p["d"], p["c"]) for p in m], [("2026-09-07", 1), ("2026-09-14", 2.5), ("2026-09-21", 3)])
        # 匯率用 ISO 週當鍵：同一週不同日子只留新的
        old = [{"d": "2026-09-17", "c": 31.4}]
        new = [{"d": "2026-09-18", "c": 31.5}]
        m = A.merge_weekly(old, new, key=lambda p: A.iso_week(p["d"]))
        self.assertEqual([(p["d"], p["c"]) for p in m], [("2026-09-18", 31.5)])

    def test_long_targets_from_real_assets_json(self):
        assets = A.load_assets()
        ids = sorted(t["id"] for t in A.long_targets(assets))
        self.assertEqual(ids, ["btc", "fx_usd", "gold_intl", "gspc", "nvda", "sp500tr", "tw00646", "tw00679b", "tw2330", "twii", "wti"])
        extra = [t for t in A.long_targets(assets) if t["id"] == "sp500tr"][0]
        self.assertEqual((extra["kind"], extra["symbol"]), ("yahoo", "^SP500TR"))          # 只是基準序列，不在 assets.json
        kinds = {t["id"]: t["kind"] for t in A.long_targets(assets)}
        self.assertEqual(kinds["fx_usd"], "finmind")
        self.assertEqual(kinds["gold_intl"], "yahoo")
        syms = {t["id"]: t["symbol"] for t in A.long_targets(assets)}
        self.assertEqual(syms["tw00679b"], "00679B.TWO")
        self.assertEqual(syms["gold_intl"], "GC=F")

    def test_dump_long_is_one_point_per_line(self):
        text = A.dump_long({"id": "x", "interval": "1wk"}, [{"d": "2026-09-14", "c": 1.5}, {"d": "2026-09-21", "c": 2}])
        self.assertEqual(text.count('{"d":'), 2)
        self.assertTrue(json.loads(text)["points"][1]["c"] == 2)


# --------------------------------------------------------------------------
# 4. 風險
# --------------------------------------------------------------------------

class TestRisk(unittest.TestCase):
    def test_weekly_returns(self):
        r = A.weekly_returns([{"d": "2026-09-07", "c": 100}, {"d": "2026-09-14", "c": 110}, {"d": "2026-09-21", "c": 99}])
        self.assertEqual([d for d, _ in r], ["2026-09-14", "2026-09-21"])
        self.assertAlmostEqual(r[0][1], 0.10)
        self.assertAlmostEqual(r[1][1], -0.10)

    def test_annualized_vol_known_value(self):
        # 週報酬 +1%／−1% 交替 52 週：樣本標準差 = sqrt(52·0.0001/51)
        rets = [("d%d" % i, 0.01 if i % 2 else -0.01) for i in range(52)]
        v = A.annualized_vol(rets, 52)
        expected = (52 * 0.0001 / 51) ** 0.5 * (52 ** 0.5) * 100
        self.assertAlmostEqual(v["pct"], round(expected, 2), places=2)
        self.assertEqual(v["weeks"], 52)

    def test_constant_growth_has_zero_vol(self):
        rets = [("d", 0.002)] * 60
        self.assertEqual(A.annualized_vol(rets, 52)["pct"], 0.0)

    def test_short_window_is_insufficient_not_computed(self):
        rets = [("d", 0.01)] * 40                          # 52 週視窗要 ≥47
        v = A.annualized_vol(rets, 52)
        self.assertIsNone(v["pct"])
        self.assertIn("資料不足", v["reason"])
        ok = A.annualized_vol([("d", 0.01), ("d", -0.01)] * 24, 52)      # 48 ≥ 47：夠
        self.assertIsNotNone(ok["pct"])

    def test_max_drawdown(self):
        series = pts(closes=[100, 120, 60, 90, 130, 125])
        dd = A.max_drawdown(series)
        self.assertEqual(dd["pct"], -50.0)
        self.assertEqual((dd["peakValue"], dd["troughValue"]), (120, 60))
        self.assertEqual(dd["peakDate"], series[1]["d"])
        self.assertEqual(dd["troughDate"], series[2]["d"])
        self.assertEqual(dd["recoveredDate"], series[4]["d"])

    def test_max_drawdown_not_recovered(self):
        dd = A.max_drawdown(pts(closes=[100, 80, 90]))
        self.assertEqual(dd["pct"], -20.0)
        self.assertIsNone(dd["recoveredDate"])

    def test_current_drawdown(self):
        cd = A.current_drawdown(pts(closes=[100, 200, 150]))
        self.assertEqual(cd["pct"], -25.0)
        self.assertEqual(cd["highValue"], 200)

    def test_correlation_matrix_identity_inverse_and_insufficient(self):
        rets = [0.02 if i % 2 else -0.01 for i in range(199)]              # 有起伏的報酬（常數報酬的相關係數沒有定義）
        ca, ci = [100.0], [100.0]
        for r in rets:
            ca.append(ca[-1] * (1 + r))
            ci.append(ci[-1] * (1 - r))                                       # 每週報酬剛好反號 → 相關係數 −1
        a = pts(n=200, closes=ca)
        b = [dict(p) for p in a]                          # 一模一樣
        inv = pts(n=200, closes=ci)
        short = pts(n=50, closes=ca[:50])
        res, problems = A.correlation_matrix({"a": a, "b": b, "inv": inv, "short": short})
        self.assertEqual(problems, [])
        m = res["matrix"]
        self.assertEqual(m["a"]["a"]["r"], 1.0)
        self.assertEqual(m["a"]["b"]["r"], 1.0)
        self.assertEqual(m["a"]["b"]["n"], 156)
        self.assertLess(m["a"]["inv"]["r"], -0.99)
        self.assertIsNone(m["a"]["short"]["r"])
        self.assertIn("資料不足", m["a"]["short"]["reason"])

    def test_diagonal_not_one_is_shouted(self):
        """對照組：把「對角線必須是 1」的檢查拿掉 → 這一條會紅。"""
        orig = A.pearson
        A.pearson = lambda xs, ys: 0.5
        try:
            _, problems = A.correlation_matrix({"a": pts(n=200)})
        finally:
            A.pearson = orig
        self.assertTrue(problems and "對角線" in problems[0])

    def test_ten_year_window(self):
        self.assertTrue(A.ten_year_window(pts(start="2016-01-04", n=530))["available"])     # 2016-01 → 2026-02，滿 10 年
        self.assertFalse(A.ten_year_window(pts(start="2016-01-04", n=520))["available"])    # 520 週只有 9.97 年
        w = A.ten_year_window(pts(start="2017-01-09", n=508))
        self.assertFalse(w["available"])
        self.assertIn("2027-01", w["reason"])

    def test_build_risk_structure_and_labels(self):
        series = {"gspc": pts(n=300), "fx_usd": pts(n=300), "tw00679b": pts(start="2017-01-09", n=508)}
        by_id = {"gspc": {"name": "S&P", "assetClass": "index", "currency": "USD"},
                 "fx_usd": {"name": "USD/TWD", "assetClass": "fx", "currency": "TWD"},
                 "tw00679b": {"name": "00679B", "assetClass": "bond_etf", "currency": "TWD"}}
        risk, problems = A.build_risk(series, by_id, NOW)
        self.assertEqual(problems, [])
        g = risk["assets"]["gspc"]
        self.assertEqual(g["dataLabel"], "單一來源")
        self.assertEqual(risk["assets"]["fx_usd"]["dataLabel"], "有對照")
        self.assertIn("volatility", g)
        self.assertIsNotNone(g["volatility"]["1y"]["pct"])
        self.assertIsNotNone(g["volatility"]["5y"]["pct"])
        self.assertEqual(g["volatility"]["1y"]["label"], "單一來源")
        self.assertEqual(g["lastBar"], series["gspc"][-1]["d"])
        self.assertTrue(g["lastWeek"].startswith("20"))
        self.assertFalse(risk["assets"]["tw00679b"]["tenYearWindow"]["available"])
        self.assertIn("2027-01", risk["assets"]["tw00679b"]["tenYearWindow"]["reason"])
        self.assertEqual(risk["correlation"]["matrix"]["gspc"]["gspc"]["r"], 1.0)
        self.assertIn("labels", risk)


# --------------------------------------------------------------------------
# 5. 拆解
# --------------------------------------------------------------------------

class TestDecompose(unittest.TestCase):
    def test_troy_ounce_conversion(self):
        self.assertAlmostEqual(A.implied_gold_twd_per_gram(3000.0, 32.0), 3000.0 * 32.0 / 31.1035, places=6)
        self.assertEqual(A.GRAMS_PER_TROY_OUNCE, 31.1035)

    def test_gold_same_day_and_previous_day(self):
        gold = [{"d": "2026-09-22", "buy": 4300.0, "sell": 4350.0, "c": 4350.0},
                {"d": "2026-09-23", "buy": 4310.0, "sell": 4360.0, "c": 4360.0}]
        gc = [{"d": "2026-09-21", "c": 4000.0}, {"d": "2026-09-22", "c": 4100.0},
              {"d": "2026-09-23", "c": 4200.0, "provisional": True}]                  # 進行中的不算
        fx = [{"d": "2026-09-22", "spotBuy": 31.4, "spotSell": 31.6, "c": 31.6},
              {"d": "2026-09-23", "spotBuy": 31.5, "spotSell": 31.7, "c": 31.7}]
        out = A.decompose_gold(gold, gc, fx)
        rows = {r["d"]: r for r in out["daily"]}
        r22 = rows["2026-09-22"]
        imp = 4100.0 * 31.5 / 31.1035
        self.assertAlmostEqual(r22["impliedSameDay"], round(imp, 2), places=2)
        self.assertAlmostEqual(r22["residualSameDayPct"], round((4350.0 / imp - 1) * 100, 3), places=3)
        self.assertEqual(r22["gcPrevDate"], "2026-09-21")
        r23 = rows["2026-09-23"]
        self.assertNotIn("impliedSameDay", r23)                                        # 當天的 GC 是進行中，沒有
        self.assertEqual(r23["gcPrevDate"], "2026-09-22")
        self.assertEqual(out["label"], "估算")
        self.assertEqual(out["gramsPerTroyOunce"], 31.1035)

    def test_gold_without_matching_fx_is_skipped(self):
        out = A.decompose_gold([{"d": "2026-09-22", "sell": 4350.0, "buy": 4300.0}], [{"d": "2026-09-22", "c": 4100.0}], [])
        self.assertEqual(out["daily"], [])
        self.assertIn("資料不足", out["summarySameDay"]["reason"])

    def test_live_row_uses_latest_json(self):
        latest = {"assets": {
            "gold_twd": {"status": "ok", "sell": 4400.0, "date": "2026-09-24", "quoteTime": "15:05"},
            "gold_intl": {"status": "ok", "price": 4150.0, "date": "2026-09-24", "fetchedAt": "x"},
            "fx_usd": {"status": "ok", "spotBuy": 31.5, "spotSell": 31.7, "date": "2026-09-23"}}}
        row = A.live_gold_row(latest)
        self.assertAlmostEqual(row["implied"], round(4150.0 * 31.6 / 31.1035, 2), places=2)
        self.assertEqual(row["fxDate"], "2026-09-23")                                   # 匯率是前一天的，照實寫
        self.assertEqual(row["label"], "估算")
        latest["assets"]["fx_usd"]["status"] = "error"
        self.assertIn("reason", A.live_gold_row(latest))

    def test_00646_monthly_decomposition(self):
        # 三個序列各 3 個月：00646 漲 10%、S&P 漲 5%、匯率漲 2%  → combined = 1.05*1.02−1 = 7.1%，殘差 2.9%
        def s(vals):
            return [{"d": d, "c": c} for d, c in vals]
        p646 = s([("2026-06-29", 100.0), ("2026-07-27", 110.0), ("2026-08-31", 110.0)])
        pg = s([("2026-06-29", 1000.0), ("2026-07-27", 1050.0), ("2026-08-31", 1050.0)])
        pf = s([("2026-06-26", 30.0), ("2026-07-31", 30.6), ("2026-08-28", 30.6)])
        out = A.decompose_00646(p646, pg, pf, current_month="2026-09")
        row = out["monthly"][0]
        self.assertEqual(row["month"], "2026-07")
        self.assertAlmostEqual(row["combinedPct"], 7.1, places=3)
        self.assertAlmostEqual(row["residualPct"], 2.9, places=3)
        self.assertEqual(out["monthly"][1]["residualPct"], 0.0)
        self.assertEqual(out["summaryAll"]["months"], 2)
        self.assertEqual(out["label"], "估算")

    def test_current_month_is_excluded_from_00646(self):
        def s(vals):
            return [{"d": d, "c": c} for d, c in vals]
        p = s([("2026-06-29", 100.0), ("2026-07-27", 110.0), ("2026-08-31", 120.0), ("2026-09-14", 130.0)])
        out = A.decompose_00646(p, p, p, current_month="2026-09")
        self.assertEqual([r["month"] for r in out["monthly"]], ["2026-07", "2026-08"])
        out2 = A.decompose_00646(p, p, p)                                   # 沒給當月就全算（測試用）
        self.assertEqual([r["month"] for r in out2["monthly"]], ["2026-07", "2026-08", "2026-09"])

    def test_month_end_uses_last_bar_in_month(self):
        m = A.month_end_closes([{"d": "2026-08-03", "c": 1}, {"d": "2026-08-24", "c": 2}, {"d": "2026-08-31", "c": 3}])
        self.assertEqual(m["2026-08"], ("2026-08-31", 3))


# --------------------------------------------------------------------------
# 6. 白名單、7. status／結束碼／dry-run
# --------------------------------------------------------------------------

class TestPolicy(unittest.TestCase):
    def test_bank_of_taiwan_is_refused_before_any_connection(self):
        f = A.PolicedFetcher(verbose=False)
        with self.assertRaises(net_policy.HostNotAllowed):
            f.get("https://rate.bot.com.tw/xrt")
        with self.assertRaises(net_policy.HostNotAllowed):
            f.get("https://example.com/x.json")
        self.assertEqual(f.count, 0)

    def test_bank_of_taiwan_is_refused_even_if_added_to_the_allowlist(self):
        """第二道保險：就算哪天有人把台銀加進白名單，FORBIDDEN_HOST_PARTS 照樣擋。對照組：把那一條清空 → 這一條紅。"""
        orig = net_policy.ALLOWED_HOSTS
        net_policy.ALLOWED_HOSTS = orig + ("rate.bot.com.tw",)
        try:
            with self.assertRaises(net_policy.HostNotAllowed):
                net_policy.assert_host_allowed("https://rate.bot.com.tw/xrt")
            with self.assertRaises(net_policy.HostNotAllowed):
                A.PolicedFetcher(verbose=False).get("https://rate.bot.com.tw/gold")
        finally:
            net_policy.ALLOWED_HOSTS = orig

    def test_response_hook_refuses_redirect_to_bank_of_taiwan(self):
        class R(object):
            url = "https://rate.bot.com.tw/gold"
            history = []
        with self.assertRaises(net_policy.HostNotAllowed):
            net_policy.response_hook(R())

    def test_sanitize_removes_paths(self):
        s = A.sanitize("讀不到 %s\\data\\x.json 與 C:\\Users\\someone\\secret.txt" % A.ROOT)
        self.assertNotIn(A.ROOT, s)
        self.assertNotIn("someone", s)


class World(object):
    """暫存目錄裡的一個假倉庫。patch=True：把 analyze.py 的路徑常數指過去（跑完指回來）；
    patch=False：常數不動，測試自己把 self.paths 傳給 run(paths=…)（A1-3 的 --out 就是這個用法）。"""

    def __init__(self, patch=True):
        self.tmp = tempfile.mkdtemp(prefix="iw-analyze-")
        self.data = os.path.join(self.tmp, "data")
        for sub in ("history", "history-long", "analysis"):
            os.makedirs(os.path.join(self.data, sub), exist_ok=True)
        self.paths = {"long": os.path.join(self.data, "history-long"), "analysis": os.path.join(self.data, "analysis"),
                      "assets": os.path.join(self.data, "assets.json"), "latest": os.path.join(self.data, "latest.json"),
                      "history": os.path.join(self.data, "history")}
        self.patched = patch
        if patch:
            self.saved = (A.LONG_DIR, A.ANALYSIS_DIR, A.ASSETS_FILE, A.LATEST_FILE, fd.HIST_DIR)
            A.LONG_DIR, A.ANALYSIS_DIR, A.ASSETS_FILE, A.LATEST_FILE, fd.HIST_DIR = (
                self.paths["long"], self.paths["analysis"], self.paths["assets"], self.paths["latest"], self.paths["history"])

    def close(self):
        if self.patched:
            A.LONG_DIR, A.ANALYSIS_DIR, A.ASSETS_FILE, A.LATEST_FILE, fd.HIST_DIR = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, obj):
        p = os.path.join(self.data, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)

    def read(self, rel):
        with io.open(os.path.join(self.data, rel), encoding="utf-8") as fh:
            return json.load(fh)


ASSETS = {"assets": [
    {"id": "gold_twd", "name": "黃金存摺", "group": "貴金屬", "assetClass": "gold_tw", "owner": "local", "type": "bot_gold", "symbol": "TWD", "currency": "TWD"},
    {"id": "gold_intl", "name": "國際金價", "group": "貴金屬", "assetClass": "commodity", "owner": "cloud", "type": "yahoo", "cadence": "full", "symbol": "GC=F", "currency": "USD"},
    {"id": "gspc", "name": "S&P 500", "group": "海外", "assetClass": "index", "owner": "cloud", "type": "yahoo", "symbol": "^GSPC", "currency": "USD"},
    {"id": "tw00646", "name": "00646", "group": "台股", "assetClass": "index_etf", "owner": "cloud", "type": "twse_stock", "symbol": "00646", "yahooSymbol": "00646.TW", "currency": "TWD"},
    {"id": "fx_usd", "name": "USD/TWD", "group": "匯率", "assetClass": "fx", "owner": "cloud", "type": "finmind_fx", "cadence": "full", "symbol": "USD", "currency": "TWD"},
]}


def fill_world(w, with_gold_intl_daily=True):
    w.write("assets.json", ASSETS)
    for aid in ("gold_intl", "gspc", "tw00646", "fx_usd", "sp500tr"):
        w.write("history-long/%s.json" % aid, {"id": aid, "points": pts(start="2019-01-07", n=400)})
    bar_days = [(A.parse_day("2026-09-01") + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 12)]
    w.write("history/gold_bar.json", {"points": [{"d": d, "g1000": 4441200.0, "g500": 2224353.0, "g250": 1114313.0, "g100": 447022.0,
                                                   "tael": 167822.0, "c": 4441.2, "dateSource": "quote"} for d in bar_days]})
    days = [(A.parse_day("2026-03-02") + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 200)]
    w.write("history/gold_twd.json", {"points": [{"d": d, "buy": 4300.0, "sell": 4350.0, "c": 4350.0, "dateSource": "quote"} for d in days]})
    if with_gold_intl_daily:
        w.write("history/gold_intl.json", {"points": [{"d": d, "c": 4000.0, "dateSource": "yahoo"} for d in days]})
    w.write("history/fx_usd.json", {"points": [{"d": d, "spotBuy": 31.4, "spotSell": 31.6, "c": 31.6, "dateSource": "finmind"} for d in days]})
    w.write("latest.json", {"assets": {
        "gold_twd": {"status": "ok", "sell": 4350.0, "date": days[-1]},
        "gold_intl": {"status": "ok", "price": 4000.0, "date": days[-1]},
        "fx_usd": {"status": "ok", "spotBuy": 31.4, "spotSell": 31.6, "date": days[-2]}}})


class TestRunOffline(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()

    def test_offline_run_produces_all_files_and_exit_zero(self):
        fill_world(self.w)
        with redirect_stdout(io.StringIO()):
            status, code = A.run("review", offline=True, now=NOW)
        self.assertEqual(code, 0)
        self.assertTrue(status["ok"])
        self.assertEqual(status["requests"], 0)
        st = self.w.read("analysis/status.json")
        self.assertEqual(st["errors"], [])
        risk = self.w.read("analysis/risk.json")
        self.assertIn("gspc", risk["assets"])
        self.assertEqual(risk["correlation"]["matrix"]["gspc"]["tw00646"]["r"], 1.0)     # 合成序列一模一樣
        dec = self.w.read("analysis/decompose.json")
        self.assertTrue(dec["gold"]["daily"])
        self.assertEqual(dec["gold"]["live"]["fxDate"], dec["gold"]["daily"][-2]["d"])
        self.assertTrue(dec["tw00646"]["monthly"])
        self.assertTrue(any("融資成本" in n for n in dec["gold"]["notes"]))                 # 期貨基差的解釋
        for aid, e in status["longHistory"].items():
            self.assertIn("offline", e["action"])
        cost = self.w.read("analysis/cost.json")
        self.assertIn("data/analysis/cost.json", status["produced"])
        self.assertTrue(cost["trackingDifference"]["primary"]["available"])
        self.assertEqual(cost["trackingDifference"]["primary"]["benchmark"], "^SP500TR")
        w1 = cost["trackingDifference"]["primary"]["windows"]["1y"]
        self.assertIn("from", w1)
        self.assertIn("through", w1)
        self.assertEqual(cost["premium"]["tw00646"]["status"][:7], "offline")
        self.assertIn("reason", cost["premium"]["tw00646"]["estimated"])                      # 沒累積 → 不顯示中位數
        self.assertEqual(cost["goldSpread"]["summary"]["n"], 200)
        self.assertEqual(cost["barPremium"]["n"], 12)
        self.assertAlmostEqual(cost["barPremium"]["latest"]["rows"][0]["premiumPct"], round((4441.2 / 4350.0 - 1) * 100, 3), places=3)

    def test_missing_gold_intl_daily_is_exit_two_with_error_recorded(self):
        fill_world(self.w, with_gold_intl_daily=False)
        with redirect_stdout(io.StringIO()):
            status, code = A.run("review", offline=True, now=NOW)
        self.assertEqual(code, 2)
        self.assertFalse(status["ok"])
        self.assertTrue(any("gold_intl" in e for e in status["errors"]))
        st = self.w.read("analysis/status.json")
        self.assertFalse(st["ok"])
        self.assertTrue(os.path.exists(os.path.join(self.w.data, "analysis", "risk.json")))       # 風險照樣產出
        dec = self.w.read("analysis/decompose.json")
        self.assertIn("reason", dec["gold"])
        self.assertTrue(dec["tw00646"]["monthly"])

    def test_no_long_history_at_all_is_exit_two_not_a_crash(self):
        self.w.write("assets.json", ASSETS)
        with redirect_stdout(io.StringIO()):
            status, code = A.run("review", offline=True, now=NOW)
        self.assertEqual(code, 2)
        self.assertTrue(any("長歷史" in e for e in status["errors"]))
        self.assertTrue(os.path.exists(os.path.join(self.w.data, "analysis", "status.json")))

    def test_status_has_no_local_paths(self):
        fill_world(self.w, with_gold_intl_daily=False)
        with redirect_stdout(io.StringIO()):
            A.run("review", offline=True, now=NOW)
        with io.open(os.path.join(self.w.data, "analysis", "status.json"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn(self.w.tmp.replace("\\", "/"), text.replace("\\", "/"))
        self.assertNotIn(os.environ.get("USERNAME", "<none>"), text)

    def test_dry_run_writes_nothing_and_sends_nothing(self):
        self.w.write("assets.json", ASSETS)
        with redirect_stdout(io.StringIO()):
            status, code = A.run("review", dry_run=True, now=NOW)
        self.assertEqual(code, 0)
        self.assertEqual(status["requests"], 0)
        self.assertEqual(os.listdir(os.path.join(self.w.data, "analysis")), [])
        self.assertTrue(all("dry-run" in e["action"] for e in status["longHistory"].values()))
        self.assertTrue(any("full" in e["action"] or "整段" in e["action"] for e in status["longHistory"].values()))

    def test_full_backfill_and_incremental_fetch_start_on_a_monday(self):
        """對照組：period1 改回 0 → 這一條會紅（Yahoo 會把週對齊到星期四）。"""
        self.w.write("assets.json", ASSETS)
        stale = pts(start="2019-01-07", n=300)                                  # 最後一根很久以前 → incremental
        self.w.write("history-long/gspc.json", {"id": "gspc", "points": stale})
        with redirect_stdout(io.StringIO()):
            status, _ = A.run("review", dry_run=True, now=NOW)
        full = status["longHistory"]["tw00646"]["action"]
        self.assertIn("period1=345600", full)                                    # 1970-01-05 星期一
        inc = status["longHistory"]["gspc"]["action"]
        p1 = int(inc.split("period1=")[1].split("）")[0].split(";")[0].split("；")[0])
        self.assertEqual(A.parse_day(A.local_day(p1)).weekday(), 0, inc)         # 補抓的起點也是星期一
        self.assertEqual(A.YAHOO_PERIOD1_MONDAY, 345600)
        self.assertEqual(A.local_day(A.YAHOO_PERIOD1_MONDAY), "1970-01-05")

    def test_parse_reports_which_weekday_bars_start_on(self):
        _, info = A.parse_yahoo_weekly(weekly_body(n=60, start="2025-07-07"), NOW_EPOCH)      # 週一
        self.assertEqual(info["dominantWeekday"], "Mon")
        _, info = A.parse_yahoo_weekly(weekly_body(n=60, start="2025-07-10"), NOW_EPOCH)      # 週四
        self.assertEqual(info["dominantWeekday"], "Thu")

    def test_recent_long_history_is_not_refetched(self):
        self.w.write("assets.json", ASSETS)
        recent = pts(start=(NOW.date() - timedelta(days=7 * 9 + 2)).strftime("%Y-%m-%d"), n=10)      # 最後一根 2 天前
        self.w.write("history-long/gspc.json", {"id": "gspc", "points": recent})
        with redirect_stdout(io.StringIO()):
            status, _ = A.run("review", dry_run=True, now=NOW)
        self.assertIn("這週不必抓", status["longHistory"]["gspc"]["action"])


class TestCost(unittest.TestCase):
    """成本：追蹤差（含幣別換算）、折溢價（分母是淨值、預估／確定分開、確定回填前一交易日）、黃金價差、條塊溢價。"""

    def weekly(self, start_v, end_v, n=53, base="2025-09-15", fx=False):
        out = []
        for i in range(n):
            d = (A.parse_day(base) + timedelta(days=7 * i)).strftime("%Y-%m-%d")
            v = start_v + (end_v - start_v) * i / (n - 1.0)
            out.append({"d": d, "spotBuy": v - 0.1, "spotSell": v + 0.1, "c": v + 0.1} if fx else {"d": d, "c": v})
        return out

    def test_tracking_difference_math_with_fx_conversion(self):
        """對照組：把匯率換算拿掉（基準只用美元報酬）→ 這一條會紅。"""
        r = A.tracking_difference(self.weekly(100, 110), self.weekly(1000, 1050), self.weekly(30.0, 30.6, fx=True), 52)
        self.assertEqual(r["weeks"], 52)
        self.assertAlmostEqual(r["r646Pct"], 10.0, places=3)
        self.assertAlmostEqual(r["rFxPct"], 2.0, places=3)                                    # 用的是中價
        self.assertAlmostEqual(r["rBenchTwdPct"], 7.1, places=3)                              # 1.05 × 1.02 − 1
        self.assertAlmostEqual(r["diffPct"], 2.9, places=3)
        self.assertAlmostEqual(r["annualizedDiffPct"], (1.10 / 1.071 - 1) * 100, places=3)
        self.assertEqual(r["from"], "2025-09-15")
        self.assertEqual(r["through"], "2026-09-14")

    def test_tracking_difference_walks_back_up_to_three_weeks_for_the_start(self):
        p646 = self.weekly(100, 110)
        pb = self.weekly(1000, 1050)
        pfx = self.weekly(30.0, 30.6, fx=True)
        del pb[0]                                                                             # 基準少了起點那一週
        r = A.tracking_difference(p646, pb, pfx, 51)
        self.assertNotIn("reason", r)
        self.assertEqual(r["from"], "2025-09-22")

    def test_tracking_difference_insufficient(self):
        r = A.tracking_difference(self.weekly(100, 110, n=10), self.weekly(1000, 1050, n=10), self.weekly(30.0, 30.6, n=10, fx=True), 52)
        self.assertIn("資料不足", r["reason"])

    def test_build_tracking_marks_missing_benchmark(self):
        series = {"tw00646": self.weekly(100, 110), "gspc": self.weekly(1000, 1050), "fx_usd": self.weekly(30.0, 30.6, fx=True)}
        t = A.build_tracking(series)
        self.assertFalse(t["primary"]["available"])
        self.assertIn("sp500tr", t["primary"]["reason"])
        self.assertTrue(t["reference"]["available"])
        self.assertEqual(t["reference"]["label"], "估算")

    def test_parse_all_etf_and_backfill_official_nav_to_previous_trading_day(self):
        """對照組：預估／確定的標籤對調、或官方淨值填到今天而不是前一交易日 → 這一條會紅。"""
        j = {"a1": [{"msgArray": [
            {"a": "00646", "c": 1234, "d": 10, "e": 76.8, "f": 76.42, "g": 0.5, "h": "76.5400", "i": "20260924", "j": "17:00:00"},
            {"a": "00679B", "c": 1, "d": 0, "e": 25.64, "f": 25.6565, "g": -0.06, "h": "未結出", "i": "20260924", "j": "17:00:00"}]}]}
        rec = A.parse_all_etf(j, {"00646", "00679B"})
        self.assertEqual(rec["00646"]["d"], "2026-09-24")
        self.assertEqual(rec["00646"]["prevOfficialNav"], 76.54)
        self.assertIsNone(rec["00679B"]["prevOfficialNav"])                                  # 未結出 → 沒有
        closes = [{"d": "2026-09-22", "c": 76.3, "dateSource": "official"}, {"d": "2026-09-23", "c": 76.6, "dateSource": "official"},
                  {"d": "2026-09-24", "c": 76.8, "dateSource": "intraday", "provisional": True}]
        rows = A.update_nav_rows([], rec["00646"], closes)
        by = dict((r["d"], r) for r in rows)
        self.assertAlmostEqual(by["2026-09-24"]["estPremiumPct"], round((76.8 / 76.42 - 1) * 100, 3), places=3)
        self.assertEqual(by["2026-09-24"]["estTag"], "預估")
        self.assertEqual(by["2026-09-23"]["officialNav"], 76.54)                             # 回填到前一個交易日，不是今天
        self.assertEqual(by["2026-09-23"]["officialClose"], 76.6)
        self.assertAlmostEqual(by["2026-09-23"]["officialPremiumPct"], round((76.6 / 76.54 - 1) * 100, 3), places=3)
        self.assertEqual(by["2026-09-23"]["officialTag"], "確定")
        self.assertNotIn("officialNav", by["2026-09-24"])
        rec2 = dict(rec["00646"], d="2026-09-25", prevOfficialNav=76.7)
        closes2 = closes[:-1] + [{"d": "2026-09-24", "c": 76.8, "dateSource": "official"}]
        rows2 = A.update_nav_rows(rows, rec2, closes2)
        by2 = dict((r["d"], r) for r in rows2)
        self.assertEqual(by2["2026-09-24"]["officialNav"], 76.7)                              # 隔天回填昨天
        self.assertEqual(by2["2026-09-24"]["estTag"], "預估")                                 # 昨天的預估還在
        self.assertEqual(len(rows2), 3)                                                    # 09-23、09-24、09-25
        latest = A.latest_premium_rows(rows2)
        self.assertEqual([x["tag"] for x in latest], ["預估", "確定"])
        self.assertEqual(latest[1]["nav"], 76.7)

    def test_premium_denominator_is_nav(self):
        """對照組：分母改成價格 → 這一條會紅。"""
        self.assertEqual(A.premium_pct(101.0, 100.0), 1.0)
        self.assertEqual(A.premium_pct(99.0, 100.0), -1.0)
        self.assertEqual(A.premium_pct(103.0, 100.0), 3.0)                                    # 用價格當分母會是 2.913
        self.assertIsNone(A.premium_pct(None, 100.0))

    def test_premium_summary_needs_twenty_days(self):
        rows = [{"d": "2026-09-%02d" % (i + 1), "estPremiumPct": 0.5} for i in range(19)]
        self.assertIn("reason", A.premium_summary(rows, "estPremiumPct"))
        rows.append({"d": "2026-09-20", "estPremiumPct": 0.7})
        s = A.premium_summary(rows, "estPremiumPct")
        self.assertEqual(s["n"], 20)
        self.assertEqual(s["medianPct"], 0.5)

    def test_gold_spread_math(self):
        out = A.gold_spread([{"d": "2026-09-24", "buy": 4330.0, "sell": 4382.0}])
        self.assertAlmostEqual(out["latest"]["spreadPct"], round(52.0 / 4356.0 * 100, 3), places=3)
        self.assertEqual(out["label"], "單一來源")

    def test_bar_premium_math_including_tael(self):
        """對照組：台兩的公克數改壞 → 這一條會紅。"""
        out = A.bar_premium([{"d": "2026-09-24", "g1000": 4441200.0, "tael": 167822.0}], [{"d": "2026-09-24", "sell": 4382.0}])
        rows = dict((r["spec"], r) for r in out["latest"]["rows"])
        self.assertAlmostEqual(rows["1 公斤"]["premiumPct"], round((4441.2 / 4382.0 - 1) * 100, 3), places=3)
        self.assertAlmostEqual(rows["金鑽 1 台兩"]["perGram"], round(167822.0 / 37.5, 2), places=2)
        self.assertAlmostEqual(rows["金鑽 1 台兩"]["premiumPct"], round((167822.0 / 37.5 / 4382.0 - 1) * 100, 3), places=3)
        self.assertEqual(out["n"], 1)

    def test_tpex_dates_get_a_year(self):
        today = A.parse_day("2026-09-25")
        self.assertEqual(A.tpex_dates_with_year(["08/11", "09/25", "12/30", "x"], today), ["2026-08-11", "2026-09-25", "2025-12-30", None])

    def test_all_etf_fetch_uses_production_headers_and_records_status(self):
        seen = {}

        class F(object):
            count = 1
            def get(self, url, **kw):
                seen["url"], seen["headers"] = url, kw.get("headers")
                return {"a1": [{"msgArray": [{"a": "00646", "e": 76.8, "f": 76.42, "h": "76.54", "i": "20260924", "j": "17:00"},
                                             {"a": "00679B", "e": 25.64, "f": 25.6565, "h": "25.5407", "i": "20260924", "j": "17:00"}]}]}
        w = World()
        try:
            w.write("assets.json", ASSETS)
            problems = []
            out = A.build_premium(F(), False, w.paths, problems, NOW)
            self.assertEqual(seen["headers"]["Referer"], "https://mis.twse.com.tw/stock/index.jsp")
            self.assertEqual(out["tw00646"]["status"][:2], "接了")
            self.assertTrue(os.path.exists(os.path.join(w.data, "analysis", "nav", "tw00646.json")))
            self.assertEqual(out["tw00646"]["latest"][0]["tag"], "預估")
            self.assertEqual(problems, [])
        finally:
            w.close()

    def test_blocked_all_etf_is_reported_not_faked(self):
        class F(object):
            count = 1
            session = None
            def get(self, url, **kw):
                raise fd.FetchError("HTTP 502")
            def _wait(self, host, delay):
                pass
        w = World()
        try:
            w.write("assets.json", ASSETS)
            problems = []
            orig = A.fetch_tpex_30d
            A.fetch_tpex_30d = lambda f, today: {"title": "櫃買 30 日（僅 30 日）", "n": 30, "medianPct": -0.4}
            try:
                out = A.build_premium(F(), False, w.paths, problems, NOW)
            finally:
                A.fetch_tpex_30d = orig
            self.assertEqual(out["tw00646"]["status"], "未接（來源在雲端被擋）")
            self.assertEqual(out["tw00679b"]["status"], "僅 30 日")
            self.assertTrue(problems)
        finally:
            w.close()


class TestPathsDict(unittest.TestCase):
    """A1-3 預留：run(paths=…) 把所有讀寫指到別的地方，模組常數不動（--out <倉庫外> 就是這樣用）。"""

    def test_run_with_explicit_paths_writes_only_there(self):
        w = World(patch=False)
        try:
            fill_world(w)
            with redirect_stdout(io.StringIO()):
                status, code = A.run("review", offline=True, now=NOW, paths=w.paths)
            self.assertEqual(code, 0)
            for name in ("status.json", "risk.json", "decompose.json", "cost.json"):
                self.assertTrue(os.path.exists(os.path.join(w.data, "analysis", name)), name)
            self.assertEqual(A.ANALYSIS_DIR, os.path.join(A.DATA_DIR, "analysis"))           # 模組常數沒被動
            risk = w.read("analysis/risk.json")
            self.assertEqual(risk["generatedAt"], A.iso(NOW))
        finally:
            w.close()


class FakeFetcher(object):
    """給 adhoc 用的假連線：回固定的 Yahoo 回應或丟 FetchError；只數請求。"""

    def __init__(self, body=None, error=None):
        self.body, self.error, self.count, self.urls = body, error, 0, []

    def get(self, url, **kw):
        self.count += 1
        self.urls.append(url)
        if self.error:
            raise fd.FetchError(self.error)
        return self.body


def adhoc_body(n=300, currency="GBp", base=8000.0, name="Fake Tracker"):
    start = monday((NOW - timedelta(weeks=n + 1)).strftime("%Y-%m-%d"))
    return weekly_body(n=n, start=start, closes=[base + i for i in range(n)], currency=currency, name=name)


class TestAdhoc(unittest.TestCase):
    """A1-3 試算：代號與結果只寫到 --out（倉庫外）；便士 ÷100；查無就明確失敗；index.json；status 也在 out。
    fixture 代號一律 FAKE 開頭。"""

    def setUp(self):
        self.w = World(patch=False)
        fill_world(self.w)
        self.out = tempfile.mkdtemp(prefix="iw-adhoc-out-")

    def tearDown(self):
        A.WRITE_ROOTS = None
        self.w.close()
        shutil.rmtree(self.out, ignore_errors=True)

    def run_adhoc(self, symbol="FAKE.L", currency="GBp", n=300, error=None, cls="index_etf", er="0.07", out=None, now=NOW):
        f = FakeFetcher(body=adhoc_body(n=n, currency=currency), error=error)
        with redirect_stdout(io.StringIO()):
            status, code = A.adhoc_run(symbol, cls, er, out or self.out, now=now, fetcher=f, paths=self.w.paths)
        return status, code, f

    def result(self, symbol="FAKE.L", day=None):
        p = os.path.join(self.out, symbol, (day or NOW.strftime("%Y-%m-%d")) + ".json")
        with io.open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def test_pence_divided_files_written_index_updated(self):
        """對照組：便士 ÷100 拿掉 → 這一條會紅。"""
        status, code, f = self.run_adhoc()
        self.assertEqual(code, 0, status)
        self.assertEqual(f.count, 1)
        self.assertIn("FAKE.L", f.urls[0])
        self.assertIn("period1=345600", f.urls[0])
        day = NOW.strftime("%Y-%m-%d")
        r = self.result()
        self.assertEqual(r["currency"], "GBP")
        self.assertIn("便士", r["priceUnitNote"])
        self.assertAlmostEqual(r["risk"]["currentDrawdown"]["lastValue"], 82.99, places=4)          # 8299 便士 → 82.99 英鎊
        self.assertEqual(r["expenseRatio"], {"pct": 0.07, "label": "使用者輸入"})
        self.assertEqual(r["runDate"], day)
        self.assertEqual(r["dataThrough"], r["risk"]["currentDrawdown"]["dataThrough"])
        self.assertIsNotNone(r["risk"]["volatility"]["1y"]["pct"])
        self.assertLessEqual(len(r["correlation"]["top"]), 3)
        rs = [x["r"] for x in r["correlation"]["all"]]
        self.assertEqual(rs, sorted(rs, reverse=True))
        self.assertIn("資料不足", r["valuation"]["reason"])
        self.assertIn("資料不足", r["premium"]["reason"])
        self.assertIn("稅務", r["notes"][0])
        self.assertEqual(r["name"], "Fake Tracker")
        with io.open(os.path.join(self.out, "index.json"), encoding="utf-8") as fh:
            idx = json.load(fh)
        self.assertEqual(idx["symbols"]["FAKE.L"]["latest"], "FAKE.L/%s.json" % day)
        self.assertEqual(idx["symbols"]["FAKE.L"]["currency"], "GBP")
        self.assertEqual(idx["symbols"]["FAKE.L"]["dataThrough"], r["dataThrough"])
        with io.open(os.path.join(self.out, "status.json"), encoding="utf-8") as fh:
            st = json.load(fh)
        self.assertTrue(st["ok"])
        self.assertEqual(st["requests"], 1)
        self.assertEqual(st["mode"], "adhoc")

    def test_usd_and_pounds_are_not_divided(self):
        """對照組：把 GBP（英鎊）也當成便士 → 這一條會紅。"""
        for cur in ("USD", "GBP"):
            status, code, _ = self.run_adhoc(symbol="FAKE2", currency=cur)
            self.assertEqual(code, 0, status)
            r = self.result("FAKE2")
            self.assertEqual(r["currency"], cur)
            self.assertIsNone(r["priceUnitNote"])
            self.assertAlmostEqual(r["risk"]["currentDrawdown"]["lastValue"], 8299.0, places=4)

    def test_out_inside_repo_is_refused_before_any_network(self):
        """對照組：拿掉「--out 不可以在倉庫裡」的檢查 → 這一條會紅。"""
        inside = os.path.join(A.ROOT, "data", "adhoc-should-never-exist")
        status, code, f = self.run_adhoc(out=inside)
        self.assertEqual(code, 2)
        self.assertEqual(f.count, 0)                                                         # 連網之前就擋
        self.assertFalse(os.path.exists(inside))
        self.assertTrue(any("--out" in e for e in status["errors"]), status["errors"])
        self.assertIsNone(A.WRITE_ROOTS)

    def test_write_guard_refuses_anything_outside_out(self):
        """對照組：讓寫檔函式略過允許清單 → 這一條會紅。"""
        A.WRITE_ROOTS = [self.out]
        try:
            with self.assertRaises(A.WriteRefused):
                A.write_json(os.path.join(A.ANALYSIS_DIR, "adhoc-guard-test.json"), {"x": 1})
            with self.assertRaises(A.WriteRefused):
                A.save_long("adhocguard", {"id": "adhocguard"}, [{"d": "2026-09-14", "c": 1.0}], paths=A.default_paths())
            with self.assertRaises(A.WriteRefused):
                A.save_nav("adhocguard", "X", [], paths=A.default_paths())
            A.write_json(os.path.join(self.out, "ok.json"), {"x": 1})                        # out 底下可以
            self.assertFalse(os.path.exists(os.path.join(A.ANALYSIS_DIR, "adhoc-guard-test.json")))
            self.assertFalse(os.path.exists(os.path.join(A.LONG_DIR, "adhocguard.json")))
        finally:
            A.WRITE_ROOTS = None

    def test_unknown_symbol_fails_loudly_without_any_result_file(self):
        """對照組：查無代號也產一個空檔 → 這一條會紅。"""
        status, code, f = self.run_adhoc(symbol="FAKE404", error="HTTP 404")
        self.assertEqual(code, 2)
        self.assertEqual(f.count, 1)
        self.assertTrue(any("查無" in e for e in status["errors"]), status["errors"])
        self.assertEqual(sorted(os.listdir(self.out)), ["status.json"])                       # 只有狀態，沒有結果、沒有 index

    def test_young_listing_marks_correlation_and_5y_vol_insufficient(self):
        """對照組：上市未滿 3 年也硬算相關 → 這一條會紅。"""
        status, code, _ = self.run_adhoc(symbol="FAKE3", n=60, currency="USD")
        self.assertEqual(code, 0, status)
        r = self.result("FAKE3")
        self.assertIn("未滿 3 年", r["correlation"]["reason"])
        self.assertEqual(r["correlation"]["top"], [])
        self.assertIsNone(r["risk"]["volatility"]["5y"]["pct"])
        self.assertIn("資料不足", r["risk"]["volatility"]["5y"]["reason"])
        self.assertIsNotNone(r["risk"]["volatility"]["1y"]["pct"])

    def test_same_day_rerun_overwrites_the_same_file(self):
        """對照組：檔名多帶時間、同一天產兩份 → 這一條會紅。"""
        self.run_adhoc()
        self.run_adhoc()
        self.assertEqual(os.listdir(os.path.join(self.out, "FAKE.L")), [NOW.strftime("%Y-%m-%d") + ".json"])
        with io.open(os.path.join(self.out, "index.json"), encoding="utf-8") as fh:
            self.assertEqual(sorted(json.load(fh)["symbols"]), ["FAKE.L"])

    def test_status_goes_to_out_and_repo_tree_is_untouched(self):
        """對照組：adhoc 的 status.json 寫回 data/analysis → 這一條會紅（第二層保險會擋、結果檔也不會產）。"""
        repo_status = os.path.join(A.ANALYSIS_DIR, "status.json")

        def snap():
            if not os.path.exists(repo_status):
                return None
            with io.open(repo_status, encoding="utf-8") as fh:
                return (fh.read(), os.path.getmtime(repo_status))

        def porcelain():
            try:
                return subprocess.run(["git", "status", "--porcelain"], cwd=A.ROOT, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, timeout=60).stdout
            except Exception:                             # noqa: B902
                return None
        before, git_before = snap(), porcelain()
        status, code, _ = self.run_adhoc()
        self.assertEqual(code, 0, status)
        self.run_adhoc(symbol="FAKE404", error="HTTP 404")
        self.assertTrue(os.path.exists(os.path.join(self.out, "status.json")))
        self.assertEqual(snap(), before)                                                     # 倉庫的 status.json 一個位元組沒動
        if git_before is not None:
            self.assertEqual(porcelain(), git_before)                                        # 跑前跑後 git status 一樣

    def test_same_symbol_as_a_public_asset_is_skipped_in_correlation(self):
        assets = self.w.read("assets.json")
        assets["assets"].append({"id": "fakepub", "name": "假的公開標的", "type": "yahoo", "symbol": "FAKE.PUB", "owner": "cloud",
                                 "assetClass": "index_etf", "currency": "USD", "enabled": True})
        self.w.write("assets.json", assets)
        self.w.write("history-long/fakepub.json", {"id": "fakepub", "points": pts(start="2019-01-07", n=400)})
        status, code, _ = self.run_adhoc(symbol="FAKE.PUB", currency="USD")
        self.assertEqual(code, 0, status)
        r = self.result("FAKE.PUB")
        self.assertEqual(r["correlation"]["skippedSameSymbol"], ["fakepub"])
        self.assertFalse(any(x["id"] == "fakepub" for x in r["correlation"]["all"]))

    def test_bad_class_or_expense_ratio_or_symbol_is_refused_before_network(self):
        for kw in (dict(cls="gold_tw"), dict(cls="other"), dict(er="abc"), dict(er="55"), dict(symbol="../x"), dict(symbol=""), dict(symbol="..")):
            status, code, f = self.run_adhoc(**kw)
            self.assertEqual(code, 2, kw)
            self.assertEqual(f.count, 0, kw)
            self.assertTrue(status["errors"], kw)

    def test_cli_interface_is_pinned_and_matches_the_private_workflow_example(self):
        """私人倉庫的 workflow 依賴這些參數名；改了要同時改範本並在 CHANGELOG 標「私人 workflow 需更新」。"""
        opts = set()
        for a in A.build_parser()._actions:
            opts.update(a.option_strings)
        for flag in ("--adhoc", "--asset-class", "--expense-ratio", "--out", "--now", "--slot"):
            self.assertIn(flag, opts)
        self.assertEqual(A.ADHOC_CLASSES, ("stock", "index_etf", "bond_etf", "commodity", "crypto", "fx", "index"))
        with io.open(os.path.join(ROOT, "docs", "adhoc-workflow.example.yml"), encoding="utf-8") as fh:
            text = fh.read()
        for flag in ("--adhoc", "--asset-class", "--expense-ratio", "--out"):
            self.assertIn(flag, text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("contents: write", text)
        self.assertIn("repository: davidjjx/invest-watch", text)
        self.assertIn("SYMBOL: ${{ inputs.symbol }}", text)                                  # 代號經 env 傳、不直接拼進指令
        for cls in A.ADHOC_CLASSES:
            self.assertIn("- " + cls + "\n", text)                                            # 下拉七類跟程式一致
        self.assertNotIn("gold_tw", text)

    def test_normal_mode_still_requires_slot_and_adhoc_requires_class(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(A.main(["--offline"]), 2)
            self.assertEqual(A.main(["--adhoc", "FAKE.L", "--out", self.out]), 2)             # 沒給類別，連網之前就結束
        self.assertFalse(os.path.exists(os.path.join(self.out, "status.json")))


class TestMain(unittest.TestCase):
    def test_wrong_slot_does_nothing_and_returns_two(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(A.main(["--slot", "light"]), 2)
        self.assertIn("review", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
