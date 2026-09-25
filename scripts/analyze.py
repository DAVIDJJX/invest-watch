#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze.py — InvestWatch 分析系列：每天 15:30 review 那一輪算一次的「風險」與「拆解」（A1-1）

這支程式只在雲端 review（15:30）那一次完整更新裡跑（.github/workflows/update-data.yml 只在
slot=review 呼叫它），做三件事：

  1. 維護 data/history-long/<id>.json：Yahoo 週線全歷史（雲端負責、有 Yahoo 代號的標的）
     ＋ USD/TWD 週線（FinMind 轉載的台銀每日牌價，每週取最後一個營業日）。
     一週只補抓一次：存檔最後一根早於「最近一個已完成週」的週一（今天所在 ISO 週的週一減 7 天）才發請求；
     只有週一那次 review 會抓，週一沒跑到週二會自動補。
  2. 算 data/analysis/risk.json：年化波動（1 年／5 年）、最大回檔、目前距歷史高點的回檔、
     相關係數矩陣（3 年、週報酬、ISO 週對齊、成對可用）。
  3. 算 data/analysis/decompose.json：台銀台幣金價 ≈ 國際金價 × USD/TWD ÷ 31.1035 ＋ 殘差；
     00646 月報酬 ≈ S&P 500 月報酬 ＋ 匯率效果 ＋ 殘差。
  4. （A1-2）算 data/analysis/cost.json：00646 追蹤差（主口徑 ^SP500TR 總報酬指數、對照口徑 ^GSPC，各 1 年／3 年）、
     ETF 折溢價（證交所 all_etf.txt 每個交易日存一筆到 data/analysis/nav/<id>.json：預估／確定分開、確定晚一天回填）、
     黃金存摺價差、實體條塊相對存摺的溢價；靜態成本表 data/analysis/static-costs.json 是手寫的、這裡只引用。
     ^SP500TR 只是基準序列（EXTRA_LONG_SERIES），不進 assets.json、不做卡片。

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
    python scripts/analyze.py --adhoc FAKE.L --asset-class index_etf --out /tmp/adhoc
        # A1-3 試算：一支不在清單上的代號；結果只寫到 --out（必須在倉庫外），只在私人倉庫的 Actions 裡跑
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
INCREMENTAL_LOOKBACK_DAYS = 21          # 補抓時往前多要三週，讓合併有重疊、不會漏
WINDOWS_WEEKS = {"1y": 52, "5y": 260}
MIN_COVERAGE = 0.9                      # 視窗內缺超過 10% 就資料不足
CORR_WEEKS = 156                        # 3 年
CORR_MIN_OVERLAP = 100
TEN_YEARS_WEEKS = 520

# ---- A1-2：成本 ----
# 不在 assets.json 裡、只當基準用的長歷史序列（2026-09-25 A1-2 探測：^SP500TR 週線 1988-01 起可用）
EXTRA_LONG_SERIES = [
    {"id": "sp500tr", "kind": "yahoo", "symbol": "^SP500TR", "name": "S&P 500 總報酬指數（含股息再投資）",
     "currency": "USD", "unit": "點", "assetClass": "index",
     "note": "只是 00646 追蹤差的基準序列，不進 assets.json、不做卡片"},
]
# 證交所 all_etf.txt：正式抓法＝fetch_data 的標頭＋這個 Referer（2026-09-25 A1-2 探測：runner 上 200；沒有 Referer 會回 502「安全性考量」頁）
ALL_ETF_URL = "https://mis.twse.com.tw/stock/data/all_etf.txt"
ALL_ETF_HEADERS = {"Accept": "application/json, text/plain, */*", "Referer": "https://mis.twse.com.tw/stock/index.jsp"}
NAV_ASSETS = (("tw00646", "00646"), ("tw00679b", "00679B"))      # (assets.json 的 id, 證交所代號)
NAV_MIN_DAYS = 20                                                # 累積滿 20 個交易日才顯示中位數
NAV_MAX_ROWS = 400
# 櫃買 30 日（all_etf 被擋時 00679B 的退路；只回最近 30 個交易日、日期沒有年份）
TPEX_URL = "https://info.tpex.org.tw/api/etfProduct?lang=zh-tw&query=00679B"
TPEX_HEADERS = {"Accept": "application/json, text/plain, */*", "Referer": "https://info.tpex.org.tw/ETF/zh/detail.html?query=00679B",
                "X-Requested-With": "XMLHttpRequest"}
TD_WINDOWS = {"1y": 52, "3y": 156}                                # 追蹤差視窗（週）
TD_MAX_BACK_WEEKS = 3                                            # 視窗起點那一週缺資料時，往前最多找幾週
GOLD_SPOT_NOTE = ("黃金現貨口徑暫缺：Yahoo 的 XAUUSD=X 與 XAU=X 在 2026-09-25 的探測都回 404（查無），"
                  "所以只能用 GC=F 期貨。GC=F 通常是兩三個月後到期的合約，價格比現貨高出大約融資成本"
                  "（年化幾 %，換成兩三個月就是零點幾 %）：公式值本身就比現貨高，台銀本行賣出低於公式值不代表台銀賣得比現貨便宜。")

# 資料標籤（David 的裁決：星星只給「方法」，資料的等級用文字）
LABEL_ESTIMATE, LABEL_SINGLE, LABEL_CROSSCHECKED, LABEL_MULTI = "估算", "單一來源", "有對照", "多來源一致"
LABEL_NOTES = {
    LABEL_ESTIMATE: "由公式或不同時點的資料推算出來的值，不是任何來源直接給的數字",
    LABEL_SINGLE: "只有一個來源，沒有拿別的來源對過",
    LABEL_CROSSCHECKED: "有拿別的來源核對過（核對的方法與日期寫在旁邊）",
    LABEL_MULTI: "兩個以上獨立來源給出一致的值",
}
LABEL_USER_INPUT = "使用者輸入"                        # A1-3：費用率由使用者輸入，不是任何來源給的
ADHOC_CLASSES = ("stock", "index_etf", "bond_etf", "commodity", "crypto", "fx", "index")   # 裁決：七類，不含 gold_tw（台銀黃金沒有 Yahoo 代號）
ADHOC_PENCE = "GBp"                                    # Yahoo 對倫敦掛牌的報價單位：便士。完全等於這個字串才 ÷100；GBP（英鎊）不動
ADHOC_TAX_NOTE = "註冊地造成的稅務差異（股息預扣稅、遺產稅）本系統不計算，需另查最新規定"
ADHOC_MAX_SYMBOL_LEN = 24
WRITE_ROOTS = None                                     # adhoc 模式設成 [--out]：之後任何寫檔不在裡面就拒絕（第二層保險）；正式模式 None＝不限制
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


