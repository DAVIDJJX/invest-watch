#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_update_local_lock.py — 本機腳本 update_local.ps1 的三件事，在沙盒裡【真的跑】一遍

跑法：
    python scripts/test_update_local_lock.py              # 正式組：三件事都要成立
    python scripts/test_update_local_lock.py --contrast   # 對照組：把修正拿掉，問題必須重現

這支【不碰 GitHub、不碰台銀、不碰你的真倉庫】。它在暫存目錄裡：
    * 建一個 bare repo 當「遠端」，clone 一份當「筆電」、一份當「雲端」；
    * 用【目前工作區】的 scripts/ 與 data/ 當種子（不是最後一次 commit——
      2026-09-09 的教訓：只帶已提交的內容，對照組就驗到舊版上了）；
    * 把種子裡的 scripts/fetch_data.py 換成一個替身：睡幾秒、改寫本機分片的時間戳，
      不發任何網路請求。merge_latest.py 與 publish.py 用真的，推到那個假遠端。
互斥鎖的名字帶倉庫路徑的雜湊，所以沙盒跟正式排程互不干擾。

它在驗什麼？
    A. 互斥鎖：同時啟動 4 個 update_local.ps1（2026-09-14 22:23 真的發生過四個工作同一秒啟動）
       → 只有一個真的抓資料、推送；其餘三個寫「另一個實例執行中，略過」、結束碼 0；
       log 裡沒有任何 index.lock／cannot lock ref；遠端剛好多一個 commit；分片是完整的 JSON。
    B. 未追蹤的檔案不算「工作區不乾淨」：倉庫根目錄放一個陌生資料夾與一個陌生檔案、
       遠端又比本機新 → 照常快轉、抓取、推送，log 不出現「跳過自動快轉」。
    C. 死掉的 .git/index.lock：放超過 2 分鐘且沒有 git 在跑 → 清掉並繼續；
       剛建立的 → 不動它（你可能正在用 git）。

對照組（--contrast）各自把對應的修正從沙盒裡的 ps1 拿掉，必須重現：
    A'. 互撞（index.lock／cannot lock ref／推送被拒／結束碼非零／多出或少掉 commit，任一）
    B'. 結束碼 4 與「跳過自動快轉」
    C'. 死鎖檔留著 → 這一輪失敗

只在 Windows 上能跑（需要 PowerShell 與 Windows 的具名 Mutex）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCES = 4
_fails = []

STUB_FETCH = '''# -*- coding: utf-8 -*-
"""fetch_data.py 的替身（只存在於測試沙盒）：不連網，睡幾秒，改寫本機分片的時間戳。"""
import json, os, sys, time
from datetime import datetime, timedelta, timezone
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
time.sleep(float(os.environ.get("IW_STUB_SLEEP", "4")))
p = os.path.join(ROOT, "data", "sources", "local.json")
with open(p, encoding="utf-8") as fh:
    d = json.load(fh)
now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
d["runAt"] = now
for e in d["assets"].values():
    e["status"] = "ok"
    e["lastAttemptAt"] = now
    e["lastSuccessAt"] = now
with open(p, "w", encoding="utf-8", newline="\\n") as fh:      # 跟真的一樣：不是原子寫入
    json.dump(d, fh, ensure_ascii=False, indent=1)
    fh.write("\\n")
print("（替身）已寫出分片 data/sources/local.json（%d 項）" % len(d["assets"]))
'''


def say(msg=""):
    print(msg)


def check(label, ok, detail=""):
    say("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  — " + detail) if detail else ""))
    if not ok:
        _fails.append(label)
    return ok


def run(args, cwd, check_rc=True):
    p = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    out = p.stdout.decode("utf-8", "replace")
    if check_rc and p.returncode != 0:
        say(out)
        raise RuntimeError("指令失敗（%d）：%s" % (p.returncode, " ".join(args)))
    return p.returncode, out


def git(cwd, *args, **kw):
    return run(["git"] + list(args), cwd, **kw)


def identity(repo):
    git(repo, "config", "user.name", "InvestWatch 測試")
    git(repo, "config", "user.email", "test@example.invalid")


