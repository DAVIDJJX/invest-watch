# -*- coding: utf-8 -*-
"""
iw_notify.py — 自動駕駛的通知信、停止報告、事後偵測。

寄信的方法：觸發私人倉庫的 notify.yml（GitHub 機器人開一個 issue 指派給你 → GitHub 寄信、手機 App 通知）。
用的是這台電腦上已經登入的 gh；這裡不存、也不處理任何密碼或權杖。

指令（由 Claude 在停下來的時候執行；hook 也會直接呼叫裡面的函式）：
  python iw_notify.py send  --stage <階段> --kind stop|tier2|ready --report <報告檔> [--worktree <路徑>]
  python iw_notify.py close --stage <階段>          放行並合併完之後，把那一封的 issue 留言「已放行」並關掉
  python iw_notify.py test                          寄一封測試信
  python iw_notify.py status                        看現在的狀態（唯讀）
  python iw_notify.py retry                         把之前沒寄出去的再寄一次
  python iw_notify.py pr open|edit|request-review --stage <階段> [--title …] [--body-file …]   PR 唯三的寫入（P2；文字先過隱私掃描）
  python iw_notify.py verify --stage <階段> [--commit …] [--wait]                            驗收機這個 commit 綠不綠（唯讀）
  python iw_notify.py review-status --stage <階段> [--wait]                                 外部審查（Codex）完成了沒（唯讀）

「可以合併」（ready）那一種信寄出之前，程式自己會檢查：標籤在不在、分支推了沒、有沒有動到不能動的檔、
要先給 David 看 diff 的檔看過了沒、審查代理批准了沒（紀錄是 hook 寫的，不是 Claude 轉述的）。
P2 之後多四件（關卡）：驗收機對這個 commit 綠、外部審查完成（Codex，或 David 手打免除）、Codex 的每條意見都有回覆、重大意見沒有被判不採納；
本機的測試數要跟驗收機一致。任何一項不過就不寄；沒有任何跳過的選項（P1-1 的 --no-review 已拿掉）。一般模式也一樣。
"""
import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import time

import iw_common as C
import iw_review as R
import iw_state as ST

SECTIONS =["一句話", "你要決定的事", "做了什麼", "怎麼自己看", "名詞解釋", "要繼續", "要修改", "不確定"]
KIND_NAMES = {"stop": "停下來了，要你決定", "tier2": "請看 diff", "ready": "做完了，等你放行才合併"}
LEVEL_KEYS = ("minor", "decide", "approve")                       # 三個等級的字串只寫在 config.json 的 stopLevels（有測試釘住）


def level_for(st, cfg, kind=None):
    """信的等級（規格 P1-1 第 3 節）：由程式依停下的原因決定，模型改不了。回傳 (鍵, 標籤, 白話原因)；標籤的字只寫在 config.json 的 stopLevels。
      minor（小事）：守門擋下一個動作（編號 8 或沒有編號），那個動作沒有執行、暫停之後沒有再試任何不准的動作。
      approve（放行上線）：寄「可以合併」的信；或已經合併、只差文件那一筆（要再放行一次重開時間窗）。
      decide（要決定）：其他所有情況（程式判定的停止條件、模型自己判斷停、請看 diff、模型被換、用量上限、冒充的指令詞、暫停後又重試、沒寄信就停）。"""
    labels = cfg["stopLevels"]
    if kind == "ready":
        return "approve", labels["approve"], "做完了，停在合併前等你放行"
    if ST.merged_awaiting_docs(st):
        return "approve", labels["approve"], "已經合併進正式版，只差補文件那一筆；要你再放行一次重開時間窗"
    p = st.get("pause") or {}
    sr = st.get("stop_required")
    reason = str(p.get("reason") or "")
    code = p.get("code")
    retries = int(p.get("retries") or 0)
    if reason == "blocked" and code in (None, 8) and not retries and not sr:
        return "minor", labels["minor"], "檢查程式擋下一個動作（%s），那個動作沒有執行、沒有重試、沒有改到任何東西" % ("停止條件 %s" % code if code else "沒有編號")
    if sr:
        why = "停止條件 %s：%s" % (sr.get("code"), sr.get("reason"))
    elif reason == "blocked":
        why = "檢查程式擋下一個動作（%s）%s" % ("停止條件 %s" % code if code else "沒有編號", "，之後又試了 %d 次不准的動作" % retries if retries else "")
    elif reason == "model":
        why = "模型被換掉"
    elif reason == "effort":
        why = "思考強度被改掉"
    elif reason.startswith("api:"):
        why = "用量上限或 API 錯誤"
    elif reason == "forged":
        why = "收到不是 David 親手輸入的指令詞"
    elif reason == "unknown":
        why = "沒有寄停止報告就停了"
    elif reason == "tier2" or kind == "tier2":
        why = "動到了要先給你看 diff 的檔"
    elif reason.startswith("legacy"):
        why = "舊版紀錄的暫停"
    elif kind == "stop":
        why = "Claude 自己判斷要停下來問你"
    else:
        why = "停下來了"
    return "decide", labels["decide"], why


def level_line(st, cfg, kind=None):
    """信的第一行：等級：…（程式依停下的原因判定：…）。回傳 (標籤, 這一行)。"""
    key, label, why = level_for(st, cfg, kind)
    return label, "等級：%s（程式依停下的原因判定：%s）" % (label, why)


# ---------------------------------------------------------------- 找程式

def find_exe(name, extra=()):
    p = shutil.which(name)
    if p:
        return p
    for cand in extra:
        if os.path.exists(cand):
            return cand
    return None


def gh_exe():
    return find_exe("gh", ("C:/Program Files/GitHub CLI/gh.exe", "/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"))


# ---------------------------------------------------------------- 寄出去

