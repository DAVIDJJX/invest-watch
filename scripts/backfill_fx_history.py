#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_fx_history.py — 把匯率的日線歷史往前回補到 400 個營業日（分析系列 A1-6，一次性）

為什麼要有這支？
    data/history/fx_usd.json、fx_cny.json 是 2026-03-02 才開始累積的（約 145 點），卡片上的區間位置
    算不出完整的一年。FinMind 轉載的台銀每日牌價可以往前問，所以一次把它補到 400 點。

三條規則（沿用停點 8-0）：
    1. 只增不改：只新增【比現有第一天更早】的日期；現有的點一個欄位都不動、dateSource 不改寫。
    2. 重疊區逐日比對：同一天兩邊都有的，即期買入／即期賣出任何一筆差超過 0.3% 就停下來、什麼都不寫，
       把明細印出來請人決定（結束碼 1）。
    3. 新補的點標 dateSource=finmind；補完只留最近 400 點（跟 fetch_data.py 的上限一樣）。

用法：
    python scripts/backfill_fx_history.py --dry-run          # 只比對、只列出會補哪些日期，不寫檔
    python scripts/backfill_fx_history.py                    # 真的補（每個幣別一個請求）
    python scripts/backfill_fx_history.py --threshold 0.3 --days 600

結束碼：0＝補好了（或 dry-run 沒有問題）；1＝重疊區有超過門檻的差額，沒有寫檔；2＝來源抓不到。
"""
import argparse
import json
import os
import sys
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fetch_data as fd      # noqa: E402
import net_policy            # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:            # noqa: B902
    pass

THRESHOLD_PCT = 0.3
DEFAULT_DAYS = 600           # 約 400 個營業日再多一點


def diff_pct(ours, theirs):
    if not ours or not theirs:
        return None
    return abs(theirs - ours) / ours * 100.0


def plan_backfill(old_points, rows, threshold=THRESHOLD_PCT):
    """old_points：現有的歷史點；rows：FinMind 解析後的每日列（[{date, spotBuy, spotSell, …}]）。
    回傳 (new_points, report)。report 裡有重疊區的比對結果；有超過門檻的就不該寫檔。"""
    by_day = dict((p["d"], p) for p in old_points if p.get("d"))
    first = min(by_day) if by_day else None
    compared, same, over, diffs = 0, 0, [], []
    added = []
    for r in rows:
        d = r["date"]
        if d in by_day:
            compared += 1
            ok = True
            for ours_key, theirs_key, label in (("spotBuy", "spotBuy", "即期買入"), ("spotSell", "spotSell", "即期賣出")):
                a, b = by_day[d].get(ours_key), r.get(theirs_key)
                if a is None or b is None:
                    continue
                p = diff_pct(a, b)
                if p is not None and abs(a - b) > 1e-9:
                    ok = False
                    row = {"d": d, "field": label, "ours": a, "finmind": b, "diffPct": round(p, 4)}
                    diffs.append(row)
                    if p > threshold:
                        over.append(row)
            if ok:
                same += 1
        elif first is None or d < first:
            added.append({"d": d, "spotBuy": r.get("spotBuy"), "spotSell": r["spotSell"], "c": r["spotSell"], "dateSource": "finmind"})
    only_ours = sorted(d for d in by_day if rows and d >= rows[0]["date"] and d not in set(r["date"] for r in rows))
    between = sorted(r["date"] for r in rows if first and r["date"] > first and r["date"] not in by_day)
    report = {"compared": compared, "same": same, "diffs": diffs, "over": over, "threshold": threshold,
              "added": len(added), "addedFrom": added[0]["d"] if added else None, "addedThrough": added[-1]["d"] if added else None,
              "firstExisting": first, "onlyOurs": only_ours, "onlyFinmindInsideOurRange": between}
    return added, report


def apply_backfill(old_points, added):
    """只把更早的日期接在前面，現有的點原封不動；只留最近 MAX_POINTS 點。"""
    have = set(p.get("d") for p in old_points)
    merged = sorted([p for p in added if p["d"] not in have], key=lambda p: p["d"]) + list(old_points)
    return merged[-fd.MAX_POINTS:]


def print_report(aid, report):
    print("[%s] 重疊區比對 %d 天，完全一致 %d 天，不一致 %d 筆（門檻 %.1f%%，超過門檻 %d 筆）"
          % (aid, report["compared"], report["same"], len(report["diffs"]), report["threshold"], len(report["over"])))
    for r in report["diffs"][:20]:
        print("    %s %s 本站 %s／FinMind %s，差 %.4f%%%s" % (r["d"], r["field"], r["ours"], r["finmind"], r["diffPct"],
                                                          "  ★ 超過門檻" if r["diffPct"] > report["threshold"] else ""))
    if report["onlyOurs"]:
        print("    只有本站有的日期：%s" % "、".join(report["onlyOurs"][:10]))
    if report["onlyFinmindInsideOurRange"]:
        print("    本站區間內只有 FinMind 有的日期（不補，只記下來）：%s" % "、".join(report["onlyFinmindInsideOurRange"][:10]))
    print("    會新增 %d 個更早的日期：%s ～ %s（現有第一天 %s）"
          % (report["added"], report["addedFrom"], report["addedThrough"], report["firstExisting"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="匯率日線歷史一次性回補（只增不改）")
    ap.add_argument("--dry-run", action="store_true", help="只比對與列出，不寫檔")
    ap.add_argument("--threshold", type=float, default=THRESHOLD_PCT, help="重疊區差額超過幾 %% 就停（預設 0.3）")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="往前問幾個曆日（預設 600，約 400 個營業日）")
    args = ap.parse_args(argv)
    with open(os.path.join(fd.DATA_DIR, "assets.json"), encoding="utf-8") as fh:
        assets = [a for a in json.load(fh)["assets"] if a.get("type") == "finmind_fx" and a.get("enabled", True)]
    f = fd.Fetcher(verbose=True)
    f.session.hooks["response"].append(net_policy.response_hook)
    start = (fd.now_tpe() - timedelta(days=args.days)).date().isoformat()
    plans, stop = [], False
    for a in assets:
        old = fd.load_history(a["id"])
        url = "%s?dataset=TaiwanExchangeRate&data_id=%s&start_date=%s" % (fd.FINMIND_URL, a["symbol"], start)
        try:
            net_policy.assert_host_allowed(url)
            rows = fd.fetch_finmind_fx(f, a["symbol"], start)
        except (fd.FetchError, net_policy.HostNotAllowed) as e:
            print("::error::%s 抓不到：%s" % (a["id"], e))
            return 2
        added, report = plan_backfill(old, rows, args.threshold)
        print_report(a["id"], report)
        if report["over"]:
            stop = True
        plans.append((a, old, added))
    print("對外請求：%d" % f.count)
    if stop:
        print("重疊區有超過 %.1f%% 的差額：一個點都沒寫，請先看上面的明細。" % args.threshold)
        return 1
    if args.dry_run:
        print("dry-run：沒有寫檔。")
        return 0
    for a, old, added in plans:
        merged = apply_backfill(old, added)
        fd.save_history(a, merged)
        print("[%s] 寫入 %d 點（%s ～ %s）" % (a["id"], len(merged), merged[0]["d"], merged[-1]["d"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
