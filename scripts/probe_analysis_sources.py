#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_analysis_sources.py — 分析系列 停點 A0：新資料來源探測（只測不接）

這支程式只做一件事：對「分析系列 A1～A4 可能會用到的新資料來源」各發一個請求，
記下抓不抓得到、格式長怎樣、能回溯到哪一年、最新一筆是哪一天。
它【不寫倉庫裡的任何檔案、不改任何設定、不接任何來源進正式抓取】；
結果只進 job summary（或你指定的、倉庫以外的檔案）與螢幕上的一行 PROBE_RESULT。

幾條寫死的規矩（每一條在 scripts/test_probe_analysis.py 都有測試釘住）：

  1. 【永遠 exit 0】探測失敗是「結果」，不是「程式錯」。但綠燈不可以騙人，所以：
       * 每一項的狀態四選一：可用／降級／失敗／未測，沒有空白。
         「降級」＝抓得到，但不合需求（粒度被改、回溯不夠、資料太舊、只有估算值）。
       * summary 第一行就是四種狀態的統計；每個失敗另外發一行 ::warning::。
       * 測完一項就寫一列 summary；最外層 try/finally，中途出事也留得下已測的部分。
       * 「可用」的判準逐項寫死在這支程式裡，【不可以只看 HTTP 200】。
       * 表裡有兩列活的對照列：00679B.TW（規格寫錯的代號，必須「失敗」）與
         GC=F range=max&interval=1wk（規格原寫法，Yahoo 會無聲地把週線換成月線，必須「降級」）。
         任何一列被判成「可用」，summary 頂端會印「探測判準失效」。
  2. 【台銀碰不到】主機白名單：不在名單上的主機直接拒絕、連線都不發；bot.com.tw 永遠拒絕。
     轉址也逐跳檢查。
  3. 【對來源克制】每個來源一個請求；所有請求之間固定等 3 秒（證交所 3.5 秒）；
     FinMind 一律不重試，第一個 402／403 就整組改記「未測」（連續 4xx 會被它封 IP）；
     只有 Yahoo 遇到 429 會等 10 秒重試一次；大檔用串流讀、讀到需要的那一段就斷線。
  4. 【公開的輸出不帶個資】不記電腦名稱、不印本機路徑；不讀任何持倉相關的檔案，
     只讀 data/assets.json 的 Yahoo 代號。

用法：
    python scripts/probe_analysis_sources.py --dry-run                 # 只列出會發哪些請求，不連網
    python scripts/probe_analysis_sources.py --summary <倉庫外的路徑>   # 正式探測
    python scripts/probe_analysis_sources.py --only finmind,fred       # 只測某幾組（修 bug 後重測用）
    python scripts/probe_analysis_sources.py --compare a.json b.json   # 兩個環境的結果並排（不連網）

組別：yahoo、finmind、fred、cape、cpi、nav。
正式探測前請先跑離線測試；離線測試紅就不要探測：
    python -m unittest discover -s scripts -p "test_probe_analysis.py"
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote as url_quote, urlsplit

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

TPE = timezone(timedelta(hours=8))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
WHITESPACE = " " + chr(13) + chr(10) + chr(9)

OK, DEGRADED, FAILED, SKIPPED = "可用", "降級", "失敗", "未測"
STATES = (OK, DEGRADED, FAILED, SKIPPED)
GROUPS = ("yahoo", "finmind", "fred", "cape", "cpi", "nav")

GAP_SECONDS = 3.0                 # 所有請求之間固定等這麼久（不分主機，最簡單也一定符合「同站 2 秒以上」）
GAP_SECONDS_TWSE = 3.5            # PLAN 第 7 章：證交所 3 秒以上
TIMEOUT = 20

# 主機白名單。不在這裡的主機一律不送；轉址也逐跳檢查。
ALLOWED_HOSTS = (
    "query1.finance.yahoo.com",
    "api.finmindtrade.com",
    "fred.stlouisfed.org",
    "www.multpl.com",
    "www.econ.yale.edu", "shillerdata.com", "img1.wsimg.com",       # 只在 multpl 失敗時才會用到，而且只做 HEAD
    "data.gov.tw", "ws.dgbas.gov.tw", "nstatdb.dgbas.gov.tw",
    "mis.twse.com.tw", "www.twse.com.tw", "info.tpex.org.tw",
)
# 就算哪天有人手滑把它加進白名單，這一條照樣擋：這個系列不准增加對台銀的任何請求。
FORBIDDEN_HOST_PARTS = ("bot.com.tw",)

# FRED 會把「自稱 Chrome、其實不是瀏覽器」的連線吊住 20 多秒到逾時（2026-09-21 查證），
# 用誠實的 User-Agent 反而 2 秒多就回。不放 email：這個倉庫是公開的。
HONEST_UA = "invest-watch-probe/1.0 (+https://github.com/davidjjx/invest-watch)"

DGBAS_XML_FALLBACK = "https://ws.dgbas.gov.tw/001/Upload/461/relfile/11525/230555/pr0101a1m.xml"
DGBAS_CAP = 400 * 1024            # 整檔 15.7MB；「總指數」排在最前面約 183KB，讀到 400KB 還找不到就算降級


class ProbeFail(Exception):
    """這個來源這次不能用（原因寫在訊息裡）。"""


class ProbeDegraded(Exception):
    """抓得到，但不合需求。訊息是原因，detail 是已經量到的東西。"""

    def __init__(self, message, detail=None):
        Exception.__init__(self, message)
        self.detail = detail or {}


class FinMindBlocked(ProbeFail):
    """FinMind 回 402（超量）或 403（封 IP）：整組停手，不再發任何請求。"""


class HostNotAllowed(Exception):
    pass


# ==========================================================================
# 連線：白名單、固定間隔、不亂重試
# ==========================================================================

def host_of(url):
    """網址真正會連到的主機。帶帳密的寫法（https://好主機@壞主機/）看起來像好主機、實際連到壞主機，
    一律當成「沒有主機」，白名單自然會拒絕。"""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return ""
    return (parts.hostname or "").lower()


def assert_host_allowed(url):
    host = host_of(url)
    for bad in FORBIDDEN_HOST_PARTS:
        if bad in host:
            raise HostNotAllowed("這個系列不准對台銀發任何請求（%s）" % host)
    if host not in ALLOWED_HOSTS:
        raise HostNotAllowed("主機不在白名單裡，拒絕送出（%s）" % (host or "?"))
    return host


class Req(object):
    def __init__(self, url, method="GET", headers=None, data=None, timeout=TIMEOUT,
                 cap=None, stop=None, retry429=False):
        self.url, self.method, self.headers, self.data = url, method, headers or {}, data
        self.timeout, self.cap, self.stop, self.retry429 = timeout, cap, stop, retry429


class Resp(object):
    def __init__(self, status=None, headers=None, body=b"", seconds=0.0, requests_made=0,
                 error=None, truncated=False):
        self.status, self.headers, self.body = status, headers or {}, body
        self.seconds, self.requests_made, self.error, self.truncated = seconds, requests_made, error, truncated

    def text(self):
        return self.body.decode("utf-8", "replace")

    def header(self, name):
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return None


class Net(object):
    """真的連網的那一層。測試時換成不連網的替身（只要有 fetch(req) 就行）。"""

    def __init__(self, sleep=time.sleep, clock=time.time, session=None):
        if session is None:
            import requests                          # 只有真的要連網才需要
            session = requests.Session()
        self._session = session
        self._last = None
        self._sleep = sleep
        self._clock = clock
        self.sent = []                               # 每一個真的送出去的 (method, url)

    def _pace(self, host):
        gap = GAP_SECONDS_TWSE if host.endswith("twse.com.tw") else GAP_SECONDS
        if self._last is not None:
            wait = gap - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)

    def _once(self, req, url):
        host = assert_host_allowed(url)              # 每一跳都檢查，轉址也不例外
        self._pace(host)
        self.sent.append((req.method, url))          # 走到這裡就算「對外發了一次」，連不上也算
        kw = dict(headers=req.headers, timeout=req.timeout, allow_redirects=False)
        try:
            if req.method == "HEAD":
                return self._session.head(url, **kw)
            if req.method == "POST":
                return self._session.post(url, data=req.data, **kw)
            return self._session.get(url, stream=bool(req.cap), **kw)
        finally:
            self._last = self._clock()

    def fetch(self, req):
        before = len(self.sent)
        waited = [0.0]
        orig_sleep = self._sleep

        def counting_sleep(sec):                     # 禮貌性的等待不算進「這個來源回得多快」
            waited[0] += sec
            orig_sleep(sec)

        self._sleep = counting_sleep
        t0 = self._clock()
        elapsed = lambda: max(0.0, self._clock() - t0 - waited[0])     # noqa: E731
        made = lambda: len(self.sent) - before                          # noqa: E731  連不上也算一次
        try:
            url, r, retried = req.url, None, False
            for _hop in range(4):
                r = self._once(req, url)
                if r.status_code == 429 and req.retry429 and not retried:
                    retried = True
                    r.close()
                    print("      . 429（請求過於頻繁），等 10 秒重試一次")
                    self._sleep(10)
                    continue
                loc = r.headers.get("Location")
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    r.close()
                    url = loc if re.match(r"^https?://", loc) else re.sub(r"^(https?://[^/]+).*$", r"\1", url) + loc
                    continue
                break
            body, truncated = b"", False
            if req.method != "HEAD":
                if req.cap:
                    checked = 0
                    for chunk in r.iter_content(16384):
                        body += chunk
                        if len(body) - checked >= 65536 or len(body) >= req.cap:
                            checked = len(body)
                            if (req.stop and req.stop(body)) or len(body) >= req.cap:
                                truncated = True
                                break
                    r.close()                         # 讀到需要的那一段就斷線，不把整檔拉完
                else:
                    body = r.content
            return Resp(r.status_code, dict(r.headers), body, elapsed(), made(), None, truncated)
        except HostNotAllowed:
            raise
        except Exception as e:
            return Resp(None, {}, b"", elapsed(), made(), "%s：%s" % (e.__class__.__name__, short_error(e)))
        finally:
            self._sleep = orig_sleep


