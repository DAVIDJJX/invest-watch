#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_latest.py — 把兩邊的分片合成前端要看的 data/latest.json

為什麼需要這一支？
    以前雲端和家用電腦都直接整檔覆寫 data/latest.json，最後寫的人贏。
    雲端固定比本機晚 12 分鐘，所以本機 20:00 抓到的台銀黃金與匯率，
    20:07 就被雲端「抓不到台銀」的錯誤蓋掉（2026-09-03 實際發生過）。

    現在兩邊各自只寫自己的分片 data/sources/<owner>.json，
    latest.json 由這一支從兩個分片算出來，只有一個寫入者，不會再互相覆蓋。

輸出的 latest.json：
    * 外層欄位跟改造前完全一樣（updatedAt / updatedAtText / timezone /
      slot / slotLabel / mode / summary / requests / assets），前端一行都不用改。
    * 每一項【額外】加 freshness / lastDueAt / nextDueAt 三個欄位，
      原有欄位一個都不動。
    * 外層【額外】加 sources 區塊，記錄兩邊各自的狀況。

分片缺席或損壞時的原則（很重要）：
    標的絕對不可以從 latest.json 消失。少一項前端就整張卡片不見，
    使用者只會覺得「東西不見了」，那比明白寫著「更新失敗」更糟。
    所以分片不見或壞掉時，該 owner 的標的一律補上 status="error" 的紀錄。

    而且分片壞掉時這支【還是要正常產出 latest.json 並回結束碼 0】。
    如果這裡直接失敗，workflow 後面的 commit 步驟就不會跑，
    網站連一份可看的 latest.json 都拿不到。壞掉的事實記在 sources 裡，
    再由 watchdog 去開 Issue。

用法：
    python scripts/merge_latest.py
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedule_util as su                                    # noqa: E402

TPE = timezone(timedelta(hours=8))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
ASSETS_FILE = os.path.join(DATA_DIR, "assets.json")
LATEST_FILE = os.path.join(DATA_DIR, "latest.json")
SOURCES_DIR = os.path.join(DATA_DIR, "sources")

SOURCES = ("cloud", "local")

SLOT_LABEL = {
    "morning": "晨報（了解今天狀況）",
    "midday": "午盤（盤中整理）",
    "close": "收盤（檢討與分析）",
    "manual": "手動更新",
}

SOURCE_LABEL = {"cloud": "雲端排程", "local": "家用電腦"}


def now_tpe():
    return datetime.now(TPE)


def iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


# --------------------------------------------------------------------------
# 讀分片
# --------------------------------------------------------------------------

def load_shard(source):
    """讀一個分片，回傳 (資料, 狀態)。

    狀態有三種：
      "ok"       讀到了，格式正常
      "missing"  檔案不存在（例如那一邊從來沒跑過，或是被誤刪）
      "corrupt"  檔案在但 JSON 解不開，或解出來不是預期的結構

    這裡刻意不丟例外：分片壞掉不能讓整支程式停擺，
    否則 workflow 後面的 commit 不會跑，網站就完全拿不到資料。
    """
    path = os.path.join(SOURCES_DIR, "%s.json" % source)
    if not os.path.exists(path):
        return None, "missing"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None, "corrupt"
    if not isinstance(data, dict) or not isinstance(data.get("assets"), dict):
        return None, "corrupt"
    # runAt 讀不懂也算損毀。fetch_data.py 的 write_shard 一定會寫這一欄，
    # 缺了或壞了就代表檔案不完整。這一條很重要：如果放它過關，下面挑「較新的分片」
    # 時會找不到時間、退回用「現在」當 updatedAt，網站就會在當下的時間戳底下
    # 顯示幾小時前的舊價格，而且旁邊還寫著「13 項全部成功」——正好是誠實鐵則禁止的事。
    if su.parse_iso(data.get("runAt")) is None:
        return None, "corrupt"
    return data, "ok"


