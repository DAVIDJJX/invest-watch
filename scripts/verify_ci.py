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
系統層擋下的連線（只留下 IP）→紅；突變分片沒有封鎖→紅；測試數或突變數變少→紅；存活的突變不在 main 的例外清單→紅；結果檔含不該公開的字串→紅；沒有系統層的封鎖→不紅，但結果寫「封鎖層級 2／3」。
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


def git(repo, args, timeout=60):
    p = subprocess.run(["git"] + list(args), cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace")


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

def cmd_lockdown(a):
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    egress = os.path.join(out, "egress")
    os.makedirs(egress, exist_ok=True)
    info = {"started_at": now_iso(), "layers": {"python": "sitecustomize", "browser": None, "ip": None}, "user": None, "notes": [],
            "browser": None, "selftest": {}}
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
    if sys.platform.startswith("linux"):
        rc, o = sudo(["-v"])
        if rc != 0:
            info["notes"].append("沒有免密碼 sudo：沒有系統層的封鎖")
        else:
            rc, o = sudo(["useradd", "-m", "-s", "/bin/bash", TEST_USER])
            if rc == 0 or "already exists" in o:
                info["user"] = TEST_USER
                home = "/tmp/%s-home" % TEST_USER
                repo = os.path.abspath(a.repo)
                sudo(["chmod", "-R", "a+rwX", repo], timeout=300)
                sudo(["chmod", "-R", "a+rwX", out], timeout=120)
                for base in (repo, out, HERE):                      # 讓那個使用者走得到倉庫、結果資料夾與驗收程式（上層資料夾預設別人進不去）
                    d = os.path.dirname(base)
                    while d and d != os.path.dirname(d):
                        sudo(["chmod", "o+x", d])
                        d = os.path.dirname(d)
                sudo(["chmod", "-R", "a+rX", HERE])
                sudo(["mkdir", "-p", home])
                sudo(["chown", "-R", TEST_USER, home])
                # 倉庫是別的使用者的：git 預設會拒絕（dubious ownership）。只對這個測試用的使用者放行。
                sudo(["-u", TEST_USER, "env", "HOME=" + home, "git", "config", "--global", "--add", "safe.directory", "*"])
                accept = ["-I", "OUTPUT", "1", "-m", "owner", "--uid-owner", TEST_USER, "-o", "lo", "-j", "ACCEPT"]
                log = ["-A", "OUTPUT", "-m", "owner", "--uid-owner", TEST_USER, "-j", "LOG", "--log-prefix", IPTABLES_PREFIX]
                drop = ["-A", "OUTPUT", "-m", "owner", "--uid-owner", TEST_USER, "-j", "DROP"]
                applied = {"iptables": [], "ip6tables": []}
                for table in ("iptables", "ip6tables"):
                    if sudo([table] + accept)[0] == 0:
                        applied[table].append(accept)
                        if sudo([table] + log)[0] == 0:             # 記錄被擋的封包（拿不到 LOG 模組就只擋不記）
                            applied[table].append(log)
                        if sudo([table] + drop)[0] == 0:
                            applied[table].append(drop)
                ok4 = accept in applied["iptables"] and drop in applied["iptables"]
                ok6 = accept in applied["ip6tables"] and drop in applied["ip6tables"]
                info["iptables"] = {"ipv4": ok4, "ipv6": ok6, "applied": applied, "log": log in applied["iptables"]}
                if ok4:
                    info["layers"]["ip"] = "iptables(uid-owner %s%s%s)" % (TEST_USER, "" if ok6 else "；沒有 IPv6 規則", "" if log in applied["iptables"] else "；只擋不記")
                    rc, o = sudo(["-u", TEST_USER, "curl", "-sS", "--max-time", "5", "http://%s/" % SELFTEST_IP], timeout=30)
                    info["selftest"]["ip"] = "blocked" if rc != 0 else "NOT blocked"
                    if rc == 0:
                        info["layers"]["ip"] = None
                        info["notes"].append("系統層自我測試沒過：以 %s 連 %s 竟然成功" % (TEST_USER, SELFTEST_IP))
                else:
                    info["notes"].append("iptables 加規則失敗（沒有系統層的封鎖）")
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
    inner = [sys.executable, "-X", "utf8", "-W", "ignore", os.path.join(HERE, "verify_ci.py"), "run-tests-inner", "--repo", repo,
             "--out", os.path.join(out, "tests.json"), "--log", os.path.join(out, "tests-verbose.txt")]
    cmd, env = locked_command(out, lock, inner)
    print("跑全套測試（%s）…" % ("使用者 %s、封鎖層級 %s" % (user, lock.get("level")) if user else "封鎖層級 %s" % lock.get("level", "?")))
    t0 = time.time()
    rc, o = run(cmd, cwd=repo, timeout=a.timeout, env=env)
    print(safe_tail(o, 4000))
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


def cmd_unlock(a):
    out = os.path.abspath(a.out)
    lock = read_json(os.path.join(out, "lockdown.json"), {}) or {}
    egress_dir = os.path.join(out, "egress")
    if lock.get("user"):                                            # 測試用的使用者寫出來的檔（瀏覽器的 netlog）別人預設讀不到：先放寬，才收集得到、也才上傳得了
        sudo(["chmod", "-R", "a+rwX", out], timeout=120)
    hosts = {}
    py_hosts, py_events = parse_python_log(_read(os.path.join(egress_dir, "python.jsonl")))
    for h, n in py_hosts.items():
        hosts.setdefault(h, {"count": 0, "via": []})
        hosts[h]["count"] += n
        if "python" not in hosts[h]["via"]:
            hosts[h]["via"].append("python")
    netlogs = sorted(glob.glob(os.path.join(egress_dir, "chrome-netlog-*.json")))
    for p in netlogs:
        for h, n in parse_netlog_hosts(_read(p)).items():
            hosts.setdefault(h, {"count": 0, "via": []})
            hosts[h]["count"] += n
            if "browser" not in hosts[h]["via"]:
                hosts[h]["via"].append("browser")
    blocked, counters, selftest_seen = [], None, False
    if (lock.get("layers") or {}).get("ip"):
        rc, o = sudo(["dmesg"], timeout=60)
        if rc != 0:
            rc, o = sudo(["journalctl", "-k", "--no-pager"], timeout=60)
        for b in parse_iptables_log(o if rc == 0 else ""):
            if b["ip"] == SELFTEST_IP and b["port"] == "80":        # 封鎖時自己故意連的那一次：證明「擋下而且記到了」，不列進清單
                selftest_seen = True
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
    """子程序輸出的最後一段，要印進執行紀錄之前先掃：公開倉庫的執行紀錄誰都看得到。命中就不印內容，只說有命中。"""
    tail = (text or "")[-limit:]
    hits = privacy_scan({"輸出": tail}, main_guards())
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
EXPORT_FILES = ("tests.json", "compare.json", "lockdown.json", "egress.json")
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


def cmd_compare(a):
    repo, out = os.path.abspath(a.repo), os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    lock = read_json(os.path.join(out, "lockdown.json"), None)
    if lock and not getattr(a, "inner", False):                    # 這一步會載入分支上的突變定義（＝執行分支的程式碼）：有封鎖就在封鎖裡跑
        inner = [sys.executable, "-X", "utf8", os.path.join(HERE, "verify_ci.py"), "compare", "--out", out, "--repo", repo,
                 "--main-ref", a.main_ref, "--inner"]
        cmd, env = locked_command(out, lock, inner)
        rc, o = run(cmd, cwd=repo, timeout=600, env=env)
        print(safe_tail(o, 4000))
        return rc
    main_ref = a.main_ref
    rc, main_sha = git(repo, ["rev-parse", main_ref])
    rc2, head_sha = git(repo, ["rev-parse", "HEAD"])
    res = {"main_ref": main_ref, "main_sha": main_sha.strip() if rc == 0 else None, "head_sha": head_sha.strip() if rc2 == 0 else None}
    # 測試
    branch_tests = collect_tests(branch_test_sources(repo))
    main_srcs = main_test_sources(repo, main_ref)
    main_tests = collect_tests(main_srcs) if main_srcs is not None else None
    res["tests_defined"] = len(branch_tests)
    res["tests_defined_main"] = len(main_tests) if main_tests is not None else None
    res["tests_removed"], res["tests_modified"] = diff_items(main_tests or {}, branch_tests)
    # 突變
    defs_path = os.path.join(repo, "scripts", "mutations", "autopilot_mutations.py")
    branch_defs = load_module(defs_path, "iw_defs_branch").MUTATIONS if os.path.exists(defs_path) else []
    main_defs_text = git_show(repo, main_ref, "scripts/mutations/autopilot_mutations.py")
    main_defs = None
    if main_defs_text is not None:
        tmp = os.path.join(out, "main_autopilot_mutations.py")
        write_text(tmp, main_defs_text)
        main_defs = load_module(tmp, "iw_defs_main").MUTATIONS
    res["mutations_total"] = len(branch_defs)
    res["mutations_main"] = len(main_defs) if main_defs is not None else None
    res["mutations_removed"], res["mutations_modified"] = diff_items(mutation_items(main_defs or []), mutation_items(branch_defs))
    runner_path = os.path.join(HERE, "run_mutations.py")
    if not os.path.exists(runner_path):
        runner_path = os.path.join(repo, "scripts", "mutations", "run_mutations.py")
    try:
        res["mutation_anchor_problems"] = load_module(runner_path, "iw_runner").check_anchors(repo, branch_defs) if os.path.exists(runner_path) else ["找不到突變的執行器"]
    except Exception as e:                                          # noqa: B902
        res["mutation_anchor_problems"] = ["核對錨點時出錯：%r" % (e,)]
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
    cmd = [sys.executable, "-X", "utf8", runner, "--defs", defs, "--known", known, "--repo", repo, "--copy", os.path.join(a.copy_dir, "iw-mutcopy-" + tag),
           "--shard", a.shard, "--no-baseline", "--out", os.path.join(out, "mut-%s.json" % tag), "--timeout", str(a.timeout)]
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


def judge(tests, egress, cmp_, mut, lockdown, privacy, mut_lockdowns=None):
    """回傳紅的原因清單（空的＝綠）。每一條規則在 scripts/test_verify_ci.py 都有「改壞→紅」的對照。
    mut_lockdowns：每一片突變的封鎖紀錄（None＝舊的呼叫方式，不檢查這一項）。"""
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
    if not ((lockdown or {}).get("layers") or {}).get("python"):
        reasons.append("Python 層的封鎖沒有生效")
    if mut_lockdowns is not None:                                   # 突變也會執行分支上的程式碼：每一片都要在封鎖裡跑
        shards = len(mut.get("shards") or [])
        if len(mut_lockdowns) < shards:
            reasons.append("有 %d 片突變沒有封鎖紀錄（突變也要在封鎖裡跑）" % (shards - len(mut_lockdowns)))
        if any(not ((x or {}).get("layers") or {}).get("python") for x in mut_lockdowns):
            reasons.append("有突變分片的 Python 層封鎖沒有生效")
    if privacy:
        reasons.append("隱私掃描沒過（含不該公開的字串，或有一段沒有掃描）：%s" % "；".join(privacy)[:300])
    return reasons


# 隱私掃描（跟 scripts/test_autopilot_config.py 的樣式一致；另外加 main 上 test_analysis_guards 的三個掃描器）
_LOCAL_USER = re.compile(r"(?i)[a-z]:[\\/]+users[\\/]+(?!fake\b)[a-z0-9_.-]+")
_LOCAL_ROOT = re.compile(r"(?i)[a-z]:[\\/]+claude_use")
_TOKEN = re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")
_MAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


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
        if _TOKEN.search(text):
            hits.append("%s：像權杖的字串" % name)
        for m in _MAIL.findall(text):
            if not (m.endswith(("@example.com", "@example.invalid", "@github.com")) or "noreply" in m or m.startswith("git@")):
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
    mut_lockdowns = [x for x in (read_json(os.path.join(d, "lockdown.json"), None) for d in shard_dirs) if x]
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
           "lockdown": {"level": lockdown.get("level"), "layers": lockdown.get("layers"), "selftest": lockdown.get("selftest"), "notes": lockdown.get("notes"),
                        "mutation_shards": [{"level": x.get("level"), "layers": x.get("layers"), "selftest": x.get("selftest")} for x in mut_lockdowns]},
           "verifier_changed": bool(cmp_.get("verifier_changed")), "verifier_source": cmp_.get("verifier_source"),
           "allowed_hosts_from": "main" if allowed else "（讀不到 net_policy，白名單為空）", "privacy_scanner": "main" if guards is not None else "（讀不到）",
           "generated_at": now_iso(), "schema": 2}
    privacy = privacy_scan({"verify-result": json.dumps(res, ensure_ascii=False), "failures": failures,
                            "egress-hosts": "\n".join(sorted((egress_raw.get("hosts") or {}).keys()))}, guards)
    privacy = job_privacy + privacy
    if guards is None:
        privacy = privacy + ["隱私掃描器讀不到（main 上的 test_analysis_guards.py）"]
    reasons = judge(tests, egress, cmp_, mut, lockdown, privacy, mut_lockdowns=mut_lockdowns)
    res["reasons"] = reasons
    res["red"] = bool(reasons)
    if privacy:
        res = {"commit": res["commit"], "ref": res["ref"], "run_id": res["run_id"], "run_url": res["run_url"], "red": True, "reasons": reasons,
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
    p.add_argument("--main-ref", default="origin/main", dest="main_ref")
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
    p.add_argument("--main-ref", default="origin/main", dest="main_ref")
    p.add_argument("--inner", action="store_true", help="（內部用）已經在封鎖裡了，直接做")
    p = sub.add_parser("mutations")
    p.add_argument("--out", required=True)
    p.add_argument("--repo", default=os.environ.get("GITHUB_WORKSPACE") or os.getcwd())
    p.add_argument("--shard", default="1/1")
    p.add_argument("--main-ref", default="origin/main", dest="main_ref")
    p.add_argument("--copy-dir", default="/tmp", dest="copy_dir")
    p.add_argument("--timeout", type=int, default=1800)
    p = sub.add_parser("collect")
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--repo", default=os.environ.get("GITHUB_WORKSPACE") or os.getcwd())
    p.add_argument("--main-ref", default="origin/main", dest="main_ref")
    p.add_argument("--summary", default=None)
    a = ap.parse_args(argv)
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
