# -*- coding: utf-8 -*-
"""
iw_receive.py — 接收模式（P3）：雲端（Cowork）做好的包怎麼驗、怎麼套上，以及給 Cowork 讀的狀態檔（status.json）。

「接收：<階段>」啟動之後，筆電上的 Claude 不改程式；程式由雲端寫好，打成一個包放進
.autopilot/runs/<階段>/inbox/<NN>/（NN＝seq，兩位數）：
    bundle.git     git bundle create 做的，內容是 <base>..<head> 的 commit
    manifest.json  {"schema":1,"stage","seq","base","head","bundle_sha256","files":[…],"commits":[{"sha","subject"}],
                    "author":{"name","email"},"created_at","note"}
    replies.md     （選配）Codex 意見的回覆表，格式同 03_第三方審查.md

這裡的每一條都是程式判定（規格 P3 第 2.1 節、Cowork 裁決 1～5）：
    check_package  驗包八條；任何一條不過＝整包拒收（寫 rejected.md、寫 status.json、寄小事的短信、不暫停）
    apply_package  只套「剛驗過、雜湊沒變」的那一包：先把 bundle 複製到狀態資料夾、對複製的那一份再算一次雜湊、從它 fetch，
                   再 merge --ff-only；Claude 自己不碰 bundle（守門擋 Claude 從檔案 fetch 與 merge）
    mark_tested    本機全套跑完之後：tests.txt 第一行是這個 commit、全綠沒有 skipped，而且主目錄生效中的保護檔與推送前的檢查沒被動過
                   （雲端寫的測試在筆電上執行，跑完要確認它沒動到保護）；過了才記 tested_head，守門才准推
    wait_inbox     等下一個 seq 的包放好（兩個檔都在、大小 stableSeconds 秒不變）
    write_status   .autopilot/runs/<階段>/status.json：只從狀態檔與本機已存的結果組出來、不連網；先寫暫存檔再換名

status.json、usage.json、inbox/ 只准程式寫：守門擋 Claude 直接寫（裁決 10）。
"""
import hashlib
import io
import json
import os
import re
import shutil
import time

import iw_common as C
import iw_state as ST

MANIFEST = "manifest.json"
BUNDLE = "bundle.git"
REJECTED = "rejected.md"
REPLIES = "replies.md"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class Reject(Exception):
    """整包拒收。reason 是給 David 與 Cowork 看的白話（不帶命中的字本身）；rule 是第幾條。"""

    def __init__(self, rule, reason):
        Exception.__init__(self, reason)
        self.rule = rule
        self.reason = reason


# ---------------------------------------------------------------- 位置

def rcfg(cfg):
    return cfg.get("receive") or {}


def runs_dir(main_root, cfg, stage):
    return os.path.join(main_root, *(cfg["runsDir"].split("/") + [stage]))


def inbox_dir(main_root, cfg, stage):
    return os.path.join(runs_dir(main_root, cfg, stage), rcfg(cfg).get("inboxDir") or "inbox")


def seq_name(seq):
    return "%02d" % int(seq)


def branch_name(cfg, stage):
    return cfg["branchPrefix"] + stage


def stage_worktree(main_root, cfg, stage):
    return os.path.join(main_root, *(cfg["worktreeDir"].split("/") + [cfg["tagPrefix"] + stage]))


def private_copy(sd, stage, seq):
    """驗過的 bundle 複製一份到狀態資料夾（Claude 寫不到）：套包時只從這一份 fetch，驗完之後收件匣裡的檔被換掉也沒用。"""
    d = os.path.join(sd, "inbox")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s-%s.bundle" % (stage, seq_name(seq)))


def sha256_file(path):
    return ST.file_sha256(path)


# ---------------------------------------------------------------- 狀態

def receive_state(st):
    r = (st or {}).get("receive")
    return r if isinstance(r, dict) else {}


def _set_receive(sd, stage, fn):
    """改 state["receive"]（只在紀錄的階段就是這一個時改）。回傳改完的狀態。"""
    def outer(s):
        if s.get("stage") != stage:
            return
        r = s.setdefault("receive", {})
        fn(s, r)
    return ST.update(sd, outer)


def remember(sd, stage, key, data):
    """把最後一次驗收機、Codex 的結果摘要存進狀態檔（status.json 讀這一份，不連網）。"""
    def fn(s):
        if s.get("stage") == stage:
            s.setdefault("status_cache", {})[key] = data
    try:
        ST.update(sd, fn)
    except Exception:                                              # noqa: B902
        pass


# ---------------------------------------------------------------- 驗包

