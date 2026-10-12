# -*- coding: utf-8 -*-
"""
iw_common.py — 自動駕駛共用的小工具：路徑正規化、設定檔、輸出、時間、git 查詢。

只用標準函式庫；Windows（Git Bash）與 macOS 都要能跑，Python 3.8 以上。

為什麼輸出要這樣寫（2026-10-01 在桌面 App 實測）：
  * Windows 上 Python 對管線預設用系統字碼頁（Big5），Claude Code 當 UTF-8 讀，中文會變亂碼
    → stderr 一律自己編成 UTF-8 寫 bytes；JSON 一律 ensure_ascii（只剩 ASCII，怎麼解都不會錯）。
  * hook 的結束碼只有 2 會擋；其他非 0 是「不擋的錯誤」→ 需要擋的地方一律用 block()。
"""
import datetime
import fnmatch
import io
import json
import os
import posixpath
import re
import subprocess
import sys

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDE_DIR = os.path.dirname(HOOKS_DIR)
ROOT = os.path.dirname(CLAUDE_DIR)                 # 這一份檔案所在的 checkout 根目錄；生效的那一份＝主目錄
CONFIG_FILE = os.path.join(CLAUDE_DIR, "autopilot", "config.json")
ALLOWLIST_FILE = os.path.join(CLAUDE_DIR, "autopilot", "allowlist.json")
STATE_DIR_NAME = "iw-autopilot"

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------- 輸出

def _write_bytes(stream, text):
    data = text.encode("utf-8", errors="replace")
    try:
        stream.buffer.write(data)
        stream.buffer.flush()
    except Exception:                                # 沒有 buffer（測試裡換成 StringIO）就照一般寫法
        stream.write(text)


def err(text):
    """寫到 stderr（UTF-8）。"""
    _write_bytes(sys.stderr, text if text.endswith("\n") else text + "\n")


def out_json(obj):
    """hook 的 JSON 輸出：只輸出這一個物件、全 ASCII。"""
    _write_bytes(sys.stdout, json.dumps(obj, ensure_ascii=True))


def out_text(text):
    _write_bytes(sys.stdout, text)


class Block(Exception):
    """要擋下這個動作。reason 是給 Claude（與 David）看的白話原因。code 是停止條件的編號（沒有就 None）。"""

    def __init__(self, reason, code=None):
        Exception.__init__(self, reason)
        self.reason = reason
        self.code = code


# ---------------------------------------------------------------- 時間

def now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt=None):
    return (dt or now()).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not s:
        return None
    try:
        s = str(s).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        m = re.match(r"^(.*\d)(\.\d+)([+-]\d{2}:\d{2})?$", s)       # 3.8～3.10 的 fromisoformat 不吃毫秒位數不是 3 或 6 的寫法
        if m:
            frac = (m.group(2) + "000000")[:7]
            s = m.group(1) + frac + (m.group(3) or "")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


def local_stamp(dt=None):
    """給人看的本地時間（信裡用）。"""
    return (dt or now()).astimezone().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- 路徑

def home_dir():
    return os.path.expanduser("~")


_PS_PROVIDER = re.compile(r"^(?:[A-Za-z.]+/)?FileSystem::", re.I)


def _plain(path):
    """去掉頭尾空白與一層引號、反斜線改正斜線、拿掉 PowerShell 的 FileSystem:: 與 Windows 的 //?/ 前綴。"""
    p = str(path).strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
        p = p[1:-1]
    p = p.replace("\\", "/")
    p = _PS_PROVIDER.sub("", p)
    m = re.match(r"^//[?.]/(.*)$", p)                             # //?/D:/x、//./D:/x：同一個檔的另一種寫法
    if m:
        rest = m.group(1)
        if rest[:4].lower() == "unc/":
            p = "//" + rest[4:]
        elif re.match(r"^[A-Za-z]:(/|$)", rest):
            p = rest
    return p


def weird_path(path, drive_relative=True):
    """Windows 上「看不出實際指到哪裡」的路徑寫法。回傳白話說明；一般的路徑回 None。
    會寫入的動作碰到這種路徑一律擋（寧可擋錯）：網路路徑可以指回這台電腦、裝置路徑可以指到任何磁碟區。
    drive_relative=False：不檢查「D:foo」這一種（git 的 a:b 是分支對應，不是路徑）。"""
    if path is None:
        return None
    p = _plain(path)
    if re.match(r"^//[?.]/", p):
        return "裝置或磁碟區的路徑（//?/ 或 //./ 開頭）"
    if re.match(r"^//[^/]+/[^/]+", p):
        return "網路路徑（//主機/資料夾）"
    if drive_relative and re.match(r"^[A-Za-z]:([^/]|$)", p):
        return "磁碟代號後面沒有斜線的相對寫法（像 D:foo）"
    for comp in p.split("/"):
        if len(comp) > 2 and not comp.strip(". "):
            return "只有點或空白的資料夾名稱"
        if any(ord(ch) < 32 for ch in comp):
            return "含有控制字元"
    return None


