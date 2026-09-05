#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_race_recovery.py — 競態實測：兩邊同時推，誰的資料都不能被吃掉

跑法：
    python scripts/test_race_recovery.py

這支【不會碰 GitHub】。它在暫存目錄裡自己建一個 bare repo 當「遠端」，
再 clone 兩份當作「雲端那台」和「家用電腦那台」。git 的語意完全一樣
（一樣會被拒、一樣要重試），但不會在公開倉庫留下垃圾 commit。

它在驗什麼？
    舊做法 push 被拒之後用 git reset --soft 重新 commit。reset --soft 不動
    索引和工作區，所以重新 commit 出來的樹會把對方剛推上去的分片與歷史檔
    【還原回舊版】，而且檔案結構完整、summary 也說「13 項全部成功」，
    完全看不出資料被洗掉了。這支測試就是要證明新的流程不會這樣。

情境：
    劇本 A（3-B / 3-C / 3-H / 3-I）
      1. 雲端那台：更新自己的 8 項 + 產生一份今天的報告 → 推送成功
      2. 家用電腦：更新自己的 5 項 → 推送被拒 → 走重試流程
      3. 檢查最終的遠端內容
    劇本 B（3-J）
      遠端裝一個永遠拒絕的 pre-receive 掛勾，讓三次重試全部失敗，
      檢查有沒有留下爛攤子。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TPE = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = datetime.now(TPE).strftime("%Y-%m-%d")

CLOUD_IDS = None      # 從 assets.json 算出來，不寫死
LOCAL_IDS = None

_fails = []


def say(msg=""):
    print(msg)


def check(label, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    say("  [%s] %s%s" % (mark, label, ("  — " + detail) if detail else ""))
    if not ok:
        _fails.append(label)
    return ok


def run(args, cwd, check_rc=True, env=None):
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    if env:
        e.update(env)
    p = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, env=e)
    out = p.stdout.decode("utf-8", "replace")
    if check_rc and p.returncode != 0:
        say(out)
        raise RuntimeError("指令失敗（%d）：%s" % (p.returncode, " ".join(args)))
    return p.returncode, out


def git(cwd, *args, **kw):
    return run(["git"] + list(args), cwd, **kw)


def py(cwd, *args, **kw):
    return run([sys.executable] + list(args), cwd, **kw)


def configure_identity(repo):
    """測試用的 clone 沒有 git 身分（真倉庫是設在 local config 裡，clone 不會帶過來）。
    這裡設一組只屬於這個暫存 clone 的假身分，不會影響你真正的 git 設定。"""
    git(repo, "config", "user.name", "InvestWatch 測試")
    git(repo, "config", "user.email", "test@example.invalid")


def read(cwd, rel):
    with open(os.path.join(cwd, rel), encoding="utf-8") as fh:
        return json.load(fh)