def read_bundle_header(path):
    """git bundle 的開頭（v2／v3）：回傳 (前提 commit 清單, [(commit, ref)])。看不懂就拒收。"""
    try:
        with io.open(path, "rb") as fh:
            head = fh.read(1 << 20)
    except Exception as e:                                         # noqa: B902
        raise Reject(2, "讀不到 bundle.git（%r）" % (e,))
    end = head.find(b"\n\n")
    if end < 0:
        raise Reject(2, "bundle.git 不是 git bundle（看不到開頭的清單）")
    lines = head[:end].decode("utf-8", "replace").split("\n")
    if lines[0] not in ("# v2 git bundle", "# v3 git bundle"):
        raise Reject(2, "bundle.git 不是認得的 git bundle 版本")
    prereq, refs = [], []
    for ln in lines[1:]:
        if ln.startswith("@"):
            if ln != "@object-format=sha1":                         # v3 的能力宣告：只認 sha1；部分內容（@filter）一律不收
                raise Reject(2, "bundle.git 帶了不認得的設定（%s）" % ln[:40])
        elif ln.startswith("-"):
            prereq.append(ln[1:].split(" ", 1)[0].lower())
        else:
            sha, _, ref = ln.partition(" ")
            refs.append((sha.lower(), ref.strip()))
    return prereq, refs


def _subject_patterns(cfg):
    return [re.compile(p) for p in cfg["cloudJob"]["subjectPatterns"] + cfg["goldJob"]["subjectPatterns"]]


def data_commits_between(main_root, cfg, base, ref):
    """base 到正式版之間的每一筆是不是排程的資料 commit（跟 iw_notify.tripwire 同一套判斷）。都是回 None；不是回那一筆的說明。"""
    rc, out = C.git(["-c", "core.quotepath=false", "log", "--reverse", "--name-only", "--format=%x01%H%x02%P%x02%an%x02%s", "%s..%s" % (base, ref)],
                    main_root, timeout=60)
    if rc != 0:
        return "查不出起點到正式版之間有哪些 commit"
    cloud, gold = cfg["cloudJob"], cfg["goldJob"]
    for chunk in out.split("\x01"):
        if not chunk.strip():
            continue
        head, _, rest = chunk.partition("\n")
        parts = head.split("\x02")
        if len(parts) < 4:
            return "看不懂正式版的紀錄"
        sha, parents, author, subject = parts[0], parts[1].split(), parts[2], parts[3]
        files = [f for f in rest.split("\n") if f.strip()]
        if len(parents) >= 2:
            return "起點之後正式版多了一個合併（%s），不只是資料更新" % sha[:7]
        data_only = bool(files) and all(f.startswith(cloud["pathPrefix"]) for f in files)
        if data_only and author == cloud["author"] and any(re.match(p, subject) for p in cloud["subjectPatterns"]):
            continue
        if files and set(files) <= set(gold["files"]) and any(re.match(p, subject) for p in gold["subjectPatterns"]):
            continue
        return "起點之後正式版多了一筆不是排程資料更新的 commit（%s）" % sha[:7]
    return None


def _parse_commit(raw):
    """git cat-file commit 的內容 → (parents, author (name, email), committer (name, email), message)。"""
    head, _, msg = raw.partition("\n\n")
    parents, author, committer = [], None, None
    for ln in head.split("\n"):
        if ln.startswith("parent "):
            parents.append(ln[7:].strip().lower())
        elif ln.startswith("author ") or ln.startswith("committer "):
            m = re.match(r"^(author|committer) (.*) <([^<>]*)> \d+ [+-]\d{4}$", ln)
            who = (m.group(2), m.group(3)) if m else (None, None)
            if ln.startswith("author "):
                author = who
            else:
                committer = who
    return parents, author, committer, msg


def path_problem(path, cfg):
    """這個路徑能不能出現在包裡（裁決 5 的「一律不收」清單＋保護範圍＋路徑寫法）。可以回 None；不行回一句原因。比對不分大小寫。"""
    rc_ = rcfg(cfg)
    low = path.lower()
    segs = path.replace("\\", "/").split("/")
    if not path or path.startswith("/") or "\\" in path or ":" in path:
        return "路徑寫法不對（開頭是斜線、或含有反斜線、冒號）"
    for s in segs:
        if s in ("", ".", "..") or s != s.rstrip(". ") or any(ord(ch) < 32 for ch in s):
            return "路徑裡有空的、點、或結尾是點或空白的一段（Windows 會把它當成別的檔）"
        if re.match(r"^\.git", s, re.I) and s.lower() not in (".gitignore",) or re.search(r"~\d+$", s):
            return "路徑裡有 .git 開頭或短檔名（~1）寫法的一段"
    protected = list(cfg["tier1"]["paths"]) + list(cfg["selfFiles"]["paths"]) + list((cfg.get("verify") or {}).get("protected") or [])
    if C.glob_match(low, protected):
        return "動到保護範圍的檔（保護檔只走筆電本機的一般模式）"
    if C.glob_match(low, rc_.get("neverPaths") or []):
        return "動到接收模式一律不收的檔（拿來改規則或設定的檔）"
    if segs[-1].lower() in [n.lower() for n in (rc_.get("neverNames") or [])]:
        return "動到接收模式一律不收的檔名（%s）" % segs[-1]
    return None