def norm(path, cwd=None):
    """把路徑變成可以比較的樣子：正斜線、小寫磁碟代號、解掉 . 與 ..；相對路徑接在 cwd 後面。
    也吃 Git Bash 的 /c/Users/... 與 ~。回傳 None 表示沒有東西。比較大小寫請用 key()。

    另外照 Windows 的規矩把「同一個檔的其他寫法」收成一種：每一段結尾的點與空白會被 Windows 拿掉
    （.claude. 就是 .claude）、冒號後面是同一個檔的另一個資料流（settings.json::$DATA 就是 settings.json）、
    //?/D:/… 是 D:/… 的長路徑寫法。別的系統上這樣收只會多擋、不會少擋。"""
    if path is None:
        return None
    p = _plain(path)
    if not p:
        return None
    if p == "~" or p.startswith("~/"):
        p = home_dir().replace("\\", "/") + p[1:]
    m = re.match(r"^/([A-Za-z])(/.*)?$", p)                       # Git Bash：/c/Users → c:/Users
    if m and IS_WINDOWS:
        p = m.group(1) + ":" + (m.group(2) or "/")
    m = re.match(r"^/(cygdrive|mnt)/([A-Za-z])(/.*)?$", p)
    if m and IS_WINDOWS:
        p = m.group(2) + ":" + (m.group(3) or "/")
    m = re.match(r"^([A-Za-z]):(?!/)(.*)$", p)                    # D:foo：相對於 D 槽「現在的資料夾」
    if m:
        base = norm(cwd) if cwd is not None else None
        if base is None or not base.lower().startswith(m.group(1).lower() + ":"):
            return None
        p = base.rstrip("/") + "/" + m.group(2)
    is_abs = bool(re.match(r"^[A-Za-z]:/", p)) or p.startswith("/")
    if not is_abs:
        if cwd is None:
            return None                                           # 相對路徑又不知道在哪個目錄：無法判斷
        base = norm(cwd)
        if base is None:
            return None
        p = base.rstrip("/") + "/" + p
    drive = ""
    m = re.match(r"^([A-Za-z]):(/.*)$", p)
    if m:
        drive, p = m.group(1).lower() + ":", m.group(2)
    comps = []
    for comp in p.split("/"):
        if comp not in ("", ".", ".."):
            if ":" in comp:
                comp = comp.split(":", 1)[0]                      # 另一個資料流：檔案本身還是冒號前面那一個
            comp = comp.rstrip(". ")                              # Windows 會把結尾的點與空白拿掉
            if not comp:
                comp = "."
        comps.append(comp)
    p = posixpath.normpath("/".join(comps))
    if p.startswith("//"):
        p = "/" + p.lstrip("/")
    return drive + p


def key(path, cwd=None):
    """比較用：norm 之後全部小寫（Windows 與 macOS 預設檔案系統都不分大小寫）。"""
    n = norm(path, cwd)
    return n.lower() if n is not None else None


def real_key(path, cwd=None):
    """跟 key() 一樣，但先請作業系統把「捷徑類」的東西解開：資料夾連結（junction）、符號連結、
    Windows 的短檔名（CLAUDE~1）、subst 出來的磁碟代號。解不出來回 None（呼叫的人還有 key() 那一份可以用）。"""
    n = norm(path, cwd)
    if n is None or weird_path(path) or (cwd is not None and weird_path(cwd)):
        return None                                               # 網路路徑不去問作業系統（會卡在連線逾時），由 weird_path 那一關擋
    if n in _real_cache:
        return _real_cache[n]
    try:
        r = norm(os.path.realpath(n))
    except Exception:                                             # noqa: B902
        r = None
    _real_cache[n] = r.lower() if r else None
    return _real_cache[n]


_real_cache = {}                                                  # 一次 hook 執行裡同一個路徑不必問兩次（每次執行都是新的程序）


def is_under(path_key, root_key):
    if path_key is None or root_key is None:
        return False
    root_key = root_key.rstrip("/")
    return path_key == root_key or path_key.startswith(root_key + "/")


def rel_to(path_key, root_key):
    root_key = root_key.rstrip("/")
    if path_key == root_key:
        return ""
    return path_key[len(root_key) + 1:]


def glob_match(rel, patterns):
    """rel 是相對於倉庫根目錄、正斜線、小寫的路徑。pattern 支援 * 與結尾的 /**。"""
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


# ---------------------------------------------------------------- 設定

_cache = {}


def load_json(path):
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def config(path=None):
    path = path or CONFIG_FILE
    if path not in _cache:
        _cache[path] = load_json(path)
    return _cache[path]


def allowlist(path=None):
    path = path or ALLOWLIST_FILE
    if path not in _cache:
        _cache[path] = load_json(path)
    return _cache[path]


def stage_ok(name):
    """階段名稱：英數開頭，只含英數、點、底線、連字號，最長 32。"""
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$", name or ""))


# ---------------------------------------------------------------- git

SAFE_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_NAMESPACE",
                     "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_GLOBAL",
                     "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT", "GIT_EXEC_PATH", "GIT_PREFIX")


