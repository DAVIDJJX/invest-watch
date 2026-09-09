#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_schedule.py — 對照 data/schedule.json，驗某一天的排程有沒有準時、報告有沒有準時到

只讀不寫。給一個日期，拉那一天所有「更新市場資料」的 run，算出：

  7-H  每個排定時刻（平日 08:10、08:40 … 17:40，以及四個報告時刻）有沒有對應的
       dispatch；dispatch 的 created_at 與排定時刻差幾秒（門檻 60 秒）。
  7-I  四份報告的 archive 檔案是否存在、產生時間是否在門檻內
       （門檻＝schedule.json 裡 reports 的時間 + 15 分鐘：09:45／11:45／13:50／15:45）。
  多出來的 run（備援 cron、手動 Execute now）另列，不算進 PASS/FAIL。

用法：
    git pull                                   # 先把 archive 拉到最新（這支只讀本機檔案）
    python scripts/verify_schedule.py --date 2026-09-10
    python scripts/verify_schedule.py --date 2026-09-10 --tolerance 60 --grace-minutes 15

需要 gh（GitHub CLI）已登入。結束碼：0 = 7-H 與 7-I 都 PASS（或還沒到時間）；1 = 有 FAIL。
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import schedule_util as su                                   # noqa: E402

TPE = timezone(timedelta(hours=8))
REPO = "DAVIDJJX/invest-watch"
WORKFLOW_FILE = "update-data.yml"
MATCH_WINDOW_MIN = 10          # 排定時刻前後幾分鐘內的 dispatch 才算是它的


