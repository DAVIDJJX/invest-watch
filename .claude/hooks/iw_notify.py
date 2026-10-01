# -*- coding: utf-8 -*-
"""
iw_notify.py — 自動駕駛的通知信、停止報告、事後偵測。

寄信的方法：觸發私人倉庫的 notify.yml（GitHub 機器人開一個 issue 指派給你 → GitHub 寄信、手機 App 通知）。
用的是這台電腦上已經登入的 gh；這裡不存、也不處理任何密碼或權杖。

指令（由 Claude 在停下來的時候執行；hook 也會直接呼叫裡面的函式）：
  python iw_notify.py send  --stage <階段> --kind stop|tier2|ready --report <報告檔> [--worktree <路徑>] [--no-review]
  python iw_notify.py close --stage <階段>          放行並合併完之後，把那一封的 issue 留言「已放行」並關掉
  python iw_notify.py test                          寄一封測試信
  python iw_notify.py status                        看現在的狀態（唯讀）
  python iw_notify.py retry                         把之前沒寄出去的再寄一次

「可以合併」（ready）那一種信寄出之前，程式自己會檢查：標籤在不在、分支推了沒、有沒有動到不能動的檔、
要先給 David 看 diff 的檔看過了沒、審查代理批准了沒（紀錄是 hook 寫的，不是 Claude 轉述的）。任何一項不過就不寄。
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
import iw_state as ST

SECTIONS = ["一句話", "你要決定的事", "做了什麼", "怎麼自己看", "名詞解釋", "要繼續", "要修改", "不確定"]
KIND_NAMES = {"stop": "停下來了，要你決定", "tier2": "請看 diff", "ready": "做完了，等你放行才合併"}


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

def check_report(text, stage, kind, cfg):
    """回傳問題清單（空的＝格式沒問題）。規格第 5 節：固定八段、一頁內、不貼程式碼。"""
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
    want_go = ("放行 " if kind == "ready" else "繼續 ") + stage
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
    report = io.open(a.report, encoding="utf-8").read()
    problems = check_report(report, stage, kind, cfg)
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
    has_wt = os.path.isdir(wt)
    if kind in ("ready", "tier2") and not has_wt:
        return fail("找不到這個階段的 worktree：%s" % wt)
    if has_wt:
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
        rc2, remote_head = C.git(["rev-parse", "-q", "--verify", "refs/remotes/%s/%s" % (cfg["remote"], branch)], wt)
        if rc != 0 or rc2 != 0 or remote_head != head:
            return fail("分支 %s 還沒推上去（或推上去的不是現在這個 commit）。David 要能在 GitHub 上看到才行。" % branch)
        ok_review = [r for r in (st.get("reviews") or []) if r.get("kind") == "acceptance" and r.get("verdict") == "APPROVE"
                     and r.get("commit") == head and r.get("stage") == stage and r.get("model_ok", True)]
        if not ok_review:
            if st.get("active") or not a.no_review:
                return fail("沒有審查代理對這個 commit（%s）的批准紀錄。先跑驗收審查；紀錄是 hook 寫的，不能用轉述的。" % head[:7])
            extra.append("注意：這一階段沒有經過審查代理（不是自動駕駛的流程）。")
        cand = {"sha": head, "tag_sha": tag_sha, "branch": branch, "registered_at": C.iso()}
        slug = github_slug(main_root, cfg)
        if slug:
            extra.append("改了哪些檔（GitHub）：https://github.com/%s/compare/%s...%s" % (slug, cfg["mainBranch"], branch))
    if not st.get("tripwire_baseline"):
        rc, tip = C.git(["rev-parse", "-q", "--verify", "refs/remotes/%s/%s" % (cfg["remote"], cfg["mainBranch"])], main_root)
        if rc == 0:
            st["tripwire_baseline"] = tip
    safety, findings = tripwire(main_root, sd, cfg, st, fetch=not a.offline)
    title = "[自動駕駛] %s：%s" % (stage, one_line(report) or KIND_NAMES[kind])
    body = "\n\n".join([report.strip()] + extra + [model_line(st, cfg) + "\n" + safety])
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
        if kind == "ready":
            s["candidate"] = cand
            s["status"] = "awaiting_approval"
            if s.get("credential"):
                s["credential"]["revoked"] = "重新寄了「可以合併」的信"
        else:
            if s.get("active"):
                s["status"] = "stopped"
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
    title = "[自動駕駛] %s：%s" % (stage, text.split("\n")[0][:60])
    body = "%s\n\n（這是程式自動寄的短信，時間 %s。詳細情況請看電腦上的 Claude Code。）\n\n%s" % (text, C.local_stamp(), model_line(st, cfg))
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


def cmd_status(a, main_root, sd, cfg):
    st = ST.load(sd)
    keep = dict((k, st.get(k)) for k in ("active", "stage", "status", "epoch", "session_id", "started_at", "clock_started_at", "stop_required",
                                         "pause", "candidate", "model_violation", "model_approved", "models_seen", "effort_seen",
                                         "notified_epoch", "last_notification", "tripwire_baseline") if k in st)
    keep["reviews"] = [(r.get("kind"), r.get("verdict"), (r.get("commit") or "")[:7], r.get("epoch")) for r in (st.get("reviews") or [])]
    keep["tier2"] = (st.get("tier2") or {}).get("touched")
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
    s.add_argument("--no-review", action="store_true", help="不是自動駕駛的流程（David 在場的一般階段）才可以用")
    s.add_argument("--offline", action="store_true", help=argparse.SUPPRESS)
    c = sub.add_parser("close")
    c.add_argument("--stage", required=True)
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
