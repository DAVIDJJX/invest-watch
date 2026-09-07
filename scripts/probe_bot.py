#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_bot.py — 量測：這台機器抓臺灣銀行，到底哪些頁面抓得到？

為什麼需要這一支？
    筆電存在的唯一理由是 2026-09-03 那次 GitHub Actions 抓台銀被擋。
    但那可能只是暫時的、也可能只擋某幾頁。這件事到現在沒有人系統性地驗證過，
    而停點 8 要決定「哪些標的的 owner 可以從 local 改成 cloud」，方向錯了會很貴。
    這一支【只量測、不改任何正式程式、不寫任何資料檔】。

量測條件必須跟正式抓取一模一樣，否則結果沒有意義：
    * 直接用 scripts/fetch_data.py 裡的 Fetcher 與 BROWSER_HEADERS（含重載驗證頁的邏輯）
    * 直接用正式的解析函式判斷「內容有沒有真的解析成功」——
      只看 HTTP 狀態碼會被機器人驗證頁騙（它常常回 200）

但有一件事刻意跟正式流程不同：
    ★ 每個網址用一個【全新的】Fetcher。
      Fetcher 一旦在某個網址被擋，會把 rate.bot.com.tw 整個記進 blocked，
      之後同一個 Fetcher 對這個 host 的所有請求都直接丟例外、根本不連網。
      共用一個 Fetcher 的話，「只擋黃金頁」和「全部被擋」在結果上會長得一模一樣。
      分開之後每個網址都是獨立的問題，才量得出差別。

順序輪替：
    每次執行把五個網址的順序輪一格（第 1 次 a→e、第 2 次 b→e→a …），
    並記下每個網址是這一次的第幾個。頻率型的防護常常跟「請求順位」有關，
    輪替之後表格看得出來，成本是零。

彙整時的判定規則（停點 8 要用）：
    三組分開判，每一組【所有樣本全部通過】才算「抓得到」，差一次都不算——
    不穩定就是不能依賴，原因是 IP 還是時段對決策沒有差別。
      匯率組   a、b  → 通過則 fx_usd、fx_cny 可以改由雲端抓
      存摺組   c、d  → 通過則 gold_twd、gold_cny 可以改由雲端抓
      條塊組   e     → 通過則 gold_bar 可以改由雲端抓
    沒通過的組留在筆電。
    ⚠ 這次量的是「會不會被直接擋」，不是「每 30 分鐘一次會不會被限流」——
      後者由停點 8-A 那條「連續 3 次被擋就退回 stale」來守。

用法：
    python scripts/probe_bot.py                          # 印在畫面上
    python scripts/probe_bot.py --summary out.md         # 另外寫一份 markdown
    python scripts/probe_bot.py --rotate 2               # 指定輪替起點（雲端用 GITHUB_RUN_NUMBER）
結束碼永遠是 0：這是量測，不是關卡。
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data as fd                                      # noqa: E402

TPE = timezone(timedelta(hours=8))
GAP_BETWEEN_URLS = 3.0        # 網址之間至少隔這麼久（禮貌；Fetcher 自己另有 2.5 秒同站間隔）
FINGERPRINT_CHARS = 200       # 失敗時只留回應前這麼多字，不存整份 HTML


# --------------------------------------------------------------------------
# 五個網址與各自的「解析成功」判準——全部用正式的解析函式
# --------------------------------------------------------------------------

def check_fx_day(content):
    """a. 當日匯率 CSV：要找得到 USD 那一列（跟 fetch_bot_fx_day 同一種讀法）。"""
    txt = fd.decode_bot_csv(content)
    for line in txt.strip().splitlines()[1:]:
        p = line.split(",")
        if len(p) >= 14 and p[0].strip() == "USD":
            return True, "USD 即期賣出=%s" % p[13].strip()
    return False, "CSV 裡找不到 USD 列"


def check_fx_l6m(content):
    """b. 半年匯率 CSV：要找得到 USD 列，而且第一欄是 YYYYMMDD 的資料日期。"""
    txt = fd.decode_bot_csv(content)
    rows = 0
    for line in txt.strip().splitlines()[1:]:
        p = line.split(",")
        if len(p) >= 15 and re.match(r"^\d{8}$", p[0].strip()) and p[1].strip() == "USD":
            rows += 1
    return (rows >= 1), "USD 資料列 %d 筆" % rows


