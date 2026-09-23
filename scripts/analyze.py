#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze.py — InvestWatch 分析系列：每天 15:30 review 那一輪算一次的「風險」與「拆解」（A1-1）

這支程式只在雲端 review（15:30）那一次完整更新裡跑（.github/workflows/update-data.yml 只在
slot=review 呼叫它），做三件事：

  1. 維護 data/history-long/<id>.json：Yahoo 週線全歷史（雲端負責、有 Yahoo 代號的標的）
     ＋ USD/TWD 週線（FinMind 轉載的台銀每日牌價，每週取最後一個營業日）。
     一週實際只補抓一次：最後一根完成週棒超過 7 天才發請求。
  2. 算 data/analysis/risk.json：年化波動（1 年／5 年）、最大回檔、目前距歷史高點的回檔、
     相關係數矩陣（3 年、週報酬、ISO 週對齊、成對可用）。
  3. 算 data/analysis/decompose.json：台銀台幣金價 ≈ 國際金價 × USD/TWD ÷ 31.1035 ＋ 殘差；
     00646 月報酬 ≈ S&P 500 月報酬 ＋ 匯率效果 ＋ 殘差。

每一次都寫 data/analysis/status.json（上次執行時間、有沒有全部產出、錯誤清單）——
分析壞掉時頁面要看得出來，不能讓人以為數字是新的。

誠實規則（測試釘住）：
  * 每個日期都來自來源的時間戳（Yahoo K 棒的時間戳、FinMind 的 date 欄），絕不用「現在」充當資料日期。
  * Yahoo 週線用 period1=345600（1970-01-05，星期一）&period2=<現在>&interval=1wk，並檢查回應宣告的粒度
    【與】實際時間間距（range=max 會無聲地變成月線）；進行中的當週 K 棒（起始＋7 天 > 現在）不收。
    period1 一定要是星期一：Yahoo 把「一週」的起點對齊 period1 那一天的星期幾——用 0（1970-01-01 是星期四）
    會讓歷史夠長的 ^GSPC 變成「週四起」的週，跟其他標的差半週（2026-09-24 實跑發現）。
  * 視窗內資料不夠（缺 >10%）就寫「資料不足」，不硬算；00679B 的 10 年視窗一律「資料不足，2027-01 起才滿 10 年」。
  * 每個數字帶資料截止（哪一週／哪一天）、視窗長度、資料標籤（估算／單一來源／有對照／多來源一致）。
  * 主機白名單（scripts/net_policy.py）：不在名單上的主機連線都不發；台銀永遠拒絕。
  * 這支程式與它的輸出永遠不出現任何投資判斷用語；輸出只有事實與標籤。

結束碼：0 全部產出；2 有東西沒產出（status.json 的 errors 有內容）；1 程式本身壞掉。
workflow 對這一步 continue-on-error：分析壞掉絕不卡住行情與報告。

用法：
    python scripts/analyze.py --slot review               # 正式
    python scripts/analyze.py --slot review --offline     # 不連網：只用倉庫裡已有的 history-long 重算
    python scripts/analyze.py --slot review --dry-run     # 只列出這一輪會發哪些請求
