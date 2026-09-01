#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bump_assets.py — 幫所有 HTML 裡的 css/js 引用加上（或更新）版本號

為什麼需要這個？
    瀏覽器會把 style.css、app.js 這些檔案快取起來。改版之後，
    使用者可能拿到「新的 HTML ＋ 舊的 JS」，畫面就會怪怪的甚至壞掉。
    2026-09-01 就發生過：新增了按鈕，但瀏覽器還在用舊的 JS，按鈕按了沒反應。

    在網址後面加一段版本號（例如 app.js?v=20260901-3），
    版本一變瀏覽器就會重新下載，問題就消失了。

用法：
    python scripts/bump_assets.py            # 版本號設成今天日期＋流水號
    python scripts/bump_assets.py --check    # 只檢查有沒有漏掉的引用，不修改

改完程式要 commit 之前跑一次就好。
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPE = timezone(timedelta(hours=8))

# 只處理自己的檔案；lib/ 的圖表函式庫版本固定，不需要加
PATTERN = re.compile(r'((?:src|href)=")((?:js|css)/[A-Za-z0-9_.-]+)(\?v=[A-Za-z0-9.-]+)?(")')


def html_files():
    return sorted(
        os.path.join(ROOT, f) for f in os.listdir(ROOT) if f.endswith(".html")
    )


def next_version(files):
    """今天日期 + 流水號。同一天改很多次也不會撞號。"""
    today = datetime.now(TPE).strftime("%Y%m%d")
    used = set()
    for p in files:
        with open(p, encoding="utf-8") as fh:
            for m in re.finditer(r"\?v=(\d{8})-(\d+)", fh.read()):
                if m.group(1) == today:
                    used.add(int(m.group(2)))
    return "%s-%d" % (today, (max(used) + 1) if used else 1)


def main():
    ap = argparse.ArgumentParser(description="幫 HTML 裡的 css/js 引用加版本號")
    ap.add_argument("--check", action="store_true",
                    help="只檢查有沒有沒加版本號的引用，不修改檔案")
    args = ap.parse_args()

    files = html_files()
    if not files:
        print("找不到任何 HTML 檔")
        return 1

    if args.check:
        missing = []
        for p in files:
            with open(p, encoding="utf-8") as fh:
                for m in PATTERN.finditer(fh.read()):
                    if not m.group(3):
                        missing.append((os.path.basename(p), m.group(2)))
        if missing:
            print("以下引用沒有版本號（改版後可能讓使用者拿到舊檔）：")
            for f, ref in missing:
                print("  %-16s %s" % (f, ref))
            return 1
        print("所有 css/js 引用都有版本號。")
        return 0

    version = next_version(files)
    total = 0
    for p in files:
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        new_text, n = PATTERN.subn(
            lambda m: m.group(1) + m.group(2) + "?v=" + version + m.group(4), text)
        if n and new_text != text:
            with open(p, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new_text)
        total += n
        print("  %-16s %d 個引用" % (os.path.basename(p), n))

    print("版本號已更新為 %s（共 %d 個引用）" % (version, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