def sh(*args):
    p = subprocess.run(list(args), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("指令失敗：%s\n%s" % (" ".join(args), p.stderr.decode("utf-8", "replace")))
    return p.stdout.decode("utf-8", "replace")


def parse_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch_runs(day, repo):
    """拉那一個台北日子的所有 run。GitHub 的 created 篩選用 UTC，台北的一天跨兩個 UTC 日期，
    所以多拉一天再用台北時間過濾。"""
    d0 = (day - timedelta(days=1)).strftime("%Y-%m-%d")
    d1 = day.strftime("%Y-%m-%d")
    out = sh("gh", "api", "repos/%s/actions/workflows/%s/runs?per_page=100&created=%s..%s"
             % (repo, WORKFLOW_FILE, d0, d1),
             "--jq", ".workflow_runs[] | {id:.id, created:.created_at, started:.run_started_at, "
                     "event:.event, conclusion:.conclusion, status:.status}")
    runs = []
    for line in out.strip().splitlines():
        r = json.loads(line)
        r["created_tpe"] = parse_utc(r["created"]).astimezone(TPE)
        if r["created_tpe"].date() != day:
            continue
        r["queue_seconds"] = int((parse_utc(r["started"]) - parse_utc(r["created"])).total_seconds()) \
            if r.get("started") else None
        runs.append(r)
    return sorted(runs, key=lambda r: r["created_tpe"])


def read_mode_slot(run_id):
    """mode / slot 只印在第一步的 log 裡（API 不回傳 workflow_dispatch 的 inputs）。"""
    log = sh("gh", "run", "view", str(run_id), "--log")
    m = re.search(r"mode=(\w+)", log)
    s = re.search(r"slot=(\w+)", log)
    return (m.group(1) if m else "?"), (s.group(1) if s else "?")


def main():
    ap = argparse.ArgumentParser(description="驗某一天的排程有沒有準時")
    ap.add_argument("--date", required=True, help="台北日期，YYYY-MM-DD")
    ap.add_argument("--tolerance", type=int, default=60, help="7-H：dispatch 晚幾秒內算準時（預設 60）")
    ap.add_argument("--grace-minutes", type=int, default=15, help="7-I：報告晚幾分鐘內算準時（預設 15）")
    ap.add_argument("--repo", default=REPO)
    args = ap.parse_args()

    day = datetime.strptime(args.date, "%Y-%m-%d").date()
    now = datetime.now(TPE)
    sch = su.load_schedule()

    # 本機 archive 是不是最新的：只提醒，不動任何東西
    try:
        subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=ROOT, capture_output=True)
        behind = sh("git", "-C", ROOT, "rev-list", "--count", "HEAD..origin/main").strip()
        if behind != "0":
            print("⚠ 本機落後 origin/main %s 個 commit，7-I 讀的是本機 archive——請先 git pull 再跑。" % behind)
    except Exception:
        pass

    # --- 排定時刻 -----------------------------------------------------------
    expected = su.expected_times("cloud", "light", day, sch)          # full ∪ light
    report_times = {slot: su._at(day, hhmm) for slot, hhmm in (sch.get("reports") or {}).items()}
    time_slot = {t: slot for slot, t in report_times.items()}        # 排定時刻 → 報告 slot
    full_days = ((sch.get("cloud") or {}).get("full") or {}).get("days")
    reports_today = su.day_matches(full_days, day) if report_times else False

    # --- 實際的 run ---------------------------------------------------------
    runs = fetch_runs(day, args.repo)
    dispatches = [r for r in runs if r["event"] == "workflow_dispatch"]
    for r in dispatches:
        r["mode"], r["slot"] = read_mode_slot(r["id"])
    others = [r for r in runs if r["event"] != "workflow_dispatch"]

    # --- 7-H：每個排定時刻配一個 dispatch -----------------------------------
    print("=" * 96)
    print("排程驗證  %s（%s）  現在 %s" % (day, "平日" if reports_today else "週末", now.strftime("%H:%M")))
    print("=" * 96)
    print("\n【7-H】排定時刻 vs dispatch（門檻 %d 秒）" % args.tolerance)
    print("%-6s %-11s %-8s %-9s %8s %8s  %s" % ("排定", "slot", "dispatch", "mode/slot", "晚(秒)", "排隊(秒)", "結果"))
    print("-" * 96)
    used = set()
    h_fail = h_total = 0
    for t in expected:
        want_slot = time_slot.get(t, "light")
        if t > now:
            print("%-6s %-11s %-8s %-9s %8s %8s  %s" % (t.strftime("%H:%M"), want_slot, "—", "—", "—", "—", "尚未到"))
            continue
        h_total += 1
        cands = [r for r in dispatches if r["id"] not in used
                 and abs((r["created_tpe"] - t).total_seconds()) <= MATCH_WINDOW_MIN * 60
                 and r["slot"] == want_slot]
        if not cands:
            h_fail += 1
            print("%-6s %-11s %-8s %-9s %8s %8s  %s" % (t.strftime("%H:%M"), want_slot, "沒有", "—", "—", "—", "★ FAIL 沒有對應的 dispatch"))
            continue
        r = min(cands, key=lambda x: abs((x["created_tpe"] - t).total_seconds()))
        used.add(r["id"])
        late = int((r["created_tpe"] - t).total_seconds())
        ok = 0 <= late < args.tolerance and r["conclusion"] == "success"
        if not ok:
            h_fail += 1
        why = "" if ok else ("★ FAIL " + ("run 沒成功（%s）" % r["conclusion"] if r["conclusion"] != "success" else "晚了 %d 秒" % late))
        print("%-6s %-11s %-8s %-9s %8d %8s  %s" % (
            t.strftime("%H:%M"), want_slot, r["created_tpe"].strftime("%H:%M:%S"),
            "%s/%s" % (r["mode"], r["slot"]), late,
            r["queue_seconds"] if r["queue_seconds"] is not None else "—", why or "PASS"))

    # --- 7-I：四份報告 --------------------------------------------------------
    print("\n【7-I】報告是否準時到（門檻＝排定 + %d 分）" % args.grace_minutes)
    print("%-11s %-6s %-6s %-19s  %s" % ("slot", "排定", "門檻", "產生於", "結果"))
    print("-" * 96)
    i_fail = i_total = 0
    if not reports_today:
        print("  今天不產報告（週末）。")
    for slot, t in sorted(report_times.items(), key=lambda kv: kv[1]):
        if not reports_today:
            break
        deadline = t + timedelta(minutes=args.grace_minutes)
        path = os.path.join(ROOT, "data", "archive", day.strftime("%Y-%m-%d"), "%s.json" % slot)
        if deadline > now and not os.path.exists(path):
            print("%-11s %-6s %-6s %-19s  %s" % (slot, t.strftime("%H:%M"), deadline.strftime("%H:%M"), "—", "尚未到"))
            continue
        i_total += 1
        if not os.path.exists(path):
            i_fail += 1
            print("%-11s %-6s %-6s %-19s  %s" % (slot, t.strftime("%H:%M"), deadline.strftime("%H:%M"), "不存在", "★ FAIL 沒有這份報告"))
            continue
        with open(path, encoding="utf-8") as fh:
            rep = json.load(fh)
        gen = datetime.fromisoformat(rep["generatedAt"])
        pb = rep.get("producedBy") or {}
        ok = t <= gen <= deadline
        if not ok:
            i_fail += 1
        note = "PASS" if ok else ("★ FAIL " + ("早於排定時刻（手動觸發？）" if gen < t else "晚了 %d 分" % int((gen - deadline).total_seconds() // 60)))
        print("%-11s %-6s %-6s %-19s  %s  （runId %s）" % (
            slot, t.strftime("%H:%M"), deadline.strftime("%H:%M"), gen.strftime("%m-%d %H:%M:%S"), note, pb.get("runId")))

    # --- 多出來的 run --------------------------------------------------------
    extra = [r for r in dispatches if r["id"] not in used] + others
    print("\n【另列】不對應任何排定時刻的 run（備援 cron、手動觸發）")
    if not extra:
        print("  沒有。")
    for r in sorted(extra, key=lambda x: x["created_tpe"]):
        print("  %s  %-18s %-16s %-8s runId %s" % (
            r["created_tpe"].strftime("%H:%M:%S"), r["event"],
            ("%s/%s" % (r["mode"], r["slot"])) if r["event"] == "workflow_dispatch" else "light/light（備援）",
            r["conclusion"], r["id"]))

    # --- 結論 ----------------------------------------------------------------
    print("\n" + "=" * 96)
    print("7-H：%s（%d 個排定時刻已到，%d 個不合格）" % ("PASS" if h_fail == 0 else "FAIL", h_total, h_fail))
    print("7-I：%s（%d 份報告門檻已到，%d 份不合格）" % ("PASS" if i_fail == 0 else "FAIL", i_total, i_fail))
    return 0 if (h_fail == 0 and i_fail == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