def git_env():
    env = dict(os.environ)
    for k in list(env.keys()):
        if k in SAFE_GIT_ENV_DROP or k.startswith("GIT_CONFIG_KEY_") or k.startswith("GIT_CONFIG_VALUE_"):
            env.pop(k, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def git(args, cwd, timeout=20, clean_env=True):
    """跑 git，回 (結束碼, stdout 文字)。失敗不丟例外，由呼叫的人決定怎麼辦。"""
    try:
        p = subprocess.run(["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, env=git_env() if clean_env else None)
        return p.returncode, p.stdout.decode("utf-8", errors="replace").strip()
    except Exception as e:                                         # noqa: B902
        return 99, "git 執行失敗：%r" % (e,)


def head_blobs(worktree, ref="HEAD"):
    """ref 那個版本裡每個檔的內容編號：{路徑: 編號}。查不出來回 None。"""
    rc, out = git(["-c", "core.quotepath=false", "ls-tree", "-r", ref], worktree, timeout=30)
    if rc != 0:
        return None
    res = {}
    for line in out.split("\n"):
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and path:
            res[path] = parts[2]
    return res


def common_dir(checkout_root=None):
    """這個 checkout 所屬倉庫的共用 .git 目錄。不開子程序：.git 是資料夾就是它；是檔案（worktree）就照裡面寫的找。"""
    root = checkout_root or ROOT
    dotgit = os.path.join(root, ".git")
    if os.path.isdir(dotgit):
        return os.path.abspath(dotgit)
    if os.path.isfile(dotgit):
        with io.open(dotgit, encoding="utf-8", errors="replace") as fh:
            m = re.match(r"^gitdir:\s*(.+?)\s*$", fh.read().strip())
        if not m:
            raise RuntimeError(".git 檔的格式看不懂：%s" % dotgit)
        gitdir = m.group(1)
        if not os.path.isabs(gitdir):
            gitdir = os.path.join(root, gitdir)
        cfile = os.path.join(gitdir, "commondir")
        if os.path.isfile(cfile):
            with io.open(cfile, encoding="utf-8", errors="replace") as fh:
                c = fh.read().strip()
            return os.path.abspath(c if os.path.isabs(c) else os.path.join(gitdir, c))
        return os.path.abspath(gitdir)
    raise RuntimeError("這裡不是 git 倉庫：%s" % root)


def main_root(checkout_root=None):
    """主目錄（main 所在的那個 checkout）＝共用 .git 目錄的上一層。"""
    return os.path.dirname(common_dir(checkout_root))


def state_dir(checkout_root=None):
    return os.path.join(common_dir(checkout_root), STATE_DIR_NAME)


# ---------------------------------------------------------------- 「正式版在哪裡」只用全名
# git 解析一個短名（例如 origin/main）的順序是：標籤（refs/tags/）→ 本機分支（refs/heads/）→ 遠端的記號（refs/remotes/）。
# 所以只要有人建一個名字就叫 origin/main 的標籤或本機分支，寫 origin/main 的地方拿到的就是它，git 只印一行警告——
# 「跟正式版比」的每一關（這個階段改了哪些檔、驗收機本身有沒有改、要掃哪些檔）都會被它帶著走；
# 驗收機那一邊，推一個同名的標籤就能換掉「main 上的驗收程式」。2026-10-06 審查代理看 P2 最後一個 commit 時指出的，Cowork 裁決在 P2 修。
# 做法：檢查程式裡指「遠端的正式版」一律用下面這個函式給的全名（全名不受同名的標籤與分支影響）；
# 另外不准從 Claude Code 建立或推送會跟這些記號撞名的分支與標籤（reserved_ref_name）。
REMOTE_REF_FORMAT = "refs/remotes/%s/%s"
RESERVED_REF_PREFIXES = ("refs/", "remotes/")                       # 另外加上「<遠端的名字>/」，例如 origin/


def remote_main_ref(cfg):
    """遠端的正式版分支在本機的記號，全名（例如 refs/remotes/origin/main）。"""
    return REMOTE_REF_FORMAT % (cfg["remote"], cfg["mainBranch"])


def reserved_ref_name(name, remote="origin", literal=False):
    """分支或標籤的名字會不會跟 git 內部的記號撞名：以「<遠端的名字>/」「refs/」「remotes/」開頭的都算（不分大小寫——Windows 的檔案系統不分）。
    name 是推送或 fetch 的目的地時，可能帶 refs/heads/、refs/tags/ 這一層（先拿掉一層再看）；
    literal=True＝這就是要建立的名字本身（git tag 名字、git branch 名字）：照字面看，連 refs/heads/… 開頭的也算撞名。"""
    n = (name or "").strip().lower()
    if not literal:
        for lead in ("refs/heads/", "refs/tags/"):
            if n.startswith(lead):
                n = n[len(lead):]
                break
    return n.startswith(((remote or "origin").lower() + "/",) + RESERVED_REF_PREFIXES)


def in_claude_session(environ=None):
    """是不是 Claude Code 開出來的程序（Bash／PowerShell 工具、hook）。筆電排程與你自己的終端機不是。"""
    e = os.environ if environ is None else environ
    return bool(e.get("CLAUDECODE") or e.get("CLAUDE_CODE_CHILD_SESSION"))
