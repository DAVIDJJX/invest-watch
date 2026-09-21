#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_probe_analysis.py — probe_analysis_sources.py 的離線測試（完全不連網）

為什麼探測腳本也要測：探測腳本永遠 exit 0，所以它「判錯」的時候沒有任何東西會紅。
最危險的兩種判錯：
  (1) 只看 HTTP 200 就說「可用」——Yahoo 的 range=max 會無聲地把週線換成月線，
      FinMind 出錯時 HTTP 也照樣 200；
  (2) 手滑對不該碰的主機發請求（台銀）。
這份測試把判準一條一條釘住：餵假回應，檢查每一種該判成什麼。
把 probe_analysis_sources.py 的判準改壞，這裡一定要紅（對照組，見 CHANGELOG 停點 A0）。

順序規則：正式探測之前先跑這一份；這一份紅就停，不打任何真實請求。

    python -m unittest discover -s scripts -p "test_probe_analysis.py" -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import probe_analysis_sources as P   # noqa: E402

DAY = 86400
NOW = datetime(2026, 9, 21, 15, 50, tzinfo=P.TPE)      # 測試裡的「現在」：2026-09-21（一）15:50


# --------------------------------------------------------------------------
# 假回應
# --------------------------------------------------------------------------

def read_text(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def write_text(path, text):
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def resp(status=200, body="", headers=None, seconds=0.5, truncated=False):
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False)
    if isinstance(body, str):
        body = body.encode("utf-8")
    return P.Resp(status, headers or {"Content-Type": "application/json"}, body, seconds, 1, None, truncated)


def yahoo_body(granularity="1wk", step_days=7, points=700, end=None, first_trade=-1325583000,
               null_at=(), tail_dup=False, last_value=100.0):
    end = end or int(NOW.timestamp()) - 3 * DAY
    ts = [end - (points - 1 - i) * step_days * DAY for i in range(points)]
    closes = [50.0 + i * 0.1 for i in range(points)]
    closes[-1] = last_value
    for i in null_at:
        closes[i] = None
    if tail_dup:
        ts.append(ts[-1] + 2 * DAY)
        closes.append(closes[-1])
    return {"chart": {"error": None, "result": [{
        "meta": {"dataGranularity": granularity, "range": "", "gmtoffset": -14400, "currency": "USD",
                 "exchangeName": "SNP", "firstTradeDate": first_trade},
        "timestamp": ts, "indicators": {"quote": [{"close": closes}]}}]}}


YAHOO_404 = {"chart": {"result": None, "error": {"code": "Not Found",
                                                 "description": "No data found, symbol may be delisted"}}}


def finmind_body(rows, msg="success", status=200):
    return {"msg": msg, "status": status, "data": rows}


def recent(days_ago=1):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def daily_dates(start_year, n_years):
    """每年取 1 筆就夠判斷回溯年數；最後補一筆最近的日期。"""
    out = ["%04d-06-15" % (start_year + i) for i in range(n_years)]
    return out + [recent(1)]


def fred_csv(series, rows):
    return "observation_date,%s\n" % series + "\n".join("%s,%s" % r for r in rows) + "\n"


MULTPL_PAGE = """<html><head><title>Shiller PE Ratio</title>
<meta name="description" content="Shiller PE Ratio chart. Current Shiller PE Ratio is 40.94, a change of -0.05 from previous market close.">
</head><body><div id="current"><b>Current <span class="currentTitle">Shiller PE Ratio</span>:</b>
40.94 <span class="neg">-0.05 (-0.13%)</span>
<div id="timestamp">4:00 PM EDT, Fri Sep 18</div></div></body></html>"""


