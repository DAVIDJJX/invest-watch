#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_ci.py — 驗收機（停點 P2）：在 GitHub 的執行機上，從實際的 commit 重跑全套測試、三種掃描、突變對照，
封鎖對外連線並記錄每一個對外請求，跟 main 比，寫一份機器可讀、綁定 commit 的結果檔。

工作流程 .github/workflows/verify.yml 只做固定的幾步，邏輯都在這裡；每一步是這個檔的一個子指令：
  extract-verifier  從 origin/main 取驗收程式到一個資料夾（main 上還沒有＝P2 第一次：用分支的，結果標 verifier_source=branch）
  lockdown          封鎖對外連線：另一個使用者＋iptables（第 1 層）、瀏覽器的名稱解析規則（第 2 層）、Python 的 socket（第 3 層）
  run-tests         用封鎖中的環境跑全套測試（子程序）→ tests.json、tests-verbose.txt
  unlock            收集對外請求的紀錄（Python 層的名稱、瀏覽器的名稱、iptables 擋下的 IP）→ egress.json；還原規則
  export            上傳之前：只挑判定需要的檔、逐個過隱私掃描 → 另一個資料夾（詳細輸出與瀏覽器的 netlog 不上傳）
  compare           跟 main 比：測試數、被刪改的測試、突變數、被刪改的突變、動到的保護範圍檔、驗收機本身有沒有改 → compare.json
  mutations         跑一片突變（封鎖中；執行器與例外清單用 main 的，定義用分支的）→ mut-*.json
  collect           合併、判定紅綠、結果檔過隱私掃描、寫 job summary → verify-result.json；紅＝非零結束

判定規則（David 2026-10-04 的裁決）：台銀與資料來源網域出現→紅；未知主機→紅；Chrome 自己的背景連線→列出、不紅（名單在下面，算保護範圍）；
系統層擋下的連線（只留下 IP）→紅；突變分片沒有封鎖→紅；測試數或突變數變少→紅；存活的突變不在 main 的例外清單→紅；結果檔含不該公開的字串→紅。
2026-10-06 的裁決（取代 10/4 的「沒有系統層→不紅，只寫封鎖層級 2／3」）：全套測試與每一片突變都要有系統層封鎖，IPv4 與 IPv6 的規則都要；
哪一段缺了，那一次驗收就是紅，原因以「封鎖沒設成」開頭（跟測試紅分開）。結果檔照樣寫每一段的層級。對策是重跑那一次驗收。
誰改得到什麼（2026-10-06，Codex 的審查意見）：分支上的測試用另一個使用者跑；受測的 checkout 與結果資料夾對它唯讀，它只寫得到結果資料夾底下的
egress（對外請求的紀錄）與 inner（它交出來的原始結果）。每一個會執行分支程式碼的步驟跑完，原本的使用者停掉它留下的程序、把結果收進它寫不到的地方、
核對 checkout 跟 commit 一模一樣，記在 seal.json；少一步的紀錄或 checkout 被動過→紅。擋不住的：測試在自己的程序裡回報假數字（靠外部審查看 diff）。
分支改了驗收機本身（verify.yml、這個檔、突變執行器、例外清單）→ 結果標 verifier_changed，判定照 main 的版本做；寄「可以合併」那一關不認這種綠。

