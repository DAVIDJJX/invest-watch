#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autopilot_install.py — 自動駕駛的安裝與檢查（每台電腦跑一次；之後換 MacBook 也是跑這一支）

  python scripts/autopilot_install.py                  裝「推送前的檢查」、設定事後偵測的起點、檢查環境
  python scripts/autopilot_install.py --check          只檢查，不改任何東西
  python scripts/autopilot_install.py --uninstall      移除「推送前的檢查」
  python scripts/autopilot_install.py --bootstrap-from <worktree>
        P1 自己的實戰用：保護檔還沒合併進 main 之前，把那個 worktree 裡「已經 commit」的保護檔複製一份到主目錄的 .claude/
        （主目錄的 .claude/ 目前整個被 git 忽略，不會弄髒 main），並記下是從哪個 commit 來的。

為什麼「推送前的檢查」要另外裝：git 的 hook（.git/hooks/pre-push）本來就不進版控。這裡裝的只是一個很短的入口，
真正的規則在主目錄的 .claude/hooks/iw_prepush.py（進版控；改它要經過放行）。那個檔不在的時候（例如 P1 被退回），入口自動放行。

這支程式不連網、不處理任何密碼或權杖。
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SHIM = """#!/bin/sh
# InvestWatch 自動駕駛：推送前的檢查（第二道保護）。由 scripts/autopilot_install.py 安裝。
# 真正的規則在主目錄的 .claude/hooks/iw_prepush.py（進版控；改它要經過放行）。移除：刪掉這個檔。
common="$(git rev-parse --git-common-dir 2>/dev/null)" || exit 0
case "$common" in
  /*|[A-Za-z]:*) ;;
  *) common="$(pwd)/$common" ;;
esac
logic="$(dirname "$common")/.claude/hooks/iw_prepush.py"
[ -f "$logic" ] || exit 0
input="$(cat)"
rc=127
for c in "py -3" python3 python; do
  printf '%s\\n' "$input" | $c -X utf8 "$logic" "$@"
  rc=$?
  case "$rc" in
    0) exit 0 ;;
    10) exit 1 ;;
    49|126|127|9009) continue ;;
    *) break ;;
  esac
done
# The check did not run to the end (no usable Python, or the script itself is broken).
# Inside Claude Code: block. Anywhere else (the scheduled job, your own terminal): let it through.
if [ -n "$CLAUDECODE$CLAUDE_CODE_CHILD_SESSION" ]; then
  echo "iw pre-push: the check could not run (rc=$rc); blocking this push." >&2
  exit 1
fi
echo "iw pre-push: the check could not run (rc=$rc); not a Claude Code session, letting it through." >&2
exit 0
"""

LIVE_PARTS = [".claude/settings.json", ".claude/hooks", ".claude/agents", ".claude/skills", ".claude/autopilot"]