def short_error(e):
    """錯誤訊息留頭也留尾：真正的原因（例如憑證驗不過）通常寫在最後面。"""
    msg = re.sub(r"\s+", " ", str(e))
    return msg if len(msg) <= 260 else msg[:100] + " …… " + msg[-150:]


# ==========================================================================
# 小工具
# ==========================================================================

def now_tpe():
    return datetime.now(TPE)


def ts_to_date(ts, gmtoffset=0):
    """epoch 秒 → 交易所當地日期。不用 datetime.fromtimestamp：Windows 遇到 1970 年以前的
    負數會丟 OSError（^GSPC 的 firstTradeDate 是 1927 年），那會變成「雲端成功、筆電失敗」的假差異。"""
    return (EPOCH + timedelta(seconds=int(ts) + int(gmtoffset or 0))).strftime("%Y-%m-%d")


def days_old(date_str, now=None):
    now = now or now_tpe()
    d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    return (now.date() - d).days


def years_between(a, b):
    da = datetime.strptime(a[:10], "%Y-%m-%d")
    db = datetime.strptime(b[:10], "%Y-%m-%d")
    return (db - da).days / 365.25


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def parse_json(resp):
    try:
        return json.loads(resp.text())
    except ValueError:
        raise ProbeFail("回應不是 JSON")


def need_200(resp):
    if resp.status != 200:
        raise ProbeFail("HTTP %s" % resp.status)


def fingerprint(resp):
    """失敗時留下「對方到底回了什麼」：狀態碼、型別、標題、開頭一小段。不留整份內容。"""
    if resp is None:
        return None
    if resp.status is None:
        return {"status": None, "error": resp.error}
    text = resp.text()[:4000]
    m = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    return {
        "status": resp.status,
        "contentType": resp.header("Content-Type"),
        "server": resp.header("Server"),
        "title": re.sub(r"\s+", " ", m.group(1)).strip()[:80] if m else None,
        "head": re.sub(r"\s+", " ", text[:160]).strip(),
    }


# ==========================================================================
# 各來源的判準（「可用」的定義寫死在這裡；只看 HTTP 200 一律不算）
# ==========================================================================

# ---- Yahoo chart ---------------------------------------------------------

def yahoo_series(resp):
    j = None
    try:
        j = json.loads(resp.text())
    except ValueError:
        pass
    chart = (j or {}).get("chart") or {}
    err = chart.get("error")
    if err:
        raise ProbeFail("Yahoo 回報錯誤：%s／%s（HTTP %s）"
                        % (err.get("code"), err.get("description"), resp.status))
    need_200(resp)
    if j is None:
        raise ProbeFail("回應不是 JSON")
    res = (chart.get("result") or [None])[0]
    if not res:
        raise ProbeFail("chart.result 是空的")
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    closes = ((((res.get("indicators") or {}).get("quote")) or [{}])[0] or {}).get("close") or []
    off = meta.get("gmtoffset") or 0
    pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
    if not pairs:
        raise ProbeFail("沒有任何一根有收盤價")
    gaps = [(b - a) / 86400.0 for a, b in zip(ts, ts[1:])]
    tail_dup = (len(pairs) >= 2 and pairs[-1][1] == pairs[-2][1]
                and (pairs[-1][0] - pairs[-2][0]) < 6.5 * 86400)
    ftd = meta.get("firstTradeDate")
    return {
        "granularity": meta.get("dataGranularity"), "range": meta.get("range"),
        "points": len(pairs), "nulls": len(ts) - len(pairs),
        "earliest": ts_to_date(pairs[0][0], off), "latest": ts_to_date(pairs[-1][0], off),
        "medianGapDays": round(median(gaps), 2) if gaps else None,
        "tailDuplicate": bool(tail_dup),
        "firstTradeDate": ts_to_date(ftd, off) if isinstance(ftd, (int, float)) else None,
        "gmtoffset": off, "rawFirst2": ts[:2], "rawLast2": ts[-2:],
        "nonPositive": sum(1 for _t, c in pairs if c <= 0),
        "lastValue": pairs[-1][1], "currency": meta.get("currency"),
        "exchange": meta.get("exchangeName"),
    }


def check_yahoo(resp, want, lo_gap, hi_gap, min_points, min_years=None, value_range=None, max_age=None):
    m = yahoo_series(resp)
    base = {"earliest": m["earliest"], "latest": m["latest"], "points": m["points"],
            "fields": ["timestamp", "close"], "metrics": m}
    notes = []
    if m["nulls"]:
        notes.append("收盤價有 %d 個 null（整段休市），要濾掉" % m["nulls"])
    if m["tailDuplicate"]:
        notes.append("尾端多一個跟前一根同值的即時點，算的時候要去重")
    if m["nonPositive"]:
        notes.append("有 %d 根 ≤0 的價格" % m["nonPositive"])
    if m["firstTradeDate"] and m["firstTradeDate"] < "1970" and m["earliest"] < "1970-02":
        notes.append("period1=0 只回 1970 年以後（Yahoo 其實從 %s 就有，要更早得用負的 period1；分析用不到）"
                     % m["firstTradeDate"])
    # 第一道：Yahoo 自己宣告的粒度。range=max 時它會無聲地把週線換成月線，HTTP 仍是 200。
    if m["granularity"] != want:
        raise ProbeDegraded("要的是 %s，Yahoo 回的是 %s（HTTP 200、沒有任何錯誤訊息）"
                            % (want, m["granularity"]), base)
    # 第二道：實際的時間間距。宣告說是週線，間距也得真的是 7 天左右。
    g = m["medianGapDays"]
    if g is None or not (lo_gap <= g <= hi_gap):
        raise ProbeDegraded("宣告是 %s，但時間戳的中位間距是 %s 天" % (want, g), base)
    if m["points"] < min_points:
        raise ProbeDegraded("只有 %d 根（至少要 %d）" % (m["points"], min_points), base)
    if max_age is not None and days_old(m["latest"]) > max_age:
        raise ProbeDegraded("最新一筆是 %d 天前" % days_old(m["latest"]), base)
    if value_range and not (value_range[0] <= m["lastValue"] <= value_range[1]):
        raise ProbeDegraded("最新值 %s 不在合理範圍 %s" % (m["lastValue"], (value_range,)), base)
    if min_years is not None:
        yrs = years_between(m["earliest"], m["latest"])
        if yrs < min_years:
            notes.insert(0, "只有 %.1f 年，不足 %d 年：%d 年百分位屆時要顯示「資料不足」"
                         % (yrs, min_years, min_years))
            raise ProbeDegraded("；".join(notes), base)
    base["note"] = "；".join(notes)
    return base


# ---- FinMind -------------------------------------------------------------

def finmind_rows(resp, allow_empty=False):
    body = None
    try:
        body = json.loads(resp.text())
    except ValueError:
        pass
    inner = (body or {}).get("status") if isinstance(body, dict) else None
    if resp.status in (402, 403) or inner in (402, 403):
        raise FinMindBlocked("FinMind 回 %s：%s" % (resp.status if resp.status in (402, 403) else inner,
                                                  (body or {}).get("msg") if isinstance(body, dict) else ""))
    need_200(resp)
    if not isinstance(body, dict):
        raise ProbeFail("回應不是預期的 JSON")
    # FinMind 出錯時 HTTP 可能照樣是 200，錯誤只寫在 msg／status 裡：兩邊都要看。
    if body.get("msg") != "success" or body.get("status") != 200:
        raise ProbeFail("FinMind 回報失敗：msg=%r status=%r" % (body.get("msg"), body.get("status")))
    data = body.get("data")
    if not isinstance(data, list):
        raise ProbeFail("回應裡沒有 data 陣列")
    if not data and not allow_empty:
        raise ProbeFail("data 是空的")
    return data


def finmind_base(rows, date_key="date"):
    dates = sorted(str(r.get(date_key)) for r in rows if r.get(date_key))
    return {"earliest": dates[0] if dates else None, "latest": dates[-1] if dates else None,
            "points": len(rows), "fields": sorted(rows[0].keys()) if rows else []}


def need_fields(base, *names):
    missing = [n for n in names if n not in base["fields"]]
    if missing:
        raise ProbeFail("少了欄位：%s（實際欄位 %s）" % ("、".join(missing), "、".join(base["fields"])))


def fresh_or_degraded(base, max_age, extra_note=""):
    age = days_old(base["latest"])
    if age > max_age:
        raise ProbeDegraded("最新一筆 %s 是 %d 天前（門檻 %d 天）%s"
                            % (base["latest"], age, max_age, extra_note), base)