class WriteRefused(AnalyzeError):
    """adhoc 模式下寫到 --out 以外的地方：不是資料問題，是程式想寫進倉庫，一律擋。"""


def _norm_path(p):
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def path_inside(path, root):
    """path 是否落在 root 底下（含 root 本身）；不同磁碟機算不在。"""
    a, b = _norm_path(path), _norm_path(root)
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def assert_write_allowed(path):
    """第二層保險：adhoc 模式下，不管路徑怎麼組出來，不在 --out 底下就拒寫（正式模式 WRITE_ROOTS 是 None、不限制）。"""
    if WRITE_ROOTS is None:
        return
    if not any(path_inside(path, r) for r in WRITE_ROOTS):
        raise WriteRefused("拒絕寫入 %s：adhoc 模式只准寫到 --out 目錄" % sanitize(path))


def write_json(path, obj):
    assert_write_allowed(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def default_paths():
    """所有讀寫的位置都從這一個字典來（A1-3 的 --out <倉庫外> 只要換掉它）。呼叫時才讀模組常數，測試可以改常數。"""
    return {"long": LONG_DIR, "analysis": ANALYSIS_DIR, "assets": ASSETS_FILE, "latest": LATEST_FILE, "history": fd.HIST_DIR}


def load_history_daily(aid, paths=None):
    """data/history/<id>.json 的 points（日線）；沒有就空清單。"""
    paths = paths or default_paths()
    p = os.path.join(paths["history"], "%s.json" % aid)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("points") or []
    except Exception:
        return []


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
            "exchange": meta.get("exchangeName"), "name": meta.get("longName") or meta.get("shortName"),
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

def long_path(aid, paths=None):
    return os.path.join((paths or default_paths())["long"], "%s.json" % aid)


def load_long(aid, paths=None):
    p = long_path(aid, paths)
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


def save_long(aid, head, points, paths=None):
    paths = paths or default_paths()
    assert_write_allowed(long_path(aid, paths))
    os.makedirs(paths["long"], exist_ok=True)
    head = dict(head)
    head["count"] = len(points)
    head["firstDate"] = points[0]["d"] if points else None
    head["lastDate"] = points[-1]["d"] if points else None
    head["updatedAt"] = iso(now_tpe())
    with open(long_path(aid, paths), "w", encoding="utf-8", newline="\n") as fh:
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


def last_completed_week_monday(today):
    """最近一個已完成 ISO 週的週一＝今天所在 ISO 週的週一減 7 天（週一當天，上一週剛走完）。"""
    return today - timedelta(days=today.weekday() + 7)


def refetch_plan(existing_points, today):
    """要不要補抓、從哪裡開始。回傳 (mode, start_date_or_None, reason)。
    mode：full（整段）／incremental（從最後一根往前 21 天）／skip（存檔已經有最近一個完成週）。
    判斷是確定性的、不用年齡門檻：存檔最後一根的日期早於「最近一個已完成週的週一」才補抓——
    每週只在週一那次 review 抓一次，週一沒跑到，週二會自動補上。（A1-1 原本用「超過 7 天」，
    結果週二到下週一每天都補抓：最後一根永遠是上週一，年齡 8～13 天。）
    週棒日期不一定是週一（週一休市會是週二；匯率是該週最後一個營業日），所以比的是「早於週一」而不是「等於週一」。"""
    if not existing_points:
        return "full", None, "沒有長歷史，整段回補"
    last = parse_day(existing_points[-1]["d"])
    due = last_completed_week_monday(today)
    if last < due:
        start = last - timedelta(days=INCREMENTAL_LOOKBACK_DAYS)
        return "incremental", start.strftime("%Y-%m-%d"), "最後一根 %s 早於最近一個完成週的週一 %s，補抓 %s 起" % (last, due, start)
    return "skip", None, "最後一根 %s 已是最近一個完成週（週一 %s），這週不必抓" % (last, due)


def long_targets(assets):
    """要維護長歷史的標的：雲端負責且有 Yahoo 代號的，加上 USD/TWD（FinMind），再加 EXTRA_LONG_SERIES（純基準序列）。
    「讀清單」跟「算一檔」是分開的：update_long_history 吃的是這裡回的一個 target 字典，A1-3 的試算可以自己組一個。"""
    out = []
    for a in assets:
        if not a.get("enabled", True) or (a.get("owner") or "cloud") != "cloud":
            continue
        sym = yahoo_symbol(a)
        if sym:
            out.append({"id": a["id"], "kind": "yahoo", "symbol": sym, "asset": a})
        elif a["id"] == FX_LONG_ID and a.get("type") == "finmind_fx":
            out.append({"id": a["id"], "kind": "finmind", "symbol": a.get("symbol") or "USD", "asset": a})
    for e in EXTRA_LONG_SERIES:
        out.append({"id": e["id"], "kind": e["kind"], "symbol": e["symbol"], "asset": e})
    return out


def update_long_history(target, f, now, status, only=None, offline=False, dry_run=False, paths=None):
    """維護一個標的的 history-long。回傳這一項最後的 points（可能是舊的）。"""
    aid, a = target["id"], target["asset"]
    existing = load_long(aid, paths)
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
                "note": "只收已完成的週（起始＋7 天 ≤ 抓取當下）；存檔最後一根早於最近一個完成週的週一才補抓（每週一次）。"
                        "period1 用 1970-01-05（星期一）：Yahoo 把週的起點對齊 period1 的星期幾，用 0 會讓 ^GSPC 變成週四起的週；"
                        "所以只回 1970 以後（Yahoo 對 ^GSPC 其實從 1927 就有，分析用不到更早）。",
                "barsStartOn": info.get("dominantWeekday"),
                "firstTradeDateAtYahoo": info.get("firstTradeDate")}
        if a["id"] == "tw00679b":
            head["note"] += " 00679B 只有 2017-01 起，10 年視窗要到 2027-01 才夠。"
        save_long(aid, head, merged, paths)
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
    save_long(aid, head, merged, paths)
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
            "dataThrough": last["d"], "lastValue": last["c"]}


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
               "為什麼殘差常常是負的：" + GOLD_SPOT_NOTE,
               "拆開來看的話，「GC=F 口徑的殘差 − 現貨口徑的殘差」才是期貨基差、剩下的才是台銀價差；現貨代號查無，這一步暫時做不到。",
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


