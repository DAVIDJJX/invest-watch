#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
publish.py — 把這一輪產生的資料提交並推上去，推不上去時安全地重試

雲端（GitHub Actions）和家用電腦都呼叫這一支。競態處理只寫這一份，
不要用 bash 和 PowerShell 各寫一遍——同一套邏輯寫兩遍就會壞兩次。

──────────────────────────────────────────────────────────────────────
為什麼重試不能只用 git reset --soft（這是這支程式存在的主要理由）
──────────────────────────────────────────────────────────────────────
舊做法是：push 被拒 → git fetch → git reset --soft origin/main
          → git add data/ → 重新 commit。

這會【吃掉對方剛推上去的資料】，而且外表完全看不出來：

  * reset --soft 只搬動 HEAD，索引（index）和工作區【完全不動】。
  * 所以 reset 之後，索引裡仍然是「對方推送之前」的舊檔案。
  * 接著 git add data/ 又把工作區那份同樣過期的副本蓋上去。
  * commit 出來的樹 = 對方推送之前的狀態 + 我這一輪的改動
    → 對方剛推上去的分片與歷史檔被【還原回舊版】。

  更糟的是 data/latest.json 是從兩個分片算出來的衍生檔。用舊分片重算，
  等於把對方的新價格洗掉，而檔案本身結構完整、summary 也說「13 項全部成功」，
  沒有任何地方看得出資料被還原了。這正是誠實鐵則禁止的事。

正確的做法（本檔實作的）：

  1. git fetch origin <當前分支>
  2. 把【我這一輪產生的檔案】複製到暫存目錄
  3. git reset --mixed origin/<當前分支>
        --mixed 會把索引對齊遠端；但它【不動工作區】。
  4. git checkout origin/<當前分支> -- data
        這一步不能省。因為 merge_latest.py 讀的是【工作區】，
        不是索引。不把工作區換成遠端的最新內容，重算時就會拿到
        我手上那份過期的對方分片，結果跟舊做法一樣糟。
  5. 把暫存目錄的檔案複製回來（只有我的）
  6. 重跑衍生步驟（--rebuild 指定，見下面）
  7. git add 我的清單 + data/latest.json
  8. commit → push

  第 2、5 步刻意列舉「我自己的檔案」而不是「對方的檔案」：
  對方會寫哪些檔案是【開放集合】（哪天多寫一個就漏了），
  而我這一輪產生了哪些檔案是【封閉集合】——那些檔案就是這支流程自己寫出來的。
  風險方向從「有沒有漏列對方的」變成「有沒有漏列自己的」，後者控制得住。

──────────────────────────────────────────────────────────────────────
分支語意
──────────────────────────────────────────────────────────────────────
這支程式一律對【當前分支】操作，程式裡不會出現寫死的 main。
理由：3-A 要在 feat/split-sources 上手動觸發 Actions，寫死 main 會把
分支的資料推去 main。「必須在 main 上才能跑」的保險放在 update_local.ps1
（那是給排程用的），不放在這裡。

用法：
    python scripts/publish.py --source cloud --rebuild merge,report:close \\
           --message "data: 2026-09-05 close 更新"
    python scripts/publish.py --source local --rebuild merge \\
           --message "data: 2026-09-05 10:05 本機補抓"

--rebuild 指定「重試時要重跑哪些衍生步驟」，只會重跑這一輪自己真的跑過的：
    merge          → python scripts/merge_latest.py
    report:<slot>  → python scripts/report.py --slot <slot>
本機不產報告，所以本機只會傳 merge，絕對不會碰 report.py 或 report-latest.json。
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TPE = timezone(timedelta(hours=8))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
ASSETS_FILE = os.path.join(DATA_DIR, "assets.json")

SOURCES = ("cloud", "local")

