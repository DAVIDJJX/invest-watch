#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cleanup_fake_history_points.py — 一次性：清掉「憑空造出來」的歷史點

背景
    2026-09-05 之前，台銀那幾項寫歷史點時用的是 now_tpe()（現在），
    不是資料本身的掛牌日期。台銀週末不掛牌，所以週六抓到的還是週五那一筆，
    卻被寫成一個週六的點，值跟週五一模一樣——走勢圖上就多了一根不存在的 K 線。
    程式已經修好（日期一律取自來源），這一支負責把已經寫進去的假點清掉。

    ⚠ 只處理台銀來源（bot_gold / bot_gold_bar / bot_fx）。
      比特幣週末真的有交易，它的週末點是真的，絕對不能碰。

分成兩類，信心等級完全不同
    甲類：日期落在【週六或週日】的點。
          台銀週末一定不掛牌，這類判斷不會錯 → 可以刪。
    乙類：平日、但值與前一個點完全相同。
          可能是國定假日留下的假點，【也可能是金價那天真的沒動】。
          刪錯就是刪掉真資料 → 只列出來給人看，程式不准自動刪。

用法
    python scripts/cleanup_fake_history_points.py              # 只列清單，不改檔（預設）
    python scripts/cleanup_fake_history_points.py --delete-a   # 真的刪掉甲類
    python scripts/cleanup_fake_history_points.py --delete-b-dates gold_twd:2026-09-07,...
                                                               # 逐筆指定要刪的乙類