def dispatch(cfg, title, body, stage="", action="open", runner=None):
    """觸發 notify.yml。回傳 (成功, 說明)。runner 是測試用的替身。"""
    n = cfg["notify"]
    args = ["workflow", "run", n["workflow"], "-R", n["repo"], "-f", "title=" + title[:200], "-f", "body=" + body[:60000],
            "-f", "stage=" + (stage or ""), "-f", "action=" + action]
    if runner is not None:
        return runner(args)
    if os.environ.get(NO_SIDE_EFFECTS_ENV):
        return False, "測試模式：不對外寄信"
    exe = gh_exe()
    if not exe:
        return False, "這台電腦找不到 gh 指令"
    try:
        p = subprocess.run([exe] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    except Exception as e:                                         # noqa: B902
        return False, "gh 執行失敗：%r" % (e,)
    if p.returncode != 0:
        return False, "gh 回報失敗（%d）：%s" % (p.returncode, p.stderr.decode("utf-8", "replace").strip()[:300])
    return True, "已觸發 %s 的 %s" % (n["repo"], n["workflow"])


POPPED = []                                  # 這個程序裡「本來要跳」的本機通知（測試看這裡）
NO_SIDE_EFFECTS_ENV = "IW_TEST_NO_SIDE_EFFECTS"    # 測試程式自己設的開關：不對外寄信、不在桌面跳通知


def notify_desktop(title, text, real):
    """信寄不出去時的本機通知。real=False（寄信那一步被換成替身＝在測試裡）只記下來，不真的跳。
    為什麼要分：2026-10-01 測試模擬「信寄不出去」時，真的在 David 的桌面跳了好幾次通知，
    他以為真的有信寄不出去、有東西等著合併。測試不可以在別人的桌面上留下任何東西。"""
    POPPED.append((title, text))
    if real and not os.environ.get(NO_SIDE_EFFECTS_ENV):
        toast(title, text)


def toast(title, text):
    """跳一個本機通知（信寄不出去時的後援）。失敗就算了。只經過 notify_desktop() 呼叫。"""
    try:
        if C.IS_WINDOWS:
            ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                  "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                  "$x=$t.GetElementsByTagName('text');$x.Item(0).AppendChild($t.CreateTextNode($env:IW_T)) > $null;"
                  "$x.Item(1).AppendChild($t.CreateTextNode($env:IW_B)) > $null;"
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('InvestWatch').Show([Windows.UI.Notifications.ToastNotification]::new($t))")
            env = dict(os.environ, IW_T=title[:60], IW_B=text[:180])
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e", 'display notification "%s" with title "%s"' % (text[:180].replace('"', "'"), title[:60].replace('"', "'"))],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except Exception:                                              # noqa: B902
        pass


def outbox_dir(main_root, cfg):
    return os.path.join(main_root, *cfg["outboxDir"].split("/"))


def to_outbox(main_root, cfg, title, body, stage, action, why):
    d = outbox_dir(main_root, cfg)
    os.makedirs(d, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S") + "-%s.md" % re.sub(r"[^A-Za-z0-9._-]", "_", stage or "x")
    p = os.path.join(d, name)
    with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("<!-- iw-outbox stage=%s action=%s -->\n<!-- 沒寄出去的原因：%s -->\n# %s\n\n%s\n" % (stage, action, why, title, body))
    return p


def deliver(main_root, cfg, title, body, stage="", action="open", runner=None, quiet=False):
    """寄信；寄不出去就寫進本機的待寄資料夾並跳通知。回傳 (成功, 說明)。"""
    ok, detail = dispatch(cfg, title, body, stage, action, runner)
    if ok:
        return True, detail
    p = to_outbox(main_root, cfg, title, body, stage, action, detail)
    if not quiet:
        notify_desktop("自動駕駛：信沒有寄出去", title, real=runner is None)
    return False, "%s。內容已寫到 %s，下次會再試。" % (detail, p)


def retry_outbox(main_root, cfg, runner=None):
    d = outbox_dir(main_root, cfg)
    sent = 0
    if not os.path.isdir(d):
        return 0
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if not name.endswith(".md") or not os.path.isfile(p):
            continue
        text = io.open(p, encoding="utf-8").read()
        m = re.match(r"<!-- iw-outbox stage=(.*?) action=(\w+) -->\n<!--.*?-->\n# (.*?)\n\n", text, re.S)
        if not m:
            continue
        body = "（這封信晚到了：原本在 %s 就該寄出。）\n\n" % name[:15] + text[m.end():]
        ok, _ = dispatch(cfg, m.group(3), body, m.group(1), m.group(2), runner)
        if not ok:
            break
        os.makedirs(os.path.join(d, "sent"), exist_ok=True)
        os.replace(p, os.path.join(d, "sent", name))
        sent += 1
    return sent


# ---------------------------------------------------------------- 停止報告的格式

def check_report(text, stage, kind, cfg, approve_word=False):
    """回傳問題清單（空的＝格式沒問題）。規格第 5 節：固定八段、一頁內、不貼程式碼。
    approve_word：已合併、只差文件那一筆時寄的信，「要繼續」那一行要的是「放行 <階段>」（重開時間窗），不是「繼續」。"""
    problems = []
    lines = [l for l in text.replace("\r\n", "\n").split("\n")]
    body_lines = [l for l in lines if l.strip()]
    pos = []
    for s in SECTIONS:
        idx = next((i for i, l in enumerate(lines) if re.match(r"^\s*(#+\s*|\*\*)?%s" % re.escape(s), l)), None)
        if idx is None:
            problems.append("少了「%s」這一段" % s)
        pos.append(idx)
    got = [p for p in pos if p is not None]
    if got != sorted(got):
        problems.append("八段的順序不對（應該是：%s）" % "→".join(SECTIONS))
    n = cfg["notify"]
    if len(body_lines) > int(n["reportMaxLines"]):
        problems.append("太長：%d 行（上限 %d 行，要一頁內）" % (len(body_lines), int(n["reportMaxLines"])))
    if len(text) > int(n["reportMaxChars"]):
        problems.append("太長：%d 個字（上限 %d）" % (len(text), int(n["reportMaxChars"])))
    if "```" in text or re.search(r"^( {4}|\t)\S", text, re.M):
        problems.append("信裡不貼程式碼（完整的技術內容留在本機的 .autopilot/runs/）")
    if pos[1] is not None and pos[2] is not None and pos[2] > pos[1]:
        decisions = [l for l in lines[pos[1] + 1:pos[2]] if re.match(r"^\s*(\d+[.、)]|[-*])\s+", l)]
        if len(decisions) > 3:
            problems.append("「你要決定的事」最多 3 件（現在 %d 件）" % len(decisions))
    want_go = ("放行 " if (kind == "ready" or approve_word) else "繼續 ") + stage
    if want_go not in text:
        problems.append("「要繼續」那一行要有 David 可以直接照打的字：%s" % want_go)
    if ("修改 %s：" % stage) not in text and ("修改 %s:" % stage) not in text:
        problems.append("「要修改」那一行要有：修改 %s：＿＿" % stage)
    if re.search(r"gh[opsu]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}", text):
        problems.append("信裡出現像權杖的字串")
    return problems


def one_line(text):
    """標題用：取「一句話」那一段的第一句。"""
    lines = text.replace("\r\n", "\n").split("\n")
    for i, l in enumerate(lines):
        if re.match(r"^\s*(#+\s*|\*\*)?一句話", l):
            rest = re.sub(r"^\s*(#+\s*|\*\*)?一句話\**[:：]?\s*", "", l).strip()
            if rest:
                return rest[:60]
            for l2 in lines[i + 1:]:
                if l2.strip():
                    return l2.strip()[:60]
    return ""


def model_line(st, cfg):
    """每份停止報告固定有的一行（規格第 11 節）。內容來自 hook 的紀錄，不是 Claude 自己寫的。"""
    models = sorted((st.get("models_seen") or {}).keys())
    efforts = sorted((st.get("effort_seen") or {}).keys())
    need_m, need_e = cfg["requiredModel"], cfg["requiredEffort"]
    m = "、".join(models) if models else "（沒有紀錄）"
    e = "、".join(efforts) if efforts else "設定值為 %s，這一段沒有紀錄可以驗證" % need_e
    switched = [x for x in models if x != need_m] or [x for x in efforts if x != need_e] or list(st.get("model_approved") or [])
    if st.get("model_violation"):
        switched = switched or [st["model_violation"].get("model")]
    sw = "否" if not switched else "是（%s）" % "、".join(sorted(set(str(x) for x in switched)))
    reviews = [r for r in (st.get("reviews") or []) if r.get("models")]
    tail = ""
    if reviews:
        rm = sorted(set(x for r in reviews for x in (r.get("models") or [])))
        tail = "／審查代理的模型：%s" % "、".join(rm)
    return "本階段使用模型：%s／思考強度：%s／中途是否切換：%s%s" % (m, e, sw, tail)


# ---------------------------------------------------------------- 事後偵測（main 上有沒有「不是資料更新、又沒有放行紀錄」的變更）

def tripwire(main_root, sd, cfg, st, fetch=True):
    """回傳 (給信用的一行或幾行, 發現清單)。"""
    remote, branch = cfg["remote"], cfg["mainBranch"]
    ref = "%s/%s" % (remote, branch)
    if fetch:
        rc, _ = C.git(["fetch", "-q", remote, branch], main_root, timeout=45)
        if rc != 0:
            return "安全檢查：這次連不上 GitHub，沒有檢查正式版（main）。", []
    base = st.get("tripwire_baseline")
    if not base:
        return "安全檢查：還沒有設定檢查的起點（安裝後第一次寄信才會設定）。", []
    rc, out = C.git(["log", "--first-parent", "--reverse", "--name-only", "--format=%x01%H%x02%P%x02%an%x02%s", "%s..%s" % (base, ref)], main_root, timeout=60)
    if rc != 0:
        return "安全檢查：查不出正式版（main）的變更紀錄（%s）。" % out[:80], []
    gold = cfg["goldJob"]
    cloud = cfg["cloudJob"]
    gold_files = set(gold["files"])
    okd = set()
    for a in ST.approvals(sd):
        for k in ("candidate", "merge", "docs"):
            if a.get(k):
                okd.add(a[k])
    findings, total = [], 0
    for chunk in out.split("\x01"):
        if not chunk.strip():
            continue
        head, _, rest = chunk.partition("\n")
        parts = head.split("\x02")
        if len(parts) < 4:
            continue
        sha, parents, author, subject = parts[0], parts[1].split(), parts[2], parts[3]
        files = [f for f in rest.split("\n") if f.strip()]
        total += 1
        if len(parents) >= 2:
            if parents[1] in okd or sha in okd:
                continue
            findings.append((sha, subject, "合併，但沒有放行紀錄"))
            continue
        if sha in okd:
            continue
        data_only = bool(files) and all(f.startswith(cloud["pathPrefix"]) for f in files)
        if data_only and author == cloud["author"] and any(re.match(p, subject) for p in cloud["subjectPatterns"]):
            continue
        if files and set(files) <= gold_files and any(re.match(p, subject) for p in gold["subjectPatterns"]):
            continue
        if data_only:
            findings.append((sha, subject, "只改資料，但不是排程固定的提交格式"))
        else:
            findings.append((sha, subject, "不是資料更新，也沒有放行紀錄"))
    if not findings:
        return "安全檢查：正式版（main）上從上次放行到現在有 %d 筆變更，都是排程的資料更新或放行過的合併。" % total, []
    lines = ["安全檢查：⚠ 正式版（main）上有 %d 筆變更要你看一下——" % len(findings)]
    for sha, subject, why in findings[:5]:
        lines.append("- %s「%s」：%s" % (sha[:7], subject[:50], why))
    if len(findings) > 5:
        lines.append("- ……還有 %d 筆" % (len(findings) - 5))
    return "\n".join(lines), findings


# ---------------------------------------------------------------- 第二層的檔（動了就要給 David 看 diff）

def tier_files(worktree, cfg, base_ref):
    """這個分支（含還沒 commit 的）相對於 main 動了哪些檔。回傳 (第一層, 自己的檔, 第二層, 全部)。"""
    files = set()
    for args in (["diff", "--name-only", base_ref + "...HEAD"], ["diff", "--name-only", "HEAD"], ["diff", "--name-only", "--cached"]):
        rc, out = C.git(args, worktree)
        if rc != 0:
            raise RuntimeError("查不出這個分支動了哪些檔：%s" % out[:120])
        files |= set(f for f in out.split("\n") if f.strip())
    rc, out = C.git(["ls-files", "--others", "--exclude-standard"], worktree)
    if rc == 0:
        files |= set(f for f in out.split("\n") if f.strip())
    files = sorted(files)
    t1 = [f for f in files if C.glob_match(f, cfg["tier1"]["paths"])]
    sf = [f for f in files if C.glob_match(f, cfg["selfFiles"]["paths"])]
    t2 = [f for f in files if C.glob_match(f, cfg["tier2"]["paths"])]
    return t1, sf, t2, files


def preexisting_protected(main_root, cfg, stage):
    """啟動自動駕駛的當下，這個階段的分支上已經動過哪些「自動駕駛期間動不得」的檔（第一層＋自動駕駛自己的檔）。
    回傳 {路徑: 那個當下的內容編號}（被刪掉的檔記成 "(deleted)"）。沒有 worktree 或沒有這類變更回 {}。
    用途：這些是 David 在場時做的（例如 P1 自己就是在改保護檔），不算自動駕駛動的；之後只要內容變了就不再算數。"""
    wt = stage_worktree(main_root, cfg, stage)
    if not os.path.isdir(wt):
        return {}
    rc, out = C.git(["-c", "core.quotepath=false", "diff", "--name-only", "%s/%s...HEAD" % (cfg["remote"], cfg["mainBranch"])], wt)
    if rc != 0:
        return {}
    hot = [f for f in out.split("\n") if f.strip() and (C.glob_match(f, cfg["tier1"]["paths"]) or C.glob_match(f, cfg["selfFiles"]["paths"]))]
    if not hot:
        return {}
    blobs = C.head_blobs(wt) or {}
    return dict((f, blobs.get(f, "(deleted)")) for f in hot)


def diff_text(worktree, base_ref, paths):
    out = []
    rc, a = C.git(["diff", base_ref + "...HEAD", "--"] + list(paths), worktree, timeout=60)
    rc2, b = C.git(["diff", "HEAD", "--"] + list(paths), worktree, timeout=60)
    if rc == 0 and a:
        out.append(a)
    if rc2 == 0 and b:
        out.append("（下面是還沒 commit 的部分）\n" + b)
    return "\n".join(out)


def diff_hash(worktree, base_ref, path):
    return hashlib.sha256(diff_text(worktree, base_ref, [path]).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- send

def stage_worktree(main_root, cfg, stage):
    return os.path.join(main_root, *(cfg["worktreeDir"].split("/") + [cfg["tagPrefix"] + stage]))


def github_slug(main_root, cfg):
    rc, out = C.git(["remote", "get-url", cfg["remote"]], main_root)
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(\.git)?/?$", out.strip()) if rc == 0 else None
    return m.group(1) if m else None


def cmd_send(a, main_root, sd, cfg, runner=None):
    st = ST.load(sd)
    stage, kind = a.stage, a.kind
    if not C.stage_ok(stage):
        return fail("階段名稱不對：%s" % stage)
    if st.get("active") and st.get("stage") != stage:
        return fail("現在自動駕駛的階段是 %s，不是 %s。" % (st.get("stage"), stage))
    merged_only_docs = ST.merged_awaiting_docs(st) and st.get("stage") == stage
    if kind == "ready" and merged_only_docs:
        return fail("這個階段已經合併進 main、只差文件那一筆，不能再寄「可以合併」的信。要重開文件的時間窗請 David 輸入「放行 %s」。" % stage)
    report = io.open(a.report, encoding="utf-8").read()
    problems = check_report(report, stage, kind, cfg, approve_word=merged_only_docs)
    if problems:
        return fail("停止報告的格式有問題，還沒寄：\n- " + "\n- ".join(problems))
    wt = a.worktree or stage_worktree(main_root, cfg, stage)
    base = "%s/%s" % (cfg["remote"], cfg["mainBranch"])
    if not a.offline:
        # 「這個分支改了哪些檔」是跟 origin/main 比的；那是本機的一個記號，可以被改（改到分支的頂端，就會算成什麼都沒改）。
        # 所以先向遠端問一次 main 真正的位置。問不到時，「可以合併」的信不寄（反正也寄不出去）。
        rc, _out = C.git(["fetch", "-q", cfg["remote"], cfg["mainBranch"]], main_root, timeout=45)
        if rc != 0 and kind == "ready":
            return fail("連不上 GitHub，沒辦法確認正式版（main）現在的位置，先不寄「可以合併」的信。等網路恢復再寄一次。")
    extra, cand = [], None
    acks = {}
    waived, head_for_lines = False, None
    t1 = sf = []
    runs_dir = os.path.join(main_root, *(cfg["runsDir"].split("/") + [stage]))
    has_wt = os.path.isdir(wt)
    if kind in ("ready", "tier2") and not has_wt:
        return fail("找不到這個階段的 worktree：%s" % wt)
    if has_wt:
        rc, head_for_lines = C.git(["rev-parse", "HEAD"], wt)
        head_for_lines = head_for_lines.strip() if rc == 0 else None
        t1, sf, t2, _all = tier_files(wt, cfg, base)
        if kind == "ready":
            if st.get("active"):
                # 自動駕駛期間：這兩類檔只接受「啟動之前就已經在分支上、而且內容到現在沒變」的（David 在場時做的）
                pre, blobs = st.get("preexisting") or {}, C.head_blobs(wt) or {}
                moved = [f for f in t1 + sf if not (f in pre and blobs.get(f, "(deleted)") == pre[f])]
                if moved:
                    return fail("這個分支在自動駕駛期間動到了不能動的檔（停止條件 3）：%s。不能寄「可以合併」的信；請改寄停止報告（--kind stop）。"
                                % "、".join(moved))
            when = "自動駕駛啟動之前、你在場時改的" if st.get("active") else "這個階段不是自動駕駛跑的"

            def names(files):
                return "、".join(files[:6]) + ("……等 %d 個" % len(files) if len(files) > 6 else "")
            if t1:
                extra.append("⚠ 注意：這個階段動到了排程、更新流程、資料來源或隱私相關的檔（%s）：%s。" % (when, names(t1)))
            if sf:
                extra.append("⚠ 注意：這個階段改了自動駕駛自己的檔——也就是保護本身（%s）：%s。合併之後才會生效。" % (when, names(sf)))
        touched = (st.get("tier2") or {}).get("touched") or {}
        for f in t2:
            h = diff_hash(wt, base, f)
            if touched.get(f, {}).get("ack") and touched.get(f, {}).get("hash") == h:
                continue
            acks[f] = h
        if kind == "ready" and acks:
            return fail("這個階段動到了要先給 David 看 diff 的檔（%s），而且他還沒看過現在這一版。請先寄「請看 diff」的信（--kind tier2），等他輸入「繼續 %s」。"
                        % ("、".join(sorted(acks)), stage))
        if kind == "tier2":
            if not acks:
                return fail("沒有需要給 David 看 diff 的檔（或他已經看過現在這一版）。")
            d = diff_text(wt, base, sorted(acks))
            dl = d.split("\n")
            cap = int(cfg["notify"]["diffMaxLines"])
            shown = "\n".join(dl[:cap])
            try:
                runs_dir = os.path.join(main_root, *(cfg["runsDir"].split("/") + [stage]))
                os.makedirs(runs_dir, exist_ok=True)
                with io.open(os.path.join(runs_dir, "tier2-%s.diff" % time.strftime("%Y%m%d-%H%M%S")), "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(d + "\n")
            except Exception:                                      # noqa: B902
                pass
            extra.append("要請你看的檔：%s" % "、".join(sorted(acks)))
            extra.append("<details><summary>diff（%d 行%s）</summary>\n\n```diff\n%s\n```\n\n</details>"
                         % (min(len(dl), cap), "，後面還有 %d 行沒列" % (len(dl) - cap) if len(dl) > cap else "", shown))
            extra.append("完整的 diff 在你電腦上：`.autopilot/runs/%s/`（分支還沒推上 GitHub——這類檔要你看過才推）。" % stage)
        if cfg.get("verify") and head_for_lines:
            vchanged, vfirst = R.verifier_changed(main_root, cfg, head_for_lines)
            if vchanged and not vfirst:                             # 這個 commit 改了驗收機本身：信裡寫明、附完整 diff（2026-10-05 裁決三）
                extra += verifier_change_extras(main_root, cfg, stage, wt, base, head_for_lines, vchanged, st)
    if kind == "ready":
        # 檢查程式看不到腳本裡面做了什麼；寄「可以合併」之前再看兩件事：主目錄沒被動過、保護檔跟放行過的版本一樣
        rc, dirty = C.git(["status", "--porcelain"], main_root)
        if rc != 0 or dirty.strip():
            return fail("主目錄（main 所在的資料夾）有沒 commit 的改動或多出來的檔：\n%s\n自動駕駛不應該動主目錄。"
                        "如果是筆電排程剛好在跑，等它跑完再寄一次。" % dirty.strip()[:400])
        import iw_events                                            # 放在這裡才不會互相 import
        problems = iw_events.integrity_problems(argparse.Namespace(main_root=main_root, sd=sd))
        if problems:
            return fail("保護檔的檢查沒過（跟放行過的版本不一樣）：%s。不能寄「可以合併」的信；請改寄停止報告（--kind stop），把這件事原樣告訴 David。"
                        % "；".join(problems[:4]))
        rc, head = C.git(["rev-parse", "HEAD"], wt)
        if rc != 0:
            return fail("讀不到 worktree 的 HEAD。")
        rc, dirty = C.git(["status", "--porcelain", "--untracked-files=no"], wt)
        if rc != 0 or dirty.strip():
            return fail("worktree 還有沒 commit 的改動，先 commit 再寄。")
        tag = cfg["tagPrefix"] + stage
        rc, tag_sha = C.git(["rev-parse", "-q", "--verify", "refs/tags/%s^{commit}" % tag], wt)
        if rc != 0:
            return fail("還沒有標籤 %s。" % tag)
        rc, _ = C.git(["merge-base", "--is-ancestor", tag_sha, head], wt)
        if rc != 0:
            return fail("標籤 %s 不在這個分支的歷史裡。" % tag)
        rc, branch = C.git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
        if cfg.get("verify") and cfg.get("externalReview"):
            foreign = R.foreign_commit_check(main_root, sd, cfg, stage, fetch=not a.offline)   # P2：有人動了 PR 的分支？先看這個，訊息才說得清楚
            if foreign:
                return fail("%s。不能寄「可以合併」的信；請改寄 --kind stop（等級「要你決定」），等 David 看過 PR。" % foreign)
        rc2, remote_head = C.git(["rev-parse", "-q", "--verify", "refs/remotes/%s/%s" % (cfg["remote"], branch)], wt)
        if rc != 0 or rc2 != 0 or remote_head != head:
            return fail("分支 %s 還沒推上去（或推上去的不是現在這個 commit）。David 要能在 GitHub 上看到才行。" % branch)
        ok_review = [r for r in (st.get("reviews") or []) if r.get("kind") == "acceptance" and r.get("verdict") == "APPROVE"
                     and r.get("commit") == head and r.get("stage") == stage and r.get("model_ok", True)]
        if not ok_review:
            return fail("沒有審查代理對這個 commit（%s）的批准紀錄。先跑驗收審查；紀錄是 hook 寫的，不能用轉述的（一般模式也一樣，沒有跳過的選項）。" % head[:7])
        if not st.get("active"):
            extra.append("注意：這一階段是一般模式（David 在場、不是自動駕駛跑的）；審查代理對這個 commit 的批准是 hook 記的。")
        # ---- 關卡（P2 第 3 節）：同一個 commit，驗收機綠、外部審查完成（Codex 或 David 免除）、每條意見有回覆、重大意見沒被判不採納；數字以驗收機為準
        if cfg.get("verify") and cfg.get("externalReview"):
            vs = R.verify_status(main_root, sd, cfg, stage, head)
            ext = R.external_status(main_root, sd, cfg, stage, head, st)
            waived = ext.get("mode") == "waived"
            _GATE_CACHE[(stage, head)] = (vs, ext)
            if not waived:
                if not vs.get("green"):
                    return fail("驗收機%s（%s）。不能寄「可以合併」的信：等驗收機綠了再寄；查不到也算不綠。" % ("還在跑" if vs.get("pending") else "不綠", vs.get("why")))
                if not ext.get("complete"):
                    mark_external_incomplete(sd, stage, head, ext)
                    return fail("外部審查還沒完成（%s）。程式已經記下「外部審查未完成」。請改寄 --kind stop（等級會是「要你決定」），信裡給 David 兩個選項："
                                "等（Codex 審完或額度恢復之後手打「繼續 %s」）、或手打「免外部審查 %s」。%s"
                                % (ext.get("why"), stage, stage,
                                   "這一段改到保護系統本身（第一層或自動駕駛自己的檔）：信裡要寫明這一點，並說等 Codex 比較妥當。" if (t1 or sf) else ""))
                # 2026-10-06 裁決第三節：完成＝最新的 commit 審過而且沒有 P0，加上這個 PR 上 Codex 提過的每一條都有回覆（不分哪個 commit）
                def ids(items):
                    return "、".join("留言 %s（%s）" % (f.get("id"), f.get("path") or "?") for f in items)
                left = "已經審了 %s 輪，上限 %d 輪" % (ext.get("rounds") or "?", ext.get("round_cap") or 0)
                if ext.get("p0_on_head"):
                    return fail("Codex 對最新的 commit（%s）提了 %d 條 P0：%s。P0 不能不採納，一定要修：修好、推上去、再請它審一次（%s；"
                                "到了上限還沒過就停下來寄 --kind stop，等級「要你決定」）。"
                                % (head[:7], len(ext["p0_on_head"]), ids(ext["p0_on_head"]), left))
                rows = R.parse_responses(os.path.join(runs_dir, cfg["externalReview"]["responsesFile"]), R.ruling_records(ST.load(sd), stage))
                missing, rejected, unruled = R.responses_problems(ext.get("findings") or [], rows)
                if missing:
                    return fail("Codex 的意見還有 %d 條沒有回覆（留言 id：%s）。這個 PR 上它提過的每一條都要回，不分是哪個 commit 的："
                                "在 .autopilot/runs/%s/%s 的表裡，回覆那一欄只能是「採納並修」或「不採納」（一字不差），"
                                "最後一欄的理由或修在哪不能空；同一條不能有兩種回覆。"
                                % (len(missing), "、".join(str(x) for x in missing), stage, cfg["externalReview"]["responsesFile"]))
                if rejected:
                    return fail("有 P0 被判「不採納」：%s。P0 不能不採納，一定要修。不能寄「可以合併」的信。" % ids(rejected))
                if unruled:
                    return fail("有 P1 被判「不採納」，但理由欄沒有指到 Cowork 的裁決檔：%s。P1 要不採納，理由欄只能寫「Cowork 裁決：<檔名>」或"
                                "「延後到 <階段>，Cowork 裁決：<檔名>」。<檔名>要是程式存的那一份（.autopilot/runs/%s/ 裡的「裁決-<時間>.md」）："
                                "David 第一行手打「裁決：%s」、下面貼 Cowork 寫的內容，程式存檔、核對過是人打的；內容沒有被改過，而且裁決裡要寫出這一條的留言編號。"
                                "自己放進資料夾的檔不算。沒有這樣的裁決就視為還沒回覆：請改寄 --kind stop（等級「要你決定」），把這幾條列成表，由 David 與 Cowork 裁決。"
                                % (ids(unruled), stage, stage))
                unfixed = R.unfixed_on_head(ext.get("findings") or [], rows)
                if unfixed:
                    return fail("有 %d 條針對最新 commit（%s）的意見，回覆寫的是「採納並修」：%s。但最新的 commit 就是被審的那一個——修的 commit 還沒推上來。"
                                "修好、推上去、再請 Codex 審一次（%s；到了上限還沒過就停下來寄 --kind stop）。真的不修，要照「不採納」的規矩寫。"
                                % (len(unfixed), head[:7], ids(unfixed), left))
            else:
                if not vs.get("green"):                                     # 免除只免外部審查：驗收機照樣要綠
                    return fail("David 免了外部審查，但驗收機%s（%s）。免除不包括驗收機，不能寄。" % ("還在跑" if vs.get("pending") else "不綠", vs.get("why")))
            local_ran = local_test_count(os.path.join(runs_dir, "tests.txt"))
            if local_ran is None:                                      # 沒有證據不能當成對得上（2026-10-05 Codex 的審查意見：原本缺檔就略過比對）
                return fail("找不到本機的測試紀錄，或讀不出條數：.autopilot/runs/%s/tests.txt 要有全套測試的輸出（「Ran N tests」那一行）。"
                            "本機的條數要跟驗收機的（%s）對得上才能寄「可以合併」的信。" % (stage, vs.get("ran")))
            local_problem = local_test_problem(os.path.join(runs_dir, "tests.txt"))
            if local_problem:                                          # 輔助的檢查：本機那一份要全綠、沒有 skipped（它不是具名字串掃描的證據，見下一段）
                return fail("%s。.autopilot/runs/%s/tests.txt 要是帶鹽跑完、最後一行是「OK」的全套測試輸出，才能寄「可以合併」的信。" % (local_problem, stage))
            # 具名字串：檢查程式自己掃這個階段改到的每一個檔，不靠上面那個文字檔（2026-10-06 Codex 的審查意見，P0）。
            # 驗收機上沒有鹽，這一種只有施工的這台電腦掃得了；免了外部審查也照掃。缺鹽、讀不到、命中，都不寄。
            named_problem = stage_named_term_problem(main_root, wt, cfg, head)
            if named_problem:
                return fail("%s。寄「可以合併」的信之前，程式會自己拿這個 commit（%s）跟正式版比、改到的每一個檔，用倉庫外的鹽比一次具名字串；沒過就不寄。"
                            % (named_problem, head[:7]))
            if local_ran != vs.get("ran"):
                return fail("本機的測試條數（%s，tests.txt）跟驗收機的（%s）對不上。數字以驗收機為準，對不上是「要你決定」：請改寄 --kind stop。" % (local_ran, vs.get("ran")))
            # PR 內文最後那一段「動到的保護範圍檔」是開 PR、改內文的那個當下列的；之後為了回應審查又加的 commit 可能動到別的保護檔
            # （2026-10-06 Codex 的審查意見：原本寄信前不看 PR 內文，舊清單漏了檔也照樣放行）。寄之前拿現在的 diff 再對一次。
            pr_now = ext.get("pr") if isinstance(ext.get("pr"), dict) else None
            touched = t1 + sf + t2
            if pr_now is None and touched:                             # 沒有 PR（例如找不到 PR、David 免了外部審查）：免除只免外部審查，保護範圍還是要揭露
                return fail("這個階段動到了保護範圍檔（%d 個：%s），但找不到這個分支的 PR。免除外部審查只免審查，保護範圍還是要寫在 PR 內文裡："
                            "請先用 iw_notify.py pr open 開 PR（程式會列出清單），再寄。"
                            % (len(touched), "、".join(touched[:8]) + ("……" if len(touched) > 8 else "")))
            if pr_now is not None:
                stale = protected_list_missing(pr_now.get("body"), t1 + sf + t2)
                if stale:
                    return fail("PR 內文最後那一段「動到的保護範圍檔」跟現在的 diff 對不上，漏了 %d 個：%s。開 PR 之後又動到新的保護檔時，"
                                "請用 iw_notify.py pr edit 重新送一次內文（程式會重新列清單），再寄。"
                                % (len(stale), "、".join(stale[:12]) + ("……" if len(stale) > 12 else "")))
        cand = {"sha": head, "tag_sha": tag_sha, "branch": branch, "registered_at": C.iso()}
        slug = github_slug(main_root, cfg)
        if slug:
            extra.append("改了哪些檔（GitHub）：https://github.com/%s/compare/%s...%s" % (slug, cfg["mainBranch"], branch))
    extra.append(gate_lines(main_root, sd, cfg, stage, head_for_lines, kind))
    extra = [x for x in extra if x]
    if not st.get("tripwire_baseline"):
        rc, tip = C.git(["rev-parse", "-q", "--verify", "refs/remotes/%s/%s" % (cfg["remote"], cfg["mainBranch"])], main_root)
        if rc == 0:
            st["tripwire_baseline"] = tip
    safety, findings = tripwire(main_root, sd, cfg, st, fetch=not a.offline)
    wf_line, wf_snap = "", None
    if cfg.get("verify"):                                           # 事後偵測：自上次停止信以來，有沒有任何分支新增或修改了流程檔（2026-10-05 裁決二）
        wf_line, wf_snap = R.workflow_watch(main_root, cfg, st, stage=stage, fetch=not a.offline)
    level, first_line = level_line(st, cfg, kind)                   # 等級由程式判（寄信之前的狀態），模型只能在內文補白話
    title = "[自動駕駛] %s：【%s】%s" % (stage, level, one_line(report) or KIND_NAMES[kind])
    body = "\n\n".join([first_line, report.strip()] + extra + [model_line(st, cfg) + "\n" + safety + ("\n" + wf_line if wf_line else "")])
    runs = os.path.join(main_root, *(cfg["runsDir"].split("/") + [stage]))
    try:
        os.makedirs(runs, exist_ok=True)
        with io.open(os.path.join(runs, "sent-%s-%s.md" % (time.strftime("%Y%m%d-%H%M%S"), kind)), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# %s\n\n%s\n" % (title, body))
    except Exception:                                              # noqa: B902
        pass
    ok, detail = deliver(main_root, cfg, title, body, stage, "open", runner)

    def apply(s):
        s["stage"] = stage
        s["notified_epoch"] = s.get("epoch")
        s["last_notification"] = {"kind": kind, "ok": ok, "at": C.iso(), "detail": detail[:200]}
        s.setdefault("tripwire_baseline", st.get("tripwire_baseline"))
        if findings:
            s["tripwire_findings"] = [f[0] for f in findings]
        if wf_snap is not None:
            s["workflow_snapshot"] = wf_snap                        # 下一封信跟這一份比
        if kind == "ready":
            s["candidate"] = cand
            s["status"] = "awaiting_approval"
            s["pause"] = None
            s["stop_required"] = None
            if s.get("credential"):
                s["credential"]["revoked"] = "重新寄了「可以合併」的信"
            if waived:
                ws = s.setdefault("waived_stages", [])
                if not ws or ws[-1] != stage:
                    ws.append(stage)
        else:
            if s.get("active") and not s.get("pause"):
                # 模型自己判斷要停（條件 2、4、5、6）而寄停止報告、或寄「請看 diff」：寄信的同時進暫停（P1-1 第 1 節）。status 不動。
                s["pause"] = {"reason": "stop-mail" if kind == "stop" else "tier2", "code": (s.get("stop_required") or {}).get("code"),
                              "detail": "停止報告已寄出，等 David 回覆" if kind == "stop" else "「請看 diff」的信已寄出，等 David 看過", "at": C.iso(), "retries": 0}
            if kind == "tier2":
                t = s.setdefault("tier2", {}).setdefault("touched", {})
                for f, h in acks.items():
                    t[f] = {"at": C.iso(), "ack": False, "hash": h, "reported": True}
    ST.update(sd, apply)
    ST.log(sd, {"event": "notify", "kind": kind, "stage": stage, "ok": ok, "detail": detail[:200]})
    if ok:
        say("信已經寄出：%s\n（%s）\n現在請結束這一輪，等 David 回覆。" % (title, detail))
        return 0
    say("信沒有寄出去：%s\n這件事要照實寫進給 David 的回覆裡。現在請結束這一輪。" % detail)
    return 1


# ---------------------------------------------------------------- 關卡（P2）：驗收機、外部審查、PR 的三種寫入

_GATE_CACHE = {}                               # (階段, commit) → (驗收機狀態, 外部審查狀態)：同一次寄信不重查


def verifier_change_extras(main_root, cfg, stage, wt, base, head, files, st):
    """這個 commit 改了驗收機本身（verify.yml、驗收程式、突變執行器、例外清單）：信裡寫明，並附上改動的完整 diff。
    完整的 diff 另外存一份在報告資料夾；信裡放得下就全放（GitHub 的 issue 內文有長度上限，超過時照實說後面還有多少）。"""
    approved = R.verifier_change_problem(st, cfg, stage, head, check_transcript=False) is None
    out = ["⚠ 這一段改的是驗收機本身（%s）。%s" % (
        "、".join(files),
        "David 已經手打「驗收機變更 %s」同意這個 commit 用驗收機的結果。" % stage if approved
        else "沒有 David 在一般模式手打的「驗收機變更 %s」，這個 commit 的驗收機結果不算數。" % stage)]
    # 跟「現在 main 上的驗收機」直接比（兩點，不是從分岔點算）：關卡比的就是這兩邊的 blob，信裡給 David 看的也要是同一件事
    rc, d = C.git(["diff", base, head, "--"] + list(files), wt, timeout=60)
    if rc != 0 or not d.strip():
        d = diff_text(wt, base, files)
    saved = ""
    try:
        runs_dir = os.path.join(main_root, *(cfg["runsDir"].split("/") + [stage]))
        os.makedirs(runs_dir, exist_ok=True)
        name = "verifier-change-%s.diff" % head[:12]
        with io.open(os.path.join(runs_dir, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(d + "\n")
        saved = "完整的 diff 也存在你電腦上：`%s/%s/%s`。" % (cfg["runsDir"], stage, name)
    except Exception:                                              # noqa: B902
        pass
    cap = int(cfg["notify"].get("verifierDiffMaxChars", 40000))
    shown = d if len(d) <= cap else d[:cap]
    out.append("<details><summary>驗收機改動的完整 diff（%d 行%s）</summary>\n\n```diff\n%s\n```\n\n</details>"
               % (len(d.split("\n")), "" if len(d) <= cap else "；信裡放不下，只列前 %d 個字，後面還有 %d 個字" % (cap, len(d) - cap), shown))
    if saved:
        out.append(saved)
    return out


PROTECTED_HEAD = "**動到的保護範圍檔**（程式列的）"


def protected_list_missing(body, files):
    """PR 內文最後那一段（程式列的保護範圍檔）有沒有涵蓋 files 裡的每一個檔。回傳漏掉的檔（那一段不在＝全部都算漏）。"""
    body = body or ""
    i = body.rfind(PROTECTED_HEAD)
    section = body[i:] if i >= 0 else ""
    return [f for f in files if ("`%s`" % f) not in section]


def protected_section(main_root, cfg, stage):
    """PR 內文最後由程式加的一段：這個分支動到的保護範圍檔（AGENTS.md 的審查規則要求在 PR 內文標示；由程式列，不靠記得）。"""
    head = PROTECTED_HEAD
    wt = stage_worktree(main_root, cfg, stage)
    if not os.path.isdir(wt):
        return head + "：讀不到這個階段的 worktree，沒有列。"
    try:
        t1, sf, t2, _all = tier_files(wt, cfg, "%s/%s" % (cfg["remote"], cfg["mainBranch"]))
    except Exception:                                              # noqa: B902
        return head + "：查不出這個分支動了哪些檔，沒有列。"
    if not (t1 or sf or t2):
        return head + "：沒有。"
    lines = [head]
    for label, files in (("第一層（排程、流程檔、資料來源、隱私、驗收機；平常一律不能動）", t1), ("自動駕駛自己的檔（保護程式）", sf),
                         ("第二層（要先給倉庫主人看 diff）", t2)):
        if files:
            lines.append("- %s：%s" % (label, "、".join("`%s`" % f for f in files)))
    return "\n".join(lines)


def local_test_count(path):
    """本機 tests.txt 裡「Ran N tests」的 N（最後一個）。沒有檔回 None。"""
    try:
        text = io.open(path, encoding="utf-8", errors="replace").read()
    except Exception:                                              # noqa: B902
        return None
    found = re.findall(r"^Ran (\d+) tests?", text, re.M)
    return int(found[-1]) if found else None


def local_test_problem(path):
    """本機 tests.txt 的最後結論是不是「全綠、沒有 skipped」。是回 None；不是回一句原因。
    驗收機上沒有鹽，具名字串那幾條在那一邊是 skipped；本機這一份有 skipped，多半就是沒有帶鹽跑。
    這是輔助的檢查：這個檔是施工的一方自己存的，沒有綁住是哪一個 commit 跑的，不能當成具名字串掃過的證據（2026-10-06 Codex 的審查意見）。
    具名字串由檢查程式自己掃：推送之前 iw_prepush.file_content_problem，寄 ready 之前 stage_named_term_problem。"""
    try:
        text = io.open(path, encoding="utf-8", errors="replace").read()
    except Exception:                                              # noqa: B902
        return "讀不到本機的測試紀錄"
    tail = text[text.rfind("\nRan "):] if "\nRan " in text else (text if text.startswith("Ran ") else "")
    lines = [x.strip() for x in tail.split("\n") if x.strip()]
    verdict = lines[1] if len(lines) > 1 else ""
    if verdict == "OK":
        return None
    m = re.match(r"^OK \((.*)\)$", verdict)
    if m:
        return "本機的全套測試不是完整的全綠（%s）：有 skipped 多半是沒有帶鹽跑，具名字串那幾條就沒有掃到。請在有鹽的電腦上重跑全套" % m.group(1)[:60]
    return "本機的全套測試不是全綠（最後一行是「%s」）" % verdict[:60]


def mark_external_incomplete(sd, stage, head, ext):
    """程式記下「外部審查未完成」：David 手打「免外部審查 <階段>」的前提。"""
    rec = {"stage": stage, "sha": head, "status": "incomplete", "why": (ext or {}).get("why"), "pr": (ext or {}).get("pr_url"), "at": C.iso()}

    def fn(s):
        s["external_review"] = rec
    ST.update(sd, fn)
    ST.log(sd, {"event": "external-incomplete", "stage": stage, "sha": (head or "")[:12], "why": rec["why"]})
    return rec


def verify_gate_problem(main_root, sd, cfg, stage, sha, now=None):
    """放行時再查一次驗收機（P2 第 3 節）。綠回 None；不綠或查不到回原因。設定裡沒有驗收機（舊版）就不查。"""
    if not (cfg.get("verify") and cfg.get("externalReview")):
        return None
    try:
        vs = R.verify_status(main_root, sd, cfg, stage, sha, now=now)
    except Exception as e:                                         # noqa: B902
        return "查驗收機時出錯（%r）" % (e,)
    return None if vs.get("green") else vs.get("why")


def gate_lines(main_root, sd, cfg, stage, head, kind):
    """停止信固定多兩行（P2 第 3 節）：「驗收機：…」「外部審查（GPT）：…」。程式寫，Claude 改不了。"""
    if not (cfg.get("verify") and cfg.get("externalReview")):
        return ""
    if not head:
        return "驗收機：沒有紀錄（這個階段還沒有 worktree 或 commit）\n外部審查（GPT）：沒有紀錄"
    vs, ext = _GATE_CACHE.get((stage, head)) or (None, None)
    st = ST.load(sd)
    try:
        if vs is None:
            vs = R.verify_status(main_root, sd, cfg, stage, head)
        if ext is None:
            ext = R.external_status(main_root, sd, cfg, stage, head, st)
    except Exception as e:                                         # noqa: B902
        return "驗收機：查不到（%r）\n外部審查（GPT）：查不到" % (e,)
    rows = R.parse_responses(os.path.join(main_root, *(cfg["runsDir"].split("/") + [stage, cfg["externalReview"]["responsesFile"]])), R.ruling_records(st, stage))
    lines = [R.gate_line_verify(vs), R.gate_line_external(ext, rows)]
    ws = st.get("waived_stages") or []
    if ext.get("mode") == "waived" and ws and ws[-1] != stage:
        lines.append("提醒：連續兩個階段（%s、%s）都免了外部審查，請 Codex 帳號的持有人檢查 Codex 的設定。" % (ws[-1], stage))
    return "\n".join(lines)


# 公開文字（PR 的標題內文、commit 訊息、標籤訊息）裡的本機絕對路徑：這幾種文字只該出現相對於倉庫的路徑，所以任何絕對路徑的寫法都擋。
# 2026-10-06 Codex 的審查意見（P0）：原本只認「磁碟機:\Users\…」與專案資料夾兩種，/home/某人、/Users/某人、別的磁碟機路徑都掃不到。
_PR_LOCAL = (
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"),                                         # 磁碟機代號開頭：C:\…、D:/…（網址的 https:// 不算）
    re.compile(r"(?<![\w.:/~-])/(?:home|Users|root|mnt|media|Volumes)/[^\s/]"),            # Unix／macOS 的家目錄與掛載點
    re.compile(r"(?<![\w.:/~-])/[A-Za-z]/[^\s/]"),                                          # Git Bash 的寫法：/c/…、/d/…
    re.compile(r"(?<![\w/.])~[\\/]"),                                                       # 家目錄的縮寫：~/…
    re.compile(r"\\\\[A-Za-z0-9_.$-]+\\[A-Za-z0-9_.$-]"),                                   # 網路磁碟：\\主機\分享
    re.compile(r"(?i)\bfile:/"),                                                            # file:// 網址
)
# 公開文字裡像密鑰的東西。2026-10-06 Codex 的審查意見（P0）：原本只認兩種 GitHub 權杖的開頭，別家的金鑰、JWT、私鑰區塊、
# 「password＝值」這類寫法都掃不到。SECRET_PATTERNS 是格式很明確的那幾種（驗收機那一份 SECRET_PATTERNS 要跟這裡一樣，有測試釘住）；
# _PR_KEY_VALUE 是「敏感的鍵名後面直接接一個值」，只用在公開文字（PR 內文、commit 訊息、標籤訊息）——那幾種文字本來就不該有這種寫法。
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
_PR_SECRETS = tuple(re.compile(p) for p in SECRET_PATTERNS)
_PR_KEY_VALUE = (
    re.compile(r"(?i)(?<![A-Za-z0-9])(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)\s*[=:]\s*[^\s，。、；）)\]】」]{6,}"),
    re.compile(r"(?:密碼|權杖|金鑰|密鑰)\s*[=:：]\s*[A-Za-z0-9_+/=.\-]{6,}"),
)
_PR_MAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 公開文字裡可以出現的電子郵件：只有這幾個確切的系統地址，寫在這一個地方（驗收機那一份 SYSTEM_MAIL_* 要跟這裡一樣，有測試釘住）。
# 2026-10-06 Codex 的審查意見（P0）：原本寫成「地址裡有 noreply 就放行」，某人+noreply@公司 這種私人信箱也過；@github.com 整個網域也放行。
PUBLIC_MAIL_EXACT = ("noreply@anthropic.com", "noreply@github.com", "git@github.com")
PUBLIC_MAIL_DOMAINS = ("users.noreply.github.com",)                # GitHub 給每個帳號的匿名地址：<數字>+<帳號>@users.noreply.github.com


def public_mail_ok(addr):
    """這個電子郵件地址可不可以出現在公開文字裡：只有清單上確切的系統地址可以。"""
    a = (addr or "").strip().lower()
    return a in PUBLIC_MAIL_EXACT or a.rsplit("@", 1)[-1] in PUBLIC_MAIL_DOMAINS


def pr_text_problems(main_root, title, body):
    """PR 的標題與內文要過隱私掃描才送出（公開倉庫的 PR 人人看得到）。掃描器讀不到就當成沒過（寧可擋）。"""
    problems = ["標題是空的"] if not (title or "").strip() else []
    return problems + text_privacy_problems(main_root, "%s\n%s" % (title or "", body or ""))


_GUARDS_CACHE = {}
NO_SALT = ("具名字串的鹽讀不到：這一種掃不了，不能送出。鹽放在倉庫外，只有施工用的那台電腦有；"
           "請停下來寄 --kind stop，信裡寫明鹽不在（這是小事，補上鹽就可以繼續）")


def text_privacy_problems(main_root, text):
    """一段要公開的文字（PR 的標題內文、commit 訊息、標籤訊息）有沒有不該公開的東西。回傳問題的類別（不帶命中的字本身）。
    掃描器（主目錄上的 scripts/test_analysis_guards.py＝main 的版本）讀不到就當成有問題（寧可擋）。"""
    text = text or ""
    problems = []
    if any(rx.search(text) for rx in _PR_LOCAL):
        problems.append("有本機的絕對路徑")
    if any(rx.search(text) for rx in _PR_SECRETS):
        problems.append("有像權杖的字串")
    if any(rx.search(text) for rx in _PR_KEY_VALUE):
        problems.append("有像密碼或密鑰的寫法（敏感的鍵名後面直接接了值）")
    for m in _PR_MAIL.findall(text):
        if not public_mail_ok(m):
            problems.append("有電子郵件地址")
            break
    if "<pasted_content" in text or "pasted_content>" in text:
        problems.append("內文含貼上的區塊標籤（規格原文不放進 PR）")
    try:
        mod = guards_module(main_root)
        hits = list(mod.privacy_hits(text)) + list(mod.profile_key_hits(text)) + list(mod.fxplan_key_hits(text))
        if hits:
            problems.append("隱私掃描命中（個人資料的字樣或設定鍵名，%d 處）" % len(hits))      # 只說有幾處：這句話會進紀錄檔
        # 具名字串（私人清單上的名稱；倉庫裡只有加鹽的雜湊，鹽放在倉庫外）。2026-10-06 Codex 的審查意見（P0）：公開文字原本完全沒比這一種——
        # 清單上的名稱只要不帶通用的隱私字樣就過了。鹽或雜湊清單讀不到＝這一種掃不了＝不能送出（Cowork 的裁決：缺鹽就擋；
        # 施工一律在有鹽的那台電腦上做。驗收機不放鹽；檔案內容裡的具名字串由檢查程式自己帶鹽掃，見下面 named_term_blob_problem）。
        salt, entries = mod.load_salt(), mod.load_digests()
        if not salt:
            problems.append(NO_SALT)
        elif not entries:
            problems.append("具名字串的雜湊清單讀不到或是空的：這一種掃不了，不能送出")
        elif mod.named_term_hits(text, salt, entries):
            problems.append("具名字串命中（私人清單上的名稱）")
    except Exception as e:                                         # noqa: B902
        problems.append("隱私掃描器讀不到（%r）" % (e,))
    return problems


def guards_module(main_root):
    """主目錄上的隱私掃描器（scripts/test_analysis_guards.py＝main 的版本）。讀不到就丟例外：呼叫的人要當成「掃不了」（寧可擋）。"""
    p = os.path.join(main_root, "scripts", "test_analysis_guards.py")
    key = (p, os.path.getmtime(p))                                  # 檔不在＝這裡就出錯＝讀不到（擋）
    mod = _GUARDS_CACHE.get(key)
    if mod is None:                                                 # 一次推送可能要掃幾十筆 commit 訊息、幾十個檔：同一份掃描器只載入一次
        import importlib.util
        spec = importlib.util.spec_from_file_location("iw_pr_guards", p)
        mod = importlib.util.module_from_spec(spec)
        # 載入的時候不可以在主目錄留下 scripts/__pycache__/：主目錄要保持乾淨（寄信前會查），而且檢查程式本來就不該寫主目錄。
        # 2026-10-06 驗收機抓到的：寄 ready 之前多載入一次掃描器之後，下一次寄信就被「主目錄多出來的檔」擋下（本機的環境關掉了寫快取，測不出來）。
        saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.dont_write_bytecode = saved
        _GUARDS_CACHE[key] = mod
    return mod


# ---------------------------------------------------------------- 具名字串：檢查程式自己掃檔案內容
# 2026-10-06 Codex 的審查意見（P0）：檔案內容裡的具名字串只有全套測試會掃，而驗收機上沒有鹽、那幾條永遠是 skipped；
# 寄信前核對的「本機測試紀錄全綠、沒有 skipped」讀的是施工的一方自己存的文字檔，沒有綁住是哪一個 commit 跑的，舊檔或改寫過的檔都會過。
# Cowork 的裁決：不靠那個文字檔。由檢查程式自己拿 commit 裡的內容，用主目錄上的掃描器與倉庫外的鹽當場比——
# 推送之前比「每一筆要推的 commit 改到的檔」（iw_prepush.file_content_problem），寄 ready 之前再比「這個階段跟正式版比、改到的每一個檔」。
# 證據是當場從 commit 算出來的，沒有可以留舊的、可以改寫的檔。鹽、雜湊清單、掃描器、任何一個檔讀不到，一律當成掃不了（擋）。
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"              # git 的空樹：第一筆 commit 沒有 parent，拿它當比較的對象


def changed_blobs(repo, revs):
    """git diff <revs> 新增或改過的檔：[(內容編號, 路徑)]。查不出來、讀不懂回 None（呼叫的人要擋）。
    刪掉的檔不算（沒有內容要公開）；子模組的指標（160000）不是檔案內容，也不算。"""
    rc, out = C.git(["diff", "--raw", "-z", "--no-renames", "--no-abbrev", "--diff-filter=ACMRT"] + list(revs) + ["--"], repo, timeout=60)
    if rc != 0:
        return None
    toks = [t for t in out.split("\0") if t]
    if len(toks) % 2:
        return None
    res = []
    for i in range(0, len(toks), 2):
        meta, path = toks[i].split(), toks[i + 1]
        if len(meta) != 5 or not meta[0].startswith(":") or not re.match(r"^[0-9a-f]{40,64}$", meta[3]):
            return None
        if meta[1] != "160000":
            res.append((meta[3], path))
    return res


def named_term_blob_problem(main_root, repo, blobs):
    """blobs＝[(內容編號, 路徑)]。把每一個檔在 commit 裡的內容讀出來，連同檔名，比一次具名字串。都沒有回 None；不能放行回一句原因。
    回報只說是哪幾個檔，不帶命中的字本身；檔名自己命中的不列檔名。資料檔（data/ 底下的 JSON）跟全套測試一樣只掃鍵名與字串值。"""
    try:
        mod = guards_module(main_root)
        salt, entries = mod.load_salt(), mod.load_digests()
    except Exception as e:                                         # noqa: B902
        return "隱私掃描器讀不到（%r），檔案內容裡的具名字串掃不了，先擋下。" % (e,)
    if not salt:
        return NO_SALT
    if not entries:
        return "具名字串的雜湊清單讀不到或是空的：這一種掃不了，不能送出"
    strings_only = getattr(mod, "strings_only", None)
    seen, in_text, in_name = set(), [], 0
    for blob, path in blobs or []:
        if (blob, path) in seen:
            continue
        seen.add((blob, path))
        rc, text = C.git(["cat-file", "blob", blob], repo, timeout=60)
        if rc != 0:
            return "讀不到 %s 在 commit 裡的內容（%s），具名字串掃不了，先擋下。" % (path, blob[:7])
        if strings_only and path.startswith("data/") and path.endswith(".json"):
            text = strings_only(text)
        if mod.named_term_hits(path, salt, entries):
            in_name += 1                                            # 檔名本身命中：不列檔名（列了就等於把那個名稱寫出來）
            continue
        if mod.named_term_hits(text, salt, entries):
            in_text.append(path)
    if not (in_text or in_name):
        return None
    parts = []
    if in_text:
        parts.append("內容命中的檔 %d 個：%s" % (len(in_text), "、".join(sorted(set(in_text))[:8]) + ("……" if len(set(in_text)) > 8 else "")))
    if in_name:
        parts.append("檔名本身命中的 %d 個（不列檔名）" % in_name)
    return "檔案裡有具名字串（私人清單上的名稱）——%s" % "；".join(parts)


def stage_named_term_problem(main_root, repo, cfg, head):
    """寄 ready 之前：這個階段跟正式版比、改到的每一個檔（head 那個 commit 裡的內容），比一次具名字串。沒有回 None；不能寄回一句原因。"""
    base = "%s/%s" % (cfg["remote"], cfg["mainBranch"])
    blobs = changed_blobs(repo, ["%s...%s" % (base, head)])
    if blobs is None:
        return "查不出這個階段（%s）跟 %s 比改了哪些檔，檔案內容裡的具名字串掃不了" % (str(head)[:7], base)
    return named_term_blob_problem(main_root, repo, blobs)


def cmd_pr(a, main_root, sd, cfg):
    """PR 唯三的寫入：open（開）、edit（改標題與內文）、request-review（留言剛好是 @codex review）。其他對 PR 的寫入守門一律擋。"""
    ext = cfg.get("externalReview") or {}
    stage = a.stage
    if not C.stage_ok(stage):
        return fail("階段名稱不對：%s" % stage)
    if not ext:
        return fail("設定裡沒有外部審查（externalReview）。")
    st = ST.load(sd)
    if st.get("active") and st.get("stage") != stage:
        return fail("現在自動駕駛的階段是 %s，不是 %s。" % (st.get("stage"), stage))
    branch = cfg["branchPrefix"] + stage
    slug = R.slug(main_root, cfg)
    if not slug:
        return fail("看不出這個倉庫在 GitHub 的名字。")
    wt = stage_worktree(main_root, cfg, stage)
    rc, head = C.git(["rev-parse", "HEAD"], wt) if os.path.isdir(wt) else (1, "")
    head = head.strip() if rc == 0 else None
    if a.action == "request-review":
        body = ext["trigger"]
        pr, err = R.find_pr(main_root, cfg, branch)
        if pr is None:
            return fail("找不到這個分支的 PR：%s" % err)
        # 輪數上限（2026-10-06 裁決第三節）：開 PR 時自動審的那一次算第 1 輪，之後每留一則算一輪。到了上限就不再留言；上限不是自動放行。
        cap = R.round_cap(cfg, stage)
        said, err = R.gh_pages("repos/%s/issues/%s/comments" % (slug, pr["number"]), hints={"sha": head or ""})
        if said is None:
            return fail("查不到 PR 的留言（%s），算不出已經審了幾輪：查不到就不留言。" % err)
        used = 1 + len([x for x in said if (x.get("body") or "").strip() == body])
        if used >= cap:
            return fail("這個 PR 已經請 Codex 審了 %d 輪，這個階段的上限是 %d 輪，不再留言。到了上限還沒達成完成條件："
                        "停下來寄 --kind stop（等級「要你決定」），把剩下還沒解決的意見列成表，由 David 決定。上限不是自動放行。" % (used, cap))
        rc, out = R.gh(["pr", "comment", str(pr["number"]), "-R", slug, "--body", body])
        if rc != 0:
            return fail("留言沒有送出：%s" % out)

        def fn(s):
            s["external_review"] = {"stage": stage, "status": "requested", "pr": pr.get("url"), "sha": head, "at": C.iso()}
        ST.update(sd, fn)
        ST.log(sd, {"event": "pr", "action": "request-review", "stage": stage, "pr": pr.get("number"), "round": used + 1, "cap": cap})
        say("已在 PR #%s 留言「%s」（%s）。這是第 %d 輪審查，這個階段的上限是 %d 輪。" % (pr["number"], body, pr.get("url"), used + 1, cap))
        return 0
    title = (a.title or "").strip()
    try:
        body = io.open(a.body_file, encoding="utf-8").read() if a.body_file else ""
    except Exception as e:                                         # noqa: B902
        return fail("讀不到內文的檔：%r" % (e,))
    body = body.rstrip() + "\n\n" + protected_section(main_root, cfg, stage) + "\n"
    problems = pr_text_problems(main_root, title, body)
    if problems:
        return fail("PR 的文字沒過隱私掃描，沒有送出：\n- " + "\n- ".join(problems))
    if a.action == "open":
        rc, out = R.gh(["pr", "create", "-R", slug, "--base", cfg["mainBranch"], "--head", branch, "--title", title, "--body", body], timeout=60)
        if rc != 0:
            return fail("開 PR 沒有成功：%s" % out)
        url = out.strip().split("\n")[-1] if out.strip() else ""

        def fn(s):
            s.setdefault("pull_requests", {})[stage] = {"url": url, "branch": branch, "opened_at": C.iso(), "sha": head}
        ST.update(sd, fn)
        ST.log(sd, {"event": "pr", "action": "open", "stage": stage, "url": url})
        say("已開 PR：%s（%s → %s）。" % (url or "（gh 沒有印網址）", branch, cfg["mainBranch"]))
        return 0
    pr, err = R.find_pr(main_root, cfg, branch)
    if pr is None:
        return fail("找不到這個分支的 PR：%s" % err)
    rc, out = R.gh(["pr", "edit", str(pr["number"]), "-R", slug, "--title", title, "--body", body], timeout=60)
    if rc != 0:
        return fail("改 PR 沒有成功：%s" % out)
    ST.log(sd, {"event": "pr", "action": "edit", "stage": stage, "pr": pr.get("number")})
    say("已改 PR #%s 的標題與內文。" % pr["number"])
    return 0


def _head_of_stage(main_root, cfg, stage, commit=None):
    if commit:
        return commit
    wt = stage_worktree(main_root, cfg, stage)
    rc, head = C.git(["rev-parse", "HEAD"], wt) if os.path.isdir(wt) else (1, "")
    return head.strip() if rc == 0 else None


def cmd_verify(a, main_root, sd, cfg):
    """驗收機對這個 commit 綠不綠（唯讀）。--wait：每 pollSeconds 查一次，直到有結果或超過 timeoutMinutes。結束碼 0 綠、1 紅或查不到、2 等不到。"""
    if not C.stage_ok(a.stage):
        return fail("階段名稱不對：%s" % a.stage)
    v = cfg.get("verify") or {}
    if not v:
        return fail("設定裡沒有驗收機（verify）。")
    head = _head_of_stage(main_root, cfg, a.stage, a.commit)
    if not head:
        return fail("看不出要查哪個 commit（沒有 worktree，也沒有 --commit）。")
    foreign = R.foreign_commit_check(main_root, sd, cfg, a.stage)
    if foreign:
        return fail(foreign + "。停下來：寫停止報告、寄 --kind stop，等 David 看過 PR。")
    deadline = time.time() + 60.0 * float(v.get("timeoutMinutes", 40)) if a.wait else 0
    while True:
        vs = R.verify_status(main_root, sd, cfg, a.stage, head)
        if vs.get("green") or not vs.get("pending") or time.time() >= deadline:
            break
        say("驗收機還在跑（%s），%d 秒後再查…" % (vs.get("url") or "", int(v.get("pollSeconds", 60))))
        time.sleep(float(v.get("pollSeconds", 60)))
    import json
    say(json.dumps({"commit": head, "green": vs.get("green"), "pending": vs.get("pending"), "why": vs.get("why"), "url": vs.get("url"),
                    "ran": vs.get("ran"), "verifier_changed": vs.get("verifier_changed"), "changed_files": vs.get("changed_files"),
                    "verifier_change_approved": vs.get("verifier_change_approved")}, ensure_ascii=False, indent=1))
    if vs.get("green"):
        return 0
    return 2 if vs.get("pending") else 1


def cmd_review_status(a, main_root, sd, cfg):
    """外部審查（Codex）完成了沒（唯讀）。--wait：等到完成或超過 externalReview.timeoutMinutes；逾時就記下「外部審查未完成」。"""
    if not C.stage_ok(a.stage):
        return fail("階段名稱不對：%s" % a.stage)
    ext_cfg = cfg.get("externalReview") or {}
    if not ext_cfg:
        return fail("設定裡沒有外部審查（externalReview）。")
    head = _head_of_stage(main_root, cfg, a.stage, a.commit)
    if not head:
        return fail("看不出要查哪個 commit（沒有 worktree，也沒有 --commit）。")
    foreign = R.foreign_commit_check(main_root, sd, cfg, a.stage)
    if foreign:
        return fail(foreign + "。停下來：寫停止報告、寄 --kind stop，等 David 看過 PR。")
    deadline = time.time() + 60.0 * float(ext_cfg.get("timeoutMinutes", 60)) if a.wait else 0
    while True:
        st = ST.load(sd)
        ext = R.external_status(main_root, sd, cfg, a.stage, head, st)
        if ext.get("complete") or time.time() >= deadline:
            break
        say("Codex 還沒審完（%s），%d 秒後再查…" % (ext.get("why"), int((cfg.get("verify") or {}).get("pollSeconds", 60))))
        time.sleep(float((cfg.get("verify") or {}).get("pollSeconds", 60)))
    if not ext.get("complete") and (a.wait or a.mark_incomplete):
        mark_external_incomplete(sd, a.stage, head, ext)
        say("已記下「外部審查未完成」（%s）。David 可以手打「免外部審查 %s」，或等 Codex 審完再「繼續 %s」。" % (ext.get("why"), a.stage, a.stage))
    import json
    say(json.dumps({"commit": head, "mode": ext.get("mode"), "complete": ext.get("complete"), "why": ext.get("why"), "pr": ext.get("pr_url"),
                    "findings": ext.get("findings"), "major": len(ext.get("major") or []), "others": ext.get("others"), "reviews": ext.get("reviews")},
                   ensure_ascii=False, indent=1))
    return 0 if ext.get("complete") else 1


def cmd_close(a, main_root, sd, cfg, runner=None):
    st = ST.load(sd)
    body = "已放行，合併完成。%s" % ("合併 commit：%s" % (st.get("credential") or {}).get("merge", {}).get("sha", "")[:7]
                                    if (st.get("credential") or {}).get("merge") else "")
    ok, detail = deliver(main_root, cfg, "[自動駕駛] %s：已放行" % a.stage, body, a.stage, "close", runner)
    ST.log(sd, {"event": "notify", "kind": "close", "stage": a.stage, "ok": ok, "detail": detail[:200]})
    say(("已經請 GitHub 把那一封的 issue 留言並關掉。（%s）" if ok else "關 issue 沒有成功：%s") % detail)
    return 0 if ok else 1


def fallback(main_root, sd, cfg, text, stage=None, runner=None, min_gap_key=None):
    """hook 用的後援信：一句話。min_gap_key：同一種原因在幾分鐘內不重寄。"""
    try:
        st = ST.load(sd)
    except Exception:                                              # noqa: B902
        st = {}
    stage = stage or st.get("stage") or "?"
    if min_gap_key:
        last = C.parse_iso(((st.get("fallbacks") or {}).get(min_gap_key)))
        if last is not None and (C.now() - last).total_seconds() < 60 * float(cfg["notify"]["minMinutesBetweenFallbacks"]):
            return True, "剛寄過同一種，這次不重寄"
    level, first_line = level_line(st, cfg)
    title = "[自動駕駛] %s：【%s】%s" % (stage, level, text.split("\n")[0][:60])
    body = "%s\n\n%s\n\n（這是程式自動寄的短信，時間 %s。詳細情況請看電腦上的 Claude Code。）\n\n%s" % (first_line, text, C.local_stamp(), model_line(st, cfg))
    ok, detail = deliver(main_root, cfg, title, body, stage, "open", runner)

    def apply(s):
        s.setdefault("fallbacks", {})[min_gap_key or "x"] = C.iso()
        s["notified_epoch"] = s.get("epoch")
        s["last_notification"] = {"kind": "fallback", "ok": ok, "at": C.iso(), "detail": detail[:200]}
    try:
        ST.update(sd, apply)
    except Exception:                                              # noqa: B902
        pass
    ST.log(sd, {"event": "notify", "kind": "fallback", "stage": stage, "ok": ok, "text": text[:120], "detail": detail[:200]})
    return ok, detail


def cmd_finish_docs(a, main_root, sd, cfg):
    """把一個早先的階段結案：它的文件（回滾表、合併紀錄）併進了另一個階段的那一筆文件 commit（P1 的回滾表併進 P1-1 就是這樣）。
    只准在「併入的那個階段」文件那一筆已經落地之後執行；之後對它輸入「放行 <階段>」不會再開任何時間窗。"""
    st = ST.load(sd)
    if not C.stage_ok(a.stage) or not C.stage_ok(a.merged_into) or a.stage == a.merged_into:
        return fail("階段名稱不對：%s → %s" % (a.stage, a.merged_into))
    if st.get("stage") != a.merged_into:
        return fail("現在紀錄裡的階段是 %s，不是 %s；只有併入的那個階段在紀錄裡時才能結案。" % (st.get("stage"), a.merged_into))
    docs = ((st.get("credential") or {}).get("docs") or {}).get("sha")
    if not docs:
        return fail("%s 的文件那一筆還沒有推上 main，不能把 %s 結案。" % (a.merged_into, a.stage))
    note = "文件併入 %s（%s）" % (a.merged_into, docs[:7])
    now = C.iso()

    def fn(s):
        s.setdefault("closed_stages", {})[a.stage] = {"at": now, "note": note, "docs": docs, "merged_into": a.merged_into}
    ST.update(sd, fn)
    ST.append_approval(sd, {"stage": a.stage, "docs": docs, "note": note})
    ST.log(sd, {"event": "finish-docs", "stage": a.stage, "merged_into": a.merged_into, "docs": docs})
    say("已結案：%s（%s）。之後輸入「放行 %s」不會再開任何時間窗。" % (a.stage, note, a.stage))
    return 0


def cmd_status(a, main_root, sd, cfg):
    st = ST.load(sd)
    keep = dict((k, st.get(k)) for k in ("active", "stage", "status", "epoch", "session_id", "started_at", "clock_started_at", "stop_required",
                                         "pause", "candidate", "model_violation", "model_approved", "models_seen", "effort_seen",
                                         "notified_epoch", "last_notification", "tripwire_baseline", "closed_stages") if k in st)
    keep["protectionVersion"] = cfg.get("protectionVersion")
    keep["paused"] = ST.is_paused(st)
    keep["merged_awaiting_docs"] = ST.merged_awaiting_docs(st)
    keep["rulings"] = [(r.get("stage"), r.get("at"), r.get("path"), "forged" if r.get("forged") else ("verified" if r.get("verified") else "pending"))
                       for r in (st.get("rulings") or [])]
    keep["reviews"] = [(r.get("kind"), r.get("verdict"), (r.get("commit") or "")[:7], r.get("epoch")) for r in (st.get("reviews") or [])]
    keep["tier2"] = (st.get("tier2") or {}).get("touched")
    keep["retries"] = (st.get("pause") or {}).get("retries") if st.get("pause") else None      # 暫停中又被擋了幾次（P2 第 7 節：要印出來）
    keep["external_review"] = st.get("external_review")
    keep["waived_stages"] = st.get("waived_stages")
    keep["effort_violation"] = st.get("effort_violation")
    keep["effort_approved"] = st.get("effort_approved")
    keep["foreign_commit"] = st.get("foreign_commit")
    vc = st.get("verifier_change")
    if vc:
        keep["verifier_change"] = dict((k, vc.get(k)) for k in ("stage", "sha", "files", "status", "at", "approved_at", "revoked"))
    w = st.get("external_waiver")
    if w:
        keep["external_waiver"] = dict((k, w.get(k)) for k in ("stage", "sha", "issued_at", "expires_at", "revoked"))
    cred = st.get("credential")
    if cred:
        keep["credential"] = dict((k, cred.get(k)) for k in ("stage", "candidate", "issued_at", "expires_at", "merge", "docs", "revoked"))
    import json
    say(json.dumps(keep, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


def say(text):
    C.out_text(text if text.endswith("\n") else text + "\n")


def fail(text):
    C.err(text)
    return 3


def main(argv=None, runner=None, main_root=None):
    ap = argparse.ArgumentParser(description="自動駕駛的通知信")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("send")
    s.add_argument("--stage", required=True)
    s.add_argument("--kind", required=True, choices=["stop", "tier2", "ready"])
    s.add_argument("--report", required=True)
    s.add_argument("--worktree")
    s.add_argument("--offline", action="store_true", help=argparse.SUPPRESS)
    c = sub.add_parser("close")
    c.add_argument("--stage", required=True)
    p = sub.add_parser("pr", help="PR 唯三的寫入（P2）：open、edit、request-review；文字先過隱私掃描")
    p.add_argument("action", choices=["open", "edit", "request-review"])
    p.add_argument("--stage", required=True)
    p.add_argument("--title")
    p.add_argument("--body-file", dest="body_file")
    v = sub.add_parser("verify", help="驗收機對這個 commit 綠不綠（唯讀）")
    v.add_argument("--stage", required=True)
    v.add_argument("--commit")
    v.add_argument("--wait", action="store_true")
    r = sub.add_parser("review-status", help="外部審查（Codex）完成了沒（唯讀）")
    r.add_argument("--stage", required=True)
    r.add_argument("--commit")
    r.add_argument("--wait", action="store_true")
    r.add_argument("--mark-incomplete", action="store_true", dest="mark_incomplete", help="沒完成就記下「外部審查未完成」（免外部審查的前提）")
    f = sub.add_parser("finish-docs", help="把早先的階段結案：它的文件併進了另一個階段的文件 commit")
    f.add_argument("--stage", required=True)
    f.add_argument("--merged-into", required=True, dest="merged_into")
    sub.add_parser("test")
    sub.add_parser("status")
    sub.add_parser("retry")
    a = ap.parse_args(argv)
    main_root = main_root or C.main_root()
    sd = os.path.join(C.common_dir(main_root), C.STATE_DIR_NAME)
    cfg = C.config(os.path.join(main_root, ".claude", "autopilot", "config.json"))
    if a.cmd == "send":
        return cmd_send(a, main_root, sd, cfg, runner)
    if a.cmd == "close":
        return cmd_close(a, main_root, sd, cfg, runner)
    if a.cmd == "finish-docs":
        return cmd_finish_docs(a, main_root, sd, cfg)
    if a.cmd == "pr":
        return cmd_pr(a, main_root, sd, cfg)
    if a.cmd == "verify":
        return cmd_verify(a, main_root, sd, cfg)
    if a.cmd == "review-status":
        return cmd_review_status(a, main_root, sd, cfg)
    if a.cmd == "status":
        return cmd_status(a, main_root, sd, cfg)
    if a.cmd == "retry":
        n = retry_outbox(main_root, cfg, runner)
        say("補寄了 %d 封。" % n)
        return 0
    if a.cmd == "test":
        ok, detail = deliver(main_root, cfg, "[自動駕駛] 測試信",
                             "這是一封測試信，確認通知寄得到你的信箱與手機。\n\n收到的話，請回到 Claude Code 告訴我「收到了」。\n\n（寄出時間：%s）" % C.local_stamp(),
                             "test", "open", runner)
        say(("測試信已經寄出（%s）。" if ok else "測試信沒有寄出去：%s") % detail)
        return 0 if ok else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:                                          # noqa: B902
        C.err("iw_notify 出錯：%r" % (e,))
        sys.exit(1)
