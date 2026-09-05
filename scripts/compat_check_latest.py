#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compat_check_latest.py — 相容性回歸檢查：新的 latest.json 有沒有少掉舊的鍵

為什麼需要？
    分片改版之後 data/latest.json 改由 merge_latest.py 產生，
    但前端 js/ 這一輪【一行都沒改】。只要少掉一個鍵，前端就可能整片空白，
    而且不會報錯——它只會安靜地畫不出東西。

    所以要拿改造前的 latest.json 當基準，逐鍵比對，
    確認「舊的有、新的沒有」這份清單是空的。多出來的鍵沒關係（前端不看就是了）。

比對範圍：
    * 外層每一個鍵
    * summary 底下每一個鍵
    * 每一項資產底下每一個鍵（連 lastGood 這種巢狀的也一起比）

用法：
    git show main:data/latest.json > /tmp/baseline.json
    python scripts/compat_check_latest.py /tmp/baseline.json data/latest.json

結束碼：0=沒有少鍵，1=有少鍵（或檔案讀不到）
"""

import argparse
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# 這些欄位本來就會依當次執行的情況出現或不出現，同一個標的在不同輪次會不一樣。
# 拿單一份快照當基準時，快照剛好有的就會變成「以後每一輪都必須有」，
# 於是一份完全健康的檔案也會被判不合格。誤報多了就沒人看警告了，
# 所以這些單獨列出來、不算缺鍵——但還是印出來讓人看見。
RUN_CONDITIONAL = {
    "carriedOver",       # 這一輪被刻意跳過才有（例如盤中輕量更新不重抓實體金條塊）
    "carriedReason",
    "lastGood",          # 只有在「這次失敗、但以前成功過」時才有
    "lastGood.price", "lastGood.bars", "lastGood.date", "lastGood.fetchedAt",
    "sourceNote",        # 換用備援來源時才會加註
    "quoteTimeError",    # 抓報價時間出問題才有
    "quoteTimeNote",
}


def key_paths(obj, prefix=""):
    """把巢狀結構攤平成一組「鍵的路徑」。

    list 一律只看第一個元素當代表：latest.json 裡的 list（points / spark / bars）
    元素形狀都一樣，全部展開只會讓輸出爆掉。
    """
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = "%s.%s" % (prefix, k) if prefix else k
            out.add(path)
            out |= key_paths(v, path)
    elif isinstance(obj, list) and obj:
        out |= key_paths(obj[0], "%s[]" % prefix)
    return out


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser(description="比對新舊 latest.json 的鍵")
    ap.add_argument("baseline", help="改造前的 latest.json（通常從 git show 取出）")
    ap.add_argument("current", help="新產出的 latest.json")
    args = ap.parse_args()

    try:
        old, new = load(args.baseline), load(args.current)
    except Exception as e:
        print("讀不到檔案：%s" % e)
        return 1

    print("=" * 66)
    print("相容性回歸檢查")
    print("  基準：%s（%d 項資產）" % (args.baseline, len(old.get("assets") or {})))
    print("  新版：%s（%d 項資產）" % (args.current, len(new.get("assets") or {})))
    print("=" * 66)

    # --- 先檢查基準本身健不健康 -------------------------------------------
    # 這個檢查的強度完全取決於基準有多完整。抓失敗的標的在 latest.json 裡只剩
    # 十幾個欄位的錯誤紀錄，價格、走勢點、漲跌全都不在——拿那種標的當基準，
    # 等於根本沒問過那些欄位，卻會印出「合格」。
    # 基準是每半小時被機器人改寫的檔案，隨手挑一個 commit 很可能剛好挑到台銀失敗那次，
    # 所以這裡要擋下來，逼人換一個健康的基準，而不是安靜地給一個沒有意義的通過。
    old_assets_all = old.get("assets") or {}
    sick = sorted(aid for aid, e in old_assets_all.items()
                  if (e or {}).get("status") != "ok")
    print("\n[基準健康度]")
    if sick:
        print("  !! 基準裡有 %d 項不是成功狀態：%s" % (len(sick), "、".join(sick)))
        print("     這些標的在基準裡只剩錯誤紀錄，欄位本來就少一大半，")
        print("     拿它當基準會讓檢查形同虛設（少掉的鍵根本沒被問過）。")
        print("     請換一個 13 項全部成功的 commit 當基準，例如：")
        print("       git log --oneline -20 -- data/latest.json")
        print("       git show <commit>:data/latest.json > baseline.json")
        print("\n結果：無法判定。基準不健康，這次檢查不算數。")
        return 2
    print("  OK 基準的 %d 項全部是成功狀態，欄位齊全，可以當基準" % len(old_assets_all))

    problems = []

    # --- 1. 外層（不含 assets，那個逐項比）---------------------------------
    old_top = {k: v for k, v in old.items() if k != "assets"}
    new_top = {k: v for k, v in new.items() if k != "assets"}
    lost_top = sorted(key_paths(old_top) - key_paths(new_top))
    print("\n[外層欄位]")
    print("  舊有 %d 個鍵，新有 %d 個鍵" % (len(key_paths(old_top)), len(key_paths(new_top))))
    if lost_top:
        problems.append(("外層", lost_top))
        for k in lost_top:
            print("  !! 少了：%s" % k)
    else:
        print("  OK 一個都沒少")
    gained = sorted(key_paths(new_top) - key_paths(old_top))
    if gained:
        print("  （新增：%s）" % "、".join(gained))

    # --- 2. 資產：先看有沒有整項不見 ---------------------------------------
    old_assets = old.get("assets") or {}
    new_assets = new.get("assets") or {}
    lost_assets = sorted(set(old_assets) - set(new_assets))
    print("\n[資產是否整項消失]")
    if lost_assets:
        problems.append(("整項消失", lost_assets))
        print("  !! 新版少了這些標的：%s" % "、".join(lost_assets))
    else:
        print("  OK %d 項全都在" % len(old_assets))
    added_assets = sorted(set(new_assets) - set(old_assets))
    if added_assets:
        print("  （新增標的：%s）" % "、".join(added_assets))

    # --- 3. 資產：逐項比鍵 --------------------------------------------------
    print("\n[每一項資產底下的鍵]")
    per_asset_lost = {}
    per_asset_conditional = {}
    all_gained = set()
    for aid, o in old_assets.items():
        n = new_assets.get(aid)
        if n is None:
            continue
        lost = sorted(key_paths(o) - key_paths(n))
        hard = [k for k in lost if k not in RUN_CONDITIONAL]
        soft = [k for k in lost if k in RUN_CONDITIONAL]
        if hard:
            per_asset_lost[aid] = hard
        if soft:
            per_asset_conditional[aid] = soft
        all_gained |= (key_paths(n) - key_paths(o))
    if per_asset_lost:
        problems.append(("資產欄位", per_asset_lost))
        for aid, lost in sorted(per_asset_lost.items()):
            print("  !! %-10s 少了：%s" % (aid, "、".join(lost)))
    else:
        print("  OK 每一項的鍵都沒少")
    if per_asset_conditional:
        print("  （下面這些是「有時才有」的欄位，不算缺鍵——"
              "例如金條塊只有被跳過那一輪才有 carriedOver）")
        for aid, soft in sorted(per_asset_conditional.items()):
            print("     %-10s %s" % (aid, "、".join(soft)))
    if all_gained:
        print("  （新增：%s）" % "、".join(sorted(all_gained)))

    # --- 結論 --------------------------------------------------------------
    print("\n" + "=" * 66)
    if problems:
        print("結果：不合格。「舊的有、新的沒有」的清單不是空的，前端可能會壞。")
        return 1
    print("結果：合格。「舊的有、新的沒有」的清單是空的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