def safe_int(v):
    """把分片裡的 requests 轉成整數。轉不動就當 0，不要為了一個統計欄位讓整支掛掉。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def missing_reason(source, status):
    if status == "corrupt":
        return "%s的資料檔損毀（data/sources/%s.json 解不開）" % (
            SOURCE_LABEL.get(source, source), source)
    return "尚未收到%s的資料（data/sources/%s.json 不存在）" % (
        SOURCE_LABEL.get(source, source), source)


def stub_quote(asset, message, when):
    """分片拿不到這一項時的替代紀錄。

    欄位形狀刻意跟 fetch_data.py 的 error_quote 一致——前端已經會處理那個形狀了，
    這裡多發明一種格式只會多一個壞掉的可能。
    """
    return {
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
        "errorAt": iso(when),
        "fetchedAt": iso(when),
        "source": asset.get("owner") or "cloud",
        "lastAttemptAt": None,
        "lastSuccessAt": None,
    }


# --------------------------------------------------------------------------
# 合併
# --------------------------------------------------------------------------

def merge(now=None, schedule=None, warn=None):
    """把兩個分片合成 latest.json 的內容。回傳 (latest, warnings)。"""
    now = now or now_tpe()
    warnings = []

    def add_warning(msg):
        warnings.append(msg)
        if warn:
            warn(msg)

    with open(ASSETS_FILE, encoding="utf-8") as fh:
        cfg = json.load(fh)
    enabled = [a for a in cfg["assets"] if a.get("enabled", True)]
    sch = schedule if schedule is not None else su.load_schedule()

    # 同一個 id 出現兩次（複製貼上新增標的時忘了改 id）只保留第一筆。
    # 不去重的話 assets_out 會被後面那筆覆蓋，但成功/失敗的計數會加兩次，
    # 結果就是「合計 13 項、成功 14」這種自相矛盾的摘要。
    seen_ids, deduped = set(), []
    for a in enabled:
        if a["id"] in seen_ids:
            add_warning("data/assets.json 裡的 %s 出現不只一次，只採用第一筆。" % a["id"])
            continue
        seen_ids.add(a["id"])
        deduped.append(a)
    enabled = deduped

    # 排程漏掉整個 owner 的區塊 → 那一邊算不出任何排定點，
    # 依規格會全部判成 fresh。資料再舊都說是新的，這一定要講出來。
    for s in SOURCES:
        if not su.has_schedule(s, sch):
            add_warning("data/schedule.json 裡沒有 %s 的排程，"
                        "它負責的標的無法判斷是否過期，一律標成失敗。" % s)

    shards, shard_status = {}, {}
    for s in SOURCES:
        shards[s], shard_status[s] = load_shard(s)
        if shard_status[s] == "corrupt":
            add_warning("data/sources/%s.json 解不開，%s負責的標的這一輪全部標成失敗。"
                        % (s, SOURCE_LABEL.get(s, s)))
        elif shard_status[s] == "missing":
            add_warning("data/sources/%s.json 不存在，%s負責的標的這一輪全部標成失敗。"
                        % (s, SOURCE_LABEL.get(s, s)))

    # --- 逐項合併。順序照 assets.json，前端的卡片順序才不會跳動 -------------
    assets_out = {}
    owner_of = {}
    ok_ids, err_ids = [], []
    per_source_counts = {s: {"ok": 0, "error": 0} for s in SOURCES}

    for a in enabled:
        aid = a["id"]
        owner = a.get("owner") or "cloud"

        if owner not in SOURCES:
            # assets.json 的 owner 打錯字（例如寫成 Cloud）。這種設定錯誤不能讓
            # 整支程式掛掉——掛掉的話 latest.json 不會產出，網站上「所有」卡片都不見，
            # 只是為了一個標的的設定筆誤。標成失敗、把原因寫清楚，其他 12 項照常。
            entry = stub_quote(
                a, "data/assets.json 的 owner 寫成「%s」，只接受 cloud 或 local" % owner,
                now)
            add_warning("%s 的 owner 是「%s」，不是 cloud 或 local，先標成失敗。"
                        % (aid, owner))
        else:
            shard = shards.get(owner)
            if shard is None:
                entry = stub_quote(a, missing_reason(owner, shard_status.get(owner)), now)
            else:
                src = shard["assets"].get(aid)
                if src is None:
                    # 分片讀得到，但裡面沒有這一項：通常是剛在 assets.json 新增、
                    # 那一邊還沒跑過。一樣要出現在 latest.json，只是標成失敗。
                    entry = stub_quote(
                        a, "%s的資料檔裡沒有這一項（可能是剛新增，還沒抓過）"
                           % SOURCE_LABEL.get(owner, owner), now)
                    add_warning("%s 不在 data/sources/%s.json 裡，先標成失敗。"
                                % (aid, owner))
                elif not isinstance(src, dict):
                    # 分片本身解得開，但這一項的內容不是物件（半截寫入、手動改壞）。
                    # 只讓這一項壞掉，不要拖垮其他 12 項。
                    entry = stub_quote(
                        a, "%s的資料檔裡這一項格式不對（不是一筆報價）"
                           % SOURCE_LABEL.get(owner, owner), now)
                    add_warning("data/sources/%s.json 裡的 %s 格式不對，先標成失敗。"
                                % (owner, aid))
                else:
                    entry = dict(src)

        # --- 補上三個新欄位。原有欄位一個都不動 ---------------------------
        if not su.cadence_is_valid(a):
            add_warning("%s 的 cadence 寫成「%s」，只接受 full 或 light，"
                        "先照 %s 的預設排程判斷。"
                        % (aid, a.get("cadence"), owner))
        cadence = su.cadence_of(a, sch)
        verdict, last_due, next_due = su.freshness(
            entry.get("lastSuccessAt"), now, owner, cadence, sch)
        # 規格說「lastDue 不存在就算 fresh」，指的是「還沒有任何排定時間過去」。
        # 但如果整個 owner 的排程區塊都不見了，lastDue 也會是 None——
        # 那是設定壞掉，不是資料還新。這種情況一律標成 error，
        # 不能因為設定漏了一段就把八個月前的價格說成最新。
        if not su.has_schedule(owner, sch) and verdict == "fresh":
            verdict = "error"
        entry["freshness"] = verdict
        entry["lastDueAt"] = su.iso(last_due)
        entry["nextDueAt"] = su.iso(next_due)

        assets_out[aid] = entry
        owner_of[aid] = owner

    # 統計一律【從 assets_out 回推】，不要邊跑迴圈邊累加。
    # 迴圈是「每一筆設定」跑一次，assets_out 是「每一個 id」一筆，
    # 兩者只要對不上（例如 id 重複），就會出現「合計 13 項、成功 14」這種
    # 自己打自己臉的摘要。從最終結果回推就不可能不一致。
    for aid, entry in assets_out.items():
        good = entry.get("status") == "ok"
        (ok_ids if good else err_ids).append(aid)
        # owner 打錯字的標的不屬於任何一邊，不計入 sources 統計，但一定要進 summary，
        # 否則「總共幾項」會跟前端看到的卡片數對不上。
        bucket = per_source_counts.get(owner_of.get(aid))
        if bucket:
            bucket["ok" if good else "error"] += 1

    # --- 分片裡有、assets.json 沒有的孤兒標的：跳過 -------------------------
    known = {a["id"] for a in enabled}
    for s in SOURCES:
        if shards.get(s) is None:
            continue
        orphans = sorted(set(shards[s]["assets"]) - known)
        if orphans:
            print(". data/sources/%s.json 裡有 assets.json 已經沒有的標的，略過：%s"
                  % (s, "、".join(orphans)))

    # --- 外層欄位 ----------------------------------------------------------
    # updatedAt 取兩邊 runAt 的較新者；slot / mode 也整組取自那一邊，
    # 不能一個欄位取這邊、另一個取那邊，否則會出現「時間是 15:17 但時段寫早報」。
    live = [(su.parse_iso((shards[s] or {}).get("runAt")), s)
            for s in SOURCES if shards.get(s)]
    live = [(t, s) for t, s in live if t]
    if live:
        newest_at, newest_src = max(live, key=lambda x: x[0])
        head = shards[newest_src]
    else:
        # 兩邊都不見或都壞掉。還是要產出一份 latest.json，
        # 讓前端有東西可讀、看得到 13 張標著失敗的卡片。
        newest_at, head = now, {}
        add_warning("兩個分片都讀不到，latest.json 用現在時間產出，13 項全部標成失敗。")

    slot = head.get("slot") or "manual"
    latest = {
        "updatedAt": iso(newest_at),
        "updatedAtText": newest_at.strftime("%Y-%m-%d %H:%M"),
        "timezone": "Asia/Taipei (UTC+8)",
        "slot": slot,
        "slotLabel": head.get("slotLabel") or SLOT_LABEL.get(slot, slot),
        "mode": head.get("mode") or "full",
        "summary": {
            "total": len(assets_out),
            "ok": len(ok_ids),
            "error": len(err_ids),
            "errorIds": err_ids,
        },
        "requests": sum(safe_int((shards[s] or {}).get("requests")) for s in SOURCES),
        "assets": assets_out,
    }

    # --- sources：兩邊各自的狀況，給 watchdog 與之後的前端用 ---------------
    sources_out = {}
    for s in SOURCES:
        shard = shards.get(s)
        run_at = su.parse_iso((shard or {}).get("runAt"))
        age = int((now - run_at).total_seconds() // 60) if run_at else None
        # nextDueAt 用這個 owner 的預設 cadence——問的是「這一邊下次該跑」，
        # 不是某個標的下次該更新。
        nxt = su.next_expected(s, su.default_cadence(s, sch), now, sch)
        sources_out[s] = {
            "runAt": iso(run_at),
            "slot": (shard or {}).get("slot"),
            "mode": (shard or {}).get("mode"),
            "ok": per_source_counts[s]["ok"],
            "error": per_source_counts[s]["error"],
            "requests": safe_int((shard or {}).get("requests")),
            "ageMinutes": age,
            "nextDueAt": su.iso(nxt),
            "status": shard_status.get(s, "missing"),
        }
    latest["sources"] = sources_out

    return latest, warnings


def main():
    ap = argparse.ArgumentParser(description="把兩個分片合成 data/latest.json")
    ap.add_argument("--dry-run", action="store_true", help="只印出結果，不寫檔")
    args = ap.parse_args()

    now = now_tpe()
    # ::warning:: 是 GitHub Actions 的標註格式，會在 workflow 摘要頁亮黃燈。
    # 本機跑的時候就只是一行普通訊息，不影響。
    latest, warnings = merge(now=now, warn=lambda m: print("::warning::%s" % m))

    print("=" * 62)
    print("合併 data/latest.json  台北時間 %s" % now.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 62)
    for s in SOURCES:
        info = latest["sources"][s]
        print("  [%-5s] %-7s runAt=%s  ok=%d error=%d  age=%s 分  下次=%s"
              % (s, info["status"], info["runAt"] or "—",
                 info["ok"], info["error"],
                 info["ageMinutes"] if info["ageMinutes"] is not None else "—",
                 info["nextDueAt"] or "—"))

    counts = {}
    for e in latest["assets"].values():
        counts[e["freshness"]] = counts.get(e["freshness"], 0) + 1
    print("\n  合計 %d 項：成功 %d / 失敗 %d" % (
        latest["summary"]["total"], latest["summary"]["ok"], latest["summary"]["error"]))
    print("  新舊判定：%s" % "、".join("%s %d" % (k, counts[k]) for k in sorted(counts)))
    if latest["summary"]["errorIds"]:
        print("  失敗標的：%s" % "、".join(latest["summary"]["errorIds"]))

    if args.dry_run:
        print("\n--dry-run：沒有寫入任何檔案。")
        return 0

    with open(LATEST_FILE, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(latest, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print("\n. 已寫出 data/latest.json")

    # 分片壞掉也回 0：這一支的責任是「不管怎樣都要生出一份能看的 latest.json」，
    # 回非零會讓 workflow 後面的 commit 步驟不跑，網站反而什麼都拿不到。
    if warnings:
        print(". 有 %d 則警告（已標成 ::warning::），但 latest.json 已正常產出。"
              % len(warnings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
