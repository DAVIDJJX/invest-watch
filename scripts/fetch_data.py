#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
InvestWatch 資料抓取腳本
========================

用途：讀 data/assets.json 的監控清單，依 PLAN.md 第 7 章的實測規格逐一抓取行情，
      更新 data/history/<id>.json（各標的日線歷史，最多 400 點）
      與自己那一邊的分片 data/sources/<cloud|local>.json。

**這支程式不寫 data/latest.json，也不產生報告。**
      latest.json 由 scripts/merge_latest.py 從兩個分片算出來；
      報告由 scripts/report.py 產生，而且只有雲端會跑。
      這樣每個檔案都只有一個寫入者，兩邊才不會互相覆蓋
      （2026-09-03 20:00 本機抓到黃金、20:07 雲端抓不到就把它蓋成 error）。

設計原則（對應 PLAN.md 第 3 章）：
  * 誠實：任一來源失敗 → 保留舊歷史不動、在 latest.json 標 status="error" 與錯誤訊息，
          絕不拿舊資料假裝是新的；整支腳本不會因單一來源壞掉而中斷。
  * 禮貌：同一網站的請求間隔 >= 2 秒（證交所 >= 3 秒），帶瀏覽器 User-Agent，
          Yahoo 遇 429 等 10 秒重試一次。
  * 隱私：本檔只處理公開市場資料，不碰任何個人持倉資訊。

用法：
    python scripts/fetch_data.py --source cloud --slot close   # 雲端負責的 8 項
    python scripts/fetch_data.py --source local --slot close   # 本機負責的 5 項
    python scripts/fetch_data.py --source local --light        # 盤中輕量更新
    python scripts/fetch_data.py --only gold_twd --source local  # 只抓部分（測試用）

⚠ --source 是必填的。all 會兩邊都抓、寫出兩個分片，只給手動測試用；
  正式排程一定要指明 cloud 或 local，否則會寫到不屬於自己的分片。

只依賴 requests，不用 pandas。
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

try:  # Windows 主控台輸出中文
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 路徑與常數

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
HIST_DIR = os.path.join(DATA_DIR, "history")
ASSETS_FILE = os.path.join(DATA_DIR, "assets.json")
LATEST_FILE = os.path.join(DATA_DIR, "latest.json")

# 分片：每一邊只寫自己的那一個檔，兩邊永遠不會寫到同一個檔案。
# data/latest.json 改由 scripts/merge_latest.py 從這兩個分片算出來，
# 這支程式從此不再直接寫 latest.json。
SOURCES_DIR = os.path.join(DATA_DIR, "sources")

TPE = timezone(timedelta(hours=8))          # 台北時間
MAX_POINTS = 400                            # 每個標的最多保留幾個歷史點
SPARK_POINTS = 40                           # latest.json 內迷你走勢線的點數

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# 台銀對每個新連線的第一次請求會回一次驗證頁（不是資料），重載同一個網址就會拿到
# 真內容。這裡做的就是一般瀏覽器都會做的重新載入，沒有去解那道驗證題。
BOT_CHALLENGE_MARK = b"Challenge Validation"


class FetchError(Exception):
    """單一資料來源抓取／解析失敗。會被上層接住，只影響該標的。"""


class SkipAsset(Exception):
    """這一輪刻意不更新這個標的（例如盤中輕量更新跳過實體條塊）。
    上層會沿用上一次的結果，並標記原因——不是錯誤，也不會顯示成失敗。"""


# ---------------------------------------------------------------- 共用工具


def now_tpe():
    return datetime.now(TPE)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def to_float(s):
    """把 4,592,501 / 31.63500 / 空白 轉成 float；'-' 或空值回 None。"""
    if s is None:
        return None
    t = str(s).replace(",", "").replace("　", "").strip()
    if t in ("", "-", "--", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def roc_to_date(s):
    """民國日期 115/08/31 轉成 2026-08-31。"""
    m = re.match(r"\s*(\d{2,3})/(\d{1,2})/(\d{1,2})\s*$", str(s))
    if not m:
        return None
    y, mo, d = int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))
    return "%04d-%02d-%02d" % (y, mo, d)