def check_gold_page(content):
    """c. 黃金主頁：要找得到「掛牌時間」（跟 fetch_gold_page 同一條正規式）。"""
    html = content.decode("utf-8", "replace")
    m = re.search(r"掛牌時間：\s*([0-9]{4}/[0-9]{2}/[0-9]{2}\s+[0-9]{2}:[0-9]{2})", html)
    return (m is not None), ("掛牌時間=%s" % m.group(1)) if m else "找不到「掛牌時間」"


def check_gold_chart(content):
    """d. 黃金走勢表：正式的 parse_gold_chart 要解析出 200 列以上。"""
    html = content.decode("utf-8", "replace")
    pts = fd.parse_gold_chart(html)
    return (len(pts) >= 200), "解析出 %d 列" % len(pts)


def check_gold_bars(content):
    """e. 實體金條塊：正式的表格解析器要拿到至少 3 個規格的賣出價。"""
    html = content.decode("utf-8", "replace")
    table = fd._table_by_summary(html, "此表格為黃金條塊表格")
    if table is None:
        return False, "找不到黃金條塊表格"
    sells = [v for v in fd._row_values(table, "本行賣出") if v is not None]
    return (len(sells) >= 3), "解析出 %d 個規格的賣出價" % len(sells)


URLS = [
    ("a", "匯率當日 CSV",  "https://rate.bot.com.tw/xrt/flcsv/0/day",      check_fx_day),
    ("b", "匯率半年 CSV",  "https://rate.bot.com.tw/xrt/flcsv/0/L6M/USD",  check_fx_l6m),
    ("c", "黃金存摺主頁",  "https://rate.bot.com.tw/gold?Lang=zh-TW",      check_gold_page),
    ("d", "黃金走勢表",    "https://rate.bot.com.tw/gold/chart/year/TWD",  check_gold_chart),
    ("e", "實體金條塊",    "https://rate.bot.com.tw/gold/quote/recent",    check_gold_bars),
]

GROUPS = {"匯率組": "ab", "存摺組": "cd", "條塊組": "e"}


# --------------------------------------------------------------------------
# 量測
# --------------------------------------------------------------------------

def fingerprint(resp):
    """失敗時留一個小指紋：狀態碼、<title>、前 200 字（去掉換行）。
    萬一台銀換了驗證頁樣式，我們看得出來它長什麼樣，但不存整份 HTML。"""
    if resp is None:
        return {"status": None, "title": None, "head": None}
    html = resp.content.decode("utf-8", "replace")
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    head = re.sub(r"\s+", " ", html[:FINGERPRINT_CHARS * 3]).strip()[:FINGERPRINT_CHARS]
    return {
        "status": resp.status_code,
        "title": re.sub(r"\s+", " ", m.group(1)).strip()[:80] if m else None,
        "head": head,
    }


def probe_one(key, name, url, checker, position):
    """對一個網址做一次完整的正式抓取＋正式解析。"""
    f = fd.Fetcher(verbose=True)          # ★ 每個網址一個全新的 Fetcher，理由見檔頭

    # 把 session.get 包一層，記下最後一個回應——Fetcher 被擋時會丟例外、不回傳回應，
    # 但指紋需要它。這只是旁觀，不改變 Fetcher 任何行為。
    last = {"resp": None}
    real_get = f.session.get

    def recording_get(*a, **kw):
        r = real_get(*a, **kw)
        last["resp"] = r
        return r
    f.session.get = recording_get

    started = time.time()
    result = {
        "key": key, "name": name, "url": url, "position": position,
        "ok": False, "status": None, "bytes": None, "requests": 0,
        "detail": None, "error": None, "fingerprint": None,
        "seconds": None,
    }
    try:
        r = f.get(url, delay=2.5)
        result["status"] = r.status_code
        result["bytes"] = len(r.content)
        ok, detail = checker(r.content)
        result["ok"] = bool(ok)
        result["detail"] = detail
        if not ok:
            result["fingerprint"] = fingerprint(r)
    except Exception as e:                # FetchError（含被擋）或解析例外
        result["error"] = str(e)
        resp = last["resp"]
        if resp is not None:
            result["status"] = resp.status_code
            result["bytes"] = len(resp.content)
        result["fingerprint"] = fingerprint(resp)
    finally:
        result["requests"] = f.count
        result["seconds"] = round(time.time() - started, 1)
    return result


def rotated(start):
    n = len(URLS)
    start = start % n
    return URLS[start:] + URLS[:start]