# 重試次數與間隔。間隔隨機是為了讓兩邊撞在一起時不要每次都同時重試
#（固定間隔的話兩邊會一起醒來、一起再撞一次）。
MAX_ATTEMPTS = 3
RETRY_SLEEP_RANGE = (5, 15)

# 只給測試用：scripts/test_race_recovery.py 會把它設成 0，
# 否則跑一輪要多等 10～30 秒。正式執行不會設這個環境變數。
_SLEEP_OVERRIDE = os.environ.get("INVESTWATCH_RETRY_SLEEP")


def retry_sleep_seconds():
    if _SLEEP_OVERRIDE is not None:
        return int(_SLEEP_OVERRIDE)
    return random.randint(*RETRY_SLEEP_RANGE)

# 這幾個流程只會寫 data/ 底下的東西。還原時只還原 data/，
# 不要用 git checkout -- . 把整個工作區掃掉——那會消滅使用者正在改的
# js/ 或 css/ 檔案。程式碼若在遠端有更動，reset --mixed 已經讓它進到
# commit 的樹裡了，不需要靠工作區。
RESTORE_SCOPE = "data"


# --------------------------------------------------------------------------
# git 小工具
# --------------------------------------------------------------------------

def git(*args, **kw):
    """跑一個 git 指令。回傳 (結束碼, 輸出)。"""
    check = kw.pop("check", True)
    quiet = kw.pop("quiet", False)
    p = subprocess.run(["git"] + list(args), cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", "replace").strip()
    if not quiet:
        label = " ".join(args)
        print("  $ git %s" % label)
        for line in out.splitlines():
            print("      %s" % line)
    if check and p.returncode != 0:
        raise RuntimeError("git %s 失敗（結束碼 %d）" % (" ".join(args), p.returncode))
    return p.returncode, out


def current_branch():
    _, out = git("rev-parse", "--abbrev-ref", "HEAD", quiet=True)
    return out.strip()


def path_in_tree(ref, path):
    """遠端那棵樹裡有沒有這個路徑。沒有就不要 checkout，否則 git 會報錯中止。"""
    rc, _ = git("cat-file", "-e", "%s:%s" % (ref, path), check=False, quiet=True)
    return rc == 0


def run_python(*args):
    """跑同一個 Python 直譯器底下的另一支腳本。"""
    cmd = [sys.executable] + list(args)
    print("  $ python %s" % " ".join(args))
    p = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", "replace")
    for line in out.splitlines():
        print("      %s" % line)
    if p.returncode != 0:
        raise RuntimeError("python %s 失敗（結束碼 %d）" % (" ".join(args), p.returncode))
    return out


# --------------------------------------------------------------------------
# 我這一輪產生了哪些檔案
# --------------------------------------------------------------------------

def owned_paths(source, rebuild_steps, now=None):
    """列出【我這一輪會寫的檔案】，一律用倉庫相對路徑、正斜線。

    歸屬完全從 data/assets.json 的 owner 欄位算出來，不寫死任何 id——
    以後新增或搬動標的只要改 assets.json，這裡自動跟著對。

    data/latest.json 是衍生檔：兩邊都會在重算後提交它，所以它一定在清單裡。
    """
    now = now or datetime.now(TPE)
    paths = ["data/sources/%s.json" % source]

    with open(ASSETS_FILE, encoding="utf-8") as fh:
        cfg = json.load(fh)
    for a in cfg["assets"]:
        if not a.get("enabled", True):
            continue
        if (a.get("owner") or "cloud") == source:
            paths.append("data/history/%s.json" % a["id"])

    paths.append("data/latest.json")

    # 報告檔只有真的跑了報告那一輪才算是我的
    for step in rebuild_steps:
        if step.startswith("report:"):
            slot = step.split(":", 1)[1]
            day = now.strftime("%Y-%m-%d")
            paths += [
                "data/archive/%s/%s.json" % (day, slot),
                "data/archive/%s/snapshot.json" % day,
                "data/archive/index.json",
                "data/report-latest.json",
            ]
    return sorted(set(paths))


def parse_rebuild(spec):
    """把 --rebuild 的字串拆成步驟清單，順便擋掉打錯字。"""
    steps = [s.strip() for s in (spec or "").split(",") if s.strip()]
    for s in steps:
        if s == "merge":
            continue
        if s.startswith("report:") and s.split(":", 1)[1] in (
                "morning", "midday", "close", "manual"):
            continue
        raise SystemExit("--rebuild 不認得「%s」；只接受 merge 或 report:<slot>" % s)
    return steps


def run_rebuild(steps):
    """重跑衍生步驟。只跑呼叫端指定的，不要自作主張多跑一個。"""
    for s in steps:
        if s == "merge":
            run_python("scripts/merge_latest.py")
        elif s.startswith("report:"):
            run_python("scripts/report.py", "--slot", s.split(":", 1)[1])


# --------------------------------------------------------------------------
# 重試時的還原程序
# --------------------------------------------------------------------------

def stash_mine(paths, tmp_dir):
    """把我這一輪產生的檔案複製到暫存目錄（只複製真的存在的）。"""
    kept = []
    for rel in paths:
        src = os.path.join(ROOT, rel)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(tmp_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        kept.append(rel)
    return kept


def unstash_mine(kept, tmp_dir):
    for rel in kept:
        src = os.path.join(tmp_dir, rel)
        dst = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


def reset_to_remote(ref):
    """把索引和 data/ 工作區整個回到遠端的狀態。

    reset --mixed 對齊索引；checkout 對齊工作區。兩件事都要做：
    少了 reset，commit 會夾帶舊索引內容；
    少了 checkout，重算會讀到我手上那份過期的對方檔案。
    """
    git("reset", "--mixed", ref)
    if path_in_tree(ref, RESTORE_SCOPE):
        git("checkout", ref, "--", RESTORE_SCOPE)


def stage(paths):
    """把我的檔案放進索引。-A 讓「這一輪刪掉的檔案」也能正確記錄。"""
    existing = [p for p in paths if os.path.exists(os.path.join(ROOT, p))]
    if existing:
        git("add", "-A", "--", *existing)
    return existing


def index_has_changes():
    rc, _ = git("diff", "--cached", "--quiet", check=False, quiet=True)
    return rc != 0


def dirty_paths(scope=None):
    args = ["status", "--porcelain"]
    if scope:
        args += ["--", scope]
    _, out = git(*args, quiet=True)
    return [l for l in out.splitlines() if l.strip()]


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="提交並推送這一輪產生的資料")
    ap.add_argument("--source", choices=SOURCES, required=True,
                    help="這一輪是誰在跑：cloud 或 local")
    ap.add_argument("--message", required=True, help="commit 訊息")
    ap.add_argument("--rebuild", default="merge",
                    help="重試時要重跑的衍生步驟，逗號分隔。"
                         "merge 或 report:<slot>。本機只傳 merge。")
    ap.add_argument("--remote", default="origin")
    args = ap.parse_args()

    steps = parse_rebuild(args.rebuild)
    branch = current_branch()
    if branch in ("", "HEAD"):
        print("錯誤：現在不在任何分支上（detached HEAD），不敢提交。")
        return 2
    ref = "%s/%s" % (args.remote, branch)

    print("=" * 66)
    print("publish  身分=%s  分支=%s  重試時重跑=%s"
          % (args.source, branch, "、".join(steps) or "（無）"))
    print("=" * 66)

    tmp_dir = os.path.join(ROOT, ".publish-tmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)

    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                # 隨機間隔：兩邊同時被拒時不要每次都一起重試
                wait = retry_sleep_seconds()
                print("\n--- 第 %d 次嘗試（先等 %d 秒）---" % (attempt, wait))
                time.sleep(wait)

                git("fetch", args.remote, branch)

                # 1) 先把我這一輪的成果搬到安全的地方
                os.makedirs(tmp_dir, exist_ok=True)
                mine = owned_paths(args.source, steps)
                kept = stash_mine(mine, tmp_dir)
                print("  已保留我這一輪的 %d 個檔案" % len(kept))

                # 2) 索引與 data/ 工作區整個回到遠端狀態
                #    （對方的新分片、新歷史、新報告全部就位）
                reset_to_remote(ref)

                # 3) 只把我的放回去
                unstash_mine(kept, tmp_dir)
                shutil.rmtree(tmp_dir, ignore_errors=True)

                # 4) 用「對方的新資料 + 我的新資料」重算衍生檔
                run_rebuild(steps)

            msg = args.message if attempt == 1 else \
                "%s（第 %d 次重試）" % (args.message, attempt - 1)

            mine = owned_paths(args.source, steps)
            stage(mine)
            if not index_has_changes():
                print("\n資料沒有變動，這次不用 commit。")
                return finish_clean(0)

            git("commit", "-m", msg)
            rc, _ = git("push", args.remote, "HEAD:%s" % branch, check=False)
            if rc == 0:
                print("\n推送成功。")
                return finish_clean(0)

            print("\n推送被拒——遠端這期間有新的 commit。")
            if attempt == MAX_ATTEMPTS:
                break

        # --- 三次都失敗 -------------------------------------------------
        # 不能留下「已 commit 但推不上去、又跟遠端分岔」的爛攤子，
        # 也不能留下髒的工作區。這一輪抓到的資料就放棄，下一輪會重抓。
        print("\n連續 %d 次都推不上去，放棄這一輪並把工作區還原乾淨。" % MAX_ATTEMPTS)
        git("fetch", args.remote, branch)
        reset_to_remote(ref)
        remove_untracked(owned_paths(args.source, steps))
        return finish_clean(1, expect_clean=True)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def remove_untracked(paths):
    """刪掉「我這一輪新建、但遠端沒有」的檔案。

    reset + checkout 只能還原遠端有的檔案；我新建的（例如今天第一份報告
    data/archive/<今天>/morning.json）在遠端不存在，還原不到，
    留著就會讓工作區一直是髒的。只刪自己清單裡的，不用 git clean，
    免得誤刪使用者自己放在 data/ 的東西。
    """
    _, out = git("ls-files", "--", "data", quiet=True)
    tracked = set(out.splitlines())
    for rel in paths:
        if rel in tracked:
            continue
        full = os.path.join(ROOT, rel)
        if os.path.isfile(full):
            os.remove(full)
            print("  已刪除未追蹤的殘留：%s" % rel)


def finish_clean(code, expect_clean=False):
    """收尾檢查：工作區的 data/ 必須是乾淨的。

    為什麼要當成硬性條件？update_local.ps1 會在工作區有未提交變動時
    跳過與遠端同步。只要留下一次髒工作區，之後每一次排程都會安靜地跳過同步，
    本機資料就默默凍住，而且 log 上完全看不出異常。
    """
    bad = dirty_paths(scope="data")
    if bad:
        print("\n!! data/ 底下還有未提交的變動，這會讓下一次排程跳過同步：")
        for line in bad:
            print("     %s" % line)
        return 3
    print(". data/ 工作區乾淨。")

    # data/ 以外的髒污是使用者自己正在改的東西，不是這支程式該管的，
    # 只提醒不失敗——這也是還原時只還原 data/ 的原因。
    others = [l for l in dirty_paths() if not l[3:].startswith("data/")]
    if others:
        print(". 註：data/ 以外還有 %d 個未提交的變動（你自己正在改的檔案，"
              "本程式不會碰）：" % len(others))
        for line in others[:5]:
            print("     %s" % line)
    if expect_clean and others:
        print(". （放棄這一輪，但保留了你自己的未提交變動）")
    return code


if __name__ == "__main__":
    sys.exit(main())