def multpl_table(n_rows=1870, first=("Sep 18, 2026", "40.94"), zero_at=None):
    rows = ['<tr class="odd"><td>%s</td><td>&#x2002;%s</td></tr>' % first]
    y, m = 2026, 9
    for i in range(n_rows - 1):
        val = "0.00" if zero_at == i else "%.2f" % (15 + (i % 200) / 10.0)
        rows.append('<tr><td>%s 1, %d</td><td>&#x2002;%s</td></tr>'
                    % (["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][m - 1], y, val))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return '<table id="datatable"><tr><th>Date</th><th>Value</th></tr>%s</table>' % "".join(rows)


def dgbas_xml(months=548, second_item=True, last_value="112.32"):
    out = ['<?xml version="1.0" encoding="utf-8" ?>\r\n<DataSet Sender_NAME="行政院主計總處">\r\n']
    y, m = 1981, 1
    for i in range(months):
        v = last_value if i == months - 1 else "%.2f" % (52.95 + i * 0.1)
        for typ, val in (("原始值", v), ("年增率(%)", "" if i < 12 else "2.04")):   # 真檔：第一年的年增率是空的
            out.append("<Obs><Item>總指數(指數基期：民國110年=100)</Item><TIME_PERIOD>%dM%02d</TIME_PERIOD>"
                       "<FREQ>M</FREQ><TYPE>%s</TYPE>\r\n<Item_VALUE>%s</Item_VALUE></Obs>\r\n" % (y, m, typ, val))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    if second_item:
        out.append("<Obs><Item>一.食物類(指數基期：民國110年=100)</Item><TIME_PERIOD>1981M01</TIME_PERIOD>"
                   "<FREQ>M</FREQ><TYPE>原始值</TYPE>\r\n<Item_VALUE>38.52</Item_VALUE></Obs>\r\n")
    return "".join(out)


def nstatdb_body(months=548, start=(1981, 1), series="總指數", last_value=112.32, drop_last_obs=False):
    """主計總處總體統計資料庫的 SDMX-JSON（形狀照 2026-09-21 實際看到的回應）。"""
    y, m = start
    values, obs = [], {}
    for i in range(months):
        values.append({"id": "%d-M%d" % (y, m), "name": "%d年%d月" % (y - 1911, m)})
        obs[str(i)] = [last_value if i == months - 1 else round(52.95 + i * 0.1, 2)]
        m += 1
        if m == 13:
            y, m = y + 1, 1
    if drop_last_obs:
        obs.pop(str(months - 1))
    return {"meta": {"test": False, "sender": {"id": "dgbas", "name": "行政院主計總處"}},
            "data": {"dataSets": [{"action": "Information", "series": {"0": {"observations": obs}}}],
                     "structure": {"name": "消費者物價基本分類指數", "dimensions": {
                         "series": [{"keyPosition": 0, "id": "fldid", "name": "基本分類",
                                     "values": [{"id": "1", "name": series}]}],
                         "observation": [{"id": "ym", "name": "統計期", "values": values, "role": "TIME_PERIOD"}]}}}}


NSTATDB_HTML_SHELL = "<html><head><title>總體統計資料庫</title></head><body></body></html>"


def all_etf_body(h_00646="76.58", h_00679b="25.7842", date="20260921"):
    return {"a1": [
        {"msgArray": [
            {"a": "00646", "b": "元大S&P500", "c": 1234567000, "d": 2500000, "e": 76.3, "f": 76.61, "g": -0.4,
             "h": h_00646, "i": date, "j": "14:30:00", "k": "3"},
            {"a": "00679B", "b": "元大美債20年", "c": 6100000000, "d": -1500000, "e": 25.69, "f": 25.75, "g": -0.23,
             "h": h_00679b, "i": date, "j": "14:30:00", "k": 3}], "rtCode": "0000"},
        {"msgArray": [{"a": "0050", "b": "元大台灣50", "c": "1", "d": "0", "e": "1", "f": "1", "g": "0",
                       "h": "1", "i": date, "j": "14:30:00", "k": "1"}], "rtCode": "0000"}]}


def twse_chart_body(n=740, last_gap_days=2):
    end = NOW - timedelta(days=last_gap_days)
    nav = [{"date": (end - timedelta(days=(n - 1 - i) * 1.48)).strftime("%Y/%m/%d"), "count": 60 + i * 0.02}
           for i in range(n)]
    return {"netPrice": nav, "atmps": [{"date": x["date"], "count": -0.3} for x in nav]}


def tpex_body():
    nav = [{"date": "%02d/%02d" % (8 if i < 12 else 9, (i % 28) + 1), "count": 25.0 + i * 0.01} for i in range(30)]
    return {"stockNo": "00679B", "shortName": "元大美債20年", "underlyingIndex": "ICE美國政府20+年期債券指數",
            "netPrice": nav, "atmps": [{"date": x["date"], "count": -0.4} for x in nav], "close1": nav}


# --------------------------------------------------------------------------
# 不連網的替身：看網址決定回什麼，並記下每一個請求
# --------------------------------------------------------------------------

class FakeNet(object):
    def __init__(self, overrides=None):
        self.requests = []
        self.overrides = overrides or {}

    def fetch(self, req):
        P.assert_host_allowed(req.url)                 # 跟真的那一層一樣：不在白名單就拒絕
        self.requests.append(req)
        for part, r in self.overrides.items():
            if part in req.url:
                return r(req) if callable(r) else r
        return self.default(req)

    def default(self, req):
        u = req.url
        if "finance.yahoo.com" in u:
            if "00679B.TW?" in u:
                return resp(404, YAHOO_404)
            if "range=max" in u:
                return resp(200, yahoo_body("1mo", 30, 300))
            if "%5EVIX" in u:
                return resp(200, yahoo_body("1d", 1, 1260, end=int(NOW.timestamp()) - DAY, last_value=17.5))
            return resp(200, yahoo_body("1wk", 7, 700))
        if "finmindtrade" in u:
            if "datalist" in u:
                return resp(200, finmind_body(["United States 10-Year", "United States 20-Year"]))
            if "GovernmentBondsYield" in u:
                return resp(200, finmind_body([{"date": d, "name": "United States 20-Year", "value": 4.9}
                                               for d in daily_dates(1994, 32)]))
            if "MonthRevenue" in u:
                rows, y, m = [], 2022, 1
                for _i in range(56):
                    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
                    rows.append({"date": "%04d-%02d-01" % (ny, nm), "stock_id": "2330", "revenue": 1,
                                 "revenue_year": y, "revenue_month": m, "country": "Taiwan"})
                    y, m = ny, nm
                return resp(200, finmind_body(rows))                       # 最後一筆：2026 年 8 月營收
            if "TaiwanStockPER" in u:
                return resp(200, finmind_body([{"date": d, "stock_id": "2330", "dividend_yield": 1.5, "PER": 25.0,
                                                "PBR": 7.0} for d in daily_dates(2005, 21)]))
            if "MarginPurchase" in u:
                return resp(200, finmind_body([{"date": d, "stock_id": "2330", "MarginPurchaseTodayBalance": 21000,
                                                "ShortSaleTodayBalance": 300} for d in daily_dates(2001, 25)]))
            if "InstitutionalInvestors" in u:
                rows = []
                for d in daily_dates(2005, 21):
                    for n in ("Foreign_Investor", "Investment_Trust", "Dealer_self"):
                        rows.append({"date": d, "stock_id": "2330", "name": n, "buy": 10, "sell": 5})
                return resp(200, finmind_body(rows))
            if "TaiwanExchangeRate" in u:
                rows = [{"date": "2006-01-%02d" % (i + 1), "currency": "X", "cash_buy": -1.0, "cash_sell": -1.0,
                         "spot_buy": -1.0, "spot_sell": -1.0} for i in range(20)]
                for i in range(5000):
                    d = (NOW - timedelta(days=5000 - i)).strftime("%Y-%m-%d")
                    rows.append({"date": d, "currency": "X", "cash_buy": 31.0, "cash_sell": 32.0,
                                 "spot_buy": 31.4, "spot_sell": 31.5})
                return resp(200, finmind_body(rows))
        if "fred.stlouisfed.org" in u:
            sid = u.split("id=")[1]
            if sid == "CPIAUCSL":
                rows = [("%04d-%02d-01" % (1960 + i // 12, i % 12 + 1), "%.1f" % (200 + i * 0.15)) for i in range(800)]
                return resp(200, fred_csv(sid, rows[:-1] + [("2026-08-01", "330.1")]), {"Content-Type": "text/csv"})
            rows = [((NOW - timedelta(days=13000 - i)).strftime("%Y-%m-%d"), "" if i % 50 == 0 else "1.25")
                    for i in range(12999)] + [(recent(3), "1.31")]
            return resp(200, fred_csv(sid, rows), {"Content-Type": "text/csv"})
        if "multpl.com/shiller-pe/table" in u:
            return resp(200, multpl_table(), {"Content-Type": "text/html"})
        if "multpl.com" in u:
            return resp(200, MULTPL_PAGE, {"Content-Type": "text/html"})
        if "data.gov.tw" in u:
            return resp(200, {"success": True, "result": {"modifiedDate": "2026-09-05 10:00:00", "distribution": [
                {"resourceDownloadUrl": "https://ws.dgbas.gov.tw/001/Upload/461/relfile/11525/230555/pr0101a1m.xml"}]}})
        if "ws.dgbas.gov.tw" in u:
            return resp(200, dgbas_xml(), {"Content-Type": "text/xml", "Content-Length": "16462137"}, truncated=True)
        if "nstatdb" in u:
            return resp(200, nstatdb_body())
        if "all_etf.txt" in u:
            return resp(200, all_etf_body(), {"Content-Type": "text/plain"})
        if "ajaxEtfInfoChart" in u:
            return resp(200, twse_chart_body())
        if "info.tpex.org.tw" in u:
            return resp(200, tpex_body())
        raise AssertionError("測試沒有準備這個網址的假回應：" + u)


class FrozenNow(unittest.TestCase):
    """把探測腳本裡的「現在」固定在 2026-09-21，測試才不會哪天自己變紅。"""

    def setUp(self):
        self._orig_now = P.now_tpe
        P.now_tpe = lambda: NOW
        self._orig_env = os.environ.pop("GITHUB_ACTIONS", None)

    def tearDown(self):
        P.now_tpe = self._orig_now
        if self._orig_env is not None:
            os.environ["GITHUB_ACTIONS"] = self._orig_env

    def run_all(self, overrides=None, only=None):
        net = FakeNet(overrides)
        with redirect_stdout(io.StringIO()) as out:
            payload = P.run_probe(net, only=only, now=NOW)
        return payload, net, out.getvalue()

    def state_of(self, payload, key):
        return [r for r in payload["results"] if r["key"] == key][0]


# --------------------------------------------------------------------------
# 1. 碰不到台銀；白名單以外的主機一律不送
# --------------------------------------------------------------------------

class TestHosts(FrozenNow):
    def test_bot_is_refused(self):
        for url in ("https://rate.bot.com.tw/xrt?Lang=zh-TW", "https://www.bot.com.tw/", "http://rate.bot.com.tw/gold"):
            with self.assertRaises(P.HostNotAllowed):
                P.assert_host_allowed(url)

    def test_bot_is_refused_even_if_someone_adds_it_to_the_allowlist(self):
        orig = P.ALLOWED_HOSTS
        P.ALLOWED_HOSTS = orig + ("rate.bot.com.tw",)
        try:
            with self.assertRaises(P.HostNotAllowed):
                P.assert_host_allowed("https://rate.bot.com.tw/xrt")
        finally:
            P.ALLOWED_HOSTS = orig

    def test_unknown_host_is_refused(self):
        with self.assertRaises(P.HostNotAllowed):
            P.assert_host_allowed("https://example.com/data.json")

    def test_a_url_that_only_looks_like_a_good_host_is_refused(self):
        # https://好主機@壞主機/ 會連到壞主機；白名單要看的是真正連到的那一台
        for url in ("https://www.multpl.com@rate.bot.com.tw/xrt", "https://www.multpl.com:443@evil.example.com/",
                    "https://api.finmindtrade.com.evil.example.com/api", "ftp://www.multpl.com/x", "not a url"):
            with self.assertRaises(P.HostNotAllowed, msg=url):
                P.assert_host_allowed(url)
        self.assertEqual(P.assert_host_allowed("https://WWW.Multpl.com:443/shiller-pe"), "www.multpl.com")

    def test_the_hop_sent_before_a_refused_redirect_is_still_counted(self):
        class Session(object):
            def get(self, url, **kw):
                return FakeHttp(302, {"Location": "https://rate.bot.com.tw/gold"})

        net = P.Net(sleep=lambda x: None, clock=lambda: 0.0, session=Session())
        item = {"key": "X-3", "group": "cape", "spec": "-", "title": "被轉址帶走", "expect": None, "when": None,
                "req": lambda ctx: P.Req("https://www.multpl.com/shiller-pe"), "check": lambda r, ctx: {}}
        rec = P.run_item(item, net, {})
        self.assertEqual((rec["state"], rec["requests"]), (P.FAILED, 1))
        self.assertEqual(len(net.sent), 1)

    def test_every_planned_request_is_on_the_allowlist_and_none_is_bot(self):
        items = P.build_items(P.load_yahoo_symbols(), NOW)
        ctx = {"shillerXlsUrl": "https://img1.wsimg.com/blobby/go/x/downloads/y/ie_data.xls"}
        n = 0
        for it in items:
            if it["req"] is None:
                continue
            req = it["req"](ctx)
            self.assertNotIn("bot.com.tw", req.url)
            P.assert_host_allowed(req.url)             # 不在白名單會丟例外
            n += 1
        self.assertGreaterEqual(n, 28)

    def test_a_full_run_never_asks_for_bot(self):
        payload, net, _ = self.run_all()
        self.assertTrue(net.requests)
        self.assertFalse([r.url for r in net.requests if "bot.com.tw" in r.url])

    def test_redirect_to_a_host_off_the_allowlist_is_not_followed(self):
        class Session(object):
            def __init__(self):
                self.urls = []

            def get(self, url, **kw):
                self.urls.append(url)
                return FakeHttp(302, {"Location": "https://rate.bot.com.tw/gold"})

        s = Session()
        net = P.Net(sleep=lambda x: None, clock=lambda: 0.0, session=s)
        with self.assertRaises(P.HostNotAllowed):
            net.fetch(P.Req("https://www.multpl.com/shiller-pe"))
        self.assertEqual(s.urls, ["https://www.multpl.com/shiller-pe"])    # 第二跳根本沒送出去

    def test_refused_host_becomes_a_failed_row_not_a_crash(self):
        item = {"key": "X-1", "group": "cape", "spec": "-", "title": "壞網址", "expect": None, "when": None,
                "req": lambda ctx: P.Req("https://rate.bot.com.tw/xrt"), "check": lambda r, ctx: {}}
        net = FakeNet()
        rec = P.run_item(item, net, {})
        self.assertEqual(rec["state"], P.FAILED)
        self.assertEqual(net.requests, [])
        self.assertIn("台銀", rec["note"])


class FakeHttp(object):
    def __init__(self, status, headers=None, content=b""):
        self.status_code, self.headers, self.content = status, headers or {}, content

    def close(self):
        pass

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]


# --------------------------------------------------------------------------
# 2. 對來源克制：固定間隔、FinMind 不重試、大檔讀到就斷
# --------------------------------------------------------------------------

class TestManners(FrozenNow):
    def make_net(self, replies):
        class Session(object):
            def __init__(self):
                self.calls = []

            def _next(self, method, url, kw):
                self.calls.append((method, url, kw))
                return replies.pop(0)

            def get(self, url, **kw):
                return self._next("GET", url, kw)

            def post(self, url, **kw):
                return self._next("POST", url, kw)

            def head(self, url, **kw):
                return self._next("HEAD", url, kw)

        clock = {"t": 1000.0}
        slept = []

        def sleep(sec):
            slept.append(sec)
            clock["t"] += sec

        s = Session()
        return P.Net(sleep=sleep, clock=lambda: clock["t"], session=s), s, slept

    def test_at_least_three_seconds_between_any_two_requests(self):
        net, s, slept = self.make_net([FakeHttp(200, {}, b"{}"), FakeHttp(200, {}, b"{}"), FakeHttp(200, {}, b"{}")])
        net.fetch(P.Req("https://query1.finance.yahoo.com/v8/finance/chart/A"))
        net.fetch(P.Req("https://query1.finance.yahoo.com/v8/finance/chart/B"))
        net.fetch(P.Req("https://www.twse.com.tw/zh/ETFortune/ajaxEtfInfoChart", method="POST"))
        self.assertEqual(len(slept), 2)
        self.assertGreaterEqual(slept[0], 3.0)
        self.assertGreaterEqual(slept[1], 3.5)                              # 證交所 3.5 秒

    def test_waiting_time_is_not_counted_as_the_source_being_slow(self):
        net, s, slept = self.make_net([FakeHttp(200, {}, b"{}"), FakeHttp(200, {}, b"{}")])
        net.fetch(P.Req("https://api.finmindtrade.com/api/v4/data?dataset=A"))
        r = net.fetch(P.Req("https://api.finmindtrade.com/api/v4/data?dataset=B"))
        self.assertEqual(len(slept), 1)
        self.assertLess(r.seconds, 0.5)                                     # 等的那 3 秒不算

    def test_a_connection_that_never_got_through_still_counts_as_one_request(self):
        class Boom(object):
            status_code = None

        def explode(*a, **kw):
            raise IOError("HTTPSConnectionPool(host='ws.dgbas.gov.tw', port=443): Max retries exceeded "
                          + "x" * 300 + " [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate")

        net, s, slept = self.make_net([])
        s.get = explode
        r = net.fetch(P.Req("https://ws.dgbas.gov.tw/x.xml"))
        self.assertIsNone(r.status)
        self.assertEqual(r.requests_made, 1)                                # 連不上也是對外發了一次
        self.assertIn("CERTIFICATE_VERIFY_FAILED", r.error)                 # 訊息很長也要留得住最後面的真正原因
        self.assertLess(len(r.error), 330)

    def test_certificate_failure_is_explained_and_never_bypassed(self):
        dead = P.Resp(None, {}, b"", 1.0, 1, "SSLError：…… [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        payload, net, _ = self.run_all(only={"cpi"}, overrides={"ws.dgbas.gov.tw": dead})
        r = self.state_of(payload, "T-02")
        self.assertEqual(r["state"], P.FAILED)
        self.assertIn("中繼憑證", r["note"])
        src = read_text(os.path.join(HERE, "probe_analysis_sources.py"))
        self.assertNotIn("verify=False", src.replace(" ", ""))              # 永遠不准關掉憑證驗證

    def test_relative_redirect_stays_on_the_same_host(self):
        net, s, slept = self.make_net([FakeHttp(302, {"Location": "/shiller-pe/"}), FakeHttp(200, {}, b"ok")])
        r = net.fetch(P.Req("https://www.multpl.com/shiller-pe"))
        self.assertEqual([c[1] for c in s.calls], ["https://www.multpl.com/shiller-pe",
                                                   "https://www.multpl.com/shiller-pe/"])
        self.assertEqual((r.status, r.requests_made), (200, 2))

    def test_finmind_is_never_retried(self):
        net, s, slept = self.make_net([FakeHttp(500, {}, b"oops"), FakeHttp(200, {}, b"{}")])
        r = net.fetch(P.Req("https://api.finmindtrade.com/api/v4/data?dataset=X"))
        self.assertEqual(r.status, 500)
        self.assertEqual(len(s.calls), 1)

    def test_finmind_429_is_not_retried_either(self):
        net, s, slept = self.make_net([FakeHttp(429, {}, b""), FakeHttp(200, {}, b"{}")])
        net.fetch(P.Req("https://api.finmindtrade.com/api/v4/data?dataset=X"))
        self.assertEqual(len(s.calls), 1)

    def test_yahoo_429_waits_ten_seconds_and_retries_once(self):
        net, s, slept = self.make_net([FakeHttp(429, {}, b""), FakeHttp(429, {}, b""), FakeHttp(200, {}, b"{}")])
        with redirect_stdout(io.StringIO()):
            r = net.fetch(P.Req("https://query1.finance.yahoo.com/v8/finance/chart/A", retry429=True))
        self.assertEqual(len(s.calls), 2)                                   # 只重試一次
        self.assertEqual(r.status, 429)
        self.assertIn(10, slept)

    def test_only_yahoo_items_ask_for_the_429_retry(self):
        ctx = {"shillerXlsUrl": "https://img1.wsimg.com/x/ie_data.xls"}
        for it in P.build_items(P.load_yahoo_symbols(), NOW):
            if it["req"] is None:
                continue
            req = it["req"](ctx)
            self.assertEqual(req.retry429, it["group"] == "yahoo", it["key"])

    def test_big_file_is_cut_off_as_soon_as_the_wanted_block_is_read(self):
        xml = (dgbas_xml() + "<Obs><Item>一.食物類(指數基期：民國110年=100)</Item></Obs>" * 40000).encode("utf-8")
        self.assertGreater(len(xml), 2 * 1024 * 1024)
        net, s, slept = self.make_net([FakeHttp(200, {"Content-Length": str(len(xml))}, xml)])
        r = net.fetch(P.Req("https://ws.dgbas.gov.tw/x.xml", cap=P.DGBAS_CAP, stop=P.dgbas_stop))
        self.assertTrue(r.truncated)
        self.assertLessEqual(len(r.body), P.DGBAS_CAP + 65536)
        self.assertTrue(s.calls[0][2].get("stream"))
        out = P.check_dgbas_xml(r, {})                                      # 截斷的那一段照樣解析得出來
        self.assertEqual(out["latest"], "2026-08")

    def test_cap_stops_the_download_even_if_the_block_never_ends(self):
        xml = dgbas_xml(months=548, second_item=False).encode("utf-8") * 6
        net, s, slept = self.make_net([FakeHttp(200, {}, xml)])
        r = net.fetch(P.Req("https://ws.dgbas.gov.tw/x.xml", cap=P.DGBAS_CAP, stop=P.dgbas_stop))
        self.assertLessEqual(len(r.body), P.DGBAS_CAP + 16384)

    def test_finmind_402_stops_the_whole_group(self):
        payload, net, out = self.run_all(only={"finmind"}, overrides={
            "TaiwanStockMonthRevenue": resp(402, {"msg": "Requests reach the upper limit.", "status": 402})})
        states = dict((r["key"], r["state"]) for r in payload["results"])
        self.assertEqual(states["F-01"], P.OK)
        self.assertEqual(states["F-03"], P.FAILED)
        for key in ("F-04", "F-05", "F-06", "F-07", "F-08"):
            self.assertEqual(states[key], P.SKIPPED, key)
        sent = [r.url for r in net.requests]
        self.assertEqual(len(sent), 3)                                      # datalist、公債、月營收，之後一個都沒送
        self.assertFalse([u for u in sent if "TaiwanStockPER" in u])

    def test_finmind_403_in_the_body_also_stops_the_group(self):
        payload, net, _ = self.run_all(only={"finmind"}, overrides={
            "datalist": resp(200, {"msg": "ip banned", "status": 403})})
        self.assertEqual(len(net.requests), 1)
        self.assertEqual(self.state_of(payload, "F-02")["state"], P.SKIPPED)


# --------------------------------------------------------------------------
# 3. 判準：只看 HTTP 200 一律不算「可用」
# --------------------------------------------------------------------------

class TestYahooCriteria(FrozenNow):
    def wk(self, r):
        return P.check_yahoo(r, "1wk", 6, 8, 52, min_years=10)

    def test_real_weekly_with_long_history_is_ok(self):
        out = self.wk(resp(200, yahoo_body("1wk", 7, 700)))
        self.assertEqual(out["points"], 700)

    def test_http_200_but_monthly_bars_is_degraded(self):
        with self.assertRaises(P.ProbeDegraded) as cm:
            self.wk(resp(200, yahoo_body("1mo", 30, 300)))
        self.assertIn("1mo", str(cm.exception))

    def test_declared_weekly_but_timestamps_are_a_month_apart_is_degraded(self):
        with self.assertRaises(P.ProbeDegraded) as cm:
            self.wk(resp(200, yahoo_body("1wk", 30, 300)))
        self.assertIn("中位間距", str(cm.exception))

    def test_monthly_spacing_but_declared_monthly_never_passes_as_weekly(self):
        with self.assertRaises(P.ProbeDegraded):
            self.wk(resp(200, yahoo_body("1mo", 7, 700)))

    def test_404_with_error_body_is_failed(self):
        with self.assertRaises(P.ProbeFail) as cm:
            self.wk(resp(404, YAHOO_404))
        self.assertIn("Not Found", str(cm.exception))

    def test_http_200_with_empty_result_is_failed(self):
        with self.assertRaises(P.ProbeFail):
            self.wk(resp(200, {"chart": {"result": [], "error": None}}))

    def test_http_200_with_html_is_failed(self):
        with self.assertRaises(P.ProbeFail):
            self.wk(resp(200, "<html>Will be right back</html>"))

    def test_all_null_closes_is_failed(self):
        with self.assertRaises(P.ProbeFail):
            self.wk(resp(200, yahoo_body("1wk", 7, 60, null_at=range(60))))

    def test_short_history_is_degraded_with_a_plain_reason(self):
        with self.assertRaises(P.ProbeDegraded) as cm:
            self.wk(resp(200, yahoo_body("1wk", 7, 300)))                   # 5.7 年
        self.assertIn("資料不足", str(cm.exception))

    def test_nulls_and_duplicate_tail_are_reported_not_fatal(self):
        out = self.wk(resp(200, yahoo_body("1wk", 7, 700, null_at=(5, 6), tail_dup=True)))
        self.assertIn("null", out["note"])
        self.assertIn("去重", out["note"])
        self.assertEqual(out["metrics"]["nulls"], 2)

    def test_first_trade_date_before_1970_does_not_crash(self):
        out = self.wk(resp(200, yahoo_body("1wk", 7, 700, first_trade=-1325583000)))
        self.assertEqual(out["metrics"]["firstTradeDate"], "1927-12-30")
        self.assertEqual(P.ts_to_date(-1325583000), "1927-12-30")

    def test_period1_zero_cutoff_is_mentioned_when_yahoo_has_older_data(self):
        end = int(NOW.timestamp()) - 3 * DAY
        out = self.wk(resp(200, yahoo_body("1wk", 7, end // (7 * DAY) + 1, end=end)))
        self.assertTrue(out["earliest"].startswith("1970-01"), out["earliest"])
        self.assertIn("1927-12-30", out["note"])

    def test_stale_daily_series_is_degraded(self):
        body = yahoo_body("1d", 1, 1260, end=int(NOW.timestamp()) - 20 * DAY, last_value=17.5)
        with self.assertRaises(P.ProbeDegraded):
            P.check_yahoo(resp(200, body), "1d", 0.5, 4, 1200, value_range=(5, 100), max_age=6)

    def test_silly_value_is_degraded(self):
        body = yahoo_body("1d", 1, 1260, end=int(NOW.timestamp()) - DAY, last_value=0.02)
        with self.assertRaises(P.ProbeDegraded):
            P.check_yahoo(resp(200, body), "1d", 0.5, 4, 1200, value_range=(5, 100), max_age=6)


class TestFinMindCriteria(FrozenNow):
    def test_http_200_with_error_message_is_failed(self):
        with self.assertRaises(P.ProbeFail):
            P.finmind_rows(resp(200, finmind_body([{"date": "2026-09-18"}], msg="dataset not found", status=400)))

    def test_http_200_success_message_but_wrong_inner_status_is_failed(self):
        with self.assertRaises(P.ProbeFail):
            P.finmind_rows(resp(200, finmind_body([{"date": "2026-09-18"}], msg="success", status=500)))

    def test_empty_data_is_failed_for_a_probe(self):
        with self.assertRaises(P.ProbeFail):
            P.finmind_rows(resp(200, finmind_body([])))

    def test_402_and_403_are_the_stop_signal(self):
        for code in (402, 403):
            with self.assertRaises(P.FinMindBlocked):
                P.finmind_rows(resp(code, {"msg": "x", "status": code}))
            with self.assertRaises(P.FinMindBlocked):
                P.finmind_rows(resp(200, {"msg": "x", "status": code}))

    def test_missing_field_is_failed(self):
        rows = [{"date": d, "stock_id": "2330", "PER": 25.0} for d in daily_dates(2005, 21)]
        with self.assertRaises(P.ProbeFail) as cm:
            P.check_finmind_per(resp(200, finmind_body(rows)), {})
        self.assertIn("PBR", str(cm.exception))

    def test_stale_rows_are_degraded(self):
        rows = [{"date": "%04d-06-15" % y, "stock_id": "2330", "dividend_yield": 1.5, "PER": 25.0, "PBR": 7.0}
                for y in range(2005, 2026)]
        with self.assertRaises(P.ProbeDegraded):
            P.check_finmind_per(resp(200, finmind_body(rows)), {})

    def test_fx_sentinel_rows_are_counted_and_excluded_from_the_start_date(self):
        payload, _, _ = self.run_all(only={"finmind"})
        r = self.state_of(payload, "F-07")
        self.assertEqual(r["state"], P.OK)
        self.assertEqual(r["metrics"]["sentinelRows"], 20)
        self.assertNotEqual(r["earliest"][:4], "2006")                      # 起點是第一筆有效值，不是 -1 那幾列
        self.assertIn("-1", r["note"])

    def test_fx_with_too_few_valid_rows_is_degraded(self):
        rows = [{"date": recent(i), "spot_buy": 4.3, "spot_sell": 4.4, "cash_buy": 4.2, "cash_sell": 4.5}
                for i in range(1, 200)]
        with self.assertRaises(P.ProbeDegraded):
            P.make_check_finmind_fx(1200, "CNY")(resp(200, finmind_body(rows)), {})

    def test_month_revenue_reports_the_revenue_month_not_the_row_date(self):
        payload, _, _ = self.run_all(only={"finmind"})
        r = self.state_of(payload, "F-03")
        self.assertEqual(r["state"], P.OK)
        self.assertEqual(r["metrics"]["latestRevenueMonth"], "2026-08")
        self.assertEqual(r["metrics"]["latestRowDate"], "2026-09-01")
        self.assertTrue(r["metrics"]["dateIsFirstOfNextMonth"])

    def test_short_bond_history_is_degraded(self):
        rows = [{"date": d, "name": "United States 20-Year", "value": 4.9} for d in daily_dates(2021, 5)]
        with self.assertRaises(P.ProbeDegraded) as cm:
            P.check_finmind_bond(resp(200, finmind_body(rows)), {})
        self.assertIn("FRED", str(cm.exception))


class TestFredCriteria(FrozenNow):
    def check(self, body, status=200, series="DGS20", min_rows=5, rng=(0, 20), age=7):
        return P.make_check_fred(series, min_rows, rng, age)(resp(status, body, {"Content-Type": "text/csv"}), {})

    def test_blank_and_dot_both_count_as_missing(self):
        out = self.check(fred_csv("DGS20", [("2026-09-10", "4.9"), ("2026-09-11", ""), ("2026-09-14", "."),
                                            ("2026-09-15", "4.8"), ("2026-09-16", "4.8"), ("2026-09-17", "4.9"),
                                            (recent(3), "4.95")]))
        self.assertEqual(out["metrics"]["emptyRows"], 2)
        self.assertEqual(out["points"], 5)
        self.assertEqual(out["latest"], recent(3))

    def test_html_instead_of_csv_is_failed(self):
        with self.assertRaises(P.ProbeFail):
            self.check("<!DOCTYPE html><html><title>FRED</title></html>")

    def test_wrong_series_in_header_is_failed(self):
        with self.assertRaises(P.ProbeFail):
            self.check(fred_csv("DGS10", [(recent(3), "4.1")] * 6))

    def test_latest_value_far_in_the_past_is_degraded(self):
        with self.assertRaises(P.ProbeDegraded):
            self.check(fred_csv("DGS20", [("2023-01-0%d" % i, "4.0") for i in range(1, 8)]))

    def test_too_few_rows_is_degraded(self):
        with self.assertRaises(P.ProbeDegraded):
            self.check(fred_csv("DGS20", [(recent(3), "4.9")]))

    def test_fred_requests_use_an_honest_user_agent_without_email(self):
        ua = [it["req"]({}).headers["User-Agent"] for it in P.build_items(P.load_yahoo_symbols(), NOW)
              if it["group"] == "fred"]
        self.assertEqual(len(ua), 3)
        for u in ua:
            self.assertNotIn("Mozilla", u)
            self.assertNotIn("@", u)


class TestCapeCriteria(FrozenNow):
    def test_two_places_agree_is_ok(self):
        ctx = {}
        out = P.check_multpl_current(resp(200, MULTPL_PAGE), ctx)
        self.assertEqual(ctx["capeCurrent"], 40.94)
        self.assertIn("估算", out["note"])

    def test_two_places_disagree_is_degraded(self):
        with self.assertRaises(P.ProbeDegraded):
            P.check_multpl_current(resp(200, MULTPL_PAGE.replace("\n40.94 ", "\n39.10 ")), {})

    def test_only_one_place_is_degraded(self):
        with self.assertRaises(P.ProbeDegraded):
            P.check_multpl_current(resp(200, MULTPL_PAGE.replace('id="current"', 'id="cur"')), {})

    def test_challenge_page_is_failed_even_with_http_200(self):
        with self.assertRaises(P.ProbeFail):
            P.check_multpl_current(resp(200, "<html><title>Just a moment...</title>cf-challenge</html>"), {})

    def test_table_ok(self):
        out = P.check_multpl_table(resp(200, multpl_table()), {"capeCurrent": 40.94})
        self.assertEqual(out["points"], 1870)
        self.assertEqual(out["earliest"][:4], "1871")
        self.assertEqual(out["latest"], "2026-09-18")

    def test_table_with_a_zero_is_degraded(self):
        with self.assertRaises(P.ProbeDegraded):
            P.check_multpl_table(resp(200, multpl_table(zero_at=400)), {})

    def test_short_table_is_degraded(self):
        with self.assertRaises(P.ProbeDegraded):
            P.check_multpl_table(resp(200, multpl_table(n_rows=120)), {})

    def test_fallbacks_are_skipped_when_multpl_works_and_run_when_it_does_not(self):
        payload, net, _ = self.run_all(only={"cape"})
        self.assertEqual(len(net.requests), 2)
        for key in ("C-03", "C-04", "C-05"):
            self.assertEqual(self.state_of(payload, key)["state"], P.SKIPPED)
        home = '<a href="https:\\/\\/img1.wsimg.com\\/blobby\\/go\\/e5e7\\/downloads\\/02d6\\/ie_data.xls?ver=1">data</a>'
        payload, net, _ = self.run_all(only={"cape"}, overrides={
            "multpl.com": resp(403, "<html><title>403 Forbidden</title></html>"),
            "econ.yale.edu": resp(200, "", {"Last-Modified": "Tue, 10 Oct 2023 00:00:00 GMT"}),
            "shillerdata.com": resp(200, home),
            "img1.wsimg.com": resp(200, "", {"Last-Modified": "Mon, 07 Sep 2026 00:00:00 GMT"})})
        states = dict((r["key"], r["state"]) for r in payload["results"])
        self.assertEqual(states["C-01"], P.FAILED)
        self.assertEqual((states["C-03"], states["C-04"], states["C-05"]), (P.DEGRADED,) * 3)
        methods = dict((P.host_of(r.url), r.method) for r in net.requests)
        self.assertEqual(methods["www.econ.yale.edu"], "HEAD")              # .xls 只看標頭，不下載
        self.assertEqual(methods["img1.wsimg.com"], "HEAD")


class TestCpiCriteria(FrozenNow):
    def test_dgbas_block_is_parsed(self):
        out = P.check_dgbas_xml(resp(200, dgbas_xml(), {"Content-Length": "16462137"}, truncated=True), {})
        self.assertEqual((out["earliest"], out["latest"], out["points"]), ("1981-01", "2026-08", 548))
        self.assertEqual(out["metrics"]["lastValue"], 112.32)
        self.assertEqual(out["metrics"]["baseText"], "指數基期：民國110年=100")

    def test_growth_rate_rows_are_not_mistaken_for_the_index(self):
        out = P.check_dgbas_xml(resp(200, dgbas_xml()), {})
        self.assertEqual(out["points"], 548)                                # 不是 1096

    def test_block_missing_is_degraded(self):
        with self.assertRaises(P.ProbeDegraded):
            P.check_dgbas_xml(resp(200, dgbas_xml().replace("總指數", "某指數")), {})

    def test_old_data_is_degraded(self):
        with self.assertRaises(P.ProbeDegraded):
            P.check_dgbas_xml(resp(200, dgbas_xml(months=530)), {})         # 最新只到 2025 年 2 月

    def test_stop_rule(self):
        self.assertFalse(P.dgbas_stop(dgbas_xml(second_item=False).encode("utf-8")))
        self.assertTrue(P.dgbas_stop(dgbas_xml(second_item=True).encode("utf-8")))

    def test_nstatdb_long_range_is_parsed(self):
        ctx = {}
        out = P.check_nstatdb(resp(200, nstatdb_body()), ctx, min_months=500)
        self.assertEqual((out["earliest"], out["latest"], out["points"]), ("1981-01", "2026-08", 548))
        self.assertEqual(out["metrics"]["lastValue"], 112.32)
        self.assertEqual(ctx["nstatdbLast"], ("2026-08", 112.32))

    def test_nstatdb_unpublished_month_comes_back_as_zero_and_is_dropped(self):
        # 2026-09-21 實測：問到 2026-09（還沒公布），它回 HTTP 200、值 0.0。最新一筆必須是 2026-08，不是 2026-09＝0。
        body = nstatdb_body(months=549, last_value=0.0)
        out = P.check_nstatdb(resp(200, body), {}, min_months=500)
        self.assertEqual((out["latest"], out["points"]), ("2026-08", 548))
        self.assertNotEqual(out["metrics"]["lastValue"], 0.0)
        self.assertEqual(out["metrics"]["unpublishedZeroMonths"], ["2026-09"])
        self.assertIn("陷阱", out["note"])

    def test_nstatdb_zero_in_the_middle_is_degraded(self):
        body = nstatdb_body()
        body["data"]["dataSets"][0]["series"]["0"]["observations"]["300"] = [0.0]
        with self.assertRaises(P.ProbeDegraded) as cm:
            P.check_nstatdb(resp(200, body), {})
        self.assertIn("中間", str(cm.exception))

    def test_nstatdb_html_shell_is_failed_even_with_http_200(self):
        with self.assertRaises(P.ProbeFail):
            P.check_nstatdb(resp(200, NSTATDB_HTML_SHELL), {})

    def test_nstatdb_waf_page_is_failed(self):
        with self.assertRaises(P.ProbeFail) as cm:
            P.check_nstatdb(resp(200, "<html><head><title>Request Rejected</title></head></html>"), {})
        self.assertIn("防火牆", str(cm.exception))

    def test_nstatdb_json_without_values_is_failed_not_ok(self):
        # 只看「是 JSON、裡面有數字」會把錯誤訊息當成資料；一定要真的解析出月份與值
        with self.assertRaises(P.ProbeFail):
            P.check_nstatdb(resp(200, {"meta": {"code": 100.25}, "data": {"dataSets": []}}), {})

    def test_nstatdb_wrong_series_short_range_and_stale_are_degraded(self):
        for body, kw in ((nstatdb_body(series="一.食物類"), {}),
                         (nstatdb_body(months=8, start=(2026, 1)), {"min_months": 500}),
                         (nstatdb_body(months=530), {}),                      # 最新只到 2025 年 2 月
                         (nstatdb_body(drop_last_obs=True), {})):             # 宣告的月份比值多
            with self.assertRaises(P.ProbeDegraded):
                P.check_nstatdb(resp(200, body), {}, **kw)

    def test_short_range_is_only_tried_when_the_long_range_did_not_work(self):
        payload, net, _ = self.run_all(only={"cpi"})
        self.assertEqual(self.state_of(payload, "T-03")["state"], P.OK)
        self.assertEqual(self.state_of(payload, "T-04")["state"], P.SKIPPED)
        urls = [r.url for r in net.requests if "nstatdb" in r.url]
        self.assertEqual(len(urls), 1)
        self.assertIn("startTime=1981-01&endTime=2026-09", urls[0])
        payload, net, _ = self.run_all(only={"cpi"}, overrides={
            "startTime=1981-01": resp(200, NSTATDB_HTML_SHELL, {"Content-Type": "text/html"}),
            "nstatdb": resp(200, nstatdb_body(months=12, start=(2025, 9)))})
        self.assertEqual(self.state_of(payload, "T-03")["state"], P.FAILED)
        self.assertEqual(self.state_of(payload, "T-04")["state"], P.OK)
        urls = [r.url for r in net.requests if "nstatdb" in r.url]
        self.assertEqual(len(urls), 2)
        self.assertIn("startTime=2025-09&endTime=2026-08", urls[1])

    def test_metadata_lookup_does_not_pretend_to_have_a_data_date(self):
        payload, _, _ = self.run_all(only={"cpi"})
        r = self.state_of(payload, "T-01")
        self.assertEqual(r["state"], P.OK)
        self.assertIsNone(r["latest"])
        self.assertIn("沒有資料日期", r["note"])

    def test_download_url_comes_from_the_metadata_but_only_if_it_is_dgbas(self):
        ctx = {}
        P.check_datagov_meta(resp(200, {"result": {"distribution": [
            {"resourceDownloadUrl": "https://ws.dgbas.gov.tw/001/Upload/new/path/pr0101a1m.xml"}]}}), ctx)
        self.assertIn("/new/path/", ctx["dgbasXmlUrl"])
        with self.assertRaises(P.ProbeFail):
            P.check_datagov_meta(resp(200, {"result": {"distribution": [
                {"resourceDownloadUrl": "https://evil.example.com/pr0101a1m.xml"}]}}), {})


class TestNavCriteria(FrozenNow):
    def test_all_etf_ok_and_mixed_types_are_handled(self):
        out = P.check_all_etf(resp(200, all_etf_body()), {})
        self.assertEqual(out["latest"], "2026-09-21")
        self.assertEqual(out["metrics"]["etfs"]["00679B"]["prevOfficialNav"], 25.7842)
        self.assertIn("估算", out["note"])

    def test_nav_not_settled_is_degraded(self):
        with self.assertRaises(P.ProbeDegraded) as cm:
            P.check_all_etf(resp(200, all_etf_body(h_00646="未結出")), {})
        self.assertIn("未結出", str(cm.exception))

    def test_etf_missing_is_failed(self):
        body = all_etf_body()
        body["a1"][0]["msgArray"].pop()
        with self.assertRaises(P.ProbeFail):
            P.check_all_etf(resp(200, body), {})

    def test_twse_chart_ok_and_stale_is_degraded(self):
        out = P.check_twse_etf_chart(resp(200, twse_chart_body()), {})
        self.assertEqual(out["points"], 740)
        with self.assertRaises(P.ProbeDegraded):
            P.check_twse_etf_chart(resp(200, twse_chart_body(last_gap_days=30)), {})

    def test_twse_chart_empty_is_failed(self):
        with self.assertRaises(P.ProbeFail):
            P.check_twse_etf_chart(resp(200, {"netPrice": [], "atmps": []}), {})

    def test_tpex_is_never_better_than_degraded(self):
        with self.assertRaises(P.ProbeDegraded) as cm:
            P.check_tpex_etf(resp(200, tpex_body()), {})
        self.assertIn("30", str(cm.exception))

    def test_tpex_cloudflare_page_is_failed(self):
        with self.assertRaises(P.ProbeFail):
            P.check_tpex_etf(resp(200, "<html><title>Just a moment...</title></html>"), {})


# --------------------------------------------------------------------------
# 4. 對照列、結果表、永遠 exit 0、不寫倉庫
# --------------------------------------------------------------------------

class TestControlsAndOutput(FrozenNow):
    def test_full_run_states(self):
        payload, net, out = self.run_all()
        states = dict((r["key"], r["state"]) for r in payload["results"])
        self.assertEqual(states["Y-00"], P.DEGRADED)                        # 對照列：range=max → 月線
        self.assertEqual(states["Y-07"], P.FAILED)                          # 對照列：00679B.TW → 404
        self.assertEqual(states["Y-06"], P.OK)
        self.assertFalse(payload["controlsBroken"])
        self.assertEqual(states["F-09"], P.SKIPPED)
        self.assertEqual(states["N-03"], P.DEGRADED)
        for r in payload["results"]:
            self.assertIn(r["state"], P.STATES)
        self.assertEqual(sum(payload["tally"].values()), len(payload["results"]))
        self.assertEqual(payload["requestsSent"], len(net.requests))

    def test_00679b_symbol_comes_from_assets_json(self):
        payload, net, _ = self.run_all(only={"yahoo"})
        urls = [r.url for r in net.requests]
        self.assertTrue([u for u in urls if "/00679B.TWO?" in u])
        self.assertEqual(P.load_yahoo_symbols()["tw00679b"], "00679B.TWO")
        self.assertTrue([u for u in urls if "period1=0&period2=" in u and "GC%3DF" in u])
        self.assertEqual(len([u for u in urls if "range=max" in u]), 1)     # 只有對照列用規格原寫法

    def test_broken_criteria_are_shouted_at_the_top(self):
        # 假設哪天判準壞了（或 Yahoo 變了）：兩列對照列都回「真的週線」
        payload, _, out = self.run_all(only={"yahoo"}, overrides={
            "range=max": resp(200, yahoo_body("1wk", 7, 700)),
            "00679B.TW?": resp(200, yahoo_body("1d", 1, 5, end=int(NOW.timestamp()) - DAY))})
        self.assertTrue(payload["controlsBroken"])
        self.assertIn("探測判準失效", out)
        md = P.render_summary(payload)
        self.assertLess(md.index("探測判準失效"), md.index("| # |"))         # 在表格上面
        self.assertIn("Y-00", md.split("| # |")[0])
        self.assertIn("Y-07", md.split("| # |")[0])

    def test_control_verdict_logic(self):
        mk = lambda key, expect, state: {"key": key, "expect": expect, "state": state}   # noqa: E731
        broken, _ = P.control_verdict([mk("Y-00", P.DEGRADED, P.DEGRADED), mk("Y-07", P.FAILED, P.FAILED)])
        self.assertFalse(broken)
        broken, lines = P.control_verdict([mk("Y-00", P.DEGRADED, P.OK), mk("Y-07", P.FAILED, P.FAILED)])
        self.assertTrue(broken)
        broken, lines = P.control_verdict([mk("Y-00", P.DEGRADED, P.DEGRADED), mk("Y-07", P.FAILED, P.OK)])
        self.assertTrue(broken)
        broken, lines = P.control_verdict([mk("Y-00", P.DEGRADED, P.FAILED), mk("Y-07", P.FAILED, P.FAILED)])
        self.assertFalse(broken)
        self.assertIn("沒有證明力", lines[0])                                # 連不上不等於判準壞了，但要講清楚

    def test_summary_first_lines_carry_the_tally_and_every_row_has_a_state(self):
        payload, _, _ = self.run_all()
        md = P.render_summary(payload)
        head = md.split("| # |")[0]
        t = payload["tally"]
        self.assertIn("可用 %d／降級 %d／失敗 %d／未測 %d" % (t[P.OK], t[P.DEGRADED], t[P.FAILED], t[P.SKIPPED]), head)
        rows = [l for l in md.split("\n") if l.startswith("| ") and not l.startswith("| #") and "---" not in l]
        self.assertEqual(len(rows), len(payload["results"]))
        for l in rows:
            self.assertTrue(any("**%s**" % s in l for s in P.STATES), l)
        self.assertIn("最新一筆", md)
        for name in ("大盤融資", "大盤三大法人", "NVDA 週線", "長區間日線"):
            self.assertIn(name, md)                                         # 決定不測的，表上要寫明

    def test_failures_emit_a_warning_line(self):
        _, _, out = self.run_all(only={"yahoo"})
        self.assertIn("::warning::Y-07", out)

    def test_public_output_has_no_machine_name_or_local_path(self):
        payload, _, out = self.run_all()
        blob = json.dumps(payload, ensure_ascii=False) + P.render_summary(payload) + out
        for secret in (os.environ.get("COMPUTERNAME"), os.environ.get("USERNAME"), P.ROOT, P.ROOT.replace("\\", "/")):
            if secret and len(secret) >= 4:
                self.assertNotIn(secret.lower(), blob.lower())
        self.assertIn(payload["where"], ("local", "actions"))

    def test_probe_result_is_one_parseable_line(self):
        _, _, out = self.run_all(only={"fred"})
        line = [l for l in out.split("\n") if l.startswith("PROBE_RESULT ")]
        self.assertEqual(len(line), 1)
        back = json.loads(line[0][len("PROBE_RESULT "):])
        self.assertEqual(len(back["results"]), 3)

    def test_a_crashing_check_only_fails_its_own_row(self):
        item = {"key": "X-2", "group": "cape", "spec": "-", "title": "壞掉的判準", "expect": None, "when": None,
                "req": lambda ctx: P.Req("https://www.multpl.com/x"), "check": lambda r, ctx: 1 / 0}
        rec = P.run_item(item, FakeNet({"multpl.com": resp(200, "x")}), {})
        self.assertEqual(rec["state"], P.FAILED)
        self.assertIn("檢查程式出錯", rec["note"])

    def test_unreachable_is_failed_with_the_reason(self):
        dead = P.Resp(None, {}, b"", 20.0, 1, "ConnectTimeout：timed out")
        payload, _, _ = self.run_all(only={"fred"}, overrides={"fred.stlouisfed.org": dead})
        for r in payload["results"]:
            self.assertEqual(r["state"], P.FAILED)
            self.assertIn("連不上", r["note"])


class TestAlwaysExitZeroAndNoRepoWrites(FrozenNow):
    def test_exit_zero_when_everything_fails(self):
        class DeadNet(object):
            def __init__(self, *a, **kw):
                pass

            def fetch(self, req):
                return P.Resp(None, {}, b"", 0.1, 1, "ConnectionError：offline")

        orig = P.Net
        P.Net = DeadNet
        try:
            with redirect_stdout(io.StringIO()) as out:
                code = P.main([])
        finally:
            P.Net = orig
        self.assertEqual(code, 0)
        self.assertIn("PROBE_RESULT", out.getvalue())
        self.assertIn("可用 0", out.getvalue())

    def test_exit_zero_even_when_the_probe_itself_blows_up(self):
        class ExplodingNet(object):
            def __init__(self, *a, **kw):
                raise RuntimeError("no network stack")

        orig, old_err = P.Net, sys.stderr
        P.Net, sys.stderr = ExplodingNet, io.StringIO()                     # 預期內的 traceback 不要灑在測試輸出上
        try:
            with redirect_stdout(io.StringIO()) as out:
                code = P.main([])
        finally:
            P.Net, sys.stderr = orig, old_err
        self.assertEqual(code, 0)
        self.assertIn("探測程式本身出錯", out.getvalue())

    def test_exit_zero_on_bad_arguments(self):
        with redirect_stdout(io.StringIO()):
            err = io.StringIO()
            old = sys.stderr
            sys.stderr = err
            try:
                self.assertEqual(P.main(["--no-such-flag"]), 0)
                self.assertEqual(P.main(["--only", "nonsense"]), 0)
            finally:
                sys.stderr = old

    def test_partial_results_survive_a_crash_midway(self):
        calls = {"n": 0}

        def flaky(req):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt()
            return resp(200, fred_csv("DGS20", [(recent(3), "4.9")]), {"Content-Type": "text/csv"})

        tmp = tempfile.mkdtemp(prefix="iw-probe-test-")
        path = os.path.join(tmp, "summary.md")
        with redirect_stdout(io.StringIO()) as out:
            try:
                P.run_probe(FakeNet({"fred.stlouisfed.org": flaky}), only={"fred"}, summary_path=path, now=NOW)
            except KeyboardInterrupt:
                pass
        self.assertIn("PROBE_RESULT", out.getvalue())                       # finally 裡還是印了
        text = read_text(path)
        self.assertIn("R-01", text)

    def test_summary_inside_the_repo_is_refused(self):
        inside = os.path.join(P.ROOT, "data", "probe-summary.md")
        with redirect_stdout(io.StringIO()) as out:
            self.assertFalse(P.write_outside_repo(inside, "x"))
        self.assertFalse(os.path.exists(inside))
        self.assertIn("拒絕寫入", out.getvalue())

    def test_summary_outside_the_repo_is_written(self):
        tmp = tempfile.mkdtemp(prefix="iw-probe-test-")
        path = os.path.join(tmp, "s.md")
        with redirect_stdout(io.StringIO()):
            P.run_probe(FakeNet(), only={"nav"}, summary_path=path, now=NOW)
        text = read_text(path)
        self.assertIn("分析資料來源探測", text)
        self.assertIn("PROBE_RESULT", text)
        self.assertNotIn("探測進行中", text)                                # 跑完的版本不該留著「進行中」

    def test_dry_run_sends_nothing_and_lists_zero_bot_requests(self):
        class NoNet(object):
            def __init__(self, *a, **kw):
                raise AssertionError("dry-run 不應該建立連線")

        orig = P.Net
        P.Net = NoNet
        try:
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(P.main(["--dry-run"]), 0)
        finally:
            P.Net = orig
        text = out.getvalue()
        self.assertIn("台銀（bot.com.tw）：0 個", text)
        self.assertNotIn("會被白名單拒絕", text)
        self.assertIn("00679B.TWO", text)

    def test_compare_prints_both_environments_side_by_side(self):
        tmp = tempfile.mkdtemp(prefix="iw-probe-test-")
        a, _, _ = self.run_all(only={"fred"})
        b, _, _ = self.run_all(only={"fred"}, overrides={
            "id=T10Y2Y": P.Resp(None, {}, b"", 20.0, 1, "ReadTimeout：timed out")})
        b["where"] = "actions"
        pa, pb = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
        write_text(pa, json.dumps(a, ensure_ascii=False))
        write_text(pb, "noise\nPROBE_RESULT " + json.dumps(b, ensure_ascii=False) + "\n")
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(P.main(["--compare", pa, pb]), 0)
        text = out.getvalue()
        self.assertIn("狀態不同", text)
        self.assertIn("一致", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