def _changed_entries(main_root, parent, sha):
    """一筆 commit 改到的檔：[(新的模式, 舊的模式, 新的內容編號, 狀態, 路徑)]。不偵測改名（改名＝刪一個、加一個）。"""
    rc, out = C.git(["diff-tree", "-r", "-z", "--raw", "--no-renames", "--no-abbrev", parent, sha], main_root, timeout=60)
    if rc != 0:
        raise Reject(5, "查不出 commit %s 改了哪些檔" % sha[:7])
    toks = [t for t in out.split("\0") if t != ""]
    res = []
    i = 0
    while i + 1 < len(toks):
        meta, path = toks[i].split(), toks[i + 1]
        i += 2
        if len(meta) != 5 or not meta[0].startswith(":"):
            raise Reject(5, "看不懂 commit %s 改檔的清單" % sha[:7])
        res.append((meta[1], meta[0][1:], meta[3], meta[4], path))
    return res


def _blob_is_big_binary(main_root, blob, limit):
    rc, size = C.git(["cat-file", "-s", blob], main_root)
    if rc != 0 or not size.strip().isdigit():
        raise Reject(8, "讀不到內容 %s 的大小" % blob[:7])
    if int(size) <= limit:
        return False
    import subprocess
    try:
        p = subprocess.run(["git", "cat-file", "blob", blob], cwd=main_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=C.git_env())
        return b"\0" in p.stdout[:8000]
    except Exception:                                              # noqa: B902
        raise Reject(8, "讀不到內容 %s" % blob[:7])


def replies_problem(path):
    """replies.md 的格式（同 03_第三方審查.md 的表）：每一列都要是「| 留言 id | 等級 | 檔案:行 | 採納並修／不採納 | 理由或修在哪 |」。"""
    import iw_review as R
    try:
        text = io.open(path, encoding="utf-8").read()
    except Exception:                                              # noqa: B902
        return "讀不到 replies.md"
    rows = 0
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not re.sub(r"[\s|:\-]", "", s) or "留言 id" in s:
            continue                                                # 表頭與分隔線
        if len(cells) < 5 or not re.match(r"^\d+(-\d+)?$", cells[0]):
            return "replies.md 有一列不是「留言 id｜等級｜檔案:行｜回覆｜理由」的格式"
        if cells[3] not in (R.RESPONSE_ADOPT, R.RESPONSE_REJECT) or R.blank_reason(cells[4]):
            return "replies.md 留言 %s 那一列的回覆不是「%s」或「%s」、或理由是空的" % (cells[0], R.RESPONSE_ADOPT, R.RESPONSE_REJECT)
        rows += 1
    return None if rows else "replies.md 裡沒有任何一列回覆"