def git(args, cwd):
    p = subprocess.run(["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip()


def common_dir(root):
    rc, out = git(["rev-parse", "--git-common-dir"], root)
    if rc != 0:
        raise SystemExit("這裡不是 git 倉庫：%s" % root)
    return os.path.abspath(out if os.path.isabs(out) else os.path.join(root, out))


def install_prepush(root, quiet=False):
    """把入口寫到共用 .git 目錄的 hooks/pre-push。原本就有別人的 pre-push 的話，先備份成 pre-push.before-iw。"""
    hooks = os.path.join(common_dir(root), "hooks")
    os.makedirs(hooks, exist_ok=True)
    p = os.path.join(hooks, "pre-push")
    if os.path.exists(p):
        old = io.open(p, encoding="utf-8", errors="replace").read()
        if old == SHIM:
            if not quiet:
                print("推送前的檢查：已經裝好了（%s）" % p)
            return p
        if "iw_prepush.py" not in old:
            shutil.copyfile(p, p + ".before-iw")
            if not quiet:
                print("原本的 pre-push 備份成 %s" % (p + ".before-iw"))
    with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(SHIM)
    try:
        os.chmod(p, 0o755)
    except OSError:
        pass
    if not quiet:
        print("推送前的檢查：已安裝（%s）" % p)
    return p


def uninstall_prepush(root, quiet=False):
    p = os.path.join(common_dir(root), "hooks", "pre-push")
    if os.path.exists(p) and "iw_prepush.py" in io.open(p, encoding="utf-8", errors="replace").read():
        os.remove(p)
        if not quiet:
            print("已移除 %s" % p)
    elif not quiet:
        print("沒有我們裝的 pre-push，不必移除。")


LOCAL_IGNORE = ".autopilot/"


def ensure_local_ignore(root, quiet=False):
    """讓這台電腦的 git 忽略 .autopilot/（寫在 .git/info/exclude，不進版控）。
    為什麼需要：P1 合併之前，main 上的 .gitignore 還沒有這一行；自動駕駛一啟動就會在主目錄建 .autopilot/，
    主目錄就多出沒進版控的資料夾（筆電排程要求主目錄乾淨）。合併之後這一行是多餘的，留著無妨。"""
    p = os.path.join(common_dir(root), "info", "exclude")
    text = io.open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else ""
    if LOCAL_IGNORE in [x.strip() for x in text.split("\n")]:
        if not quiet:
            print("本機的忽略清單：已經有 %s" % LOCAL_IGNORE)
        return p
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(("" if not text or text.endswith("\n") else "\n") + "# InvestWatch 自動駕駛：每個階段的報告與紀錄只留在本機\n" + LOCAL_IGNORE + "\n")
    if not quiet:
        print("本機的忽略清單：加了 %s（%s）" % (LOCAL_IGNORE, p))
    return p


def state_dir(root):
    return os.path.join(common_dir(root), "iw-autopilot")


def set_baseline(root):
    """事後偵測的起點：安裝當下 origin/main 的位置（之後每封信會列出從這裡以後、沒有放行紀錄的非資料變更）。"""
    sd = state_dir(root)
    os.makedirs(sd, exist_ok=True)
    p = os.path.join(sd, "state.json")
    st = {"version": 1, "active": False, "stage": None, "status": None}
    if os.path.exists(p):
        st = json.load(io.open(p, encoding="utf-8"))
    if st.get("tripwire_baseline"):
        print("事後偵測的起點：已經有了（%s）" % st["tripwire_baseline"][:7])
        return
    rc, tip = git(["rev-parse", "-q", "--verify", "refs/remotes/origin/main"], root)
    if rc != 0:
        print("事後偵測的起點：找不到 origin/main，先不設定")
        return
    st["tripwire_baseline"] = tip
    with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print("事後偵測的起點：%s（安裝當下的 origin/main）" % tip[:7])


def bootstrap(source, root):
    """把 source 這個 worktree 裡已經 commit 的保護檔複製到主目錄的 .claude/，並記下來源的 commit。"""
    source = os.path.abspath(source)
    main_root = os.path.dirname(common_dir(root))
    rc, dirty = git(["status", "--porcelain", "--"] + LIVE_PARTS, source)
    if rc != 0 or dirty.strip():
        raise SystemExit("來源的保護檔還有沒 commit 的改動，先 commit：\n%s" % dirty)
    rc, sha = git(["rev-parse", "HEAD"], source)
    rc2, files = git(["ls-tree", "-r", "--name-only", "HEAD", "--"] + LIVE_PARTS, source)
    names = [f for f in files.split("\n") if f.strip()]
    if rc != 0 or rc2 != 0 or not names:
        raise SystemExit("來源裡找不到保護檔。")
    rc, tracked = git(["ls-tree", "-r", "--name-only", "HEAD", "--"] + LIVE_PARTS, main_root)
    if tracked.strip():
        raise SystemExit("主目錄的 main 上已經有保護檔了（已經合併過），不需要、也不可以再用這個方式覆蓋。")
    for part in LIVE_PARTS[1:]:
        old = os.path.join(main_root, *part.split("/"))
        if os.path.isdir(old):
            shutil.rmtree(old)
    for f in names:
        src = os.path.join(source, *f.split("/"))
        dst = os.path.join(main_root, *f.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    sd = state_dir(root)
    os.makedirs(sd, exist_ok=True)
    with io.open(os.path.join(sd, "install.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"source_commit": sha, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": names}, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print("已經把 %d 個保護檔從 %s（commit %s）複製到 %s/.claude/" % (len(names), source, sha[:7], main_root))
    return sha


def check(root):
    """環境檢查。回傳問題數。"""
    main_root = os.path.dirname(common_dir(root))
    bad = 0

    def line(ok, text):
        print("  %s %s" % ("✓" if ok else "✗", text))
        return 0 if ok else 1

    print("自動駕駛的環境檢查（主目錄：%s）" % main_root)
    bad += line(sys.version_info >= (3, 8), "Python %s" % sys.version.split()[0])
    gh = shutil.which("gh") or next((p for p in ("C:/Program Files/GitHub CLI/gh.exe", "/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), None)
    bad += line(bool(gh), "gh（寄通知用）：%s" % (gh or "找不到"))
    hook = os.path.join(common_dir(root), "hooks", "pre-push")
    ok = os.path.exists(hook) and io.open(hook, encoding="utf-8", errors="replace").read() == SHIM
    bad += line(ok, "推送前的檢查（.git/hooks/pre-push）%s" % ("已安裝" if ok else "沒有裝，或不是最新的"))
    logic = os.path.join(main_root, ".claude", "hooks", "iw_prepush.py")
    bad += line(os.path.exists(logic), "主目錄的保護檔：%s" % ("在" if os.path.exists(logic) else "不在（還沒合併，或還沒用 --bootstrap-from 放進來）"))
    settings = os.path.join(main_root, ".claude", "settings.json")
    bad += line(os.path.exists(settings), "主目錄的 .claude/settings.json：%s" % ("在" if os.path.exists(settings) else "不在"))
    rc, branch = git(["rev-parse", "--abbrev-ref", "HEAD"], main_root)
    bad += line(branch == "main", "主目錄在 %s 分支" % branch)
    py = shutil.which("py") or shutil.which("python3") or shutil.which("python")
    bad += line(bool(py), "hook 用的 Python：%s" % (py or "找不到 py／python3／python"))
    model, effort = "設定檔寫的那一個", "設定檔寫的那一個"
    try:                                                            # 模型與強度只寫在設定檔（2026-10-05 起施工是 Opus 5.5），這裡照它印
        with io.open(os.path.join(main_root, ".claude", "autopilot", "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        model = cfg.get("requiredModelLabel") or cfg.get("requiredModel") or model
        effort = cfg.get("requiredEffortLabel") or cfg.get("requiredEffort") or effort
    except Exception:                                               # noqa: B902
        pass
    print("提醒：自動駕駛只在「資料夾選 %s」的工作階段有效；模型選 %s、思考強度選 %s。" % (os.path.basename(main_root), model, effort))
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description="自動駕駛的安裝與檢查")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--bootstrap-from")
    ap.add_argument("--root", default=ROOT, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.uninstall:
        uninstall_prepush(a.root)
        return 0
    if a.check:
        return 1 if check(a.root) else 0
    if a.bootstrap_from:
        bootstrap(a.bootstrap_from, a.root)
    install_prepush(a.root)
    ensure_local_ignore(a.root)
    set_baseline(a.root)
    return 1 if check(a.root) else 0


if __name__ == "__main__":
    sys.exit(main())