def build_decompose(series, now, problems, paths=None):
    paths = paths or default_paths()
    out = {"generatedAt": iso(now), "slot": "review", "labels": LABEL_NOTES,
           "notes": ["拆解全部是估算：公式與殘差的意義寫在各自的 notes；這裡只有事實與資料標籤，沒有任何判斷。"],
           "gold": None, "tw00646": None}
    try:
        gold_pts = load_history_daily("gold_twd", paths)
        gc_pts = load_history_daily("gold_intl", paths)
        fx_pts = load_history_daily("fx_usd", paths)
        if not gold_pts:
            raise AnalyzeError("沒有 gold_twd 的日線歷史（data/history/gold_twd.json）")
        if not gc_pts:
            raise AnalyzeError("沒有 gold_intl 的日線歷史（第一次完整更新之後才會有 data/history/gold_intl.json）")
        if not fx_pts:
            raise AnalyzeError("沒有 fx_usd 的日線歷史")
        latest = None
        if os.path.exists(paths["latest"]):
            with open(paths["latest"], encoding="utf-8") as fh:
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
# 成本（A1-2）：追蹤差、折溢價、黃金價差、條塊溢價
# ==========================================================================

def week_map(points, value=None):
    """{該週星期一(date): (d, value)}；value 預設取 c（匯率給 fx_mid 才會用中價）。"""
    out = {}
    for p in points:
        if p.get("c") is None:
            continue
        v = value(p) if value else p["c"]
        if v is None:
            continue
        out[monday_of(p["d"])] = (p["d"], v)
    return out


def tracking_difference(p646, pbench, pfx, weeks):
    """00646 報酬 − 基準換算台幣的報酬，視窗 weeks 週（ISO 週對齊）。
    基準換台幣：(1＋基準美元報酬)(1＋USD/TWD 中價變動) − 1。年化用幾何：((1＋r646)/(1＋r基準台幣))^(52/實際週數) − 1。
    起點那一週三個序列要齊；缺就往前最多找 3 週，還是不齊就資料不足。"""
    m1, m2, m3 = week_map(p646), week_map(pbench), week_map(pfx, value=fx_mid)
    common = set(m1) & set(m2) & set(m3)
    if not common:
        return {"weeks": weeks, "reason": "資料不足：三個序列沒有共同的週"}
    end = max(common)
    target = end - timedelta(weeks=weeks)
    start = None
    for k in range(TD_MAX_BACK_WEEKS + 1):
        key = target - timedelta(weeks=k)
        if key in common:
            start = key
            break
    if start is None:
        return {"weeks": weeks, "reason": "資料不足：視窗起點 %s 前後 %d 週內三個序列不齊" % (target, TD_MAX_BACK_WEEKS)}
    r646 = m1[end][1] / m1[start][1] - 1.0
    rb = m2[end][1] / m2[start][1] - 1.0
    rfx = m3[end][1] / m3[start][1] - 1.0
    rbt = (1.0 + rb) * (1.0 + rfx) - 1.0
    actual = (end - start).days / 7.0
    ann = ((1.0 + r646) / (1.0 + rbt)) ** (52.0 / actual) - 1.0 if actual > 0 else None
    return {"weeks": int(round(actual)), "from": m1[start][0], "through": m1[end][0],
            "fromWeek": start.strftime("%Y-%m-%d"), "throughWeek": end.strftime("%Y-%m-%d"),
            "r646Pct": pct(r646, 3), "rBenchPct": pct(rb, 3), "rFxPct": pct(rfx, 3), "rBenchTwdPct": pct(rbt, 3),
            "diffPct": pct(r646 - rbt, 3), "annualizedDiffPct": pct(ann, 3)}


def build_tracking(series):
    p646, pfx = series.get("tw00646"), series.get(FX_LONG_ID)
    out = {"asset": "tw00646",
           "method": "追蹤差＝00646 報酬 − [(1＋基準美元報酬)(1＋USD/TWD 中價變動) − 1]；週線、ISO 週對齊；年化用幾何；每個視窗附起訖日期",
           "primary": None, "reference": None}
    for key, bench_id, label, notes in (
            ("primary", "sp500tr", "^SP500TR", [
                "基準是含股息再投資的總報酬指數：差額裡沒有股息效果，剩下的是費用、期貨替代、匯率時點差（00646 換匯的時點跟台銀那一週的中價不完全一樣）。",
                "資料標籤「估算」：兩邊的週棒日期不一定同一天（台股週一起、美股週一起但時區不同），視窗起訖照實列出。"]),
            ("reference", "gspc", "^GSPC", [
                "基準是價格指數（不含股息）而 00646 不配息：差額裡含股息效果（約每年 +1%）、費用、期貨替代、匯率時點差；留著當對照。"])):
        pb = series.get(bench_id)
        o = {"benchmark": label, "benchmarkId": bench_id, "label": LABEL_ESTIMATE, "available": False, "windows": {}, "notes": notes}
        missing = [n for n, v in (("tw00646", p646), (bench_id, pb), (FX_LONG_ID, pfx)) if not v]
        if missing:
            o["reason"] = "缺長歷史：%s" % "、".join(missing)
        else:
            o["available"] = True
            for name, w in TD_WINDOWS.items():
                o["windows"][name] = tracking_difference(p646, pb, pfx, w)
        out[key] = o
    return out