def check_finmind_datalist(resp, ctx):
    rows = finmind_rows(resp)
    names = [str(x) for x in rows]
    ctx["finmindHeaders"] = sorted(resp.headers.keys())
    base = {"earliest": None, "latest": None, "points": len(names), "fields": [],
            "metrics": {"dataIds": names, "responseHeaders": ctx["finmindHeaders"]}}
    if "United States 20-Year" not in names:
        raise ProbeDegraded("清單裡沒有 United States 20-Year：%s" % "、".join(names), base)
    quota = [h for h in ctx["finmindHeaders"] if re.search(r"limit|remain|quota", h, flags=re.I)]
    base["note"] = ("data_id 共 %d 個（全是美國公債）；回應標頭%s"
                    % (len(names), "有額度欄位：" + "、".join(quota) if quota
                       else "沒有任何額度欄位，用量限制只能依文件：免金鑰每小時 300 次"))
    return base


def check_finmind_bond(resp, ctx):
    rows = finmind_rows(resp)
    base = finmind_base(rows)
    need_fields(base, "date", "name", "value")
    vals = [(r["date"], r["value"]) for r in rows if isinstance(r.get("value"), (int, float))]
    ctx["finmindUS20"] = dict(vals[-15:])
    last = vals[-1][1] if vals else None
    base["metrics"] = {"lastValue": last}
    if last is None or not (0 <= last <= 20):
        raise ProbeDegraded("最新值 %s 不合理" % last, base)
    fresh_or_degraded(base, 7)
    yrs = years_between(base["earliest"], base["latest"])
    note = "最新 %s＝%s%%；只是 00679B 到期殖利率的【近似】，日後要標估算" % (base["latest"], last)
    if yrs < 10:
        raise ProbeDegraded("只回溯 %.1f 年（自 %s），算不了 10 年百分位，長歷史要靠 FRED；%s"
                            % (yrs, base["earliest"], note), base)
    base["note"] = "回溯 %.1f 年；%s" % (yrs, note)
    return base


def check_finmind_revenue(resp, ctx):
    rows = finmind_rows(resp)
    base = finmind_base(rows)
    need_fields(base, "revenue", "revenue_year", "revenue_month")
    last = sorted(rows, key=lambda r: str(r.get("date")))[-1]
    y, mth = int(last["revenue_year"]), int(last["revenue_month"])
    nxt = "%04d-%02d-01" % ((y + 1, 1) if mth == 12 else (y, mth + 1))
    now = now_tpe()
    lag = (now.year - y) * 12 + (now.month - mth)
    base["metrics"] = {"latestRevenueMonth": "%04d-%02d" % (y, mth), "latestRowDate": last.get("date"),
                       "dateIsFirstOfNextMonth": last.get("date") == nxt,
                       "createTime": last.get("create_time")}
    base["latest"] = "%04d-%02d（營收所屬月）" % (y, mth)
    if len(rows) < 37:
        raise ProbeDegraded("只有 %d 個月（年增率與連續月趨勢至少要 37 個月）" % len(rows), base)
    if lag > 2:
        raise ProbeDegraded("最新的營收所屬月份是 %d 個月前" % lag, base)
    base["note"] = ("陷阱：date 欄是【所屬月份的下個月 1 日】（date=%s 對應 %d 月營收%s），"
                    "月份要用 revenue_year／revenue_month；每月約 10 日前後才有新一筆"
                    % (last.get("date"), mth, "" if last.get("date") == nxt else "——這次實測不符，請人工確認"))
    return base


def check_finmind_per(resp, ctx):
    rows = finmind_rows(resp)
    base = finmind_base(rows)
    need_fields(base, "PER", "PBR", "dividend_yield")
    bad = sum(1 for r in rows if not isinstance(r.get("PER"), (int, float)) or r["PER"] <= 0)
    base["metrics"] = {"perNonPositive": bad}
    fresh_or_degraded(base, 5)
    yrs = years_between(base["earliest"], base["latest"])
    tail = "文件寫平日 18:00 才更新，15:30 那一輪拿到的會是前一個交易日"
    if yrs < 10:
        raise ProbeDegraded("只回溯 %.1f 年，10 年百分位不夠；%s" % (yrs, tail), base)
    base["note"] = "回溯 %.1f 年；PER≤0 的列 %d 筆（算百分位前要濾掉）；%s" % (yrs, bad, tail)
    return base


def check_finmind_margin(resp, ctx):
    rows = finmind_rows(resp)
    base = finmind_base(rows)
    need_fields(base, "MarginPurchaseTodayBalance", "ShortSaleTodayBalance")
    last = sorted(rows, key=lambda r: str(r.get("date")))[-1]
    base["metrics"] = {"latestMarginBalance": last.get("MarginPurchaseTodayBalance"),
                       "latestShortBalance": last.get("ShortSaleTodayBalance")}
    fresh_or_degraded(base, 5)
    yrs = years_between(base["earliest"], base["latest"])
    if yrs < 1:
        raise ProbeDegraded("只回溯 %.1f 年（「近一年新高」至少要 1 年）" % yrs, base)
    base["note"] = ("回溯 %.1f 年；最新融資餘額 %s——量級像【張】不是股，接之前要拿證交所的數字對一次；"
                    "文件寫平日 21:00 才更新，15:30 拿到的是前一個交易日"
                    % (yrs, last.get("MarginPurchaseTodayBalance")))
    return base


def check_finmind_institutional(resp, ctx):
    rows = finmind_rows(resp)
    base = finmind_base(rows)
    need_fields(base, "buy", "sell", "name")
    first_seen = {}
    for r in rows:
        n, d = str(r.get("name")), str(r.get("date"))
        if n not in first_seen or d < first_seen[n]:
            first_seen[n] = d
    base["metrics"] = {"namesFirstSeen": first_seen}
    for must in ("Foreign_Investor", "Investment_Trust"):
        if must not in first_seen:
            raise ProbeFail("name 裡沒有 %s（實際：%s）" % (must, "、".join(sorted(first_seen))))
    fresh_or_degraded(base, 5)
    note = ("長表（每天每類法人一列），name 共 %d 種、各自的起始日不同：%s；單位是【股】；"
            "文件寫平日 20:00 才更新，15:30 拿到的是前一個交易日"
            % (len(first_seen), "、".join("%s 自 %s" % kv for kv in sorted(first_seen.items(), key=lambda kv: kv[1]))))
    if resp.seconds > 20:
        raise ProbeDegraded("整段要 %.0f 秒才回完，太慢；%s" % (resp.seconds, note), base)
    base["note"] = note
    return base


def make_check_finmind_fx(min_valid, code):
    def check(resp, ctx):
        rows = finmind_rows(resp)
        base = finmind_base(rows)
        need_fields(base, "spot_buy", "spot_sell")
        valid = [r for r in rows if isinstance(r.get("spot_sell"), (int, float)) and r["spot_sell"] > 0]
        sentinel = len(rows) - len(valid)
        first_valid = min(str(r["date"]) for r in valid) if valid else None
        base["metrics"] = {"sentinelRows": sentinel, "firstValidDate": first_valid, "validRows": len(valid)}
        fresh_or_degraded(base, 5)
        note = ("共 %d 列，其中 %d 列的即期賣出是 -1（它用 -1 當缺值，算百分位前一定要濾掉）；"
                "第一筆有效的即期賣出在 %s" % (len(rows), sentinel, first_valid))
        if len(valid) < min_valid:
            raise ProbeDegraded("%s 有效列只有 %d（門檻 %d）；%s" % (code, len(valid), min_valid, note), base)
        yrs = years_between(first_valid, base["latest"])
        if yrs < 10:
            note += "；有效資料只有 %.1f 年，10 年百分位屆時要顯示「資料不足」" % yrs
        base["earliest"] = first_valid
        base["note"] = note
        return base
    return check


# ---- FRED fredgraph.csv --------------------------------------------------

def make_check_fred(series, min_rows, value_range, max_age_days, ctx_key=None):
    def check(resp, ctx):
        need_200(resp)
        lines = resp.text().replace("\r", "").strip().split("\n")
        head = [c.strip() for c in lines[0].split(",")]
        if len(head) < 2 or head[0] not in ("observation_date", "DATE") or head[1] != series:
            raise ProbeFail("表頭不是預期的 CSV（實際：%s）" % lines[0][:60])
        vals, empties, run, best = [], 0, None, (0, None, None)
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) < 2:
                continue
            d, v = parts[0].strip(), parts[1].strip()
            if v in ("", "."):                       # 規格以為缺值是「.」，2026-09 實測是空字串：兩種都要容忍
                empties += 1
                run = (run[0] + 1, run[1], d) if run else (1, d, d)
                if run[0] > best[0]:
                    best = run
                continue
            run = None
            try:
                vals.append((d, float(v)))
            except ValueError:
                raise ProbeFail("第二欄不是數字：%s" % line[:40])
        if not vals:
            raise ProbeFail("沒有任何一列有值")
        if ctx_key:
            ctx[ctx_key] = dict(vals[-15:])
        base = {"earliest": vals[0][0], "latest": vals[-1][0], "points": len(vals), "fields": head,
                "metrics": {"emptyRows": empties, "longestEmptyRun": {"rows": best[0], "from": best[1], "to": best[2]},
                            "lastValue": vals[-1][1], "headerLine": lines[0][:40]}}
        note = ("最新 %s＝%s；缺值 %d 列%s；要用誠實的 User-Agent（送 Chrome 的會被吊到逾時）；"
                "條款禁止儲存／封存：日後現抓現算，公開倉庫只放算出來的指標"
                % (vals[-1][0], vals[-1][1], empties,
                   "（最長一段 %s～%s 共 %d 列）" % (best[1], best[2], best[0]) if best[0] > 5 else ""))
        if len(vals) < min_rows:
            raise ProbeDegraded("有值的只有 %d 列（門檻 %d）；%s" % (len(vals), min_rows, note), base)
        if not (value_range[0] <= vals[-1][1] <= value_range[1]):
            raise ProbeDegraded("最新值 %s 不在合理範圍；%s" % (vals[-1][1], note), base)
        if days_old(vals[-1][0]) > max_age_days:
            raise ProbeDegraded("最新有值的一列是 %d 天前；%s" % (days_old(vals[-1][0]), note), base)
        base["note"] = note
        return base
    return check


