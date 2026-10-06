# -*- coding: utf-8 -*-
"""
iw_guard.py — 第一道保護：每個動作執行之前的檢查（PreToolUse）。

兩組規則：
  【永遠有效】只要工作階段開在這個專案資料夾就生效，不管有沒有在自動駕駛：
      * 沒有有效的放行通行證時，擋下任何「合併進 main、推上 main、強推、刪或移動標籤、改寫歷史」的指令
      * 擋下寫入：通行證與狀態資料夾、hook 腳本、.claude/settings*.json、自動駕駛設定、git 的 hook 與設定、對話紀錄
      * 主目錄（main 所在的那個資料夾）只能看、只能快轉跟上 origin/main
  【自動駕駛期間多擋】清單以外的工具與程式、排程與資料來源相關的檔、模型或思考強度不對、超過時間上限、
      審查代理以外的子代理、會替我送訊息或改工作階段設定的工具；進入停止狀態後只准寫報告與寄信

這個檔只做判斷，不改任何東西：要改的狀態用 effects 交回去，由 iw_events.py 在拿到鎖之後寫。
看不懂的寫法一律擋（寧可擋錯）。規則的出處與「擋不住的事」見 docs/AUTOPILOT.md。
"""
import os
import re

import iw_common as C
import iw_shell as S
import iw_state as ST
from iw_common import Block

MAIN = "main"
REMOTE = "origin"
REMOTE_MAIN = C.REMOTE_REF_FORMAT % (REMOTE, MAIN)                  # 遠端正式版的記號一律用全名（同名的標籤或本機分支蓋不過它）

READONLY_GIT = set("""status diff log show rev-parse rev-list ls-files ls-tree cat-file describe merge-base merge-tree name-rev
shortlog blame grep for-each-ref show-ref ls-remote count-objects check-ignore check-attr diff-tree diff-index diff-files var version
help whatchanged show-branch cherry range-diff fsck verify-commit verify-tag""".split())

HISTORY_REWRITE = set(["rebase", "filter-branch", "filter-repo", "replace", "fast-import", "update-ref", "prune"])

# 主目錄裡可以用的 git 子指令（除了唯讀的）。其他一律擋：主目錄只留 main、乾淨，筆電排程要用。
MAIN_CHECKOUT_EXTRA = set(["fetch", "push", "worktree", "branch", "tag", "config", "remote", "merge", "pull", "reflog", "stash",
                           "symbolic-ref", "gc"])

# 認得的 git 子指令。不在這裡的（包含自訂別名、外掛、底層的傳輸指令）在這個專案的倉庫裡一律擋——
# 第一版是「列出危險的、其他放行」，結果 git send-pack（底層的推送，不經過推送前的檢查）就這樣漏掉了。
KNOWN_GIT = READONLY_GIT | HISTORY_REWRITE | MAIN_CHECKOUT_EXTRA | set("""add commit rm mv reset checkout switch restore clean apply am
cherry-pick revert init clone notes archive bundle sparse-checkout update-index read-tree write-tree commit-tree
hash-object mktree pack-refs repack format-patch push""".split())

# 不經過「推送前的檢查」就能把東西送出去、或會另外執行指令的 git 子指令：在哪個資料夾都擋
GIT_NEVER = set("""send-pack http-push receive-pack upload-pack upload-archive http-fetch fetch-pack remote-http remote-https
remote-ftp remote-ftps remote-ext remote-fd daemon shell subtree svn p4 cvsserver imap-send send-email instaweb http-backend
credential credential-manager credential-store credential-cache submodule bisect difftool mergetool maintenance""".split())

# gh 對 PR／issue（P2）：PR 的寫入只准經過主目錄那支 iw_notify.py pr（開、改標題內文、留言剛好是 @codex review）；其他一律擋，兩種模式都是。
# 2026-10-05 Codex 的審查意見：原本列的是「會寫的子指令」，漏了 revert（它會直接開一個 PR，不經過隱私掃描）。
# 會寫的列不完、gh 以後還會加新的，所以反過來只列「確定只讀的」；不在這裡的一律擋。
GH_PR_READS = set("view list diff checks status".split())
GH_ISSUE_READS = set("view list status".split())
# gh run／gh workflow：驗收機的執行紀錄是證據。確定不會改到紀錄的才放行；rerun（重跑驗收）與 workflow run（觸發通知）各有自己的規則。
# 2026-10-06 Codex 的審查意見：一般模式原本放行 gh run delete——同一個 commit 可以有兩次執行（推分支、推標籤），關卡認最新的那一次；
# 把最新的紅的刪掉，比較舊的綠的就重新算數。
GH_RUN_READS = set("list view watch download".split())
GH_WORKFLOW_READS = set("list view".split())

READONLY_PROGRAMS =set("""cat head tail less more ls dir stat file wc grep egrep fgrep rg diff cmp md5sum sha256sum sha1sum od xxd strings
test [ [[ echo printf true false : pwd basename dirname realpath which type date sort uniq cut tr awk sed find du df sleep wait cd pushd popd
export set exit return jq yq nl base64 column tac rev fold expand unexpand paste join comm cksum sha512sum b2sum hexdump zcat bzcat xzcat
tree printenv id whoami uname hostname nproc seq expr""".split())

# PowerShell：對一個物件「呼叫方法」時，只有這些確定不會動到檔案的名字放行（字串處理、日期、查表）。
# 其他的一律擋：(Get-Item 檔).Delete()、.MoveTo()、.CopyTo()、.Replace() 都是這種寫法，而且路徑不在方法的參數裡。
PS_SAFE_MEMBERS = set("""tostring trim trimstart trimend split substring contains startswith endswith tolower toupper tolowerinvariant
toupperinvariant padleft padright indexof lastindexof gettype equals compareto getenumerator gethashcode tochararray
adddays addhours addminutes addseconds addmonths tolocaltime touniversaltime getvalue containskey trygetvalue foreach where""".split())


PS_SAFE_PROPERTIES = set("""name fullname length count basename extension directoryname directory path id value key lastwritetime
creationtime lastaccesstime mode parent root exists attributes target linktype pspath psparentpath pschildname taskname taskpath state
status description displayname processname handles cpu ws message timecreated line linenumber filename matches""".split())


def _ps_member(text):
    """PowerShell 的「.方法」：(運算式).Delete、$x.Delete、$x.Directory.Delete → 回傳最後那個名字（小寫）；不是這種寫法回 None。"""
    m = re.match(r"^(?:\$[^.\s\[]*)?(?:\[[^\]]*\])*((?:\.[A-Za-z_]\w*(?:\[[^\]]*\])*)+)$", text)      # 中間可以夾 [0] 這種取第幾個
    return re.findall(r"\.([A-Za-z_]\w*)", m.group(1))[-1].lower() if m else None


def _is_extract(p, argv):
    """這次是不是在「解開」壓縮檔（建立或列出內容不算）。"""
    args = argv[1:]
    if p in ("tar", "bsdtar"):
        first = args[0] if args else ""
        return any(a in ("-x", "--extract", "--get") or (a.startswith("-") and not a.startswith("--") and "x" in a) for a in args) or \
            (bool(first) and not first.startswith("-") and "x" in first)
    if p == "unzip":
        return not any(a in ("-l", "-t", "-Z", "-v", "-z") for a in args)
    if p in ("7z", "7za"):
        return bool(args) and args[0] in ("x", "e")
    if p == "cpio":
        return any(a.startswith("-") and "i" in a for a in args) or "--extract" in args
    return True

READONLY_PS = set("""get-content gc cat type get-childitem gci ls dir test-path select-string sls get-item gi get-itemproperty
resolve-path measure-object select-object where-object write-output echo write-host set-location cd sl get-location pwd
out-null format-table format-list sort-object get-date get-filehash""".split())

# 直接改檔案或資料夾的指令：它們的路徑一定要看得出來（不可以有沒指定過的變數、萬用字元要看得出範圍）
FILE_MUTATORS = set("""rm rmdir mv cp tee touch ln link unlink chmod chown chgrp truncate dd install rsync scp patch tar bsdtar unzip zip
gzip gunzip bzip2 xz 7z 7za cpio shred split csplit mkdir attrib icacls takeown cacls mklink fsutil robocopy xcopy copy move del erase
rd ren rename subst net expand certutil bitsadmin
set-content sc add-content ac out-file tee-object copy-item cpi move-item mi remove-item ri rename-item rni new-item ni clear-content clc
set-item si clear-item cli set-itemproperty sp remove-itemproperty rp new-itemproperty md expand-archive compress-archive
export-csv export-clixml new-psdrive start-bitstransfer set-acl""".split())

# 把壓縮檔解到「現在的資料夾」的程式：現在的資料夾如果包住保護檔，就擋
EXTRACTORS = set(["tar", "bsdtar", "unzip", "7z", "7za", "cpio", "patch", "expand-archive"])

SHELLS = set(["sh", "bash", "zsh", "dash", "ksh", "ash", "git-bash", "git-sh", "busybox", "mintty"])
PS_SHELLS = set(["powershell", "pwsh", "powershell_ise"])
INLINE_CODE = {"python": "-c", "python3": "-c", "py": "-c", "node": "-e", "perl": "-e", "ruby": "-e", "php": "-r", "deno": "eval"}
GIT_DANGER_VERBS = set("""push merge pull rebase reset tag update-ref branch config remote commit filter-branch checkout switch
credential send-pack http-push receive-pack""".split())

# 會替別的程式另外取名字的指令：之後就看不出實際執行的是誰
ALIASERS = set(["alias", "set-alias", "sal", "new-alias", "nal", "doskey", "hash", "enable"])

GIT_ENV_OK = set(["GIT_PAGER", "GIT_TERMINAL_PROMPT", "GIT_MERGE_AUTOEDIT", "GIT_EDITOR", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                  "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE", "GIT_OPTIONAL_LOCKS"])
# git 會把這幾個環境變數的值當成指令執行（編輯器、分頁器）：只准設成不做事的值
EXEC_ENV = set(["GIT_EDITOR", "GIT_PAGER", "GIT_SEQUENCE_EDITOR", "EDITOR", "VISUAL", "PAGER"])
EXEC_ENV_SAFE = set(["", "true", ":", "cat", "less", "more"])
GIT_DASH_C_OK = set(["core.quotepath", "color.ui", "advice.detachedhead", "core.autocrlf", "core.safecrlf",
                     "i18n.logoutputencoding", "i18n.commitencoding", "diff.renames"])

# 會替我送訊息、排程、或開別的工作階段的工具：內容裡出現放行詞就擋（永遠有效）
MESSAGING_TOOLS = re.compile(r"^(CronCreate|ScheduleWakeup|SendMessage|RemoteTrigger|PushNotification|"
                             r"mcp__scheduled-tasks__.*|mcp__ccd_session_mgmt__send_message|mcp__ccd_session__spawn_task)$")
APPROVAL_WORDS = ("放行", "自動駕駛", "裁決", "免外部審查", "驗收機變更")

# 暫停（或停在合併前）期間，除了讀檔與搜尋之外還准用的工具：都不會動到任何檔。清單寫死、越窄越好（test_pause_allowed_list_is_narrow）。
PAUSE_TOOLS = ("Skill", "ToolSearch", "TodoWrite", "SubagentHandback")
_SQUEEZE = re.compile("[\\s" + "".join(chr(c) for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x3000, 0x00A0, 0x00AD)) + "]+")

_SELF_RE = re.compile(r"(^|/)\.claude/(settings(\.local)?\.json|hooks|agents|skills|autopilot)(/|$)")
# 自由文字（直接寫在指令裡的程式碼、看不懂的指令）用比較寬的比對：前面不必是斜線，後面不必是結尾。
# 代價：連 worktree 裡的副本也會被當成保護檔——程式碼看不出它實際開哪一個檔，寧可擋錯。要改副本請用改檔工具。
_SELF_TEXT_RE = re.compile(r"\.claude/(settings(\.local)?\.json|hooks|agents|skills|autopilot)(/|\b)")


def prog_name(text):
    """C:/Program Files/Git/cmd/git.exe → git"""
    t = text.replace("\\", "/").rstrip("/").split("/")[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".com"):
        if t.endswith(ext):
            t = t[:-len(ext)]
    return t


def url_key(url):
    """把遠端網址變成可以比較的樣子：github.com/davidjjx/invest-watch"""
    u = (url or "").strip().lower()
    u = re.sub(r"^[a-z+]+://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)
    u = u.replace(":", "/", 1) if re.match(r"^[^/]+:[^/]", u) and not re.match(r"^[a-z]:[\\/]", u) else u
    u = re.sub(r"\.git/?$", "", u).rstrip("/")
    return u


def repo_slug(key):
    """只留「帳號/倉庫」兩段來比（github.com、ssh.github.com:443、www.github.com 都是同一個倉庫）。"""
    parts = [x for x in (key or "").split("/") if x]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (key or None)


# ---------------------------------------------------------------- git 查詢（真的去問 git；測試裡換成假的）

class GitInfo(object):
    def __init__(self, main_root):
        self.main_root = main_root
        self.main_key = C.key(main_root)
        self._cache = {}

    def info(self, cdir):
        """cdir 在哪個 checkout：ours／toplevel／branch（分離時是 None）／head／is_main_checkout。不在倉庫裡回 None。"""
        k = C.key(cdir) if cdir else None
        if k is None:
            return None
        if k in self._cache:
            return self._cache[k]
        res = None
        if os.path.isdir(cdir):
            rc, out = C.git(["rev-parse", "--show-toplevel", "--git-common-dir", "--abbrev-ref", "HEAD"], cdir)
            lines = out.split("\n") if rc == 0 else []
            if len(lines) >= 3:
                common = lines[1]
                if not os.path.isabs(common):
                    common = os.path.join(cdir, common)
                rc2, head = C.git(["rev-parse", "-q", "--verify", "HEAD^{commit}"], cdir)
                top = C.key(lines[0])
                res = {"ours": C.key(common) == self.main_key + "/.git", "toplevel": top,
                       "branch": None if lines[2] == "HEAD" else lines[2],
                       "head": head if rc2 == 0 else None, "is_main_checkout": top == self.main_key}
        self._cache[k] = res
        return res

    def main_tips(self):
        rc, out = C.git(["for-each-ref", "--format=%(objectname)", "refs/heads/" + MAIN, REMOTE_MAIN], self.main_root)
        return set(out.split()) if rc == 0 else set()

    def upstream(self, cdir):
        rc, out = C.git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cdir)
        return out if rc == 0 else None

    def tag_exists(self, name):
        rc, _ = C.git(["rev-parse", "-q", "--verify", "refs/tags/" + name], self.main_root)
        return rc == 0

    def rev(self, cdir, ref):
        rc, out = C.git(["rev-parse", "-q", "--verify", ref + "^{commit}"], cdir)
        return out if rc == 0 else None

    def head_is_published(self, cdir):
        rc, out = C.git(["branch", "-r", "--contains", "HEAD"], cdir)
        if rc != 0 or out.strip():
            return True                                             # 問不出來就當成已經推出去（保守）
        rc, out = C.git(["tag", "--points-at", "HEAD"], cdir)
        return rc != 0 or bool(out.strip())

    def remote_url_key(self, cdir, name):
        rc, out = C.git(["remote", "get-url", name], cdir)
        return url_key(out) if rc == 0 else None

    def our_url_key(self):
        return self.remote_url_key(self.main_root, "origin")

    def alias(self, name):
        rc, out = C.git(["config", "--get", "alias." + name], self.main_root)
        return out if rc == 0 and out else None

    def staged_files(self, cdir, include_worktree=False):
        rc, out = C.git(["diff", "--cached", "--name-only"], cdir)
        files = set(out.split("\n")) if rc == 0 and out else set()
        if include_worktree:
            rc, out = C.git(["diff", "--name-only"], cdir)
            if rc == 0 and out:
                files |= set(out.split("\n"))
        return sorted(f for f in files if f)

    def changed_vs_main(self, cdir):
        rc, out = C.git(["diff", "--name-only", REMOTE_MAIN + "...HEAD"], cdir)
        return sorted(f for f in out.split("\n") if f) if rc == 0 else None

    def head_blobs(self, cdir):
        return C.head_blobs(cdir)

    def merge_head(self, cdir):
        """這個資料夾正在合併的那個 commit（沒有進行中的合併回 None）。"""
        rc, out = C.git(["rev-parse", "-q", "--verify", "MERGE_HEAD^{commit}"], cdir)
        return out if rc == 0 and out else None

    def gh_verify_runs(self, cfg, sha):
        """GitHub 上驗收機對這個 commit 的執行紀錄（唯讀；只有「重跑驗收」那條規則會問）。查不到回 None。"""
        import iw_review as RV                                      # 用到才載入：守門平常不連 GitHub
        v = cfg.get("verify") or {}
        s = RV.slug(self.main_root, cfg)
        if not v or not s:
            return None
        obj, _err = RV.gh_json(["api", "repos/%s/actions/workflows/%s/runs?head_sha=%s&per_page=100" % (s, v["workflow"], sha)], hints={"sha": sha})
        if not isinstance(obj, dict) or not isinstance(obj.get("workflow_runs"), list):
            return None
        return obj["workflow_runs"]