def build_world(tmp, mutate=None):
    """回傳 (origin, laptop, cloudside)。mutate(ps1 文字) → 改過的文字，用來做對照組。"""
    os.makedirs(tmp, exist_ok=True)
    origin = os.path.join(tmp, "origin.git")
    seed = os.path.join(tmp, "seed")
    git(tmp, "init", "--quiet", "--bare", "-b", "main", origin)
    git(tmp, "clone", "--quiet", ROOT.replace("\\", "/"), seed)
    for sub in ("scripts", "data"):
        dst = os.path.join(seed, sub)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(os.path.join(ROOT, sub), dst,
                        ignore=shutil.ignore_patterns("__pycache__", "*.log", "archive"))
    with open(os.path.join(seed, "scripts", "fetch_data.py"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(STUB_FETCH)
    if mutate:
        p = os.path.join(seed, "scripts", "update_local.ps1")
        with open(p, encoding="utf-8", newline="") as fh:      # utf-8（不是 -sig）：BOM 原樣留著
            text = fh.read()
        new = mutate(text)
        if new == text:
            raise RuntimeError("對照組的突變沒有改到任何東西——ps1 的寫法變了，請更新這支測試")
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(new)
    identity(seed)
    git(seed, "checkout", "--quiet", "-B", "main")
    git(seed, "remote", "set-url", "origin", origin.replace("\\", "/"))
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "--allow-empty", "-m", "測試用種子")
    git(seed, "push", "--quiet", "-u", "origin", "main")
    laptop = os.path.join(tmp, "laptop")
    cloud = os.path.join(tmp, "cloudside")
    git(tmp, "clone", "--quiet", origin.replace("\\", "/"), laptop)
    git(tmp, "clone", "--quiet", origin.replace("\\", "/"), cloud)
    identity(laptop)
    identity(cloud)
    return origin, laptop, cloud


def start_ps1(repo, *extra):
    ps1 = os.path.join(repo, "scripts", "update_local.ps1")
    return subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1]
                            + list(extra), cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def read_log(repo):
    p = os.path.join(repo, "scripts", "update_local.log")
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def commits(origin):
    return int(git(origin, "rev-list", "--count", "main")[1].strip())


def shard_ok(repo):
    try:
        with open(os.path.join(repo, "data", "sources", "local.json"), encoding="utf-8") as fh:
            return bool(json.load(fh).get("assets"))
    except Exception:
        return False


def cloud_pushes_one_commit(cloud):
    """讓遠端比筆電新：雲端那台改一下自己的分片並推上去。"""
    p = os.path.join(cloud, "data", "sources", "cloud.json")
    with open(p, encoding="utf-8") as fh:
        d = json.load(fh)
    d["runAtText"] = "sandbox %s" % time.time()
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    git(cloud, "pull", "--quiet", "--ff-only")
    git(cloud, "commit", "--quiet", "-am", "雲端那台的更新（測試）")
    git(cloud, "push", "--quiet")


COLLISION = re.compile(r"index\.lock|cannot lock ref|rejected|Another git process", re.I)


# --------------------------------------------------------------------------
# A. 互斥鎖
# --------------------------------------------------------------------------