# ---- CAPE：multpl.com ----------------------------------------------------

MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
          "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
CHALLENGE_MARKS = ("captcha", "cf-challenge", "Just a moment", "Attention Required", "Access denied")


def check_multpl_current(resp, ctx):
    need_200(resp)
    html = resp.text()
    for mark in CHALLENGE_MARKS:
        if mark.lower() in html.lower() and "Shiller PE" not in html:
            raise ProbeFail("看起來是擋機器人的頁面（出現「%s」）" % mark)
    m1 = re.search(r"Current Shiller PE Ratio is\s*([0-9]+(?:\.[0-9]+)?)", html)
    m2 = re.search(r'id="current"(.{0,600}?)id="timestamp"', html, flags=re.S)
    v2 = None
    if m2:
        nums = re.findall(r"(?<![0-9.])([0-9]{1,3}\.[0-9]{1,2})(?![0-9])", re.sub(r"<[^>]+>", " ", m2.group(1)))
        v2 = float(nums[0]) if nums else None
    v1 = float(m1.group(1)) if m1 else None
    ts = re.search(r'id="timestamp"[^>]*>([^<]+)<', html)
    base = {"earliest": None, "latest": ts.group(1).strip() if ts else None, "points": 1, "fields": ["現值"],
            "metrics": {"fromMetaDescription": v1, "fromCurrentBlock": v2, "server": resp.header("Server")}}
    if v1 is None and v2 is None:
        raise ProbeFail("兩個位置都解析不到數字（版面改了？）")
    if v1 is None or v2 is None:
        raise ProbeDegraded("只在一個位置解析到數字（%s／%s），沒辦法互相驗證" % (v1, v2), base)
    if abs(v1 - v2) > 0.011:
        raise ProbeDegraded("兩個位置的數字不一樣（%s 與 %s）" % (v1, v2), base)
    if not (5 <= v1 <= 80):
        raise ProbeDegraded("數字 %s 不在合理範圍" % v1, base)
    ctx["capeCurrent"] = v1
    base["note"] = ("現值 %s（頁內兩處一致）；這是 multpl 依當日股價【估算】的，不是 Shiller 的原始月值；"
                    "個人網站、沒有條款也沒有保證；單次樣本，「穩不穩定」要接上以後才看得出來" % v1)
    return base


def parse_multpl_table(html):
    rows = []
    for d, cell in re.findall(r"<td>\s*([A-Z][a-z]{2} [0-9]{1,2}, [0-9]{4})\s*</td>\s*<td[^>]*>(.*?)</td>", html, flags=re.S):
        mon, day, year = re.match(r"([A-Za-z]{3}) ([0-9]{1,2}), ([0-9]{4})", d).groups()
        text = re.sub(r"<[^>]+>|&#x?[0-9a-fA-F]+;|&[a-z]+;", " ", cell)
        num = re.search(r"-?[0-9]+(?:\.[0-9]+)?", text)
        rows.append(("%s-%02d-%02d" % (year, MONTHS.get(mon, 0), int(day)), float(num.group(0)) if num else None))
    return rows


def check_multpl_table(resp, ctx):
    need_200(resp)
    rows = parse_multpl_table(resp.text())
    if not rows:
        raise ProbeFail("解析不到任何一列（版面改了？）")
    dates = sorted(r[0] for r in rows)
    zeros = sum(1 for _d, v in rows if v is None or v == 0)
    first = rows[0]
    base = {"earliest": dates[0], "latest": dates[-1], "points": len(rows), "fields": ["Date", "Value"],
            "metrics": {"zeroOrUnparsed": zeros, "firstRow": list(first),
                        "firstRowIsIntraMonth": not first[0].endswith("-01")}}
    if zeros:
        raise ProbeDegraded("有 %d 列的值是 0 或解析不出來（別的鏡像就是用 0 冒充缺值）" % zeros, base)
    if len(rows) < 1800 or int(dates[0][:4]) > 1881:
        raise ProbeDegraded("只有 %d 列、自 %s" % (len(rows), dates[0]), base)
    cur = ctx.get("capeCurrent")
    if cur is not None and first[1] is not None and abs(first[1] - cur) > 0.5:
        raise ProbeDegraded("第一列 %s 跟現值頁的 %s 差太多" % (first[1], cur), base)
    base["note"] = ("逐月 %d 列；第一列 %s 是「當日」估算值（不是 1 號），其餘每月 1 號；"
                    "CAPE 是月資料，日後一週抓一次就夠" % (len(rows), first[0]))
    return base


def check_head_xls(resp, ctx):
    need_200(resp)
    lm = resp.header("Last-Modified")
    base = {"earliest": None, "latest": lm, "points": None, "fields": [],
            "metrics": {"contentType": resp.header("Content-Type"), "contentLength": resp.header("Content-Length"),
                        "lastModified": lm}}
    raise ProbeDegraded("只做了 HEAD（Last-Modified：%s）。這是 .xls 二進位檔，專案只依賴 requests、解不了；"
                        "最多只能當人工對照的來源" % lm, base)


def check_shillerdata_home(resp, ctx):
    need_200(resp)
    m = re.search(r'https?:(?:\\?/){2}img1\.wsimg\.com[^"\'\s<>]*?ie_data\.xls[^"\'\s<>]*', resp.text())
    url = m.group(0).replace("\\/", "/") if m else None
    ctx["shillerXlsUrl"] = url
    base = {"earliest": None, "latest": None, "points": None, "fields": [], "metrics": {"xlsLinkFound": bool(url)}}
    if not url:
        raise ProbeFail("首頁找不到 ie_data.xls 的連結")
    raise ProbeDegraded("找到 ie_data.xls 的連結（網址帶 UUID，每次換檔可能會變）", base)


# ---- 台灣 CPI ------------------------------------------------------------

def find_strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            for s in find_strings(v):
                yield s
    elif isinstance(obj, list):
        for v in obj:
            for s in find_strings(v):
                yield s
    elif isinstance(obj, str):
        yield obj