# ---------------------------------------------------------------- 情境

class Ctx(object):
    def __init__(self, main_root, state, cfg, allow, git, inp, home=None, now=None):
        self.main_root = main_root
        self.main_key = C.key(main_root)
        self.state = ST.normalize(dict(state)) if state else {}     # 舊版（P1）寫的 paused／stopped 也看得懂
        self.cfg = cfg
        self.allow = allow
        self.git = git
        self.inp = inp or {}
        self.home = (home or C.home_dir()).replace("\\", "/")
        self.home_key = C.key(self.home)
        self.main_real = C.real_key(main_root) or self.main_key       # 解開捷徑類的東西之後的實際位置
        self.home_real = C.real_key(self.home) or self.home_key
        self.now = now or C.now()
        self.effects = []
        self.cwd = self.inp.get("cwd") or main_root
        self.scratch_key = C.key(self.inp.get("scratchpad_dir")) if self.inp.get("scratchpad_dir") else None

    @property
    def autopilot(self):
        st = self.state
        return bool(st.get("active")) and st.get("session_id") == self.inp.get("session_id")

    @property
    def stage(self):
        return self.state.get("stage")

    def worktree_keys(self):
        base = self.main_key + "/" + self.cfg["worktreeDir"].strip("/").lower() + "/" + (self.cfg["tagPrefix"] + (self.stage or "")).lower()
        return [base, base + "-merge"]

    def write_roots(self):
        roots = self.worktree_keys() + [self.main_key + "/.autopilot"]
        if self.scratch_key:
            roots.append(self.scratch_key)
        return roots


# 家目錄底下會影響 shell 或 git 行為的檔：shell 的啟動檔（Bash 工具每次都會讀）、git 的個人設定
_USER_FILES = re.compile(r"^(\.bashrc|\.bash_profile|\.bash_login|\.bash_logout|\.profile|\.zshrc|\.zshenv|\.zprofile|\.inputrc|"
                         r"\.gitconfig|\.config/git(/.*)?|\.config/gh(/.*)?|appdata/roaming/github cli(/.*)?|"
                         r"\.ssh(/.*)?|\.claude\.json)$")
# 存登入憑證或權杖的地方：連用指令「讀」都擋（瀏覽器的資料夾裡有本站設定頁存的 PAT）
_SECRET_FILES = re.compile(r"^(\.git-credentials|\.config/gh/hosts\.yml|appdata/roaming/github cli/hosts\.yml|\.ssh/id_[^/]*|"
                           r"\.ssh/[^/]*\.pem|appdata/local/(google/chrome|microsoft/edge|bravesoftware/[^/]+|chromium)/user data(/.*)?|"
                           r"appdata/roaming/mozilla/firefox/profiles(/.*)?|library/application support/(google/chrome|microsoft edge)(/.*)?)$")


def _classify_key(k, main_key, home_key, ctx):
    if k is None:
        return None, None
    if C.is_under(k, main_key + "/.git"):
        return "gitdir", "git 的內部檔（.git）"
    wt_base = main_key + "/" + ctx.cfg["worktreeDir"].strip("/").lower()
    if C.is_under(k, wt_base):
        rel = C.rel_to(k, wt_base)
        sub = rel.split("/", 1)[1] if "/" in rel else ""
        if sub == ".git" or sub.startswith(".git/"):
            return "gitdir", "worktree 的 .git"
        if sub and C.glob_match(sub, ctx.cfg["selfFiles"]["paths"]):
            return "copy", "自動駕駛自己的檔（worktree 裡的副本）"
        return None, None
    if C.is_under(k, main_key):
        rel = C.rel_to(k, main_key)
        if _SELF_RE.search("/" + rel):
            return "live", "生效中的保護檔（%s）" % rel
        return None, None
    if C.is_under(k, home_key):
        rel = C.rel_to(k, home_key)
        if rel.startswith(".claude/"):
            sub = rel[len(".claude/"):]
            if re.match(r"^(settings(\.local)?\.json|hooks|agents|skills)(/|$)", sub):
                return "user", "你個人層級的 Claude Code 設定（~/.claude/%s）" % sub
            if sub.startswith("projects/") and sub.endswith(".jsonl"):
                return "transcript", "對話紀錄"
            return None, None
        if _SECRET_FILES.match(rel):
            return "secret", "存登入憑證或權杖的地方（~/%s）" % rel.split("/user data/")[0]
        if _USER_FILES.match(rel) or re.search(r"powershell/[^/]*profile\.ps1$", rel):
            return "user", "你個人層級的設定（shell 的啟動檔、git 的設定：~/%s）" % rel
        return None, None
    if k.endswith("/etc/gitconfig") or k.endswith("/etc/bash.bashrc") or k.endswith("/etc/profile"):
        return "user", "整台電腦共用的 git 或 shell 設定"
    return None, None


def _names_state_dir(low):
    """路徑裡有沒有提到狀態資料夾（.git/iw-autopilot）。
    流程檔的資料夾（.claude/skills/iw-autopilot）剛好同名：只看「有沒有這幾個字」的話，流程檔連 worktree 裡的副本都改不了
    （2026-10-02 實際發生：一般模式下要改 SKILL.md 被當成「寫入狀態資料夾」擋下）。
    所以：某一段含有這個名字就算，唯一的例外是「前一段是 skills、這一段剛好就是這個名字」——那是流程檔，
    交給後面「自動駕駛自己的檔」的規則（主目錄那一份照樣擋；worktree 的副本在自動駕駛期間照樣擋）。"""
    segs = [s.rstrip(". ") for s in low.split("/")]
    for i, s in enumerate(segs):
        if C.STATE_DIR_NAME in s and not (s == C.STATE_DIR_NAME and i > 0 and segs[i - 1] == "skills"):
            return True
    return False


def classify_path(path_text, cwd, ctx, dynamic=False):
    """這個路徑屬於哪一種受保護的東西。回傳 (種類, 白話名稱) 或 (None, None)。
    種類：state／gitdir／live（生效中的保護檔）／copy（worktree 裡的副本）／user（使用者層級的設定）／transcript
    dynamic：這個字裡有執行時才知道的東西（變數沒有被代回去），只能看字面。"""
    t = path_text.replace("\\", "/")
    t = re.sub(r"(:+\$[A-Za-z0-9_]+)+", "", t)                      # 檔名::$DATA、資料夾:$I30:$INDEX_ALLOCATION：同一個東西的另一種寫法，那個 $ 不是變數
    low = t.lower()
    unresolved = dynamic or "$" in t or "`" in t or "%" in t or (t.startswith("~") and not (t == "~" or t.startswith("~/")))
    if _names_state_dir(low) or (unresolved and C.STATE_DIR_NAME in low):       # 路徑裡有變數：看不出是哪一份，照舊「有這幾個字就算」
        return "state", "自動駕駛的通行證與狀態資料夾"
    if not unresolved:
        kind, name = _classify_key(C.key(t, cwd), ctx.main_key, ctx.home_key, ctx)
        if kind is None:                                            # 再用「解開捷徑之後的實際位置」看一次（資料夾連結、短檔名）
            rk = C.real_key(t, cwd)
            if rk is not None:
                kind, name = _classify_key(rk, ctx.main_real, ctx.home_real, ctx)
        return kind, name
    # 解不出完整路徑（裡面有變數）：只能看字面
    if re.search(r"(^|/)\.git(/|$)", low):
        return "gitdir", "git 的內部檔（.git）"
    m = _SELF_RE.search(low)
    if m:
        if "/.claude/worktrees/" in low[:m.start() + 1]:
            return "copy", "自動駕駛自己的檔（worktree 裡的副本）"
        return "live", "保護檔（路徑裡有變數，無法確認是不是生效的那一份）"
    if ".claude.json" in low or ".gitconfig" in low or ".bashrc" in low or ".bash_profile" in low:
        return "user", "你個人層級的設定"
    if ".claude/projects/" in low and ".jsonl" in low:
        return "transcript", "對話紀錄"
    return None, None


def covers_protected(path_text, cwd, ctx):
    """這個路徑是不是「包住保護檔的資料夾」（主目錄、主目錄的 .claude、家目錄、它們的上層）。
    刪掉、搬走或整個複製這種資料夾，保護就跟著沒了。"""
    roots = ((ctx.main_key + "/.claude", ctx.home_key + "/.claude"), (ctx.main_real + "/.claude", ctx.home_real + "/.claude"))
    for k, pair in ((C.key(path_text, cwd), roots[0]), (C.real_key(path_text, cwd), roots[1])):
        if k is not None and any(C.is_under(r, k) for r in pair):
            return True
    return False


# ---------------------------------------------------------------- 入口

def decide(inp, ctx):
    """回傳 (Block 或 None, effects)。"""
    try:
        _decide(inp, ctx)
        return None, ctx.effects
    except Block as b:
        return b, ctx.effects


def _decide(inp, ctx):
    tool = inp.get("tool_name") or ""
    ti = inp.get("tool_input") if isinstance(inp.get("tool_input"), dict) else {}
    auto = ctx.autopilot
    if auto:
        _autopilot_gate(tool, ti, ctx)                              # 模型、強度、時間、停止狀態
    if tool == "Bash":
        _shell(str(ti.get("command") or ""), "bash", ctx, auto)
    elif tool == "PowerShell":
        if auto:
            raise Block("自動駕駛期間不用 PowerShell 工具（清單以外）。請改用 Bash，或停下來寄信說明為什麼需要。", 8)
        _shell(str(ti.get("command") or ""), "powershell", ctx, auto)
    elif tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        _file_write(ti.get("file_path") or ti.get("notebook_path") or "", tool, ctx, auto)
    if MESSAGING_TOOLS.match(tool):
        blob = _SQUEEZE.sub("", _strings(ti))                       # 中間插空白、全形空白、零寬字元都不算數
        if any(w in blob for w in APPROVAL_WORDS):
            raise Block("這個工具會替人送出訊息，內容裡不可以有 David 的指令詞（放行、自動駕駛）——那幾句只能由他親手輸入。", 1)
    if auto:
        _autopilot_tool(tool, ti, ctx)


