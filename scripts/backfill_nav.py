#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_nav.py — 00646 的官方淨值一次回補三年（分析系列 A1-4，一次性）

為什麼要有這支？
    data/analysis/nav/tw00646.json 是 2026-09-23 才開始每天累積的，「確定」口徑的折溢價只有自己幾十天的歷史。
    證交所 ETF e添富有 00646 近三年的官方每日淨值（A1-2 探測 N-05a／N-05b 證實可用），一次補齊。

來源與請求（一共 3 個，每個只發一次、不自動重試）：
    1. GET  https://www.twse.com.tw/zh/ETFortune/etfInfo/00646          先拿頁面（同一個 session 的 cookie）
    2. POST https://www.twse.com.tw/zh/ETFortune/ajaxEtfInfoChart       同一個 session 取三年官方淨值（type=fundPric）
    3. GET  https://query1.finance.yahoo.com/v8/finance/chart/00646.TW  三年日線收盤價（data/history 沒有的日期才用）
    第 1 個拿不到（含「安全性考量」頁）就不發第 2 個。只准連這兩個主機；台銀一律擋（scripts/net_policy.py 的判斷）。
    不自動跟著轉址：回轉址就停（結束碼 2），要轉去的主機一個請求都不發。

寫到哪裡：
    data/analysis/nav/tw00646.backfill.json（新檔）。tw00646.json 一個字都不動：scripts/analyze.py 每天 15:30 那一輪
    讀淨值時把這裡的列只增不改地併進去（只補那邊沒有的日期）。這樣分支跟每天的累積不會改到同一個檔（A1-4 的裁決）。

規則：
    1. 只增不改：只寫 tw00646.json 沒有的日期；既有的列一個欄位都不動（這支程式根本不寫 tw00646.json）。
    2. 收盤價重疊：data/history 與 Yahoo 同一天都有的，差超過 0.3% 就停、什麼都不寫（結束碼 1）。
    3. 官方淨值重疊：tw00646.json 既有列的 officialNav 與 e添富同一天的淨值，四捨五入到小數第二位要一樣；不一樣就停、什麼都不寫（結束碼 1）。
       另外列出兩類給人看：既有列有官方淨值、e添富沒有那一天的；既有列沒有官方淨值、e添富有的。
    4. 配對：淨值日期與收盤日期要同一天。收盤價先找 data/history，沒有才找 Yahoo；兩邊都沒有、或淨值是 0 或解析不出來，略過並計數。
    5. 新列只有「確定」口徑的欄位：officialTag 確定、officialLabel 回補、officialNavSeenOn＝回補當天、每個欄位的 dateSource
       （淨值 twse-etfortune；收盤價 Yahoo 直接抓的寫 yahoo，取自 data/history 的照抄那一點自己記的來源，例如 data/history:official，
       沒記的只寫 data/history）。折溢價＝(收盤 − 官方淨值) ÷ 官方淨值，是算出來的。e添富自己的折溢價（atmps）只拿來對照、不寫進檔。

用法：
    py -3.12 scripts/backfill_nav.py --save-raw <資料夾> --dry-run   # 真的抓、原始回應存到倉庫外的資料夾、只列結果不寫檔
    py -3.12 scripts/backfill_nav.py --from-raw <資料夾>             # 不發任何請求，用存下來的原始回應寫檔
    py -3.12 scripts/backfill_nav.py --save-raw <資料夾>             # 抓＋寫檔