def check_package(main_root, sd, cfg, stage, pkg_dir, fetch=True):
    """驗包八條（規格 2.1、裁決 2、3、5、11）。過了回 info；任何一條不過丟 Reject。"""
    import iw_notify as N
    rc_ = rcfg(cfg)
    st = ST.load(sd)
    name = os.path.basename(os.path.normpath(pkg_dir))
    # ---- 第 1 條：格式、階段、seq（資料夾名稱＝seq；拒收的也佔號：seq＝上一個收到的＋1）
    if not re.match(r"^\d{2,}$", name):
        raise Reject(1, "包的資料夾名稱要是兩位數的 seq（例如 01），現在是「%s」" % name[:20])
    seq = int(name)
    try:
        m = C.load_json(os.path.join(pkg_dir, MANIFEST))
    except Exception:                                              # noqa: B902
        raise Reject(1, "manifest.json 讀不到或不是 JSON")
    if not isinstance(m, dict) or m.get("schema") != 1:
        raise Reject(1, "manifest.json 的 schema 不是 1")
    if m.get("stage") != stage:
        raise Reject(1, "manifest.json 的階段（%s）不是現在的階段（%s）" % (str(m.get("stage"))[:20], stage))
    last = int(receive_state(st).get("last_seq") or 0)
    if m.get("seq") != seq or seq != last + 1:
        raise Reject(1, "seq 不對：資料夾是 %s、manifest 寫 %s、應該是 %d（上一個收到的是 %d）" % (name, str(m.get("seq"))[:6], last + 1, last))
    base, head = str(m.get("base") or "").lower(), str(m.get("head") or "").lower()
    if not SHA_RE.match(base) or not SHA_RE.match(head) or base == head:
        raise Reject(1, "manifest.json 的 base／head 不是 40 碼的 commit（或兩個一樣）")
    author = m.get("author") or {}
    want = cfg["cloudAuthor"]
    if not isinstance(author, dict) or author.get("name") != want["name"] or author.get("email") != want["email"]:
        raise Reject(4, "manifest.json 的作者跟設定的雲端作者不一樣")
    # ---- 第 2 條：bundle 的雜湊、git bundle verify、只有一個 ref、前提只有 base
    bundle = os.path.join(pkg_dir, BUNDLE)
    if not os.path.isfile(bundle) or os.path.islink(bundle):
        raise Reject(2, "沒有 bundle.git")
    if os.path.getsize(bundle) > int(rc_.get("bundleMaxBytes", 50 * 1024 * 1024)):
        raise Reject(8, "bundle.git 太大（上限 %d MB）" % (int(rc_.get("bundleMaxBytes", 0)) // (1024 * 1024)))
    want_sha = str(m.get("bundle_sha256") or "").lower()
    actual = sha256_file(bundle)
    if not re.match(r"^[0-9a-f]{64}$", want_sha) or actual != want_sha:
        raise Reject(2, "bundle.git 的 SHA-256 跟 manifest.json 寫的不一樣")
    copy = private_copy(sd, stage, seq)
    shutil.copyfile(bundle, copy)
    if sha256_file(copy) != actual:                                 # 複製的當下被換掉
        raise Reject(2, "bundle.git 在驗的時候被換掉了")
    prereq, refs = read_bundle_header(copy)
    ref = "refs/heads/" + branch_name(cfg, stage)
    if refs != [(head, ref)]:
        raise Reject(2, "bundle.git 要剛好只有一個 ref（%s，指到 manifest 的 head）" % ref)
    if prereq != [base]:
        raise Reject(2, "bundle.git 需要的前提 commit 要剛好是 manifest 的 base")
    # ---- 第 3 條：base 是分支現在的頭；分支還沒有（第一包）＝在正式版的歷史上、之後只有排程的資料 commit
    rc, cur = C.git(["rev-parse", "-q", "--verify", ref + "^{commit}"], main_root)
    if rc == 0 and cur.strip():
        if cur.strip().lower() != base:
            raise Reject(3, "base 不是分支 %s 現在的頭（%s）" % (branch_name(cfg, stage), cur.strip()[:7]))
    else:
        if fetch:
            C.git(["fetch", "-q", cfg["remote"], cfg["mainBranch"]], main_root, timeout=60)
        main_ref = C.remote_main_ref(cfg)
        rc, _ = C.git(["merge-base", "--is-ancestor", base, main_ref], main_root)
        if rc != 0:
            raise Reject(3, "第一包的 base 不在正式版（main）的歷史上")
        why = data_commits_between(main_root, cfg, base, main_ref)
        if why:
            raise Reject(3, "第一包的 base 太舊：%s" % why)
    rc, out = C.git(["bundle", "verify", copy], main_root, timeout=60)
    if rc != 0:
        raise Reject(2, "git bundle verify 沒過")
    # 物件先抓進來（不寫任何 ref、不寫 FETCH_HEAD；物件本身不改任何檔），才讀得到 commit
    rc, out = C.git(["fetch", "-q", "--no-write-fetch-head", "--no-tags", copy, ref], main_root, timeout=120)
    if rc != 0:
        raise Reject(2, "讀不進 bundle.git 的內容")
    # ---- 第 4 條：每一筆 commit 單 parent、一條直線、作者與提交者＝cloudAuthor、訊息過隱私掃描、標題不像排程的資料 commit
    rc, out = C.git(["rev-list", "--reverse", "--topo-order", "%s..%s" % (base, head)], main_root, timeout=60)
    shas = [x.strip().lower() for x in out.split("\n") if x.strip()] if rc == 0 else []
    if not shas:
        raise Reject(4, "base..head 之間沒有 commit")
    prev, commits, pats = base, [], _subject_patterns(cfg)
    for sha in shas:
        rc, raw = C.git(["cat-file", "commit", sha], main_root)
        if rc != 0:
            raise Reject(4, "讀不到 commit %s" % sha[:7])
        parents, au, co, msg = _parse_commit(raw)
        if parents != [prev]:
            raise Reject(4, "commit %s 不是單一 parent 接在前一筆後面（不收合併與分岔）" % sha[:7])
        if au != (want["name"], want["email"]) or co != (want["name"], want["email"]):
            raise Reject(4, "commit %s 的作者或提交者不是設定的雲端作者" % sha[:7])
        subject = msg.split("\n", 1)[0].strip()
        problems = N.text_privacy_problems(main_root, msg)
        if problems:
            raise Reject(4, "commit %s 的訊息沒過隱私掃描（%s）" % (sha[:7], "；".join(problems)[:200]))
        if any(p.match(subject) for p in pats):
            raise Reject(4, "commit %s 的標題長得像排程的資料更新（合併時會被當成資料 commit）" % sha[:7])
        commits.append({"sha": sha, "subject": subject, "parent": prev})
        prev = sha
    if prev != head:
        raise Reject(4, "commit 的鏈接不到 manifest 的 head")
    mc = m.get("commits")
    if not isinstance(mc, list) or [(str(c.get("sha") or "").lower(), c.get("subject")) for c in mc if isinstance(c, dict)] != \
            [(c["sha"], c["subject"]) for c in commits] or len(mc) != len(commits):
        raise Reject(4, "manifest.json 的 commits 跟 bundle 實際的內容不一致")
    # ---- 第 5～8 條：檔案清單（每一筆 commit 改到的檔的聯集）、保護範圍與一律不收、tier2、二進位
    files, blobs, modes_bad = set(), [], []
    limit = int(rc_.get("binaryMaxBytes", 2 * 1024 * 1024))
    for c in commits:
        for new_mode, old_mode, blob, status, path in _changed_entries(main_root, c["parent"], c["sha"]):
            files.add(path)
            if new_mode in ("120000", "160000") or old_mode in ("120000", "160000"):
                modes_bad.append(path)
            if status != "D" and not re.match(r"^0+$", blob):
                blobs.append((blob, path))
    mf = m.get("files")
    if not isinstance(mf, list) or len(mf) != len(set(mf)) or set(mf) != files:
        raise Reject(5, "manifest.json 的 files 跟實際改到的檔不一樣（多列或少列）")
    if modes_bad:
        raise Reject(6, "包裡有捷徑（symlink）或子模組：%s" % "、".join(sorted(modes_bad)[:5]))
    for path in sorted(files):
        why = path_problem(path, cfg)
        if why:
            raise Reject(6, "%s：%s" % (path[:80], why))
    for blob, path in blobs:
        if _blob_is_big_binary(main_root, blob, limit):
            raise Reject(8, "%s 是超過 %d MB 的二進位檔" % (path[:80], limit // (1024 * 1024)))
    named = N.named_term_blob_problem(main_root, main_root, blobs)
    if named:
        raise Reject(4, named)
    rp = os.path.join(pkg_dir, REPLIES)
    if os.path.exists(rp):
        why = replies_problem(rp)
        if why:
            raise Reject(1, why)
    tier2 = sorted(f for f in files if C.glob_match(f, cfg["tier2"]["paths"]))
    return {"seq": seq, "base": base, "head": head, "files": sorted(files), "tier2": tier2, "commits": [{"sha": c["sha"], "subject": c["subject"]} for c in commits],
            "bundle_sha256": want_sha, "copy": copy, "replies": os.path.exists(rp), "note": str(m.get("note") or "")[:200]}


def reject(main_root, sd, cfg, stage, pkg_dir, err, runner=None):
    """拒收：寫 rejected.md、記狀態（seq 佔號）、寫 status.json、寄小事的短信。不進暫停：回到等包。"""
    import iw_notify as N
    name = os.path.basename(os.path.normpath(pkg_dir))
    seq = int(name) if re.match(r"^\d{2,}$", name) else None
    now = C.iso()
    try:
        with io.open(os.path.join(pkg_dir, REJECTED), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# 拒收（%s）\n\n- 第 %s 條：%s\n- 時間：%s\n- 這個資料夾留著當證據；請把修正做成下一個 seq 的包。\n"
                     % (name, err.rule, err.reason, now, ))
    except Exception:                                              # noqa: B902
        pass

    def fn(s, r):
        if seq is not None and seq == int(r.get("last_seq") or 0) + 1:
            r["last_seq"] = seq                                     # 拒收的也佔號（裁決 3）
        r["last_result"] = "rejected"
        r["reason"] = "第 %s 條：%s" % (err.rule, err.reason)
        r["last_at"] = now
    _set_receive(sd, stage, fn)
    ST.log(sd, {"event": "receive-reject", "stage": stage, "seq": name, "rule": err.rule, "reason": err.reason[:200]})
    write_status(main_root, sd, cfg, stage)
    N.fallback(main_root, sd, cfg, "雲端的包 %s 被拒收（第 %s 條：%s）。筆電回到等包，請把修正做成下一個 seq 的包。" % (name, err.rule, err.reason[:160]),
               stage=stage, runner=runner, min_gap_key="receive-reject-%s" % name, minor_reason="拒收一個雲端的包、回到等包（不暫停）")


def accept(main_root, sd, cfg, stage, info):
    now = C.iso()

    def fn(s, r):
        r["last_seq"] = info["seq"]
        r["last_result"] = "accepted"
        r["reason"] = None
        r["last_at"] = now
        r["accepted"] = {"seq": info["seq"], "base": info["base"], "head": info["head"], "bundle_sha256": info["bundle_sha256"],
                         "copy": info["copy"], "files": info["files"], "tier2": info["tier2"], "at": now}
    _set_receive(sd, stage, fn)
    ST.log(sd, {"event": "receive-accept", "stage": stage, "seq": info["seq"], "head": info["head"][:12], "files": len(info["files"])})
    write_status(main_root, sd, cfg, stage)


# ---------------------------------------------------------------- 套包

def _locked(st):
    return ST.is_paused(st) or st.get("status") in ("awaiting_approval", "approved", "merged", "done")


def apply_package(main_root, sd, cfg, stage, pkg_dir):
    """只套剛驗過的那一包。成功回 (True, 說明)；不行回 (False, 原因)——不行的時候什麼都沒改。"""
    st = ST.load(sd)
    if st.get("stage") != stage or ST.mode_of(st) != "receive" or not st.get("active"):
        return False, "現在不是階段 %s 的接收模式" % stage
    if _locked(st):
        return False, "暫停中、或已經寄了「可以合併」的信（狀態 %s），不套新的包" % st.get("status")
    r = receive_state(st)
    acc = r.get("accepted") or {}
    name = os.path.basename(os.path.normpath(pkg_dir))
    if not re.match(r"^\d{2,}$", name) or acc.get("seq") != int(name) or r.get("last_result") != "accepted" or r.get("last_seq") != acc.get("seq"):
        return False, "包 %s 不是最後一個驗過的包（先 receive --check）" % name
    if r.get("applied_seq") is not None and r.get("applied_seq") == acc.get("seq"):
        return False, "包 %s 已經套過了" % name
    copy = acc.get("copy")
    if not copy or sha256_file(copy) != acc.get("bundle_sha256"):
        return False, "驗過的那一份 bundle 不見了或被改過，不套（請重新 receive --check）"
    branch = branch_name(cfg, stage)
    ref = "refs/heads/" + branch
    wt = stage_worktree(main_root, cfg, stage)
    rc, cur = C.git(["rev-parse", "-q", "--verify", ref + "^{commit}"], main_root)
    cur = cur.strip().lower() if rc == 0 else ""
    if cur and cur != acc["base"]:
        return False, "分支 %s 現在的頭（%s）不是這一包的 base（%s）" % (branch, cur[:7], acc["base"][:7])
    if not os.path.isdir(wt):
        args = (["worktree", "add", wt, branch] if cur else ["worktree", "add", "--no-track", "-b", branch, wt, acc["base"]])
        rc, out = C.git(args, main_root, timeout=120)
        if rc != 0:
            return False, "建不出這個階段的 worktree（%s）" % out[:160]
    rc, head = C.git(["rev-parse", "HEAD"], wt)
    rc2, br = C.git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
    if rc != 0 or head.strip().lower() != acc["base"] or br.strip() != branch:
        return False, "worktree 不在分支 %s 的 base（%s）上" % (branch, acc["base"][:7])
    rc, dirty = C.git(["status", "--porcelain", "--untracked-files=all"], wt)
    if rc != 0 or dirty.strip():
        return False, "worktree 有沒 commit 的改動，不套（接收模式不改程式）"
    rc, out = C.git(["fetch", "-q", "--no-tags", copy, ref], wt, timeout=120)
    if rc != 0:
        return False, "從驗過的 bundle 抓不進來（%s）" % out[:160]
    rc, fh = C.git(["rev-parse", "FETCH_HEAD"], wt)
    if rc != 0 or fh.strip().lower() != acc["head"]:
        return False, "抓進來的 commit 不是驗過的那一個"
    rc, out = C.git(["merge", "--ff-only", "-q", acc["head"]], wt, timeout=120)
    rc2, now_head = C.git(["rev-parse", "HEAD"], wt)
    if rc != 0 or now_head.strip().lower() != acc["head"]:
        return False, "快轉沒有成功（%s）" % out[:160]
    now = C.iso()

    def fn(s, rr):
        rr["applied_seq"] = acc["seq"]
        rr["applied_head"] = acc["head"]
        rr["applied_at"] = now
        rr["tested_head"] = None
        s["clock_started_at"] = now                                 # 接收模式的 8 小時從最後一次接受包起算（裁決 8）
    _set_receive(sd, stage, fn)
    ST.log(sd, {"event": "receive-apply", "stage": stage, "seq": acc["seq"], "head": acc["head"][:12]})
    write_status(main_root, sd, cfg, stage)
    t2 = acc.get("tier2") or []
    return True, ("已套上包 %s（%s → %s，%d 個檔）。%s" % (name, acc["base"][:7], acc["head"][:7], len(acc.get("files") or []),
                                                    ("動到要先給 David 看 diff 的檔（%s）：跑完全套之後寄 --kind tier2，等他「繼續」才能推。" % "、".join(t2)) if t2 else ""))


def mark_tested(main_root, sd, cfg, stage, runner=None):
    """本機全套跑完之後（裁決 7）：tests.txt 對得上、全綠；主目錄生效中的保護檔與推送前的檢查沒被動過。過了記 tested_head。"""
    import iw_events as E
    import iw_notify as N
    st = ST.load(sd)
    if st.get("stage") != stage or ST.mode_of(st) != "receive":
        return False, "現在不是階段 %s 的接收模式" % stage
    r = receive_state(st)
    wt = stage_worktree(main_root, cfg, stage)
    rc, head = C.git(["rev-parse", "HEAD"], wt)
    head = head.strip().lower() if rc == 0 else ""
    if not head or head != r.get("applied_head"):
        return False, "worktree 現在的 commit 不是收包程式套上的那一個"
    rc, dirty = C.git(["status", "--porcelain", "--untracked-files=all"], wt)
    if rc != 0 or dirty.strip():
        return False, "worktree 有沒 commit 的改動（測試留下的檔也算）；接收模式不改程式"
    path = os.path.join(runs_dir(main_root, cfg, stage), "tests.txt")
    try:
        first = io.open(path, encoding="utf-8", errors="replace").readline().strip()
    except Exception:                                              # noqa: B902
        return False, "讀不到 %s/%s/tests.txt" % (cfg["runsDir"], stage)
    if first != "commit " + head:
        return False, "tests.txt 的第一行不是「commit %s」（這份輸出不是這個 commit 跑的；這一關只防忘了重跑，證據以驗收機為準）" % head
    why = N.local_test_problem(path)
    if why:
        return False, why
    problems = E.integrity_problems(E.Env(root=main_root))
    if problems:
        now = C.iso()
        detail = "跑完本機全套之後，生效中的保護檔或推送前的檢查跟放行過的版本不一樣：%s" % "；".join(problems[:4])

        def fn(s, rr):
            s["stop_required"] = {"code": 3, "reason": detail[:300], "at": now}
            if not s.get("pause"):
                s["pause"] = {"reason": "integrity", "detail": detail[:300], "code": 3, "at": now, "retries": 0}
        _set_receive(sd, stage, fn)
        ST.log(sd, {"event": "receive-integrity", "stage": stage, "problems": problems[:6]})
        write_status(main_root, sd, cfg, stage)
        N.fallback(main_root, sd, cfg, detail[:400] + "。自動駕駛已暫停；請看 Claude Code，確認沒事之後再輸入「繼續 %s」。" % stage,
                   stage=stage, runner=runner, min_gap_key="receive-integrity")
        return False, detail

    def fn2(s, rr):
        rr["tested_head"] = head
        rr["tested_at"] = C.iso()
    _set_receive(sd, stage, fn2)
    ST.log(sd, {"event": "receive-tested", "stage": stage, "head": head[:12]})
    write_status(main_root, sd, cfg, stage)
    return True, "本機全套與完整性檢查都過了（%s）。現在可以推 %s。" % (head[:7], branch_name(cfg, stage))


# ---------------------------------------------------------------- 等包

def wait_inbox(main_root, sd, cfg, stage, minutes=None, poll=15.0, sleep=time.sleep, clock=time.time):
    """等下一個 seq 的包放好。回傳 (找到了沒, 資料夾或說明)。"""
    rc_ = rcfg(cfg)
    minutes = float(minutes if minutes is not None else rc_.get("waitMinutes", 240))
    stable = float(rc_.get("stableSeconds", 30))
    st = ST.load(sd)
    nxt = int(receive_state(st).get("last_seq") or 0) + 1
    d = os.path.join(inbox_dir(main_root, cfg, stage), seq_name(nxt))
    deadline = clock() + 60.0 * minutes
    _set_receive(sd, stage, lambda s, r: r.__setitem__("waiting_since", C.iso()))
    write_status(main_root, sd, cfg, stage)
    seen, since = None, None
    while True:
        a, b = os.path.join(d, MANIFEST), os.path.join(d, BUNDLE)
        if os.path.isfile(a) and os.path.isfile(b):
            sizes = (os.path.getsize(a), os.path.getsize(b))
            if sizes != seen:
                seen, since = sizes, clock()
            elif clock() - since >= stable:
                _set_receive(sd, stage, lambda s, r: r.__setitem__("waiting_since", None))
                write_status(main_root, sd, cfg, stage)
                return True, d
        if clock() >= deadline:
            write_status(main_root, sd, cfg, stage)
            return False, "等了 %s 分鐘，還沒有 %s/%s 的包" % (("%.0f" % minutes), rc_.get("inboxDir") or "inbox", seq_name(nxt))
        sleep(poll)


# ---------------------------------------------------------------- status.json

def _scrub(obj):
    """status.json 給 Cowork 讀：任何字串裡的本機路徑、信箱、像權杖的東西換掉（規格 2.2：內容不得含本機絕對路徑、信箱）。"""
    import iw_notify as N
    if isinstance(obj, dict):
        return dict((k, _scrub(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    if not isinstance(obj, str):
        return obj
    s = obj
    for rx in N._PR_LOCAL:                                          # 整段路徑一起換掉（不只換磁碟代號）
        s = re.sub(rx.pattern + r"[^\s，。、；）)\]」』\"']*", "<本機路徑>", s, flags=rx.flags & ~re.UNICODE) if rx.search(s) else s
    for rx in N._PR_SECRETS:
        s = rx.sub("<像權杖的字串>", s)
    s = N._PR_MAIL.sub(lambda mm: mm.group(0) if N.public_mail_ok(mm.group(0)) else "<信箱>", s)
    return s


def _codex_block(st, cfg, stage, main_root):
    cache = (st.get("status_cache") or {}).get("codex")
    if not cache:
        return {"rounds": 0, "complete": False, "p0_on_head": False, "comments": []}
    import iw_review as R
    rows = R.parse_responses(os.path.join(runs_dir(main_root, cfg, stage), (cfg.get("externalReview") or {}).get("responsesFile") or "03_第三方審查.md"))
    cap = int(rcfg(cfg).get("commentMaxChars", 600))
    out = []
    for f in cache.get("comments") or []:
        body = str(f.get("body") or "")
        out.append({"id": f.get("id"), "path": f.get("path"), "line": f.get("line"), "severity": f.get("severity"),
                    "body": body[:cap], "replied": (rows.get(f.get("id")) or {}).get("response") in ("adopt", "reject")})
    return {"rounds": cache.get("rounds") or 0, "complete": bool(cache.get("complete")), "p0_on_head": bool(cache.get("p0_on_head")),
            "commit": cache.get("commit"), "comments": out}


def build_status(main_root, sd, cfg, stage, now=None):
    st = ST.load(sd)
    if st.get("stage") != stage:
        return None
    r = receive_state(st)
    branch = branch_name(cfg, stage)
    rc, head = C.git(["rev-parse", "-q", "--verify", "refs/heads/%s^{commit}" % branch], main_root)
    head = head.strip().lower() if rc == 0 and head.strip() else None
    mode = ST.mode_of(st)
    cache = st.get("status_cache") or {}
    v = cache.get("verify") or {}
    reviews = [x for x in (st.get("reviews") or []) if isinstance(x, dict) and x.get("kind") == "acceptance"]
    p = st.get("pause")
    cred = st.get("credential") or {}
    status = st.get("status") if st.get("active") else "ended"
    if mode == "receive" and r.get("waiting_since") and status == "running" and not ST.is_paused(st):
        status = "waiting"
    obj = {
        "schema": 1, "updated_at": C.iso(now), "stage": stage, "mode": mode, "status": status,
        "branch": {"name": branch, "head": head},
        "inbox": ({"last_seq": int(r.get("last_seq") or 0), "last_result": r.get("last_result"), "reason": r.get("reason"),
                   "applied_head": r.get("applied_head"), "tested_head": r.get("tested_head")} if mode == "receive" else None),
        "verify": {"state": v.get("state") or "none", "run_url": v.get("run_url"), "tests": v.get("tests"), "red_kind": v.get("red_kind"),
                   "commit": v.get("commit")},
        "codex": _codex_block(st, cfg, stage, main_root),
        "reviewer": {"verdict": (reviews[-1].get("verdict") if reviews else None) or "none", "commit": reviews[-1].get("commit") if reviews else None},
        "pause": ({"code": p.get("code"), "reason": p.get("detail") or p.get("reason"), "at": p.get("at")} if isinstance(p, dict) else None),
        "credential": ({"candidate": cred.get("candidate"), "expires_at": cred.get("expires_at")}
                       if cred and cred.get("stage") == stage and not cred.get("revoked") and not cred.get("docs") else None),
    }
    return _scrub(obj)


def write_status(main_root, sd, cfg, stage, now=None):
    """寫 .autopilot/runs/<階段>/status.json（先寫暫存檔再換名）。紀錄裡的階段不是這一個就不寫，回 None。"""
    obj = build_status(main_root, sd, cfg, stage, now=now)
    if obj is None:
        return None
    d = runs_dir(main_root, cfg, stage)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "status.json")
    tmp = p + ".tmp.%d" % os.getpid()
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, p)
    return p


def remember_verify(sd, stage, head, vs):
    why = str(vs.get("why") or "")
    state = "green" if vs.get("green") else ("running" if vs.get("pending") else
                                             ("none" if why.startswith(("驗收機還沒有", "查不到驗收機")) else "red"))
    res = vs.get("result") or {}
    remember(sd, stage, "verify", {"commit": head, "state": state, "run_url": vs.get("url"),
                                   "tests": vs.get("ran") if vs.get("ran") is not None else (res.get("tests") or {}).get("ran"),
                                   "red_kind": None if vs.get("green") else why[:200]})


def remember_codex(sd, stage, head, ext):
    comments = [{"id": f.get("id"), "path": f.get("path"), "line": f.get("line"), "severity": f.get("severity"),
                 "body": f.get("body") or f.get("excerpt") or ""} for f in (ext.get("findings") or [])]
    remember(sd, stage, "codex", {"commit": head, "rounds": ext.get("rounds") or 0, "complete": bool(ext.get("complete")),
                                  "p0_on_head": bool(ext.get("p0_on_head")), "comments": comments})


def digest(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