def _strings(obj):
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "\n".join(_strings(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return "\n".join(_strings(v) for v in obj)
    return ""


# ---------------------------------------------------------------- 自動駕駛：模型、強度、時間、停止狀態

def _autopilot_gate(tool, ti, ctx):
    st, cfg = ctx.state, ctx.cfg
    need_model, need_effort = cfg["requiredModel"], cfg["requiredEffort"]
    model_label, effort_label = cfg.get("requiredModelLabel") or need_model, cfg.get("requiredEffortLabel") or need_effort
    effort = ((ctx.inp.get("effort") or {}).get("level")) if isinstance(ctx.inp.get("effort"), dict) else None
    model = ST.norm_model(ctx.inp.get("_observed_model") or "") or None
    approved_models = set([need_model] + list(st.get("model_approved") or []))
    approved_efforts = set([need_effort] + list(st.get("effort_approved") or []))      # 中途由 David 手打「放行模型」臨時放行的強度（只有 Max 可以）
    approvable = list(cfg.get("approvableEfforts") or [])

    first_start =st.get("status") == "pending" and not st.get("activated_at")
    if first_start:                                                 # 啟動後的第一個動作：這時才看得到強度
        if effort is not None and effort != need_effort:
            ctx.effects.append(("deactivate", "思考強度是 %s，不是 %s" % (effort, need_effort)))
            raise Block("自動駕駛沒有啟動：思考強度現在是「%s」，規定是「%s（%s）」。\n"
                        "請告訴 David：用輸入框旁邊的思考強度選單（Effort；快速鍵 Ctrl+Shift+E）改成 %s，再重新輸入「自動駕駛：%s」。Max 也不行，只認 %s。"
                        % (effort, need_effort, effort_label, effort_label, st.get("stage"), effort_label), 9)
        if model is not None and model not in approved_models:
            ctx.effects.append(("deactivate", "模型是 %s，不是 %s" % (model, need_model)))
            raise Block("自動駕駛沒有啟動：現在的模型是「%s」，規定是「%s」。\n請告訴 David：用輸入框旁邊的模型選單改回 %s，"
                        "再重新輸入「自動駕駛：%s」。" % (model, need_model, model_label, st.get("stage")), 9)
        ctx.effects.append(("activate", {"effort": effort, "model": model}))
    else:
        if st.get("model_violation") and st["model_violation"].get("model") not in approved_models:
            raise Block("模型被換成「%s」，自動駕駛已暫停。要用它繼續，請 David 輸入「放行模型」；"
                        "或等額度恢復、把模型改回來之後輸入「繼續 %s」。" % (st["model_violation"].get("model"), st.get("stage")), 9)
        if model is not None and model not in approved_models:
            ctx.effects.append(("model_violation", model))
            raise Block("模型被換成「%s」，自動駕駛已暫停（已寄信）。要用它繼續，請 David 輸入「放行模型」；"
                        "或等額度恢復後輸入「繼續 %s」。" % (model, st.get("stage")), 9)
        if effort is not None and effort not in approved_efforts:
            can = effort in approvable
            ctx.effects.append(("pause", "effort", "思考強度被改成 %s（規定 %s）%s" % (effort, need_effort, "；這個強度可以臨時放行：輸入「放行模型」" if can else ""), 9, True))
            ctx.effects.append(("effort_violation", effort))
            raise Block("思考強度被改成「%s」（規定是 %s），自動駕駛已暫停（已寄信）。%s請 David 用思考強度選單（Ctrl+Shift+E）改回 %s，再輸入「繼續 %s」。"
                        % (effort, need_effort, "要用它跑這一輪（卡住的難題才用），請 David 輸入「放行模型」；不然" if can else "", effort_label, st.get("stage")), 9)
        if st.get("status") == "pending":                           # 「繼續／修改」之後的第一個動作：模型與強度都對，接著做
            ctx.effects.append(("activate", {"effort": effort, "model": model}))

    reason = stop_reason(ctx)
    if reason and not _is_wrapup(tool, ti, ctx):
        if ST.is_paused(st):
            ctx.effects.append(("pause_retry",))                    # 暫停中又試了不准的動作：記下來（信的等級不再是最輕的那一級）
        raise Block(reason + "\n現在只能做三件事：寫停止報告（.autopilot/runs/%s/）、執行寄信的指令、唯讀地查看。" % ctx.stage,
                    (st.get("stop_required") or st.get("pause") or {}).get("code"))
    ctx.effects.append(("heartbeat", effort, model))


def stop_reason(ctx):
    """現在是不是「該停下來」的狀態。是就回原因。
    暫停（pause）與程式判定的停止條件（stop_required）只有 David 手打的指令能解除；停在合併前（awaiting_approval）只認「放行／修改」。
    done（文件那一筆已經推上去）不鎖：收 worktree、刪分支、關信照常做，被擋就是一般的暫停（David 的裁決：認「繼續」）。"""
    st, cfg = ctx.state, ctx.cfg
    stage = st.get("stage")
    sr = st.get("stop_required")
    if sr:
        return "已經碰到停止條件 %s：%s。要 David 輸入「繼續 %s」才接著做。" % (sr.get("code"), sr.get("reason"), stage)
    p = st.get("pause")
    if p:
        word = "放行" if ST.merged_awaiting_docs(st) else "繼續"
        return "自動駕駛暫停中（%s）。要 David 輸入「%s %s」才接著做。" % (p.get("detail") or p.get("reason") or "原因見信", word, stage)
    status = st.get("status")
    if status == "awaiting_approval":
        return "已經停在合併前，等 David 輸入「放行 %s」或「修改 %s：＿＿」。" % (stage, stage)
    t0 = C.parse_iso(st.get("clock_started_at"))
    if t0 is not None and (ctx.now - t0).total_seconds() > 3600.0 * float(cfg.get("maxHours", 8)):
        ctx.effects.append(("stop_required", 9, "超過時間上限 %s 小時" % cfg.get("maxHours", 8)))
        return "已經碰到停止條件 9：這一輪超過時間上限 %s 小時。" % cfg.get("maxHours", 8)
    return None


def _notify_command(argv, cwd, ctx):
    """python <主目錄>/.claude/hooks/iw_notify.py …（寄信、登記要合併的 commit）"""
    if not argv or prog_name(argv[0]) not in ctx.allow["programs"]["pythonInterpreters"]:
        return False
    for a in argv[1:]:
        if a.endswith(".py"):
            return C.key(a, cwd) == ctx.main_key + "/.claude/hooks/iw_notify.py"
        if not a.startswith("-") and not re.match(r"^[0-9.]+$", a) and a not in ("utf8", "ignore"):
            return False
    return False


def _runs_key(ctx):
    """這個階段的報告資料夾（.autopilot/runs/<階段>/）：暫停期間唯一准寫的地方。"""
    return ctx.main_key + "/.autopilot/runs/" + (ctx.stage or "").lower()


def _is_wrapup(tool, ti, ctx):
    """暫停（或停在合併前）期間唯一准做的事（規格 P1-1 第 1 節：讀檔與搜尋、寫報告、寄信；清單越窄越好）：
      * 讀檔與搜尋：Read／Grep／Glob（不經過 hook）
      * 寫報告：Write／Edit 只准寫 .autopilot/runs/<階段>/（暫存資料夾、.autopilot/ 其他地方都不准）
      * 寄信：主目錄那一支 iw_notify.py（send／status／retry／close）
      * 唯讀地查看：唯讀程式與 git 的唯讀子指令——不准 git -c、不准會寫檔的選項（--output、sed -i、sort -o…）、不准 tee、不准 heredoc；
        輸出若轉向，只准到 .autopilot/runs/<階段>/；用 ;、&&、管線串在後面的寫入指令一樣擋
      * Skill、ToolSearch、TodoWrite、SubagentHandback（都不動任何檔）
    其他一律擋。"""
    if tool in PAUSE_TOOLS:
        return True
    runs = _runs_key(ctx)
    if tool in ("Write", "Edit"):
        k = C.key(str(ti.get("file_path") or ""), ctx.cwd)
        return k is not None and C.is_under(k, runs)
    if tool == "Bash":
        try:
            parsed = S.parse_bash(str(ti.get("command") or ""), {"HOME": ctx.home})
        except S.ParseError:
            return False
        if parsed.complex or parsed.heredocs:
            return False
        cwd = ctx.cwd
        for c in parsed.cmds:
            argv = c.argv
            if not argv:
                if c.redirs:
                    return False
                continue
            if c.words[0].opaque:
                return False
            p = prog_name(argv[0])
            if p in ("cd", "pushd"):
                if len(argv) < 2 or c.words[1].opaque:
                    return False
                cwd = C.norm(argv[1], cwd) or cwd
            for op, w in c.redirs:                                  # 輸出只能轉向到報告資料夾（寄信那一支也一樣）
                if op in ("<<<",):
                    return False
                if ">" in op and w.text not in ("/dev/null",) and not re.match(r"^[0-9]+$", w.text):
                    k = C.key(w.text, cwd) if not w.opaque else None
                    if k is None or not C.is_under(k, runs):
                        return False
            if _notify_command(argv, cwd, ctx):
                if "pr" in argv[1:]:                                # 暫停中不開 PR、不改 PR、不留言（那不是「寫報告、寄信、唯讀查看」）
                    return False
                continue
            if p == "git" and _git_sub(argv)[3]:                   # git -c：臨時改設定，暫停中不准
                return False
            if not _is_readonly(p, c.words, "bash"):
                return False
        return True
    return False


def _mutating_flags(p, argv):
    """平常只是讀的程式，加了這些選項就會寫檔或執行別的指令。"""
    args = argv[1:]
    positional = [a for a in args if not a.startswith("-")]
    if p == "sed":
        return any(a.startswith("-i") or a.startswith("--in-place") or a in ("-f", "--file") or a.startswith("--file=") or
                   (a.startswith("-") and not a.startswith("--") and ("i" in a[1:] or "f" in a[1:])) for a in args)
    if p == "find":
        return any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls") for a in args)
    if p == "awk":
        return any(a in ("-i", "inplace", "-f", "--file") or a.startswith("--file=") or a.startswith("-f") for a in args)
    if p == "sort":
        return any(a == "-o" or (a.startswith("-o") and not a.startswith("--")) or a.startswith("--output") for a in args)
    if p == "uniq":
        return len(positional) >= 2                                 # uniq 輸入 輸出
    if p == "xxd":
        return any(a.startswith("-r") for a in args) or len(positional) >= 2
    if p in ("less", "more"):
        return any(a.startswith("-o") or a.startswith("-O") or a.startswith("--log-file") or a.startswith("--LOG-FILE") for a in args)
    if p == "date":
        return any(a == "-s" or a.startswith("--set") for a in args)
    if p == "yq":
        return any(a == "-i" or a.startswith("-i=") or a.startswith("--inplace") for a in args)
    if p in ("tree", "iconv"):
        return any(a.startswith("-o") or a.startswith("--output") for a in args)
    if p == "rg":
        return any(a == "--pre" or a.startswith("--pre=") or a.startswith("--hostname-bin") for a in args)
    if p in ("export", "set"):
        return False
    return False


# ---------------------------------------------------------------- 自動駕駛：工具清單

def _autopilot_tool(tool, ti, ctx):
    allow = ctx.allow["tools"]
    if tool == "Agent":
        kind = str(ti.get("subagent_type") or "")
        if kind not in allow["agentTypes"]:
            raise Block("自動駕駛期間只能用審查代理（%s）這一個子代理；「%s」不在清單裡。" % ("、".join(allow["agentTypes"]), kind or "預設"), 8)
        if ti.get("model"):
            raise Block("呼叫審查代理不可以帶 model 參數（會蓋過它檔案裡寫死的模型）。", 8)
        if ti.get("isolation"):
            raise Block("審查代理不需要 isolation 參數。", 8)
        head = _review_header(str(ti.get("prompt") or ""))
        if head is None:
            raise Block("呼叫審查代理的提示開頭要有三行：REVIEW-KIND: restatement 或 acceptance／STAGE: <階段>／COMMIT: <commit 或 none>。", 8)
        if head["stage"] != ctx.stage:
            raise Block("審查的階段（%s）跟現在的階段（%s）不一樣。" % (head["stage"], ctx.stage), 8)
        rounds = [r for r in (ctx.state.get("reviews") or []) if r.get("kind") == head["kind"] and r.get("epoch") == ctx.state.get("epoch")]
        limit = int(ctx.cfg["reviewer"]["maxRounds"])
        if len(rounds) >= limit and not any(r.get("verdict") == "APPROVE" for r in rounds[-1:]):
            ctx.effects.append(("stop_required", 7, "審查代理 %d 輪後仍不批准" % limit))
            raise Block("停止條件 7：審查代理已經審了 %d 輪仍不批准。請寫停止報告、寄信，由 David 決定。" % limit, 7)
        ctx.effects.append(("review_begin", head))
        return
    if tool in ("Bash", "Edit", "Write"):
        return
    if tool.startswith("mcp__Claude_Browser__"):
        if tool not in allow["allow"]:
            raise Block("這個瀏覽器工具不在自動駕駛的清單裡：%s" % tool, 8)
        for url in _urls_in(ti):
            host = re.sub(r"^[a-z]+://", "", url.lower()).split("/")[0].split(":")[0]
            if host not in allow["browserHosts"]:
                raise Block("自動駕駛期間內建瀏覽器只能開本機預覽（%s）；「%s」不在清單裡。" % ("、".join(allow["browserHosts"]), host), 8)
        return
    if tool == "AskUserQuestion":
        raise Block("自動駕駛期間不用「問問題」的工具（David 不在電腦前）。需要他決定的事：寫進停止報告、寄信、停下來。", 2)
    if tool not in allow["allow"]:
        raise Block("工具「%s」不在自動駕駛的清單裡（停止條件 8）。需要它的話：寫停止報告、寄信，不要換別的方法繞過。" % tool, 8)


def _urls_in(obj):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "url" and isinstance(v, str) and v not in ("back", "forward"):
                out.append(v)
            else:
                out.extend(_urls_in(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_urls_in(v))
    return out


def _review_header(prompt):
    m = re.match(r"^\s*REVIEW-KIND:\s*(restatement|acceptance)\s*\n\s*STAGE:\s*(\S+)\s*\n\s*COMMIT:\s*(\S+)\s*(\n|$)", prompt)
    if not m:
        return None
    return {"kind": m.group(1), "stage": m.group(2), "commit": m.group(3)}


# ---------------------------------------------------------------- 改檔工具

def _file_write(path, tool, ctx, auto):
    if not path:
        return
    path = str(path)
    weird = C.weird_path(path)
    if weird:
        raise Block("看不懂這個路徑的寫法（%s），看不出實際會寫到哪個檔。請用一般的完整路徑。" % weird, 3)
    kind, name = classify_path(path, ctx.cwd, ctx)
    if kind in ("state", "gitdir", "live", "user", "transcript", "secret"):
        raise Block("不能寫入%s。這是保護的一部分，只有 David 自己動手、或經過「放行」合併才能改。" % name, 3)
    if not auto:
        return
    if kind == "copy":
        raise Block("自動駕駛期間不能改自動駕駛自己的檔（連 worktree 裡的副本也不行）：%s。要改就停下來寄信，改成一般的階段處理。" % name, 3)
    k = C.key(path, ctx.cwd)
    if k is None:
        raise Block("看不懂這個路徑：%s" % path, 8)
    if C.is_under(k, ctx.home_key + "/.claude/projects") and "/memory/" in k and k.endswith(".md"):
        return                                                      # 記憶檔
    roots = ctx.write_roots()
    if not any(C.is_under(k, r) for r in roots):
        raise Block("自動駕駛期間只能改這個階段的 worktree、.autopilot/ 與暫存資料夾裡的檔；「%s」在外面（主目錄不能直接改）。" % path, 8)
    for wt in ctx.worktree_keys():
        if C.is_under(k, wt):
            _tier_check([C.rel_to(k, wt)], ctx, "改")
            break


def not_preexisting(files, state, blobs):
    """把「啟動自動駕駛之前就已經在這個分支上、而且內容到現在一個字沒變」的檔拿掉，回傳剩下的。
    那些是 David 在場時做的變更（例如 P1 自己：它的分支本來就是在改保護檔），不算「自動駕駛動了它」；
    它們照樣會列在停止報告裡給 David 看。只要內容跟啟動當時不一樣（自動駕駛又改了它），就不算在內。"""
    pre = (state or {}).get("preexisting") or {}
    if not pre or blobs is None:
        return list(files)
    return [f for f in files if not (f in pre and blobs.get(f, "(deleted)") == pre[f])]


def _tier_check(rels, ctx, verb):
    cfg = ctx.cfg
    for rel in rels:
        if not rel:
            continue
        if C.glob_match(rel, cfg["tier1"]["paths"]):
            ctx.effects.append(("stop_required", 3, "需要動到 %s（排程、流程、資料來源或隱私相關，動不得）" % rel))
            raise Block("停止條件 3：%s 是自動駕駛期間動不得的檔（排程、流程、資料來源或隱私相關）。不要%s它——寫停止報告、寄信，由 David 決定。" % (rel, verb), 3)
        if C.glob_match(rel, cfg["selfFiles"]["paths"]):
            ctx.effects.append(("stop_required", 3, "需要動到自動駕駛自己的檔 %s" % rel))
            raise Block("停止條件 3：%s 是自動駕駛自己的檔，自動駕駛期間不能%s。寫停止報告、寄信，由 David 決定。" % (rel, verb), 3)
        if C.glob_match(rel, cfg["tier2"]["paths"]):
            ctx.effects.append(("tier2", rel))


# ---------------------------------------------------------------- shell 指令

_RAW_TRIPWIRES = [
    (re.compile(r"\b(CLAUDECODE|CLAUDE_CODE_CHILD_SESSION)\b"), "動到 Claude Code 用來辨識自己的環境變數"),
    (re.compile(r"\bgit[\w.\-]*\s+credential|credential-manager|\bgh\s+auth\s+(token|refresh|login|logout|setup-git|switch)\b", re.I), "讀或改登入憑證"),
    (re.compile(r"\benv\s+(-i\b|--ignore-environment\b|-u\b|--unset\b)", re.I), "清掉環境變數再執行"),
    (re.compile(r"IW_TEST_NO_SIDE_EFFECTS"), "設定測試專用的開關（會讓通知信不寄、本機通知不跳）"),
]
_IFS_OK = re.compile(r"\bIFS=(''|\"\")?[ \t]+read\b")
# shell 或直譯器「一啟動就照它去執行別的程式」的環境變數
_CODE_ENV_RE = re.compile(r"\b(BASH_ENV|PROMPT_COMMAND|PS4|SHELLOPTS|BASHOPTS|LD_PRELOAD|DYLD_INSERT_LIBRARIES|PYTHONSTARTUP|PERL5OPT|"
                          r"RUBYOPT|NODE_OPTIONS|GIT_ASKPASS|SSH_ASKPASS)\s*=")
_EXEC_ENV_RE = re.compile(r"\b(" + "|".join(sorted(EXEC_ENV)) + r")\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s;&|)]*)")
_INLINE_PUSH = re.compile(r"\b(git|gh)(\.exe)?\b[^\n]{0,200}\b(push|send-pack|http-push|receive-pack|update-ref|filter-branch|"
                          r"merge|rebase|credential|hookspath|symbolic-ref)\b", re.I)
_GH_API_WRITE_PATH = re.compile(r"/(git|contents|merges|pulls|branches|rulesets|hooks|keys|actions/secrets|actions/variables)(/|\b)", re.I)

# 會在 Claude Code 之外另外執行指令的程式（排程、遠端、另一個身分）：那樣執行的指令不經過這裡的檢查
DETACHERS = set("""at crontab launchctl wmic psexec runas ssh invoke-wmimethod invoke-cimmethod register-scheduledtask
register-scheduledjob new-scheduledtask new-scheduledtaskaction new-scheduledtasktrigger set-scheduledtask start-job sajb
invoke-command icm""".split())


def _shell(command, shell, ctx, auto):
    raw = command
    for rx, what in _RAW_TRIPWIRES:
        if rx.search(raw):
            raise Block("這段指令會%s，不允許。" % what, 3)
    if re.search(r"\bIFS\b", _IFS_OK.sub("", raw)):
        raise Block("這段指令用到或改了 IFS（它可以把一個字拆成好幾個字，檢查程式就看不準實際執行的是什麼），不允許。"
                    "「while IFS= read -r 行」這個寫法可以用。", 3)
    for m in re.finditer(r"\bGIT_[A-Z_]+(?=\s*=)", raw):
        if m.group(0) not in GIT_ENV_OK:
            raise Block("這段指令設定了 %s（會改變 git 找倉庫或設定的方式），不允許。" % m.group(0), 3)
    m = _CODE_ENV_RE.search(raw)
    if m:
        raise Block("這段指令設定了 %s（shell 或直譯器一啟動就會照它去執行別的程式），不允許。" % m.group(1), 3)
    for m in _EXEC_ENV_RE.finditer(raw):
        if m.group(2).strip("\"'") not in EXEC_ENV_SAFE:
            raise Block("這段指令把 %s 設成別的程式（git 會把它當成指令執行），不允許。只能設成 true 或 cat 這類不做事的值。" % m.group(1), 3)
    if re.search(r"api\.github\.com", raw, re.I) and re.search(r"(-X\s*(POST|PATCH|PUT|DELETE)|--request\s+(POST|PATCH|PUT|DELETE)|"
                                                                  r"\s-d\b|--data|--json|\s-F\b|--form|-Method\s+(Post|Patch|Put|Delete))", raw, re.I):
        raise Block("直接呼叫 GitHub API 做寫入，不允許（改分支、合併、改檔都要經過放行）。", 1)
    sensitive = bool(re.search(r"\b(git|gh)(\.exe)?\b", raw, re.I)) or classify_text_has_protected(raw)
    try:
        if shell == "bash":
            parsed = S.parse_bash(raw, {"HOME": ctx.home})
        else:
            parsed = S.parse_powershell(raw)
    except S.ParseError as e:
        # 一律擋：bash 是一行一行執行的，後面有語法錯誤，前面幾行照樣會跑——「看不懂就放行」等於整段都沒檢查
        raise Block("看不懂這段指令的寫法（%s）。請改成簡單的寫法，或把邏輯寫成 Python 腳本。" % e, 8 if auto else None)
    if parsed.complex and (auto or sensitive):
        raise Block("這段指令用了 case、函式定義或算術指令這類看不完整的寫法。請改成簡單的寫法，或把邏輯寫成 Python 腳本。", 8 if auto else None)
    for body in parsed.heredocs:
        _inline_code(body, ctx)
    _commands(parsed.cmds, shell, ctx.cwd, ctx, auto, raw, 0)


def classify_text_has_protected(raw):
    low = raw.replace("\\", "/").lower()
    return bool(_SELF_TEXT_RE.search(low) or re.search(r"(^|[\s\"'=/])\.git(/|\b)", low) or C.STATE_DIR_NAME in low or ".claude.json" in low
                or (".claude/projects/" in low and ".jsonl" in low))


def _inline_code(code, ctx):
    """python -c、heredoc、awk 與 sed 的程式這類「直接寫在指令裡的程式」：看不到它實際做什麼，只能看字面。"""
    if _INLINE_PUSH.search(code) or re.search(r"--no-verify|hookspath", code, re.I):
        raise Block("直接寫在指令裡的程式碼出現了 git 的推送、合併、改寫歷史或跳過 hook 的字樣，不允許。", 1)
    low = code.replace("\\", "/").lower()
    if C.STATE_DIR_NAME in low or _SELF_TEXT_RE.search(low) or re.search(r"(^|[\s\"'/(=])\.git/(hooks|config|info)", low) \
            or ".claude.json" in low or (".claude/projects/" in low and ".jsonl" in low) or ".gitconfig" in low or ".bashrc" in low:
        raise Block("直接寫在指令裡的程式碼提到了保護檔的路徑，不允許（要看內容請用讀檔工具或 cat）。", 3)


def _unwrap(words):
    """拿掉 env、timeout、xargs 這類「幫別人執行」的外殼。回傳 (剩下的字, 註記集合)。"""
    notes = set()
    w = list(words)
    for _ in range(12):
        if not w or w[0].opaque:
            break
        p = prog_name(w[0].text)
        rest = w[1:]
        if p == "env":
            while rest:
                t = rest[0].text
                if t in ("-i", "--ignore-environment", "-"):
                    notes.add("env-strip")
                    rest = rest[1:]
                elif t in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                    notes.add("env-strip" if t in ("-u", "--unset") else "env-opt")
                    rest = rest[2:]
                elif t.startswith("-"):
                    if t.startswith("-S") or t.startswith("--split-string") or t.startswith("-u") or t.startswith("--unset"):
                        notes.add("env-strip")
                    rest = rest[1:]
                elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", rest[0].raw):
                    notes.add("assign:" + t.split("=", 1)[0])
                    notes.add("assignval:" + t)
                    rest = rest[1:]
                else:
                    break
            if not rest:
                return w, notes
            w = rest
            continue
        if p == "command":
            if rest and rest[0].text in ("-v", "-V"):
                return w, notes
            while rest and rest[0].text.startswith("-"):
                rest = rest[1:]
            w = rest
            continue
        if p == "timeout":
            while rest and rest[0].text.startswith("-"):
                rest = rest[2:] if rest[0].text in ("-k", "-s", "--kill-after", "--signal") else rest[1:]
            w = rest[1:]
            continue
        if p == "nice":
            while rest and rest[0].text.startswith("-"):
                rest = rest[2:] if rest[0].text in ("-n", "--adjustment") else rest[1:]
            w = rest
            continue
        if p in ("stdbuf", "time"):
            while rest and rest[0].text.startswith("-"):
                rest = rest[1:]
            w = rest
            continue
        if p == "xargs":
            notes.add("xargs")
            while rest and rest[0].text.startswith("-"):
                rest = rest[2:] if rest[0].text in ("-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a", "--max-args", "--max-procs",
                                                     "--delimiter", "--arg-file", "--replace") and len(rest[0].text) <= 12 else rest[1:]
            w = rest
            continue
        if p in ("builtin", "nohup", "exec", "sudo", "winpty", "setsid", "wsl", "call"):
            while rest and rest[0].text.startswith("-") and p in ("exec", "sudo", "wsl"):
                rest = rest[1:]
            w = rest
            continue
        break
    return w, notes


def _is_readonly(p, words, shell):
    """這個指令是不是「確定只是讀」。"""
    if p is None:
        return False
    argv = [w.text for w in words]
    if p == "git":
        return _git_sub(argv)[0] in READONLY_GIT and not any(a.startswith("--output") or a.startswith("--open-files-in-pager") or
                                                             a.startswith("--upload-pack") or a.startswith("--exec") for a in argv[1:])
    if shell in ("powershell", "cmd"):
        return p in READONLY_PS or (p in ("echo", "type", "dir", "more", "find", "findstr", "where", "ver", "vol", "set", "rem") and shell == "cmd")
    if p == "command":                                              # command -v python：只是查這個名字是哪個程式，不會執行它
        return len(argv) > 1 and argv[1] in ("-v", "-V")
    if p == "find":                                                 # find … -exec 指令：那個指令只是讀，整個就只是讀
        if any(a in ("-delete", "-fprint", "-fprint0", "-fprintf", "-fls") for a in argv[1:]):
            return False
        return all(sub and _is_readonly(prog_name(sub[0].text), sub, shell) and not sub[0].opaque for sub in _find_subcommands(words))
    return p in READONLY_PROGRAMS and not _mutating_flags(p, argv)


def _find_subcommands(words):
    """find 的 -exec／-execdir／-ok／-okdir 後面那幾段指令（每段是一串字）。"""
    out, i = [], 1
    while i < len(words):
        if words[i].text in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            while j < len(words) and words[j].text not in (";", "+"):
                j += 1
            out.append(words[i + 1:j])
            i = j
        i += 1
    return out


def _commands(cmds, shell, cwd, ctx, auto, raw, depth):
    if depth > 6:
        raise Block("指令包了太多層。", 8 if auto else None)
    for c in cmds:
        for name, v in c.assign:
            if name.startswith("GIT_") and name not in GIT_ENV_OK:
                raise Block("設定了 %s（會改變 git 找倉庫或設定的方式），不允許。" % name, 3)
            if name in EXEC_ENV and (v.dynamic or v.text not in EXEC_ENV_SAFE):
                raise Block("把 %s 設成別的程式（git 會把它當成指令執行），不允許。" % name, 3)
        _redirects(c, cwd, ctx, auto, shell)
        if not c.words:
            continue
        words, notes = _unwrap(c.words)
        if "env-strip" in notes:
            raise Block("用 env 清掉環境變數、或用它的 -S 另外拆指令再執行，不允許。", 3)
        for n in notes:
            if n.startswith("assign:GIT_") and n[7:] not in GIT_ENV_OK:
                raise Block("設定了 %s，不允許。" % n[7:], 3)
            if n.startswith("assignval:"):
                name, _, val = n[10:].partition("=")
                if name in EXEC_ENV and val not in EXEC_ENV_SAFE:
                    raise Block("把 %s 設成別的程式（git 會把它當成指令執行），不允許。" % name, 3)
        if not words:
            continue
        argv = [w.text for w in words]
        head = words[0]
        if shell == "powershell":
            member = _ps_member(head.text)
            if member and c.post_op == "(" and member not in PS_SAFE_MEMBERS:
                raise Block("PowerShell 對一個物件呼叫 .%s()：刪除、搬移、寫入檔案都可以這樣做，而且看不到實際是哪個檔，不允許。"
                            "請改用一般的指令並寫出路徑。" % member, 8 if auto else 3)
        # ---- 看不出執行的是什麼程式
        if head.opaque and not (shell == "powershell" and not getattr(c, "call", False)):
            raise Block("看不出這一段實際執行的是什麼程式（%s）：開頭是沒指定過的變數、指令的輸出、大括號或萬用字元。"
                        "變數請在同一段指令裡先用字面值指定（例如 PY=\"…/python.exe\"，而且不要放在 && 後面或 if 裡面）。" % head.text[:60],
                        8 if auto else 1)
        if head.opaque:                                             # PowerShell 的運算式（$x | …、$x.Method()）：不是在執行程式，但參數照樣看
            _protected_args(c, words, None, cwd, ctx, auto, shell)
            continue
        p = prog_name(head.text)
        readonly = _is_readonly(p, words, shell)
        if shell == "powershell" and re.match(r"^\[[^\]]*(io\.|file|directory|process|diagnostics|reflection|scriptblock|runspace|"
                                              r"powershell|activator|webclient)[^\]]*\]::", head.text, re.I):
            raise Block("PowerShell 直接呼叫 .NET 的檔案或程序功能（%s），看不到實際會動到什麼，不允許。請改用一般的指令。" % head.text[:60],
                        8 if auto else 3)
        if p.startswith("git-") and p not in SHELLS:
            raise Block("直接執行 %s（繞過 git 的入口），不允許。請寫成「git 子指令」。" % p, 1)
        if p in ("alias", "set-alias", "sal", "new-alias", "nal", "doskey") or (p == "hash" and "-p" in argv) or \
                (p == "enable" and "-f" in argv) or (p == "shopt" and "expand_aliases" in argv):
            raise Block("替指令另外取名字（%s），之後就看不出實際執行的是什麼，不允許。" % p, 8 if auto else 1)
        if p in DETACHERS or (p == "schtasks" and not any(a.lower().lstrip("/").startswith("query") for a in argv[1:])) or \
                (p == "net" and len(argv) > 1 and argv[1].lower() == "use"):
            raise Block("%s 會在 Claude Code 之外另外執行指令或改排程（那樣執行的指令不經過檢查），不允許。" % p, 3)
        if shell == "powershell" and p in ("foreach-object", "%", "foreach"):
            names = [w.text for w in words[1:2] if re.match(r"^[A-Za-z_]\w*$", w.text)] + \
                    [argv[i + 1] for i, a in enumerate(argv[:-1]) if a.lower() in ("-membername", "-member")]
            for name in names:                                      # … | % Delete：對每個物件呼叫那個方法；… | % Name 只是取屬性
                if name.lower() not in PS_SAFE_MEMBERS and name.lower() not in PS_SAFE_PROPERTIES:
                    raise Block("PowerShell 的 ForEach-Object %s 會對前面每個物件呼叫 .%s()，看不到實際會動到什麼，不允許。" % (name, name),
                                8 if auto else 3)
        if (p == "rg" and _mutating_flags(p, argv)) or (p == "sort" and any(a.startswith("--compress-program") for a in argv[1:])) or \
                (p in ("tar", "bsdtar") and any(a in ("-I", "-F") or a.startswith(("--to-command", "--checkpoint-action", "--use-compress-program",
                                                                                      "--rsh-command", "--info-script", "--new-volume-script"))
                                                for a in argv[1:])) or \
                (p == "rsync" and any(a == "-e" or a.startswith("--rsh") for a in argv[1:])) or (p == "scp" and "-S" in argv):
            raise Block("%s 的這個選項會讓它另外執行一個程式（看不到實際是什麼），不允許。" % p, 8 if auto else 1)
        if "xargs" in notes and not readonly:
            raise Block("xargs 後面接 %s：參數來自前面的輸出，看不到實際會動到什麼，不允許。" % p, 8 if auto else 1)
        # ---- 換目錄
        if p in ("cd", "pushd", "set-location", "sl", "chdir", "push-location"):
            tgt = [w for w in words[1:] if not w.text.startswith("-") or w.text == "-"]
            if shell == "cmd":
                tgt = [w for w in tgt if w.text.lower() != "/d"]
            if not tgt:
                cwd = ctx.home if shell == "bash" else cwd
            elif tgt[0].opaque or tgt[0].text == "-":
                cwd = None
            else:
                cwd = C.norm(tgt[0].text, cwd)
            continue
        # ---- 現在的資料夾就在保護檔裡面：只能讀
        if not readonly and cwd is not None and classify_path(".", cwd, ctx)[0] in ("state", "gitdir", "live", "user"):
            raise Block("現在的資料夾在保護檔的資料夾裡（%s），這裡只能讀。" % cwd, 3)
        # ---- 殼裡的殼
        if p in SHELLS:
            if p == "busybox":
                if len(words) > 1:
                    sub = S.Cmd()
                    sub.words, sub.redirs, sub.heredoc_bodies, sub.nested = words[1:], c.redirs, c.heredoc_bodies, True
                    _commands([sub], shell, cwd, ctx, auto, raw, depth + 1)
                continue
            script = _dash_c(words)
            if script is None:
                bodies = list(c.heredoc_bodies)
                for op, w in c.redirs:
                    if op == "<<<":
                        if w.dynamic:
                            raise Block("餵給 %s 的那段字含有變數，看不出實際會執行什麼。" % p, 8 if auto else 1)
                        bodies.append(w.text)
                for body in bodies:                                 # heredoc／here-string 餵給 shell：當成指令拆開來看
                    inner = _parse(body, "bash", auto, ctx)
                    _commands(inner.cmds, "bash", cwd, ctx, auto, raw, depth + 1)
                script_file = _script_arg(words, ("-o", "-O", "+o", "+O", "--rcfile", "--init-file"))
                if any(w.text == "-s" for w in words[1:]):
                    script_file = None                              # -s：從標準輸入讀指令，後面的字只是參數
                feeds = [w for op, w in c.redirs if op == "<"]
                for w in ([script_file] if script_file is not None else []) + feeds:
                    if w.opaque or _is_stdin_path(w.text):          # bash <(指令)、bash /dev/stdin、bash < <(指令)
                        raise Block("%s 要執行的不是一個看得到的腳本檔（是指令的輸出、變數或標準輸入），看不到實際會執行什麼，不允許。" % p,
                                    8 if auto else 1)
                has_file, from_file = script_file is not None, bool(feeds)
                if not bodies and not has_file and not from_file:
                    raise Block("把指令用管線餵給 %s，看不到實際會執行什麼，不允許。請直接寫指令，或用 %s -c '…'。" % (p, p), 8 if auto else 1)
                if auto and not bodies:
                    raise Block("自動駕駛期間不直接執行 shell 腳本檔（%s）；請用 Python 腳本。" % " ".join(argv[:3]), 8)
                _protected_args(c, words, p, cwd, ctx, auto, shell)
                continue
            if script.opaque:
                raise Block("sh -c 後面的指令含有變數，看不出實際會執行什麼。", 8 if auto else 1)
            inner = _parse(script.text, "bash", auto, ctx)
            _commands(inner.cmds, "bash", cwd, ctx, auto, raw, depth + 1)
            continue
        if p in ("eval", "source", "."):
            if p == "eval":
                if any(w.opaque for w in words[1:]):
                    raise Block("eval 的內容含有變數，看不出實際會執行什麼。", 8 if auto else 1)
                inner = _parse(" ".join(argv[1:]), "bash", auto, ctx)
                _commands(inner.cmds, "bash", cwd, ctx, auto, raw, depth + 1)
                continue
            if any(w.opaque or w.text in ("/dev/stdin", "-") for w in words[1:]) or len(words) < 2:
                raise Block("%s 後面接的不是一個看得到的檔（是變數、指令的輸出或標準輸入），看不出實際會執行什麼，不允許。" % p, 8 if auto else 1)
            if auto:
                raise Block("自動駕駛期間不用 source 執行別的腳本；請用 Python 腳本。", 8)
            _protected_args(c, words, p, cwd, ctx, auto, shell)
            continue
        if p in PS_SHELLS:
            low = [a.lower() for a in argv[1:]]
            if any(re.match(r"^-(e|ec|en|enc|enco|encod|encode|encoded|encodedc.*)$", a) for a in low):
                raise Block("PowerShell 的編碼指令（-EncodedCommand）看不到內容，不允許。", 1)
            idx = next((i for i, a in enumerate(low) if a in ("-command", "-c", "-comm", "-com")), None)
            if idx is None:
                has_file = any(a in ("-file", "-f") for a in low) or any(not a.startswith("-") for a in low)
                if not has_file:
                    raise Block("把指令用管線餵給 PowerShell，看不到實際會執行什麼，不允許。", 8 if auto else 1)
                if auto:
                    raise Block("自動駕駛期間不執行 PowerShell 腳本。", 8)
                _protected_args(c, words, p, cwd, ctx, auto, shell)
                continue
            rest = words[idx + 2:]
            if not rest or rest[0].text == "-":
                raise Block("PowerShell -Command 後面沒有看得到的指令（從標準輸入讀），不允許。", 8 if auto else 1)
            if any(w.dynamic for w in rest) and shell == "bash":
                raise Block("powershell -Command 的內容含有變數，看不出實際會執行什麼。", 8 if auto else 1)
            inner = _parse(" ".join(w.text for w in rest), "powershell", auto, ctx)
            _commands(inner.cmds, "powershell", cwd, ctx, auto, raw, depth + 1)
            continue
        if p == "cmd":
            low = [re.sub(r"^/+", "/", a.lower()) for a in argv[1:]]          # Git Bash 裡要寫成 //c
            idx = next((i for i, a in enumerate(low) if re.match(r"^(/[a-z](:[a-z]+)?)*/[ckr]$", a)), None)
            if idx is None:
                raise Block("cmd 沒有接 /c：它會從標準輸入讀指令，看不到實際會執行什麼，不允許。", 8 if auto else 1)
            rest = words[idx + 2:]
            if any(w.dynamic for w in rest):
                raise Block("cmd /c 的內容含有變數，看不出實際會執行什麼。", 8 if auto else 1)
            try:                                                    # 原本加了引號、裡面有空白的字要把引號加回去，不然會被拆散
                inner = S.parse_cmd_string(" ".join('"%s"' % w.text if (w.quoted and len(rest) > 1 and re.search(r"\s", w.text) and '"' not in w.text)
                                                    else w.text for w in rest))
            except S.ParseError as e:
                raise Block("看不懂 cmd /c 後面的指令（%s）。" % e, 8 if auto else 1)
            _commands(inner.cmds, "cmd", cwd, ctx, auto, raw, depth + 1)
            continue
        if p in ("invoke-expression", "iex", "add-type"):
            raise Block("%s 會執行一段看不到內容的程式（字串裡的指令、當場編譯的程式碼），不允許。" % head.text, 8 if auto else 1)
        if p in ("start-process", "saps", "start"):
            tgt = [w for w in words[1:] if not w.text.startswith("-") and not (shell == "cmd" and w.text.startswith("/"))]
            if any(w.dynamic for w in tgt[:1]):
                raise Block("%s 要另外開的程式是變數，看不出實際是什麼。" % p, 8 if auto else 1)
            if tgt and (prog_name(tgt[0].text) in ("git", "gh", "cmd") or prog_name(tgt[0].text) in SHELLS | PS_SHELLS):
                if p != "start":
                    raise Block("用 Start-Process 另外開 %s，看不到實際的參數，不允許。" % prog_name(tgt[0].text), 8 if auto else 1)
        # ---- 直接寫在指令裡的程式
        if p in INLINE_CODE:
            _interpreter(c, words, p, cwd, ctx, auto)
        elif p in ("awk", "gawk", "mawk", "sed", "gsed"):
            for a in argv[1:]:
                if not a.startswith("-"):
                    _inline_code(a, ctx)
                    risky = re.search(r"\bsystem\s*\(|getline|\|&|\"\s*\|\s*\"|print[^\n]*>|printf[^\n]*>|\|\s*\"", a) if p.endswith("awk") else \
                        re.search(r"(^|[;{}\s])[0-9,$~!/]*[ewWrR]\s+\S|/[a-zA-Z0-9]*[wW]\s+\S|/[a-zA-Z0-9]*e[a-zA-Z0-9]*\s*($|[;}])|"
                                  r"(^|[;{}\s])[0-9,$]*e\s*($|;)", a)
                    if risky and (auto or classify_text_has_protected(a) or re.search(r"\b(git|gh)\b", a)):
                        raise Block("%s 的程式裡有寫檔或執行指令的寫法（%s），不允許。請改用 Python 腳本。" % (p, risky.group(0).strip()[:20]),
                                    8 if auto else 3)
                    break
        # ---- 各個程式的規則
        if p == "git":
            _git(words, cwd, ctx, auto)
        elif p == "gh":
            _gh(argv, ctx, auto, cwd)
        elif p in ("curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm"):
            _curl(p, argv, ctx, auto)
        elif p == "find":
            _find_exec(words, shell, cwd, ctx, auto, raw, depth)
        elif not readonly and p not in INLINE_CODE and p not in ctx.allow["programs"]["pythonInterpreters"]:
            _scan_wrapped(words, shell, cwd, ctx, auto, raw, depth)
            if len(argv) > 1 and argv[1] in ("push", "send-pack", "http-push", "receive-pack", "update-ref") and \
                    p not in ("docker", "podman", "hg", "svn", "npm", "pnpm", "yarn", "cargo", "dotnet", "gem", "twine", "kubectl", "helm"):
                raise Block("「%s %s …」看起來像是用別的名字在執行 git，不允許。" % (p, argv[1]), 1)
        if shell == "powershell" and p in FILE_MUTATORS:
            if c.post_op == "(":
                raise Block("PowerShell 的「%s」用括號裡的運算式當參數，看不出實際會動到哪個檔。請直接寫路徑，或改用 Bash。" % p, 8 if auto else 3)
            if c.pre_op == "|" and p in ("remove-item", "ri", "rm", "del", "erase", "rd", "rmdir", "move-item", "mi", "mv", "move",
                                         "copy-item", "cpi", "cp", "copy", "rename-item", "rni", "ren", "clear-content", "clc",
                                         "clear-item", "cli", "set-itemproperty", "sp", "set-acl"):
                raise Block("PowerShell 的「%s」的對象來自前面指令的輸出，看不到實際會動到什麼，不允許。請直接寫路徑。" % p, 8 if auto else 3)
        if p in EXTRACTORS and _is_extract(p, argv) and cwd is not None and covers_protected(".", cwd, ctx):
            raise Block("%s 會把檔案解到現在的資料夾（%s），而這個資料夾包住了保護檔。請到別的資料夾解。" % (p, cwd), 3)
        run_ok = None
        if p in ctx.allow["programs"]["pythonInterpreters"]:
            script = next((w for w in words[1:] if w.text.endswith(".py")), None)
            if script is not None and not script.opaque:
                k = C.key(script.text, cwd)
                if k is not None and C.is_under(k, ctx.main_key + "/.claude/hooks"):
                    run_ok = script                                 # 用 Python「執行」生效中的 hook 腳本（例如寄信）不是寫入
        _protected_args(c, words, p, cwd, ctx, auto, shell, run_ok)
        if auto:
            _autopilot_program(p, words, c, cwd, ctx, shell)


def _is_stdin_path(text):
    """指到「標準輸入或某個開著的管線」的路徑：當成腳本檔執行＝執行別的指令的輸出。"""
    return bool(re.match(r"^(-|/dev/(stdin|fd/\d+|tty)|/proc/(self|\d+)/fd/\d+)$", text.replace("\\", "/")))


def _script_arg(words, value_opts=()):
    """shell 的第一個「不是選項」的字（＝要執行的腳本檔）。沒有回 None。value_opts：後面要接值的選項。"""
    i = 1
    while i < len(words):
        t = words[i].text
        if t in value_opts:
            i += 2
            continue
        if t == "--":
            return words[i + 1] if i + 1 < len(words) else None
        if (t.startswith("-") or t.startswith("+")) and t != "-":
            i += 1
            continue
        return words[i]
    return None


def _interpreter(c, words, p, cwd, ctx, auto):
    """python／node 這類直譯器：程式碼從哪裡來。
    寫在指令裡的（-c、heredoc）→ 只能看字面；從管線或別的指令的輸出來的 → 看不到，擋。
    自動駕駛期間更嚴：只准執行腳本檔（留得下來，審查代理與 David 之後看得到）與清單裡的模組。"""
    argv = [w.text for w in words]
    flag = INLINE_CODE[p]
    is_py = p in ctx.allow["programs"]["pythonInterpreters"] or p in ("python", "python3", "py")
    # 查版本、看說明：印完就結束，不執行任何程式碼。只認「排在腳本、-c、-m 前面」的——
    # 排在後面的（python -c "…" --version、python x.py --version）只是那段程式自己的參數，照樣要檢查。
    query = set(["-V", "-VV", "--version", "-h", "--help", "-?"])
    if p == "py":
        query |= set(["-0", "-0p", "--list", "--list-paths"])       # Windows 的 py：列出裝了哪幾個版本
    inline_msg = ("自動駕駛期間不執行直接寫在指令裡的程式碼（%s）。請把它寫成腳本檔（放這個階段的 worktree 或暫存資料夾）再執行——"
                  "腳本留得下來，審查代理與 David 之後才看得到。")
    i = 1
    while i < len(argv):
        a = argv[i]
        separate = a == flag or (p == "node" and a in ("-p", "--eval", "--print"))
        attached = is_py and a.startswith("-c") and len(a) > 2
        if separate or attached:
            if auto:
                raise Block(inline_msg % (p + " " + flag), 8)
            if separate and i + 1 >= len(argv):
                return
            if words[i + 1 if separate else i].dynamic:
                raise Block("直接寫在指令裡的程式碼含有沒指定過的變數，看不出實際內容。請寫成腳本檔再執行。", 1)
            _inline_code(argv[i + 1] if separate else a[2:], ctx)
            return
        if is_py and (a == "-m" or (a.startswith("-m") and not a.startswith("--") and len(a) > 2)):
            module = a[2:] if len(a) > 2 else (argv[i + 1] if i + 1 < len(argv) else "")
            if auto and module not in ctx.allow["programs"].get("pythonModules", []):
                raise Block("自動駕駛期間 python -m 只能執行清單裡的模組（%s）；「%s」不在清單裡（停止條件 8）。"
                            % ("、".join(ctx.allow["programs"].get("pythonModules", [])), module), 8)
            return                                                  # 執行模組（unittest、http.server…）
        if a == "-":
            break
        if a in query:
            return
        if a in ("-W", "-X", "--require", "-r", "-I", "--check-hash-based-pycs") and p != "php":
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        w = words[i]                                                # 第一個不是選項的字＝腳本檔
        if w.opaque or _is_stdin_path(a):
            raise Block("%s 要執行的不是一個看得到的腳本檔（是指令的輸出、變數或標準輸入），看不到實際內容，不允許。" % p, 8 if auto else 1)
        if auto:
            k = C.key(a, cwd)
            if k is None or not (C.is_under(k, ctx.main_key) or (ctx.scratch_key and C.is_under(k, ctx.scratch_key))):
                raise Block("自動駕駛期間只執行這個專案（含這個階段的 worktree）或暫存資料夾裡的腳本；「%s」在外面。" % a, 8)
        return
    bodies = list(c.heredoc_bodies)
    feeds = [w for op, w in c.redirs if op == "<"]
    for op, w in c.redirs:
        if op == "<<<":
            bodies.append(w.text)
    if auto and (bodies or feeds):
        raise Block(inline_msg % "heredoc 或轉向餵進去的", 8)
    for w in feeds:
        if w.opaque or _is_stdin_path(w.text):
            raise Block("餵給 %s 的不是一個看得到的檔（是指令的輸出或變數），看不到實際內容，不允許。" % p, 1)
    for body in bodies:
        _inline_code(body, ctx)
    if not bodies and not feeds:
        raise Block("%s 沒有指定腳本檔，會從管線讀程式碼來執行，看不到實際內容，不允許。請寫成腳本檔再執行。" % p, 8 if auto else 1)


def _find_exec(words, shell, cwd, ctx, auto, raw, depth):
    """find … -exec 指令 ;：後面那一段是它會去執行的指令。它會動到的檔在 find 的搜尋範圍裡——範圍本身由參數檢查把關。"""
    for part in _find_subcommands(words):
        if part:
            sub = S.Cmd()
            sub.words, sub.nested = part, True
            _commands([sub], shell, cwd, ctx, auto, raw, depth + 1)


_WRAPPED_HEADS = set(["git", "gh", "cmd", "python", "python3", "py"]) | SHELLS | PS_SHELLS | set(INLINE_CODE)
_EMBEDDED_CMD = re.compile(r"(^|[\s;&|(=\"'])((git|gh)(\.exe)?\s+[a-z-]+.*)$", re.S)


def _scan_wrapped(words, shell, cwd, ctx, auto, raw, depth):
    """不認得的程式後面如果接著 git／gh／shell，當成它會去執行後面那一段（flock、watch、start、ssh、cmd 的 for／if…）。
    整個參數就是一段指令字串的（watch "git push"、trap '…' EXIT）也拆開來看。"""
    for i in range(1, len(words)):
        w = words[i]
        t = w.text
        if w.dynamic:
            continue
        name = prog_name(t)
        if name in _WRAPPED_HEADS or (name.startswith("git-") and len(name) > 4):
            sub = S.Cmd()
            sub.words, sub.nested = words[i:], True
            _commands([sub], shell, cwd, ctx, auto, raw, depth + 1)
            return
        first = prog_name(t.split()[0]) if t.split() else ""
        m = _EMBEDDED_CMD.search(t)
        if re.search(r"\s", t) and (first in _WRAPPED_HEADS or m):
            # 整個參數是一段指令（watch "git push"），或指令藏在 --opt="git push …"、巢狀引號裡：從 git 那個字開始拆開來看
            inner = _parse(t if first in _WRAPPED_HEADS else m.group(2).rstrip("\"')"), "bash", auto, ctx)
            _commands(inner.cmds, "bash", cwd, ctx, auto, raw, depth + 1)


def _parse(text, shell, auto, ctx):
    try:
        parsed = S.parse_bash(text, {"HOME": ctx.home}) if shell == "bash" else S.parse_powershell(text)
    except S.ParseError as e:
        raise Block("看不懂包在裡面的那段指令（%s）。" % e, 8 if auto else 1)
    if parsed.complex:
        raise Block("包在裡面的那段指令用了看不完整的寫法。", 8 if auto else 1)
    return parsed


def _dash_c(words):
    """sh -c '…' 的那個字串；沒有 -c 回 None。"""
    i = 1
    while i < len(words):
        t = words[i].text
        if t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
            return words[i + 1] if i + 1 < len(words) else None
        if t in ("-o", "-O", "+o", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if not t.startswith("-") and not t.startswith("+"):
            return None
        i += 1
    return None


def _redirects(c, cwd, ctx, auto, shell="bash"):
    for op, w in c.redirs:
        if op in ("<", "<<<", "<&", "<>") or re.match(r"^[0-9]+$|^-$", w.text):
            if op in ("<", "<>") and classify_path(w.text, cwd, ctx, w.dynamic)[0] == "secret":
                raise Block("不能讀取存登入憑證或權杖的地方。", 3)
            if op != "<>":
                continue
        if ">" not in op:
            continue
        if w.text in ("/dev/null", "$null", "nul", "NUL", "/dev/stderr", "/dev/stdout"):
            continue
        if w.brace or w.glob:
            raise Block("轉向的目標有大括號或萬用字元（%s），看不出實際會寫到哪個檔。請寫完整的檔名。" % w.text, 3)
        weird = C.weird_path(w.text)
        if weird:
            raise Block("看不懂轉向目標的寫法（%s）。請用一般的完整路徑。" % weird, 3)
        kind, name = classify_path(w.text, cwd, ctx, w.dynamic)
        if kind in ("state", "gitdir", "live", "user", "transcript", "secret") or (kind == "copy" and auto):
            raise Block("不能把輸出寫進%s。" % name, 3)
        if w.dynamic:
            raise Block("轉向的目標含有沒指定過的變數（%s），看不出實際會寫到哪個檔。變數請在同一段指令裡先用字面值指定"
                        "（不要放在 && 後面或 if、for 裡面）。" % w.text[:60], 8 if auto else 3)
        if cwd is None and C.key(w.text, None) is None:
            raise Block("轉向的目標是相對路徑，但前面的 cd 讓我看不出現在在哪個資料夾（%s）。請寫完整路徑。" % w.text, 8 if auto else 3)
        if cwd is not None and classify_path(".", cwd, ctx)[0] in ("state", "gitdir", "live", "user") and C.key(w.text, None) is None:
            raise Block("現在的資料夾在保護檔的資料夾裡，不能在這裡寫檔。", 3)
        if auto:
            _write_target(w, cwd, ctx, "把輸出寫到")


def _write_target(w, cwd, ctx, verb):
    """自動駕駛期間，指令直接寫入的位置要在允許的範圍裡。"""
    if w.opaque:
        raise Block("%s的位置含有沒指定過的變數、大括號或萬用字元（%s），看不出實際位置。變數請在同一段指令裡先用字面值指定。" % (verb, w.text), 8)
    low = w.text.replace("\\", "/")
    if low.startswith("/tmp/") or low == "/dev/null":
        return
    k = C.key(w.text, cwd)
    if k is None:
        raise Block("%s的位置是相對路徑，但前面的 cd 讓我看不出現在在哪個資料夾（%s）。請寫完整路徑。" % (verb, w.text), 8)
    rk = C.real_key(w.text, cwd) or k
    roots = ctx.write_roots()
    if not any(C.is_under(k, r) for r in roots) or not any(C.is_under(rk, r) for r in roots + [C.real_key(r) or r for r in roots]):
        raise Block("自動駕駛期間指令只能寫進這個階段的 worktree、.autopilot/ 與暫存資料夾；「%s」在外面。" % w.text, 8)
    for wt in ctx.worktree_keys():
        if C.is_under(k, wt):
            _tier_check([C.rel_to(k, wt)], ctx, "改")


def _path_pieces(w, shell):
    """一個參數裡可能藏著路徑的幾種寫法：整個字、--opt=路徑、-o路徑、PowerShell 的 -Path:路徑。"""
    t = w.text
    pieces = [t]
    if "=" in t:
        pieces.append(t.split("=", 1)[1])
    if t.startswith("-") and not t.startswith("--") and len(t) > 2:
        pieces.append(t[2:])
    if t.startswith("-") and ":" in t:
        pieces.append(t.split(":", 1)[1])
    if shell == "cmd" and t.startswith("/") and ":" in t:
        pieces.append(t.split(":", 1)[1])
    return [x for x in pieces if x]


def _protected_args(c, words, p, cwd, ctx, auto, shell, run_ok=None):
    """指令的參數裡提到保護檔：只有「確定只是讀」才放行。run_ok：被 Python 執行的那一支 hook 腳本（執行不是寫入）。"""
    readonly = _is_readonly(p, words, shell)
    mutator = p in FILE_MUTATORS or (p in READONLY_PROGRAMS and not readonly)      # 後者：sed -i、find -delete、sort -o 這類
    is_git = p in ("git", "gh")
    opaque_exec = p in ctx.allow["programs"]["pythonInterpreters"] or p in INLINE_CODE     # 參數只是資料，實際做什麼在腳本裡
    start = 0 if shell in ("powershell", "cmd") else 1              # PowerShell 的運算式沒有「指令名稱」，第一個字也可能是路徑
    for w in words[start:]:
        if w is run_ok:
            continue
        if not readonly and w.brace and not opaque_exec:
            raise Block("這段指令用了大括號展開（%s），檢查程式看不出實際的參數。請把它展開寫。" % w.text[:60], 8 if auto else 3)
        if mutator and w.dynamic:
            raise Block("「%s」的參數含有沒指定過的變數或指令的輸出（%s），看不出實際會動到哪個檔。變數請在同一段指令裡先用字面值指定"
                        "（不要放在 && 後面或 if、for 裡面），或改用 Python 腳本。" % (p, w.text[:60]), 8 if auto else 3)
        for piece in _path_pieces(w, shell):
            if not readonly and not opaque_exec:
                weird = C.weird_path(piece, drive_relative=not is_git)
                if weird:
                    raise Block("看不懂這個路徑的寫法（%s：%s），看不出實際會動到哪個檔。請用一般的完整路徑。" % (piece[:60], weird), 3)
            kind, name = classify_path(piece, cwd, ctx, w.dynamic)
            if kind == "secret":
                raise Block("不能用指令讀取或改動%s。" % name, 3)
            if kind is not None and not (kind == "copy" and not auto) and not readonly and not (is_git and kind == "copy"):
                raise Block("這段指令會動到%s，而且不是單純的讀取。這是保護的一部分，不允許。" % name, 3)
            if readonly or is_git or opaque_exec or p is None or piece.startswith("-"):
                continue                                            # p 是 None：PowerShell 的運算式（比較、取屬性），不是在執行指令
            if w.glob:
                flat = piece.replace("\\", "/")
                hits = [i for i in (flat.find(ch) for ch in "*?[") if i >= 0]
                base = flat[:min(hits)] if hits else flat           # 第一個萬用字元之前、到最後一個斜線為止＝它展開的起點
                base = base[:base.rfind("/") + 1] if "/" in base else ""
                if cwd is None and C.key(base or ".", None) is None:
                    raise Block("萬用字元（%s）是相對路徑，但看不出現在在哪個資料夾。請寫完整路徑。" % piece[:60], 8 if auto else 3)
                bkind = classify_path(base or ".", cwd, ctx)[0]
                if bkind in ("state", "gitdir", "live", "user", "transcript") or covers_protected(base or ".", cwd, ctx):
                    raise Block("萬用字元（%s）的範圍包到了保護檔所在的資料夾，看不出實際會動到什麼。請寫明確的檔名。" % piece[:60], 3)
                continue
            if mutator and cwd is None and C.key(piece, None) is None:
                raise Block("「%s」的對象是相對路徑（%s），但前面的 cd 讓我看不出現在在哪個資料夾。請寫完整路徑。" % (p, piece[:60]), 8 if auto else 3)
            if covers_protected(piece, cwd, ctx) and p not in ("mkdir", "md"):
                raise Block("這段指令的對象（%s）是包住保護檔的資料夾（主目錄、它的 .claude、家目錄或更上層），而且不是單純的讀取，不允許。" % piece[:60], 3)


# ---------------------------------------------------------------- git

def _git_sub(argv):
    """回傳 (子指令, 子指令之後的參數, -C 指到的目錄清單, -c 的鍵清單, 看不懂的全域選項)"""
    i, dirs, keys, bad = 1, [], [], None
    while i < len(argv):
        a = argv[i]
        if a == "-C" and i + 1 < len(argv):
            dirs.append(argv[i + 1])
            i += 2
            continue
        if a.startswith("-C") and len(a) > 2 and not a.startswith("--"):
            dirs.append(a[2:])
            i += 1
            continue
        if a == "-c" and i + 1 < len(argv):
            keys.append(argv[i + 1])
            i += 2
            continue
        if a in ("--no-pager", "-p", "--paginate", "--no-replace-objects", "--literal-pathspecs", "--no-optional-locks",
                 "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs"):
            i += 1
            continue
        if a.startswith("-"):
            if a in ("--version", "--help", "-h", "-v", "--html-path", "--man-path", "--info-path"):
                return a.lstrip("-"), [], dirs, keys, None
            bad = a
            i += 1
            continue
        return a, argv[i + 1:], dirs, keys, bad
    return None, [], dirs, keys, bad


def _git(words, cwd, ctx, auto):
    argv = [w.text for w in words]
    sub, args, dirs, keys, bad = _git_sub(argv)
    if bad:
        raise Block("git 的全域選項 %s 會改變它找倉庫或設定的方式，不允許。" % bad, 3)
    for kv in keys:
        k = kv.split("=", 1)[0].strip().lower()
        if k not in GIT_DASH_C_OK:
            raise Block("git -c %s 會臨時改設定（可以用來繞過保護），不允許。" % kv.split("=", 1)[0], 3)
    if sub is None:
        return
    sub_word = next((w for w in words if w.text == sub), None)
    if sub_word is not None and sub_word.opaque:
        raise Block("看不出 git 後面實際是哪個子指令（%s）。" % sub, 1)
    dir_words = [w for w in words if w.opaque]
    cdir = cwd
    for d in dirs:
        if "$" in d or "`" in d or any(w.text == d or w.text == "-C" + d for w in dir_words):
            cdir = None
            break
        cdir = C.norm(d, cdir)
        if cdir is None:
            break
    if any(a == "--no-verify" for a in args):
        raise Block("--no-verify 會跳過檢查，不允許。", 1)
    if sub in GIT_NEVER:
        raise Block("git %s 不允許：它不經過推送前的檢查就能把東西送出去、會另外執行指令、或會動到登入憑證。" % sub, 1)
    if any(a.startswith(("--upload-pack", "--receive-pack", "--exec", "--extcmd", "--open-files-in-pager")) for a in args) or \
            (sub == "grep" and _short_flag(args, "O", "efmABC")) or (sub == "clone" and _short_flag(args, "u", "bojc")):
        raise Block("這個 git 選項會讓 git 另外執行一個指令（看不到實際是什麼），不允許。", 1)
    if sub == "config" and _config_is_write(args) and any(
            a in ("--global", "--system", "--file", "-f") or a.startswith("--file=") for a in args):
        raise Block("改 git 的全域設定（每個倉庫都會受影響，包括推送前的檢查），不允許。要改請 David 自己動手。", 3)
    if sub == "clone":
        if auto:
            raise Block("自動駕駛期間不另外複製倉庫（用 worktree）。", 8)
        ours = repo_slug(ctx.git.our_url_key())
        for a in args:
            if not a.startswith("-") and (repo_slug(url_key(a)) == ours or C.key(a, cdir) in (ctx.main_key, ctx.main_key + "/.git")
                                          or C.real_key(a, cdir) in (ctx.main_real, ctx.main_real + "/.git")):
                raise Block("另外複製一份這個倉庫會繞過推送前的檢查，不允許（請用 worktree）。", 1)
        return
    if sub == "push":
        return _git_push(args, words, cdir, ctx, auto)
    info = ctx.git.info(cdir) if cdir else None
    if cdir is None:
        if sub in READONLY_GIT:
            return
        raise Block("看不出這個 git 指令是在哪個資料夾執行（前面的 cd 或 -C 含有變數）。請寫完整路徑。", 8 if auto else 1)
    if info is None or not info.get("ours"):
        if auto and sub not in READONLY_GIT and sub not in ("init",):
            if not os.path.isdir(cdir):
                # 2026-10-02 實戰：「git worktree add … && cd …-merge && git merge …」寫在同一段——守門在執行前就把整段看完，
                # 那時候資料夾還不存在，看不出它是不是這個倉庫。維持從嚴（擋），但把原因與正確做法說清楚。
                raise Block("這個資料夾還不存在：%s。檢查程式在執行前就把整段指令看完，所以「建立 worktree」與「進去執行 git」要分成兩個指令："
                            "先單獨執行 git worktree add …，確認資料夾在了，再用第二個指令進去合併或 commit。" % cdir, 8)
            raise Block("自動駕駛期間 git 只用在這個專案的倉庫上（%s 不是）。" % cdir, 8)
        return
    in_main = info.get("is_main_checkout")
    on_main = info.get("branch") == MAIN
    if sub in READONLY_GIT:
        return
    if sub not in KNOWN_GIT:
        raise Block("git %s 不在認得的子指令裡（可能是自訂別名、外掛或底層指令），看不出實際會做什麼；在這個專案的倉庫裡不允許。" % sub, 1)
    if sub in HISTORY_REWRITE:
        raise Block("git %s 會改寫歷史，不允許。" % sub, 4)
    if in_main and sub not in MAIN_CHECKOUT_EXTRA:
        raise Block("主目錄（main 所在的資料夾）只能看、只能快轉跟上 origin/main；git %s 會動到它。請在 worktree 裡做。" % sub, 1)
    opts = [a for a in args if a.startswith("-")]
    pos = [a for a in args if not a.startswith("-")]
    for name in _new_ref_names(sub, args):
        if C.reserved_ref_name(name, REMOTE, literal=(sub != "fetch")):          # fetch 的目的地可能寫全名；其他的是要建立的名字本身，照字面看
            raise Block(RESERVED_NAME_MSG % name, 4)

    if sub == "merge":
        if any(o in ("--abort", "--quit", "--continue") for o in opts):
            if in_main:
                raise Block("主目錄不應該有進行中的合併。", 1)
            return
        if in_main or on_main:
            if "--ff-only" in opts and pos in ([], ["origin/" + MAIN]) and not set(opts) - set(["--ff-only", "-q", "--quiet", "--stat", "--no-stat", "-n"]):
                return
            raise Block("主目錄只能用「git merge --ff-only origin/main」快轉跟上正式版；其他的合併要在暫時的 worktree 做，而且要 David 放行。", 1)
        if info.get("head") and info["head"] in ctx.git.main_tips():
            problem = ST.credential_problem(ctx.state, ctx.cfg, "merge", ctx.now)
            if problem:
                raise Block("停止條件 1：把東西合併進 main 要 David 親手輸入「放行 %s」。（%s）" % (ctx.stage or "<階段>", problem), 1)
            cand = (ctx.state.get("credential") or {}).get("candidate")
            target = ctx.git.rev(cdir, pos[0]) if len(pos) == 1 else None
            if not cand or target != cand:
                raise Block("通行證只准合併 David 放行的那一個 commit（%s）；這個指令要合併的不是它。" % (cand or "?")[:7], 1)
            if "--no-ff" not in opts:
                raise Block("合併進 main 要用 --no-ff（留下一個合併 commit，之後才退得回去）。", 1)
        return
    if sub == "pull":
        if any(o in ("--rebase", "-r") or o.startswith("--rebase=") for o in opts):
            raise Block("git pull --rebase 會改寫歷史，不允許。", 4)
        if in_main or on_main or (info.get("head") and info["head"] in ctx.git.main_tips()):
            if "--ff-only" in opts and pos in ([], ["origin"], ["origin", MAIN]):
                return
            raise Block("在 main 上只能用「git pull --ff-only」快轉；把別的分支拉進來＝合併進 main，要 David 放行。", 1)
        return
    if sub == "commit":
        if _short_flag(args, "n", "mFCct"):
            raise Block("git commit -n 會跳過檢查，不允許。", 1)
        if on_main:
            raise Block("不能直接在 main 上 commit。請在 worktree 的分支上做。", 1)
        if "--amend" in opts and ctx.git.head_is_published(cdir):
            raise Block("這個 commit 已經推出去或打了標籤，--amend 會改寫歷史，不允許。請另外做一個新的 commit。", 4)
        if auto:
            cand = (ctx.state.get("credential") or {}).get("candidate")
            if cand and ctx.git.merge_head(cdir) == cand and not ST.credential_problem(ctx.state, ctx.cfg, "merge", ctx.now):
                return                                              # David 放行的那個合併：內容由推送前的檢查把關（要跟原封不動合併的結果一模一樣）
            files = ctx.git.staged_files(cdir, include_worktree=bool(_short_flag(args, "a", "mFCct") or "--all" in opts or pos))
            _tier_check(files, ctx, "commit ")
        return
    if sub == "reset":
        if on_main:
            raise Block("不能在 main 上 reset。", 4)
        return
    if sub == "tag":
        if any(o in ("-d", "--delete") for o in opts):
            raise Block("刪除標籤要 David 自己動手（停止條件 4：不動既有標籤）。", 4)
        if any(o in ("-f", "--force") for o in opts) or _short_flag(args, "f", "mFu"):
            raise Block("移動既有的標籤，不允許（停止條件 4）。", 4)
        return
    if sub == "branch":
        destructive = _short_flag(args, "fMCDdmc", "u") or any(o in ("--force", "--delete", "--move", "--copy") for o in opts)
        if destructive and MAIN in pos:
            raise Block("不能刪除、搬動或強制改寫 main 分支。", 4)
        return
    if sub in ("checkout", "switch"):
        if "--ignore-other-worktrees" in opts or (_short_flag(args, "BC", "") and MAIN in pos) or ("--orphan" in opts and MAIN in pos):
            raise Block("不能用這種方式重設或佔用 main 分支。", 4)
        return
    if sub == "config":
        if _config_is_write(args):
            raise Block("改 git 的設定，不允許（設定可以用來繞過保護）。要改請 David 自己動手。", 3)
        return
    if sub == "remote":
        if pos and pos[0] in ("add", "set-url", "rename", "remove", "rm", "set-head", "set-branches"):
            raise Block("改 git 的遠端設定，不允許。", 3)
        return
    if sub == "fetch":
        if "--update-head-ok" in opts or _short_flag(args, "u", "") or "--force" in opts or _short_flag(args, "f", ""):
            raise Block("git fetch 的這個選項會直接改本機分支，不允許。", 4)
        for a in pos[1:]:
            dst = a.split(":", 1)[1] if ":" in a else None
            if dst is not None and (dst == MAIN or dst.endswith("/" + MAIN)):
                # refs/heads/main＝本機的 main；refs/remotes/origin/main＝本機「記著遠端 main 在哪裡」的記號。
                # 後者被改到分支的頂端，「這個分支改了哪些檔」就會算成什麼都沒改（第三輪審查找到的）。
                raise Block("用 fetch 直接改本機的 main 分支、或改「記著遠端 main 在哪裡」的那個記號（origin/main），不允許。", 4)
        return
    if sub == "init":
        target = C.key(pos[0], cdir) if pos else C.key(cdir)
        if target is None or C.is_under(target, ctx.main_key):       # 在別的地方建新倉庫不管（自動駕駛期間另有規則）
            raise Block("這個資料夾已經是倉庫了；在這裡 git init 只會動到 git 的內部設定（樣板、hook、存放位置），不允許。", 3)
        return
    if sub == "worktree":
        if pos and pos[0] == "add" and MAIN in pos[1:] and (_short_flag(args, "Bb", "") or "--force" in opts or _short_flag(args, "f", "Bb")):
            raise Block("不能用 worktree add 重設或重複佔用 main 分支。", 4)
        if auto and pos and pos[0] == "add":
            target = next((a for a in pos[1:]), None)
            k = C.key(target, cdir) if target else None
            base = ctx.main_key + "/" + ctx.cfg["worktreeDir"].strip("/").lower()
            if k is None or not C.is_under(k, base):
                raise Block("自動駕駛期間 worktree 只開在 %s/ 底下。" % ctx.cfg["worktreeDir"], 8)
        return
    if sub == "reflog":
        if pos and pos[0] in ("expire", "delete"):
            raise Block("刪除 reflog 會讓改寫歷史查不出來，不允許。", 4)
        return
    if sub == "stash":
        if in_main and (not pos or pos[0] not in ("list", "show")):
            raise Block("主目錄不能用 stash 動工作區。", 1)
        return
    if sub == "symbolic-ref":
        if len(pos) >= 2 or any(o in ("-d", "--delete") for o in opts):
            raise Block("改 HEAD 的指向，不允許。", 4)
        return
    if sub == "gc":
        if any(o.startswith("--prune") for o in opts):
            raise Block("git gc --prune 會清掉歷史物件，不允許。", 4)
        return
    return


RESERVED_NAME_MSG = ("分支或標籤的名字不可以以「origin/」「refs/」「remotes/」開頭（%s）：git 會把它跟「遠端的正式版在哪裡」那一類記號搞混——"
                     "同名的標籤或本機分支排在前面，之後寫 origin/main 的地方拿到的就是它。請換一個名字。")
# 各個會建立分支／標籤的子指令裡，「後面要接一個值」的選項（收名字的時候要跳過它們的值）
_REF_VALUE_OPTS = {"tag": ("-m", "-F", "-u", "--message", "--file", "--local-user", "--cleanup", "--sort", "--format", "--contains",
                           "--no-contains", "--points-at", "--merged", "--no-merged"),
                   "branch": ("-u", "--set-upstream-to", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format")}
_REF_QUERY_OPTS = {"tag": ("-l", "--list", "-d", "--delete", "-v", "--verify", "--contains", "--no-contains", "--points-at", "--merged", "--no-merged"),
                   "branch": ("-l", "--list", "-a", "--all", "-r", "--remotes", "-d", "-D", "--delete", "-u", "--set-upstream-to", "--unset-upstream",
                              "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--show-current", "--edit-description")}


def _option_values(args, shorts, longs):
    """選項後面接的值：短選項可以黏在一串裡（-qb 名字、-b名字），長選項可以用空白或等號（--orphan 名字、--orphan=名字）。"""
    out = []
    for i, a in enumerate(args):
        if a == "--":
            break
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a.startswith("--"):
            for f in longs:
                if a == f and nxt is not None:
                    out.append(nxt)
                elif a.startswith(f + "="):
                    out.append(a[len(f) + 1:])
        elif a.startswith("-") and len(a) > 1:
            for j, ch in enumerate(a[1:], 1):
                if ch in shorts:
                    rest = a[j + 1:]
                    if rest:
                        out.append(rest)
                    elif nxt is not None:
                        out.append(nxt)
                    break
    return out


def _plain_positionals(args, value_opts):
    """不是選項、也不是某個選項的值的那些參數（照順序）。"""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            skip = a in value_opts
            continue
        out.append(a)
    return out


def _new_ref_names(sub, args):
    """這個 git 指令會建立（或改名成）哪些分支、標籤的名字。只用來擋「會跟遠端的記號撞名」的名字，所以寧可多認、不漏認；
    起點（例如 git branch 新名字 origin/main 的第二個參數）不算名字。推送的目的地在 _git_push 另外看。"""
    if sub in ("tag", "branch"):
        if any(a in _REF_QUERY_OPTS[sub] or a.split("=", 1)[0] in _REF_QUERY_OPTS[sub] for a in args) or \
                (sub == "branch" and _short_flag(args, "dDlar", "u")) or (sub == "tag" and _short_flag(args, "ldvn", "mFu")):
            return []
        pos = _plain_positionals(args, _REF_VALUE_OPTS[sub])
        if not pos:
            return []
        if sub == "branch" and (_short_flag(args, "mMcC", "u") or any(a in ("--move", "--copy") for a in args)):
            return [pos[-1]]                                         # 改名、複製：最後一個是新名字
        return [pos[0]]
    if sub in ("checkout", "switch"):
        return _option_values(args, "bBcC" if sub == "switch" else "bB", ("--orphan", "--create", "--force-create"))
    if sub == "worktree":
        return _option_values(args, "bB", ())
    if sub == "stash":
        pos = [a for a in args if not a.startswith("-")]
        return pos[1:2] if pos[:1] == ["branch"] else []
    if sub == "fetch":                                               # git fetch <遠端> 來源:目的地——目的地是本機的分支或標籤時才算（refs/remotes/ 那一類另有規則）
        pos = [a for a in args if not a.startswith("-")]
        out = []
        for a in pos[1:]:
            dst = a.split(":", 1)[1] if ":" in a else ""
            if dst and not (dst.startswith("refs/") and not dst.startswith(("refs/heads/", "refs/tags/"))):
                out.append(dst)
        return out
    return []


def _short_flag(args, letters, takes_value):
    """短選項（可以黏在一起，例如 -am）裡有沒有 letters 中的任何一個。takes_value：後面要接值的選項字母，遇到就停。"""
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--":
            break
        if not a.startswith("-") or a.startswith("--") or len(a) < 2:
            continue
        for i, ch in enumerate(a[1:], 1):
            if ch in letters:
                return True
            if ch in takes_value:
                skip = i == len(a) - 1                              # -m "訊息"：下一個字是訊息本身，不是選項
                break
    return False


def _config_is_write(args):
    opts = [a for a in args if a.startswith("-")]
    pos = [a for a in args if not a.startswith("-")]
    if pos and pos[0] in ("set", "unset", "rename-section", "remove-section", "edit"):
        return True
    if pos and pos[0] in ("get", "list", "get-regexp", "get-all"):
        return False
    if any(o in ("--unset", "--unset-all", "--add", "--replace-all", "--rename-section", "--remove-section", "-e", "--edit") for o in opts):
        return True
    if any(o in ("--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "--get-color", "--get-colorbool") for o in opts):
        return False
    return len(pos) >= 2


def _git_push(args, words, cdir, ctx, auto):
    flag_opts = [a for a in args if a.startswith("-")]
    if any(a in ("--force", "--mirror", "--all", "--prune", "--no-verify", "--force-if-includes", "--branches") or
           a.startswith("--force-with-lease") or a.startswith("--receive-pack") or a.startswith("--exec") or a.startswith("--repo")
           for a in flag_opts) or _short_flag(args, "f", "o"):
        raise Block("這個 git push 的選項（強推、推全部分支、跳過檢查這一類）不允許。", 1)
    deleting = any(a in ("--delete",) for a in flag_opts) or _short_flag(args, "d", "o")
    pos = []
    it = iter(args)
    for a in it:
        if a in ("-o", "--push-option"):
            next(it, None)
            continue
        if a.startswith("-"):
            continue
        pos.append(a)
    dyn = set(w.text for w in words if w.opaque)
    remote = pos[0] if pos else None
    refspecs = pos[1:]
    if cdir is None:
        raise Block("看不出這個 git push 是在哪個資料夾執行（前面的 cd 或 -C 含有變數）。請寫完整路徑。", 1)
    info = ctx.git.info(cdir)
    ours_dir = bool(info and info.get("ours"))
    ours = repo_slug(ctx.git.our_url_key())
    if remote is None:                                              # 沒寫遠端＝這個資料夾預設的 origin（另外複製的一份也指到同一個倉庫）
        remote_ours = ours_dir or repo_slug(ctx.git.remote_url_key(cdir, ctx.cfg["remote"])) == ours
    elif remote in dyn:
        raise Block("git push 的遠端含有變數、大括號或萬用字元（%s），看不出會推到哪裡。" % remote, 1)
    elif remote == ".":                                             # 推給自己＝直接改本機的分支
        remote_ours = ours_dir
    elif re.search(r"[:/\\]", remote):                              # 直接寫網址或路徑
        remote_ours = repo_slug(url_key(remote)) == ours or C.key(remote, cdir) in (ctx.main_key, ctx.main_key + "/.git") \
            or C.real_key(remote, cdir) in (ctx.main_real, ctx.main_real + "/.git")
    else:
        url = ctx.git.remote_url_key(cdir, remote)
        remote_ours = (repo_slug(url) == ours) if url else (ours_dir and remote == ctx.cfg["remote"])
    if not remote_ours:
        if auto:
            raise Block("自動駕駛期間只推到這個專案的 origin。", 8)
        return
    main_push = False
    for r in refspecs:
        if r in dyn or "*" in r:
            raise Block("git push 的目的地含有變數或萬用字元（%s），看不出會推到哪裡。" % r, 1)
        if r.startswith("+"):
            raise Block("refspec 前面的 + 是強推，不允許。", 1)
        src, sep, dst = r.partition(":")
        if deleting:
            src, dst = "", r
        elif not sep:
            dst = src
        if re.match(r"^(refs/)?remotes/", dst):
            raise Block("把東西推進 refs/remotes/（本機用來記遠端位置的記號）不是正常的推送，不允許。", 4)
        if src != "" and not deleting and C.reserved_ref_name(dst, REMOTE):      # 推上去會變成一個跟遠端的記號撞名的分支或標籤
            raise Block(RESERVED_NAME_MSG % dst, 4)
        short = re.sub(r"^refs/heads/", "", dst)
        if src == "":                                               # 刪除遠端的東西
            if dst.startswith("refs/tags/") or ctx.git.tag_exists(short) or short == MAIN:
                raise Block("刪除遠端的標籤或 main，不允許（停止條件 4）。", 4)
            continue
        if dst.startswith("refs/tags/"):
            continue
        if short == MAIN or (short in ("HEAD", "@") and info and info.get("branch") == MAIN):
            main_push = True
    if not refspecs and not deleting:
        if info is None:
            raise Block("看不出這個 git push 會推哪個分支。", 1)
        up = ctx.git.upstream(cdir) if info.get("branch") else None
        if info.get("branch") == MAIN or (up and up.split("/", 1)[-1] == MAIN):
            main_push = True
    if "--tags" in flag_opts and not refspecs and info and info.get("branch") == MAIN:
        main_push = False
    if main_push:
        if not ours_dir:
            raise Block("從別的資料夾（另一份複製的倉庫）推到這個專案的 main，不允許。", 1)
        merged = bool((ctx.state.get("credential") or {}).get("merge"))
        problem = ST.credential_problem(ctx.state, ctx.cfg, "docs" if merged else "merge", ctx.now)
        if problem:
            raise Block("停止條件 1：推上 main 要 David 親手輸入「放行 %s」。（%s）" % (ctx.stage or "<階段>", problem), 1)
        if len(refspecs) != 1:
            raise Block("有通行證時，推 main 的指令只能有一個目的地（例如 git push origin HEAD:main）。", 1)
        return
    if auto and ours_dir and not deleting:
        changed = ctx.git.changed_vs_main(cdir)
        if changed is None:
            raise Block("查不出這個分支跟 main 差了哪些檔，先不推。", 8)
        _tier_check(not_preexisting(changed, ctx.state, ctx.git.head_blobs(cdir)), ctx, "推上去 ")
        pending = [p for p, v in ((ctx.state.get("tier2") or {}).get("touched") or {}).items() if not v.get("ack")]
        t2 = [f for f in changed if C.glob_match(f, ctx.cfg["tier2"]["paths"])]
        if pending or t2:
            acked = set(p for p, v in ((ctx.state.get("tier2") or {}).get("touched") or {}).items() if v.get("ack"))
            need = sorted(set(pending) | (set(t2) - acked))
            if need:
                raise Block("這個階段動到了要先給 David 看 diff 的檔（%s）。先寄「請看 diff」的停止報告，等他輸入「繼續 %s」之後才能推。"
                            % ("、".join(need), ctx.stage), 3)


# ---------------------------------------------------------------- gh 與 curl

_GH_VALUE_OPTS = set("""-R --repo -f --field -F --raw-field -X --method -H --header -q --jq -t --template --json -L --limit -b --body
--ref -r -w --workflow -e --event -s --status -u --user --created -c --commit --input -p --preview --cache --hostname --job -j --attempt
-a -i --interval --branch -B --title --label -l --assignee -m --milestone --body-file""".split())
# gh api：只有這些選項算「看得懂、而且不會寫」。其他的不是方法、不是欄位、就是認不得——認不得的一律擋。
_GH_API_SAFE_FLAGS = set(["--paginate", "--slurp", "-i", "--include", "--silent", "--verbose"])
_GH_API_SAFE_VALUE_OPTS = set(["-H", "--header", "-q", "--jq", "-t", "--template", "--cache", "--hostname", "-p", "--preview"])


def _gh_positional(argv):
    """gh 的子指令與位置參數（跳過選項與它們的值）。"""
    pos, it = [], iter(argv[1:])
    for a in it:
        if a in _GH_VALUE_OPTS:
            next(it, None)
            continue
        if a.startswith("-"):
            continue
        pos.append(a)
    return pos


def _gh_rerun(argv, pos, ctx, auto, cwd):
    """重跑驗收（2026-10-06 裁決）。兩種模式都可以用，但有範圍：只准重跑「驗收機的流程檔、head_sha 是現在分支最頂端的 commit、
    由 push 觸發」的那一次執行；同一個 commit 最多重跑 maxReruns 次。對別的流程、別的 commit、超過次數，一律擋；查不到也擋。
    重跑不是放寬：算數的是最後那一次嘗試的結果，關卡認的還是同一次執行。每一次放行都記下來（報告要寫哪一次、為什麼）。"""
    v = ctx.cfg.get("verify") or {}
    code = 3 if auto else 1
    if not v:
        raise Block("設定裡沒有驗收機（verify），不能重跑。", code)
    limit = int(v.get("maxReruns", 2))
    ids = pos[2:]
    flags = [a for a in argv[1:] if a.startswith("-")]
    if len(ids) != 1 or not ids[0].isdigit() or any(f != "--failed" for f in flags):
        raise Block("重跑驗收只准這一種寫法：gh run rerun <執行編號>（可以加 --failed）。不能省略編號、不能指定別的倉庫或單一工作、不能帶別的選項。", code)
    info = ctx.git.info(cwd) if cwd else None
    if not info or not info.get("ours") or not info.get("head"):
        raise Block("看不出現在在這個倉庫的哪個分支、哪個 commit，不能重跑驗收。請在這個階段的 worktree 裡下指令。", code)
    head = str(info["head"]).lower()
    runs = ctx.git.gh_verify_runs(ctx.cfg, head)
    if runs is None:
        raise Block("查不到驗收機對現在這個 commit（%s）的執行紀錄；查不到就不重跑。" % head[:7], code)
    want = ".github/workflows/" + v["workflow"]
    run = next((r for r in runs if str(r.get("id")) == ids[0]), None)
    if run is None:
        raise Block("執行 %s 不在驗收機（%s）對現在這個 commit（%s）的紀錄裡。只能重跑現在分支最頂端那個 commit 的驗收。" % (ids[0], v["workflow"], head[:7]), code)
    if run.get("path") != want:
        raise Block("執行 %s 不是驗收機（%s）的執行，是別的流程（%s）。只能重跑驗收機。" % (ids[0], v["workflow"], run.get("path") or "?"), code)
    if str(run.get("head_sha") or "").lower() != head:
        raise Block("執行 %s 是別的 commit（%s）的驗收，不是現在分支最頂端的 %s。只能重跑現在這個 commit 的。"
                    % (ids[0], str(run.get("head_sha") or "?")[:7], head[:7]), code)
    if run.get("event") != "push":
        raise Block("執行 %s 不是由推送觸發的那一次（是 %s）。關卡只認推送觸發的那一次，重跑別的沒有用。" % (ids[0], run.get("event") or "?"), code)
    mine = [r for r in runs if r.get("path") == want and str(r.get("head_sha") or "").lower() == head and r.get("event") == "push"]
    used = sum(max(0, int(r.get("run_attempt") or 1) - 1) for r in mine)
    if used >= limit:
        why = "這個 commit（%s）的驗收已經重跑 %d 次，上限是 %d 次" % (head[:7], used, limit)
        if auto:
            ctx.effects.append(("stop_required", 5, why))
        raise Block(why + "。還不是每一段都綠、封鎖層級 3／3，就停下來寄【%s】的信，由 David 決定；不要再重跑。" % ctx.cfg["stopLevels"]["decide"], 5 if auto else 1)
    ctx.effects.append(("verify_rerun", {"sha": head, "run": ids[0], "nth": used + 1, "limit": limit}))


def _gh(argv, ctx, auto, cwd=None):
    pos = _gh_positional(argv)
    sub = pos[0] if pos else ""
    sub2 = pos[1] if len(pos) > 1 else ""
    if sub == "auth":
        if sub2 in ("token", "refresh", "login", "logout", "setup-git", "switch") or "-t" in argv or "--show-token" in argv:
            raise Block("讀或改 GitHub 的登入憑證，不允許。", 3)
    if sub == "pr" and sub2 == "merge":
        raise Block("停止條件 1：用 gh 合併 PR＝合併進 main，要 David 放行（而且合併一律在本機做，不用 GitHub 的合併按鈕）。", 1)
    if sub == "pr" and sub2 and sub2 not in GH_PR_READS:
        raise Block("gh pr %s 不是確定只讀的子指令（只讀的只有 %s）。會寫 PR 的事（開、改、留言、審查、關閉、revert、更新分支…）"
                    "只准經過主目錄那支 iw_notify.py pr（開 PR、改標題與內文、留言剛好是 @codex review），不准直接下；"
                    "Codex 的審查與留言也不准 resolve、刪除、隱藏或駁回（P2）。" % (sub2, "、".join(sorted(GH_PR_READS))), 1)
    if sub == "issue" and sub2 and sub2 not in GH_ISSUE_READS:
        raise Block("gh issue %s 不是確定只讀的子指令（只讀的只有 %s）；公開倉庫的 issue 不由 Claude 寫（通知信走 iw_notify.py）。"
                    % (sub2, "、".join(sorted(GH_ISSUE_READS))), 1)
    if sub == "run" and sub2 and sub2 not in GH_RUN_READS and sub2 != "rerun":
        raise Block("gh run %s 會動到 GitHub 上的執行紀錄（刪掉或取消一次驗收，比較舊的結果就可能重新算數），兩種模式都不允許。"
                    "能用的只有 %s，以及有範圍的 rerun（只准重跑現在這個 commit 的驗收）。" % (sub2, "、".join(sorted(GH_RUN_READS))), 1)
    if sub == "workflow" and sub2 and sub2 not in GH_WORKFLOW_READS and sub2 not in ("run", "enable", "disable"):
        raise Block("gh workflow %s 不是確定只讀的子指令（只讀的只有 %s；run 與 enable／disable 另有規則）。" % (sub2, "、".join(sorted(GH_WORKFLOW_READS))), 1)
    if sub == "api":
        # gh 接受把短旗標黏在一起寫（-fbody=x、-XPOST、-iXPOST），帶了欄位又沒寫方法就自動變成 POST。逐種寫法去認是認不完的
        # （2026-10-05 Codex 的審查意見：-fbody=x 就這樣漏掉了），所以反過來：每一個選項都要是認得的寫法；認不得的一律擋。
        method, has_fields, unknown, i = None, False, None, 1
        while i < len(argv):
            a = argv[i]
            i += 1
            if not a.startswith("-") or a == "-":
                continue
            if a in _GH_API_SAFE_FLAGS:
                continue
            if a in _GH_API_SAFE_VALUE_OPTS:
                i += 1
                continue
            if any(a.startswith(o + "=") for o in _GH_API_SAFE_VALUE_OPTS if o.startswith("--")):
                continue
            if a in ("-X", "--method"):
                method = argv[i].upper() if i < len(argv) else "?"
                i += 1
                continue
            if a.startswith("--method="):
                method = a.split("=", 1)[1].upper()
                continue
            if a.startswith("-X") and not a.startswith("--") and a[2:].isalpha():
                method = a[2:].upper()
                continue
            if a in ("-f", "-F", "--field", "--raw-field", "--input"):
                has_fields = True
                i += 1
                continue
            if a.startswith(("--field=", "--raw-field=", "--input=")) or (a[:2] in ("-f", "-F") and len(a) > 2):
                has_fields = True
                continue
            unknown = unknown or a
        writing = (method not in (None, "GET")) or (has_fields and method != "GET")
        path = " ".join(pos[1:])
        if writing:                                                 # 第一版只擋幾種路徑，建立發行版、觸發流程就漏掉了：寫入一律擋
            raise Block("用 GitHub API 做寫入（改分支、合併、改檔、建立發行版、觸發流程…：%s），不允許。要改東西請用對應的 gh 指令；合併要經過放行。"
                        % (path[:60] or "?"), 1)
        if unknown:
            raise Block("gh api 帶了看不出是不是唯讀的選項（%s）。gh 接受把短旗標黏在一起的寫法，逐種去認是認不完的，所以認不得的一律擋。"
                        "唯讀請只用這幾種：--jq／-q、-t、--paginate、--slurp、-H、-i、-X GET。" % unknown[:30], 1)
    if sub == "repo" and sub2 in ("delete", "rename", "edit", "archive", "unarchive", "sync", "create", "fork", "deploy-key"):
        raise Block("gh repo %s 會改倉庫本身，不允許。" % sub2, 3)
    if sub in ("secret", "variable") and sub2 in ("set", "delete", "remove"):
        raise Block("改 GitHub 的密鑰或變數，不允許。", 3)
    if sub in ("ssh-key", "gpg-key", "ruleset") and sub2 in ("add", "delete", "create", "edit"):
        raise Block("改 GitHub 帳號或倉庫的安全設定，不允許。", 3)
    if (sub == "alias" and sub2 in ("set", "import", "delete")) or (sub == "extension" and sub2 not in ("list", "search", "browse", "")):
        raise Block("替 gh 另外取指令名稱或裝外掛，之後就看不出實際執行的是什麼，不允許。", 1)
    if sub == "workflow" and sub2 in ("enable", "disable"):
        raise Block("啟用或停用 GitHub 上的流程＝改排程，不允許（停止條件 3）。要改請 David 自己動手。", 3)
    if pos[:2] == ["workflow", "run"]:
        ref = None
        for i, a in enumerate(argv):
            if a in ("--ref", "-r") and i + 1 < len(argv):
                ref = argv[i + 1]
            elif a.startswith("--ref="):
                ref = a.split("=", 1)[1]
        if ref is not None and ref not in (MAIN, "refs/heads/" + MAIN):
            raise Block("從還沒放行的分支（%s）執行 workflow＝讓沒審過的流程拿著倉庫的寫入權限跑，不允許。"
                        "要這樣做請 David 自己在 GitHub 網頁上按「Run workflow」。" % ref, 3 if auto else 1)
    if pos[:2] == ["run", "download"]:
        dirs = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-D", "--dir")] + [a.split("=", 1)[1] for a in argv if a.startswith("--dir=")]
        for d in dirs or ["."]:
            if cwd is None or covers_protected(d, cwd, ctx) or classify_path(d, cwd, ctx)[0] not in (None, "copy"):
                raise Block("gh run download 會把下載的檔案放到「%s」，那個位置包住了保護檔（或看不出在哪裡）。請用 -D 指定別的資料夾。" % d, 3)
    if pos[:2] == ["run", "rerun"]:
        _gh_rerun(argv, pos, ctx, auto, cwd)
    if not auto:
        return
    allowed = ctx.allow["programs"]["ghAllowed"]
    ok = any(pos[:len(pat)] == pat for pat in allowed) or pos[:2] == ["workflow", "run"]      # workflow run 下面另外看是不是通知那一個
    if not ok:
        raise Block("自動駕駛期間 gh 只能：觸發通知（workflow run notify.yml）、唯讀地看執行紀錄與 PR、看登入狀態。「gh %s」不在清單裡（停止條件 8）。"
                    % " ".join(pos[:3]), 8)
    if pos[:2] == ["run", "download"]:
        dirs = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-D", "--dir")] + [a.split("=", 1)[1] for a in argv if a.startswith("--dir=")]
        if not dirs or any(not C.is_under(C.key(d, cwd), _runs_key(ctx)) for d in dirs):
            raise Block("自動駕駛期間 gh run download 只能下載到這個階段的報告資料夾（%s/%s/），要用 -D 指定。" % (ctx.cfg["runsDir"], ctx.stage), 8)
    repo = None
    for i, a in enumerate(argv):
        if a in ("-R", "--repo") and i + 1 < len(argv):
            repo = argv[i + 1]
        elif a.startswith("--repo="):
            repo = a.split("=", 1)[1]
    if pos[:2] == ["workflow", "run"]:
        want = ctx.cfg["notify"]["repo"].lower()
        if (repo or "").lower() != want or pos[2:3] != [ctx.cfg["notify"]["workflow"]] or any(a == "--ref" or a.startswith("--ref=") or a == "-r" for a in argv):
            raise Block("自動駕駛期間只能觸發 %s 的 %s（寄通知用）；其他流程不能觸發（停止條件 3）。" % (want, ctx.cfg["notify"]["workflow"]), 3)
    elif repo is not None and repo.lower() not in (ctx.cfg["notify"]["repo"].lower(), (ctx.git.our_url_key() or "").split("github.com/")[-1]):
        raise Block("gh 的 -R 指到別的倉庫（%s）。" % repo, 8)


def _curl(p, argv, ctx, auto):
    if not auto:
        return
    hosts = ctx.allow["programs"]["curlHosts"]
    urls = [a for a in argv[1:] if re.match(r"^[a-z]+://", a, re.I) or re.match(r"^(localhost|127\.0\.0\.1)[:/]", a)]
    if not urls:
        raise Block("自動駕駛期間 %s 要寫明網址（而且只能連本機與本站）。" % p, 8)
    for u in urls:
        host = re.sub(r"^[a-z]+://", "", u.lower()).split("/")[0].split("@")[-1].split(":")[0]
        if host not in hosts:
            raise Block("停止條件 6／8：自動駕駛期間 %s 只能連 %s；「%s」不在清單裡。對外抓資料要走專案自己的抓取程式（有白名單與計數）。"
                        % (p, "、".join(hosts), host), 8)


# ---------------------------------------------------------------- 自動駕駛：程式清單與寫入位置

def _autopilot_program(p, words, c, cwd, ctx, shell):
    allow = ctx.allow["programs"]
    if p not in allow["allow"]:
        raise Block("程式「%s」不在自動駕駛的清單裡（停止條件 8）。需要它的話：寫停止報告、寄信，不要換別的方法繞過。" % p, 8)
    argv = [w.text for w in words]
    if _mutating_flags(p, argv) and p == "find":
        raise Block("自動駕駛期間 find 不用 -delete／-exec（看不到實際會動到什麼）。請用 Python 腳本。", 8)
    targets = []
    args = [w for w in words[1:] if not w.text.startswith("-")]
    if p in ("rm", "rmdir", "touch", "mkdir", "tee", "mv"):
        targets = args
    elif p == "cp":
        targets = args[-1:]
    elif p == "sed" and _mutating_flags(p, argv):
        targets = args[1:] if len(args) > 1 else args
    for w in targets:
        _write_target(w, cwd, ctx, "「%s」要動" % p)
    if p == "git":
        sub = _git_sub(argv)[0]
        if sub in ("init",):
            raise Block("自動駕駛期間不另外建倉庫。", 8)