"""

import argparse
import json
import os
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data as fd                                      # noqa: E402

# 只有這幾種來源是台銀的，也只有它們適用「週末一定沒有牌價」這條規則
BOT_TYPES = ("bot_gold", "bot_gold_bar", "bot_fx")
WEEKDAY_NAME = ["一", "二", "三", "四", "五", "六", "日"]

# 本專案第一次執行的日期（Phase 1，commit 5715fe2）。
# 這個 bug 是「寫歷史時用現在當日期」，所以它【只可能】產生日期 >= 這一天的點。
# 更早的點全部來自台銀官方自帶日期的走勢表與 CSV，不可能是它造成的——
# 這條界線能把乙類那一堆「值剛好連兩天一樣」的正常資料排除掉，
# 不然人要一筆一筆看十幾筆根本不可能是假點的紀錄。
FIRST_RUN_DATE = "2026-08-31"


def load_assets():
    with open(fd.ASSETS_FILE, encoding="utf-8") as fh:
        cfg = json.load(fh)
    return [a for a in cfg["assets"]
            if a.get("enabled", True) and a["type"] in BOT_TYPES]


def weekday(d):
    return datetime.strptime(d, "%Y-%m-%d").weekday()      # 0=週一 … 6=週日


def scan(asset):
    """回傳 (甲類, 乙類)。每一筆是 (日期, 星期, 值, 前一日日期, 前一日的值)。"""
    points = fd.load_history(asset["id"])
    a_list, b_list = [], []
    for i, p in enumerate(points):
        d = p.get("d")
        if not d:
            continue
        prev = points[i - 1] if i > 0 else None
        row = (d, WEEKDAY_NAME[weekday(d)], p.get("c"),
               (prev or {}).get("d"), (prev or {}).get("c"))
        if weekday(d) >= 5:                    # 5=週六 6=週日
            a_list.append(row)
        elif prev is not None and p.get("c") is not None \
                and p.get("c") == prev.get("c"):
            b_list.append(row)
    return a_list, b_list


def save(asset_id, points):
    """照 fetch_data.dump_history 的格式寫回去，不要把檔案壓成一行。"""
    path = fd.hist_path(asset_id)
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    head_src = {"id": doc.get("id"), "name": doc.get("name"),
                "currency": doc.get("currency"), "unit": doc.get("unit")}
    lines = ["{"]
    for k in ("id", "name", "currency", "unit"):
        lines.append("  %s: %s," % (json.dumps(k, ensure_ascii=False),
                                    json.dumps(head_src[k], ensure_ascii=False)))
    lines.append("  \"count\": %d," % len(points))
    lines.append("  %s: %s," % (json.dumps("updatedAt"),
                                json.dumps(doc.get("updatedAt"))))
    lines.append("  \"points\": [")
    for i, p in enumerate(points):
        tail = "," if i < len(points) - 1 else ""
        lines.append("    " + json.dumps(p, ensure_ascii=False,
                                         separators=(",", ":")) + tail)
    lines.append("  ]")
    lines.append("}")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="清掉憑空造出來的歷史點")
    ap.add_argument("--delete-a", action="store_true",
                    help="真的刪掉甲類（週末的點）。不給就只列清單。")
    ap.add_argument("--delete-b-dates", default="",
                    help="逐筆指定要刪的乙類，格式 id:日期，逗號分隔。"
                         "乙類永遠不會被整批刪，必須一筆一筆指定。")
    args = ap.parse_args()

    want_b = {}
    for item in [x.strip() for x in args.delete_b_dates.split(",") if x.strip()]:
        aid, _, day = item.partition(":")
        want_b.setdefault(aid, set()).add(day)

    assets = load_assets()
    print("=" * 78)
    print("掃描台銀來源的歷史點（%d 項）" % len(assets))
    print("=" * 78)

    total_a = total_b = 0
    plan = {}
    for a in assets:
        a_list, b_list = scan(a)
        total_a += len(a_list)
        total_b += len(b_list)
        plan[a["id"]] = (a_list, b_list)

    print("\n【甲類】日期落在週六／週日 —— 台銀週末一定不掛牌，可以刪")
    print("-" * 78)
    if total_a == 0:
        print("  沒有。")
    for a in assets:
        for d, wd, c, pd_, pc in plan[a["id"]][0]:
            same = "（值與前一筆相同）" if c == pc else "（值與前一筆不同：%s）" % pc
            print("  %-10s %s(週%s)  值 %-12s  前一筆 %s 值 %-12s %s"
                  % (a["id"], d, wd, c, pd_, pc, same))

    print("\n【乙類】平日、但值與前一筆完全相同 —— 可能是假日的假點，")
    print("       也可能是那天價格真的沒動。★ 只列出來，程式不會自動刪。")
    print("-" * 78)
    if total_b == 0:
        print("  沒有。")
    suspect_b = 0
    for a in assets:
        for d, wd, c, pd_, pc in plan[a["id"]][1]:
            if d >= FIRST_RUN_DATE:
                suspect_b += 1
                mark = "★ 在本站開始自行累積之後，有可能是假點，請確認"
            else:
                mark = ("（%s 之前的官方歷史資料，"
                        "不可能是這個 bug 造成的）" % FIRST_RUN_DATE)
            print("  %-10s %s(週%s)  值 %-12s  前一筆 %s 值 %-12s %s"
                  % (a["id"], d, wd, c, pd_, pc, mark))
    if total_b:
        print("")
        print("  乙類共 %d 筆，其中【真的需要你判斷的只有 %d 筆】"
              "（日期 >= %s 的那些）。" % (total_b, suspect_b, FIRST_RUN_DATE))

    print("\n" + "=" * 78)
    print("合計：甲類 %d 筆、乙類 %d 筆" % (total_a, total_b))

    if not args.delete_a and not want_b:
        print("\n這是 dry-run，沒有改任何檔案。")
        print("要刪甲類：--delete-a")
        print("要刪乙類：--delete-b-dates gold_twd:2026-09-07,fx_usd:2026-09-07")
        return 0

    # --- 真的動手刪 --------------------------------------------------------
    removed = 0
    for a in assets:
        aid = a["id"]
        a_list, _ = plan[aid]
        drop = set()
        if args.delete_a:
            drop |= {d for d, _, _, _, _ in a_list}
        drop |= want_b.get(aid, set())
        if not drop:
            continue
        points = fd.load_history(aid)
        kept = [p for p in points if p.get("d") not in drop]
        n = len(points) - len(kept)
        if n:
            save(aid, kept)
            removed += n
            print("  %-10s 刪掉 %d 筆（%s），剩 %d 點"
                  % (aid, n, "、".join(sorted(drop)), len(kept)))
    print("\n總共刪掉 %d 筆。" % removed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
