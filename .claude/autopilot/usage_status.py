# -*- coding: utf-8 -*-
"""
usage_status.py — 用量記錄器（P3 規格 2.3）。Claude Code 的狀態列（.claude/settings.json 的 statusLine）每次更新時執行它。

從標準輸入讀 Claude Code 給的 JSON，取：
    rate_limits.five_hour.used_percentage   5 小時的用量（%）
    rate_limits.seven_day.used_percentage   7 天的用量（%）
    model.id                                現在的模型
官方文件說這兩個用量只有 Pro／Max 才有、第一次回應之後才出現、可能缺席——缺的就寫 null。
把它們原子寫入主目錄的 .autopilot/runs/usage.json（先寫暫存檔再換名），狀態列印一行「5h 42% ｜ 7d 31%」。

守門每個動作讀這個檔（iw_guard._usage_high）：自動駕駛中 5 小時的用量到門檻就暫停、寄小事的短信；一般模式只顯示、不擋。
這支腳本任何地方出錯都吞掉、印空字串：狀態列壞掉比少一筆紀錄嚴重。
主目錄的位置用 git 的共用資料夾找（.git 的上一層），不靠狀態列在哪個資料夾執行——在 worktree 裡執行也寫到主目錄那一份。
只用標準函式庫；不連網；不讀任何別的檔。
"""
import datetime
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _main_root(start):
    """start 所在 checkout 的主目錄（共用 .git 的上一層）。找不到回 None。"""
    d = os.path.abspath(start)
    for _ in range(12):
        dotgit = os.path.join(d, ".git")
        if os.path.isdir(dotgit):
            return d
        if os.path.isfile(dotgit):
            with io.open(dotgit, encoding="utf-8", errors="replace") as fh:
                m = re.match(r"^gitdir:\s*(.+?)\s*$", fh.read().strip())
            if not m:
                return None
            gitdir = m.group(1) if os.path.isabs(m.group(1)) else os.path.join(d, m.group(1))
            cfile = os.path.join(gitdir, "commondir")
            common = gitdir
            if os.path.isfile(cfile):
                with io.open(cfile, encoding="utf-8", errors="replace") as fh:
                    c = fh.read().strip()
                common = c if os.path.isabs(c) else os.path.join(gitdir, c)
            return os.path.dirname(os.path.abspath(common))
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def _pct(obj, *path):
    cur = obj
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        return None
    return float(cur)


def _settings(root):
    """config.json 裡的 runsDir 與 usage.file。讀不到就用預設值。"""
    try:
        with io.open(os.path.join(root, ".claude", "autopilot", "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        return cfg.get("runsDir") or ".autopilot/runs", (cfg.get("usage") or {}).get("file") or "usage.json"
    except Exception:                                              # noqa: B902
        return ".autopilot/runs", "usage.json"


def record(data, root, now=None):
    """把一份狀態列輸入記成 usage.json。回傳寫好的那一份（dict）。"""
    five = _pct(data, "rate_limits", "five_hour", "used_percentage")
    seven = _pct(data, "rate_limits", "seven_day", "used_percentage")
    model = (data.get("model") or {}).get("id") if isinstance(data.get("model"), dict) else None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    rec = {"updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "five_hour_pct": five, "seven_day_pct": seven,
           "model": model if isinstance(model, str) else None}
    runs, name = _settings(root)
    d = os.path.join(root, *runs.split("/"))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    tmp = p + ".tmp.%d" % os.getpid()
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rec, fh, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, p)
    return rec


def line(rec):
    def f(x):
        return "—" if x is None else ("%.0f%%" % x)
    return "5h %s ｜ 7d %s" % (f(rec.get("five_hour_pct")), f(rec.get("seven_day_pct")))


def main(stdin=None, stdout=None, root=None, now=None):
    out = stdout or sys.stdout
    try:
        raw = (stdin or sys.stdin).read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
        root = root or _main_root(HERE)
        if not root:
            out.write("")
            return 0
        out.write(line(record(data, root, now=now)))
    except Exception:                                              # noqa: B902
        try:
            out.write("")
        except Exception:                                          # noqa: B902
            pass
    return 0


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                              # noqa: B902
        pass
    sys.exit(main())