def scenario_lock(tmp, contrast):
    say("\n" + "=" * 70)
    say("A%s — 同時啟動 %d 個 update_local.ps1" % ("'（對照組：拿掉互斥鎖）" if contrast else "", INSTANCES))
    say("=" * 70)
    mutate = None
    if contrast:
        mutate = lambda t: t.replace("$script:HasLock = $script:Mutex.WaitOne(0)", "$script:HasLock = $true")
    origin, laptop, _ = build_world(os.path.join(tmp, "A"), mutate)
    rounds = 5 if contrast else 3
    reproduced = []
    for n in range(1, rounds + 1):
        before = commits(origin)
        log_before = len(read_log(laptop))
        procs = [start_ps1(laptop) for _ in range(INSTANCES)]
        codes = [p.wait(timeout=300) for p in procs]
        log = read_log(laptop)[log_before:]
        gained = commits(origin) - before
        skipped = log.count("另一個實例執行中，略過")
        started = log.count("==================== 開始")
        hits = sorted(set(m.group(0) for m in COLLISION.finditer(log)))
        say("\n  第 %d 輪：結束碼 %s、開始 %d 次、略過 %d 次、遠端多 %d 個 commit、互撞字樣 %s"
            % (n, codes, started, skipped, gained, hits or "無"))
        if contrast:
            symptoms = []
            if hits:
                symptoms.append("log 出現 " + "／".join(hits))
            if any(c != 0 for c in codes):
                symptoms.append("結束碼非零 %s" % [c for c in codes if c != 0])
            if gained != 1:
                symptoms.append("遠端多了 %d 個 commit（應該剛好 1 個）" % gained)
            if not shard_ok(laptop):
                symptoms.append("分片不是完整的 JSON")
            if symptoms:
                reproduced.append("第 %d 輪：%s" % (n, "；".join(symptoms)))
                for line in log.splitlines():
                    if COLLISION.search(line):
                        say("      log> " + line.strip()[:150])
                break
            continue
        check("第 %d 輪：%d 個實例全部結束碼 0" % (n, INSTANCES), all(c == 0 for c in codes), str(codes))
        check("第 %d 輪：只有一個真的開始跑" % n, started == 1, "開始 %d 次" % started)
        check("第 %d 輪：其餘 %d 個都寫了「另一個實例執行中，略過」" % (n, INSTANCES - 1),
              skipped == INSTANCES - 1, "略過 %d 次" % skipped)
        check("第 %d 輪：log 沒有任何 index.lock／cannot lock ref／rejected" % n, not hits, str(hits))
        check("第 %d 輪：遠端剛好多一個 commit" % n, gained == 1, "多了 %d 個" % gained)
        check("第 %d 輪：本機分片是完整的 JSON" % n, shard_ok(laptop))
        rc, dirty = git(laptop, "status", "--porcelain", "--untracked-files=no")
        check("第 %d 輪：跑完工作區乾淨" % n, not dirty.strip(), dirty.strip()[:120])
    if contrast:
        check("拿掉互斥鎖 → 互撞必須重現（最多試 %d 輪）" % rounds, bool(reproduced),
              reproduced[0] if reproduced else "五輪都沒有互撞——對照組沒有證明任何事")


# --------------------------------------------------------------------------
# B. 未追蹤的檔案不算髒
# --------------------------------------------------------------------------

def scenario_untracked(tmp, contrast):
    say("\n" + "=" * 70)
    say("B%s — 倉庫根目錄有陌生的未追蹤檔案，而且遠端比本機新"
        % ("'（對照組：拿掉 --untracked-files=no）" if contrast else ""))
    say("=" * 70)
    mutate = None
    if contrast:
        mutate = lambda t: t.replace("$dirty = git status --porcelain --untracked-files=no",
                                     "$dirty = git status --porcelain")
    origin, laptop, cloud = build_world(os.path.join(tmp, "B"), mutate)
    cloud_pushes_one_commit(cloud)
    os.makedirs(os.path.join(laptop, "陌生的空資料夾"))
    os.makedirs(os.path.join(laptop, "不相干的文件"))
    with open(os.path.join(laptop, "不相干的文件", "某份交付文件.md"), "w", encoding="utf-8") as fh:
        fh.write("這個檔案跟網站無關，也不該被 commit。\n")
    with open(os.path.join(laptop, "stray.tmp"), "w", encoding="utf-8") as fh:
        fh.write("x\n")
    before = commits(origin)
    code = start_ps1(laptop).wait(timeout=300)
    log = read_log(laptop)
    gained = commits(origin) - before
    say("\n  結束碼 %d、遠端多 %d 個 commit" % (code, gained))
    if contrast:
        check("拿掉 --untracked-files=no → 必須重現結束碼 4", code == 4, "實際 %d" % code)
        check("log 出現「跳過自動快轉」與「無法快轉 —— 中止」",
              "跳過自動快轉" in log and "無法快轉" in log)
        check("什麼都沒有推上去", gained == 0, "多了 %d 個" % gained)
        return
    check("結束碼 0", code == 0, "實際 %d" % code)
    check("log 沒有「跳過自動快轉」", "跳過自動快轉" not in log)
    check("有快轉到遠端的新 commit（log 有 git merge 的 Fast-forward／Updating）",
          bool(re.search(r"git merge: .*(Fast-forward|Updating)", log)))
    check("抓取與推送都做了（遠端多一個 commit）", gained == 1 and "推送成功" in log, "多了 %d 個" % gained)
    head = git(laptop, "rev-parse", "HEAD")[1].strip()
    remote = git(origin, "rev-parse", "main")[1].strip()
    check("筆電的 HEAD 就是遠端的 main", head == remote)
    tracked = git(laptop, "ls-files")[1]
    check("陌生檔案沒有被 commit", "stray.tmp" not in tracked and "某份交付文件" not in tracked)
    check("陌生檔案原封不動還在", os.path.exists(os.path.join(laptop, "stray.tmp")) and
          os.path.exists(os.path.join(laptop, "不相干的文件", "某份交付文件.md")))