只用標準函式庫；離線測試在 scripts/test_verify_ci.py（判定、比對、解析、掃描都是純函式）。
"""
import argparse
import ast
import glob
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VERIFIER_FILES = [".github/workflows/verify.yml", "scripts/verify_ci.py", "scripts/mutations/run_mutations.py", "scripts/mutations/known_survivors.json"]
# 判定時還要用到的 main 上的檔（白名單、保護範圍清單、隱私掃描器、main 的突變定義）
EXTRA_FROM_MAIN = {"scripts/net_policy.py": "net_policy.py", ".claude/autopilot/config.json": "config.json",
                   "scripts/test_analysis_guards.py": "test_analysis_guards.py", "scripts/mutations/autopilot_mutations.py": "main_autopilot_mutations.py"}
DEST_NAMES = {".github/workflows/verify.yml": "verify.yml", "scripts/verify_ci.py": "verify_ci.py",
              "scripts/mutations/run_mutations.py": "run_mutations.py", "scripts/mutations/known_survivors.json": "known_survivors.json"}

BOT_HOST_PARTS = ("bot.com.tw",)
# Chrome 自己的背景連線（更新、安全瀏覽、字型…）：列出來、不算紅。這份名單在 main 上才算數（算保護範圍）。
BROWSER_NOISE = [r"(^|\.)google\.com$", r"(^|\.)googleapis\.com$", r"(^|\.)gstatic\.com$", r"(^|\.)gvt1\.com$", r"(^|\.)gvt2\.com$",
                 r"(^|\.)googleusercontent\.com$", r"(^|\.)doubleclick\.net$", r"(^|\.)chromium\.org$", r"(^|\.)google-analytics\.com$",
                 r"(^|\.)googlezip\.net$"]
GITHUB_HOSTS = [r"(^|\.)github\.com$", r"(^|\.)githubusercontent\.com$", r"(^|\.)githubassets\.com$", r"(^|\.)github\.io$"]
LOOPBACK_RE = re.compile(r"^(127\.|::1$|localhost$|localhost\.|0\.0\.0\.0$|::$|lockdown-selftest\.invalid$)")
IPTABLES_PREFIX = "IWEGRESS "
TEST_USER = "iwtest"
SELFTEST_IP = "1.1.1.1"                        # 封鎖之後故意連一次，確認真的連不出去；這一筆不算進「被擋的 IP」
# 2026-10-06 裁決：系統層封鎖（IPv4 與 IPv6 都要）缺了，那一次驗收就是紅；對策是重跑，不是接受比較弱的證據。
# 為了少一點誤紅：建立測試用的使用者與設防火牆那幾步，逾時從 120 秒放寬到 300 秒，失敗（含逾時）之後自動再試一次。
LOCKDOWN_STEP_TIMEOUT = 300
LOCKDOWN_TRIES = 2
LOCKDOWN_RED = "封鎖沒設成"                    # 紅的原因用這幾個字開頭＝封鎖本身沒設成，不是測試紅
# 2026-10-06 Codex 的審查意見（P0）：原本整個 checkout 與結果資料夾對所有使用者可寫，而分支上的測試就是用測試用的使用者跑的——
# 測試可以把受測的程式改掉，也可以留一個背景程序事後覆寫 tests.json／compare.json／突變結果，讓判定對著原來的 commit 出綠燈。
# 現在：checkout 與結果資料夾對測試用的使用者唯讀；它只寫得到結果資料夾底下的兩個資料夾（對外請求的紀錄、它那一邊交出來的原始結果）；
# 每一個會執行分支程式碼的步驟跑完，由原本的使用者停掉它留下的程序、把結果收進它寫不到的地方、再核對 checkout 跟 commit 一模一樣。
INNER_DIR = "inner"                            # 測試用的使用者那一邊交出來的原始結果先放這裡
SEAL_FILE = "seal.json"                        # 每一步收好之後的紀錄（收了哪些檔、checkout 有沒有被動過）
SEAL_MAX_BYTES = 64 * 1024 * 1024              # 一個結果檔最多收這麼大（超過就不收，那一步會因為缺結果而紅）

SITECUSTOMIZE = r'''# iw-verify：第 3 層封鎖。所有 Python 程序（含測試開的子程序）一啟動就載入；記下每一個對外的名稱與連線，然後拒絕。
import io, json, os, socket, time
_LOG = os.environ.get("IW_EGRESS_LOG")
def _ok(host):
    h = str(host or "").strip().lower().rstrip(".")
    return (h == "" or h.startswith("127.") or h in ("::1", "localhost", "0.0.0.0", "::", "localhost.localdomain") or h.endswith(".localhost"))
def _log(kind, target):
    if not _LOG:
        return
    try:
        with io.open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "pid": os.getpid(), "kind": kind, "target": str(target)[:200]}) + "\n")
    except Exception:
        pass
_gai = socket.getaddrinfo
def _getaddrinfo(host, *a, **k):
    if not _ok(host):
        _log("dns", host)
        raise socket.gaierror(-2, "iw-verify: outbound blocked (%s)" % host)
    return _gai(host, *a, **k)
socket.getaddrinfo = _getaddrinfo
def _check_addr(addr):
    if isinstance(addr, tuple) and addr and not _ok(addr[0]):
        _log("connect", "%s:%s" % (addr[0], addr[1] if len(addr) > 1 else ""))
        raise OSError("iw-verify: outbound blocked (%s)" % (addr[0],))
_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex
def _c(self, addr):
    _check_addr(addr)
    return _connect(self, addr)
def _cx(self, addr):
    _check_addr(addr)
    return _connect_ex(self, addr)
socket.socket.connect, socket.socket.connect_ex = _c, _cx
'''

CHROME_WRAPPER = '''#!/bin/sh
# iw-verify：第 2 層封鎖。所有名稱解析一律失敗（MAP * ~NOTFOUND），同時把 Chrome 查過的名稱記進 netlog。
%(pre)sexec "%(chrome)s" %(extra)s--host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1" \\
  --log-net-log="%(egress)s/chrome-netlog-$$.json" --net-log-capture-mode=Default "$@"
'''
# 瀏覽器在測試用的使用者底下的幾種開法：依序試，第一個開得起來的就用，每一種的結果都寫進紀錄。
# own-dirs：設定、快取、當機報告的資料夾都指到那個使用者自己寫得到的地方（不靠繼承來的環境變數）。
CHROME_OWN_DIRS = ('d="${HOME:-/tmp}/.iw-chrome"; mkdir -p "$d/config" "$d/cache" "$d/crash" 2>/dev/null\n'
                   'export XDG_CONFIG_HOME="$d/config" XDG_CACHE_HOME="$d/cache" BREAKPAD_DUMP_LOCATION="$d/crash"\n')
CHROME_VARIANTS = [("plain", "", ""), ("own-dirs", CHROME_OWN_DIRS, ""), ("no-sandbox", "", "--no-sandbox "),
                   ("own-dirs+no-sandbox", CHROME_OWN_DIRS, "--no-sandbox ")]


def chrome_wrapper_text(chrome, egress, variant="plain"):
    pre, extra = dict((v[0], (v[1], v[2])) for v in CHROME_VARIANTS)[variant]
    return CHROME_WRAPPER % {"chrome": chrome, "egress": egress, "pre": pre, "extra": extra}


# ---------------------------------------------------------------- 小工具

def run(cmd, cwd=None, timeout=120, env=None, inp=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, env=env, input=inp)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except Exception as e:                                          # noqa: B902
        return 99, "執行失敗：%r" % (e,)


def sudo(cmd, timeout=120):
    return run(["sudo", "-n"] + list(cmd), timeout=timeout)


def sudo_retry(cmd, good=None, retried=None, label=None):
    """封鎖用的步驟：逾時放寬到 LOCKDOWN_STEP_TIMEOUT 秒；失敗（含逾時）就再試，總共最多 LOCKDOWN_TRIES 次。
    good(rc, 輸出)＝這樣算成功（預設看結束碼）。有再試就把 label 記進 retried。回傳最後一次的 (rc, 輸出)。"""
    good = good or (lambda rc, o: rc == 0)
    rc, o = 99, ""
    for attempt in range(1, LOCKDOWN_TRIES + 1):
        if attempt > 1 and retried is not None:
            retried.append(label or " ".join(cmd[:2]))
        rc, o = sudo(cmd, timeout=LOCKDOWN_STEP_TIMEOUT)
        if good(rc, o):
            break
    return rc, o


def user_made(rc, o):
    """useradd 算不算成功：結束碼 0，或它說「已經有了」（再試的那一次會看到這個：逾時的那一次其實建成了）。"""
    return rc == 0 or "already exists" in (o or "")


def add_rule(table, rule, retried=None):
    """加一條防火牆規則；失敗（含逾時）就再試一次。再試之前先問那條規則是不是其實已經在了（逾時的那一次可能有加成），免得同一條加兩次。"""
    check = ["-C", rule[1]] + list(rule[3:] if rule[0] == "-I" else rule[2:])     # -I 鏈名 位置 …／-A 鏈名 … → -C 鏈名 …
    for attempt in range(1, LOCKDOWN_TRIES + 1):
        if attempt > 1:
            if retried is not None:
                retried.append("%s %s" % (table, rule[rule.index("-j") + 1]))
            if sudo([table] + check, timeout=LOCKDOWN_STEP_TIMEOUT)[0] == 0:
                return True
        if sudo([table] + list(rule), timeout=LOCKDOWN_STEP_TIMEOUT)[0] == 0:
            return True
    return False


def git(repo, args, timeout=60):
    p = subprocess.run(["git"] + list(args), cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace")


# 「main 在哪裡」一律用全名。git 解析短名（origin/main）的順序是標籤 → 本機分支 → 遠端的記號；執行機會把標籤全部抓下來，
# 所以只要有人推一個名字就叫 origin/main 的標籤，短名拿到的就是它——「main 上的驗收程式、白名單、已知例外、突變清單」都會被換掉。
# 2026-10-06 審查代理指出、Cowork 裁決在 P2 修。流程檔傳進來的已經是全名；這裡再保一層：傳短名也一律當成遠端的記號。
MAIN_REF = "refs/remotes/origin/main"


def full_main_ref(ref):
    """--main-ref 的值換成全名：已經是 refs/ 開頭的照用；其他的（例如 origin/main）一律當成 refs/remotes/ 底下的。空的用預設值。"""
    ref = (ref or "").strip()
    if not ref:
        return MAIN_REF
    return ref if ref.startswith("refs/") else "refs/remotes/" + ref


def git_show(repo, ref, path):
    """ref 那個版本的檔案內容；沒有這個檔回 None。"""
    rc, out = git(repo, ["show", "%s:%s" % (ref, path)])
    return out if rc == 0 else None


def git_blob(repo, ref, path):
    rc, out = git(repo, ["rev-parse", "-q", "--verify", "%s:%s" % (ref, path)])
    return out.strip() if rc == 0 and out.strip() else None


def read_json(path, default=None):
    try:
        with io.open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                              # noqa: B902
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


def write_text(path, text, mode=None):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    if mode is not None:
        try:
            os.chmod(path, mode)
        except Exception:                                          # noqa: B902
            pass


def glob_match(rel, patterns):
    """跟 .claude/hooks/iw_common.glob_match 一樣的規則（這裡不 import hook，驗收機要能單獨跑）。"""
    import fnmatch
    rel = rel.lower()
    for pat in patterns:
        pat = pat.lower()
        if pat.endswith("/**"):
            base = pat[:-3]
            if rel == base or rel.startswith(base + "/"):
                return True
        elif fnmatch.fnmatchcase(rel, pat):
            return True
    return False


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def is_linux():
    """系統層的封鎖（另一個使用者、防火牆、檔案權限）只在 Linux 的執行機上做。獨立成一個函式，離線測試才換得掉。"""
    return sys.platform.startswith("linux")


def find_chrome():
    forced = os.environ.get("IW_BROWSER")
    if forced and os.path.exists(forced):
        return forced
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        p = shutil.which(name)
        if p:
            return p
    return None


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- 1. 取驗收程式

def cmd_extract_verifier(a):
    repo, dest = os.path.abspath(a.repo), os.path.abspath(a.dest)
    os.makedirs(dest, exist_ok=True)
    first_time = git_blob(repo, a.main_ref, "scripts/verify_ci.py") is None
    source = "branch" if first_time else "main"
    files = {}
    for rel, name in list(DEST_NAMES.items()) + list(EXTRA_FROM_MAIN.items()):
        text = None if first_time and rel in DEST_NAMES else git_show(repo, a.main_ref, rel)
        where = "main"
        if text is None:
            p = os.path.join(repo, *rel.split("/"))
            if os.path.exists(p):
                with io.open(p, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                where = "branch"
        if text is None:
            files[rel] = "missing"
            continue
        write_text(os.path.join(dest, name), text)
        files[rel] = where
    write_json(os.path.join(dest, "verifier-source.json"), {"source": source, "main_ref": a.main_ref, "files": files, "at": now_iso()})
    print("dir=%s" % dest)
    print("source=%s" % source)
    return 0


# ---------------------------------------------------------------- 2. 封鎖

def set_firewall(user, retried=None):
    """設防火牆：只擋 user 的對外封包（loopback 放行；其餘先記錄、再丟掉），IPv4 與 IPv6 各設一份。回傳 lockdown.json 裡 iptables 那一塊。"""
    accept = ["-I", "OUTPUT", "1", "-m", "owner", "--uid-owner", user, "-o", "lo", "-j", "ACCEPT"]
    log = ["-A", "OUTPUT", "-m", "owner", "--uid-owner", user, "-j", "LOG", "--log-prefix", IPTABLES_PREFIX]
    drop = ["-A", "OUTPUT", "-m", "owner", "--uid-owner", user, "-j", "DROP"]
    applied = {"iptables": [], "ip6tables": []}
    for table in ("iptables", "ip6tables"):
        if add_rule(table, accept, retried):
            applied[table].append(accept)
            if add_rule(table, log, retried):                       # 記錄被擋的封包（拿不到 LOG 模組就只擋不記）
                applied[table].append(log)
            if add_rule(table, drop, retried):
                applied[table].append(drop)
    ok4 = accept in applied["iptables"] and drop in applied["iptables"]
    ok6 = accept in applied["ip6tables"] and drop in applied["ip6tables"]
    return {"ipv4": ok4, "ipv6": ok6, "applied": applied, "log": log in applied["iptables"]}


def isolation_selftest(repo, out, user):
    """用測試用的使用者實際找一次：受測的 checkout 裡、結果資料夾裡（放行的那兩個資料夾除外），有沒有任何它寫得到的東西。
    回傳 {"checkout": "readonly" 或 "WRITABLE", "results": 同上, "scratch": "writable" 或 "NOT writable"}。找的過程出錯也算寫得到（寧可紅）。"""
    res = {}
    rc, o = sudo(["-u", user, "find", repo, "-writable", "-print", "-quit"], timeout=LOCKDOWN_STEP_TIMEOUT)
    res["checkout"] = "readonly" if (rc == 0 and not (o or "").strip()) else "WRITABLE"
    rc, o = sudo(["-u", user, "find", out, "(", "-path", os.path.join(out, "egress"), "-o", "-path", os.path.join(out, INNER_DIR), ")", "-prune",
                  "-o", "-writable", "-print", "-quit"], timeout=LOCKDOWN_STEP_TIMEOUT)
    res["results"] = "readonly" if (rc == 0 and not (o or "").strip()) else "WRITABLE"
    ok = all(sudo(["-u", user, "test", "-w", os.path.join(out, d)], timeout=30)[0] == 0 for d in ("egress", INNER_DIR))
    res["scratch"] = "writable" if ok else "NOT writable"
    return res


def read_plain(path, limit=None):
    """只讀一般的檔：捷徑（symlink）、資料夾、裝置一律不讀——測試用的使用者寫得到的資料夾裡，什麼都可能出現。讀不到、太大回 None。"""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        if limit is not None and os.path.getsize(path) > limit:
            return None
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:                                              # noqa: B902
        return None


def stop_leftovers(lock):
    """把測試用的使用者還在跑的程序全部停掉（測試可以留一個背景程序，等這一步結束之後再回頭動手腳）。
    回傳有沒有程序被停掉；沒有那個使用者（沒有系統層）回 None。"""
    user = (lock or {}).get("user")
    if not user:
        return None
    rc, _o = sudo(["pkill", "-KILL", "-u", user], timeout=60)       # 0＝有程序被停掉；1＝本來就沒有
    return rc == 0


def clear_inner(out, lock):
    """一個會執行分支程式碼的步驟開始之前，把測試用的使用者寫得到的那個結果資料夾清空。
    上一個步驟（或它留下的程序）放在那裡的檔，不可以被當成這一步交出來的結果——例如這一步的程序還沒寫出結果就結束了。回傳那個資料夾。"""
    inner = os.path.join(out, INNER_DIR)
    os.makedirs(inner, exist_ok=True)
    stop_leftovers(lock)
    if (lock or {}).get("user"):
        sudo(["find", inner, "-mindepth", "1", "-delete"], timeout=120)     # 資料夾是那個使用者的：要用 sudo 才清得掉
    else:
        for name in os.listdir(inner):
            p = os.path.join(inner, name)
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
            except Exception:                                      # noqa: B902
                pass
    return inner


def checkout_changes(repo):
    """受測的 checkout 跟 commit 比，改了、多了、少了哪些檔（git status；被 .gitignore 忽略的不算）。查不出來回 None。"""
    try:
        rc, o = git(repo, ["status", "--porcelain", "--untracked-files=all"])
    except Exception:                                              # noqa: B902
        return None
    if rc != 0:
        return None
    return [line.rstrip() for line in o.splitlines() if line.strip()]


def seal_stage(out, lock, repo, stage, names):
    """一個會執行分支程式碼的步驟跑完之後，由原本的（可信的）使用者做三件事，記進 seal.json：
    1. 停掉測試用的使用者留下來的程序；
    2. 把那一步交出來的結果檔，從它寫得到的資料夾收進它寫不到的結果資料夾（之後它再也改不到；捷徑、太大的檔不收）；
    3. 核對受測的 checkout 跟 commit 一模一樣——被改過就表示驗的內容不是這個 commit。
    判定時哪一步沒有這份紀錄、或 checkout 被動過，就是紅。"""
    stopped = stop_leftovers(lock)
    inner = os.path.join(out, INNER_DIR)
    if (lock or {}).get("user"):
        sudo(["chmod", "-R", "a+rX", inner], timeout=120)           # 它寫的檔，別人預設不一定讀得到
    sealed, missing = [], []
    for name in names:
        dst = os.path.join(out, name)
        try:
            if os.path.lexists(dst):
                os.remove(dst)                                      # 上一次留下來的同名結果不沿用：這一步沒交出來，就是沒有
        except Exception:                                          # noqa: B902
            pass
        text = read_plain(os.path.join(inner, name), SEAL_MAX_BYTES)
        if text is None:
            missing.append(name)
            continue
        write_text(dst, text)
        sealed.append(name)
    changes = checkout_changes(repo)
    base = (lock or {}).get("tree_changes_at_lockdown") or []
    new = None if changes is None else [c for c in changes if c not in base]
    try:
        rc, head = git(repo, ["rev-parse", "HEAD"])
    except Exception:                                              # noqa: B902
        rc, head = 1, ""
    rec = {"sealed": sealed, "missing": missing, "leftovers_stopped": stopped, "tree_clean": new == [],
           "tree_changes": None if new is None else new[:20], "head": head.strip() if rc == 0 else None, "at": now_iso()}
    seal = read_json(os.path.join(out, SEAL_FILE), {}) or {}
    seal.setdefault("stages", {})[stage] = rec
    write_json(os.path.join(out, SEAL_FILE), seal)
    if new:
        print("注意：受測的 checkout 在「%s」這一步之後跟 commit 不一樣（%d 處）——判定會是紅。" % (stage, len(new)))
    return rec


def firewall_complete(ipt):
    """IPv4 與 IPv6 的規則都設成了，才算有系統層（2026-10-06 裁決；原本只看 IPv4）。"""
    ipt = ipt or {}
    return ipt.get("ipv4") is True and ipt.get("ipv6") is True


def cmd_lockdown(a):
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    egress = os.path.join(out, "egress")
    os.makedirs(egress, exist_ok=True)
    inner = os.path.join(out, INNER_DIR)
    os.makedirs(inner, exist_ok=True)
    info = {"started_at": now_iso(), "layers": {"python": "sitecustomize", "browser": None, "ip": None}, "user": None, "notes": [],
            "browser": None, "selftest": {}, "tree_changes_at_lockdown": checkout_changes(os.path.abspath(a.repo))}
    pyguard = os.path.join(out, "pyguard")
    write_text(os.path.join(pyguard, "sitecustomize.py"), SITECUSTOMIZE)
    chrome = find_chrome()
    if chrome:
        wrapper = os.path.join(out, "chrome-wrapper.sh")
        write_text(wrapper, chrome_wrapper_text(chrome, egress), mode=0o755)
        info["browser"] = wrapper
        info["chrome"] = chrome
        info["layers"]["browser"] = "host-resolver-rules+netlog"
    else:
        info["notes"].append("找不到 Chrome：瀏覽器那幾組會紅（這一組刻意不跳過）")
    # Python 層自我測試：解析一個不存在的名稱，必須失敗、而且被記下來
    env = dict(os.environ, PYTHONPATH=pyguard + os.pathsep + os.environ.get("PYTHONPATH", ""), IW_EGRESS_LOG=os.path.join(egress, "python.jsonl"))
    rc, _o = run([sys.executable, "-c", "import socket; socket.getaddrinfo('lockdown-selftest.invalid', 80)"], env=env, timeout=30)
    logged = os.path.exists(env["IW_EGRESS_LOG"]) and "lockdown-selftest.invalid" in io.open(env["IW_EGRESS_LOG"], encoding="utf-8").read()
    info["selftest"]["python"] = "blocked+logged" if (rc != 0 and logged) else "NOT blocked"
    if info["selftest"]["python"] != "blocked+logged":
        info["layers"]["python"] = None
        info["notes"].append("Python 層的自我測試沒過")
    # 第 1 層：另一個使用者＋iptables（Linux、免密碼 sudo 才有）。只擋那個使用者的對外封包——
    # 直接把整台執行機的對外連線封掉，會弄斷它自己跟 GitHub 的連線（紀錄傳不回去、工作可能被判失聯）。
    if is_linux():
        retried = info["retried"] = []                              # 哪幾步第一次沒成、再試了一次（寫進結果，看得出這台執行機當時慢不慢）
        slow = LOCKDOWN_STEP_TIMEOUT
        rc, o = sudo_retry(["-v"], retried=retried, label="sudo -v")
        if rc != 0:
            info["notes"].append("沒有免密碼 sudo：沒有系統層的封鎖")
        else:
            rc, o = sudo_retry(["useradd", "-m", "-s", "/bin/bash", TEST_USER], good=user_made, retried=retried, label="建立使用者 %s" % TEST_USER)
            if user_made(rc, o):
                info["user"] = TEST_USER
                home = "/tmp/%s-home" % TEST_USER
                repo = os.path.abspath(a.repo)
                # 受測的 checkout 與結果資料夾：那個使用者讀得到、改不到。它只寫得到結果資料夾底下的兩個資料夾（對外請求的紀錄、它交出來的原始結果）。
                sudo(["chmod", "-R", "a+rX,go-w", repo], timeout=slow)
                sudo(["chmod", "-R", "a+rX,go-w", out], timeout=slow)
                sudo(["chown", "-R", TEST_USER, egress, inner], timeout=slow)
                sudo(["chmod", "-R", "u+rwX", egress, inner], timeout=slow)
                for base in (repo, out, HERE):                      # 讓那個使用者走得到倉庫、結果資料夾與驗收程式（上層資料夾預設別人進不去）
                    d = os.path.dirname(base)
                    while d and d != os.path.dirname(d):
                        sudo(["chmod", "o+x", d], timeout=slow)
                        d = os.path.dirname(d)
                sudo(["chmod", "-R", "a+rX", HERE], timeout=slow)
                sudo(["mkdir", "-p", home], timeout=slow)
                sudo(["chown", "-R", TEST_USER, home], timeout=slow)
                # 倉庫是別的使用者的：git 預設會拒絕（dubious ownership）。只對這個測試用的使用者放行。
                sudo(["-u", TEST_USER, "env", "HOME=" + home, "git", "config", "--global", "--add", "safe.directory", "*"], timeout=slow)
                ipt = info["iptables"] = set_firewall(TEST_USER, retried)
                ok4, ok6 = ipt["ipv4"], ipt["ipv6"]
                if firewall_complete(ipt):
                    info["layers"]["ip"] = "iptables+ip6tables(uid-owner %s%s)" % (TEST_USER, "" if ipt["log"] else "；只擋不記")
                    rc, o = sudo(["-u", TEST_USER, "curl", "-sS", "--max-time", "5", "http://%s/" % SELFTEST_IP], timeout=30)
                    info["selftest"]["ip"] = "blocked" if rc != 0 else "NOT blocked"
                    # 這一次自我測試在核心的紀錄裡留下幾筆：現在就數好。收集的時候只扣掉這幾筆，多出來的都算受測的程式連的。
                    info["selftest"]["ip_packets"] = selftest_packets(blocked_packets())
                    if rc == 0:
                        info["layers"]["ip"] = None
                        info["notes"].append("系統層自我測試沒過：以 %s 連 %s 竟然成功" % (TEST_USER, SELFTEST_IP))
                else:
                    info["notes"].append("防火牆規則沒有設成（IPv4：%s；IPv6：%s）：沒有系統層的封鎖" % ("有" if ok4 else "沒有", "有" if ok6 else "沒有"))
                # 隔離的自我測試：用那個使用者實際找一次，checkout 與結果資料夾裡有沒有它寫得到的東西
                info["selftest"].update(isolation_selftest(repo, out, TEST_USER))
                for key, what in (("checkout", "受測的 checkout"), ("results", "結果資料夾")):
                    if info["selftest"].get(key) != "readonly":
                        info["notes"].append("隔離的自我測試沒過：測試用的使用者寫得到%s裡的東西" % what)
                if chrome:                                          # 瀏覽器在那個使用者底下開不開得起來：幾種開法依序試，第一個成功的就用
                    probe = ["-u", TEST_USER, "env", "HOME=" + home, info["browser"], "--headless=new", "--disable-gpu", "--no-first-run",
                             "--user-data-dir=/tmp/%s-probe" % TEST_USER, "--dump-dom", "about:blank"]
                    info["browser_probe"], info["selftest"]["browser"] = [], "failed"
                    for name, _pre, _extra in CHROME_VARIANTS:
                        write_text(info["browser"], chrome_wrapper_text(chrome, egress, name), mode=0o755)
                        rc, o = sudo(probe, timeout=90)
                        ok = rc == 0 and "<html" in o.lower()
                        info["browser_probe"].append({"variant": name, "rc": rc, "ok": ok, "output": "" if ok else o[-600:]})
                        if ok:
                            info["selftest"]["browser"] = "ok" if name == "plain" else "ok(%s)" % name
                            info["browser_variant"] = name
                            if name != "plain":
                                info["notes"].append("瀏覽器在測試用的使用者底下要用「%s」的開法才開得起來" % name)
                            break
                    else:                                           # 都開不起來：留下診斷（那個使用者是誰、家在哪、寫不寫得進去），瀏覽器那幾組會紅
                        write_text(info["browser"], chrome_wrapper_text(chrome, egress), mode=0o755)
                        rc, o = sudo(["-u", TEST_USER, "env", "HOME=" + home, "sh", "-c",
                                      'id; echo "HOME=$HOME"; env | grep "^XDG\\|^TMP\\|^DBUS"; ls -ld "$HOME" /tmp; '
                                      'mkdir -p "$HOME/.config/google-chrome/Crash Reports" && echo mkdir-ok; ls -la "$HOME"'], timeout=30)
                        info["browser_diag"] = o[-1500:]
                        info["notes"].append("瀏覽器在測試用的使用者底下開不起來（%d 種開法都不行；細節在 lockdown.json 的 browser_probe）" % len(CHROME_VARIANTS))
            else:
                info["notes"].append("建不出使用者 %s：%s" % (TEST_USER, o[-200:]))
    else:
        info["notes"].append("不是 Linux：只有 Python 與瀏覽器兩層")
    layers = info["layers"]
    n = sum(1 for k in ("python", "browser", "ip") if layers.get(k))
    info["level"] = "%d/3" % n
    write_json(os.path.join(out, "lockdown.json"), info)
    print("封鎖層級 %s：%s" % (info["level"], "；".join("%s=%s" % (k, v or "沒有") for k, v in layers.items())))
    for note in info["notes"]:
        print("  注意：" + note)
    if info.get("retried"):
        print("  第一次沒成、再試了一次的步驟：" + "、".join(info["retried"]))
    for p in info.get("browser_probe") or []:
        print("  瀏覽器開法 %s：%s%s" % (p["variant"], "開得起來" if p["ok"] else "開不起來（rc=%s）" % p["rc"],
                                    "" if p["ok"] else "\n    " + p["output"].strip().replace("\n", "\n    ")))
    if info.get("browser_diag"):
        print("  診斷：\n    " + info["browser_diag"].strip().replace("\n", "\n    "))
    return 0


# ---------------------------------------------------------------- 3. 跑全套

def locked_command(out, lock, inner):
    """把 inner 包成「在封鎖裡跑」的指令：帶上 Python 層的守門與紀錄檔、瀏覽器的包裝，有測試用的使用者就用那個使用者跑。回傳 (cmd, env)。
    凡是會執行分支上程式碼的步驟（全套測試、每一片突變、跟 main 比時載入突變定義）都走這裡。"""
    egress = os.path.join(out, "egress")
    os.makedirs(egress, exist_ok=True)
    env_items = {"PYTHONPATH": os.path.join(out, "pyguard"), "IW_EGRESS_LOG": os.path.join(egress, "python.jsonl"),
                 "PYTHONIOENCODING": "utf-8", "PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "TMPDIR": "/tmp",
                 "IW_TEST_NO_SIDE_EFFECTS": "1"}
    for k in ("LD_LIBRARY_PATH", "pythonLocation", "Python_ROOT_DIR", "Python3_ROOT_DIR"):      # setup-python 裝的 Python 要靠這幾個才找得到自己的函式庫
        if os.environ.get(k):
            env_items[k] = os.environ[k]
    browser = (lock or {}).get("browser") or find_chrome()
    if browser:
        env_items["IW_BROWSER"] = browser
    user = (lock or {}).get("user")
    if user:
        home = "/tmp/%s-home" % user
        env_items["HOME"] = home
        sudo(["mkdir", "-p", home])
        sudo(["chown", "-R", user, home])
        return ["sudo", "-n", "-u", user, "-H", "env"] + ["%s=%s" % (k, v) for k, v in env_items.items()] + list(inner), None
    return list(inner), dict(os.environ, **env_items)


def cmd_run_tests(a):
    out = os.path.abspath(a.out)
    repo = os.path.abspath(a.repo)
    lock = read_json(os.path.join(out, "lockdown.json"), {}) or {}
    user = lock.get("user")
    inner_dir = clear_inner(out, lock)                              # 測試那一邊只寫得到這裡（開始前先清空）；跑完由這一邊收進結果資料夾
    inner = [sys.executable, "-X", "utf8", "-W", "ignore", os.path.join(HERE, "verify_ci.py"), "run-tests-inner", "--repo", repo,
             "--out", os.path.join(inner_dir, "tests.json"), "--log", os.path.join(inner_dir, "tests-verbose.txt")]
    cmd, env = locked_command(out, lock, inner)
    print("跑全套測試（%s）…" % ("使用者 %s、封鎖層級 %s" % (user, lock.get("level")) if user else "封鎖層級 %s" % lock.get("level", "?")))
    t0 = time.time()
    rc, o = run(cmd, cwd=repo, timeout=a.timeout, env=env)
    print(safe_tail(o, 4000))
    seal_stage(out, lock, repo, "run-tests", ["tests.json", "tests-verbose.txt"])
    res = read_json(os.path.join(out, "tests.json"), {}) or {}
    print("Ran %s tests in %.0fs — %s" % (res.get("ran"), time.time() - t0, "OK" if res.get("ok") else "FAILED"))
    return 0 if res.get("ok") else (rc or 1)


class _Sink(object):
    def __init__(self):
        self.passed, self.failed, self.errors, self.skipped, self.unexpected, self.expected = [], [], [], [], [], 0


def _result_class(sink):
    import unittest

    class R(unittest.TextTestResult):
        def addSuccess(self, test):
            super(R, self).addSuccess(test)
            sink.passed.append(test.id())

        def addFailure(self, test, err):
            super(R, self).addFailure(test, err)
            sink.failed.append(test.id())

        def addError(self, test, err):
            super(R, self).addError(test, err)
            sink.errors.append(test.id())

        def addSkip(self, test, reason):
            super(R, self).addSkip(test, reason)
            sink.skipped.append({"name": test.id(), "reason": str(reason)[:200]})

        def addExpectedFailure(self, test, err):
            super(R, self).addExpectedFailure(test, err)
            sink.expected += 1

        def addUnexpectedSuccess(self, test):
            super(R, self).addUnexpectedSuccess(test)
            sink.unexpected.append(test.id())
    return R


def cmd_run_tests_inner(a):
    import unittest
    repo = os.path.abspath(a.repo)
    os.chdir(repo)
    scripts = os.path.join(repo, "scripts")
    sys.path.insert(0, scripts)
    loader = unittest.defaultTestLoader
    suite = loader.discover("scripts", pattern="test_*.py", top_level_dir="scripts")
    defined = suite.countTestCases()
    sink = _Sink()
    t0 = time.time()
    os.makedirs(os.path.dirname(os.path.abspath(a.log)), exist_ok=True)
    with io.open(a.log, "w", encoding="utf-8", errors="replace", newline="\n") as log:
        runner = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=_result_class(sink))
        result = runner.run(suite)
    res = {"defined": defined, "ran": result.testsRun, "passed": len(sink.passed), "failed": sorted(sink.failed), "errors": sorted(sink.errors),
           "skipped": sorted(sink.skipped, key=lambda s: s["name"]), "expected_failures": sink.expected, "unexpected_successes": sorted(sink.unexpected),
           "seconds": round(time.time() - t0, 1), "python": sys.version.split()[0], "platform": sys.platform, "ok": result.wasSuccessful(),
           "browser": os.environ.get("IW_BROWSER")}
    write_json(a.out, res)
    return 0 if result.wasSuccessful() else 1


# ---------------------------------------------------------------- 4. 解除封鎖、收集對外請求

def parse_python_log(text):
    hosts, events = {}, 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        events += 1
        target = str(rec.get("target") or "")
        host = target.rsplit(":", 1)[0] if rec.get("kind") == "connect" and ":" in target and not target.count(":") > 1 else target
        host = host.strip("[]").lower().rstrip(".")
        if host:
            hosts[host] = hosts.get(host, 0) + 1
    return hosts, events


_NETLOG_HOST = re.compile(r'"host"\s*:\s*"([^"\\]+)"')
_NETLOG_URL = re.compile(r'"url"\s*:\s*"(?:https?|wss?)://([^/"\\]+)')          # 只看會連出去的網址；file://、chrome://、data: 不算
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def parse_netlog_hosts(text):
    """Chrome 的 netlog 裡出現過的主機名稱（它想連誰；名稱解析規則會讓它一個都連不到）。"""
    hosts = {}
    for m in list(_NETLOG_HOST.finditer(text or "")) + list(_NETLOG_URL.finditer(text or "")):
        h = m.group(1).strip().lower().rstrip(".")
        h = h.split("@")[-1]
        h = h.rsplit(":", 1)[0] if re.search(r":\d+$", h) else h
        if h and _HOSTNAME.match(h) and ("." in h or h == "localhost"):
            hosts[h] = hosts.get(h, 0) + 1
    return hosts


_IPT_LINE = re.compile(r"IWEGRESS .*?DST=(\S+).*?PROTO=(\S+)(?:.*?DPT=(\d+))?")


def parse_iptables_log(text):
    blocked = {}
    for line in (text or "").splitlines():
        m = _IPT_LINE.search(line)
        if not m:
            continue
        key = (m.group(1), m.group(2), m.group(3) or "")
        blocked[key] = blocked.get(key, 0) + 1
    return [{"ip": k[0], "proto": k[1], "port": k[2], "count": v} for k, v in sorted(blocked.items())]


def blocked_packets():
    """核心的紀錄裡，防火牆擋下並記下的封包（每個目的地一筆、附筆數）。讀不到回空的清單。"""
    rc, o = sudo(["dmesg"], timeout=60)
    if rc != 0:
        rc, o = sudo(["journalctl", "-k", "--no-pager"], timeout=60)
    return parse_iptables_log(o if rc == 0 else "")


def selftest_packets(blocked):
    """這份清單裡，連到自我測試那個位址（SELFTEST_IP 的 80 埠、TCP）的有幾筆。"""
    return sum(int(b.get("count") or 0) for b in blocked if b.get("ip") == SELFTEST_IP and b.get("port") == "80" and b.get("proto") == "TCP")


def cmd_unlock(a):
    out = os.path.abspath(a.out)
    lock = read_json(os.path.join(out, "lockdown.json"), {}) or {}
    egress_dir = os.path.join(out, "egress")
    if lock.get("user"):
        stop_leftovers(lock)                                        # 收集之前先把它留下來的程序停掉
        # 它寫出來的紀錄（瀏覽器的 netlog）別人預設讀不到：只放寬「讀」，而且只放寬紀錄那個資料夾（原本是整個結果資料夾都改成可寫）
        sudo(["chmod", "-R", "a+rX", egress_dir], timeout=120)
    hosts = {}
    py_hosts, py_events = parse_python_log(read_plain(os.path.join(egress_dir, "python.jsonl")) or "")
    for h, n in py_hosts.items():
        hosts.setdefault(h, {"count": 0, "via": []})
        hosts[h]["count"] += n
        if "python" not in hosts[h]["via"]:
            hosts[h]["via"].append("python")
    netlogs = sorted(p for p in glob.glob(os.path.join(egress_dir, "chrome-netlog-*.json")) if not os.path.islink(p))
    for p in netlogs:
        for h, n in parse_netlog_hosts(read_plain(p) or "").items():
            hosts.setdefault(h, {"count": 0, "via": []})
            hosts[h]["count"] += n
            if "browser" not in hosts[h]["via"]:
                hosts[h]["via"].append("browser")
    blocked, counters, selftest_seen = [], None, False
    if (lock.get("layers") or {}).get("ip"):
        # 封鎖時驗收機自己故意連了一次 SELFTEST_IP 的 80 埠（證明「擋下而且記到了」）。那一次留下幾筆，封鎖當下就數好記在 lockdown.json；
        # 這裡只扣掉那幾筆。2026-10-06 Codex 的審查意見：原本把所有連到那個位址的紀錄都當成自我測試丟掉——受測的程式如果也連同一個位址，會一起消失。
        baseline = int((lock.get("selftest") or {}).get("ip_packets") or 0)
        selftest_seen = baseline > 0
        for b in blocked_packets():
            if b["ip"] == SELFTEST_IP and b["port"] == "80" and b["proto"] == "TCP":
                extra = int(b["count"]) - baseline
                if extra > 0:                                       # 比自我測試多出來的：是受測的程式連的，照樣列進清單（判定會紅）
                    blocked.append(dict(b, count=extra))
                continue
            blocked.append(b)
        rc, counters = sudo(["iptables", "-L", "OUTPUT", "-v", "-n", "-x"], timeout=30)
    for table, rules in sorted(((lock.get("iptables") or {}).get("applied") or {}).items()):   # 還原（倒著刪）
        for r in reversed(rules):
            rr = list(r)
            rr[0] = "-D"
            if rr[1] == "OUTPUT" and rr[2] == "1":
                del rr[2]
            sudo([table] + rr)
    res = {"hosts": hosts, "blocked_ips": blocked, "python_events": py_events, "browser_netlogs": len(netlogs), "layers": lock.get("layers") or {},
           "level": lock.get("level"), "selftest": dict(lock.get("selftest") or {}, ip_logged=selftest_seen), "notes": lock.get("notes") or [],
           "counters": (counters or "")[-2000:], "collected_at": now_iso()}
    write_json(os.path.join(out, "egress.json"), res)
    print("對外請求：%d 個主機名稱、%d 個被擋的 IP（封鎖層級 %s）" % (len(hosts), len(blocked), res["level"]))
    return 0


_GUARDS = []


def main_guards():
    """main 上的隱私掃描器（test_analysis_guards.py，取驗收程式那一步放在這個檔旁邊）。讀不到回 None。只載入一次。"""
    if not _GUARDS:
        try:
            _GUARDS.append(load_module(os.path.join(HERE, "test_analysis_guards.py"), "iw_guards_shared"))
        except Exception:                                          # noqa: B902
            _GUARDS.append(None)
    return _GUARDS[0]


def safe_tail(text, limit):
    """子程序輸出的最後一段，要印進執行紀錄之前先掃：公開倉庫的執行紀錄誰都看得到。命中就不印內容，只說有命中。
    main 上的隱私掃描器讀不到時也不印（2026-10-06 Codex 的審查意見：受測程序的輸出不可以沒掃過就進公開紀錄。
    原本讀不到掃描器時只過通用樣式就印出來；上傳結果那一邊早就是「讀不到就什麼都不帶出去」，這裡對齊）。"""
    tail = (text or "")[-limit:]
    guards = main_guards()
    if guards is None:
        return "（讀不到 main 上的隱私掃描器，這一段輸出沒有印出來；失敗的細節看上傳的結果檔）"
    hits = privacy_scan({"輸出": tail}, guards)
    if hits:
        return "（這一段輸出含不該公開的字串，沒有印出來）"
    return tail


def _read(path):
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:                                              # noqa: B902
        return ""


# ---------------------------------------------------------------- 4b. 上傳之前：只帶必要的、掃過的

# 判定那一段需要的檔。除此之外（完整的詳細輸出、瀏覽器的 netlog、封鎖用的暫存檔與包裝）一律不上傳。
EXPORT_FILES = ("tests.json", "compare.json", "lockdown.json", "egress.json", SEAL_FILE)
EXPORT_FAILURE_CHARS = 20000
NOT_EXPORTED_BUT_SCANNED = ("tests-verbose.txt",)


def failure_excerpt(verbose_text, limit=EXPORT_FAILURE_CHARS):
    """全套測試的詳細輸出裡，只留失敗與錯誤的那幾段（讓人看得出為什麼紅）。通過的那幾百行不帶出去。"""
    blocks = re.split(r"\n={60,}\n", verbose_text or "")
    keep = [b.strip("\n") for b in blocks if re.match(r"\s*(FAIL|ERROR): ", b)]
    return ("\n" + "=" * 70 + "\n").join(keep)[:limit]


def cmd_export(a):
    """每一段在上傳之前做的事：只挑判定需要的檔，逐個過隱私掃描；命中的檔不帶出去，只記「哪個檔命中」。
    2026-10-05 Codex 的審查意見：原本整個結果資料夾先上傳、到最後一段才掃描——就算最後判紅，
    詳細輸出、診斷與瀏覽器的 netlog 也已經留在 artifact 裡 90 天。完整的詳細輸出不上傳，但照樣掃（有命中＝紅）。"""
    out, dest = os.path.abspath(a.out), os.path.abspath(a.export)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    guards = main_guards()
    if guards is None:
        # 讀不到 main 上的隱私掃描器：只剩幾條通用的樣式可用，專案自己的規則掃不到。這時候什麼都不帶出去
        # （2026-10-06 Codex 的審查意見：原本只在報告裡標「讀不到」，檔案照樣交出去上傳，之後判紅也收不回來）。
        write_json(os.path.join(dest, "export.json"), {"exported": [], "dropped": ["（全部）"], "privacy_hits": [], "scanner": "（讀不到）",
                                                      "scanner_missing": True, "at": now_iso()})
        print("讀不到 main 上的隱私掃描器：這一段什麼都沒有帶出去（判定會是紅）。")
        return 1
    names = [n for n in EXPORT_FILES if os.path.exists(os.path.join(out, n))]
    names += sorted(n for n in (os.listdir(out) if os.path.isdir(out) else []) if n.startswith("mut-") and n.endswith(".json"))
    texts = dict((n, _read(os.path.join(out, n))) for n in names)
    verbose = _read(os.path.join(out, "tests-verbose.txt"))
    excerpt = failure_excerpt(verbose)
    if excerpt:
        texts["failures.txt"] = excerpt
    report = {"exported": [], "dropped": [], "privacy_hits": [], "scanner": "main" if guards is not None else "（讀不到）", "at": now_iso()}
    scan_only = dict((n, _read(os.path.join(out, n))) for n in NOT_EXPORTED_BUT_SCANNED)
    for name in sorted(set(texts) | set(scan_only)):
        text = texts.get(name, scan_only.get(name)) or ""
        hits = privacy_scan({name: text}, guards)
        if hits:                                                    # 只記檔名，不記命中的內容
            report["privacy_hits"].append(name)
            if name in texts:
                report["dropped"].append(name)
            continue
        if name in texts:
            write_text(os.path.join(dest, name), text)
            report["exported"].append(name)
    write_json(os.path.join(dest, "export.json"), report)
    print("要上傳的檔：%s" % ("、".join(report["exported"]) or "（沒有）"))
    if report["privacy_hits"]:
        print("含不該公開的字串、沒有帶出去的：%s（判定會是紅）" % "、".join(report["privacy_hits"]))
    return 0


# ---------------------------------------------------------------- 5. 跟 main 比

def test_functions(source):
    """{Class.test_name: 原始碼的雜湊}。靜態計數：跟 loader 數出來的可能差幾條（動態產生的測試），但 main 與分支用同一把尺。"""
    out = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for f in node.body:
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name.startswith("test"):
                    seg = ast.get_source_segment(source, f) or ""
                    out[node.name + "." + f.name] = hashlib.sha1(seg.encode("utf-8")).hexdigest()[:12]
    return out


def collect_tests(sources):
    """sources：{檔名: 內容}。回傳 {檔名::Class.test: 雜湊}。"""
    out = {}
    for name, text in sources.items():
        for k, v in test_functions(text).items():
            out[name + "::" + k] = v
    return out


def diff_items(main_items, branch_items):
    removed = sorted(k for k in main_items if k not in branch_items)
    modified = sorted(k for k in main_items if k in branch_items and main_items[k] != branch_items[k])
    return removed, modified


def mutation_items(defs):
    out = {}
    for m in defs:
        old, new = m[3], m[4]
        key = json.dumps([m[2], old, new], ensure_ascii=False, sort_keys=True)
        out[m[0]] = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return out


def main_test_sources(repo, main_ref):
    rc, out = git(repo, ["ls-tree", "--name-only", main_ref, "scripts/"])
    if rc != 0:
        return None
    srcs = {}
    for line in out.splitlines():
        name = line.strip()
        if re.match(r"^scripts/test_.*\.py$", name):
            text = git_show(repo, main_ref, name)
            if text is not None:
                srcs[name] = text
    return srcs


def branch_test_sources(repo):
    srcs = {}
    for p in sorted(glob.glob(os.path.join(repo, "scripts", "test_*.py"))):
        srcs["scripts/" + os.path.basename(p)] = _read(p)
    return srcs


COMPARE_INNER = "compare-inner.json"


def branch_mutation_part(repo):
    """要載入分支上的突變定義（＝執行分支的程式碼）才算得出來的那一塊：有幾個、每一個的內容雜湊、錨點對不對。
    有封鎖的時候，這一塊在測試用的使用者那一邊算；其餘的比對（git、測試的靜態計數、保護範圍、驗收機本身）都留在原本的使用者這一邊。"""
    defs_path = os.path.join(repo, "scripts", "mutations", "autopilot_mutations.py")
    branch_defs = load_module(defs_path, "iw_defs_branch").MUTATIONS if os.path.exists(defs_path) else []
    runner_path = os.path.join(HERE, "run_mutations.py")
    if not os.path.exists(runner_path):
        runner_path = os.path.join(repo, "scripts", "mutations", "run_mutations.py")
    try:
        problems = load_module(runner_path, "iw_runner").check_anchors(repo, branch_defs) if os.path.exists(runner_path) else ["找不到突變的執行器"]
    except Exception as e:                                          # noqa: B902
        problems = ["核對錨點時出錯：%r" % (e,)]
    return {"mutations_total": len(branch_defs), "items": mutation_items(branch_defs), "mutation_anchor_problems": problems}


def cmd_compare(a):
    repo, out = os.path.abspath(a.repo), os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    lock = read_json(os.path.join(out, "lockdown.json"), None)
    inner_dir = os.path.join(out, INNER_DIR)
    if getattr(a, "inner", False):                                  # 測試用的使用者那一邊：只算要載入分支程式碼的那一塊，寫到它寫得到的資料夾
        os.makedirs(inner_dir, exist_ok=True)
        write_json(os.path.join(inner_dir, COMPARE_INNER), branch_mutation_part(repo))
        return 0
    main_ref = a.main_ref
    rc, main_sha = git(repo, ["rev-parse", main_ref])
    rc2, head_sha = git(repo, ["rev-parse", "HEAD"])
    res = {"main_ref": main_ref, "main_sha": main_sha.strip() if rc == 0 else None, "head_sha": head_sha.strip() if rc2 == 0 else None}
    # 測試（靜態計數：只讀檔、不執行分支的程式碼）
    branch_tests = collect_tests(branch_test_sources(repo))
    main_srcs = main_test_sources(repo, main_ref)
    main_tests = collect_tests(main_srcs) if main_srcs is not None else None
    res["tests_defined"] = len(branch_tests)
    res["tests_defined_main"] = len(main_tests) if main_tests is not None else None
    res["tests_removed"], res["tests_modified"] = diff_items(main_tests or {}, branch_tests)
    # 突變：分支上的定義要載入才讀得到（＝執行分支的程式碼）。有封鎖就交給測試用的使用者那一邊在封鎖裡算，這一邊只收它交出來的結果
    if lock:
        inner = [sys.executable, "-X", "utf8", os.path.join(HERE, "verify_ci.py"), "compare", "--out", out, "--repo", repo,
                 "--main-ref", a.main_ref, "--inner"]
        clear_inner(out, lock)                                      # 上一步留在那個資料夾裡的東西先清掉
        cmd, env = locked_command(out, lock, inner)
        rc, o = run(cmd, cwd=repo, timeout=600, env=env)
        print(safe_tail(o, 4000))
        seal_stage(out, lock, repo, "compare", [COMPARE_INNER])
        part = read_json(os.path.join(out, COMPARE_INNER), None)
        if not isinstance(part, dict) or not isinstance(part.get("items"), dict):
            part = {"mutations_total": 0, "items": {}, "mutation_anchor_problems": ["突變定義那一塊沒有算出來（封鎖裡的那一步沒有交出結果）"]}
    else:
        part = branch_mutation_part(repo)
    main_defs_text = git_show(repo, main_ref, "scripts/mutations/autopilot_mutations.py")
    main_defs = None
    if main_defs_text is not None:                                  # main 上的定義是放行過的程式碼：在這一邊載入
        tmp = os.path.join(out, "main_autopilot_mutations.py")
        write_text(tmp, main_defs_text)
        main_defs = load_module(tmp, "iw_defs_main").MUTATIONS
    branch_items = dict((str(k), str(v)) for k, v in part["items"].items())
    res["mutations_total"] = len(branch_items)
    res["mutations_main"] = len(main_defs) if main_defs is not None else None
    res["mutations_removed"], res["mutations_modified"] = diff_items(mutation_items(main_defs or []), branch_items)
    res["mutation_anchor_problems"] = [str(x) for x in (part.get("mutation_anchor_problems") or [])]
    # 保護範圍（清單用 main 上的 config）
    cfg_text = git_show(repo, main_ref, ".claude/autopilot/config.json") or _read(os.path.join(repo, ".claude", "autopilot", "config.json"))
    try:
        cfg = json.loads(cfg_text) if cfg_text else {}
    except ValueError:
        cfg = {}
    rc, changed = git(repo, ["diff", "--name-only", "%s...HEAD" % main_ref])
    changed = [f for f in changed.splitlines() if f.strip()] if rc == 0 else []
    res["changed_files"] = len(changed)
    res["protected_touched"] = {
        "tier1": [f for f in changed if glob_match(f, (cfg.get("tier1") or {}).get("paths") or [])],
        "self": [f for f in changed if glob_match(f, (cfg.get("selfFiles") or {}).get("paths") or [])],
        "tier2": [f for f in changed if glob_match(f, (cfg.get("tier2") or {}).get("paths") or [])]}
    # 驗收機本身
    vfiles = (cfg.get("verify") or {}).get("files") or VERIFIER_FILES
    changed_v = [f for f in vfiles if git_blob(repo, "HEAD", f) != git_blob(repo, main_ref, f)]
    src = read_json(os.path.join(HERE, "verifier-source.json"), {}) or {}
    res["verifier_first_time"] = git_blob(repo, main_ref, "scripts/verify_ci.py") is None
    res["verifier_source"] = src.get("source") or ("branch" if res["verifier_first_time"] else "main")
    res["verifier_changed_files"] = changed_v
    res["verifier_changed"] = bool(changed_v) or res["verifier_source"] == "branch"
    res["compared_at"] = now_iso()
    write_json(os.path.join(out, "compare.json"), res)
    print("測試 %s（main %s）、突變 %s（main %s）、動到保護範圍 %d 個檔、驗收機%s" % (
        res["tests_defined"], res["tests_defined_main"], res["mutations_total"], res["mutations_main"],
        sum(len(v) for v in res["protected_touched"].values()), "本身有改：" + "、".join(changed_v) if res["verifier_changed"] else "沒改"))
    return 0


# ---------------------------------------------------------------- 6. 突變（一片）

def cmd_mutations(a):
    """跑一片突變。突變會載入分支上的定義、反覆開 unittest 子程序，所以跟全套測試一樣要在封鎖裡跑、對外請求一樣要收集
    （2026-10-05 Codex 的審查意見；原本這一段沒有封鎖）。沒有封鎖紀錄時照樣跑，但判定時這一片算沒有封鎖＝紅。"""
    repo, out = os.path.abspath(a.repo), os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    lock = read_json(os.path.join(out, "lockdown.json"), None)
    runner = os.path.join(HERE, "run_mutations.py")
    known = os.path.join(HERE, "known_survivors.json")
    if not os.path.exists(runner):
        runner = os.path.join(repo, "scripts", "mutations", "run_mutations.py")
    if not os.path.exists(known):
        known = os.path.join(repo, "scripts", "mutations", "known_survivors.json")
    defs = os.path.join(repo, "scripts", "mutations", "autopilot_mutations.py")
    tag = a.shard.replace("/", "-of-")
    inner_dir = clear_inner(out, lock)                              # 突變那一邊只寫得到這裡（開始前先清空）；跑完由這一邊收進結果資料夾
    result_name = "mut-%s.json" % tag
    cmd = [sys.executable, "-X", "utf8", runner, "--defs", defs, "--known", known, "--repo", repo, "--copy", os.path.join(a.copy_dir, "iw-mutcopy-" + tag),
           "--shard", a.shard, "--no-baseline", "--out", os.path.join(inner_dir, result_name), "--timeout", str(a.timeout)]
    browser = (lock or {}).get("browser") or find_chrome()
    if browser:
        cmd += ["--browser", browser]
    if lock is None:
        print("注意：這一片沒有封鎖紀錄（lockdown.json）——突變是在沒有封鎖的情況下跑的，判定時會算紅。")
        rc, o = run(cmd, cwd=repo, timeout=a.timeout * 3, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    else:
        lcmd, env = locked_command(out, lock, cmd)
        print("跑這一片突變（%s、封鎖層級 %s）…" % ("使用者 %s" % lock.get("user") if lock.get("user") else "原本的使用者", lock.get("level")))
        rc, o = run(lcmd, cwd=repo, timeout=a.timeout * 3, env=env)
    print(safe_tail(o, 6000))
    seal_stage(out, lock, repo, "mutations", [result_name])
    return rc


# ---------------------------------------------------------------- 7. 判定、結果檔

def classify_host(host, allowed_hosts):
    host = (host or "").strip().lower().rstrip(".")
    if not host or LOOPBACK_RE.match(host):
        return "loopback"
    if any(part in host for part in BOT_HOST_PARTS):
        return "bot"
    if host in set(h.lower() for h in (allowed_hosts or [])) or any(re.search(p, host) for p in GITHUB_HOSTS):
        return "source"
    if any(re.search(p, host) for p in BROWSER_NOISE):
        return "browser"
    return "unknown"


def judge_egress(raw, allowed_hosts):
    hosts = (raw or {}).get("hosts") or {}
    out = {"bot_hits": 0, "bot_hosts": [], "source_hosts": [], "browser_hosts": [], "unknown_hosts": [], "loopback_hosts": [],
           "blocked_ips": (raw or {}).get("blocked_ips") or [], "layers": (raw or {}).get("layers") or {}, "level": (raw or {}).get("level"),
           "python_events": (raw or {}).get("python_events", 0), "browser_netlogs": (raw or {}).get("browser_netlogs", 0)}
    for host, rec in sorted(hosts.items()):
        kind = classify_host(host, allowed_hosts)
        if kind == "bot":
            out["bot_hits"] += int(rec.get("count") or 0)
            out["bot_hosts"].append(host)
        elif kind == "source":
            out["source_hosts"].append(host)
        elif kind == "browser":
            out["browser_hosts"].append(host)
        elif kind == "unknown":
            out["unknown_hosts"].append(host)
        else:
            out["loopback_hosts"].append(host)
    return out


def merge_egress_raw(raws):
    """各段（全套測試、每一片突變）收集到的對外請求合成一份：主機名稱的次數相加、被擋的 IP 相加。"""
    out = {"hosts": {}, "blocked_ips": [], "python_events": 0, "browser_netlogs": 0, "jobs": 0}
    blocked = {}
    for r in raws:
        if not r:
            continue
        out["jobs"] += 1
        for h, rec in sorted((r.get("hosts") or {}).items()):
            cur = out["hosts"].setdefault(h, {"count": 0, "via": []})
            cur["count"] += int((rec or {}).get("count") or 0)
            for v in (rec or {}).get("via") or []:
                if v not in cur["via"]:
                    cur["via"].append(v)
        for b in r.get("blocked_ips") or []:
            key = (str(b.get("ip") or ""), str(b.get("proto") or ""), str(b.get("port") or ""))
            blocked[key] = blocked.get(key, 0) + int(b.get("count") or 0)
        out["python_events"] += int(r.get("python_events") or 0)
        out["browser_netlogs"] += int(r.get("browser_netlogs") or 0)
    out["blocked_ips"] = [{"ip": k[0], "proto": k[1], "port": k[2], "count": v} for k, v in sorted(blocked.items())]
    return out


def weakest_level(lockdowns):
    """幾段封鎖裡最弱的層級（「2/3」比「3/3」弱）；有一段沒有紀錄就是 0/3。"""
    levels = [str((x or {}).get("level") or "0/3") for x in lockdowns]
    return min(levels) if levels else None


def merge_mutations(shards, known, expected_total=None):
    results, shards_seen = [], []
    for s in shards:
        shards_seen.append(s.get("shard"))
        results += [r for r in (s.get("results") or []) if not str(r.get("id", "")).startswith("基準")]
    ids = sorted(set(r["id"] for r in results))
    red = sorted(r["id"] for r in results if r.get("ok"))
    anchor_errors = sorted(r["id"] for r in results if r.get("error"))
    survivors = sorted(r["id"] for r in results if not r.get("ok") and not r.get("error"))
    known = known or {}
    return {"total_ran": len(ids), "red": len(red), "survivors": survivors, "anchor_errors": anchor_errors,
            "known_exceptions": sorted(k for k in survivors if k in known), "unexplained_survivors": sorted(k for k in survivors if k not in known),
            "shards": shards_seen, "missing": max(0, (expected_total or 0) - len(ids)) if expected_total is not None else 0,
            "seconds": round(sum(float(r.get("seconds") or 0) for r in results), 1)}


def system_layer_gap(lock):
    """這一段的系統層封鎖（另一個使用者＋防火牆）缺了什麼；完整就回 None。
    完整＝有系統層、IPv4 與 IPv6 的規則都設成了、自我測試（以測試用的使用者往外連）真的被擋下。"""
    lock = lock or {}
    if not (lock.get("layers") or {}).get("ip"):
        return "沒有系統層"
    ipt = lock.get("iptables") or {}
    miss = [name for key, name in (("ipv4", "IPv4"), ("ipv6", "IPv6")) if ipt.get(key) is not True]
    if miss:
        return "防火牆少了 %s 的規則" % "、".join(miss)
    if (lock.get("selftest") or {}).get("ip") != "blocked":
        return "系統層的自我測試沒過"
    # 同一個使用者也是「改不到受測的檔與結果檔」那一道的基礎：自我測試要證明它在 checkout 與結果資料夾裡找不到任何寫得到的東西
    if (lock.get("selftest") or {}).get("checkout") != "readonly":
        return "測試用的使用者改得到受測的 checkout"
    if (lock.get("selftest") or {}).get("results") != "readonly":
        return "測試用的使用者改得到結果檔"
    return None


def seal_problems(label, seal, stages):
    """這一段的封存紀錄（seal.json）有沒有問題。stages：這一段一定要有的步驟。回傳原因的清單（空的＝沒問題）。
    少了哪一步的紀錄、那一步交出來的結果檔沒收到、或 checkout 在那一步之後跟 commit 不一樣——都算：驗的內容不能確定就是這個 commit。"""
    out = []
    recs = (seal or {}).get("stages") or {}
    for stage in stages:
        r = recs.get(stage)
        if not isinstance(r, dict):
            out.append("%s：沒有「%s」那一步的封存紀錄（結果檔有沒有收好、受測的檔有沒有被動過，查不到）" % (label, stage))
            continue
        if r.get("tree_clean") is not True:
            what = "、".join(str(x) for x in (r.get("tree_changes") or [])[:5]) or "查不出來"
            out.append("%s：受測的 checkout 在「%s」那一步之後跟 commit 不一樣（%s）——驗的內容不是這個 commit" % (label, stage, what[:200]))
        if r.get("missing"):
            out.append("%s：「%s」那一步沒有交出結果檔（%s）" % (label, stage, "、".join(str(x) for x in r["missing"])[:120]))
    return out


def lockdown_brief(lock, job=None):
    """結果檔裡每一段封鎖的摘要：層級、每一層、自我測試、防火牆 IPv4／IPv6 有沒有設成、缺了什麼、哪幾步再試過。"""
    lock = lock or {}
    ipt = lock.get("iptables") or {}
    return {"job": job or lock.get("job"), "level": lock.get("level"), "layers": lock.get("layers"), "selftest": lock.get("selftest"),
            "ipv4": ipt.get("ipv4"), "ipv6": ipt.get("ipv6"), "system_layer_gap": system_layer_gap(lock),
            "notes": lock.get("notes"), "retried": lock.get("retried") or []}


def judge(tests, egress, cmp_, mut, lockdown, privacy, mut_lockdowns=None, seals=None):
    """回傳紅的原因清單（空的＝綠）。每一條規則在 scripts/test_verify_ci.py 都有「改壞→紅」的對照。
    mut_lockdowns：每一片突變的封鎖紀錄（None＝舊的呼叫方式，不檢查這一項）。
    seals：每一段的封存紀錄，[(這一段的名字, seal.json 的內容, 一定要有的步驟)]（None＝不檢查這一項）。"""
    reasons = []
    tests = tests or {}
    cmp_ = cmp_ or {}
    mut = mut or {}
    egress = egress or {}
    if not tests:
        reasons.append("沒有測試結果（tests.json 不在）")
    else:
        if int(tests.get("ran") or 0) == 0:
            reasons.append("沒有跑到任何測試")
        bad = len(tests.get("failed") or []) + len(tests.get("errors") or [])
        if bad:
            reasons.append("測試有 %d 條紅：%s" % (bad, "、".join((tests.get("failed") or []) + (tests.get("errors") or []))[:300]))
        if tests.get("unexpected_successes"):
            reasons.append("有 %d 條預期失敗的測試竟然過了" % len(tests["unexpected_successes"]))
    if cmp_.get("tests_defined_main") is not None and tests.get("defined", 0) < cmp_["tests_defined_main"]:
        reasons.append("測試數變少：main %d → 這裡 %d" % (cmp_["tests_defined_main"], tests.get("defined", 0)))
    # 「這個 commit 裡定義了幾條測試」是原本的使用者從檔案數出來的（不執行分支的程式碼）；「跑了幾條」是測試那一邊回報的。
    # 回報的比數出來的少＝有測試沒有被跑到（被跳過載入、或回報不實）。
    if tests and cmp_.get("tests_defined") is not None and int(tests.get("ran") or 0) < int(cmp_["tests_defined"]):
        reasons.append("跑的測試（%d 條）比這個 commit 裡定義的（%d 條）少：有測試沒有被跑到" % (int(tests.get("ran") or 0), int(cmp_["tests_defined"])))
    if cmp_.get("mutations_main") is not None and int(cmp_.get("mutations_total") or 0) < cmp_["mutations_main"]:
        reasons.append("突變數變少：main %d → 這裡 %d" % (cmp_["mutations_main"], cmp_.get("mutations_total") or 0))
    if cmp_.get("mutation_anchor_problems"):
        reasons.append("突變的錨點有 %d 個問題：%s" % (len(cmp_["mutation_anchor_problems"]), "；".join(cmp_["mutation_anchor_problems"])[:300]))
    if mut.get("unexplained_survivors"):
        reasons.append("有 %d 個突變沒有紅、也不在 main 的例外清單裡：%s" % (len(mut["unexplained_survivors"]), "、".join(mut["unexplained_survivors"])))
    if mut.get("anchor_errors"):
        reasons.append("突變執行時錨點出錯：%s" % "、".join(mut["anchor_errors"]))
    if mut.get("missing"):
        reasons.append("突變有 %d 個沒跑到（分片沒全部回來？）" % mut["missing"])
    if egress.get("bot_hits"):
        reasons.append("對台銀的請求出現 %d 次（%s）——這個系列對台銀的請求數要是 0" % (egress["bot_hits"], "、".join(egress.get("bot_hosts") or [])))
    if egress.get("source_hosts"):
        reasons.append("測試期間連了資料來源或 GitHub（測試應該全部離線）：%s" % "、".join(egress["source_hosts"]))
    if egress.get("unknown_hosts"):
        reasons.append("測試期間連了名單外的主機：%s" % "、".join(egress["unknown_hosts"]))
    if egress.get("blocked_ips"):                                   # 不是 Python、也不是瀏覽器發的連線（例如測試叫了 curl）：只留下 IP，看不出是誰，一律紅
        reasons.append("系統層擋下了對外連線（測試應該全部離線；這種連線只留下 IP，看不出是連誰）：%s" % "、".join(
            "%s%s／%s×%s" % (b.get("ip"), (":%s" % b.get("port")) if b.get("port") else "", b.get("proto"), b.get("count"))
            for b in egress["blocked_ips"])[:300])
    # 封鎖本身沒設成：原因一律用 LOCKDOWN_RED 開頭，跟「測試紅」分開（2026-10-06 裁決：系統層缺了就紅，取代 10/4 的「不紅、只寫層級」）
    if not ((lockdown or {}).get("layers") or {}).get("python"):
        reasons.append("%s：全套測試那一段的 Python 層的封鎖沒有生效" % LOCKDOWN_RED)
    gap = system_layer_gap(lockdown)
    if gap:
        reasons.append("%s：全套測試那一段沒有完整的系統層封鎖（%s）。這不是測試紅；對策是重跑這一次驗收，不是接受比較弱的證據" % (LOCKDOWN_RED, gap))
    if mut_lockdowns is not None:                                   # 突變也會執行分支上的程式碼：每一片都要在封鎖裡跑
        shards = len(mut.get("shards") or [])
        if len(mut_lockdowns) < shards:
            reasons.append("%s：有 %d 片突變沒有封鎖紀錄（突變也要在封鎖裡跑）" % (LOCKDOWN_RED, shards - len(mut_lockdowns)))
        if any(not ((x or {}).get("layers") or {}).get("python") for x in mut_lockdowns):
            reasons.append("%s：有突變分片的 Python 層封鎖沒有生效" % LOCKDOWN_RED)
        gaps = ["%s（%s）" % ((x or {}).get("job") or "第 %d 片" % (i + 1), system_layer_gap(x)) for i, x in enumerate(mut_lockdowns) if system_layer_gap(x)]
        if gaps:
            reasons.append("%s：有 %d 片突變沒有完整的系統層封鎖：%s。這不是測試紅；對策是重跑這一次驗收，不是接受比較弱的證據"
                           % (LOCKDOWN_RED, len(gaps), "、".join(gaps)[:300]))
    for label, seal, stages in (seals or []):                       # 每一段：結果檔有沒有由原本的使用者收好、受測的 checkout 有沒有被動過
        reasons += seal_problems(label, seal, stages)
    if privacy:
        reasons.append("隱私掃描沒過（含不該公開的字串，或有一段沒有掃描）：%s" % "；".join(privacy)[:300])
    return reasons


# 隱私掃描（跟 scripts/test_autopilot_config.py 的樣式一致；另外加 main 上 test_analysis_guards 的三個掃描器）
_LOCAL_USER = re.compile(r"(?i)[a-z]:[\\/]+users[\\/]+(?!fake\b)[a-z0-9_.-]+")
_LOCAL_ROOT = re.compile(r"(?i)[a-z]:[\\/]+claude_use")
# 像密鑰的東西：格式很明確的那幾種（跟 .claude/hooks/iw_notify.py 的 SECRET_PATTERNS 一樣，有測試釘住）。
# 2026-10-06 Codex 的審查意見：原本只認兩種 GitHub 權杖的開頭。
SECRET_PATTERNS = (
    r"gh[opsur]_[A-Za-z0-9]{20,}",                                  # GitHub 的各種權杖
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b",           # AWS 的金鑰編號
    r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}",                            # Slack 的權杖
    r"hooks\.slack\.com/services/[A-Za-z0-9/]{20,}",                # Slack 的 webhook
    r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",   # JWT
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",                       # 私鑰區塊
    r"\bsk-[A-Za-z0-9_-]{20,}",                                     # sk- 開頭的 API 金鑰
    r"\bAIza[0-9A-Za-z_-]{35}",                                     # Google 的 API 金鑰
    r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}",                      # Stripe 的金鑰
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}",                      # Authorization 標頭裡的權杖
)
_SECRETS = tuple(re.compile(p) for p in SECRET_PATTERNS)
_MAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Unix／macOS 的家目錄路徑（/home/某人、/Users/某人）。2026-10-06 Codex 的審查意見：原本只認 Windows 的兩種寫法。
# 驗收機的結果裡本來就會出現執行機自己的路徑，所以有一份排除清單——只准列 GitHub 執行機固定的帳號與這支程式自己建的測試用使用者，
# 不可以隨手加（這個檔是保護範圍）。磁碟機開頭的路徑維持上面兩條：測試的輸出裡本來就有一看就知道是假的路徑（D:/Fake/…），沒辦法一律擋。
_UNIX_HOME = re.compile(r"(?<![\w.:/~-])/(home|Users)/([^\s/\"'<>|:*?\\,;)\]}]+)")
RUNNER_HOME_PREFIXES = ("/home/runner", "/home/" + TEST_USER)
# 可以出現的電子郵件：確切的系統地址（跟 .claude/hooks/iw_notify.py 的 PUBLIC_MAIL_* 一樣，有測試釘住），加上保留給測試用的網域。
# 原本寫成「地址裡有 noreply 就放行」、@github.com 整個網域也放行（同一條審查意見）。
SYSTEM_MAIL_EXACT = ("noreply@anthropic.com", "noreply@github.com", "git@github.com")
SYSTEM_MAIL_DOMAINS = ("users.noreply.github.com",)
FIXTURE_MAIL_DOMAINS = ("example.com", "example.invalid")          # 測試資料用的假地址（保留網域，不會是誰的信箱）


def mail_ok(addr):
    a = (addr or "").strip().lower()
    return a in SYSTEM_MAIL_EXACT or a.rsplit("@", 1)[-1] in SYSTEM_MAIL_DOMAINS + FIXTURE_MAIL_DOMAINS


NAMED_SCAN_NOT_RUN = "未跑（無鹽）"
NAMED_SCAN_RAN = "有跑（有鹽）"


def named_term_scan_status(guards):
    """具名字串的掃描（私人清單上的名稱；要倉庫外的鹽）在這台機器上跑不跑得了。驗收機上沒有鹽、也不放任何密鑰（公開倉庫的 CI 不放密鑰），
    所以那幾條測試在這裡是 skipped——結果檔要明白寫出「未跑（無鹽）」，不可以讓人以為驗收機掃過。
    這一種由施工那台電腦上的檢查程式自己帶鹽掃（推送之前、寄「可以合併」之前各一次；.claude/hooks/iw_notify.py 的 named_term_blob_problem）。"""
    try:
        salt = guards.load_salt() if guards is not None else None
    except Exception:                                              # noqa: B902
        salt = None
    return NAMED_SCAN_RAN if salt else NAMED_SCAN_NOT_RUN


def unix_home_hit(text):
    """文字裡有沒有 Unix／macOS 的家目錄路徑（排除清單上的執行機固定路徑不算）。"""
    for m in _UNIX_HOME.finditer(text or ""):
        path = "/%s/%s" % (m.group(1), m.group(2))
        if path not in RUNNER_HOME_PREFIXES:
            return True
    return False


def privacy_scan(texts, guards=None):
    """texts：{名稱: 內容}。回傳命中清單（名稱：原因）。guards：main 上的 test_analysis_guards 模組（有就多掃三種）。
    原因只寫類別與處數，絕不帶命中的字串本身。"""
    hits = []
    for name, text in sorted(texts.items()):
        text = text or ""
        if _LOCAL_USER.search(text):
            hits.append("%s：本機的使用者資料夾" % name)
        if _LOCAL_ROOT.search(text):
            hits.append("%s：本機的絕對路徑" % name)
        if unix_home_hit(text):
            hits.append("%s：本機的家目錄路徑" % name)
        if any(rx.search(text) for rx in _SECRETS):
            hits.append("%s：像權杖的字串" % name)
        for m in _MAIL.findall(text):
            if not mail_ok(m):
                hits.append("%s：電子郵件" % name)
                break
        if guards is not None:
            try:
                extra = list(guards.privacy_hits(text)) + list(guards.profile_key_hits(text)) + list(guards.fxplan_key_hits(text))
            except Exception:                                       # noqa: B902
                hits.append("%s：掃描器出錯（當成命中）" % name)
                continue
            if extra:                                               # 只說有幾處，不把命中的字帶出來——這份清單會進紅的原因、結果檔與執行紀錄
                hits.append("%s：個人資料的字樣或設定鍵名（%d 處）" % (name, len(extra)))
    return hits


def _find_files(root, name):
    out = []
    for base, _dirs, names in os.walk(root):
        for n in names:
            if n == name or (name.endswith("*") and n.startswith(name[:-1])):
                out.append(os.path.join(base, n))
    return sorted(out)


def summary_md(res):
    t, e, c, m = res.get("tests") or {}, res.get("egress") or {}, res.get("compare") or {}, res.get("mutations") or {}
    lines = ["### 驗收機：%s" % ("🔴 紅" if res.get("red") else "🟢 綠"), "",
             "- commit：`%s`　分支／標籤：`%s`　驗收程式來源：%s%s" % ((res.get("commit") or "")[:12], res.get("ref"), c.get("verifier_source"),
                                                                "（**驗收機本身有改**：%s）" % "、".join(c.get("verifier_changed_files") or []) if c.get("verifier_changed") else ""),
             "- 測試：跑了 %s 條，通過 %s、紅 %s、skipped %s；靜態計數 %s（main %s）" % (t.get("ran"), t.get("passed"), len(t.get("failed") or []) + len(t.get("errors") or []),
                                                                         len(t.get("skipped") or []), t.get("defined"), c.get("tests_defined_main")),
             "- 突變：跑了 %s 個，紅 %s；存活 %s（已知例外 %s、沒解釋的 %s）；定義 %s（main %s）" % (m.get("total_ran"), m.get("red"), len(m.get("survivors") or []),
                                                                                 len(m.get("known_exceptions") or []), len(m.get("unexplained_survivors") or []),
                                                                                 c.get("mutations_total"), c.get("mutations_main")),
             "- 對外連線（全套測試＋每一片突變，共 %s 段，都在封鎖裡跑）：封鎖層級 %s；台銀 %s 次；資料來源／GitHub %d 個；未知主機 %d 個；瀏覽器自己的 %d 個；被擋的 IP %d 個" % (
                 e.get("jobs"), e.get("level"), e.get("bot_hits", 0), len(e.get("source_hosts") or []), len(e.get("unknown_hosts") or []),
                 len(e.get("browser_hosts") or []), len(e.get("blocked_ips") or [])),
             "- 動到的保護範圍檔：第一層 %d、自己的檔 %d、第二層 %d" % tuple(len((c.get("protected_touched") or {}).get(k) or []) for k in ("tier1", "self", "tier2"))]
    if res.get("named_term_scan"):
        lines.append("- 具名字串掃描：%s%s" % (res["named_term_scan"], "。驗收機不放鹽也不放任何密鑰；這一種由施工那台電腦上的檢查程式自己帶鹽掃"
                                           "（推送之前掃每一筆要推的 commit 改到的檔，寄「可以合併」之前再掃這個階段改到的每一個檔）" if res["named_term_scan"] == NAMED_SCAN_NOT_RUN else ""))
    lk = res.get("lockdown") or {}
    jobs = [lk] + list(lk.get("mutation_shards") or [])
    lines.append("- 每一段的封鎖層級（每一段都要 3／3，IPv4 與 IPv6 的防火牆規則都要有）：%s" % "；".join(
        "%s %s%s" % (j.get("job") or "?", j.get("level") or "沒有紀錄", "（%s）" % j["system_layer_gap"] if j.get("system_layer_gap") else "") for j in jobs))
    seals = res.get("seals") or []
    if seals:
        bad = [str(s.get("job")) for s in seals if not s.get("stages") or any(st.get("tree_clean") is not True or st.get("missing") for st in s["stages"].values())]
        lines.append("- 結果檔由原本的使用者保管（測試用的使用者改不到），每一步之後核對受測的 checkout 跟 commit 一模一樣：%s"
                     % ("%d 段都正常" % len(seals) if not bad else "有問題（%s）" % "、".join(bad)))
    again = ["%s：%s" % (j.get("job") or "?", "、".join(j["retried"])) for j in jobs if j.get("retried")]
    if again:
        lines.append("- 封鎖時第一次沒成、自動再試了一次的步驟：%s" % "；".join(again))
    if str(res.get("run_attempt") or "").isdigit() and int(res["run_attempt"]) > 1:
        lines.append("- 這是同一次執行的第 %s 次嘗試（重跑過 %d 次）" % (res["run_attempt"], int(res["run_attempt"]) - 1))
    if res.get("red_kind") == "lockdown":
        lines += ["", "**這一次紅是因為%s，不是測試紅。** 對策是重跑這一次驗收（同一個 commit 最多重跑 2 次）。" % LOCKDOWN_RED]
    if res.get("reasons"):
        lines += ["", "**紅的原因**", ""] + ["- " + r for r in res["reasons"]]
    if t.get("skipped"):
        lines += ["", "**沒跑的測試（skipped，列出理由）**", ""] + ["- `%s`：%s" % (s["name"], s["reason"]) for s in t["skipped"]]
    if c.get("tests_removed") or c.get("tests_modified"):
        lines += ["", "**跟 main 比：測試**", ""]
        lines += ["- 被刪：`%s`" % x for x in c.get("tests_removed") or []]
        lines += ["- 被改：`%s`" % x for x in c.get("tests_modified") or []]
    if c.get("mutations_removed") or c.get("mutations_modified"):
        lines += ["", "**跟 main 比：突變**", ""]
        lines += ["- 被刪：`%s`" % x for x in c.get("mutations_removed") or []]
        lines += ["- 被改：`%s`" % x for x in c.get("mutations_modified") or []]
    for k, title in (("tier1", "第一層（一律不能動）"), ("self", "自動駕駛自己的檔"), ("tier2", "第二層（要給 David 看 diff）")):
        files = (c.get("protected_touched") or {}).get(k) or []
        if files:
            lines += ["", "**動到的保護範圍檔：%s**" % title, ""] + ["- `%s`" % f for f in files]
    if e.get("browser_hosts") or e.get("unknown_hosts") or e.get("source_hosts") or e.get("bot_hosts"):
        lines += ["", "**對外請求（主機名稱）**", ""]
        for k, label in (("bot_hosts", "台銀"), ("source_hosts", "資料來源／GitHub"), ("unknown_hosts", "未知"), ("browser_hosts", "瀏覽器自己的")):
            lines += ["- %s：`%s`" % (label, h) for h in e.get(k) or []]
    if e.get("blocked_ips"):
        lines += ["", "**系統層擋下的連線（只有 IP）**", ""] + ["- `%s`　%s　埠 %s　%s 次" % (b.get("ip"), b.get("proto"), b.get("port") or "?", b.get("count"))
                                                    for b in e["blocked_ips"]]
    return "\n".join(lines) + "\n"


def job_dirs(inputs):
    """下載下來的每一段結果各在一個資料夾。回傳 (全套測試那一段的資料夾或 None, [每一片突變的資料夾])。"""
    main_dir, shard_dirs = None, []
    for base, _dirs, names in sorted(os.walk(inputs)):
        if main_dir is None and ("tests.json" in names or "compare.json" in names):
            main_dir = base
        if any(n.startswith("mut-") and n.endswith(".json") for n in names):
            shard_dirs.append(base)
    return main_dir, sorted(shard_dirs)


def cmd_collect(a):
    repo, out, inputs = os.path.abspath(a.repo), os.path.abspath(a.out), os.path.abspath(a.inputs)
    os.makedirs(out, exist_ok=True)
    main_dir, shard_dirs = job_dirs(inputs)

    def pick(d, name):
        return (read_json(os.path.join(d, name), {}) or {}) if d else {}
    tests = pick(main_dir, "tests.json")
    cmp_ = pick(main_dir, "compare.json")
    lockdown = pick(main_dir, "lockdown.json")
    shards = [read_json(p, {}) or {} for p in _find_files(inputs, "mut-*")]
    mut_lockdowns = []
    for d in shard_dirs:
        x = read_json(os.path.join(d, "lockdown.json"), None)
        if x:
            mut_lockdowns.append(dict(x, job=os.path.basename(d)))     # 哪一片：資料夾名＝那一段上傳時的名字（verify-mutations-N）
    # 每一段的封存紀錄：全套測試那一段要有「跑全套」「跟 main 比」兩步，每一片突變要有「跑突變」那一步
    seals = [(os.path.basename(main_dir), pick(main_dir, SEAL_FILE), ["run-tests", "compare"])] if main_dir else []
    seals += [(os.path.basename(d), pick(d, SEAL_FILE), ["mutations"]) for d in shard_dirs]
    egress_raw = merge_egress_raw([pick(main_dir, "egress.json")] + [pick(d, "egress.json") for d in shard_dirs])
    egress_raw["layers"] = lockdown.get("layers") or {}
    egress_raw["level"] = weakest_level([lockdown] + mut_lockdowns + [None] * max(0, len(shard_dirs) - len(mut_lockdowns)))
    allowed = []
    guards = None
    try:
        allowed = list(load_module(os.path.join(HERE, "net_policy.py"), "iw_net_policy").ALLOWED_HOSTS)
    except Exception:                                              # noqa: B902
        allowed = []
    try:
        guards = load_module(os.path.join(HERE, "test_analysis_guards.py"), "iw_guards")
    except Exception:                                              # noqa: B902
        guards = None
    known = read_json(os.path.join(HERE, "known_survivors.json"), {}) or {}
    known = dict((k, v) for k, v in known.items() if not k.startswith("_"))
    egress = judge_egress(egress_raw, allowed)
    egress["jobs"] = egress_raw.get("jobs")
    mut = merge_mutations(shards, known, expected_total=cmp_.get("mutations_total"))
    failures = ""
    for p in _find_files(inputs, "failures.txt"):
        failures += _read(p)
    job_privacy = []                                                # 各段上傳之前自己掃出來的（只有檔名）；哪一段沒有掃描紀錄也算
    for d in ([main_dir] if main_dir else []) + list(shard_dirs):
        rep = read_json(os.path.join(d, "export.json"), None)
        label = os.path.basename(d)
        if rep is None:
            job_privacy.append("%s：這一段沒有上傳前的掃描紀錄（export.json）" % label)
            continue
        if rep.get("scanner_missing"):
            job_privacy.append("%s：這一段讀不到隱私掃描器，什麼都沒有帶出來" % label)
        job_privacy += ["%s：%s 含不該公開的字串（上傳前就擋下了）" % (label, n) for n in rep.get("privacy_hits") or []]
    res = {"commit": os.environ.get("GITHUB_SHA") or cmp_.get("head_sha"), "ref": os.environ.get("GITHUB_REF_NAME") or os.environ.get("GITHUB_REF"),
           "run_id": os.environ.get("GITHUB_RUN_ID"), "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
           "run_url": "%s/%s/actions/runs/%s" % (os.environ.get("GITHUB_SERVER_URL", "https://github.com"), os.environ.get("GITHUB_REPOSITORY", "?"),
                                                 os.environ.get("GITHUB_RUN_ID", "?")),
           "tests": tests, "egress": egress, "compare": cmp_, "mutations": mut,
           "lockdown": dict(lockdown_brief(lockdown, job=os.path.basename(main_dir) if main_dir else None),
                            mutation_shards=[lockdown_brief(x) for x in mut_lockdowns]),
           "verifier_changed": bool(cmp_.get("verifier_changed")), "verifier_source": cmp_.get("verifier_source"),
           "allowed_hosts_from": "main" if allowed else "（讀不到 net_policy，白名單為空）", "privacy_scanner": "main" if guards is not None else "（讀不到）",
           "named_term_scan": named_term_scan_status(guards),
           "generated_at": now_iso(), "schema": 2}
    privacy = privacy_scan({"verify-result": json.dumps(res, ensure_ascii=False), "failures": failures,
                            "egress-hosts": "\n".join(sorted((egress_raw.get("hosts") or {}).keys()))}, guards)
    privacy = job_privacy + privacy
    if guards is None:
        privacy = privacy + ["隱私掃描器讀不到（main 上的 test_analysis_guards.py）"]
    reasons = judge(tests, egress, cmp_, mut, lockdown, privacy, mut_lockdowns=mut_lockdowns, seals=seals)
    res["seals"] = [{"job": label, "stages": dict((s, dict((k, ((seal.get("stages") or {}).get(s) or {}).get(k)) for k in ("tree_clean", "sealed", "missing", "leftovers_stopped")))
                                                  for s in stages if isinstance((seal.get("stages") or {}).get(s), dict))}
                    for label, seal, stages in seals]
    res["reasons"] = reasons
    res["red"] = bool(reasons)
    # 紅的原因分兩種：封鎖沒設成（對策是重跑），其他（測試、突變、對外連線、隱私…）。只有前一種時 red_kind 是 lockdown。
    res["lockdown_reasons"] = [r for r in reasons if r.startswith(LOCKDOWN_RED)]
    res["red_kind"] = None if not reasons else ("lockdown" if len(res["lockdown_reasons"]) == len(reasons) else "other")
    if privacy:
        res = {"commit": res["commit"], "ref": res["ref"], "run_id": res["run_id"], "run_attempt": res["run_attempt"], "run_url": res["run_url"],
               "red": True, "reasons": reasons,
               "privacy_hits": [h.split("：", 1)[0] for h in privacy], "note": "結果檔含不該公開的字串，內容沒有寫出來", "generated_at": now_iso(), "schema": 2}
    write_json(os.path.join(out, "verify-result.json"), res)
    md = summary_md(res) if not privacy else "### 驗收機：🔴 紅\n\n結果檔含不該公開的字串（%s），內容沒有寫出來。\n" % "、".join(res["privacy_hits"])
    write_text(os.path.join(out, "summary.md"), md)
    if a.summary:
        with io.open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(md)
    print(md)
    return 1 if res["red"] else 0


# ---------------------------------------------------------------- 入口

def main(argv=None):
    ap = argparse.ArgumentParser(description="驗收機（verify.yml 的每一步）")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("extract-verifier")
    p.add_argument("--repo", default=os.environ.get("GITHUB_WORKSPACE") or os.getcwd())
    p.add_argument("--main-ref", default=MAIN_REF, dest="main_ref")
    p.add_argument("--dest", default="/tmp/verifier")
    p = sub.add_parser("lockdown")
    p.add_argument("--out", required=True)
    p.add_argument("--repo", default=os.environ.get("GITHUB_WORKSPACE") or os.getcwd())
    p = sub.add_parser("run-tests")
    p.add_argument("--out", required=True)
    p.add_argument("--repo", default=os.environ.get("GITHUB_WORKSPACE") or os.getcwd())
    p.add_argument("--timeout", type=int, default=1800)
    p = sub.add_parser("run-tests-inner")
    p.add_argument("--repo", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--log", required=True)
    p = sub.add_parser("unlock")
    p.add_argument("--out", required=True)
    p = sub.add_parser("export")
    p.add_argument("--out", required=True)
    p.add_argument("--export", required=True)
    p = sub.add_parser("compare")
    p.add_argument("--out", required=True)
    p.add_argument("--repo", default=os.environ.get("GITHUB_WORKSPACE") or os.getcwd())
    p.add_argument("--main-ref", default=MAIN_REF, dest="main_ref")
    p.add_argument("--inner", action="store_true", help="（內部用）已經在封鎖裡了，直接做")
    p = sub.add_parser("mutations")
    p.add_argument("--out", required=True)
    p.add_argument("--repo", default=os.environ.get("GITHUB_WORKSPACE") or os.getcwd())
    p.add_argument("--shard", default="1/1")
    p.add_argument("--main-ref", default=MAIN_REF, dest="main_ref")
    p.add_argument("--copy-dir", default="/tmp", dest="copy_dir")
    p.add_argument("--timeout", type=int, default=1800)
    p = sub.add_parser("collect")
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--repo", default=os.environ.get("GITHUB_WORKSPACE") or os.getcwd())
    p.add_argument("--main-ref", default=MAIN_REF, dest="main_ref")
    p.add_argument("--summary", default=None)
    a = ap.parse_args(argv)
    if hasattr(a, "main_ref"):
        a.main_ref = full_main_ref(a.main_ref)                      # 不管流程檔傳什麼，進到每一步的都是全名
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:                                              # noqa: B902
        pass
    fn = {"extract-verifier": cmd_extract_verifier, "lockdown": cmd_lockdown, "run-tests": cmd_run_tests, "run-tests-inner": cmd_run_tests_inner,
          "unlock": cmd_unlock, "export": cmd_export, "compare": cmd_compare, "mutations": cmd_mutations, "collect": cmd_collect}.get(a.cmd)
    if fn is None:
        ap.print_help()
        return 2
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