def parse_all_etf(j, codes):
    """證交所 all_etf.txt（副檔名 .txt、內容 JSON）→ {代號: {...}}。h 是【前一營業日】的官方淨值，未結出時是文字。"""
    out = {}
    for grp in ((j or {}).get("a1") or []):
        for e in (grp.get("msgArray") or []):
            code = str(e.get("a"))
            if code not in codes:
                continue
            i = str(e.get("i") or "")
            h = fd.to_float(e.get("h"))
            out[code] = {"d": "%s-%s-%s" % (i[:4], i[4:6], i[6:]) if len(i) == 8 else None, "time": e.get("j"),
                         "price": fd.to_float(e.get("e")), "estNav": fd.to_float(e.get("f")),
                         "prevOfficialNav": h if (h and h > 0) else None,
                         "units": fd.to_float(e.get("c")), "unitsChange": fd.to_float(e.get("d"))}
    return out


def nav_path(aid, paths=None):
    return os.path.join((paths or default_paths())["analysis"], "nav", "%s.json" % aid)


def load_nav(aid, paths=None):
    p = nav_path(aid, paths)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("rows") or []
    except Exception:
        return []


def save_nav(aid, symbol, rows, paths=None):
    paths = paths or default_paths()
    p = nav_path(aid, paths)
    assert_write_allowed(p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    head = {"id": aid, "symbol": symbol, "source": "證交所 all_etf.txt（15:30 那一輪抓一次）",
            "note": "estNav／estPremiumPct 是投信的盤中預估（標「預估」）；officialNav 是前一營業日的官方淨值、隔天才拿得到、回填到那一天（標「確定」）；"
                    "折溢價＝(價格 − 淨值) ÷ 淨值。最多留 %d 列。" % NAV_MAX_ROWS,
            "count": len(rows), "firstDate": rows[0]["d"] if rows else None, "lastDate": rows[-1]["d"] if rows else None,
            "updatedAt": iso(now_tpe())}
    lines = ["{"]
    for k, v in head.items():
        lines.append("  %s: %s," % (json.dumps(k, ensure_ascii=False), json.dumps(v, ensure_ascii=False)))
    lines.append('  "rows": [')
    for i, r in enumerate(rows):
        lines.append("    " + json.dumps(r, ensure_ascii=False, separators=(",", ":")) + ("," if i < len(rows) - 1 else ""))
    lines.append("  ]")
    lines.append("}")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def premium_pct(price, nav):
    """折溢價＝(價格 − 淨值) ÷ 淨值 × 100。分母一定是淨值。"""
    if not price or not nav:
        return None
    return round((price - nav) / nav * 100.0, 3)


def update_nav_rows(rows, rec, closes):
    """把今天 all_etf 的一筆併進累積列：今天這列寫預估（標「預估」）；rec 帶來的前一營業日官方淨值回填到上一個交易日那列（標「確定」）。
    上一個交易日＝data/history 裡最後一個早於今天、已定案的收盤日；那天的收盤價拿來算確定口徑的折溢價。"""
    by_d = dict((r["d"], dict(r)) for r in rows if r.get("d"))
    d = rec.get("d")
    if d and rec.get("price") and rec.get("estNav"):
        row = by_d.setdefault(d, {"d": d})
        row.update({"time": rec.get("time"), "price": rec["price"], "estNav": rec["estNav"],
                    "estPremiumPct": premium_pct(rec["price"], rec["estNav"]), "estTag": "預估", "estLabel": LABEL_ESTIMATE,
                    "units": rec.get("units"), "unitsChange": rec.get("unitsChange")})
    h = rec.get("prevOfficialNav")
    if h and d:
        prev = [p for p in closes if p.get("d") and p["d"] < d and p.get("c") and not p.get("provisional")]
        if prev:
            pp = max(prev, key=lambda p: p["d"])
            row = by_d.setdefault(pp["d"], {"d": pp["d"]})
            row.update({"officialNav": h, "officialClose": pp["c"], "officialPremiumPct": premium_pct(pp["c"], h),
                        "officialTag": "確定", "officialLabel": LABEL_SINGLE, "officialNavSeenOn": d})
    return [by_d[k] for k in sorted(by_d)][-NAV_MAX_ROWS:]


def premium_summary(rows, key):
    vals = [(r["d"], r[key]) for r in rows if isinstance(r.get(key), (int, float))]
    n = len(vals)
    if n < NAV_MIN_DAYS:
        return {"n": n, "reason": "累積不到 %d 個交易日（目前 %d），先不顯示中位數" % (NAV_MIN_DAYS, n)}
    xs = [v for _d, v in vals]
    return {"n": n, "from": vals[0][0], "through": vals[-1][0], "medianPct": round(median(xs), 3),
            "meanPct": round(mean(xs), 3), "minPct": round(min(xs), 3), "maxPct": round(max(xs), 3)}


def latest_premium_rows(rows):
    out = []
    est = [r for r in rows if isinstance(r.get("estPremiumPct"), (int, float))]
    off = [r for r in rows if isinstance(r.get("officialPremiumPct"), (int, float))]
    if est:
        r = est[-1]
        out.append({"d": r["d"], "price": r["price"], "nav": r["estNav"], "premiumPct": r["estPremiumPct"], "tag": "預估"})
    if off:
        r = off[-1]
        out.append({"d": r["d"], "price": r["officialClose"], "nav": r["officialNav"], "premiumPct": r["officialPremiumPct"], "tag": "確定"})
    return out


def policed_post(f, url, headers, data=None):
    """PolicedFetcher 只有 get；櫃買的端點要 POST。一樣先查白名單、一樣守同站間隔。"""
    net_policy.assert_host_allowed(url)
    host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
    f._wait(host, 3.0)
    f.count += 1
    hdrs = dict(fd.BROWSER_HEADERS)
    hdrs.update(headers or {})
    r = f.session.post(url, headers=hdrs, data=data, timeout=30)
    if r.status_code != 200:
        raise fd.FetchError("HTTP %d" % r.status_code)
    try:
        return r.json()
    except Exception:
        raise fd.FetchError("回應不是合法 JSON")


def tpex_dates_with_year(md_list, today):
    """櫃買的日期只有 MM/DD：晚於今天的月日就是去年。"""
    out = []
    for md in md_list:
        try:
            m, d = [int(x) for x in str(md).split("/")]
        except ValueError:
            out.append(None)
            continue
        y = today.year
        if (m, d) > (today.month, today.day):
            y -= 1
        out.append("%04d-%02d-%02d" % (y, m, d))
    return out


def fetch_tpex_30d(f, today):
    """all_etf 被擋時 00679B 的退路：櫃買最近 30 個交易日的官方折溢價（atmps，%）。"""
    j = policed_post(f, TPEX_URL, TPEX_HEADERS)
    at = j.get("atmps") or []
    dates = tpex_dates_with_year([x.get("date") for x in at], today)
    vals = [(d, fd.to_float(x.get("count"))) for d, x in zip(dates, at) if d and fd.to_float(x.get("count")) is not None]
    if not vals:
        raise AnalyzeError("櫃買回應裡沒有折溢價")
    xs = [v for _d, v in vals]
    return {"title": "櫃買 30 日（僅 30 日）", "n": len(vals), "from": vals[0][0], "through": vals[-1][0],
            "medianPct": round(median(xs), 3), "minPct": round(min(xs), 3), "maxPct": round(max(xs), 3),
            "label": LABEL_SINGLE, "note": "只有最近 30 個交易日、日期沒有年份（年份是推的）；長期百分位得靠每天累積"}


def build_premium(f, offline, paths, problems, now):
    out = {}
    codes = dict((sym, aid) for aid, sym in NAV_ASSETS)
    fetched, err = None, None
    if not offline and f is not None:
        try:
            j = f.get(ALL_ETF_URL, delay=3.0, expect_json=True, headers=ALL_ETF_HEADERS)
            fetched = parse_all_etf(j, set(codes))
        except (fd.FetchError, net_policy.HostNotAllowed) as e:
            err = sanitize(e)
            problems.append("折溢價：all_etf.txt 抓不到（%s）" % err)
    for aid, sym in NAV_ASSETS:
        rows = load_nav(aid, paths)
        entry = {"file": "data/analysis/nav/%s.json" % aid, "symbol": sym,
                 "notes": ["預估口徑＝(all_etf.txt 那一筆的成交價 − 投信盤中預估淨值) ÷ 預估淨值，檔內的日期與時間照抄，標「預估」；確定口徑＝(前一營業日收盤 − 官方淨值) ÷ 官方淨值，官方淨值隔天才拿得到、回填到那一天，標「確定」。",
                           "累積滿 %d 個交易日才顯示中位數。" % NAV_MIN_DAYS]}
        if offline:
            entry["status"] = "offline：沒有連網，只列既有累積"
        elif fetched is None:
            blocked = bool(err and ("HTTP" in err or "安全性" in err))
            entry["status"] = "未接（來源在雲端被擋）" if blocked else "未接（抓取失敗）"
            entry["reason"] = err
            if aid == "tw00679b" and f is not None:
                try:
                    entry["short"] = fetch_tpex_30d(f, now.date())
                    entry["status"] = "僅 30 日"
                except (fd.FetchError, net_policy.HostNotAllowed, AnalyzeError) as e:
                    entry["short"] = {"title": "櫃買 30 日", "reason": sanitize(e)}
                    problems.append("折溢價 00679B：櫃買退路也抓不到（%s）" % sanitize(e))
        elif sym not in fetched:
            entry["status"] = "未接（all_etf.txt 裡沒有 %s）" % sym
            problems.append("折溢價 %s：all_etf.txt 裡沒有這一檔" % aid)
        else:
            rec = fetched[sym]
            rows = update_nav_rows(rows, rec, load_history_daily(aid, paths))
            save_nav(aid, sym, rows, paths)
            entry["status"] = "接了（每個交易日存一筆）"
            entry["today"] = {"d": rec["d"], "time": rec["time"], "price": rec["price"], "estNav": rec["estNav"], "prevOfficialNav": rec["prevOfficialNav"]}
        entry["rows"] = len(rows)
        entry["latest"] = latest_premium_rows(rows)
        entry["estimated"] = dict(premium_summary(rows, "estPremiumPct"), label=LABEL_ESTIMATE)
        entry["official"] = dict(premium_summary(rows, "officialPremiumPct"), label=LABEL_SINGLE)
        entry["label"] = "預估口徑「估算」；確定口徑「單一來源」"
        out[aid] = entry
    return out


def gold_spread(gold_pts):
    """黃金存摺價差＝(本行賣出 − 本行買入) ÷ 中價，每日一筆。"""
    rows = []
    for p in gold_pts:
        b, s_ = p.get("buy"), p.get("sell")
        if p.get("d") and b and s_:
            mid = (b + s_) / 2.0
            rows.append({"d": p["d"], "buy": b, "sell": s_, "spreadPct": round((s_ - b) / mid * 100.0, 3)})
    rows = rows[-NAV_MAX_ROWS:]
    if not rows:
        return {"reason": "沒有黃金存摺的本行買入／本行賣出資料"}
    xs = [r["spreadPct"] for r in rows]
    return {"label": LABEL_SINGLE, "latest": rows[-1], "daily": rows,
            "summary": {"n": len(rows), "from": rows[0]["d"], "through": rows[-1]["d"], "medianPct": round(median(xs), 3),
                        "meanPct": round(mean(xs), 3), "minPct": round(min(xs), 3), "maxPct": round(max(xs), 3)},
            "notes": ["台銀黃金存摺沒有手續費，成本就是這個價差；資料來自家用電腦抓的台銀牌價（data/history/gold_twd.json）。"]}


def bar_premium(bar_pts, gold_pts):
    """實體條塊相對存摺的溢價＝(整條價 ÷ 公克) ÷ 存摺本行賣出 − 1，各規格每日一筆。跟卡片上的 premiumPct 同一個定義。"""
    sell_by_d = dict((p["d"], p["sell"]) for p in gold_pts if p.get("d") and p.get("sell"))
    daily, latest = [], None
    for p in bar_pts:
        d = p.get("d")
        gold = sell_by_d.get(d)
        if not d or not gold:
            continue
        row, specs = {"d": d, "goldSell": gold}, []
        for label, key, grams in fd.BAR_SPECS:
            price = p.get(key)
            if price and grams:
                per_gram = price / grams
                prem = round((per_gram / gold - 1.0) * 100.0, 3)
                row[key + "Pct"] = prem
                specs.append({"spec": label, "grams": grams, "price": price, "perGram": round(per_gram, 2), "premiumPct": prem})
        daily.append(row)
        latest = {"d": d, "goldSell": gold, "rows": specs}
    daily = daily[-NAV_MAX_ROWS:]
    if not daily:
        return {"reason": "條塊與存摺沒有同一天的資料"}
    return {"label": LABEL_SINGLE, "n": len(daily), "from": daily[0]["d"], "through": daily[-1]["d"], "latest": latest, "daily": daily,
            "notes": ["台銀不公布條塊的歷史牌價，本站自 2026-08-31 起自己每天記；天數少的時候照實標。",
                      "1 台兩＝37.5 公克（跟 fetch_data 的 BAR_SPECS 同一份）。"]}


def build_cost(series, now, problems, paths=None, f=None, offline=False):
    paths = paths or default_paths()
    cost = {"generatedAt": iso(now), "slot": "review", "labels": LABEL_NOTES,
            "notes": ["成本全部是事實或估算，資料標籤寫在各段；這裡只有事實與資料標籤，沒有任何判斷。"],
            "trackingDifference": None, "premium": {}, "goldSpread": None, "barPremium": None, "staticCosts": None}
    try:
        cost["trackingDifference"] = build_tracking(series)
    except Exception as e:                                # noqa: B902
        problems.append("成本（追蹤差）：%s：%s" % (e.__class__.__name__, sanitize(e)))
        cost["trackingDifference"] = {"reason": sanitize(e)}
    cost["premium"] = build_premium(f, offline, paths, problems, now)
    gold_pts = load_history_daily("gold_twd", paths)
    try:
        if not gold_pts:
            raise AnalyzeError("沒有 gold_twd 的日線歷史")
        cost["goldSpread"] = gold_spread(gold_pts)
    except AnalyzeError as e:
        problems.append("成本（黃金價差）：%s" % e)
        cost["goldSpread"] = {"reason": str(e)}
    try:
        bars = load_history_daily("gold_bar", paths)
        if not bars:
            raise AnalyzeError("沒有 gold_bar 的日線歷史")
        cost["barPremium"] = bar_premium(bars, gold_pts)
    except AnalyzeError as e:
        problems.append("成本（條塊溢價）：%s" % e)
        cost["barPremium"] = {"reason": str(e)}
    sc = os.path.join(paths["analysis"], "static-costs.json")
    info = {"file": "data/analysis/static-costs.json", "present": os.path.exists(sc)}
    if info["present"]:
        try:
            with open(sc, encoding="utf-8") as fh:
                entries = (json.load(fh) or {}).get("entries") or []
            info["entries"] = len(entries)
            info["checkedOn"] = max([e.get("checkedOn") or "" for e in entries] or [""]) or None
        except Exception as e:                            # noqa: B902
            info["reason"] = "讀不到：%s" % sanitize(e)
    cost["staticCosts"] = info
    return cost


# ==========================================================================
# 主流程
# ==========================================================================

# ==========================================================================
# A1-3 試算：一支不在清單上的代號。計算程式公開；代號與結果只寫到 --out（倉庫外），只在私人倉庫的 Actions 裡跑
# ==========================================================================

def adhoc_out_check(out):
    """第一層：--out 必須在倉庫目錄之外，連網之前就擋。回傳絕對路徑。"""
    if not out:
        raise AnalyzeError("adhoc 需要 --out <倉庫外的目錄>")
    out_abs = os.path.abspath(out)
    if path_inside(out_abs, ROOT):
        raise AnalyzeError("--out 不可以在倉庫目錄裡面：試算的代號與結果不進公開倉庫")
    return out_abs


def adhoc_paths(out_abs, base=None):
    """adhoc 的讀寫位置：長歷史與 assets 從公開倉庫讀（唯讀），所有輸出寫到 out。"""
    base = base or default_paths()
    return {"long": base["long"], "assets": base["assets"], "latest": base["latest"], "history": base["history"],
            "analysis": out_abs, "out": out_abs}


def pence_to_pounds(points, info):
    """Yahoo 對倫敦掛牌的報價常是便士：meta.currency 完全等於 GBp 才 ÷100 換成英鎊並記錄；GBP（英鎊）與其他幣別一律不動。"""
    cur = info.get("currency")
    if cur == ADHOC_PENCE:
        pts = [dict(p, c=round(p["c"] / 100.0, 6)) for p in points]
        return pts, "GBP", "Yahoo 的報價單位是便士（GBp），已 ÷100 換成英鎊（GBP）"
    return points, cur, None


def adhoc_correlations(sym_points, public_series, by_id):
    """試算標的對每一個公開標的的 3 年週報酬相關（每一對各自算，跟 risk.json 同一套規則）；依 r 由高到低。"""
    rows, insufficient = [], []
    for aid in sorted(public_series):
        corr, _problems = correlation_matrix({"adhoc": sym_points, aid: public_series[aid]})
        cell = corr["matrix"]["adhoc"][aid]
        a = by_id.get(aid, {})
        row = {"id": aid, "name": a.get("name"), "assetClass": a.get("assetClass"), "currency": a.get("currency")}
        row.update(cell)
        (rows if isinstance(cell.get("r"), (int, float)) else insufficient).append(row)
    rows.sort(key=lambda r: r["r"], reverse=True)
    return rows, insufficient


def build_adhoc(symbol, asset_class, expense_ratio, pts, info, currency, unit_note, now, paths):
    assets = load_assets(paths)
    by_id = {a["id"]: a for a in assets}
    by_id.update({e["id"]: e for e in EXTRA_LONG_SERIES})
    public, same = {}, []
    for t in long_targets(assets):
        if (t.get("symbol") or "").upper() == symbol.upper():
            same.append(t["id"])
            continue
        old = load_long(t["id"], paths)
        if old and old.get("points"):
            public[t["id"]] = old["points"]
    rets = weekly_returns(pts)
    first, last = pts[0]["d"], pts[-1]["d"]
    young = parse_day(first) > now.date() - timedelta(days=3 * 365)
    if young:
        corr = {"window": "3 年（156 週）", "label": LABEL_SINGLE, "top": [], "all": [],
                "reason": "資料不足：上市未滿 3 年（第一根週棒 %s）" % first}
    else:
        rows, insufficient = adhoc_correlations(pts, public, by_id)
        corr = {"window": "3 年（156 週）", "label": LABEL_SINGLE, "top": rows[:3], "all": rows, "insufficient": insufficient,
                "note": "皮爾森相關，週報酬、ISO 週對齊、成對可用（每一對各自取兩邊都有的最近 156 週），重疊不到 %d 週寫資料不足；各自幣別、不含匯率換算" % CORR_MIN_OVERLAP}
    if same:
        corr["skippedSameSymbol"] = same
    label = LABEL_SINGLE
    return {
        "symbol": symbol, "assetClass": asset_class, "name": info.get("name"), "exchange": info.get("exchange"),
        "currency": currency, "priceUnitNote": unit_note,
        "runDate": now.astimezone(TPE).strftime("%Y-%m-%d"), "generatedAt": iso(now), "dataThrough": last,
        "firstBar": first, "bars": len(pts), "dataLabel": label, "source": "Yahoo Finance chart API（週線，跟正式清單同一套抓法與檢查）",
        "dateSourceNote": DATE_SOURCE_NOTES["yahoo-week"],
        "expenseRatio": ({"pct": expense_ratio, "label": LABEL_USER_INPUT} if expense_ratio is not None
                         else {"pct": None, "label": LABEL_USER_INPUT, "reason": "沒有輸入"}),
        "risk": {"volatility": {k: dict(annualized_vol(rets, w), label=label) for k, w in WINDOWS_WEEKS.items()},
                 "maxDrawdown": dict(max_drawdown(pts) or {}, label=label),
                 "currentDrawdown": dict(current_drawdown(pts) or {}, label=label),
                 "tenYearWindow": ten_year_window(pts)},
        "correlation": corr,
        "valuation": {"reason": "資料不足：試算不做估值"},
        "premium": {"reason": "資料不足：試算不做折溢價"},
        "trend": {"reason": "A2 完成後才有趨勢面"},
        "notes": [ADHOC_TAX_NOTE, "相關係數是各自幣別的週報酬，不含匯率換算。", "這裡只有事實與資料標籤，沒有任何判斷。"],
        "footer": "以上為量化整理，未經回測驗證，不構成投資建議。",
    }


def update_adhoc_index(out_abs, symbol, rel, result):
    """每跑完一次就更新 index.json：每個代號的最新檔、資料截止日與幾個摘要數字；網站先讀它（1 個請求）。"""
    p = os.path.join(out_abs, "index.json")
    idx = {"version": 1, "note": "每個代號的最新一筆；網站先讀這一檔再點進各檔。同一天重跑覆蓋同一檔、舊檔全部保留。", "symbols": {}}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                old = json.load(fh) or {}
            if isinstance(old.get("symbols"), dict):
                idx["symbols"] = old["symbols"]
        except Exception:                                 # noqa: B902
            pass
    top = (result.get("correlation") or {}).get("top") or []
    vol = result["risk"]["volatility"]
    idx["symbols"][symbol] = {"latest": rel, "runDate": result["runDate"], "dataThrough": result["dataThrough"],
                              "assetClass": result["assetClass"], "currency": result["currency"], "bars": result["bars"],
                              "vol1yPct": vol["1y"].get("pct"), "vol5yPct": vol["5y"].get("pct"),
                              "maxDrawdownPct": (result["risk"]["maxDrawdown"] or {}).get("pct"),
                              "currentDrawdownPct": (result["risk"]["currentDrawdown"] or {}).get("pct"),
                              "top1": {"id": top[0]["id"], "r": top[0]["r"]} if top else None,
                              "priceUnitNote": result.get("priceUnitNote")}
    idx["updatedAt"] = iso(now_tpe())
    write_json(p, idx)


def adhoc_run(symbol, asset_class, expense_ratio, out, now=None, fetcher=None, paths=None):
    """試算入口。回傳 (status, 結束碼)：0 成功、2 使用者輸入或來源的問題（查無代號、--out 在倉庫裡…）、1 程式壞掉。
    代號查無就明確失敗、不產生結果檔；status.json 也只寫到 out。"""
    global WRITE_ROOTS
    now = now or now_tpe()
    symbol = (symbol or "").strip()
    status = {"generatedAt": iso(now), "mode": "adhoc", "ok": False, "errors": [], "requests": 0, "produced": []}
    code = 0
    out_abs = None
    try:
        if (not symbol or len(symbol) > ADHOC_MAX_SYMBOL_LEN or not re.match(r"^[A-Za-z0-9.^=\-]+$", symbol)
                or not re.search(r"[A-Za-z0-9]", symbol)):
            raise AnalyzeError("代號格式不對：只接受英數字與 . ^ = -，最長 %d 字" % ADHOC_MAX_SYMBOL_LEN)
        if asset_class not in ADHOC_CLASSES:
            raise AnalyzeError("類別必須是 %s 之一" % "／".join(ADHOC_CLASSES))
        er = None
        if expense_ratio not in (None, ""):
            try:
                er = float(expense_ratio)
            except (TypeError, ValueError):
                raise AnalyzeError("費用率要是數字（百分比／年）")
            if not (0.0 <= er <= 20.0):
                raise AnalyzeError("費用率超出合理範圍（0～20，百分比／年）")
        out_abs = adhoc_out_check(out)                    # 第一層：連網之前
        paths = adhoc_paths(out_abs, paths)
        WRITE_ROOTS = [out_abs]                           # 第二層：從這裡起任何寫檔都要在 out 底下
        f = fetcher or PolicedFetcher(verbose=True)
        try:
            pts, info = fetch_yahoo_weekly(f, symbol, YAHOO_PERIOD1_MONDAY, int(now.timestamp()), now_epoch=now.timestamp())
        except fd.FetchError as e:
            if "404" in str(e):
                raise AnalyzeError("查無此代號：%s（Yahoo 回 404）" % symbol)
            raise
        finally:
            status["requests"] = getattr(f, "count", 0)
        pts, currency, unit_note = pence_to_pounds(pts, info)
        result = build_adhoc(symbol, asset_class, er, pts, info, currency, unit_note, now, paths)
        rel = "%s/%s.json" % (symbol, result["runDate"])
        write_json(os.path.join(out_abs, symbol, result["runDate"] + ".json"), result)
        status["produced"].append(rel)
        update_adhoc_index(out_abs, symbol, rel, result)
        status["produced"].append("index.json")
        status["dataThrough"] = result["dataThrough"]
    except (AnalyzeError, fd.FetchError, net_policy.HostNotAllowed) as e:
        status["errors"].append(sanitize(e))
        code = 2
    except Exception as e:                                # noqa: B902
        status["errors"].append("程式壞掉：%s：%s" % (e.__class__.__name__, sanitize(e)))
        traceback.print_exc(limit=3)
        code = 1
    status["ok"] = not status["errors"]
    status["finishedAt"] = iso(now_tpe())
    if out_abs is not None:
        write_json(os.path.join(out_abs, "status.json"), status)     # 裁決：adhoc 的 status.json 也寫到 out，不碰 data/analysis
    WRITE_ROOTS = None
    return status, code


def load_assets(paths=None):
    with open((paths or default_paths())["assets"], encoding="utf-8") as fh:
        return json.load(fh)["assets"]


def run(slot, offline=False, dry_run=False, only=None, now=None, paths=None):
    now = now or now_tpe()
    paths = paths or default_paths()
    status = {"generatedAt": iso(now), "lastRun": iso(now), "slot": slot, "ok": False, "mode": "dry-run" if dry_run else "offline" if offline else "live",
              "errors": [], "warnings": [], "produced": [], "requests": 0, "longHistory": {},
              "note": "分析只在 review（15:30）那一輪跑；出錯不影響行情與報告。錯誤訊息不含機器名與路徑。"}
    code = 0
    try:
        assets = load_assets(paths)
        by_id = {a["id"]: a for a in assets}
        by_id.update({e["id"]: e for e in EXTRA_LONG_SERIES})
        f = None if (offline or dry_run) else PolicedFetcher(verbose=True)
        series = {}
        for t in long_targets(assets):
            try:
                pts = update_long_history(t, f, now, status, only=only, offline=offline, dry_run=dry_run, paths=paths)
                if pts:
                    series[t["id"]] = pts
                print("[%s] %s" % (t["id"], status["longHistory"][t["id"]]["action"]))
            except (AnalyzeError, fd.FetchError, net_policy.HostNotAllowed) as e:
                status["errors"].append("history-long %s：%s" % (t["id"], sanitize(e)))
                status["longHistory"][t["id"]]["action"] = "失敗：%s" % sanitize(e)
                print("[%s] 失敗：%s" % (t["id"], sanitize(e)))
                old = load_long(t["id"], paths)
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
        write_json(os.path.join(paths["analysis"], "risk.json"), risk)
        status["produced"].append("data/analysis/risk.json")
        dec = build_decompose(series, now, problems, paths)
        write_json(os.path.join(paths["analysis"], "decompose.json"), dec)
        status["produced"].append("data/analysis/decompose.json")
        cost = build_cost(series, now, problems, paths, f=f, offline=offline)
        write_json(os.path.join(paths["analysis"], "cost.json"), cost)
        status["produced"].append("data/analysis/cost.json")
        if f is not None:
            status["requests"] = f.count
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
        write_json(os.path.join(paths["analysis"], "status.json"), status)
    if code == 0 and status["errors"]:
        code = 2
    return status, code


def build_parser():
    """參數名是私人倉庫 workflow 依賴的介面（docs/adhoc-workflow.example.yml）：測試釘住，改了要同時改範本並在 CHANGELOG 標「私人 workflow 需更新」。"""
    ap = argparse.ArgumentParser(description="InvestWatch 分析系列：風險、拆解、成本（只在 review 那一輪跑）；--adhoc 是 A1-3 的試算入口")
    ap.add_argument("--slot", default=None, help="這一輪的時段；正式只接受 review（--adhoc 不用給）")
    ap.add_argument("--offline", action="store_true", help="不連網，只用倉庫裡已有的 history-long 重算")
    ap.add_argument("--dry-run", action="store_true", help="只列出會發哪些請求，不連網、不寫檔")
    ap.add_argument("--only", default=None, help="只更新這些 id 的長歷史（逗號分隔），測試用")
    ap.add_argument("--now", default=None, help="測試用：把「現在」當成這個時間（ISO 8601）")
    ap.add_argument("--adhoc", default=None, metavar="SYMBOL", help="A1-3 試算：一支不在清單上的 Yahoo 代號（結果只寫到 --out，倉庫外）")
    ap.add_argument("--asset-class", default=None, choices=ADHOC_CLASSES, help="試算標的的類別")
    ap.add_argument("--expense-ratio", default=None, help="試算標的的費用率（百分比／年，選填；標「使用者輸入」）")
    ap.add_argument("--out", default=None, help="試算結果的目錄，必須在倉庫之外")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else now_tpe()
    if now.tzinfo is None:
        now = now.replace(tzinfo=TPE)
    if args.adhoc is not None:
        if not args.asset_class:
            print("試算需要 --asset-class（%s 之一）" % "／".join(ADHOC_CLASSES))
            return 2
        print("=" * 62)
        print("InvestWatch 試算（adhoc）  台北時間：%s" % now.strftime("%Y-%m-%d %H:%M:%S"))
        print("=" * 62)
        status, code = adhoc_run(args.adhoc, args.asset_class, args.expense_ratio, args.out, now=now)
        print("產出：%s" % ("、".join(status["produced"]) or "無"))
        if status["errors"]:
            print("::error::試算失敗：%s" % "；".join(status["errors"])[:600])
        print("對外請求：%d；結束碼 %d" % (status["requests"], code))
        return code
    if not args.slot:
        print("需要 --slot（正式只接受 review）；試算請用 --adhoc。")
        return 2
    if args.slot != "review" and not (args.offline or args.dry_run):
        print("分析只在 review（15:30）那一輪跑；收到的是 --slot %s，什麼都不做。" % args.slot)
        return 2
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