結束碼：0＝好了（或 --dry-run 沒有問題）；1＝重疊的比對不過，沒有寫檔；2＝來源抓不到或格式跟預期不一樣，沒有寫檔。
"""
import argparse
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import analyze as az                                   # noqa: E402  NAV_MAX_ROWS、LABEL_BACKFILL、路徑、折溢價的算法都用 analyze 那一份
import fetch_data as fd                                # noqa: E402  Yahoo 的解析（日期用交易所當地日期）
import net_policy                                      # noqa: E402  台銀的禁止判斷與專案的白名單
from probe_analysis_sources import twse_blocked        # noqa: E402  「安全性考量」頁的判斷跟探測同一份

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                      # noqa: B902
    pass

TPE = timezone(timedelta(hours=8))
AID, SYMBOL, YAHOO_SYMBOL = "tw00646", "00646", "00646.TW"
CLOSE_THRESHOLD_PCT = 0.3              # 收盤價：data/history 與 Yahoo 同一天差超過這個百分比就停（跟 backfill_fx_history 同一個門檻）
NAV_DECIMALS = 2                       # 官方淨值：兩邊四捨五入到小數第幾位要一樣
YEARS, EXTRA_DAYS = 3, 30              # 往回抓三年，Yahoo 那一邊再多 30 天
GAP_SECONDS = 3.0                      # 請求之間固定等這麼久（同站禮貌間隔）
HOSTS = ("www.twse.com.tw", "query1.finance.yahoo.com")      # 這支腳本只准連這兩個主機
REDIRECT_STATUSES = (301, 302, 303, 307, 308)                 # 回這些就是轉址：不跟過去，當成來源有問題（結束碼 2）
PAGE_URL = "https://www.twse.com.tw/zh/ETFortune/etfInfo/00646"
CHART_URL = "https://www.twse.com.tw/zh/ETFortune/ajaxEtfInfoChart"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d&interval=1d"
NAV_SOURCE, CLOSE_SOURCE = "twse-etfortune", "yahoo"    # 淨值的來源；回補時直接抓 Yahoo 的收盤價
HIST_SOURCE = "data/history"           # 收盤價取自 data/history 的，dateSource 寫這個＋那一點自己記的來源（見 close_source）
RAW_FILES = {"page": "etfortune-page.html", "chart": "etfortune-chart.json", "yahoo": "yahoo-00646.json", "log": "requests.json"}


class SourceError(Exception):
    """來源抓不到、被擋、或格式跟預期不一樣。"""


# ---------------------------------------------------------------- 連線（只准兩個主機、每個請求只發一次）


def check_host(url):
    """不准連就丟 net_policy.HostNotAllowed：台銀一律擋、要在專案的白名單裡、而且只能是這支腳本的那兩個主機。"""
    host = net_policy.assert_host_allowed(url)
    if host not in HOSTS:
        raise net_policy.HostNotAllowed("這支回補腳本只准連 %s（%s）" % ("、".join(HOSTS), host or "?"))
    return host


def redirect_host(url, location):
    """轉址要去的主機（只拿來寫進錯誤訊息）；看不出來就「?」。"""
    try:
        return urlparse(urljoin(url, location or "")).hostname or "?"
    except ValueError:
        return "?"


class OneShot(object):
    """同一個 session 發請求：送出前查主機；不自動跟著轉址（回轉址就停，要去的那個主機一個請求都不發）；不自動重試；
    每一個請求都記下來（方法、主機、路徑、狀態）。send：測試換成假的；預設用 requests，而且 allow_redirects=False。"""

    def __init__(self, send=None, sleep=time.sleep):
        self.log = []
        self.sleep = sleep
        self._last = None
        if send is None:
            import requests
            self.session = requests.Session()

            def send(method, url, headers, data):
                # requests 預設會自動跟著轉址，下一跳要等送出之後才查得到主機（A1-4 Codex 第 1 輪）。關掉；轉址由 request() 當成來源有問題。
                return self.session.request(method, url, headers=headers, data=data, timeout=30, allow_redirects=False)
        self._send = send

    def request(self, method, url, headers=None, data=None):
        host = check_host(url)
        if self._last is not None:
            gap = time.time() - self._last
            if gap < GAP_SECONDS:
                self.sleep(GAP_SECONDS - gap)
        self._last = time.time()
        rec = {"method": method, "host": host, "path": url.split(host, 1)[-1].split("?", 1)[0]}
        self.log.append(rec)
        r = self._send(method, url, dict(fd.BROWSER_HEADERS, **(headers or {})), data)
        rec["status"] = getattr(r, "status_code", None)
        if rec["status"] in REDIRECT_STATUSES:
            where = redirect_host(url, (getattr(r, "headers", None) or {}).get("Location"))
            raise SourceError("%s 回轉址（HTTP %s，要轉到 %s）；不跟過去，那裡一個請求都沒有發" % (host, rec["status"], where))
        return r


def fetch_all(net, today):
    """發那三個請求，回傳原始回應 {page, chart, yahoo}（文字）。任何一步不對就丟 SourceError；第 1 步不對就不發第 2 步。"""
    start = (today - timedelta(days=YEARS * 365)).strftime("%Y/%m/%d")
    r = net.request("GET", PAGE_URL)
    page = r.text or ""
    if twse_blocked(page[:3000]):
        raise SourceError("證交所回「安全性考量」頁（HTTP %s），不發淨值的請求" % r.status_code)
    if r.status_code != 200 or ("00646" not in page and "ETF" not in page):
        raise SourceError("e添富的頁面拿不到（HTTP %s），不發淨值的請求" % r.status_code)
    jsonish = {"Accept": "application/json, text/plain, */*", "X-Requested-With": "XMLHttpRequest", "Referer": PAGE_URL}
    r = net.request("POST", CHART_URL, headers=jsonish,
                    data={"id": SYMBOL, "startDate": start, "endDate": today.strftime("%Y/%m/%d"), "type": "fundPric"})
    chart = r.text or ""
    if twse_blocked(chart[:3000]) or r.status_code != 200:
        raise SourceError("e添富的淨值拿不到（HTTP %s）" % r.status_code)
    p1 = int(datetime(today.year, today.month, today.day, tzinfo=TPE).timestamp()) - (YEARS * 365 + EXTRA_DAYS) * 86400
    p2 = int(time.time())
    r = net.request("GET", YAHOO_URL % (YAHOO_SYMBOL, p1, p2), headers={"Accept": "application/json, text/plain, */*"})
    if r.status_code != 200:
        raise SourceError("Yahoo 00646.TW 日線拿不到（HTTP %s）" % r.status_code)
    return {"page": page, "chart": chart, "yahoo": r.text or ""}


# ---------------------------------------------------------------- 解析


def parse_chart(text):
    """e添富 ajaxEtfInfoChart 的回應 → ({日期: 官方淨值}, {日期: e添富自己的折溢價}, 略過的列)。日期 YYYY/MM/DD → YYYY-MM-DD。"""
    try:
        j = json.loads(text)
    except ValueError:
        raise SourceError("e添富的回應不是 JSON")
    if not isinstance(j, dict) or not isinstance(j.get("netPrice"), list) or not j["netPrice"]:
        raise SourceError("e添富的回應沒有 netPrice（欄位：%s）" % (sorted(j.keys())[:20] if isinstance(j, dict) else type(j).__name__))
    navs, skipped = {}, []
    for x in j["netPrice"]:
        d = _ymd(x.get("date") if isinstance(x, dict) else None)
        v = fd.to_float(x.get("count")) if isinstance(x, dict) else None
        if d is None or v is None or v <= 0:
            skipped.append({"date": x.get("date") if isinstance(x, dict) else None, "why": "淨值是 0 或解析不出來"})
            continue
        navs[d] = v
    prem = {}
    for x in (j.get("atmps") or []):
        d = _ymd(x.get("date") if isinstance(x, dict) else None)
        v = fd.to_float(x.get("count")) if isinstance(x, dict) else None
        if d and v is not None:
            prem[d] = v
    if not navs:
        raise SourceError("e添富的 netPrice 一列都解析不出來")
    return navs, prem, skipped


def _ymd(s):
    try:
        dt = datetime.strptime(str(s).strip(), "%Y/%m/%d")
    except (TypeError, ValueError):
        return None
    return dt.strftime("%Y-%m-%d")


def parse_yahoo(text):
    """Yahoo chart 回應 → {日期: 收盤}（只要已定案的；日期是交易所當地日期，見 fetch_data.parse_yahoo_chart）。"""
    try:
        j = json.loads(text)
        _price, _cur, pts = fd.parse_yahoo_chart(j)
    except (ValueError, fd.FetchError) as e:
        raise SourceError("Yahoo 的回應解析不了：%s" % e)
    return dict((p["d"], p["c"]) for p in pts if p.get("d") and p.get("c") and not p.get("provisional"))


# ---------------------------------------------------------------- 比對與配對


def close_diff_pct(a, b):
    return abs(b - a) / a * 100.0 if a else None


def close_source(d, hist_closes, hist_sources):
    """收盤價那一欄的 dateSource：Yahoo 直接抓的寫 yahoo；取自 data/history 的照抄那一點自己記的來源
    （data/history:official、data/history:yahoo…），那一點沒記來源就只寫 data/history，不猜。"""
    if d not in hist_closes:
        return CLOSE_SOURCE
    src = (hist_sources or {}).get(d)
    return HIST_SOURCE + ":" + src if src else HIST_SOURCE


def plan_backfill(existing_rows, navs, hist_closes, yahoo_closes, today_str, hist_sources=None):
    """existing_rows：tw00646.json 現有的列；navs：{日期: 官方淨值}；hist_closes／yahoo_closes：{日期: 收盤}；
    hist_sources：{日期: data/history 那一點自己記的 dateSource}（沒記的日期不在裡面）。
    回傳 (new_rows, report)。report["ok"] 是 False 就不該寫檔。"""
    report = {"threshold": {"closePct": CLOSE_THRESHOLD_PCT, "navDecimals": NAV_DECIMALS}}
    # 收盤價重疊
    both = sorted(set(hist_closes) & set(yahoo_closes))
    close_over = [{"d": d, "history": hist_closes[d], "yahoo": yahoo_closes[d], "diffPct": round(close_diff_pct(hist_closes[d], yahoo_closes[d]), 4)}
                  for d in both if close_diff_pct(hist_closes[d], yahoo_closes[d]) > CLOSE_THRESHOLD_PCT]
    report["closeOverlap"] = {"compared": len(both), "same": sum(1 for d in both if hist_closes[d] == yahoo_closes[d]),
                              "maxDiffPct": round(max([close_diff_pct(hist_closes[d], yahoo_closes[d]) for d in both] or [0.0]), 4),
                              "over": close_over}
    # 官方淨值重疊（既有列 vs e添富）
    existing = dict((r["d"], r) for r in existing_rows if r.get("d"))
    nav_same, nav_over, only_existing, only_source = 0, [], [], []
    for d, r in sorted(existing.items()):
        mine = r.get("officialNav")
        if isinstance(mine, (int, float)):
            if d in navs:
                if round(mine, NAV_DECIMALS) == round(navs[d], NAV_DECIMALS):
                    nav_same += 1
                else:
                    nav_over.append({"d": d, "existing": mine, "etfortune": navs[d]})
            else:
                only_existing.append({"d": d, "existing": mine})
        elif d in navs:
            only_source.append({"d": d, "etfortune": navs[d]})
    report["navOverlap"] = {"compared": nav_same + len(nav_over), "same": nav_same, "over": nav_over,
                            "existingHasNavSourceDoesNot": only_existing, "existingLacksNavSourceHas": only_source}
    # 配對：只補現有檔案沒有的日期
    new_rows, skipped, from_hist, from_yahoo = [], [], 0, 0
    for d in sorted(navs):
        if d in existing:
            continue
        nav = navs[d]
        if d in hist_closes:
            close = hist_closes[d]
            from_hist += 1
        elif d in yahoo_closes:
            close = yahoo_closes[d]
            from_yahoo += 1
        else:
            skipped.append(d)
            continue
        new_rows.append({"d": d, "officialNav": nav, "officialClose": close, "officialPremiumPct": az.premium_pct(close, nav),
                         "officialTag": "確定", "officialLabel": az.LABEL_BACKFILL, "officialNavSeenOn": today_str,
                         "dateSource": {"officialNav": NAV_SOURCE, "officialClose": close_source(d, hist_closes, hist_sources)}})
    new_rows = new_rows[-az.NAV_MAX_ROWS:]
    report["added"] = {"rows": len(new_rows), "from": new_rows[0]["d"] if new_rows else None, "through": new_rows[-1]["d"] if new_rows else None,
                       "closeFrom": {"data/history": from_hist, "yahoo": from_yahoo}}
    report["skippedNoClose"] = {"count": len(skipped), "dates": skipped}
    report["ok"] = not close_over and not nav_over
    return new_rows, report


def premium_check(new_rows, source_prem):
    """對照：我們算的折溢價跟 e添富自己的（atmps）差多少（只列出來，不擋、不寫進檔）。"""
    diffs = [abs(r["officialPremiumPct"] - source_prem[r["d"]]) for r in new_rows if r["d"] in source_prem and r.get("officialPremiumPct") is not None]
    return {"compared": len(diffs), "maxAbsDiffPct": round(max(diffs), 3) if diffs else None}


# ---------------------------------------------------------------- 寫檔


def build_file(new_rows, report, net_log, today_str):
    head = {"id": AID, "symbol": SYMBOL,
            "source": "官方每日淨值：證交所 ETF e添富（%s，同一個 session 先開 %s）；收盤價：data/history/%s.json 有的日期取那裡"
                      "（那一點自己記的來源照抄進 dateSource），沒有的回補時直接抓 Yahoo %s 日線"
                      % (CHART_URL, PAGE_URL, AID, YAHOO_SYMBOL),
            "note": "一次性回補（分析系列 A1-4）。scripts/analyze.py 每天 15:30 那一輪把這裡的列只增不改地併進 %s.json（只補那邊沒有的日期）。"
                    "每一列只有「確定」口徑：折溢價＝(同一天的收盤 − 官方淨值) ÷ 官方淨值，是算出來的；dateSource 寫每個欄位的來源。" % AID,
            "backfilledOn": today_str,
            "requests": net_log,
            "closeFrom": report["added"]["closeFrom"],
            "count": len(new_rows), "firstDate": new_rows[0]["d"] if new_rows else None, "lastDate": new_rows[-1]["d"] if new_rows else None}
    lines = ["{"]
    for k, v in head.items():
        lines.append("  %s: %s," % (json.dumps(k, ensure_ascii=False), json.dumps(v, ensure_ascii=False)))
    lines.append('  "rows": [')
    for i, r in enumerate(new_rows):
        lines.append("    " + json.dumps(r, ensure_ascii=False, separators=(",", ":")) + ("," if i < len(new_rows) - 1 else ""))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def load_json_points(path):
    try:
        with io.open(path, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("points") or []
    except (OSError, ValueError):
        return []


def main(argv=None, net=None, now=None, paths=None):
    ap = argparse.ArgumentParser(description="00646 官方淨值一次回補三年（A1-4）")
    ap.add_argument("--dry-run", action="store_true", help="只抓、比對、列出結果，不寫檔")
    ap.add_argument("--save-raw", help="原始回應存到這個資料夾（要在倉庫外）")
    ap.add_argument("--from-raw", help="不發任何請求，讀這個資料夾裡存下來的原始回應")
    ap.add_argument("--report", help="比對結果另外存成這個 JSON 檔（要在倉庫外）")
    a = ap.parse_args(argv)
    paths = paths or az.default_paths()
    now = now or datetime.now(TPE)
    today, today_str = now.date(), now.strftime("%Y-%m-%d")
    for p in (a.save_raw, a.from_raw, a.report):
        if p and os.path.abspath(p).replace("\\", "/").lower().startswith(ROOT.replace("\\", "/").lower() + "/"):
            print("原始回應與報告要放在倉庫外：%s" % p)
            return 2
    net_log = []
    try:
        if a.from_raw:
            raw = {}
            for k in ("page", "chart", "yahoo"):
                with io.open(os.path.join(a.from_raw, RAW_FILES[k]), encoding="utf-8") as fh:
                    raw[k] = fh.read()
            try:
                with io.open(os.path.join(a.from_raw, RAW_FILES["log"]), encoding="utf-8") as fh:
                    net_log = json.load(fh)
            except (OSError, ValueError):
                net_log = []
            print("不發請求：讀 %s 裡存下來的原始回應" % a.from_raw)
        else:
            net = net or OneShot()
            try:
                raw = fetch_all(net, today)
            finally:
                net_log = list(net.log)
                print("發了 %d 個請求：%s" % (len(net_log), "；".join("%s %s%s → %s" % (x["method"], x["host"], x["path"], x.get("status")) for x in net_log)))
            if a.save_raw:
                os.makedirs(a.save_raw, exist_ok=True)
                for k in ("page", "chart", "yahoo"):
                    with io.open(os.path.join(a.save_raw, RAW_FILES[k]), "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(raw[k])
                with io.open(os.path.join(a.save_raw, RAW_FILES["log"]), "w", encoding="utf-8", newline="\n") as fh:
                    json.dump(net_log, fh, ensure_ascii=False, indent=1)
        navs, source_prem, bad = parse_chart(raw["chart"])
        yahoo = parse_yahoo(raw["yahoo"])
    except (SourceError, net_policy.HostNotAllowed, OSError) as e:
        print("停：%s。什麼都沒寫。" % e)
        return 2
    hist_points = [p for p in load_json_points(os.path.join(paths["history"], "%s.json" % AID))
                   if p.get("d") and p.get("c") and not p.get("provisional")]
    hist = dict((p["d"], p["c"]) for p in hist_points)
    hist_sources = dict((p["d"], p["dateSource"]) for p in hist_points if p.get("dateSource"))
    existing = az.load_nav(AID, paths)
    new_rows, report = plan_backfill(existing, navs, hist, yahoo, today_str, hist_sources=hist_sources)
    report["source"] = {"navRows": len(navs), "navFrom": min(navs), "navThrough": max(navs), "navSkipped": bad,
                        "yahooRows": len(yahoo), "yahooFrom": min(yahoo) if yahoo else None, "yahooThrough": max(yahoo) if yahoo else None,
                        "historyRows": len(hist)}
    report["premiumVsEtfortune"] = premium_check(new_rows, source_prem)
    report["requests"] = net_log
    if a.report:
        with io.open(a.report, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
    print("e添富：%d 天官方淨值（%s～%s）；Yahoo：%d 天收盤；data/history：%d 天收盤" % (
        len(navs), min(navs), max(navs), len(yahoo), len(hist)))
    co, no = report["closeOverlap"], report["navOverlap"]
    print("收盤價重疊 %d 天：一模一樣 %d 天，最大差 %.4f%%，超過 %.1f%% 的 %d 天" % (co["compared"], co["same"], co["maxDiffPct"], CLOSE_THRESHOLD_PCT, len(co["over"])))
    print("官方淨值重疊 %d 天：一樣 %d 天，不一樣 %d 天；既有列有、e添富沒有 %d 天；既有列沒有、e添富有 %d 天" % (
        no["compared"], no["same"], len(no["over"]), len(no["existingHasNavSourceDoesNot"]), len(no["existingLacksNavSourceHas"])))
    print("會補 %d 列（%s～%s；收盤價取自 data/history %d 天、Yahoo %d 天）；兩邊都沒有收盤價而略過 %d 天" % (
        report["added"]["rows"], report["added"]["from"], report["added"]["through"], report["added"]["closeFrom"]["data/history"],
        report["added"]["closeFrom"]["yahoo"], report["skippedNoClose"]["count"]))
    if not report["ok"]:
        print("停：重疊的比對不過，什麼都沒寫。明細在 --report 的檔或上面。")
        for x in co["over"][:10] + no["over"][:10]:
            print("  %s" % json.dumps(x, ensure_ascii=False))
        return 1
    if a.dry_run:
        print("dry-run：沒有寫檔。")
        return 0
    out = az.nav_backfill_path(AID, paths)
    az.assert_write_allowed(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with io.open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(build_file(new_rows, report, net_log, today_str))
    print("寫好了：data/analysis/nav/%s（%d 列）" % (os.path.basename(out), len(new_rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