def write(cwd, rel, obj):
    path = os.path.join(cwd, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


# --------------------------------------------------------------------------
# 佈景：一個 bare repo 當遠端，兩個 clone 當兩台機器
# --------------------------------------------------------------------------

def build_world(tmp):
    os.makedirs(tmp, exist_ok=True)
    origin = os.path.join(tmp, "origin.git")
    seed = os.path.join(tmp, "seed")

    git(tmp, "init", "--quiet", "--bare", "-b", "main", origin)
    git(tmp, "clone", "--quiet", ROOT.replace("\\", "/"), seed)

    # 用【目前工作區】的程式與資料當種子，而不是最後一次 commit——
    # 這樣測的一定是我現在手上這一版，不會測到舊的。
    for sub in ("scripts", "data"):
        dst = os.path.join(seed, sub)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(os.path.join(ROOT, sub), dst,
                        ignore=shutil.ignore_patterns("__pycache__", "*.log"))

    configure_identity(seed)
    git(seed, "checkout", "--quiet", "-B", "main")
    git(seed, "remote", "set-url", "origin", origin.replace("\\", "/"))
    git(seed, "add", "-A")
    # --allow-empty：工作區已經全部 commit 過時，複製過來的內容跟 HEAD 一樣，
    # 沒有 --allow-empty 的話 git 會以「沒有東西可以提交」失敗。
    git(seed, "commit", "--quiet", "--allow-empty", "-m", "測試用種子")
    git(seed, "push", "--quiet", "-u", "origin", "main")

    cloudside = os.path.join(tmp, "cloudside")
    localside = os.path.join(tmp, "localside")
    git(tmp, "clone", "--quiet", origin.replace("\\", "/"), cloudside)
    git(tmp, "clone", "--quiet", origin.replace("\\", "/"), localside)
    configure_identity(cloudside)
    configure_identity(localside)
    return origin, cloudside, localside


def load_owner_ids():
    global CLOUD_IDS, LOCAL_IDS
    with open(os.path.join(ROOT, "data", "assets.json"), encoding="utf-8") as fh:
        assets = [a for a in json.load(fh)["assets"] if a.get("enabled", True)]
    CLOUD_IDS = [a["id"] for a in assets if (a.get("owner") or "cloud") == "cloud"]
    LOCAL_IDS = [a["id"] for a in assets if a.get("owner") == "local"]


def bump_side(work, source, ids, delta, stamp):
    """模擬那一台機器剛抓完資料：改自己的分片與自己的歷史檔。"""
    shard_rel = "data/sources/%s.json" % source
    shard = read(work, shard_rel)
    shard["runAt"] = stamp
    shard["runAtText"] = stamp[:16].replace("T", " ")
    for aid in ids:
        e = shard["assets"][aid]
        if e.get("price") is not None:
            e["price"] = round(e["price"] + delta, 4)
        e["status"] = "ok"
        e["lastAttemptAt"] = stamp
        e["lastSuccessAt"] = stamp
    write(work, shard_rel, shard)

    for aid in ids:
        rel = "data/history/%s.json" % aid
        h = read(work, rel)
        h["_測試標記"] = "%s-%s" % (source, stamp)
        write(work, rel, h)
    return shard


def make_cloud_report(work, slot):
    """模擬雲端產生了今天的報告（含一個遠端還沒有的全新檔案）。

    這個檔案就是 3-H 要保護的東西：家用電腦的重試流程不認識它，
    但絕對不可以把它刪掉或還原掉。
    """
    marker = {"date": TODAY, "slot": slot, "_測試標記": "雲端產生的報告"}
    write(work, "data/archive/%s/%s.json" % (TODAY, slot), marker)
    write(work, "data/archive/%s/snapshot.json" % TODAY, {"_測試標記": "雲端快照"})
    write(work, "data/report-latest.json", marker)
    idx = read(work, "data/archive/index.json")
    idx["_測試標記"] = "雲端更新的索引"
    write(work, "data/archive/index.json", idx)
    return marker


# --------------------------------------------------------------------------
# 劇本 A：雲端先推成功，家用電腦被拒後重試
# --------------------------------------------------------------------------

def scenario_a(tmp):
    say("=" * 70)
    say("劇本 A — 雲端先推成功，家用電腦被拒 → 走重試流程")
    say("=" * 70)
    origin, cloudside, localside = build_world(tmp)

    t_cloud = "2026-09-05T11:11:11+08:00"
    t_local = "2026-09-05T11:22:22+08:00"

    # --- 1. 雲端那台：抓 → 合併 → 產報告 → 推送 ---------------------------
    say("\n[1] 雲端那台：更新 8 項 + 產生今天的報告，然後推送")
    cloud_shard = bump_side(cloudside, "cloud", CLOUD_IDS, +100.0, t_cloud)
    py(cloudside, "scripts/merge_latest.py")
    report_marker = make_cloud_report(cloudside, "morning")
    rc, out = py(cloudside, "scripts/publish.py", "--source", "cloud",
                 "--rebuild", "merge,report:morning",
                 "--message", "data: %s morning 更新（雲端）" % TODAY,
                 check_rc=False)
    check("雲端推送成功（結束碼 0）", rc == 0, "實際 %d" % rc)

    # --- 2. 家用電腦：抓 → 合併（此時手上的雲端分片還是舊的）→ 推送 -------
    say("\n[2] 家用電腦：更新 5 項後推送（此時它手上的雲端分片還是舊版）")
    bump_side(localside, "local", LOCAL_IDS, +7.0, t_local)
    py(localside, "scripts/merge_latest.py")

    stale = read(localside, "data/latest.json")
    stale_price = stale["assets"][CLOUD_IDS[0]]["price"]
    fresh_price = cloud_shard["assets"][CLOUD_IDS[0]]["price"]
    check("重算前家用電腦手上的雲端價格確實是舊的（測試前提成立）",
          stale_price != fresh_price,
          "手上 %s vs 雲端已推 %s" % (stale_price, fresh_price))

    # check_rc=False：推送失敗要印成 FAIL 讓人看得懂，不要丟例外把後面的檢查全中斷
    rc, out = py(localside, "scripts/publish.py", "--source", "local",
                 "--rebuild", "merge",
                 "--message", "data: %s 本機補抓（含台銀黃金與匯率）" % TODAY,
                 check_rc=False, env={"INVESTWATCH_RETRY_SLEEP": "0"})
    check("家用電腦最終推送成功（結束碼 0）", rc == 0, "實際 %d" % rc)
    check("而且確實走過了重試流程（不是一次就成功）", "第 1 次重試" in out)

    # --- 3. 驗收：把最終的遠端內容 clone 出來檢查 --------------------------
    say("\n[3] 檢查最終的遠端內容")
    verify = os.path.join(tmp, "verify")
    git(tmp, "clone", "--quiet", origin.replace("\\", "/"), verify)

    final_cloud = read(verify, "data/sources/cloud.json")
    final_local = read(verify, "data/sources/local.json")
    final_latest = read(verify, "data/latest.json")

    say("\n  ── 3-B：雲端那 8 項不可以被還原成舊值 ──")
    bad = []
    for aid in CLOUD_IDS:
        want = cloud_shard["assets"][aid]
        got = final_cloud["assets"][aid]
        if got.get("price") != want.get("price") or \
           got.get("lastSuccessAt") != want.get("lastSuccessAt"):
            bad.append("%s（價格 %s→%s，成功時間 %s）"
                       % (aid, want.get("price"), got.get("price"),
                          got.get("lastSuccessAt")))
    check("cloud.json 裡 8 項的價格與 lastSuccessAt 都是雲端推上去的那一筆",
          not bad, "；".join(bad))

    hist_bad = [aid for aid in CLOUD_IDS
                if read(verify, "data/history/%s.json" % aid).get("_測試標記")
                != "cloud-%s" % t_cloud]
    check("雲端那 8 項的歷史檔也沒有被還原", not hist_bad, "、".join(hist_bad))

    say("\n  ── 3-B：家用電腦那 5 項也要是新的 ──")
    bad = [aid for aid in LOCAL_IDS
           if final_local["assets"][aid].get("lastSuccessAt") != t_local]
    check("local.json 裡 5 項的 lastSuccessAt 是本機這一輪的", not bad, "、".join(bad))

    say("\n  ── 3-B：合併出來的 latest.json ──")
    s = final_latest["summary"]
    check("summary = ok:13 / error:0",
          s["total"] == 13 and s["ok"] == 13 and s["error"] == 0,
          "實際 total=%d ok=%d error=%d" % (s["total"], s["ok"], s["error"]))

    mixed_bad = []
    for aid in CLOUD_IDS:
        if final_latest["assets"][aid].get("price") != \
                cloud_shard["assets"][aid].get("price"):
            mixed_bad.append(aid)
    check("latest.json 裡雲端 8 項的價格是雲端的新值（證明重算時讀到的是對方的新分片）",
          not mixed_bad, "、".join(mixed_bad))
    check("latest.json 的兩個來源時間都在（cloud=%s / local=%s）"
          % (final_latest["sources"]["cloud"]["runAt"],
             final_latest["sources"]["local"]["runAt"]),
          final_latest["sources"]["cloud"]["runAt"] == t_cloud
          and final_latest["sources"]["local"]["runAt"] == t_local)

    say("\n  ── 3-H：雲端新增的、家用電腦不認識的檔案必須原封不動存活 ──")
    rel = "data/archive/%s/morning.json" % TODAY
    exists = os.path.isfile(os.path.join(verify, rel))
    same = exists and read(verify, rel) == report_marker
    check("%s 還在，而且內容沒被改" % rel, same,
          "" if same else ("檔案不存在" if not exists else "內容被改了"))
    check("data/report-latest.json 仍是雲端那一份（本機沒去碰報告）",
          read(verify, "data/report-latest.json") == report_marker)
    check("data/archive/index.json 仍帶著雲端的標記",
          read(verify, "data/archive/index.json").get("_測試標記") == "雲端更新的索引")

    say("\n  ── 3-C：本機的 commit 只動了該動的檔案 ──")
    _, changed = git(verify, "show", "--stat", "--format=", "HEAD")
    _, files = git(verify, "show", "--name-only", "--format=", "HEAD")
    touched = sorted(f for f in files.splitlines() if f.strip())
    allowed = set(["data/latest.json", "data/sources/local.json"] +
                  ["data/history/%s.json" % i for i in LOCAL_IDS])
    extra = [f for f in touched if f not in allowed]
    check("最後那個 commit 沒有動到任何不屬於本機的檔案", not extra, "、".join(extra))
    say("      最後那個 commit 動到的檔案：")
    for f in touched:
        say("        %s" % f)

    say("\n  ── 3-I：publish 跑完工作區必須乾淨 ──")
    _, st = git(localside, "status", "--porcelain")
    check("家用電腦那台 git status --porcelain 是空的",
          st.strip() == "", st.strip()[:200])

    _, st2 = git(cloudside, "status", "--porcelain")
    check("雲端那台 git status --porcelain 也是空的",
          st2.strip() == "", st2.strip()[:200])


# --------------------------------------------------------------------------
# 劇本 B：三次全部推不上去
# --------------------------------------------------------------------------

def scenario_b(tmp):
    say("\n" + "=" * 70)
    say("劇本 B（3-J）— 三次重試全部失敗，不可以留下爛攤子")
    say("=" * 70)
    origin, cloudside, localside = build_world(tmp)

    # 遠端裝一個永遠拒絕的掛勾：fetch 正常，push 一定失敗
    hooks = os.path.join(origin, "hooks")
    os.makedirs(hooks, exist_ok=True)
    with open(os.path.join(hooks, "pre-receive"), "w", newline="\n") as fh:
        fh.write("#!/bin/sh\necho '測試用：遠端一律拒絕推送'\nexit 1\n")
    os.chmod(os.path.join(hooks, "pre-receive"), 0o755)

    before_head = git(localside, "rev-parse", "HEAD")[1].strip()
    bump_side(localside, "local", LOCAL_IDS, +3.0, "2026-09-05T12:00:00+08:00")
    py(localside, "scripts/merge_latest.py")

    rc, out = py(localside, "scripts/publish.py", "--source", "local",
                 "--rebuild", "merge", "--message", "data: 一定會失敗的一輪",
                 check_rc=False, env={"INVESTWATCH_RETRY_SLEEP": "0"})

    check("結束碼是非零", rc != 0, "實際 %d" % rc)
    _, st = git(localside, "status", "--porcelain")
    check("工作區乾淨（不會讓下一次排程跳過同步）",
          st.strip() == "", st.strip()[:300])

    head = git(localside, "rev-parse", "HEAD")[1].strip()
    remote_head = git(localside, "rev-parse", "origin/main")[1].strip()
    check("HEAD 沒有跟遠端分岔（沒有留下推不上去的 commit）",
          head == remote_head,
          "HEAD=%s origin/main=%s" % (head[:8], remote_head[:8]))
    check("HEAD 就是這一輪開始前的那一個", head == before_head)


def main():
    load_owner_ids()
    say("雲端負責 %d 項：%s" % (len(CLOUD_IDS), "、".join(CLOUD_IDS)))
    say("本機負責 %d 項：%s" % (len(LOCAL_IDS), "、".join(LOCAL_IDS)))
    say("（歸屬是從 data/assets.json 的 owner 算出來的，測試裡沒有寫死 id）\n")

    tmp = tempfile.mkdtemp(prefix="investwatch-race-")
    try:
        scenario_a(os.path.join(tmp, "a"))
        scenario_b(os.path.join(tmp, "b"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    say("\n" + "=" * 70)
    if _fails:
        say("結果：%d 條不合格 —— %s" % (len(_fails), "；".join(_fails)))
        return 1
    say("結果：全部通過。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