"""
import argparse
import json
import math
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote as url_quote

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fetch_data as fd          # noqa: E402  共用 Fetcher、BROWSER_HEADERS、FinMind 解析、日線歷史讀取
import net_policy                # noqa: E402  主機白名單（台銀永遠拒絕）

DATA_DIR = os.path.join(ROOT, "data")
LONG_DIR = os.path.join(DATA_DIR, "history-long")
ANALYSIS_DIR = os.path.join(DATA_DIR, "analysis")
ASSETS_FILE = os.path.join(DATA_DIR, "assets.json")
LATEST_FILE = os.path.join(DATA_DIR, "latest.json")

TPE = timezone(timedelta(hours=8))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DAY = 86400
WEEK = 7 * DAY

GRAMS_PER_TROY_OUNCE = 31.1035          # 1 金衡盎司 = 31.1035 公克（台銀黃金存摺以公克計價）
FX_LONG_ID = "fx_usd"
FX_LONG_START = "2006-01-01"            # FinMind TaiwanExchangeRate 的起點（A0 實測：USD 自 2006-01-03 有效）
YAHOO_PERIOD1_MONDAY = 345600           # 1970-01-05 00:00 UTC（星期一）：Yahoo 的週會對齊 period1 的星期幾，所以不能用 0
REFETCH_AFTER_DAYS = 7                  # 最後一根完成週棒超過這麼多天才補抓
INCREMENTAL_LOOKBACK_DAYS = 21          # 補抓時往前多要三週，讓合併有重疊、不會漏
WINDOWS_WEEKS = {"1y": 52, "5y": 260}
MIN_COVERAGE = 0.9                      # 視窗內缺超過 10% 就資料不足
CORR_WEEKS = 156                        # 3 年
CORR_MIN_OVERLAP = 100
TEN_YEARS_WEEKS = 520

# 資料標籤（David 的裁決：星星只給「方法」，資料的等級用文字）
LABEL_ESTIMATE, LABEL_SINGLE, LABEL_CROSSCHECKED, LABEL_MULTI = "估算", "單一來源", "有對照", "多來源一致"
LABEL_NOTES = {
    LABEL_ESTIMATE: "由公式或不同時點的資料推算出來的值，不是任何來源直接給的數字",
    LABEL_SINGLE: "只有一個來源，沒有拿別的來源對過",
    LABEL_CROSSCHECKED: "有拿別的來源核對過（核對的方法與日期寫在旁邊）",
    LABEL_MULTI: "兩個以上獨立來源給出一致的值",
}
FX_ORIGIN_NOTE = "原始出處：臺灣銀行牌告匯率（每日一筆，經 FinMind TaiwanExchangeRate 取得）"
FX_LABEL_NOTE = "有對照（2026-09-18 逐日稽核 136 天，FinMind 轉載值與台銀 CSV 完全一致）"
DATE_SOURCE_NOTES = {
    "yahoo-week": "Yahoo 週線 K 棒自己的時間戳（該週第一個交易日、交易所當地日期）；只收已完成的週",
    "finmind": "FinMind TaiwanExchangeRate 回應自己的 date 欄（台銀每日牌價）；每週取最後一個營業日",
}


class AnalyzeError(Exception):
    """這一項算不出來／抓不到（原因寫在訊息裡）；其他項照樣繼續。"""


# ==========================================================================
# 小工具
# ==========================================================================

def now_tpe():
    return datetime.now(TPE)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def local_day(epoch_seconds, gmtoffset=0):
    """epoch → 交易所當地日期。不用 datetime.fromtimestamp：Windows 對 1970 以前的負數會出錯。"""
    return (EPOCH + timedelta(seconds=int(epoch_seconds) + int(gmtoffset or 0))).strftime("%Y-%m-%d")


def parse_day(s):
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def epoch_of_day(s):
    d = parse_day(s)
    return int((datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - EPOCH).total_seconds())


def monday_of(s):
    d = parse_day(s)
    return d - timedelta(days=d.weekday())


def iso_week(s):
    y, w, _ = parse_day(s).isocalendar()
    return "%04d-W%02d" % (y, w)


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def std_sample(xs):
    n = len(xs)
    if n < 2:
        return None
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def pearson(xs, ys):
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def pct(x, nd=2):
    return None if x is None else round(x * 100.0, nd)


def sanitize(msg):
    """錯誤訊息進公開的 status.json 之前，把本機路徑洗掉（隱私鐵則：輸出不帶機器名與路徑）。"""
    s = str(msg)
    for root in (ROOT, ROOT.replace("\\", "/"), ROOT.replace("/", "\\")):
        if root:
            s = s.replace(root, "<repo>")
    return re.sub(r"[A-Za-z]:\\[^\s'\"]+", "<path>", s)[:400]


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


# ==========================================================================
# 連線：正式的 Fetcher ＋ 主機白名單
# ==========================================================================

class PolicedFetcher(fd.Fetcher):
    """fetch_data 的 Fetcher（同站禮貌間隔、429 等 10 秒重試一次），外加白名單：
    送出前先查主機；轉址的每一跳也查（requests 的 response hook）。"""

    def __init__(self, verbose=True):
        fd.Fetcher.__init__(self, verbose=verbose)
        self.session.hooks["response"].append(net_policy.response_hook)

    def get(self, url, **kw):
        net_policy.assert_host_allowed(url)
        return fd.Fetcher.get(self, url, **kw)


# ==========================================================================
# Yahoo 週線
# ==========================================================================

YAHOO_WEEKLY = "https://query1.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d&interval=1wk"


def yahoo_symbol(asset):
    """有 Yahoo 代號的標的：type=yahoo 用 symbol；台股用 yahooSymbol（00679B 是 .TWO）。"""
    if asset.get("type") == "yahoo":
        return asset.get("symbol")
    return asset.get("yahooSymbol")


def parse_yahoo_weekly(j, now_epoch=None):
    """Yahoo chart 回應 → 已完成的週線 K 棒 [{d, c, dateSource:"yahoo-week"}]。

    三道檢查，任何一道不過就丟 AnalyzeError、一根都不收：
      1. 回應宣告的粒度必須是 1wk（range=max 那種寫法會被無聲地換成 1mo，HTTP 照樣 200）。
      2. K 棒時間戳的中位間距必須是 6～8 天（宣告週線、實際月線也擋得住）。
      3. 進行中的當週不收：起始時間＋7 天 > 現在的那根還沒走完。
    另外：收盤價 null 的（整週休市）直接略過；尾端跟前一根同值、間距不到 6.5 天的即時點去掉。
    """
    chart = (j or {}).get("chart") or {}
    if chart.get("error"):
        raise AnalyzeError("Yahoo 回應錯誤：%s" % chart["error"])
    results = chart.get("result") or []
    if not results:
        raise AnalyzeError("Yahoo 沒有回傳 result")
    res = results[0]
    meta = res.get("meta") or {}
    gran = meta.get("dataGranularity")
    if gran != "1wk":
        raise AnalyzeError("要的是週線（1wk），Yahoo 回的是 %s——這種回應不收（range=max 會無聲地變成月線）" % gran)
    ts = res.get("timestamp") or []
    closes = ((((res.get("indicators") or {}).get("quote")) or [{}])[0] or {}).get("close") or []
    off = meta.get("gmtoffset")
    off = int(off) if isinstance(off, (int, float)) else 0
    pairs = [(int(t), float(c)) for t, c in zip(ts, closes) if c is not None and float(c) > 0]
    if len(pairs) < 2:
        raise AnalyzeError("有效的週線 K 棒不到 2 根")
    gaps = [(b[0] - a[0]) / float(DAY) for a, b in zip(pairs, pairs[1:])]
    med = median(gaps)
    if not (6.0 <= med <= 8.0):
        raise AnalyzeError("宣告是週線，但 K 棒的中位間距是 %.1f 天——不是真的週線，不收" % med)
    now_epoch = time.time() if now_epoch is None else now_epoch
    complete = [(t, c) for t, c in pairs if t + WEEK <= now_epoch]
    dropped = len(pairs) - len(complete)
    if len(complete) >= 2 and complete[-1][1] == complete[-2][1] and complete[-1][0] - complete[-2][0] < 6.5 * DAY:
        complete.pop()
    pts, seen = [], set()
    for t, c in complete:
        d = local_day(t, off)
        if d in seen:
            continue
        seen.add(d)
        pts.append({"d": d, "c": round(c, 6), "dateSource": "yahoo-week"})
    ftd = meta.get("firstTradeDate")
    weekdays = {}
    for p in pts:
        wd = parse_day(p["d"]).strftime("%a")
        weekdays[wd] = weekdays.get(wd, 0) + 1
    info = {"granularity": gran, "droppedInProgress": dropped, "currency": meta.get("currency"),
            "exchange": meta.get("exchangeName"),
            "firstTradeDate": local_day(ftd, off) if isinstance(ftd, (int, float)) else None,
            "dominantWeekday": max(weekdays, key=weekdays.get) if weekdays else None}
    return pts, info


def fetch_yahoo_weekly(f, symbol, period1, period2, now_epoch=None):
    url = YAHOO_WEEKLY % (url_quote(symbol, safe=""), int(period1), int(period2))
    j = f.get(url, delay=2.0, expect_json=True,
              headers={"Accept": "application/json, text/plain, */*"})
    return parse_yahoo_weekly(j, now_epoch)


# ==========================================================================
# FinMind 匯率 → 週線
# ==========================================================================

def weekly_from_daily_fx(rows, today=None):
    """FinMind 的每日列 → 每個 ISO 週取最後一個營業日那一列。回傳 [{d, c, spotBuy, spotSell, dateSource:"finmind"}]。
    d 是那個營業日自己的日期（不是週一）；對齊時用 ISO 週。
    today 有給的話，today 所在的那一週還沒走完，不收（跟 Yahoo 週棒「進行中不收」同一個道理）。"""
    by_week = {}
    skip_week = iso_week(today.strftime("%Y-%m-%d")) if today else None
    for r in rows:
        if r.get("spotSell") is None:
            continue
        wk = iso_week(r["date"])
        if wk == skip_week:
            continue
        if wk not in by_week or r["date"] > by_week[wk]["date"]:
            by_week[wk] = r
    out = []
    for wk in sorted(by_week, key=lambda k: by_week[k]["date"]):
        r = by_week[wk]
        out.append({"d": r["date"], "c": r["spotSell"], "spotBuy": r.get("spotBuy"), "spotSell": r["spotSell"],
                    "dateSource": "finmind"})
    return out


def fetch_fx_weekly(f, code, start_date, today=None):
    rows = fd.fetch_finmind_fx(f, code, start_date)
    return weekly_from_daily_fx(rows, today)


# ==========================================================================
# history-long 的讀寫與更新規則
# ==========================================================================

def long_path(aid):
    return os.path.join(LONG_DIR, "%s.json" % aid)


def load_long(aid):
    p = long_path(aid)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def dump_long(head, points):
    """跟 data/history 一樣一點一行，在 GitHub 上看得出每週差了什麼。"""
    lines = ["{"]
    for k, v in head.items():
        lines.append("  %s: %s," % (json.dumps(k, ensure_ascii=False), json.dumps(v, ensure_ascii=False)))
    lines.append('  "points": [')
    for i, p in enumerate(points):
        lines.append("    " + json.dumps(p, ensure_ascii=False, separators=(",", ":")) + ("," if i < len(points) - 1 else ""))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def save_long(aid, head, points):
    os.makedirs(LONG_DIR, exist_ok=True)
    head = dict(head)
    head["count"] = len(points)
    head["firstDate"] = points[0]["d"] if points else None
    head["lastDate"] = points[-1]["d"] if points else None
    head["updatedAt"] = iso(now_tpe())
    with open(long_path(aid), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dump_long(head, points))


def merge_weekly(old, new, key=None):
    """以「哪一週」為鍵合併（Yahoo 用 d 本身；匯率用 ISO 週），新的蓋舊的、依日期排序。"""
    key = key or (lambda p: p["d"])
    table = {}
    for p in old or []:
        table[key(p)] = dict(p)
    for p in new or []:
        table[key(p)] = dict(p)
    return sorted(table.values(), key=lambda p: p["d"])


def refetch_plan(existing_points, today):
    """要不要補抓、從哪裡開始。回傳 (mode, start_date_or_None, reason)。
    mode：full（整段）／incremental（從最後一根往前 21 天）／skip（最後一根還沒超過 7 天）。"""
    if not existing_points:
        return "full", None, "沒有長歷史，整段回補"
    last = parse_day(existing_points[-1]["d"])
    age = (today - last).days
    if age > REFETCH_AFTER_DAYS:
        start = last - timedelta(days=INCREMENTAL_LOOKBACK_DAYS)
        return "incremental", start.strftime("%Y-%m-%d"), "最後一根 %s 已 %d 天，補抓 %s 起" % (last, age, start)
    return "skip", None, "最後一根 %s 才 %d 天，這週不必抓" % (last, age)


def long_targets(assets):
    """要維護長歷史的標的：雲端負責且有 Yahoo 代號的，加上 USD/TWD（FinMind）。"""
    out = []
    for a in assets:
        if not a.get("enabled", True) or (a.get("owner") or "cloud") != "cloud":
            continue
        sym = yahoo_symbol(a)
        if sym:
            out.append({"id": a["id"], "kind": "yahoo", "symbol": sym, "asset": a})
        elif a["id"] == FX_LONG_ID and a.get("type") == "finmind_fx":
            out.append({"id": a["id"], "kind": "finmind", "symbol": a.get("symbol") or "USD", "asset": a})
    return out


def update_long_history(target, f, now, status, only=None, offline=False, dry_run=False):
    """維護一個標的的 history-long。回傳這一項最後的 points（可能是舊的）。"""
    aid, a = target["id"], target["asset"]
    existing = load_long(aid)
    old_pts = (existing or {}).get("points") or []
    entry = {"points": len(old_pts), "lastDate": old_pts[-1]["d"] if old_pts else None, "action": None}
    status["longHistory"][aid] = entry
    if only and aid not in only:
        entry["action"] = "略過（--only）"
        return old_pts
    mode, start, reason = refetch_plan(old_pts, now.date())
    if offline:
        entry["action"] = "offline：不連網，沿用檔案（%s）" % reason
        return old_pts
    if mode == "skip":
        entry["action"] = reason
        return old_pts
    if target["kind"] == "yahoo":
        # period1 一律落在星期一（整段用 1970-01-05；補抓用起點那一週的星期一），週的起點才會對齊
        p1 = YAHOO_PERIOD1_MONDAY if mode == "full" else epoch_of_day(monday_of(start).strftime("%Y-%m-%d"))
        url_desc = "Yahoo %s 週線 period1=%d" % (target["symbol"], p1)
    else:
        p1 = FX_LONG_START if mode == "full" else start
        url_desc = "FinMind TaiwanExchangeRate %s 自 %s" % (target["symbol"], p1)
    if dry_run:
        entry["action"] = "dry-run：會發請求（%s；%s）" % (reason, url_desc)
        return old_pts
    if target["kind"] == "yahoo":
        new_pts, info = fetch_yahoo_weekly(f, target["symbol"], p1, int(now.timestamp()), now_epoch=now.timestamp())
        merged = merge_weekly(old_pts, new_pts)
        head = {"id": aid, "name": a["name"], "symbol": target["symbol"], "interval": "1wk", "source": "yahoo",
                "sourceLabel": "Yahoo Finance chart API（週線）", "currency": a.get("currency"), "unit": a.get("unit"),
                "dateSourceNote": DATE_SOURCE_NOTES["yahoo-week"],
                "note": "只收已完成的週（起始＋7 天 ≤ 抓取當下）；最後一根完成週棒超過 7 天才補抓。"
                        "period1 用 1970-01-05（星期一）：Yahoo 把週的起點對齊 period1 的星期幾，用 0 會讓 ^GSPC 變成週四起的週；"
                        "所以只回 1970 以後（Yahoo 對 ^GSPC 其實從 1927 就有，分析用不到更早）。",
                "barsStartOn": info.get("dominantWeekday"),
                "firstTradeDateAtYahoo": info.get("firstTradeDate")}
        if a["id"] == "tw00679b":
            head["note"] += " 00679B 只有 2017-01 起，10 年視窗要到 2027-01 才夠。"
        save_long(aid, head, merged)
        entry.update({"action": "%s：抓到 %d 根（略過進行中 %d 根），合併後 %d 根" % (mode, len(new_pts), info["droppedInProgress"], len(merged)),
                      "points": len(merged), "lastDate": merged[-1]["d"] if merged else None,
                      "barsStartOn": info.get("dominantWeekday")})
        if info.get("dominantWeekday") not in (None, "Mon"):
            status["warnings"].append("history-long %s 的週棒多半從 %s 開始，不是星期一——跟其他標的的 ISO 週對齊會差幾天"
                                      % (aid, info["dominantWeekday"]))
        return merged
    new_pts = fetch_fx_weekly(f, target["symbol"], p1, today=now.date())
    merged = merge_weekly(old_pts, new_pts, key=lambda p: iso_week(p["d"]))
    head = {"id": aid, "name": a["name"], "symbol": target["symbol"], "interval": "1wk", "source": "finmind",
            "sourceLabel": fd.FINMIND_FX_LABEL, "originNote": FX_ORIGIN_NOTE, "currency": a.get("currency"), "unit": a.get("unit"),
            "dateSourceNote": DATE_SOURCE_NOTES["finmind"], "crossCheck": FX_LABEL_NOTE,
            "note": "c 是即期賣出；每個 ISO 週取最後一個營業日那一筆，抓取當週還沒走完所以不收。FinMind 用 -1 代表沒有牌價，那些列在解析時就丟掉了。"}
    save_long(aid, head, merged)
    entry.update({"action": "%s：抓到 %d 週，合併後 %d 週" % (mode, len(new_pts), len(merged)),
                  "points": len(merged), "lastDate": merged[-1]["d"] if merged else None})
    return merged


# ==========================================================================
# 風險：波動、回檔、相關矩陣（全部是純函式，輸入 [{d, c}]）
# ==========================================================================

def weekly_returns(points):
    """[(d, r)]：r = 這一根 ÷ 前一根 − 1。相鄰兩根中間若缺一週（休市），這個報酬就跨了兩週，照算不補。"""
    out = []
    for a, b in zip(points, points[1:]):
        if a.get("c") and b.get("c") and a["c"] > 0:
            out.append((b["d"], b["c"] / a["c"] - 1.0))
    return out


def annualized_vol(returns, weeks):
    """最近 weeks 個週報酬的樣本標準差 × √52。不夠 90% 就資料不足。"""
    need = int(math.ceil(weeks * MIN_COVERAGE))
    tail = returns[-weeks:]
    if len(tail) < need:
        return {"pct": None, "weeks": len(tail), "window": "%d 週" % weeks,
                "reason": "資料不足：只有 %d 週（至少要 %d 週）" % (len(tail), need)}
    sd = std_sample([r for _d, r in tail])
    return {"pct": round(sd * math.sqrt(52) * 100.0, 2), "weeks": len(tail), "window": "%d 週" % weeks,
            "from": tail[0][0], "through": tail[-1][0]}


def max_drawdown(points):
    """整段可用區間的最大回檔：從歷史高點到之後最低點，最多跌掉幾 %。"""
    peak, peak_d, best = None, None, None
    for p in points:
        c = p.get("c")
        if c is None:
            continue
        if peak is None or c > peak:
            peak, peak_d = c, p["d"]
        dd = c / peak - 1.0
        if best is None or dd < best["dd"]:
            best = {"dd": dd, "peakDate": peak_d, "peakValue": peak, "troughDate": p["d"], "troughValue": c}
    if best is None:
        return None
    recovered = None
    for p in points:
        if p["d"] > best["troughDate"] and p.get("c") is not None and p["c"] >= best["peakValue"]:
            recovered = p["d"]
            break
    return {"pct": round(best["dd"] * 100.0, 2), "peakDate": best["peakDate"], "peakValue": best["peakValue"],
            "troughDate": best["troughDate"], "troughValue": best["troughValue"], "recoveredDate": recovered,
            "range": "%s～%s" % (points[0]["d"], points[-1]["d"]), "bars": len(points)}


def current_drawdown(points):
    """最後一根距整段歷史高點的回檔。"""
    if not points:
        return None
    hi = max(points, key=lambda p: p.get("c") or -1)
    last = points[-1]
    return {"pct": round((last["c"] / hi["c"] - 1.0) * 100.0, 2), "highDate": hi["d"], "highValue": hi["c"],
            "asOf": last["d"], "lastValue": last["c"]}


def returns_by_week(points):
    """{該週的星期一: r}，給相關矩陣用的 ISO 週對齊。"""
    return {monday_of(d): r for d, r in weekly_returns(points)}


def correlation_matrix(series, weeks=CORR_WEEKS, min_overlap=CORR_MIN_OVERLAP):
    """成對可用：每一對各自取兩邊都有的最近 weeks 週；重疊不到 min_overlap 週就寫資料不足。
    對角線一定要是 1（算出來不是 1 就是程式壞了，寫進 errors）。"""
    rbw = {aid: returns_by_week(pts) for aid, pts in series.items()}
    ids = sorted(rbw)
    matrix, problems = {}, []
    for a in ids:
        matrix[a] = {}
        for b in ids:
            ra, rb = rbw[a], rbw[b]
            if not ra or not rb:
                matrix[a][b] = {"r": None, "n": 0, "reason": "資料不足"}
                continue
            end = min(max(ra), max(rb))
            start = end - timedelta(weeks=weeks - 1)
            common = sorted(k for k in ra if k in rb and start <= k <= end)
            if len(common) < min_overlap:
                matrix[a][b] = {"r": None, "n": len(common), "reason": "資料不足：重疊只有 %d 週（至少要 %d 週）" % (len(common), min_overlap)}
                continue
            r = pearson([ra[k] for k in common], [rb[k] for k in common])
            cell = {"r": None if r is None else round(r, 3), "n": len(common),
                    "from": common[0].strftime("%Y-%m-%d"), "through": common[-1].strftime("%Y-%m-%d")}
            if a == b:
                if r is None or abs(r - 1.0) > 1e-9:
                    problems.append("相關矩陣的對角線 %s 不是 1（算出 %s）——計算有誤，這張矩陣不可信" % (a, r))
                else:
                    cell["r"] = 1.0
            matrix[a][b] = cell
    return {"ids": ids, "matrix": matrix}, problems


def ten_year_window(points):
    """10 年視窗夠不夠：第一根到最後一根要滿 10 個曆年。不夠就講清楚幾時才夠（00679B：2027-01）。"""
    if not points:
        return {"available": False, "bars": 0, "reason": "資料不足"}
    first, last = parse_day(points[0]["d"]), parse_day(points[-1]["d"])
    try:
        ready = first.replace(year=first.year + 10)
    except ValueError:                                   # 2 月 29 日
        ready = first.replace(year=first.year + 10, day=28)
    if last >= ready:
        return {"available": True, "bars": len(points), "from": points[0]["d"], "through": points[-1]["d"]}
    return {"available": False, "bars": len(points), "from": points[0]["d"],
            "reason": "資料不足，%s 起才滿 10 年" % ready.strftime("%Y-%m")}


def build_risk(series, assets_by_id, now):
    out = {"generatedAt": iso(now), "slot": "review",
           "method": {
               "returns": "週報酬＝該週完成週棒收盤 ÷ 前一週 − 1；相鄰兩根中間缺週（休市）的報酬跨週照算",
               "volatility": "最近 52／260 個週報酬的樣本標準差 × √52，以 % 表示；視窗內缺超過 10% 就寫資料不足",
               "maxDrawdown": "整段可用區間（各檔起點不同）從歷史高點到之後最低點的跌幅；recoveredDate 是之後首次回到高點的那一週",
               "currentDrawdown": "最後一根完成週棒距整段歷史高點的跌幅",
               "correlation": "皮爾森相關，週報酬、ISO 週對齊、成對可用（每一對各自取兩邊都有的最近 156 週），重疊不到 100 週寫資料不足",
               "weekDate": "週棒的日期是該週第一個交易日（Yahoo）或該週最後一個營業日（匯率）；對齊一律用 ISO 週",
           },
           "labels": LABEL_NOTES,
           "notes": [
               "所有數字都來自公開市場資料（Yahoo 週線、FinMind 轉載的台銀匯率），資料標籤寫在每個數字旁邊。",
               "幣別混用：台股與匯率是台幣、美股與商品是美元、比特幣是美元計價；相關係數是各自幣別的報酬，不含匯率換算。",
               "這裡只有事實與資料標籤，沒有任何判斷。",
           ],
           "assets": {}, "correlation": None, "problems": []}
    for aid, pts in sorted(series.items()):
        a = assets_by_id.get(aid, {})
        rets = weekly_returns(pts)
        label = LABEL_CROSSCHECKED if aid == FX_LONG_ID else LABEL_SINGLE
        entry = {"name": a.get("name"), "assetClass": a.get("assetClass"), "currency": a.get("currency"),
                 "bars": len(pts), "firstBar": pts[0]["d"] if pts else None, "lastBar": pts[-1]["d"] if pts else None,
                 "lastWeek": iso_week(pts[-1]["d"]) if pts else None, "dataLabel": label,
                 "volatility": {k: dict(annualized_vol(rets, w), label=label) for k, w in WINDOWS_WEEKS.items()},
                 "maxDrawdown": dict(max_drawdown(pts) or {}, label=label) if pts else None,
                 "currentDrawdown": dict(current_drawdown(pts) or {}, label=label) if pts else None,
                 "tenYearWindow": ten_year_window(pts)}
        if aid == FX_LONG_ID:
            entry["originNote"] = FX_ORIGIN_NOTE
            entry["crossCheck"] = FX_LABEL_NOTE
        out["assets"][aid] = entry
    corr, problems = correlation_matrix(series)
    corr.update({"window": "3 年（156 週）", "minOverlap": CORR_MIN_OVERLAP, "label": LABEL_SINGLE,
                 "note": "每一格的 n 是實際重疊的週數，from／through 是那一對的實際區間"})
    out["correlation"] = corr
    out["problems"] = problems
    return out, problems


# ==========================================================================
# 拆解：台銀金價、00646
# ==========================================================================

def implied_gold_twd_per_gram(gc_usd_per_oz, usdtwd):
    return gc_usd_per_oz * usdtwd / GRAMS_PER_TROY_OUNCE


def fx_mid(p):
    b, s = p.get("spotBuy"), p.get("spotSell")
    if b and s:
        return (b + s) / 2.0
    return s or p.get("c")


def decompose_gold(gold_pts, gc_pts, fx_pts, live=None):
    """台銀台幣金價（每公克，本行賣出）≈ 國際金價（美元／盎司）× USD/TWD ÷ 31.1035 ＋ 殘差。
    兩個口徑都算：同一天的 GC=F 收盤（事後才知道；美國那天收盤時台北已是隔天早上）、前一個交易日的 GC=F 收盤
    （台銀 15:00 掛牌時實際看得到的最新收盤）。殘差＝台銀價差＋期貨與現貨的基差＋時間差。"""
    gc = {p["d"]: p["c"] for p in gc_pts if p.get("c") and not p.get("provisional")}
    gc_days = sorted(gc)
    fx = {p["d"]: p for p in fx_pts if p.get("spotSell")}
    daily = []
    for g in gold_pts:
        d = g.get("d")
        sell, buy = g.get("sell"), g.get("buy")
        if not d or not sell or d not in fx:
            continue
        mid = fx_mid(fx[d])
        row = {"d": d, "goldSell": sell, "goldBuy": buy, "fxDate": d, "fxMid": round(mid, 4)}
        if d in gc:
            imp = implied_gold_twd_per_gram(gc[d], mid)
            row.update({"gcDate": d, "gc": gc[d], "impliedSameDay": round(imp, 2),
                        "residualSameDayPct": round((sell / imp - 1.0) * 100.0, 3)})
        prev = [x for x in gc_days if x < d]
        if prev:
            pd_ = prev[-1]
            imp2 = implied_gold_twd_per_gram(gc[pd_], mid)
            row.update({"gcPrevDate": pd_, "gcPrev": gc[pd_], "impliedPrevDay": round(imp2, 2),
                        "residualPrevDayPct": round((sell / imp2 - 1.0) * 100.0, 3)})
        if "residualSameDayPct" in row or "residualPrevDayPct" in row:
            daily.append(row)
    daily = daily[-400:]

    def summary(key):
        xs = [r[key] for r in daily if key in r]
        tail = xs[-60:]
        if not tail:
            return {"n": 0, "reason": "資料不足"}
        return {"n": len(tail), "medianPct": round(median(tail), 3), "meanPct": round(mean(tail), 3),
                "minPct": round(min(tail), 3), "maxPct": round(max(tail), 3), "window": "最近 %d 個有資料的日子" % len(tail),
                "from": [r["d"] for r in daily if key in r][-len(tail)], "through": [r["d"] for r in daily if key in r][-1]}

    out = {"formula": "台銀黃金存摺的本行賣出價（台幣／公克）≈ 國際金價（美元／盎司）× USD/TWD ÷ 31.1035 ＋ 殘差",
           "gramsPerTroyOunce": GRAMS_PER_TROY_OUNCE,
           "label": LABEL_ESTIMATE,
           "notes": [
               "國際金價用 COMEX 近月期貨（GC=F），不是倫敦現貨：殘差裡含期貨與現貨的基差。",
               "匯率用台銀即期買入與即期賣出的中價（FinMind 轉載的每日牌價，一天一筆）。",
               "同一天口徑：台銀當天牌價 vs GC=F 當天收盤——美國收盤時台北已是隔天早上，這是事後對照。",
               "前一日口徑：台銀當天牌價 vs GC=F 前一個交易日收盤——台銀 15:00 掛牌時實際看得到的最新收盤。",
               "殘差（%）＝台銀的隱含價差 ＋ 基差 ＋ 時間差 ＋ 四捨五入；正值表示台銀價格高於公式值。",
               "15:30 算分析時，匯率一定是前一個交易日的（FinMind 當天那一筆晚上才出現）：fxDate 照實寫。",
           ],
           "daily": daily,
           "summarySameDay": summary("residualSameDayPct"),
           "summaryPrevDay": summary("residualPrevDayPct"),
           "live": None}
    if live:
        out["live"] = live
    return out


def live_gold_row(latest):
    """用 latest.json 裡「現在」的三個數字算一次：國際金價的最新價（盤中、可能是進行中的）× 最新匯率。"""
    assets = (latest or {}).get("assets") or {}
    g, gc, fx = assets.get("gold_twd") or {}, assets.get("gold_intl") or {}, assets.get("fx_usd") or {}
    if not (g.get("status") == "ok" and gc.get("status") == "ok" and fx.get("status") == "ok"):
        return {"reason": "latest.json 裡黃金存摺、國際金價、匯率有一項不是 ok，這一格不算", "label": LABEL_ESTIMATE}
    if not (g.get("sell") and gc.get("price") and fx.get("spotSell")):
        return {"reason": "latest.json 缺欄位（sell／price／spotSell），這一格不算", "label": LABEL_ESTIMATE}
    mid = fx_mid(fx)
    imp = implied_gold_twd_per_gram(gc["price"], mid)
    return {"goldSell": g["sell"], "goldDate": g.get("date"), "goldQuoteTime": g.get("quoteTime"),
            "gcPrice": gc["price"], "gcDate": gc.get("date"), "gcFetchedAt": gc.get("fetchedAt"),
            "gcNote": "Yahoo 的最新價，可能是進行中的盤中價",
            "fxMid": round(mid, 4), "fxDate": fx.get("date"),
            "implied": round(imp, 2), "residualPct": round((g["sell"] / imp - 1.0) * 100.0, 3),
            "label": LABEL_ESTIMATE}


def month_end_closes(points):
    """每個月取最後一根（週線的最後一根起始日落在該月），近似月底收盤：跟真正的月底最多差 4 天。"""
    out = {}
    for p in points:
        ym = p["d"][:7]
        out[ym] = (p["d"], p["c"])
    return out


def decompose_00646(p646, pgspc, pfx, current_month=None):
    """00646 月報酬 ≈ (1＋S&P 500 月報酬)(1＋USD/TWD 月變動) − 1 ＋ 殘差。
    current_month（"YYYY-MM"）有給的話，那個月還沒走完，不算進去。"""
    m646, mg, mf = month_end_closes(p646), month_end_closes(pgspc), month_end_closes(pfx)
    months = sorted(m for m in set(m646) & set(mg) & set(mf) if not current_month or m < current_month)
    rows = []
    for prev, cur in zip(months, months[1:]):
        r646 = m646[cur][1] / m646[prev][1] - 1.0
        rg = mg[cur][1] / mg[prev][1] - 1.0
        rf = mf[cur][1] / mf[prev][1] - 1.0
        combined = (1 + rg) * (1 + rf) - 1.0
        rows.append({"month": cur, "d646": m646[cur][0], "r646Pct": pct(r646, 3), "dGspc": mg[cur][0], "rGspcPct": pct(rg, 3),
                     "dFx": mf[cur][0], "rFxPct": pct(rf, 3), "combinedPct": pct(combined, 3),
                     "residualPct": pct(r646 - combined, 3)})

    def summary(sub):
        if not sub:
            return {"months": 0, "reason": "資料不足"}
        res = [r["residualPct"] for r in sub]
        return {"months": len(sub), "from": sub[0]["month"], "through": sub[-1]["month"],
                "residualSumPct": round(sum(res), 3), "residualMeanPct": round(mean(res), 3),
                "residualStdPct": round(std_sample(res), 3) if len(res) >= 2 else None}

    return {"formula": "00646 月報酬 ≈ (1＋S&P 500 月報酬)(1＋USD/TWD 月變動) − 1 ＋ 殘差",
            "label": LABEL_ESTIMATE,
            "notes": [
                "月收盤用週線長歷史裡「起始日落在該月的最後一根週棒」近似，跟真正月底最多差 4 天；三個序列各自取，所以 d646／dGspc／dFx 可能不同天。",
                "還沒走完的當月不算。",
                "S&P 500 用價格指數（不含股息），00646 不配息（股息留在淨值裡）：殘差裡含股息效果（約每年 +1%）、費用、期貨替代、匯率時點差。",
                "殘差的累計（residualSumPct）是追蹤差距的近似值；標準差（residualStdPct）是追蹤誤差的近似值。",
            ],
            "monthly": rows, "summaryLast12": summary(rows[-12:]), "summaryAll": summary(rows)}


def build_decompose(series, now, problems):
    out = {"generatedAt": iso(now), "slot": "review", "labels": LABEL_NOTES,
           "notes": ["拆解全部是估算：公式與殘差的意義寫在各自的 notes；這裡只有事實與資料標籤，沒有任何判斷。"],
           "gold": None, "tw00646": None}
    try:
        gold_pts = fd.load_history("gold_twd")
        gc_pts = fd.load_history("gold_intl")
        fx_pts = fd.load_history("fx_usd")
        if not gold_pts:
            raise AnalyzeError("沒有 gold_twd 的日線歷史（data/history/gold_twd.json）")
        if not gc_pts:
            raise AnalyzeError("沒有 gold_intl 的日線歷史（第一次完整更新之後才會有 data/history/gold_intl.json）")
        if not fx_pts:
            raise AnalyzeError("沒有 fx_usd 的日線歷史")
        latest = None
        if os.path.exists(LATEST_FILE):
            with open(LATEST_FILE, encoding="utf-8") as fh:
                latest = json.load(fh)
        out["gold"] = decompose_gold(gold_pts, gc_pts, fx_pts, live=live_gold_row(latest))
        out["gold"]["inputs"] = {"gold_twd": {"points": len(gold_pts), "lastDate": gold_pts[-1]["d"]},
                                 "gold_intl": {"points": len(gc_pts), "lastDate": gc_pts[-1]["d"]},
                                 "fx_usd": {"points": len(fx_pts), "lastDate": fx_pts[-1]["d"], "originNote": FX_ORIGIN_NOTE}}
    except AnalyzeError as e:
        problems.append("拆解（黃金）：%s" % e)
        out["gold"] = {"reason": str(e)}
    try:
        need = ("tw00646", "gspc", FX_LONG_ID)
        missing = [k for k in need if not series.get(k)]
        if missing:
            raise AnalyzeError("缺長歷史：%s" % "、".join(missing))
        out["tw00646"] = decompose_00646(series["tw00646"], series["gspc"], series[FX_LONG_ID], current_month=now.strftime("%Y-%m"))
        out["tw00646"]["inputs"] = {k: {"bars": len(series[k]), "lastBar": series[k][-1]["d"]} for k in need}
    except AnalyzeError as e:
        problems.append("拆解（00646）：%s" % e)
        out["tw00646"] = {"reason": str(e)}
    return out


# ==========================================================================
# 主流程
# ==========================================================================

def load_assets():
    with open(ASSETS_FILE, encoding="utf-8") as fh:
        return json.load(fh)["assets"]


def run(slot, offline=False, dry_run=False, only=None, now=None):
    now = now or now_tpe()
    status = {"generatedAt": iso(now), "lastRun": iso(now), "slot": slot, "ok": False, "mode": "dry-run" if dry_run else "offline" if offline else "live",
              "errors": [], "warnings": [], "produced": [], "requests": 0, "longHistory": {},
              "note": "分析只在 review（15:30）那一輪跑；出錯不影響行情與報告。錯誤訊息不含機器名與路徑。"}
    code = 0
    try:
        assets = load_assets()
        by_id = {a["id"]: a for a in assets}
        f = None if (offline or dry_run) else PolicedFetcher(verbose=True)
        series = {}
        for t in long_targets(assets):
            try:
                pts = update_long_history(t, f, now, status, only=only, offline=offline, dry_run=dry_run)
                if pts:
                    series[t["id"]] = pts
                print("[%s] %s" % (t["id"], status["longHistory"][t["id"]]["action"]))
            except (AnalyzeError, fd.FetchError, net_policy.HostNotAllowed) as e:
                status["errors"].append("history-long %s：%s" % (t["id"], sanitize(e)))
                status["longHistory"][t["id"]]["action"] = "失敗：%s" % sanitize(e)
                print("[%s] 失敗：%s" % (t["id"], sanitize(e)))
                old = load_long(t["id"])
                if old and old.get("points"):
                    series[t["id"]] = old["points"]
                    status["warnings"].append("history-long %s 這次沒更新，風險用的是到 %s 的舊檔" % (t["id"], old["points"][-1]["d"]))
        if f is not None:
            status["requests"] = f.count
        if dry_run:
            print("\n（dry-run：不寫任何分析檔）")
            status["note"] += " dry-run：沒有連網、沒有寫分析檔。"
            return status, 0
        if not series:
            raise AnalyzeError("沒有任何長歷史可用，風險與拆解都算不出來")
        problems = []
        risk, rp = build_risk(series, by_id, now)
        problems += rp
        write_json(os.path.join(ANALYSIS_DIR, "risk.json"), risk)
        status["produced"].append("data/analysis/risk.json")
        dec = build_decompose(series, now, problems)
        write_json(os.path.join(ANALYSIS_DIR, "decompose.json"), dec)
        status["produced"].append("data/analysis/decompose.json")
        status["errors"] += [sanitize(p) for p in problems]
    except AnalyzeError as e:                             # 算不出來是「結果」（結束碼 2），不是程式壞掉
        status["errors"].append(sanitize(e))
    except Exception as e:                                # noqa: B902
        status["errors"].append("程式壞掉：%s：%s" % (e.__class__.__name__, sanitize(e)))
        traceback.print_exc(limit=3)
        code = 1
    status["ok"] = not status["errors"]
    status["finishedAt"] = iso(now_tpe())
    if not dry_run:
        write_json(os.path.join(ANALYSIS_DIR, "status.json"), status)
    if code == 0 and status["errors"]:
        code = 2
    return status, code


def main(argv=None):
    ap = argparse.ArgumentParser(description="InvestWatch 分析系列：風險與拆解（只在 review 那一輪跑）")
    ap.add_argument("--slot", required=True, help="這一輪的時段；正式只接受 review")
    ap.add_argument("--offline", action="store_true", help="不連網，只用倉庫裡已有的 history-long 重算")
    ap.add_argument("--dry-run", action="store_true", help="只列出會發哪些請求，不連網、不寫檔")
    ap.add_argument("--only", default=None, help="只更新這些 id 的長歷史（逗號分隔），測試用")
    ap.add_argument("--now", default=None, help="測試用：把「現在」當成這個時間（ISO 8601）")
    args = ap.parse_args(argv)
    if args.slot != "review" and not (args.offline or args.dry_run):
        print("分析只在 review（15:30）那一輪跑；收到的是 --slot %s，什麼都不做。" % args.slot)
        return 2
    now = datetime.fromisoformat(args.now) if args.now else now_tpe()
    if now.tzinfo is None:
        now = now.replace(tzinfo=TPE)
    only = set(x.strip() for x in args.only.split(",") if x.strip()) if args.only else None
    print("=" * 62)
    print("InvestWatch 分析  時段：%s  台北時間：%s%s" % (args.slot, now.strftime("%Y-%m-%d %H:%M:%S"),
                                                   "　【offline】" if args.offline else "　【dry-run】" if args.dry_run else ""))
    print("=" * 62)
    status, code = run(args.slot, offline=args.offline, dry_run=args.dry_run, only=only, now=now)
    print("\n" + "=" * 62)
    print("產出：%s" % ("、".join(status["produced"]) or "無"))
    if status["errors"]:
        print("::warning::分析有 %d 個問題：%s" % (len(status["errors"]), "；".join(status["errors"])[:600]))
    print("對外請求：%d；結束碼 %d" % (status["requests"], code))
    print("=" * 62)
    return code


if __name__ == "__main__":
    sys.exit(main())
