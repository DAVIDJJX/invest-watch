#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_to_shards.py — 一次性遷移：把現有的 data/latest.json 拆成兩個分片

為什麼需要這支？
    改成分片架構之後，data/latest.json 由 merge_latest.py 從
    data/sources/cloud.json 與 data/sources/local.json 算出來。
    但是**第一次部署時這兩個分片都還不存在**——
    雲端第一次跑只會產生 cloud.json，這時 merge 只看得到 8 項，
    黃金與匯率那 5 張卡片會整批從網站上消失（比顯示「更新失敗」更糟）。

    所以在切換前先跑這一支，把現有 latest.json 依 owner 拆成兩個分片，
    讓兩邊從第一天起就都有東西可以合併。

    跑完就不用再跑了。之後 cloud.json / local.json 各自由對應的排程維護。

保留哪些東西？
    每一項現有的 status / price / lastGood 等欄位原樣搬過去，
    再補上分片格式需要的三個時間欄位：
      lastAttemptAt ← 該項的 fetchedAt
      lastSuccessAt ← status=ok 就用 fetchedAt；否則用 lastGood.fetchedAt
                      （兩者都沒有就留 None，merge 會判成 error 而不是過期）
      source        ← 依 assets.json 的 owner

用法：
    python scripts/migrate_to_shards.py            # 實際寫檔
    python scripts/migrate_to_shards.py --dry-run  # 只印出會怎麼拆，不寫檔
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TPE = timezone(timedelta(hours=8))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
ASSETS_FILE = os.path.join(DATA_DIR, "assets.json")
LATEST_FILE = os.path.join(DATA_DIR, "latest.json")
SOURCES_DIR = os.path.join(DATA_DIR, "sources")

SLOT_LABEL = {
    "morning": "晨報（了解今天狀況）",
    "midday": "午盤（盤中整理）",
    "close": "收盤（檢討與分析）",
    "manual": "手動更新",
}


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def pick_last_success(entry):
    """從既有欄位盡量還原「最後一次真的成功」的時間。

    抓成功的項目：fetchedAt 就是成功時間。
    抓失敗的項目：error_quote 會把上次成功的資訊放進 lastGood，用它的 fetchedAt。
    兩者都沒有 → None，代表從來沒成功過，merge 會判成 error（而不是「過期」），
    這是誠實的做法：我們確實不知道它上次何時成功。
    """
    if entry.get("status") == "ok":
        return entry.get("fetchedAt")
    lg = entry.get("lastGood") or {}
    return lg.get("fetchedAt")


def main():
    ap = argparse.ArgumentParser(description="把 latest.json 拆成兩個分片（一次性）")
    ap.add_argument("--dry-run", action="store_true", help="只印出結果，不寫檔")
    args = ap.parse_args()

    if not os.path.exists(LATEST_FILE):
        print("找不到 %s，沒有東西可以遷移。" % LATEST_FILE)
        print("這是全新的部署 → 直接跑 fetch_data.py 產生分片即可，不需要遷移。")
        return 1

    cfg = read_json(ASSETS_FILE)
    latest = read_json(LATEST_FILE)
    assets_cfg = [a for a in cfg["assets"] if a.get("enabled", True)]
    owner_map = {a["id"]: (a.get("owner") or "cloud") for a in assets_cfg}

    src = latest.get("assets") or {}
    now = datetime.now(TPE)

    shards = {"cloud": {}, "local": {}}
    orphans = []          # latest.json 有、但 assets.json 已經沒有的（例如被移除的標的）
    missing = []          # assets.json 有、但 latest.json 沒有的（例如剛新增的標的）

    for aid, entry in src.items():
        owner = owner_map.get(aid)
        if owner is None:
            orphans.append(aid)
            continue
        e = dict(entry)
        e["source"] = owner
        e["lastAttemptAt"] = e.get("fetchedAt")
        e["lastSuccessAt"] = pick_last_success(e)
        shards[owner][aid] = e

    for aid, owner in owner_map.items():
        if aid not in src:
            missing.append((aid, owner))

    print("=" * 62)
    print("遷移來源：%s（更新於 %s）" % (LATEST_FILE, latest.get("updatedAtText")))
    print("=" * 62)
    for s in ("cloud", "local"):
        print("\n[%s] %d 項" % (s, len(shards[s])))
        for aid, e in shards[s].items():
            print("   %-10s status=%-5s lastSuccessAt=%s"
                  % (aid, e.get("status"), e.get("lastSuccessAt") or "（從未成功）"))
    if orphans:
        print("\n! latest.json 有但 assets.json 已無的標的（不遷移）：%s"
              % "、".join(orphans))
    if missing:
        print("\n! assets.json 有但 latest.json 還沒有的標的（分片裡會缺，"
              "第一次抓取後就會補上）：")
        for aid, owner in missing:
            print("   %-10s owner=%s" % (aid, owner))

    if args.dry_run:
        print("\n--dry-run：沒有寫入任何檔案。")
        return 0

    os.makedirs(SOURCES_DIR, exist_ok=True)
    slot = latest.get("slot") or "manual"
    for s in ("cloud", "local"):
        payload = {
            "source": s,
            # runAt 沿用 latest.json 的更新時間，不是「現在」——
            # 這份資料本來就是那個時間抓的，寫成現在會讓 freshness 誤判成很新。
            "runAt": latest.get("updatedAt") or now.isoformat(timespec="seconds"),
            "runAtText": latest.get("updatedAtText"),
            "timezone": "Asia/Taipei (UTC+8)",
            "slot": slot,
            "slotLabel": SLOT_LABEL.get(slot, slot),
            "mode": latest.get("mode") or "full",
            "requests": 0,
            "count": len(shards[s]),
            "migratedFrom": "data/latest.json",
            "assets": shards[s],
        }
        path = os.path.join(SOURCES_DIR, "%s.json" % s)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        print("\n已寫出 %s（%d 項）" % (path, len(shards[s])))

    print("\n遷移完成。之後這兩個分片各自由對應的排程維護，這支不用再跑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