# --------------------------------------------------------------------------
# C. 死掉的 index.lock
# --------------------------------------------------------------------------

def scenario_index_lock(tmp, contrast):
    say("\n" + "=" * 70)
    say("C%s — .git/index.lock" % ("'（對照組：拿掉清理）" if contrast else ""))
    say("=" * 70)
    mutate = None
    if contrast:
        mutate = lambda t: t.replace("if ($gitProcs -eq 0 -and $age -gt 120) {", "if ($false) {")
    origin, laptop, _ = build_world(os.path.join(tmp, "C"), mutate)
    lock = os.path.join(laptop, ".git", "index.lock")

    with open(lock, "w") as fh:
        fh.write("")
    old = time.time() - 600
    os.utime(lock, (old, old))
    before = commits(origin)
    code = start_ps1(laptop).wait(timeout=300)
    log = read_log(laptop)
    gained = commits(origin) - before
    say("\n  放了 10 分鐘的死鎖檔：結束碼 %d、遠端多 %d 個 commit、鎖檔%s"
        % (code, gained, "還在" if os.path.exists(lock) else "不見了"))
    if contrast:
        check("拿掉清理 → 死鎖檔留著，這一輪必須失敗", code != 0 and gained == 0,
              "結束碼 %d、多了 %d 個 commit" % (code, gained))
        check("log 裡看得到 index.lock 造成的錯誤", "index.lock" in log)
        if os.path.exists(lock):
            os.remove(lock)
        return
    check("log 記下「已清掉」", "已清掉" in log)
    check("鎖檔不見了", not os.path.exists(lock))
    check("這一輪照常完成（結束碼 0、遠端多一個 commit）", code == 0 and gained == 1,
          "結束碼 %d、多了 %d 個" % (code, gained))

    with open(lock, "w") as fh:          # 剛建立的鎖：可能是你自己正在用 git，不准動
        fh.write("")
    log_before = len(read_log(laptop))
    code = start_ps1(laptop).wait(timeout=300)
    log = read_log(laptop)[log_before:]
    say("  剛建立的鎖檔：結束碼 %d、鎖檔%s" % (code, "還在" if os.path.exists(lock) else "不見了"))
    check("剛建立的鎖檔不可以被刪", os.path.exists(lock))
    check("log 記下「不動它」", "不動它" in log)
    if os.path.exists(lock):
        os.remove(lock)


def main():
    if os.name != "nt":
        say("這支只在 Windows 上能跑（需要 PowerShell 與 Windows 的具名 Mutex）——略過。")
        return 0
    contrast = "--contrast" in sys.argv[1:]
    only = [a for a in sys.argv[1:] if a in ("A", "B", "C")]
    tmp = tempfile.mkdtemp(prefix="iw-lock-")
    say("沙盒：%s（%s）" % (tmp, "對照組" if contrast else "正式組"))
    try:
        if not only or "A" in only:
            scenario_lock(tmp, contrast)
        if not only or "B" in only:
            scenario_untracked(tmp, contrast)
        if not only or "C" in only:
            scenario_index_lock(tmp, contrast)
    finally:
        def _force(func, path, exc):                 # Windows 上 .git 裡的檔案是唯讀的
            try:
                os.chmod(path, 0o700)
                func(path)
            except Exception:
                pass
        shutil.rmtree(tmp, onerror=_force)
    say("\n" + "=" * 70)
    if _fails:
        say("結果：%d 項不符合預期：" % len(_fails))
        for f in _fails:
            say("  - " + f)
        return 1
    say("結果：全部符合預期。" + ("（對照組：問題都重現了，證明修正真的有在作用）" if contrast else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
