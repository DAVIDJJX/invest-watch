#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_finmind_fx.py — 拿 FinMind 的台銀每日匯率，逐日對本站既有的匯率歷史（只讀不寫）

為什麼要有這支？
    匯率的來源從「家用電腦直接抓台銀 CSV」換成「雲端經 FinMind 抓」。換來源之前一定要先證明
    兩邊講的是同一組數字；欄位一旦對錯（例如把現金賣出當成即期賣出），走勢圖會無聲地接上
    另一條線。規則沿用 2026-09-06 稽核歷史時定的那一條：
        任何一筆差額超過 0.3% 就停下來問人，不要自己決定誰對。

它做什麼：
    對 data/assets.json 裡每一個 type=finmind_fx 的標的，各發【一個】請求，從本站歷史的第一天
    問到今天，逐日比對 spotBuy／spotSell，印出：
        比對了幾天、幾天完全一致、每一筆不一致的明細、只有本站有的日期、只有 FinMind 有的日期、
        以及切換後會被補進歷史的新日期。
    不寫任何檔案。

用法：
    python scripts/audit_finmind_fx.py
    python scripts/audit_finmind_fx.py --threshold 0.3

結束碼：0 = 沒有任何一筆超過門檻；1 = 有，請停下來看明細；2 = 來源抓不到。
"""

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data as fd                                      # noqa: E402

FIELDS = (("spotBuy", "即期買入"), ("spotSell", "即期賣出"))


def pct(ours, theirs):
    return abs(theirs - ours) / ours * 100.0 if ours else float("inf")


def audit_one(f, asset, threshold):
    pts = fd.load_history(asset["id"])
    if not pts:
        print("  本站還沒有任何歷史點，沒有東西可以比。")
        return 0, 0
    first, last = pts[0]["d"], pts[-1]["d"]
    rows = fd.fetch_finmind_fx(f, asset["symbol"], first)      # 一個幣別一個請求
    theirs = dict((r["date"], r) for r in rows)
    ours = dict((p["d"], p) for p in pts)

    compared = same = over = 0
    diffs = []
    for d in sorted(ours):
        if d not in theirs:
            continue
        compared += 1
        day_same = True
        for key, label in FIELDS:
            a, b = ours[d].get(key), theirs[d].get(key)
            if a is None or b is None:
                continue
            if abs(a - b) > 1e-9:
                day_same = False
                p = pct(a, b)
                if p > threshold:
                    over += 1
                diffs.append((d, label, a, b, p))
        if day_same:
            same += 1

    only_ours = [d for d in sorted(ours) if d not in theirs]
    only_theirs = [d for d in sorted(theirs) if d not in ours and d <= last]
    to_append = [d for d in sorted(theirs) if d > last]

    print("  本站歷史：%d 點（%s ~ %s）；FinMind 回傳 %d 列" % (len(pts), first, last, len(rows)))
    print("  逐日比對 %d 天：完全一致 %d 天、有差異 %d 天、超過 %.1f%% 的 %d 筆"
          % (compared, same, compared - same, threshold, over))
    for d, label, a, b, p in diffs:
        print("    %s %s：本站 %s／FinMind %s（差 %.4f%%）%s"
              % (d, label, a, b, p, "  ★ 超過門檻" if p > threshold else ""))
    print("  只有本站有的日期（%d）：%s" % (len(only_ours), "、".join(only_ours) or "無"))
    print("  只有 FinMind 有、且在本站歷史範圍內的日期（%d）：%s"
          % (len(only_theirs), "、".join(only_theirs) or "無"))
    print("  切換後會補進歷史的新日期（%d）：%s" % (len(to_append), "、".join(to_append) or "無"))
    for d in to_append:
        r = theirs[d]
        print("    %s 即期買入 %s／即期賣出 %s／現金買入 %s／現金賣出 %s"
              % (d, r["spotBuy"], r["spotSell"], r["cashBuy"], r["cashSell"]))
    return over, compared


def main():
    ap = argparse.ArgumentParser(description="FinMind 台銀每日匯率 vs 本站匯率歷史（只讀）")
    ap.add_argument("--threshold", type=float, default=0.3, help="差額超過幾 %% 就算有問題（預設 0.3）")
    args = ap.parse_args()

    with open(fd.ASSETS_FILE, encoding="utf-8") as fh:
        assets = [a for a in json.load(fh)["assets"]
                  if a.get("enabled", True) and a.get("type") == "finmind_fx"]
    if not assets:
        print("assets.json 裡沒有 type=finmind_fx 的標的。")
        return 0

    f = fd.Fetcher(verbose=False)
    total_over = 0
    print("稽核時間（台北）：%s" % fd.now_tpe().strftime("%Y-%m-%d %H:%M:%S"))
    for a in assets:
        print("\n[%s] %s（%s）" % (a["id"], a["name"], a["symbol"]))
        try:
            over, _ = audit_one(f, a, args.threshold)
        except fd.FetchError as e:
            print("  x 來源抓不到：%s" % e)
            return 2
        total_over += over

    print("\n共發出 %d 次請求。" % f.count)
    if total_over:
        print("★ 有 %d 筆差額超過 %.1f%%：停下來，不要切換來源，先弄清楚是哪個欄位對錯了。"
              % (total_over, args.threshold))
        return 1
    print("沒有任何一筆超過 %.1f%%，可以切換。" % args.threshold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