def main():
    ap = argparse.ArgumentParser(description="量測台銀五個頁面抓不抓得到")
    ap.add_argument("--summary", default=None, help="把 markdown 表格另外寫到這個檔")
    ap.add_argument("--rotate", type=int, default=None,
                    help="輪替起點（0~4）。不給就用 GITHUB_RUN_NUMBER，本機預設 0")
    args = ap.parse_args()

    run_no = os.environ.get("GITHUB_RUN_NUMBER")
    start = args.rotate if args.rotate is not None else (int(run_no) if run_no else 0)
    where = "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local"
    now = datetime.now(TPE)

    print("=" * 70)
    print("台銀抓取量測  %s  台北時間 %s  輪替起點=%d"
          % (where, now.strftime("%Y-%m-%d %H:%M:%S"), start % len(URLS)))
    print("=" * 70)

    results = []
    order = rotated(start)
    for i, (key, name, url, checker) in enumerate(order, start=1):
        if i > 1:
            time.sleep(GAP_BETWEEN_URLS)
        print("\n[%d/%d] %s. %s\n      %s" % (i, len(order), key, name, url))
        r = probe_one(key, name, url, checker, position=i)
        results.append(r)
        mark = "✓ 解析成功" if r["ok"] else "✗ 失敗"
        print("      %s  HTTP %s  %s bytes  請求 %d 次  %.1f 秒  %s"
              % (mark, r["status"], r["bytes"], r["requests"], r["seconds"],
                 r["detail"] or r["error"] or ""))
        if r["fingerprint"]:
            fp = r["fingerprint"]
            print("      指紋：title=%r  head=%r" % (fp.get("title"), fp.get("head")))

    by_key = {r["key"]: r for r in results}
    groups = {g: all(by_key[k]["ok"] for k in keys) for g, keys in GROUPS.items()}

    payload = {
        "at": now.isoformat(timespec="seconds"),
        "where": where,
        # 只記 Actions 的 runner 名稱。本機不記電腦名稱——那是個人資訊，也沒有量測價值。
        "runner": os.environ.get("RUNNER_NAME") or None,
        "run_number": int(run_no) if run_no else None,
        "rotate": start % len(URLS),
        "groups": groups,
        "results": sorted(results, key=lambda r: r["key"]),
    }
    print("\nPROBE_RESULT " + json.dumps(payload, ensure_ascii=False))

    md = render_markdown(payload)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(md)
    print("\n" + md)
    return 0


def render_markdown(p):
    lines = []
    lines.append("## 台銀抓取量測 — %s（台北）" % p["at"].replace("T", " ")[:19])
    lines.append("")
    lines.append("- 執行環境：`%s`%s" % (p["where"], ("（runner: `%s`）" % p["runner"]) if p["runner"] else ""))
    lines.append("- 輪替起點：%d（這一次從 %s 開始）" % (p["rotate"], URLS[p["rotate"]][0]))
    lines.append("")
    lines.append("| # | 順位 | 頁面 | 結果 | HTTP | 大小 | 請求次數 | 說明 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in p["results"]:
        mark = "✓" if r["ok"] else "✗"
        note = r["detail"] or r["error"] or ""
        lines.append("| %s | %d | %s | %s | %s | %s | %d | %s |" % (
            r["key"], r["position"], r["name"], mark,
            r["status"] if r["status"] is not None else "—",
            r["bytes"] if r["bytes"] is not None else "—",
            r["requests"], note.replace("|", "／")))
    lines.append("")
    lines.append("| 組別 | 網址 | 這一次 |")
    lines.append("|---|---|---|")
    for g, keys in GROUPS.items():
        lines.append("| %s | %s | %s |" % (g, "、".join(keys), "✓ 全過" if p["groups"][g] else "✗"))
    fps = [r for r in p["results"] if r.get("fingerprint")]
    if fps:
        lines.append("")
        lines.append("<details><summary>失敗回應的指紋</summary>")
        lines.append("")
        for r in fps:
            fp = r["fingerprint"]
            lines.append("- **%s. %s**：HTTP %s，title=`%s`" % (
                r["key"], r["name"], fp.get("status"), fp.get("title")))
            lines.append("  ```")
            lines.append("  %s" % (fp.get("head") or ""))
            lines.append("  ```")
        lines.append("")
        lines.append("</details>")
    lines.append("")
    lines.append("> 判定規則：每一組所有樣本全部通過才算「抓得到」。這次量的是會不會被直接擋，"
                 "不是每 30 分鐘一次會不會被限流。")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