def check_datagov_meta(resp, ctx):
    need_200(resp)
    j = parse_json(resp)
    urls = [s for s in find_strings(j) if re.match(r"^https?://", s) and s.lower().split("?")[0].endswith(".xml")]
    good = [u for u in urls if host_of(u) == "ws.dgbas.gov.tw"]
    mod = [s for s in find_strings(j) if re.match(r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}", s)]
    # 這一項只是「查下載網址」，沒有所謂最新一筆資料；詮釋資料裡的日期不是資料日期，只放備註。
    base = {"earliest": None, "latest": None, "points": None, "fields": [],
            "metrics": {"xmlUrls": urls[:5], "newestDateStringInMetadata": max(mod)[:10] if mod else None}}
    if not good:
        raise ProbeFail("詮釋資料裡找不到 ws.dgbas.gov.tw 的 .xml 下載網址（找到的：%s）" % (urls[:3],))
    ctx["dgbasXmlUrl"] = good[0]
    base["note"] = ("拿到目前的 XML 下載網址（這一項只查網址，沒有資料日期；詮釋資料上最新的日期字串是 %s）；"
                    "授權是政府資料開放授權，可再利用、要標出處" % (max(mod)[:10] if mod else "—"))
    return base


def dgbas_stop(body):
    """讀到第二種 <Item> 出現，就代表排在最前面的「總指數」區塊已經讀完了。"""
    names = set(re.findall(rb"<Item>([^<]*)</Item>", body[-131072:] if len(body) > 131072 else body))
    first = re.search(rb"<Item>([^<]*)</Item>", body)
    return bool(first) and any(n != first.group(1) for n in names)


def check_dgbas_xml(resp, ctx):
    need_200(resp)
    text = resp.text()
    obs = re.findall(r"<Obs><Item>(總指數[^<]*)</Item><TIME_PERIOD>([0-9]{4})M([0-9]{2})</TIME_PERIOD>"
                     r"<FREQ>M</FREQ><TYPE>原始值</TYPE>\s*<Item_VALUE>([^<]*)</Item_VALUE>", text)
    vals = [("%s-%s" % (y, m), float(v)) for _n, y, m, v in obs if re.match(r"^-?[0-9.]+$", v.strip() or "x")]
    total = resp.header("Content-Length")
    bm = re.search(r"\(([^)]*基期[^)]*)\)", obs[0][0]) if obs else None
    base_text = bm.group(1) if bm else None
    base = {"earliest": vals[0][0] if vals else None, "latest": vals[-1][0] if vals else None,
            "points": len(vals), "fields": ["Item", "TIME_PERIOD", "TYPE", "Item_VALUE"],
            "metrics": {"contentLengthHeader": total, "bytesRead": len(resp.body), "stoppedEarly": resp.truncated,
                        "lastValue": vals[-1][1] if vals else None,
                        "baseText": base_text}}
    if not vals:
        raise ProbeDegraded("在前 %d KB 裡找不到「總指數」的原始值（檔案順序變了？那就得整檔下載 %s bytes）"
                            % (len(resp.body) // 1024, total), base)
    now = now_tpe()
    y, m = [int(x) for x in vals[-1][0].split("-")]
    lag = (now.year - y) * 12 + (now.month - m)
    note = ("總指數月資料 %d 個月；最新 %s＝%s（%s）；整檔 %s bytes，這次只讀了 %d KB 就斷線；"
            "CPI 次月上旬才公布，當月的實質金價只能用上個月的 CPI，要標估算；基期約 5 年換一次，換的時候整條會重編"
            % (len(vals), vals[-1][0], vals[-1][1], base["metrics"]["baseText"], total, len(resp.body) // 1024))
    if len(vals) < 500 or vals[0][0] > "1981-12":
        raise ProbeDegraded("只有 %d 個月、自 %s；%s" % (len(vals), vals[0][0], note), base)
    if lag > 2 or not (50 <= vals[-1][1] <= 200):
        raise ProbeDegraded("最新月份是 %d 個月前或數值不合理；%s" % (lag, note), base)
    base["note"] = note
    return base


def check_nstatdb(resp, ctx, min_months=0):
    need_200(resp)
    text = resp.text().lstrip(chr(0xFEFF) + WHITESPACE)
    if "Request Rejected" in text[:400]:
        raise ProbeFail("被主計總處前面的防火牆擋下（Request Rejected）")
    if not text.startswith("{"):
        raise ProbeFail("回來的不是 JSON，是互動查詢頁的 HTML 空殼——這個網址寫法不對")
    try:
        j = json.loads(text)
    except ValueError:
        raise ProbeFail("開頭像 JSON 但解析失敗")
    # SDMX-JSON：值在 data.dataSets[0].series["0"].observations（{"0":[110.22],…}），
    # 月份在 data.structure.dimensions.observation[0].values（[{"id":"2026-M1"},…]），兩邊用序號對起來。
    data = (j.get("data") or {}) if isinstance(j, dict) else {}
    dims = (data.get("structure") or {}).get("dimensions") or {}
    series_names = [v.get("name") for d in (dims.get("series") or []) for v in (d.get("values") or [])]
    periods = [str(v.get("id")) for d in (dims.get("observation") or [])[:1] for v in (d.get("values") or [])]
    obs = (((data.get("dataSets") or [{}])[0].get("series") or {}).get("0") or {}).get("observations") or {}
    vals = []
    for i, pid in enumerate(periods):
        m = re.match(r"^([0-9]{4})-M([0-9]{1,2})$", pid)
        v = obs.get(str(i))
        if m and isinstance(v, list) and v and isinstance(v[0], (int, float)):
            vals.append(("%s-%02d" % (m.group(1), int(m.group(2))), float(v[0])))
    parsed = len(vals)
    # 2026-09-21 實測：問到還沒公布的月份，它回 HTTP 200、值是 0.0——不是缺值、也沒有任何錯誤訊息。
    # 尾端的 0 視為「還沒公布」，剔除並寫進備註；中間出現 0 才是資料有問題。
    unpublished = []
    while vals and vals[-1][1] == 0:
        unpublished.insert(0, vals.pop()[0])
    zeros_inside = [p for p, v in vals if v == 0]
    base = {"earliest": vals[0][0] if vals else None, "latest": vals[-1][0] if vals else None, "points": len(vals),
            "fields": ["統計期", "總指數"],
            "metrics": {"topKeys": sorted(j.keys())[:8] if isinstance(j, dict) else None, "seriesNames": series_names,
                        "periodsDeclared": len(periods), "lastValue": vals[-1][1] if vals else None,
                        "unpublishedZeroMonths": unpublished, "zerosInside": zeros_inside[:5],
                        "datasetName": (data.get("structure") or {}).get("name")}}
    if not vals:
        raise ProbeFail("是 JSON，但解析不出任何一個月的值（結構跟 2026-09-21 看到的不一樣了？）")
    if "總指數" not in series_names:
        raise ProbeDegraded("回來的序列不是「總指數」：%s" % series_names, base)
    ctx["nstatdbLast"] = vals[-1]
    now = now_tpe()
    y, mth = [int(x) for x in vals[-1][0].split("-")]
    lag = (now.year - y) * 12 + (now.month - mth)
    note = ("官方 API（SDMX-JSON），回應很小（%d 個月才 %d KB）；最新 %s＝%s；日後以它為首選；"
            "CPI 次月上旬才公布，當月的實質金價只能用上個月的 CPI，要標估算；基期約 5 年換一次，換的時候整條會重編"
            % (len(vals), max(1, len(resp.body) // 1024), vals[-1][0], vals[-1][1]))
    if unpublished:
        note = ("陷阱：問到還沒公布的月份（%s），它回 HTTP 200、值是 0.0，不是缺值也沒有錯誤訊息——"
                "算之前一定要把尾端的 0 濾掉（這裡已剔除）；" % "、".join(unpublished)) + note
    if zeros_inside:
        raise ProbeDegraded("序列中間有 %d 個月的值是 0（%s…），不是尾端未公布的那種；%s"
                            % (len(zeros_inside), "、".join(zeros_inside[:3]), note), base)
    if parsed != len(periods):
        raise ProbeDegraded("宣告 %d 個月、只解析出 %d 個月的值；%s" % (len(periods), parsed, note), base)
    if lag > 2 or not (50 <= vals[-1][1] <= 200):
        raise ProbeDegraded("最新月份是 %d 個月前或數值不合理；%s" % (lag, note), base)
    if min_months and len(vals) < min_months:
        raise ProbeDegraded("只回了 %d 個月（要 %d 個月以上才算得了長期百分位）；%s" % (len(vals), min_months, note), base)
    base["note"] = note
    return base


# ---- ETF 淨值 ------------------------------------------------------------

def to_float(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def check_all_etf(resp, ctx):
    need_200(resp)
    j = parse_json(resp)                              # 副檔名是 .txt，內容是 JSON
    found, total = {}, 0
    for grp in (j.get("a1") or []):
        for e in (grp.get("msgArray") or []):
            total += 1
            if str(e.get("a")) in ("00646", "00679B"):
                found[str(e.get("a"))] = e
    base = {"earliest": None, "latest": None, "points": total, "fields": [], "metrics": {"etfCount": total}}
    if len(found) < 2:
        raise ProbeFail("找不到 00646／00679B（只找到 %s；共 %d 檔）" % (sorted(found), total))
    base["fields"] = sorted(found["00646"].keys())
    detail, unsettled = {}, []
    for code, e in sorted(found.items()):
        h = to_float(e.get("h"))
        i = str(e.get("i") or "")
        detail[code] = {"price": to_float(e.get("e")), "estNav": to_float(e.get("f")), "estPremiumPct": to_float(e.get("g")),
                        "prevOfficialNav": h, "prevOfficialNavRaw": e.get("h"), "dataDate": i, "dataTime": e.get("j"),
                        "units": to_float(e.get("c")), "unitsChange": to_float(e.get("d")),
                        "types": dict((k, type(e.get(k)).__name__) for k in ("c", "e", "f", "g", "h"))}
        if h is None or h <= 0:
            unsettled.append(code)
    base["metrics"]["etfs"] = detail
    ctx["allEtf"] = detail
    dates = [d["dataDate"] for d in detail.values() if re.match(r"^[0-9]{8}$", d["dataDate"] or "")]
    if dates:
        base["latest"] = "%s-%s-%s" % (max(dates)[:4], max(dates)[4:6], max(dates)[6:])
    note = ("一個請求同時有上市的 00646 與上櫃的 00679B；f／g 是投信的【盤中預估】淨值與折溢價，只能標估算；"
            "h 才是官方淨值，但它是【前一個營業日】的；c／d（發行單位數與日變動）可以當資金流向的材料；"
            "沒有歷史，要自己每天存；這台主機專案已經在用，日後只在 15:30 那一輪抓一次")
    if unsettled:
        raise ProbeDegraded("%s 的前一日官方淨值是「%s」，還沒結出；%s"
                            % ("、".join(unsettled), found[unsettled[0]].get("h"), note), base)
    if base["latest"] and days_old(base["latest"]) > 5:
        raise ProbeDegraded("資料日期 %s 是 %d 天前；%s" % (base["latest"], days_old(base["latest"]), note), base)
    base["note"] = note
    return base


def check_twse_etf_chart(resp, ctx):
    need_200(resp)
    j = parse_json(resp)
    nav, prem = j.get("netPrice") or [], j.get("atmps") or []
    dates = sorted(str(x.get("date")).replace("/", "-") for x in nav if x.get("date"))
    base = {"earliest": dates[0] if dates else None, "latest": dates[-1] if dates else None,
            "points": len(nav), "fields": sorted(j.keys()) if isinstance(j, dict) else [],
            "metrics": {"navRows": len(nav), "premiumRows": len(prem)}}
    if not nav or not prem:
        raise ProbeFail("netPrice 或 atmps 是空的（欄位：%s）" % base["fields"])
    ctx["twseNav"] = dict((str(x.get("date")).replace("/", "-"), to_float(x.get("count"))) for x in nav[-10:])
    note = ("官方每日淨值＋折溢價（收盤價對官方淨值）；這是網站內部的端點、沒有公開文件、隨時可能改；"
            "只涵蓋上市；00646 是歐美時區的 ETF，官方折溢價天生有時差雜訊")
    if any(to_float(x.get("count")) in (None, 0) for x in nav[-5:]):
        raise ProbeDegraded("最近幾筆淨值有 0 或解析不出來；%s" % note, base)
    if days_old(base["latest"]) > 7:
        raise ProbeDegraded("最新淨值 %s 是 %d 天前；%s" % (base["latest"], days_old(base["latest"]), note), base)
    if years_between(base["earliest"], base["latest"]) < 2.5:
        raise ProbeDegraded("只回溯到 %s；%s" % (base["earliest"], note), base)
    base["note"] = "最新官方淨值是 %s 的（探測時刻見右）；%s" % (base["latest"], note)
    return base


def check_tpex_etf(resp, ctx):
    need_200(resp)
    text = resp.text()
    if not text.lstrip().startswith("{"):
        raise ProbeFail("回來的不是 JSON（Cloudflare 的挑戰頁？）")
    j = parse_json(resp)
    nav, prem, close = j.get("netPrice") or [], j.get("atmps") or [], j.get("close1") or []
    base = {"earliest": (nav[0].get("date") if nav else None), "latest": (nav[-1].get("date") if nav else None),
            "points": len(nav), "fields": sorted(j.keys())[:20],
            "metrics": {"navRows": len(nav), "premiumRows": len(prem), "closeRows": len(close),
                        "underlyingIndex": j.get("underlyingIndex"), "server": resp.header("Server")}}
    if str(j.get("stockNo")) != "00679B" or not nav or not prem:
        raise ProbeFail("不是預期的內容（stockNo=%s、淨值 %d 筆）" % (j.get("stockNo"), len(nav)))
    if to_float(nav[-1].get("count")) in (None, 0):
        raise ProbeFail("最後一筆淨值是 0 或解析不出來")
    raise ProbeDegraded("抓得到官方淨值＋折溢價，但只有最近 %d 個交易日（%s～%s，日期只有月／日、沒有年），"
                        "長期的百分位得靠每天累積；網站內部端點、沒有文件、在 Cloudflare 後面"
                        % (len(nav), base["earliest"], base["latest"]), base)


# ==========================================================================
# 要測的清單
# ==========================================================================

def load_yahoo_symbols():
    """Yahoo 代號一律讀 data/assets.json（唯一真相來源）。規格寫的 00679B.TW 是錯的：它是上櫃，代號是 .TWO。"""
    with open(os.path.join(ROOT, "data", "assets.json"), encoding="utf-8") as fh:
        assets = dict((a["id"], a) for a in json.load(fh)["assets"])
    out = {}
    for aid in ("gspc", "twii", "tw2330", "tw00646", "tw00679b", "btc", "wti"):
        a = assets.get(aid) or {}
        out[aid] = a.get("yahooSymbol") or (a.get("symbol") if a.get("type") == "yahoo" else None)
    return out


def quote(s):
    return url_quote(s, safe="")


def build_items(symbols, now=None):
    from fetch_data import BROWSER_HEADERS           # import 不會連網（fetch_data 有 main 守衛）
    now = now or now_tpe()
    epoch_now = int(now.timestamp())
    browser = dict(BROWSER_HEADERS)
    jsonish = dict(BROWSER_HEADERS, Accept="application/json, text/plain, */*")
    honest = {"User-Agent": HONEST_UA, "Accept": "text/csv, */*"}
    Y = "https://query1.finance.yahoo.com/v8/finance/chart/"
    F = "https://api.finmindtrade.com/api/v4/"
    items = []

    def add(key, group, spec, title, req, check, expect=None, when=None):
        items.append({"key": key, "group": group, "spec": spec, "title": title,
                      "req": req, "check": check, "expect": expect, "when": when})

    def yreq(sym, query):
        return lambda ctx: Req(Y + quote(sym) + "?" + query, headers=jsonish, retry429=True)

    weekly = "period1=0&period2=%d&interval=1wk" % epoch_now
    wk = lambda r, ctx: check_yahoo(r, "1wk", 6, 8, 52, min_years=10)   # noqa: E731

    add("Y-00", "yahoo", "1（對照列）", "Yahoo GC=F，規格原寫法 range=max&interval=1wk",
        yreq("GC=F", "range=max&interval=1wk"), wk, expect=DEGRADED)
    for key, aid, label in (("Y-01", "gspc", "S&P 500"), ("Y-03", "twii", "加權指數"), ("Y-04", "tw2330", "台積電"),
                            ("Y-05", "tw00646", "元大 S&P500"), ("Y-06", "tw00679b", "元大美債20年（上櫃）"),
                            ("Y-08", "btc", "比特幣"), ("Y-09", "wti", "WTI 原油")):
        sym = symbols.get(aid)
        if key == "Y-03":
            add("Y-02", "yahoo", "1", "Yahoo GC=F 國際金價（近月連續期貨，不是現貨）週線，period1/period2",
                yreq("GC=F", weekly), wk)
        add(key, "yahoo", "1", "Yahoo %s %s 週線，period1/period2" % (sym, label),
            (yreq(sym, weekly) if sym else None), wk,
            when=(None if sym else (lambda ctx: "data/assets.json 裡找不到這一項的 Yahoo 代號")))
        if key == "Y-06":
            add("Y-07", "yahoo", "1（對照列）", "Yahoo 00679B.TW，規格寫的代號（上櫃應該是 .TWO）",
                yreq("00679B.TW", "range=5d&interval=1d"),
                lambda r, ctx: check_yahoo(r, "1d", 0.5, 4, 1), expect=FAILED)
    add("Y-13", "yahoo", "7", "Yahoo ^VIX 日線 range=5y",
        yreq("^VIX", "range=5y&interval=1d"),
        lambda r, ctx: check_yahoo(r, "1d", 0.5, 4, 1200, value_range=(5, 100), max_age=6))

    def freq(path):
        return lambda ctx: Req(F + path, headers=jsonish)

    add("F-01", "finmind", "9(a)", "FinMind 公債殖利率有哪些 data_id（datalist）",
        freq("datalist?dataset=GovernmentBondsYield"), check_finmind_datalist)
    add("F-02", "finmind", "9(a)", "FinMind 美國 20 年期公債殖利率，1990 起",
        freq("data?dataset=GovernmentBondsYield&data_id=United%20States%2020-Year&start_date=1990-01-01"),
        check_finmind_bond)
    add("F-03", "finmind", "3", "FinMind 月營收 2330，2002 起",
        freq("data?dataset=TaiwanStockMonthRevenue&data_id=2330&start_date=2002-01-01"), check_finmind_revenue)
    add("F-04", "finmind", "4", "FinMind 本益比／淨值比／殖利率 2330，2005 起",
        freq("data?dataset=TaiwanStockPER&data_id=2330&start_date=2005-01-01"), check_finmind_per)
    add("F-05", "finmind", "5", "FinMind 融資融券 2330，2001 起",
        freq("data?dataset=TaiwanStockMarginPurchaseShortSale&data_id=2330&start_date=2001-01-01"),
        check_finmind_margin)
    add("F-06", "finmind", "6", "FinMind 三大法人 2330，2005 起",
        freq("data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id=2330&start_date=2005-01-01"),
        check_finmind_institutional)
    add("F-07", "finmind", "2", "FinMind 台銀匯率 USD，2006 起整段",
        freq("data?dataset=TaiwanExchangeRate&data_id=USD&start_date=2006-01-01"), make_check_finmind_fx(4500, "USD"))
    add("F-08", "finmind", "2", "FinMind 台銀匯率 CNY，2006 起整段",
        freq("data?dataset=TaiwanExchangeRate&data_id=CNY&start_date=2006-01-01"), make_check_finmind_fx(1200, "CNY"))
    add("F-09", "finmind", "10(a)", "FinMind 台灣 CPI／物價", None, None,
        when=lambda ctx: "依 FinMind 的文件，它的 dataset 清單裡沒有任何物價類資料，所以不發請求")

    def fredreq(series):
        return lambda ctx: Req("https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + series, headers=honest, timeout=15)

    add("R-01", "fred", "9(b)", "FRED 美國 20 年期公債殖利率 DGS20", fredreq("DGS20"),
        make_check_fred("DGS20", 10000, (0, 20), 7, ctx_key="fredDGS20"))
    add("R-02", "fred", "10（退路）", "FRED 美國 CPI（CPIAUCSL，月資料）", fredreq("CPIAUCSL"),
        make_check_fred("CPIAUCSL", 600, (200, 500), 100))
    add("R-03", "fred", "加測", "FRED 殖利率曲線 10 年減 2 年（T10Y2Y）", fredreq("T10Y2Y"),
        make_check_fred("T10Y2Y", 5000, (-5, 5), 7))

    add("C-01", "cape", "8(a)", "multpl.com CAPE 現值頁",
        lambda ctx: Req("https://www.multpl.com/shiller-pe", headers=browser), check_multpl_current)
    add("C-02", "cape", "8(a)", "multpl.com CAPE 逐月歷史表",
        lambda ctx: Req("https://www.multpl.com/shiller-pe/table/by-month", headers=browser), check_multpl_table)
    only_if_multpl_failed = lambda ctx: (   # noqa: E731
        "8(a) multpl 的現值頁與歷史表都可用；規格說只要一個可用即可，所以 (b) 不測"
        if ctx.get("state:C-01") == OK and ctx.get("state:C-02") == OK else None)
    add("C-03", "cape", "8(b)", "Yale 舊檔 ie_data.xls（只做 HEAD，不下載）",
        lambda ctx: Req("http://www.econ.yale.edu/~shiller/data/ie_data.xls", method="HEAD", headers=browser),
        check_head_xls, when=only_if_multpl_failed)
    add("C-04", "cape", "8(b)", "Shiller 新站 shillerdata.com 首頁（找 .xls 的連結）",
        lambda ctx: Req("https://shillerdata.com/", headers=browser), check_shillerdata_home,
        when=only_if_multpl_failed)
    add("C-05", "cape", "8(b)", "Shiller 新站的 ie_data.xls（只做 HEAD，不下載）",
        lambda ctx: Req(ctx["shillerXlsUrl"], method="HEAD", headers=browser), check_head_xls,
        when=lambda ctx: (only_if_multpl_failed(ctx) or
                          (None if ctx.get("shillerXlsUrl") else "上一步沒有找到 .xls 的連結")))

    add("T-01", "cpi", "10(b)", "data.gov.tw 資料集 6019 的詮釋資料（取得目前的下載網址）",
        lambda ctx: Req("https://data.gov.tw/api/v2/rest/dataset/6019", headers=jsonish), check_datagov_meta)
    add("T-02", "cpi", "10(b)", "主計總處 CPI 的 XML（整檔 15.7MB；串流讀到「總指數」那一段就斷線）",
        lambda ctx: Req(ctx.get("dgbasXmlUrl") or DGBAS_XML_FALLBACK, headers=browser, cap=DGBAS_CAP, stop=dgbas_stop,
                        timeout=30), check_dgbas_xml)
    # 2026-09-21 在筆電上實測：這個寫法回得來（8 個月、1 KB 的 SDMX-JSON）。正式探測改問整段，
    # 一個請求同時回答「能回溯到哪一年」與「問到還沒公布的月份會不會出錯」；整段不行才退回只問今年。
    nstat = "https://nstatdb.dgbas.gov.tw/dgbasAll/webMain.aspx?sdmx/A030101015/1.1..M&startTime=%s&endTime=%s"
    last_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    add("T-03", "cpi", "10(b)", "主計總處總體統計資料庫 API：CPI 總指數，1981 年 1 月到這個月",
        lambda ctx: Req(nstat % ("1981-01", now.strftime("%Y-%m")), headers=jsonish),
        lambda r, ctx: check_nstatdb(r, ctx, min_months=500))
    year_ago = (now.replace(day=1) - timedelta(days=360)).strftime("%Y-%m")
    add("T-04", "cpi", "10(b)", "主計總處總體統計資料庫 API：只問最近 12 個月（整段不行才測）",
        lambda ctx: Req(nstat % (year_ago, last_month), headers=jsonish), check_nstatdb,
        when=lambda ctx: (None if ctx.get("state:T-03") != OK else "整段已經可用，不必再測"))

    three_years_ago = (now - timedelta(days=3 * 365)).strftime("%Y/%m/%d")
    add("N-01", "nav", "11", "證交所 all_etf.txt（一個請求同時有 00646 與上櫃的 00679B）",
        lambda ctx: Req("https://mis.twse.com.tw/stock/data/all_etf.txt", headers=jsonish), check_all_etf)
    add("N-02", "nav", "11", "證交所 ETF e添富：00646 官方淨值＋折溢價，近 3 年（POST 查詢，不送任何個人資料）",
        lambda ctx: Req("https://www.twse.com.tw/zh/ETFortune/ajaxEtfInfoChart", method="POST",
                        headers=dict(jsonish, **{"X-Requested-With": "XMLHttpRequest",
                                                 "Referer": "https://www.twse.com.tw/zh/ETFortune/etfInfo/00646"}),
                        data={"id": "00646", "startDate": three_years_ago, "endDate": now.strftime("%Y/%m/%d"),
                              "type": "fundPric"}), check_twse_etf_chart)
    add("N-03", "nav", "11", "櫃買中心 ETF 訊息中心：00679B 官方淨值＋折溢價（POST，沒有 body）",
        lambda ctx: Req("https://info.tpex.org.tw/api/etfProduct?lang=zh-tw&query=00679B", method="POST",
                        headers=dict(jsonish, Referer="https://info.tpex.org.tw/ETF/zh/detail.html?query=00679B")),
        check_tpex_etf)
    return items


NOT_TESTED_BY_DECISION = [
    "大盤融資（FinMind TaiwanStockTotalMarginPurchaseShortSale）", "大盤三大法人（FinMind TaiwanStockTotalInstitutionalInvestors）",
    "NVDA 週線長歷史", "長區間日線（GC=F 2 年日線、^GSPC 10 年日線）",
]


# ==========================================================================
# 執行、輸出
# ==========================================================================

def run_item(item, net, ctx):
    rec = {"key": item["key"], "group": item["group"], "spec": item["spec"], "title": item["title"],
           "expect": item["expect"], "state": SKIPPED, "earliest": None, "latest": None, "points": None,
           "fields": [], "note": "", "fingerprint": None, "http": None, "bytes": None, "seconds": None,
           "requests": 0, "method": None, "host": None, "probedAt": now_tpe().isoformat(timespec="seconds"),
           "metrics": {}}
    resp = None
    sent_before = len(getattr(net, "sent", ()))
    try:
        reason = item["when"](ctx) if item["when"] else None
        if reason is None and item["group"] == "finmind" and ctx.get("finmindBlocked"):
            reason = "FinMind 前面已經回了 402／403（%s），整組停手不再發請求" % ctx["finmindBlocked"]
        if reason or item["req"] is None:
            rec["note"] = reason or "沒有可測的請求"
            return rec
        req = item["req"](ctx)
        rec["method"], rec["host"] = req.method, host_of(req.url)
        resp = net.fetch(req)
        rec.update({"http": resp.status, "bytes": len(resp.body), "seconds": round(resp.seconds, 2),
                    "requests": resp.requests_made})
        if resp.status is None:
            hint = ""
            if "CERTIFICATE_VERIFY_FAILED" in (resp.error or ""):
                hint = ("（憑證鏈 Python 驗不過：常見原因是對方沒送中繼憑證，瀏覽器會自己補所以看得到；"
                        "不可以用關掉憑證驗證來繞）")
            raise ProbeFail("連不上%s：%s" % (hint, resp.error))
        out = item["check"](resp, ctx)
        rec.update({"state": OK, "earliest": out.get("earliest"), "latest": out.get("latest"),
                    "points": out.get("points"), "fields": out.get("fields") or [],
                    "note": out.get("note") or "", "metrics": out.get("metrics") or {}})
    except HostNotAllowed as e:                       # 被轉址帶去名單外的主機：前面已經送出去的那幾跳照樣要算
        rec.update({"state": FAILED, "note": str(e),
                    "requests": max(0, len(getattr(net, "sent", ())) - sent_before)})
    except ProbeDegraded as e:
        d = e.detail
        rec.update({"state": DEGRADED, "note": str(e), "earliest": d.get("earliest"), "latest": d.get("latest"),
                    "points": d.get("points"), "fields": d.get("fields") or [], "metrics": d.get("metrics") or {},
                    "fingerprint": fingerprint(resp) if resp is not None and resp.status != 200 else None})
    except FinMindBlocked as e:
        ctx["finmindBlocked"] = str(e)
        rec.update({"state": FAILED, "note": str(e), "fingerprint": fingerprint(resp)})
    except ProbeFail as e:
        rec.update({"state": FAILED, "note": str(e), "fingerprint": fingerprint(resp)})
    except Exception as e:                            # 判準自己寫壞了，也只准是這一列失敗，不准讓整支停下來
        rec.update({"state": FAILED, "note": "檢查程式出錯（%s：%s）" % (e.__class__.__name__, str(e)[:120]),
                    "fingerprint": fingerprint(resp)})
    ctx["state:" + item["key"]] = rec["state"]
    return rec


def cross_checks(results, ctx):
    by = dict((r["key"], r) for r in results)
    a, b = ctx.get("finmindUS20"), ctx.get("fredDGS20")
    if a and b and "R-01" in by:
        common = sorted(set(a) & set(b))
        if common:
            d = common[-1]
            by["R-01"]["note"] += "；交叉核對：%s FRED %s／FinMind %s" % (d, b[d], a[d])
    etf, nav = ctx.get("allEtf"), ctx.get("twseNav")
    if etf and nav and "N-02" in by:
        h = (etf.get("00646") or {}).get("prevOfficialNav")
        hit = [d for d, v in nav.items() if v is not None and h is not None and abs(v - h) < 1e-6]
        by["N-02"]["note"] += ("；交叉核對：all_etf.txt 的 h（%s）就是這裡 %s 的淨值" % (h, hit[-1]) if hit
                               else "；交叉核對：all_etf.txt 的 h（%s）在最近 10 筆淨值裡找不到同值" % h)


def tally(results):
    return dict((s, sum(1 for r in results if r["state"] == s)) for s in STATES)


def control_verdict(results):
    """兩列對照列。任何一列被判成「可用」＝判準壞了，這次的結果不可信。"""
    lines, broken = [], False
    for r in results:
        if not r.get("expect"):
            continue
        if r["state"] == OK:
            broken = True
            lines.append("%s 預期「%s」，卻被判成「可用」（不是判準壞了，就是這一列的前提變了，兩種都要人工查）"
                         % (r["key"], r["expect"]))
        elif r["state"] == r["expect"]:
            lines.append("%s → %s ✔（預期 %s）" % (r["key"], r["state"], r["expect"]))
        else:
            lines.append("%s → %s（預期 %s；這一列這次沒有證明力）" % (r["key"], r["state"], r["expect"]))
    return broken, lines


def md(s):
    return str(s if s is not None else "—").replace("|", "／").replace("\n", " ")


def render_summary(payload, finished=True):
    res = payload["results"]
    t = tally(res)
    broken, ctl = control_verdict(res)
    out = ["## 分析資料來源探測（A0：只測不接）", ""]
    if not finished:
        out += ["> 探測進行中……如果你看到的是這一行，代表它中途被中斷了，下面只有已經測完的部分。", ""]
    out += ["**可用 %d／降級 %d／失敗 %d／未測 %d**（共 %d 項、對外 %d 個請求；環境：%s；台北時間 %s）"
            % (t[OK], t[DEGRADED], t[FAILED], t[SKIPPED], len(res), payload.get("requestsSent", 0),
               payload["where"], payload["startedAt"]), ""]
    if broken:
        out += ["> **⚠ 探測判準失效，本次結果不可信：** " + "；".join(ctl), ""]
    elif ctl:
        out += ["對照列：" + "；".join(ctl), ""]
    out += ["降級＝抓得到，但不合需求（粒度被改、回溯不夠、資料太舊、只有估算值）。「最新一筆」要跟探測時刻對著看；"
            "週線的最後一根是還沒走完的這一週。", "",
            "| # | 規格第幾項 | 來源 | 狀態 | 回溯到 | 最新一筆 | 點數 | 欄位 | 備註 |",
            "|---|---|---|---|---|---|---|---|---|"]
    for r in res:
        out.append("| %s | %s | %s | **%s** | %s | %s | %s | %s | %s |" % (
            r["key"], md(r["spec"]), md(r["title"]), r["state"], md(r["earliest"]), md(r["latest"]),
            md(r["points"]), md("、".join(r["fields"][:8]) + ("…" if len(r["fields"]) > 8 else "")) if r["fields"] else "—",
            md(r["note"])))
    out += ["", "規格以外、這次決定不測的：" + "；".join(NOT_TESTED_BY_DECISION) + "。", ""]
    fps = [r for r in res if r.get("fingerprint")]
    if fps:
        out += ["<details><summary>失敗指紋（對方到底回了什麼）</summary>", ""]
        for r in fps:
            out.append("- **%s**：`%s`" % (r["key"], json.dumps(r["fingerprint"], ensure_ascii=False)))
        out += ["", "</details>", ""]
    timing = ["%s %s秒/%sKB" % (r["key"], r["seconds"], (r["bytes"] or 0) // 1024) for r in res if r["seconds"] is not None]
    out += ["<details><summary>每一項的耗時與大小</summary>", "", "、".join(timing), "", "</details>", "",
            "這支探測不寫倉庫裡任何檔案、不接任何來源；主機白名單以外的一律不送，台銀永遠拒絕。", ""]
    if finished:
        out += ["<details><summary>PROBE_RESULT（並排比較用）</summary>", "", "```json",
                json.dumps(payload, ensure_ascii=False), "```", "", "</details>", ""]
    return "\n".join(out)


def inside_repo(path):
    try:
        return os.path.realpath(path).lower().startswith(os.path.realpath(ROOT).lower() + os.sep)
    except Exception:
        return True


def write_outside_repo(path, text):
    """只准寫到倉庫以外：這支探測不可以讓倉庫多出任何檔案。"""
    if not path:
        return False
    if inside_repo(path):
        print("（拒絕寫入：輸出路徑在倉庫裡面。請指到倉庫以外的地方。）")
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return True


def run_probe(net, only=None, summary_path=None, now=None):
    ctx = {}
    symbols = load_yahoo_symbols()
    items = [i for i in build_items(symbols, now) if not only or i["group"] in only]
    payload = {"probe": "analysis-sources-A0", "where": "actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local",
               "startedAt": now_tpe().strftime("%Y-%m-%d %H:%M:%S"), "only": sorted(only) if only else None,
               "requestsSent": 0, "results": []}
    try:
        for item in items:
            rec = run_item(item, net, ctx)
            payload["results"].append(rec)
            payload["requestsSent"] += rec["requests"]
            span = "～".join(x for x in (rec["earliest"], rec["latest"]) if x)
            print("[%s] %-4s %s%s" % (rec["key"], rec["state"], rec["title"],
                                      ("（%s%s）" % (span, "，%s 點" % rec["points"] if rec["points"] else "")
                                       if span else "")))
            if rec["note"]:
                print("        %s" % rec["note"])
            if rec["state"] == FAILED:
                print("::warning::%s 失敗：%s" % (rec["key"], rec["note"][:200]))
            write_outside_repo(summary_path, render_summary(payload, finished=False))   # 測完一項就留一份
        cross_checks(payload["results"], ctx)
    finally:
        payload["finishedAt"] = now_tpe().strftime("%Y-%m-%d %H:%M:%S")
        payload["tally"] = tally(payload["results"])
        broken, lines = control_verdict(payload["results"])
        payload["controlsBroken"], payload["controls"] = broken, lines
        write_outside_repo(summary_path, render_summary(payload, finished=True))
        t = payload["tally"]
        print("\n可用 %d／降級 %d／失敗 %d／未測 %d；對外 %d 個請求"
              % (t[OK], t[DEGRADED], t[FAILED], t[SKIPPED], payload["requestsSent"]))
        if broken:
            print("⚠ 探測判準失效，本次結果不可信：" + "；".join(lines))
        else:
            print("對照列：" + "；".join(lines))
        print("\nPROBE_RESULT " + json.dumps(payload, ensure_ascii=False))
    return payload


def dry_run(only=None):
    items = [i for i in build_items(load_yahoo_symbols()) if not only or i["group"] in only]
    ctx, hosts, n = {"shillerXlsUrl": "https://img1.wsimg.com/（由上一步取得）"}, {}, 0
    for it in items:
        if it["req"] is None:
            print("%-5s —     （不發請求）%s" % (it["key"], it["title"]))
            continue
        req = it["req"](ctx)
        host = host_of(req.url)
        try:
            assert_host_allowed(req.url)
            verdict = ""
        except HostNotAllowed as e:
            verdict = "  ★ 會被白名單拒絕：%s" % e
        cond = "（有條件才測）" if it["when"] else ""
        print("%-5s %-5s %s %s%s" % (it["key"], req.method, req.url, cond, verdict))
        hosts[host] = hosts.get(host, 0) + 1
        n += 1
    print("\n最多 %d 個請求；各主機：%s" % (n, "、".join("%s %d" % kv for kv in sorted(hosts.items()))))
    print("台銀（bot.com.tw）：%d 個" % sum(v for k, v in hosts.items() if "bot.com.tw" in k))


def load_payload(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"PROBE_RESULT (\{.*\})", text)
    return json.loads(m.group(1) if m else text)


def compare(path_a, path_b):
    a, b = load_payload(path_a), load_payload(path_b)
    ra, rb = dict((r["key"], r) for r in a["results"]), dict((r["key"], r) for r in b["results"])
    print("| # | 來源 | %s（%s） | %s（%s） | 差異 |" % (a["where"], a["startedAt"], b["where"], b["startedAt"]))
    print("|---|---|---|---|---|")
    for key in sorted(set(ra) | set(rb)):
        x, y = ra.get(key), rb.get(key)
        cell = lambda r: "—" if not r else "%s／最新 %s／%s 點" % (r["state"], md(r["latest"]), md(r["points"]))   # noqa: E731
        diff = []
        if x and y:
            if x["state"] != y["state"]:
                diff.append("狀態不同")
            if x["latest"] != y["latest"]:
                diff.append("最新一筆不同")
            if x["earliest"] != y["earliest"]:
                diff.append("起點不同")
        print("| %s | %s | %s | %s | %s |" % (key, md((x or y)["title"]), cell(x), cell(y), "、".join(diff) or "一致"))


def _main(argv):
    ap = argparse.ArgumentParser(description="分析系列 A0：新資料來源探測（只測不接，永遠 exit 0）")
    ap.add_argument("--summary", help="把結果表寫到這個檔（必須在倉庫以外；GitHub 上給 $GITHUB_STEP_SUMMARY）")
    ap.add_argument("--json-out", help="把 PROBE_RESULT 另存成檔（必須在倉庫以外），給 --compare 用")
    ap.add_argument("--only", help="只測這幾組，逗號分隔：%s" % "、".join(GROUPS))
    ap.add_argument("--dry-run", action="store_true", help="只列出會發哪些請求，不連網")
    ap.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"), help="兩份結果並排（不連網）")
    args = ap.parse_args(argv)
    only = set(x.strip() for x in args.only.split(",") if x.strip()) if args.only else None
    if only and not only <= set(GROUPS):
        print("不認得的組別：%s（可用：%s）" % ("、".join(sorted(only - set(GROUPS))), "、".join(GROUPS)))
        return
    if args.compare:
        compare(*args.compare)
    elif args.dry_run:
        dry_run(only)
    else:
        payload = run_probe(Net(), only=only, summary_path=args.summary)
        if args.json_out:
            write_outside_repo(args.json_out, json.dumps(payload, ensure_ascii=False, indent=1))


def main(argv=None):
    """永遠回 0：探測失敗是結果，不是程式錯。真的出了沒料到的錯，也只印出來，不讓整個 job 變紅。"""
    try:
        _main(argv)
    except SystemExit:
        pass
    except BaseException as e:                        # noqa: B902
        print("探測程式本身出錯：%s：%s" % (e.__class__.__name__, e))
        traceback.print_exc(limit=3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