def strip_tags(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def decode_bot_csv(raw):
    """台銀 CSV 編碼：先試 utf-8-sig，失敗改 cp950（PLAN 7.3）。"""
    for enc in ("utf-8-sig", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


class Fetcher:
    """帶「同站禮貌間隔」與重試的 HTTP 取用器。"""

    def __init__(self, verbose=True):
        self.session = requests.Session()
        self.last_hit = {}      # host -> 上次請求的時間戳
        self.blocked = {}       # host -> 已確認被擋的原因
        self.verbose = verbose
        self.count = 0

    def _wait(self, host, delay):
        prev = self.last_hit.get(host)
        if prev is not None:
            gap = time.time() - prev
            if gap < delay:
                time.sleep(delay - gap)
        self.last_hit[host] = time.time()

    def get(self, url, delay=2.0, headers=None, timeout=30,
            bot_retry=2, tries=2, expect_json=False):
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        # 這個網站這一輪已經確認擋我們了，就不要再一直敲門（禮貌，也省時間）
        if host in self.blocked:
            raise FetchError(self.blocked[host])

        hdrs = dict(BROWSER_HEADERS)
        if headers:
            hdrs.update(headers)

        last_err = None
        for attempt in range(1, tries + 1):
            self._wait(host, delay)
            try:
                self.count += 1
                r = self.session.get(url, headers=hdrs, timeout=timeout)
            except Exception as e:                       # 連線層失敗
                last_err = "連線失敗：%s" % e
                if self.verbose:
                    print("      ! %s（第 %d 次）" % (last_err, attempt))
                time.sleep(3)
                continue

            # 台銀機器人驗證頁：重載即可
            if BOT_CHALLENGE_MARK in r.content:
                got = False
                for i in range(bot_retry):
                    if self.verbose:
                        print("      . 台銀驗證頁，4 秒後重載（%d/%d）" % (i + 1, bot_retry))
                    time.sleep(4)
                    self._wait(host, delay)
                    self.count += 1
                    r = self.session.get(url, headers=hdrs, timeout=timeout)
                    if BOT_CHALLENGE_MARK not in r.content:
                        got = True
                        break
                if not got:
                    # 一直過不了＝這個來源封鎖了目前這個 IP（GitHub Actions 的
                    # 資料中心 IP 常被這樣對待）。記下來，本輪不再重試。
                    last_err = ("台銀擋下了這次連線（回傳機器人驗證頁）。"
                                "雲端主機的 IP 常被這樣阻擋，本輪不再重試。")
                    self.blocked[host] = last_err
                    break

            # Yahoo 限流
            if r.status_code == 429:
                last_err = "來源回應 429（請求過於頻繁）"
                if self.verbose:
                    print("      . 遇到 429，等 10 秒重試")
                time.sleep(10)
                continue

            if r.status_code != 200:
                last_err = "HTTP %d" % r.status_code
                time.sleep(2)
                continue

            if expect_json:
                try:
                    return r.json()
                except Exception:
                    last_err = "回應不是合法 JSON"
                    time.sleep(2)
                    continue
            return r

        raise FetchError(last_err or "未知錯誤")


# ---------------------------------------------------------------- 台銀黃金（PLAN 7.1 / 7.2）

GOLD_ROW_DATE = re.compile(r"href=[\"']?/gold/chart/(\d{4}-\d{2}-\d{2})/")
GOLD_ROW_NUM = re.compile(
    r"<td[^>]*class=\"[^\"]*text-right[^\"]*\"[^>]*>\s*([\d,]+(?:\.\d+)?)\s*</td>"
)


def parse_gold_chart(html):
    """解析台銀黃金存摺歷史牌價表，回傳 [{d, buy, sell, c}]（PLAN 7.1）。

    每列長相：
      <td class="text-center"><a href=/gold/chart/2026-08-31/TWD/>2026/08/31</a></td>
      ... <td class="text-right">4509</td><td class="text-right">4558</td>
    兩個 text-right 依序是「本行買入、本行賣出」。
    href 目前沒有引號，正規式兩種寫法都容忍。
    """
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        m = GOLD_ROW_DATE.search(row)
        if not m:
            continue
        nums = GOLD_ROW_NUM.findall(row)
        if len(nums) < 2:
            continue
        buy, sell = to_float(nums[0]), to_float(nums[1])
        if buy is None or sell is None:
            continue
        out.append({"d": m.group(1), "buy": buy, "sell": sell, "c": sell})
    # 台銀這張表是「新到舊」排列，統一轉成由舊到新，最後一筆才是最新牌價
    out.sort(key=lambda p: p["d"])
    return out


def fetch_bot_gold(f, currency, light=False):
    """台銀黃金存摺牌價（歷史走勢表）。

    完整模式先抓 year（約 244 列、198KB），失敗退 half；
    輕量模式反過來，先抓 half（約 126 列、141KB）就夠——盤中要的是今天的牌價，
    歷史早就在本機的 history 檔裡了。
    """
    last_err = None
    for rng in (("half", "year") if light else ("year", "half")):
        url = "https://rate.bot.com.tw/gold/chart/%s/%s" % (rng, currency)
        try:
            html = f.get(url, delay=2.5).content.decode("utf-8", "replace")
        except FetchError as e:
            last_err = str(e)
            continue
        pts = parse_gold_chart(html)
        if pts:
            return pts
        last_err = "頁面抓到了但解析不到任何牌價列（網站格式可能改版）"
    raise FetchError(last_err or "無法取得黃金牌價")


def fetch_gold_page(f):
    """台銀黃金主頁（85KB）：一次拿到「掛牌時間」與台幣黃金存摺的買賣價。

    這一頁很關鍵——盤中只要它就夠了，不必再去下載 200KB 的一年走勢表。
    回傳 (掛牌時間, 本行買進, 本行賣出)。
    """
    html = f.get("https://rate.bot.com.tw/gold?Lang=zh-TW", delay=2.5) \
            .content.decode("utf-8", "replace")

    m = re.search(r"掛牌時間：\s*([0-9]{4}/[0-9]{2}/[0-9]{2}\s+[0-9]{2}:[0-9]{2})", html)
    quote_time = m.group(1).strip() if m else None

    buy = sell = None
    tb = _table_by_summary(html, "此表格為黃金存摺")
    if tb is not None:
        s = _row_values(tb, "本行賣出")
        b = _row_values(tb, "本行買進")
        sell = s[0] if s else None
        buy = b[0] if b else None

    if quote_time is None and sell is None:
        raise FetchError("黃金主頁解析不到掛牌時間，也解析不到牌價")
    return quote_time, buy, sell


def _table_by_summary(html, keyword):
    """依 <table ... summary="..."> 內的關鍵字找出指定表格。"""
    for tb in re.findall(r"<table[^>]*>.*?</table>", html, re.S):
        head = tb[:tb.find(">") + 1]
        if keyword in head:
            return tb
    return None


# 一整格 text-right 儲存格的內容。不能只抓「數字後面緊接 </td>」——
# 黃金主頁的價格格子裡還包了一個「買進」表單按鈕（<form>…</form>）。
# 抓整格再從裡面取第一個數字，格子是「-」就回 None，這樣欄位順序不會錯位。
GOLD_CELL = re.compile(
    r"<td[^>]*class=\"[^\"]*text-right[^\"]*\"[^>]*>(.*?)</td>", re.S
)


def _row_values(table_html, label):
    """在表格內找含指定文字（例如 本行賣出）的那一列，依序回傳各 text-right 格子的值。"""
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S):
        if label in strip_tags(row):
            out = []
            for cell in GOLD_CELL.findall(row):
                m = re.search(r"([\d,]+(?:\.\d+)?)", cell)
                out.append(to_float(m.group(1)) if m else None)
            return out
    return []


def fetch_gold_bars(f):
    """台銀實體黃金條塊掛牌價（PLAN 7.2）。解析不到就報錯，不回空值假裝成功。

    回傳 (條塊清單, 這一頁的掛牌時間)。
    注意：這一頁的掛牌時間和黃金主頁的**不一樣**——實測 2026-08-31 這頁是 14:54、
    主頁是 19:45。存摺牌價一路更新到晚上，實體條塊則大致在營業時間內設定一次，
    所以條塊卡片要用這一頁的時間，不能共用主頁的。
    """
    html = f.get("https://rate.bot.com.tw/gold/quote/recent", delay=2.5) \
            .content.decode("utf-8", "replace")

    m = re.search(r"掛牌時間：\s*([0-9]{4}/[0-9]{2}/[0-9]{2}\s+[0-9]{2}:[0-9]{2})", html)
    bar_quote_time = m.group(1).strip() if m else None

    bars = []
    t1 = _table_by_summary(html, "此表格為黃金條塊表格")
    if t1 is None:
        raise FetchError("找不到黃金條塊表格（網站格式可能改版）")
    sells = _row_values(t1, "本行賣出")
    specs = ["1 公斤", "500 公克", "250 公克", "100 公克"]
    if len(sells) < 4 or any(v is None for v in sells[:4]):
        raise FetchError("黃金條塊表格解析不到四個規格的賣出價")
    for spec, v in zip(specs, sells[:4]):
        bars.append({"spec": spec, "sell": v, "buy": None})

    t2 = _table_by_summary(html, "此表格為臺銀金鑽條塊")
    if t2 is None:
        raise FetchError("找不到臺銀金鑽條塊表格（網站格式可能改版）")
    s2 = _row_values(t2, "本行賣出")
    b2 = _row_values(t2, "本行買進")
    if not s2 or s2[0] is None:
        raise FetchError("金鑽條塊表格解析不到賣出價")
    bars.append({
        "spec": "金鑽 1 台兩",
        "sell": s2[0],
        "buy": b2[0] if b2 else None,
    })
    return bars, bar_quote_time


# ---------------------------------------------------------------- 台銀匯率（PLAN 7.3）


def fetch_bot_fx_day(f):
    """當日全幣別匯率 CSV。回傳 {幣別: {cashBuy, spotBuy, cashSell, spotSell}}。"""
    raw = f.get("https://rate.bot.com.tw/xrt/flcsv/0/day", delay=2.5).content
    txt = decode_bot_csv(raw)
    out = {}
    for line in txt.strip().splitlines()[1:]:
        p = line.split(",")
        if len(p) < 14:
            continue
        code = p[0].strip()
        if not re.match(r"^[A-Z]{3}$", code):
            continue
        out[code] = {
            "cashBuy": to_float(p[2]),
            "spotBuy": to_float(p[3]),
            "cashSell": to_float(p[12]),
            "spotSell": to_float(p[13]),
        }
    if not out:
        raise FetchError("當日匯率 CSV 解析不到任何幣別")
    return out


def fetch_bot_fx_history(f, code):
    """近六個月即期匯率歷史 CSV（L6M 大寫）。回傳 [{d, spotBuy, spotSell, c}]。"""
    url = "https://rate.bot.com.tw/xrt/flcsv/0/L6M/%s" % code
    raw = f.get(url, delay=2.5).content
    txt = decode_bot_csv(raw)
    pts = []
    for line in txt.strip().splitlines()[1:]:
        p = line.split(",")
        if len(p) < 15:
            continue
        m = re.match(r"^(\d{4})(\d{2})(\d{2})$", p[0].strip())
        if not m:
            continue
        buy, sell = to_float(p[4]), to_float(p[14])
        if sell is None:
            continue
        pts.append({
            "d": "%s-%s-%s" % m.groups(),
            "spotBuy": buy,
            "spotSell": sell,
            "c": sell,          # 主價＝即期賣出（你要換外幣時付的價）
        })
    if not pts:
        raise FetchError("%s 歷史匯率 CSV 解析不到資料" % code)
    return pts


# ---------------------------------------------------------------- 台股（PLAN 7.4 / 7.5）


def fetch_twse_realtime(f, items):
    """證交所 mis 即時報價，多檔一次抓。回傳 {代號: {...}}。

    items 是 [(代號, 市場別)]，市場別 tse=上市、otc=上櫃。
    這個前綴一定要對：00679B 是上櫃，用 tse_ 抓會回一筆空資料（不會報錯，
    但什麼都沒有），2026-09-02 實測確認要用 otc_00679B.tw 才抓得到。
    """
    ex_ch = "|".join("%s_%s.tw" % (mkt or "tse", c) for c, mkt in items)
    url = ("https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
           "?ex_ch=%s&json=1&delay=0" % ex_ch)
    j = f.get(url, delay=3.0, expect_json=True,
              headers={"Accept": "application/json, text/plain, */*",
                       "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
    if str(j.get("rtcode")) != "0000":
        raise FetchError("mis 回應 rtcode=%s" % j.get("rtcode"))
    out = {}
    for it in j.get("msgArray", []):
        code = it.get("c")
        price = to_float(it.get("z"))
        note = None
        if price is None:      # 盤中該秒無成交，z 會是 "-"（PLAN 7.4）
            hi, lo = to_float(it.get("h")), to_float(it.get("l"))
            if hi is not None and lo is not None:
                price = round((hi + lo) / 2, 4)
                note = "該時點無成交，暫以當日最高最低均價顯示"
        d = it.get("d") or ""
        date = "%s-%s-%s" % (d[0:4], d[4:6], d[6:8]) if len(d) == 8 else None
        out[code] = {
            "price": price,
            "prevClose": to_float(it.get("y")),
            "open": to_float(it.get("o")),
            "high": to_float(it.get("h")),
            "low": to_float(it.get("l")),
            "date": date,
            "time": it.get("t"),
            "name": it.get("n"),
            "note": note,
        }
    if not out:
        raise FetchError("mis 沒有回傳任何標的")
    return out


def fetch_twse_month(f, asset, yyyymm):
    """證交所官方月檔（收盤定案用）。回傳 [{d, c}]。"""
    if asset["type"] == "twse_index":
        url = ("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
               "?date=%s01&response=json" % yyyymm)
        col = 4          # 發行量加權股價指數
    else:
        url = ("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
               "?date=%s01&stockNo=%s&response=json" % (yyyymm, asset["symbol"]))
        col = 6          # 收盤價
    j = f.get(url, delay=3.5, expect_json=True,
              headers={"Accept": "application/json, text/plain, */*"})
    if j.get("stat") != "OK":
        stat = str(j.get("stat") or "")
        # 每個月 1 號、或當月還沒有交易日時，官方月檔本來就還沒發布，
        # 這不是故障，不要在報告裡當成警告刷出來。
        if "沒有符合條件" in stat:
            return []
        raise FetchError("證交所月檔回應 stat=%s" % stat)
    pts = []
    for row in j.get("data") or []:
        if len(row) <= col:
            continue
        d = roc_to_date(row[0])
        c = to_float(row[col])
        if d and c is not None:
            pts.append({"d": d, "c": c})
    return pts


# ---------------------------------------------------------------- Yahoo（PLAN 7.6）


def fetch_yahoo(f, symbol, rng="1y"):
    """Yahoo chart API。回傳 (現價, 幣別, [{d, c}])。close 陣列可能含 null，要過濾。"""
    from urllib.parse import quote
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?range=%s&interval=1d" % (quote(symbol, safe=""), rng))
    j = f.get(url, delay=2.0, expect_json=True,
              headers={"Accept": "application/json, text/plain, */*"})
    err = (j.get("chart") or {}).get("error")
    if err:
        raise FetchError("Yahoo 回應錯誤：%s" % err)
    results = (j.get("chart") or {}).get("result") or []
    if not results:
        raise FetchError("Yahoo 沒有回傳 result")
    res = results[0]
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    quotes = (res.get("indicators") or {}).get("quote") or [{}]
    closes = quotes[0].get("close") or []

    pts = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        pts.append({"d": d, "c": round(float(c), 6)})

    price = meta.get("regularMarketPrice")
    if price is None and pts:
        price = pts[-1]["c"]
    if price is None:
        raise FetchError("Yahoo 沒有回傳現價，也沒有可用的收盤資料")

    # 最新一根可能還在進行中（close=null）：用現價補上那一天
    if ts:
        last_d = datetime.fromtimestamp(ts[-1], timezone.utc).strftime("%Y-%m-%d")
        if not pts or pts[-1]["d"] != last_d:
            pts.append({"d": last_d, "c": round(float(price), 6)})
        else:
            pts[-1]["c"] = round(float(price), 6)

    return float(price), meta.get("currency"), pts


# ---------------------------------------------------------------- 歷史檔讀寫


def hist_path(asset_id):
    return os.path.join(HIST_DIR, "%s.json" % asset_id)


def load_history(asset_id):
    p = hist_path(asset_id)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("points") or []
    except Exception:
        return []


def merge_points(old, new):
    """以日期為鍵合併、去重（新的蓋舊的）、依日期排序、只留最近 MAX_POINTS 點。"""
    table = {}
    for p in old:
        if p.get("d"):
            table[p["d"]] = dict(p)
    for p in new:
        if not p.get("d"):
            continue
        base = table.get(p["d"], {})
        base.update({k: v for k, v in p.items() if v is not None})
        table[p["d"]] = base
    merged = [table[k] for k in sorted(table)]
    return merged[-MAX_POINTS:]


def dump_history(asset, points):
    """每個點一行，方便在 GitHub 上看出每天差了什麼。"""
    head = {
        "id": asset["id"],
        "name": asset["name"],
        "currency": asset.get("currency"),
        "unit": asset.get("unit"),
        "count": len(points),
        "updatedAt": iso(now_tpe()),
    }
    lines = ["{"]
    for k, v in head.items():
        lines.append("  %s: %s," % (json.dumps(k, ensure_ascii=False),
                                    json.dumps(v, ensure_ascii=False)))
    lines.append("  \"points\": [")
    for i, p in enumerate(points):
        tail = "," if i < len(points) - 1 else ""
        lines.append("    " + json.dumps(p, ensure_ascii=False,
                                         separators=(",", ":")) + tail)
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def save_history(asset, points):
    os.makedirs(HIST_DIR, exist_ok=True)
    with open(hist_path(asset["id"]), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dump_history(asset, points))


# ---------------------------------------------------------------- 各類型的抓取流程


def build_quote(asset, points, price, extra=None):
    """組出 latest.json 內一個標的的成功結果。"""
    prev = None
    if len(points) >= 2:
        prev = points[-2].get("c")
    last_date = points[-1]["d"] if points else None

    change = change_pct = None
    if price is not None and prev:
        change = round(price - prev, 6)
        change_pct = round((price - prev) / prev * 100, 4)

    q = {
        "id": asset["id"],
        "name": asset["name"],
        "shortName": asset.get("shortName") or asset["name"],
        "group": asset.get("group"),
        "type": asset["type"],
        "currency": asset.get("currency"),
        "unit": asset.get("unit"),
        "decimals": asset.get("decimals", 2),
        "priceLabel": asset.get("priceLabel"),
        "status": "ok",
        "price": round(price, 6) if price is not None else None,
        "prevClose": prev,
        "change": change,
        "changePct": change_pct,
        "date": last_date,
        "points": len(points),
        "spark": [p.get("c") for p in points[-SPARK_POINTS:] if p.get("c") is not None],
        "fetchedAt": iso(now_tpe()),
        "error": None,
    }
    if extra:
        q.update(extra)
    return q


def handle_bot_gold(f, asset, ctx):
    light = ctx.get("light")
    # 黃金主頁的牌價比歷史走勢表更即時：實測 2026-08-31 23:23 主頁是
    # 19:45 掛牌的 4,562，走勢表今天那一列還停在 14:54 的 4,558。
    # 所以只要主頁抓得到，今天這一點就以主頁為準。
    page_pt = None
    if asset["symbol"] == "TWD" and ctx.get("goldPageSell") is not None:
        page_pt = {
            "d": now_tpe().strftime("%Y-%m-%d"),
            "buy": ctx.get("goldPageBuy"),
            "sell": ctx["goldPageSell"],
            "c": ctx["goldPageSell"],
        }

    if light and page_pt:
        # 盤中輕量更新：主頁已經給了今天的牌價，不必再抓一年份走勢表，
        # 歷史檔裡舊的點原封不動。
        pts = [page_pt]
    else:
        pts = fetch_bot_gold(f, asset["symbol"], light=light)
        if page_pt:
            pts = pts + [page_pt]

    merged = merge_points(load_history(asset["id"]), pts)
    latest = pts[-1]
    if asset["symbol"] == "TWD":
        # 給實體條塊卡片算「比存摺貴多少」用
        ctx["goldSell"] = latest.get("sell")
    extra = {
        "buy": latest.get("buy"),
        "sell": latest.get("sell"),
        "quoteTime": ctx.get("goldQuoteTime"),
        "quoteTimeError": ctx.get("goldQuoteTimeError"),
    }
    return merged, build_quote(asset, merged, latest.get("sell"), extra)


BAR_SPECS = [
    # 顯示名稱, 歷史檔的欄位名, 公克數
    ("1 公斤", "g1000", 1000.0),
    ("500 公克", "g500", 500.0),
    ("250 公克", "g250", 250.0),
    ("100 公克", "g100", 100.0),
    ("金鑽 1 台兩", "tael", 37.5),
]


def handle_bot_gold_bar(f, asset, ctx):
    # 實體條塊一天大致只設定一次牌價（實測掛牌時間停在營業時間內），
    # 盤中每半小時去抓是白費，所以輕量模式直接跳過、沿用上一次的結果。
    #
    # 但「上一次的結果」如果本身就是失敗的（例如雲端那一輪被台銀擋掉），
    # 跳過就會把錯誤一直延續下去——這種情況要照抓不誤。
    if ctx.get("light") and (ctx.get("prevAssets") or {}).get(
            asset["id"], {}).get("status") == "ok":
        raise SkipAsset("實體條塊一天只更新一次，盤中的輕量更新不重抓")

    bars, bar_quote_time = fetch_gold_bars(f)
    today = now_tpe().strftime("%Y-%m-%d")

    # 台銀不公布條塊的歷史牌價，所以本站從第一次執行那天起自己累積。
    # 主序列 c ＝ 1 公斤條塊的「每公克單價」，才能跟黃金存摺的每公克牌價比較。
    by_spec = {b["spec"]: b for b in bars}
    point = {"d": today}
    for spec, key, grams in BAR_SPECS:
        b = by_spec.get(spec) or {}
        if b.get("sell") is not None:
            point[key] = b["sell"]
        if spec == "金鑽 1 台兩" and b.get("buy") is not None:
            point["taelBuy"] = b["buy"]
    if point.get("g1000") is not None:
        point["c"] = round(point["g1000"] / 1000.0, 4)
    merged = merge_points(load_history(asset["id"]), [point]) if "c" in point else None

    # 每公克單價與「比黃金存摺賣出價貴多少」——今天就看得到，不用等歷史
    gold = ctx.get("goldSell")
    for b in bars:
        grams = dict((s, g) for s, _, g in BAR_SPECS).get(b["spec"])
        b["grams"] = grams
        b["perGram"] = round(b["sell"] / grams, 2) if (grams and b.get("sell")) else None
        b["premiumPct"] = (round((b["perGram"] - gold) / gold * 100, 3)
                           if (b.get("perGram") and gold) else None)

    q = {
        "id": asset["id"],
        "name": asset["name"],
        "shortName": asset.get("shortName") or asset["name"],
        "group": asset.get("group"),
        "type": asset["type"],
        "currency": asset.get("currency"),
        "unit": asset.get("unit"),
        "decimals": asset.get("decimals", 0),
        "priceLabel": asset.get("priceLabel"),
        "status": "ok",
        "price": None,
        "bars": bars,
        "quoteTime": bar_quote_time or ctx.get("goldQuoteTime"),
        "quoteTimeNote": "實體條塊的掛牌時間與黃金存摺不同，這是條塊自己的",
        "quoteTimeError": ctx.get("goldQuoteTimeError"),
        "hasHistory": bool(merged and len(merged) >= 2),
        "historyFrom": merged[0]["d"] if merged else None,
        "points": len(merged or []),
        "goldSell": gold,          # 同日的黃金存摺賣出價，供前端說明價差
        "spark": [p.get("c") for p in (merged or [])[-SPARK_POINTS:]
                  if p.get("c") is not None],
        "date": today,
        "fetchedAt": iso(now_tpe()),
        "error": None,
    }
    return merged, q


def handle_bot_fx(f, asset, ctx):
    code = asset["symbol"]
    day = ctx.get("fxDay")
    if day is None:
        day = fetch_bot_fx_day(f)
        ctx["fxDay"] = day
    if code not in day:
        raise FetchError("當日匯率 CSV 沒有 %s 這個幣別" % code)
    today = day[code]

    old = load_history(asset["id"])
    new_pts = []
    if len(old) < 60:                     # 首次執行才回補半年歷史
        new_pts = fetch_bot_fx_history(f, code)

    # 今天這一筆用當日 CSV 蓋上去（比 L6M 檔更即時）
    if today.get("spotSell") is not None:
        new_pts = new_pts + [{
            "d": now_tpe().strftime("%Y-%m-%d"),
            "spotBuy": today.get("spotBuy"),
            "spotSell": today.get("spotSell"),
            "c": today.get("spotSell"),
        }]
    merged = merge_points(old, new_pts)
    if not merged:
        raise FetchError("合併後沒有任何匯率歷史點")
    extra = {
        "spotBuy": today.get("spotBuy"),
        "spotSell": today.get("spotSell"),
        "cashBuy": today.get("cashBuy"),
        "cashSell": today.get("cashSell"),
    }
    return merged, build_quote(asset, merged, today.get("spotSell"), extra)


def handle_twse(f, asset, ctx):
    old = load_history(asset["id"])
    new_pts = []
    notes = []

    # (1) 首次執行：用 Yahoo 一次回補一年（PLAN 7.5 建議）
    if len(old) < 60 and asset.get("yahooSymbol"):
        try:
            _, _, ypts = fetch_yahoo(f, asset["yahooSymbol"], "1y")
            new_pts += ypts
        except FetchError as e:
            notes.append("Yahoo 回補失敗：%s" % e)

    # (2) 官方月檔：本月（歷史不足時多補上個月）＝收盤定案。
    #     盤中的輕量更新不需要它——即時報價就夠了，收盤定案交給每天的完整更新。
    #     ⚠ 證交所的 STOCK_DAY 只有「上市」股票，上櫃（otc）查不到，
    #       那些標的的歷史一律走 Yahoo（PLAN 7.6 允許的來源）。
    is_otc = (asset.get("market") or "tse") == "otc"
    if is_otc:
        pass
    elif not (ctx.get("light") and len(old) >= 60):
        months = [now_tpe().strftime("%Y%m")]
        if len(old) + len(new_pts) < 60:
            prev_m = now_tpe().replace(day=1) - timedelta(days=1)
            months.insert(0, prev_m.strftime("%Y%m"))
        for ym in months:
            try:
                new_pts += fetch_twse_month(f, asset, ym)
            except FetchError as e:
                notes.append("官方月檔 %s 失敗：%s" % (ym, e))

    # (3) 即時報價（一次抓三檔，結果放 ctx 共用）
    rt = ctx.get("twseRealtime")
    rt_err = ctx.get("twseRealtimeError")
    row = (rt or {}).get(asset["symbol"])

    price = None
    extra = {}
    if row and row.get("price") is not None:
        price = row["price"]
        if row.get("date"):
            new_pts.append({"d": row["date"], "c": price})
        extra = {
            "open": row.get("open"), "high": row.get("high"), "low": row.get("low"),
            "quoteTime": row.get("time"), "sourceNote": row.get("note"),
        }

    merged = merge_points(old, new_pts)

    if price is None:
        # 即時源掛了，退回 Yahoo 現價；再不行就用最後一個收盤價並註明
        if asset.get("yahooSymbol"):
            try:
                yprice, _, ypts = fetch_yahoo(f, asset["yahooSymbol"], "1mo")
                price = yprice
                new_pts += ypts
                merged = merge_points(old, new_pts)
                extra["sourceNote"] = "證交所即時報價無回應，改用 Yahoo 備援報價"
            except FetchError as e:
                notes.append("Yahoo 備援也失敗：%s" % e)
        if price is None and merged:
            price = merged[-1].get("c")
            extra["sourceNote"] = "即時報價來源失敗，顯示的是最後一個已知收盤價"

    if not merged:
        raise FetchError("沒有取得任何資料（%s）"
                         % ("；".join(notes) or rt_err or "來源皆失敗"))

    q = build_quote(asset, merged, price, extra)
    if row and row.get("prevClose"):
        q["prevClose"] = row["prevClose"]
        if price is not None:
            q["change"] = round(price - row["prevClose"], 6)
            q["changePct"] = round((price - row["prevClose"]) / row["prevClose"] * 100, 4)
    if notes:
        q["warnings"] = notes
    return merged, q


def handle_yahoo(f, asset, ctx):
    old = load_history(asset["id"])
    # 首次執行補一年；平常補三個月；盤中輕量更新只要最近五天（回應小很多）
    if len(old) < 60:
        rng = "1y"
    elif ctx.get("light"):
        rng = "5d"
    else:
        rng = "3mo"
    price, currency, pts = fetch_yahoo(f, asset["symbol"], rng)
    merged = merge_points(old, pts)
    extra = {}
    if currency and currency != asset.get("currency"):
        extra["sourceNote"] = "來源幣別為 %s" % currency
    return merged, build_quote(asset, merged, price, extra)


HANDLERS = {
    "bot_gold": handle_bot_gold,
    "bot_gold_bar": handle_bot_gold_bar,
    "twse_index": handle_twse,
    "twse_stock": handle_twse,
    "yahoo": handle_yahoo,
    "bot_fx": handle_bot_fx,
}


# ---------------------------------------------------------------- 主流程


def guess_slot():
    """沒指定 --slot 時，依台北時間猜：12 點前晨報 / 14:30 前午盤 / 之後收盤。"""
    n = now_tpe()
    mins = n.hour * 60 + n.minute
    if mins < 12 * 60:
        return "morning"
    if mins < 14 * 60 + 30:
        return "midday"
    return "close"


SLOT_LABEL = {
    "morning": "晨報（了解今天狀況）",
    "midday": "午盤（盤中整理）",
    "close": "收盤（檢討與分析）",
    "manual": "手動更新",
}


def owner_of(asset):
    """這個標的歸誰抓。assets.json 沒寫的話當成 cloud（保守值：雲端一定跑得到）。"""
    return asset.get("owner") or "cloud"


def shard_path(source):
    return os.path.join(SOURCES_DIR, "%s.json" % source)


def read_json_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def load_prev_shard(source, enabled):
    """讀「自己這一邊」上一次的結果。

    為什麼一定要讀自己的分片而不是 latest.json？
    latest.json 是兩邊合併出來的，裡面有對方的標的。如果從那裡拿上一次結果，
    雲端跑 --light 時就會把本機那 5 項抄進 cloud.json，等於又碰了不該碰的東西。

    分片還不存在時（第一次部署、或還沒跑過 migrate_to_shards.py），
    退回讀 latest.json，但**只取自己 owner 的標的**。
    """
    d = read_json_file(shard_path(source))
    if d.get("assets"):
        return d["assets"]

    mine = {a["id"] for a in enabled if owner_of(a) == source}
    latest = read_json_file(LATEST_FILE)
    return {k: v for k, v in (latest.get("assets") or {}).items() if k in mine}


def write_shard(source, slot, mode, assets_map, run_at, requests_count):
    """寫出這一邊的分片。整檔覆蓋，但只覆蓋自己那一個檔。"""
    os.makedirs(SOURCES_DIR, exist_ok=True)
    payload = {
        "source": source,
        "runAt": iso(run_at),
        "runAtText": run_at.strftime("%Y-%m-%d %H:%M"),
        "timezone": "Asia/Taipei (UTC+8)",
        "slot": slot,
        "slotLabel": SLOT_LABEL.get(slot, slot),
        "mode": mode,
        "requests": requests_count,
        "count": len(assets_map),
        "assets": assets_map,
    }
    with open(shard_path(source), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    return payload


def stamp_times(quote, source, now, prev_entry):
    """補上三個時間欄位（這是分片格式的重點，merge 與 freshness 都靠它）。

    lastAttemptAt：這次嘗試抓取的時間，不管成敗都寫。
    lastSuccessAt：最後一次「真的抓成功」的時間。這次失敗就沿用上一次的值；
                   從來沒成功過就是 None——merge 會據此判成 error 而不是過期。
    source       ：哪一邊抓的，方便前端與除錯時一眼看出責任歸屬。
    """
    prev_entry = prev_entry or {}
    quote["source"] = source
    quote["lastAttemptAt"] = iso(now)
    if quote.get("status") == "ok":
        quote["lastSuccessAt"] = iso(now)
    else:
        quote["lastSuccessAt"] = prev_entry.get("lastSuccessAt")
    return quote


def error_quote(asset, message, prev_assets):
    """來源失敗時的紀錄：明確標成 error，並附上上次成功的資訊供前端標示。"""
    q = {
        "id": asset["id"],
        "name": asset["name"],
        "shortName": asset.get("shortName") or asset["name"],
        "group": asset.get("group"),
        "type": asset["type"],
        "currency": asset.get("currency"),
        "unit": asset.get("unit"),
        "decimals": asset.get("decimals", 2),
        "priceLabel": asset.get("priceLabel"),
        "status": "error",
        "price": None,
        "error": message,
        "errorAt": iso(now_tpe()),
        "fetchedAt": iso(now_tpe()),
    }
    old = prev_assets.get(asset["id"]) or {}
    src = old if old.get("status") == "ok" else (old.get("lastGood") or {})
    if src.get("price") is not None or src.get("bars"):
        q["lastGood"] = {
            "price": src.get("price"),
            "bars": src.get("bars"),
            "date": src.get("date"),
            "fetchedAt": src.get("fetchedAt"),
        }
    return q


def main():
    ap = argparse.ArgumentParser(description="InvestWatch 行情抓取")
    ap.add_argument("--slot", choices=["morning", "midday", "close", "manual"],
                    default=None, help="更新時段；不給就依台北時間自動判斷")
    ap.add_argument("--only", default=None, help="只抓這些 id（逗號分隔），測試用")
    ap.add_argument("--light", action="store_true",
                    help="輕量更新：只更新現價，不重抓一年份歷史。"
                         "給盤中每半小時的密集更新用。")
    # 必填，沒有預設值。曾經預設 all，結果 workflow 少寫一個參數就變成
    # 「雲端去抓台銀那 5 項並覆寫 data/sources/local.json」——正是分片架構要防的事，
    # 卻只靠「記得加參數」在維持。改成必填，忘了帶就直接報錯，不會安靜地做錯事。
    ap.add_argument("--source", choices=["cloud", "local", "all"], required=True,
                    help="這一輪由誰負責：cloud=GitHub Actions、local=家用電腦。"
                         "只會處理 assets.json 裡 owner 相符的標的，"
                         "其餘標的完全不碰。all 只給手動測試用，會寫出兩個分片。")
    args = ap.parse_args()

    slot = args.slot or guess_slot()
    only = set(x.strip() for x in args.only.split(",")) if args.only else None
    light = args.light
    source = args.source

    with open(ASSETS_FILE, encoding="utf-8") as fh:
        cfg = json.load(fh)
    enabled = [a for a in cfg["assets"] if a.get("enabled", True)]

    # --- 依 owner 縮到「這一輪該我管的標的」---------------------------------
    # 這是整個分片設計的核心：不是自己 owner 的標的，從這裡就被排除，
    # 後面完全不會被抓、不會寫 history、也不會出現在自己的分片裡。
    scoped = [a for a in enabled if owner_of(a) == source] if source != "all" else enabled
    if not scoped:
        print("assets.json 裡沒有任何 owner=%s 的標的，沒事可做。" % source)
        return 1

    # --- --only 在 owner 範圍「內」再過濾 ------------------------------------
    # 指定到範圍外的 id 要直接報錯中止，不能默默忽略——
    # 這樣之後若有別的程式誤用（例如 watchdog 拿錯清單）會立刻被發現。
    if only:
        outside = sorted(only - {a["id"] for a in scoped})
        if outside:
            print("錯誤：--only 指定的 %s 不屬於 --source %s。"
                  % ("、".join(outside), source))
            print("      這一輪負責的標的是：%s"
                  % "、".join(a["id"] for a in scoped))
            return 2
        assets = [a for a in scoped if a["id"] in only]
    else:
        assets = scoped

    # 同一個 owner 底下沒被 --only 選到的標的：沿用上次結果並標 carriedOver。
    # 分片對它自己的 owner 必須永遠是完整的，不能因為 --only 就少幾項。
    skipped = [a for a in scoped if a not in assets]

    started = now_tpe()
    print("=" * 62)
    print("InvestWatch 資料更新  時段：%s（%s）%s"
          % (slot, SLOT_LABEL.get(slot, slot), "　【輕量】" if light else ""))
    print("台北時間：%s  標的數：%d" % (started.strftime("%Y-%m-%d %H:%M:%S"), len(assets)))
    print("=" * 62)

    f = Fetcher()
    ctx = {"light": light}
    # 上一次的結果一律讀「自己的分片」。--source all 時兩邊都要讀進來。
    if source == "all":
        prev_assets = {}
        for s in ("cloud", "local"):
            prev_assets.update(load_prev_shard(s, enabled))
    else:
        prev_assets = load_prev_shard(source, enabled)
    ctx["prevAssets"] = prev_assets      # 讓 handler 能判斷上次是否成功

    # --- 先抓多個標的共用的來源，抓一次大家一起用 ---
    types = set(a["type"] for a in assets)

    if "bot_gold" in types or "bot_gold_bar" in types:
        try:
            qt, gbuy, gsell = fetch_gold_page(f)
            ctx["goldQuoteTime"] = qt
            ctx["goldPageBuy"] = gbuy
            ctx["goldPageSell"] = gsell
            print(". 黃金掛牌時間：%s（存摺台幣 買 %s / 賣 %s）"
                  % (qt, gbuy, gsell))
        except Exception as e:
            ctx["goldQuoteTime"] = None
            ctx["goldQuoteTimeError"] = str(e)
            print("! 黃金主頁抓取失敗：%s" % e)

    twse_codes = [(a["symbol"], a.get("market") or "tse") for a in assets
                  if a["type"] in ("twse_index", "twse_stock")]
    if twse_codes:
        try:
            ctx["twseRealtime"] = fetch_twse_realtime(f, twse_codes)
            print(". 台股即時報價：%d 檔" % len(ctx["twseRealtime"]))
        except Exception as e:
            ctx["twseRealtime"] = None
            ctx["twseRealtimeError"] = str(e)
            print("! 台股即時報價失敗（稍後改用備援）：%s" % e)

    # --- 逐一處理各標的 ---
    results = {}
    ok_ids, err_ids = [], []

    for a in assets:
        print("\n[%s] %s" % (a["id"], a["name"]))
        handler = HANDLERS.get(a["type"])
        if handler is None:
            results[a["id"]] = stamp_times(
                error_quote(a, "assets.json 的 type=%s 沒有對應的抓取方式" % a["type"],
                            prev_assets),
                owner_of(a), now_tpe(), prev_assets.get(a["id"]))
            err_ids.append(a["id"])
            print("    x 未知的 type")
            continue
        try:
            points, quote = handler(f, a, ctx)
            if points is not None:
                save_history(a, points)
                print("    OK 歷史 %d 點（%s ~ %s）"
                      % (len(points), points[0]["d"], points[-1]["d"]))
            if quote.get("price") is not None:
                print("    OK 現價 %s %s" % (quote["price"], a.get("currency") or ""))
            if quote.get("bars"):
                print("    OK 條塊 %d 種規格" % len(quote["bars"]))
            for w in quote.get("warnings") or []:
                print("    . 注意：%s" % w)
            results[a["id"]] = stamp_times(
                quote, owner_of(a), now_tpe(), prev_assets.get(a["id"]))
            ok_ids.append(a["id"])
        except SkipAsset as e:
            # 刻意不更新：沿用上次結果並標明原因，不算失敗
            old = prev_assets.get(a["id"])
            if old:
                carried = dict(old)
                carried["carriedOver"] = True
                carried["carriedReason"] = str(e)
                carried["source"] = owner_of(a)   # 舊資料可能沒有這欄，補上
                results[a["id"]] = carried
                if carried.get("status") == "ok":
                    ok_ids.append(a["id"])
                else:
                    err_ids.append(a["id"])
            print("    - 跳過：%s（沿用上次結果）" % e)
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            if not isinstance(e, FetchError):
                msg = "%s: %s" % (e.__class__.__name__, msg)
                traceback.print_exc(limit=2)
            results[a["id"]] = stamp_times(
                error_quote(a, msg, prev_assets),
                owner_of(a), now_tpe(), prev_assets.get(a["id"]))
            err_ids.append(a["id"])
            print("    x 失敗：%s（保留舊歷史不動）" % msg)

    ran_ok, ran_err = len(ok_ids), len(err_ids)

    # --only 只抓部分標的時，其餘標的沿用上次的結果，不要把 latest.json 洗掉。
    # 沿用的項目標上 carriedOver，前端才知道那不是這一輪抓的。
    for a in skipped:
        old = prev_assets.get(a["id"])
        if not old:
            continue
        carried = dict(old)
        carried["carriedOver"] = True
        carried["source"] = owner_of(a)           # 同上
        results[a["id"]] = carried
        if carried.get("status") == "ok":
            ok_ids.append(a["id"])
        else:
            err_ids.append(a["id"])

    finished = now_tpe()
    mode = "light" if light else "full"

    # --- 寫出分片 -----------------------------------------------------------
    # 這支程式不再寫 data/latest.json。理由：兩邊都寫同一個檔就是災情的來源
    #（2026-09-03 20:00 本機抓到黃金、20:07 雲端抓不到就把它蓋成 error）。
    # 現在各自只寫自己的分片，latest.json 交給 scripts/merge_latest.py 算。
    #
    # --source all 會處理全部 13 項，所以要依 owner 拆成兩個分片分別寫出去。
    # all 只給手動測試用；正式排程一定要指定 cloud 或 local。
    # 歸屬一律以 assets.json 的 owner 為準，不看結果裡的 source 欄位——
    # 遷移前的舊資料沒有 source，用它判斷會把本機的標的錯放進雲端分片。
    owner_map = {a["id"]: owner_of(a) for a in enabled}
    targets = ["cloud", "local"] if source == "all" else [source]
    for s in targets:
        mine = {aid: q for aid, q in results.items()
                if owner_map.get(aid) == s}
        if source == "all" and not mine:
            continue
        write_shard(s, slot, mode, mine, finished, f.count)
        print("\n. 已寫出分片 data/sources/%s.json（%d 項）" % (s, len(mine)))

    print("\n" + "=" * 62)
    print("完成：成功 %d / 失敗 %d（共發出 %d 次請求，耗時 %d 秒）"
          % (ran_ok, ran_err, f.count, (finished - started).total_seconds()))
    if skipped:
        print("本次未更新、沿用上次結果：%d 項" % len(skipped))
    if err_ids:
        print("失敗標的：%s" % ", ".join(err_ids))
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
