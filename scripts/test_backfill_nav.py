#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_backfill_nav.py — 分析系列 A1-4：00646 官方淨值一次回補（scripts/backfill_nav.py），
以及 scripts/analyze.py 每天把回補的列只增不改地併進去、近一年那一組。

全部離線：證交所 e添富與 Yahoo 的回應都是假的（數字一看就是假的），不連網。
跑法：py -3.12 -m unittest discover -s scripts -p "test_backfill_nav.py"
"""
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import analyze as A           # noqa: E402
import backfill_nav as B      # noqa: E402
import net_policy             # noqa: E402

TPE = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=TPE)


def chart_json(navs, prem=None):
    """假的 e添富回應：navs＝{YYYY-MM-DD: 淨值}。"""
    return json.dumps({"netPrice": [{"date": d.replace("-", "/"), "count": v} for d, v in sorted(navs.items())],
                       "atmps": [{"date": d.replace("-", "/"), "count": v} for d, v in sorted((prem or {}).items())]})


def yahoo_json(closes):
    """假的 Yahoo chart 回應：closes＝{YYYY-MM-DD: 收盤}；時間戳是台北當天 09:00。"""
    ds = sorted(closes)
    ts = [int(datetime.strptime(d, "%Y-%m-%d").replace(hour=9, tzinfo=TPE).timestamp()) for d in ds]
    return json.dumps({"chart": {"error": None, "result": [{
        "meta": {"gmtoffset": 28800, "currency": "TWD", "regularMarketPrice": closes[ds[-1]] if ds else None,
                 "currentTradingPeriod": {"regular": {"start": ts[-1] if ts else 0, "end": (ts[-1] + 16200) if ts else 0}}},
        "timestamp": ts, "indicators": {"quote": [{"close": [closes[d] for d in ds]}]}}]}})


class Resp(object):
    def __init__(self, text, status=200, url=None):
        self.text, self.status_code, self.url = text, status, url


class FakeNet(object):
    """假的連線：照 URL 回假回應，記下每一個請求（方法、網址、標頭、表單）。"""

    def __init__(self, page="<html>00646 ETF e添富</html>", chart=None, yahoo=None, page_status=200):
        self.page, self.chart, self.yahoo, self.page_status = page, chart, yahoo, page_status
        self.sent = []

    def __call__(self, method, url, headers, data):
        self.sent.append({"method": method, "url": url, "headers": headers, "data": data})
        if url == B.PAGE_URL:
            return Resp(self.page, self.page_status, url)
        if url == B.CHART_URL:
            return Resp(self.chart, 200, url)
        return Resp(self.yahoo, 200, url)


def days(start, n):
    """從 start 起 n 個平日（假的交易日）。"""
    d, out = datetime.strptime(start, "%Y-%m-%d"), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


class Sandbox(object):
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-backfill-nav-")
        data = os.path.join(self.tmp, "data")
        for sub in ("history", "history-long", "analysis/nav"):
            os.makedirs(os.path.join(data, sub), exist_ok=True)
        self.paths = {"long": os.path.join(data, "history-long"), "analysis": os.path.join(data, "analysis"),
                      "assets": os.path.join(data, "assets.json"), "latest": os.path.join(data, "latest.json"),
                      "history": os.path.join(data, "history")}
        self.outside = os.path.join(self.tmp, "outside")

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_history(self, closes):
        with io.open(os.path.join(self.paths["history"], "tw00646.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": "tw00646", "points": [{"d": d, "c": c, "dateSource": "yahoo"} for d, c in sorted(closes.items())]}, fh)

    def write_nav(self, rows):
        A.save_nav("tw00646", "00646", rows, self.paths)

    def nav_bytes(self):
        with io.open(A.nav_path("tw00646", self.paths), "rb") as fh:
            return fh.read()

    def backfill(self):
        with io.open(A.nav_backfill_path("tw00646", self.paths), encoding="utf-8") as fh:
            return json.load(fh)


EXISTING = [  # 既有的每天累積：一列有確定、一列只有預估（日期都是假的）
    {"d": "2026-01-05", "price": 20.5, "estNav": 20.4, "estPremiumPct": 0.49, "estTag": "預估", "estLabel": "估算",
     "officialNav": 20.3, "officialClose": 20.5, "officialPremiumPct": 0.985, "officialTag": "確定", "officialLabel": "單一來源", "officialNavSeenOn": "2026-01-06"},
    {"d": "2026-01-06", "price": 20.6, "estNav": 20.5, "estPremiumPct": 0.488, "estTag": "預估", "estLabel": "估算"},
]


class TestSourceAndNetwork(unittest.TestCase):
    def test_only_two_hosts_and_never_the_bank(self):
        """對照組：白名單多一個主機、或台銀的禁止判斷拿掉 → 這一條會紅。"""
        self.assertEqual(B.HOSTS, ("www.twse.com.tw", "query1.finance.yahoo.com"))
        self.assertEqual(B.check_host(B.PAGE_URL), "www.twse.com.tw")
        self.assertEqual(B.check_host(B.YAHOO_URL % ("00646.TW", 1, 2)), "query1.finance.yahoo.com")
        for bad in ("https://mis.twse.com.tw/stock/data/all_etf.txt", "https://api.finmindtrade.com/x", "https://rate.bot.com.tw/gold",
                    "https://evil.example/x", "https://user@www.twse.com.tw/x"):
            with self.assertRaises(net_policy.HostNotAllowed, msg=bad):
                B.check_host(bad)

    def test_page_first_then_post_in_the_same_session_then_yahoo(self):
        """GET 頁面 → 同一個 session POST 淨值（表單照探測的寫法）→ Yahoo；一共 3 個請求、每個只發一次。"""
        fake = FakeNet(chart=chart_json({"2026-01-05": 20.3}), yahoo=yahoo_json({"2026-01-05": 20.5}))
        net = B.OneShot(send=fake, sleep=lambda s: None)
        raw = B.fetch_all(net, NOW.date())
        self.assertEqual([x["method"] for x in fake.sent], ["GET", "POST", "GET"])
        self.assertEqual(fake.sent[1]["url"], B.CHART_URL)
        self.assertEqual(sorted(fake.sent[1]["data"]), ["endDate", "id", "startDate", "type"])
        self.assertEqual(fake.sent[1]["data"]["id"], "00646")
        self.assertEqual(fake.sent[1]["data"]["type"], "fundPric")
        self.assertEqual(fake.sent[1]["data"]["endDate"], "2026/11/05")
        self.assertEqual(fake.sent[1]["data"]["startDate"], (NOW.date() - timedelta(days=3 * 365)).strftime("%Y/%m/%d"))
        self.assertEqual(fake.sent[1]["headers"]["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(fake.sent[1]["headers"]["Referer"], B.PAGE_URL)
        self.assertIn("00646.TW", fake.sent[2]["url"])
        self.assertEqual(len(net.log), 3)
        self.assertEqual(sorted(set(x["host"] for x in net.log)), ["query1.finance.yahoo.com", "www.twse.com.tw"])
        self.assertEqual(set(raw), {"page", "chart", "yahoo"})

    def test_blocked_page_means_no_post(self):
        """「安全性考量」頁、或頁面不是 200：不發淨值的 POST。對照組：拿不到頁面也照樣 POST → 這一條會紅。"""
        for page, status in (("<html>00646 ETF 因為安全性考量，您所執行的頁面無法呈現</html>", 200), ("<html>00646 ETF</html>", 502)):
            fake = FakeNet(page=page, page_status=status, chart=chart_json({"2026-01-05": 20.3}), yahoo=yahoo_json({"2026-01-05": 20.5}))
            with self.assertRaises(B.SourceError):
                B.fetch_all(B.OneShot(send=fake, sleep=lambda s: None), NOW.date())
            self.assertEqual([x["method"] for x in fake.sent], ["GET"], page)

    def test_parse_chart_skips_zero_and_bad_rows_and_rejects_bad_formats(self):
        navs, prem, skipped = B.parse_chart(json.dumps({"netPrice": [{"date": "2026/01/05", "count": "20.30"}, {"date": "2026/01/06", "count": 0},
                                                                     {"date": "壞的", "count": 20.1}, {"date": "2026/01/07", "count": "--"}],
                                                        "atmps": [{"date": "2026/01/05", "count": "0.98"}]}))
        self.assertEqual(navs, {"2026-01-05": 20.3})
        self.assertEqual(prem, {"2026-01-05": 0.98})
        self.assertEqual(len(skipped), 3)
        for bad in ("<html>不是 JSON</html>", json.dumps({"atmps": []}), json.dumps({"netPrice": []}), json.dumps([1, 2]),
                    json.dumps({"netPrice": [{"date": "2026/01/05", "count": 0}]})):
            with self.assertRaises(B.SourceError, msg=bad):
                B.parse_chart(bad)


class TestPlan(unittest.TestCase):
    def plan(self, navs, hist, yahoo, existing=None):
        existing = copy.deepcopy(EXISTING if existing is None else existing)
        before = copy.deepcopy(existing)
        new_rows, report = B.plan_backfill(existing, navs, hist, yahoo, "2026-11-05")
        self.assertEqual(existing, before, "plan_backfill 不可以改到既有的列")
        return new_rows, report

    def test_only_dates_the_file_does_not_have_and_existing_rows_untouched(self):
        """只增不改：既有的日期（不管有沒有官方淨值）一列都不補。對照組：既有列被覆蓋 → 這一條會紅。"""
        navs = {"2025-12-31": 20.1, "2026-01-02": 20.2, "2026-01-05": 20.3, "2026-01-06": 20.4}
        hist = {"2025-12-31": 20.0, "2026-01-02": 20.3, "2026-01-05": 20.5, "2026-01-06": 20.6}
        new_rows, report = self.plan(navs, hist, {})
        self.assertEqual([r["d"] for r in new_rows], ["2025-12-31", "2026-01-02"])
        self.assertTrue(report["ok"])

    def test_pairs_the_same_day_and_counts_the_skipped(self):
        """淨值與收盤要同一天；收盤先找 data/history、沒有才找 Yahoo；兩邊都沒有就略過並計數。對照組：配錯一天 → 這一條會紅。"""
        navs = {"2025-12-29": 19.9, "2025-12-30": 20.0, "2025-12-31": 20.1}
        hist = {"2025-12-31": 20.2}
        yahoo = {"2025-12-29": 20.0, "2025-12-31": 20.2}
        new_rows, report = self.plan(navs, hist, yahoo)
        by = dict((r["d"], r) for r in new_rows)
        self.assertEqual(sorted(by), ["2025-12-29", "2025-12-31"])
        self.assertEqual(by["2025-12-29"]["officialClose"], 20.0)
        self.assertEqual(by["2025-12-31"]["officialClose"], 20.2)
        self.assertEqual(by["2025-12-29"]["officialPremiumPct"], A.premium_pct(20.0, 19.9))      # 同一天的收盤對同一天的淨值
        self.assertEqual(report["skippedNoClose"], {"count": 1, "dates": ["2025-12-30"]})
        self.assertEqual(report["added"]["closeFrom"], {"data/history": 1, "yahoo": 1})

    def test_new_rows_are_official_only_with_labels_and_date_sources(self):
        """預估不可以混進確定：新列只有確定口徑的欄位。對照組：標籤寫成別的字、或帶了預估欄位 → 這一條會紅。"""
        new_rows, _ = self.plan({"2025-12-31": 20.1}, {"2025-12-31": 20.2}, {})
        r = new_rows[0]
        self.assertEqual(sorted(r), ["d", "dateSource", "officialClose", "officialLabel", "officialNav", "officialNavSeenOn", "officialPremiumPct", "officialTag"])
        self.assertEqual((r["officialTag"], r["officialLabel"], r["officialNavSeenOn"]), ("確定", "回補", "2026-11-05"))
        self.assertEqual(A.LABEL_BACKFILL, "回補")
        self.assertEqual(r["dateSource"], {"officialNav": "twse-etfortune", "officialClose": "data/history"})   # data/history 那一點沒記來源：不猜
        self.assertFalse([k for k in r if k.startswith("est") or k in ("price", "units", "time")])

    def test_close_source_says_where_the_close_really_came_from(self):
        """收盤價的 dateSource 照實寫：Yahoo 直接抓的寫 yahoo；取自 data/history 的照抄那一點自己記的來源（例如證交所的 official），
        不一律寫成 yahoo。對照組：一律寫 yahoo → 這一條會紅。"""
        navs = {"2025-12-29": 19.9, "2025-12-30": 20.0, "2025-12-31": 20.1}
        hist = {"2025-12-30": 20.1, "2025-12-31": 20.2}
        rows, _ = B.plan_backfill([], navs, hist, {"2025-12-29": 20.0}, "2026-11-05", hist_sources={"2025-12-31": "official"})
        by = dict((r["d"], r["dateSource"]["officialClose"]) for r in rows)
        self.assertEqual(by, {"2025-12-29": "yahoo", "2025-12-30": "data/history", "2025-12-31": "data/history:official"})
        self.assertEqual(set(r["dateSource"]["officialNav"] for r in rows), {"twse-etfortune"})

    def test_close_overlap_over_three_tenths_of_a_percent_stops(self):
        """收盤價重疊：data/history 與 Yahoo 同一天差超過 0.3% 就停。對照組：比對關掉 → 這一條會紅。"""
        self.assertEqual(B.CLOSE_THRESHOLD_PCT, 0.3)
        navs = {"2025-12-31": 20.1}
        _, ok = self.plan(navs, {"2025-12-31": 20.0, "2026-01-02": 20.0}, {"2025-12-31": 20.0, "2026-01-02": 20.05})       # 0.25%
        self.assertTrue(ok["ok"])
        _, bad = self.plan(navs, {"2025-12-31": 20.0, "2026-01-02": 20.0}, {"2025-12-31": 20.0, "2026-01-02": 20.07})      # 0.35%
        self.assertFalse(bad["ok"])
        self.assertEqual([x["d"] for x in bad["closeOverlap"]["over"]], ["2026-01-02"])

    def test_official_nav_overlap_must_match_to_two_decimals(self):
        """既有列的官方淨值跟 e添富同一天的，四捨五入到小數第二位要一樣；不一樣就停。另外列出只有一邊有的。對照組：比對關掉 → 這一條會紅。"""
        _, ok = self.plan({"2026-01-05": 20.304, "2026-01-07": 20.5}, {}, {})
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["navOverlap"]["same"], 1)
        _, bad = self.plan({"2026-01-05": 20.31}, {}, {})
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["navOverlap"]["over"], [{"d": "2026-01-05", "existing": 20.3, "etfortune": 20.31}])
        _, lists = self.plan({"2026-01-06": 20.4}, {}, {})
        self.assertEqual(lists["navOverlap"]["existingHasNavSourceDoesNot"], [{"d": "2026-01-05", "existing": 20.3}])
        self.assertEqual(lists["navOverlap"]["existingLacksNavSourceHas"], [{"d": "2026-01-06", "etfortune": 20.4}])

    def test_cap_is_eight_hundred(self):
        """上限 800（同一個常數）。對照組：改回 400 → 這一條會紅。"""
        self.assertEqual(A.NAV_MAX_ROWS, 800)
        ds = days("2023-01-02", 850)
        navs = dict((d, 20.0) for d in ds)
        new_rows, _ = self.plan(navs, dict((d, 20.1) for d in ds), {}, existing=[])
        self.assertEqual(len(new_rows), 800)
        self.assertEqual(new_rows[-1]["d"], ds[-1])


class TestMain(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.sb.write_history({"2026-01-02": 20.3, "2026-01-05": 20.5})
        self.sb.write_nav(copy.deepcopy(EXISTING))
        self.fake = FakeNet(chart=chart_json({"2025-12-31": 20.1, "2026-01-02": 20.2, "2026-01-05": 20.3}),
                            yahoo=yahoo_json({"2025-12-31": 20.2, "2026-01-02": 20.3, "2026-01-05": 20.5}))

    def tearDown(self):
        self.sb.close()

    def run_main(self, args, net="fake"):
        net = B.OneShot(send=self.fake, sleep=lambda s: None) if net == "fake" else net
        with redirect_stdout(io.StringIO()) as out:
            rc = B.main(args, net=net, now=NOW, paths=self.sb.paths)
        return rc, out.getvalue()

    def test_writes_the_new_file_and_never_touches_the_nav_file(self):
        """寫的是 nav/tw00646.backfill.json；tw00646.json 一個位元組都不動（A1-4 的裁決：乙）。"""
        before = self.sb.nav_bytes()
        rc, out = self.run_main(["--save-raw", self.sb.outside])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.sb.nav_bytes(), before)
        j = self.sb.backfill()
        self.assertEqual([r["d"] for r in j["rows"]], ["2025-12-31", "2026-01-02"])
        self.assertEqual(j["backfilledOn"], "2026-11-05")
        self.assertEqual(j["closeFrom"], {"data/history": 1, "yahoo": 1})
        self.assertEqual([r["dateSource"]["officialClose"] for r in j["rows"]], ["yahoo", "data/history:yahoo"])   # 那一點記的來源照抄
        self.assertEqual([x["method"] for x in j["requests"]], ["GET", "POST", "GET"])
        self.assertTrue(os.path.exists(os.path.join(self.sb.outside, B.RAW_FILES["chart"])))

    def test_dry_run_writes_nothing(self):
        rc, out = self.run_main(["--dry-run"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(A.nav_backfill_path("tw00646", self.sb.paths)))

    def test_from_raw_sends_nothing(self):
        """--from-raw 不發任何請求（給它一個一用就會紅的連線）。"""
        self.run_main(["--save-raw", self.sb.outside, "--dry-run"])

        class NoNet(object):
            log = []

            def request(self, *a, **kw):
                raise AssertionError("不該發請求")
        rc, out = self.run_main(["--from-raw", self.sb.outside], net=NoNet())
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.sb.backfill()["rows"]), 2)

    def test_mismatch_writes_nothing_and_exits_one(self):
        self.fake.chart = chart_json({"2025-12-31": 20.1, "2026-01-05": 20.35})                # 既有列的 20.3 對不上
        rc, out = self.run_main([])
        self.assertEqual(rc, 1, out)
        self.assertFalse(os.path.exists(A.nav_backfill_path("tw00646", self.sb.paths)))

    def test_source_trouble_exits_two(self):
        self.fake.page = "<html>因為安全性考量，您所執行的頁面無法呈現</html>"
        rc, out = self.run_main([])
        self.assertEqual(rc, 2, out)
        self.assertFalse(os.path.exists(A.nav_backfill_path("tw00646", self.sb.paths)))

    def test_raw_and_report_must_be_outside_the_repo(self):
        rc, out = self.run_main(["--save-raw", os.path.join(ROOT, "tmp-raw")])
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.fake.sent, [])


class TestAnalyzeMergesTheBackfill(unittest.TestCase):
    """analyze.py：讀淨值時把回補的新檔只增不改地併進來；15:30 那一輪存檔時寫進 tw00646.json，檔頭留下出處。"""

    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.close()

    def write_backfill(self, rows):
        with io.open(A.nav_backfill_path("tw00646", self.sb.paths), "w", encoding="utf-8") as fh:
            json.dump({"id": "tw00646", "backfilledOn": "2026-11-05", "rows": rows}, fh, ensure_ascii=False)

    def bf_rows(self, dates, pct=0.5):
        return [{"d": d, "officialNav": 20.0, "officialClose": 20.1, "officialPremiumPct": pct, "officialTag": "確定", "officialLabel": "回補",
                 "officialNavSeenOn": "2026-11-05", "dateSource": {"officialNav": "twse-etfortune", "officialClose": "yahoo"}} for d in dates]

    def test_merge_only_adds_and_twice_is_the_same_as_once(self):
        """對照組：回補的列蓋過既有的列 → 這一條會紅。"""
        existing = copy.deepcopy(EXISTING)
        bf = self.bf_rows(["2025-12-31", "2026-01-05", "2026-01-06"])
        once = A.merge_backfill(existing, bf)
        self.assertEqual([r["d"] for r in once], ["2025-12-31", "2026-01-05", "2026-01-06"])
        self.assertEqual(once[1], EXISTING[0])
        self.assertEqual(once[2], EXISTING[1])
        self.assertEqual(A.merge_backfill(once, bf), once)
        many = self.bf_rows(days("2023-01-02", 830))
        capped = A.merge_backfill(existing, many)
        self.assertEqual(len(capped), 800)
        self.assertEqual(A.merge_backfill(capped, many), capped)

    def test_offline_summaries_include_the_backfill_without_writing(self):
        """離線：摘要算得到回補的列（近一年 252 列、可用的全部），但不寫任何淨值檔。"""
        self.sb.write_nav(copy.deepcopy(EXISTING))
        before = self.sb.nav_bytes()
        self.write_backfill(self.bf_rows(days("2024-11-01", 300)))
        out = A.build_premium(None, True, self.sb.paths, [], NOW)["tw00646"]
        self.assertEqual(self.sb.nav_bytes(), before)
        self.assertEqual(out["official"]["n"], 301)
        self.assertEqual(out["officialYear"]["n"], 252)
        self.assertEqual(out["officialYear"]["window"], 252)
        self.assertEqual(A.FX_DAILY_WINDOW, 252)
        self.assertEqual(out["officialYear"]["through"], "2026-01-05")
        self.assertIn("p25Pct", out["officialYear"])
        self.assertEqual(out["estimated"]["n"], 2)                                                  # 回補的列不會跑進預估
        self.assertTrue(any("回補" in n for n in out["notes"]))
        self.assertEqual(out["backfill"]["rows"], 300)

    def test_year_summary_waits_for_twenty_rows(self):
        self.write_backfill(self.bf_rows(days("2026-01-01", 19)))
        out = A.build_premium(None, True, self.sb.paths, [], NOW)["tw00646"]
        self.assertEqual(out["officialYear"]["n"], 19)
        self.assertIn("reason", out["officialYear"])

    def test_the_daily_run_writes_the_rows_in_and_keeps_where_they_came_from(self):
        """15:30 那一輪（抓到 all_etf）：回補的列寫進 tw00646.json、檔頭有 backfill；之後每天重寫檔頭照樣保留。對照組：不保留 → 這一條會紅。"""
        self.sb.write_history({"2026-01-05": 20.5, "2026-01-06": 20.6})
        self.sb.write_nav(copy.deepcopy(EXISTING))
        self.write_backfill(self.bf_rows(["2025-12-30", "2025-12-31"]))

        class F(object):
            count = 1

            def get(self, url, **kw):
                return {"a1": [{"msgArray": [{"a": "00646", "e": 20.7, "f": 20.6, "h": "20.55", "i": "20260107", "j": "17:00"}]}]}
        A.build_premium(F(), False, self.sb.paths, [], NOW)
        with io.open(A.nav_path("tw00646", self.sb.paths), encoding="utf-8") as fh:
            j = json.load(fh)
        ds = [r["d"] for r in j["rows"]]
        self.assertEqual(ds[:2], ["2025-12-30", "2025-12-31"])
        self.assertEqual(dict((r["d"], r) for r in j["rows"])["2026-01-05"], EXISTING[0])
        self.assertEqual(j["backfill"]["file"], "data/analysis/nav/tw00646.backfill.json")
        self.assertEqual(j["backfill"]["rows"], 2)
        self.assertIn("最多留 800 列", j["note"])
        os.remove(A.nav_backfill_path("tw00646", self.sb.paths))                                      # 回補檔就算不在了，出處照樣留著
        A.build_premium(F(), False, self.sb.paths, [], NOW)
        with io.open(A.nav_path("tw00646", self.sb.paths), encoding="utf-8") as fh:
            j2 = json.load(fh)
        self.assertEqual(j2["backfill"], j["backfill"])
        self.assertEqual([r["d"] for r in j2["rows"]][:2], ["2025-12-30", "2025-12-31"])

    def test_a_broken_backfill_file_is_ignored(self):
        with io.open(A.nav_backfill_path("tw00646", self.sb.paths), "w", encoding="utf-8") as fh:
            fh.write("{ 壞掉的")
        self.sb.write_nav(copy.deepcopy(EXISTING))
        out = A.build_premium(None, True, self.sb.paths, [], NOW)["tw00646"]
        self.assertEqual(out["official"]["n"], 1)
        self.assertNotIn("backfill", out)

    def test_docs_say_the_same_numbers_as_the_code(self):
        """docs/ANALYSIS.md 寫的上限與近一年的列數，跟程式的常數一樣（同一個門檻只有一份；文件有測試釘住）。"""
        with io.open(os.path.join(ROOT, "docs", "ANALYSIS.md"), encoding="utf-8") as fh:
            doc = fh.read()
        self.assertIn("最多留 %d 列" % A.NAV_MAX_ROWS, doc)
        self.assertIn("最後 %d 列" % A.FX_DAILY_WINDOW, doc)
        self.assertNotIn("最多留 400 列", doc)


if __name__ == "__main__":
    unittest.main()
