# -*- coding: utf-8 -*-
"""
iw_review.py — 停點 P2 的「第三方」：讀驗收機的結果、讀 Codex 的審查、核對意見的回覆、免外部審查。

只做查詢與判斷，不改任何狀態（狀態由 iw_notify／iw_events 寫）。所有對 GitHub 的讀取都走這台電腦上已登入的 gh（`gh api` 的 GET），
不存任何權杖。測試用 IW_TEST_FAKE_GH（一個 JSON 檔）換掉 gh 的回答；沙盒裡的 pre-push 是另一個程序，所以用環境變數不用 monkeypatch。

判定（David 2026-10-04、10-05 的裁決）：
  * 驗收機綠＝這個 commit 有一次「真正的」驗收：流程檔的路徑是 .github/workflows/verify.yml、head_sha 就是這個 commit、由 push 觸發，
    而且那一次 completed＋success、它的結果檔（artifact）綁的是同一個 commit、沒有標紅。只是名字對得上的 check 或 commit status 一律不看。
  * 先比 blob：這個 commit 上的 verify.yml、驗收程式、突變執行器、例外清單跟 main 上的不一樣（用 git 比，不信 CI 自己說的）→ 驗收機的綠一律不算。
    實測（2026-10-04）這台電腦的憑證推得動流程檔，所以「Claude 改不到驗收機」靠的是這一道比對，不是 GitHub 的權限。
    例外只有兩個：main 上還沒有驗收程式（P2 第一次建立）；David 在一般模式手打「驗收機變更 <階段>」同意這個 commit（一次性、綁 commit）。
  * 外部審查完成＝Codex 機器人帳號針對 PR 現在的 head 發出的 review（不是 PENDING）。review 的文字只記錄，不當依據；別人的留言忽略、列出。
  * 意見＝同一個機器人對 head 的 inline 留言；等級從內文的 [P0]／[P1] 讀，讀不到當 P1（從嚴）。每一條都要在 03_第三方審查.md 有回覆。
  * 免外部審查：David 手打、程式事後核對、綁階段與 commit、24 小時失效、「修改」或新 commit 就作廢。只免外部審查。
"""
import io
import json
import os
import re
import shutil
import subprocess

import iw_common as C
import iw_state as ST

DEFAULT_VERIFIER_FILES = [".github/workflows/verify.yml", "scripts/verify_ci.py", "scripts/mutations/run_mutations.py", "scripts/mutations/known_survivors.json"]
FAKE_ENV = "IW_TEST_FAKE_GH"


# ---------------------------------------------------------------- gh

def gh_exe():
    p = shutil.which("gh")
    if p:
        return p
    for cand in ("C:/Program Files/GitHub CLI/gh.exe", "/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"):
        if os.path.exists(cand):
            return cand
    return None


def _fake(args, hints):
    """測試用：IW_TEST_FAKE_GH 指到一個 JSON 檔，裡面是 [{"match": "子字串", "rc": 0, "text": "…"}, …]；{sha} 會換成 hints 的 sha。"""
    path = os.environ.get(FAKE_ENV)
    if not path:
        return None
    try:
        with io.open(path, encoding="utf-8") as fh:
            entries = json.load(fh)
    except Exception:                                              # noqa: B902
        return 1, "fake gh：讀不到 %s" % path
    joined = " ".join(args)
    for e in entries:
        if e.get("match") in joined:
            text = str(e.get("text") or "")
            for k, v in (hints or {}).items():
                text = text.replace("{%s}" % k, str(v))
                text = text.replace("{%s7}" % k, str(v)[:7])        # {sha7}＝短的 commit 編號（Codex 的進度留言用短的）
            if e.get("file"):
                return int(e.get("rc", 0)), text, e["file"]
            return int(e.get("rc", 0)), text
    return 1, "fake gh：沒有對應的回答（%s）" % joined[:120]


def gh(args, timeout=25, runner=None, hints=None):
    """跑 gh。回傳 (結束碼, 文字)。runner 是測試用的替身；IW_TEST_FAKE_GH 是跨程序的替身。"""
    if runner is not None:
        return runner(list(args))[:2]
    fake = _fake(args, hints)
    if fake is not None:
        return fake[0], fake[1]
    exe = gh_exe()
    if not exe:
        return 1, "這台電腦找不到 gh 指令"
    try:
        p = subprocess.run([exe] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except Exception as e:                                         # noqa: B902
        return 1, "gh 執行失敗：%r" % (e,)
    out = p.stdout.decode("utf-8", "replace")
    if p.returncode != 0:
        return p.returncode, (p.stderr.decode("utf-8", "replace").strip() or out)[:300]
    return 0, out


def gh_json(args, timeout=25, runner=None, hints=None):
    rc, text = gh(args, timeout, runner, hints)
    if rc != 0:
        return None, text
    try:
        return json.loads(text), None
    except ValueError:
        return None, "gh 回的不是 JSON：%s" % text[:120]


def gh_pages(path, runner=None, hints=None, per_page=100, max_pages=30):
    """GitHub 的清單一頁最多 100 筆：一頁一頁讀到不滿一頁為止，合成一個清單。
    2026-10-05 Codex 的審查意見：原本只讀第一頁，第 101 則之後的意見就不必回覆、關卡照樣過。
    任何一頁讀不到、或讀到上限還沒完，回 (None, 原因)——寧可說查不完，也不要漏掉後面的。"""
    out = []
    sep = "&" if "?" in path else "?"
    for page in range(1, max_pages + 1):
        obj, err = gh_json(["api", "%s%sper_page=%d&page=%d" % (path, sep, per_page, page)], runner=runner, hints=hints)
        if obj is None:
            return None, err
        if not isinstance(obj, list):
            return None, "GitHub 回的不是清單"
        out.extend(obj)
        if len(obj) < per_page:
            return out, None
    return None, "超過 %d 頁還沒讀完" % max_pages


def slug(main_root, cfg):
    """這個倉庫在 GitHub 的名字（owner/repo）。沙盒的遠端是本機資料夾，沒有名字：測試模式（IW_TEST_FAKE_GH）給一個假的。"""
    rc, out = C.git(["remote", "get-url", cfg["remote"]], main_root)
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(\.git)?/?$", out.strip()) if rc == 0 else None
    if m:
        return m.group(1)
    return "fake/invest-watch" if os.environ.get(FAKE_ENV) else None


# ---------------------------------------------------------------- 驗收機

def _vs(green, why, **kw):
    d = {"green": bool(green), "why": why, "url": None, "result": None, "ran": None, "verifier_changed": False, "pending": False}
    d.update(kw)
    return d


def verifier_changed(main_root, cfg, sha):
    """分支（sha）上的驗收機檔跟 origin/main 的 blob 比。回傳 (不同的檔清單, main 上還沒有驗收程式)。用 git 比，不信 CI 自己說的。"""
    files = (cfg.get("verify") or {}).get("files") or DEFAULT_VERIFIER_FILES
    base = "%s/%s" % (cfg["remote"], cfg["mainBranch"])
    changed = []
    for f in files:
        rc1, a = C.git(["rev-parse", "-q", "--verify", "%s:%s" % (sha, f)], main_root)
        rc2, b = C.git(["rev-parse", "-q", "--verify", "%s:%s" % (base, f)], main_root)
        a = a.strip() if rc1 == 0 else None
        b = b.strip() if rc2 == 0 else None
        if a != b:
            changed.append(f)
    rc, _ = C.git(["rev-parse", "-q", "--verify", "%s:scripts/verify_ci.py" % base], main_root)
    return changed, rc != 0


def mark_verifier_changed(sd, stage, sha, files):
    """程式判定「驗收機本身有改」：記下來（David 手打「驗收機變更 <階段>」的前提）。同一個 commit 已經記過或同意過就不動。"""
    def fn(s):
        cur = s.get("verifier_change") or {}
        if cur.get("stage") == stage and str(cur.get("sha") or "").lower() == str(sha).lower() and not cur.get("revoked"):
            return
        s["verifier_change"] = {"stage": stage, "sha": sha, "files": list(files), "status": "detected", "at": C.iso()}
    ST.update(sd, fn)


def verifier_change_problem(st, cfg, stage, sha, check_transcript=True):
    """David 的「驗收機變更」現在能不能用在這個階段的這個 commit。可以回 None；不行回一句原因。"""
    w = st.get("verifier_change") or {}
    word = "驗收機變更 " + str(stage)
    if w.get("stage") != stage or w.get("status") != "approved":
        return "要用這個 commit 的驗收結果，得由 David 在一般模式手打「%s」" % word
    if w.get("revoked"):
        return "「驗收機變更」已作廢（%s）" % w["revoked"]
    if str(w.get("sha") or "").lower() != str(sha).lower():
        return "「驗收機變更」綁的是別的 commit（%s）；有新 commit 之後要重新手打一次" % str(w.get("sha") or "?")[:7]
    if check_transcript:
        text, human = ST.human_prompt(w.get("transcript_path"), w.get("prompt_id"))
        if text is None:
            return "對話紀錄裡找不到那一則「驗收機變更」"
        if not human:
            return "對話紀錄顯示那一則「驗收機變更」不是人打的"
        if ST.clean_prompt(text) != word:
            return "對話紀錄裡那一則的內容不是「%s」" % word
    return None


def download_result(main_root, cfg, stage, run_id, sha, runner=None):
    """把這次 run 的結果檔抓到 .autopilot/runs/<階段>/verify/<sha>/，讀成 dict。讀不到回 None。"""
    v = cfg["verify"]
    s = slug(main_root, cfg)
    dest = os.path.join(main_root, *(cfg["runsDir"].split("/") + [stage, "verify", str(sha)[:12]]))
    os.makedirs(dest, exist_ok=True)
    target = os.path.join(dest, "verify-result.json")
    try:
        if os.path.exists(target):
            os.remove(target)                                      # 同一個 commit 可能有兩次執行（推分支、推標籤）：每次都重抓，不沿用舊檔
    except Exception:                                              # noqa: B902
        return None
    args =["run", "download", str(run_id), "-R", s or "", "-n", v["artifact"], "-D", dest]
    if runner is not None:
        rc, _text = runner(args)[:2]
    else:
        fake = _fake(args, {"sha": sha})
        if fake is not None:
            rc = fake[0]
            if rc == 0 and len(fake) > 2 and fake[2]:
                shutil.copyfile(fake[2], target)
            elif rc == 0 and fake[1].strip():
                with io.open(target, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(fake[1])
        else:
            rc, _text = gh(args, timeout=90)
    if rc != 0 or not os.path.exists(target):
        return None
    try:
        with io.open(target, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                              # noqa: B902
        return None


def verify_status(main_root, sd, cfg, stage, sha, runner=None, now=None):
    """這個 commit 在驗收機上綠不綠。查不到＝不綠（David 的裁決：查不到就擋）。"""
    v = cfg.get("verify") or {}
    if not v:
        return _vs(False, "設定裡沒有驗收機（verify）")
    # 1. 先比 blob（不用連網）：這個 commit 上的驗收機跟 main 上的不一樣，驗收機的綠一律不算
    changed, first = verifier_changed(main_root, cfg, sha)
    approved = False
    if changed and not first:
        problem = verifier_change_problem(ST.load(sd), cfg, stage, sha)
        if problem:
            mark_verifier_changed(sd, stage, sha, changed)
            return _vs(False, "驗收機本身有改（%s）：這種綠不算。%s" % ("、".join(changed), problem), verifier_changed=True, changed_files=changed)
        approved = True
    # 2. 只認真正的那一次驗收：流程檔的路徑對、head_sha 是這個 commit、由 push 觸發
    s = slug(main_root, cfg)
    if not s:
        return _vs(False, "看不出這個倉庫在 GitHub 的名字")
    obj, err = gh_json(["api", "repos/%s/actions/workflows/%s/runs?head_sha=%s&per_page=30" % (s, v["workflow"], sha)], runner=runner, hints={"sha": sha})
    if obj is None:
        return _vs(False, "查不到驗收機的執行紀錄（%s）" % err)
    want_path = ".github/workflows/" + v["workflow"]
    runs = [r for r in (obj.get("workflow_runs") or [])
            if str(r.get("head_sha") or "").lower() == str(sha).lower() and r.get("path") == want_path and r.get("event") == "push"]
    if not runs:
        return _vs(False, "驗收機還沒有這個 commit（%s）由 push 觸發的執行紀錄" % str(sha)[:7])
    run = max(runs, key=lambda r: (str(r.get("created_at") or ""), int(r.get("run_attempt") or 0)))     # 最新的那一次算數（推分支、推標籤各有一次）
    url = run.get("html_url")
    if run.get("status") != "completed":
        return _vs(False, "驗收機還在跑（%s）" % run.get("status"), url=url, pending=True)
    if run.get("conclusion") != "success":
        return _vs(False, "驗收機是紅的（%s）" % run.get("conclusion"), url=url)
    # 3. 結論與數字都從那一次執行的結果檔讀
    res = download_result(main_root, cfg, stage, run.get("id"), sha, runner=runner)
    if res is None:
        return _vs(False, "讀不到驗收機的結果檔（artifact「%s」）" % v["artifact"], url=url)
    if str(res.get("commit") or "").lower() != sha.lower():
        return _vs(False, "結果檔綁的 commit（%s）不是這一個（%s）" % (str(res.get("commit") or "?")[:7], str(sha)[:7]), url=url, result=res)
    # 重跑過的執行（2026-10-06 裁決：准重跑、有次數上限）：算數的是最後那一次嘗試。結果檔是哪一次嘗試寫的，要跟 GitHub 說的現在這一次對得上。
    attempt = int(run.get("run_attempt") or 1)
    if str(res.get("run_attempt") or "1") != str(attempt):
        return _vs(False, "結果檔是第 %s 次嘗試寫的，GitHub 上這一次執行現在是第 %d 次嘗試：算數的是最後那一次" % (res.get("run_attempt") or "?", attempt),
                   url=url, result=res, attempt=attempt)
    if res.get("red"):
        return _vs(False, "驗收機的結果檔標紅：%s" % "；".join(res.get("reasons") or [])[:200], url=url, result=res, attempt=attempt)
    return _vs(True, "綠", url=url, result=res, ran=(res.get("tests") or {}).get("ran"), verifier_changed=bool(changed),
               verifier_change_approved=approved, changed_files=changed, first_time=first, attempt=attempt)


# ---------------------------------------------------------------- Codex（外部審查）

_BADGE = re.compile(r"!\[P(\d) Badge\]")                          # Codex 標等級的寫法：![P0 Badge](…)
_BLOB_LINE = re.compile(r"^\s*https://github\.com/[^/\s]+/[^/\s]+/blob/[0-9a-f]{7,40}/(\S+?)#L(\d+)(?:-L\d+)?\s*$")


def severity_of(body, ext):
    m = _BADGE.search(body or "")
    if m:
        return "P%s" % m.group(1)
    m = re.search(ext.get("severityPattern") or r"\[?P([01])\]?", body or "")
    return "P%s" % m.group(1) if m else "P1"


def body_findings(review, ext):
    """review 本文裡的意見。Codex 把釘不到 diff 行上的意見寫在 review 的本文裡（一行檔案連結＋一行等級徽章與標題＋說明），
    不是行內留言——P2 自己的 PR 第一次審查就有一條 P0 是這樣來的。只讀行內留言會漏掉它們。
    編號是「review 編號-第幾條」。只有檔案連結、沒有徽章的段落也算一條（沒標等級的當 P1）。結尾的說明區塊（<details>）不看。"""
    body = (review.get("body") or "").split("<details")[0]
    found, pending = [], None

    def add(path, line, text, badge):
        clean = re.sub(r"<[^>]+>|\*\*|!\[[^\]]*\]\([^)]*\)", "", text or "").strip()
        found.append({"id": "%s-%d" % (review.get("id"), len(found) + 1), "severity": severity_of(badge or "", ext), "path": path, "line": line,
                      "excerpt": clean[:160], "where": "review 本文"})
    for line in body.replace("\r\n", "\n").split("\n"):
        m = _BLOB_LINE.match(line)
        if m:
            if pending is not None:                                 # 上一個連結後面一直沒有徽章：也算一條
                add(pending[0], pending[1], pending[2], None)
            pending = (m.group(1), int(m.group(2)), line.strip())
            continue
        if _BADGE.search(line):
            add(pending[0] if pending else None, pending[1] if pending else None, line, line)
            pending = None
    if pending is not None:
        add(pending[0], pending[1], pending[2], None)
    return found


def find_pr(main_root, cfg, branch, runner=None, hints=None):
    s = slug(main_root, cfg)
    if not s:
        return None, "看不出這個倉庫在 GitHub 的名字"
    owner = s.split("/")[0]
    hints = dict(hints or {}, branch=branch)
    obj, err = gh_json(["api", "repos/%s/pulls?head=%s:%s&state=open&per_page=100" % (s, owner, branch)], runner=runner, hints=hints)
    if obj is None:
        return None, err
    prs = obj if isinstance(obj, list) else []
    if not prs:
        return None, "找不到分支 %s 的 PR（還沒開？）" % branch
    # 只認「從這個倉庫的這條分支、合併進這個倉庫的正式版分支」的那一個 PR（2026-10-06 Codex 的審查意見，P0）：
    # 原本只照分支名稱找、直接拿第一筆。同一條分支可以另外開一個對著別的分支的 PR——那個 PR 的 diff 可以是空的或不完整，
    # Codex 對同一個 commit 的完成訊號卻照樣算數，最後合併進正式版的是沒被審過的完整改動。對得上的不是剛好一個，就當成找不到。
    def full(side):
        return str(((side or {}).get("repo") or {}).get("full_name") or "").lower()
    main = cfg["mainBranch"]
    ours = [p for p in prs if (p.get("base") or {}).get("ref") == main and full(p.get("base")) == s.lower()
            and (p.get("head") or {}).get("ref") == branch and full(p.get("head")) == s.lower()]
    if not ours:
        return None, ("分支 %s 有開著的 PR（%d 個），但沒有一個是從這個倉庫的這條分支合併進 %s 的：外部審查只認對著 %s 的那一個"
                      % (branch, len(prs), main, main))
    if len(ours) > 1:
        return None, "分支 %s 對著 %s 的 PR 有 %d 個，看不出哪一個算數：請只留一個" % (branch, main, len(ours))
    pr = ours[0]
    return {"number": pr.get("number"), "url": pr.get("html_url"), "head": ((pr.get("head") or {}).get("sha") or "").lower(), "title": pr.get("title"),
            "created_at": pr.get("created_at"), "body": pr.get("body")}, None


def codex_status(main_root, cfg, pr_number, head, runner=None, pr=None):
    """Codex 對這個 commit 審完了沒有、這個 PR 上它提過哪些意見。完成的訊號有兩種（都要綁最新的 commit）：
    A＝機器人帳號針對這個 commit 發出的 review（有意見時）；B＝機器人自己那則進度留言寫這個 commit Completed（沒有意見時；見 summary_verdict）。
    同一個 commit 兩種都有時以 A 為準。Codex 的實際行為以 2026-10-05 這個倉庫 1 號 PR 的觀察為準。
    findings 是這個 PR 上它提過的每一條（每一條都要回覆）；p0_on_head 是針對最新 commit 的 P0（有的話一定要修、再審）；rounds 是審了幾輪。"""
    ext = cfg["externalReview"]
    bot = ext["botLogin"]
    s = slug(main_root, cfg)
    hints = {"sha": head}
    revs, err1 = gh_pages("repos/%s/pulls/%s/reviews" % (s, pr_number), runner=runner, hints=hints)
    coms, err2 = gh_pages("repos/%s/pulls/%s/comments" % (s, pr_number), runner=runner, hints=hints)
    if revs is None or coms is None:
        return {"complete": False, "why": "查不到 PR 的審查（%s）" % (err1 or err2), "reviews": [], "findings": [], "others": [], "major": []}
    # 2026-10-06 裁決第三節：這個 PR 上 Codex 提過的「每一條」意見都要回覆——不分是哪個 commit 的、GitHub 有沒有把它跟著移到新的 commit。
    # 原本只算最新 commit 的：舊 commit 的意見只要再推一次就不用回了。on_head＝這一條是針對最新的 commit 提的（最新的 commit 不可以有 P0）。
    all_reviews, reviews, others = [], [], set()
    for r in (revs if isinstance(revs, list) else []):
        if (r.get("user") or {}).get("login") != bot:
            others.add((r.get("user") or {}).get("login") or "?")
            continue
        if r.get("state") == "PENDING":
            continue
        all_reviews.append(r)
        if str(r.get("commit_id") or "").lower() == head.lower():
            reviews.append(r)                                       # 針對最新 commit 的 review（訊號 A）
    head_ids = set(str(r.get("id")) for r in reviews)
    findings, inline = [], set()
    for c in (coms if isinstance(coms, list) else []):
        login = (c.get("user") or {}).get("login")
        if login != bot:
            others.add(login or "?")
            continue
        rid = c.get("pull_request_review_id")
        # GitHub 會把舊留言的 commit_id 改成新的 commit；original_commit_id 與它屬於哪一次 review 不會變，用這兩個認
        on_head = str(c.get("original_commit_id") or "").lower() == head.lower() or (rid is not None and str(rid) in head_ids)
        line = c.get("line") or c.get("original_line")
        findings.append({"id": c.get("id"), "severity": severity_of(c.get("body") or "", ext), "path": c.get("path"), "line": line,
                         "excerpt": (c.get("body") or "")[:160], "on_head": on_head, "review_id": rid})
        inline.add((None if rid is None else str(rid), c.get("path"), line))
    for r in all_reviews:                                           # review 本文裡的意見也算（同一次 review 裡同一個檔同一行已經有行內留言的不重複算）
        r_head = str(r.get("id")) in head_ids
        for f in body_findings(r, ext):
            if (str(r.get("id")), f.get("path"), f.get("line")) in inline or (r_head and (None, f.get("path"), f.get("line")) in inline):
                continue
            findings.append(dict(f, on_head=r_head, review_id=r.get("id")))
    out = {"complete": bool(reviews), "signal": "A" if reviews else None, "summary": None, "rounds": None,
           "why": None if reviews else "Codex（%s）還沒有針對 commit %s 發出 review" % (bot, head[:7]),
           "reviews": [{"id": r.get("id"), "state": r.get("state"), "body": (r.get("body") or "")[:200], "submitted_at": r.get("submitted_at")} for r in reviews],
           "findings": findings, "others": sorted(others), "major": [f for f in findings if f["severity"] in ("P0", "P1")],
           "p0_on_head": [f for f in findings if f.get("on_head") and f["severity"] == "P0"]}
    issue_comments, err3 = gh_pages("repos/%s/issues/%s/comments" % (s, pr_number), runner=runner, hints=hints)
    if issue_comments is not None:                                  # 審了幾輪：開 PR 時自動審的那一次，加上之後每一則「@codex review」
        out["rounds"] = 1 + len([x for x in issue_comments if (x.get("body") or "").strip() == ext["trigger"]])
    if reviews:                                                     # 訊號 A：這個 commit 有 review 就以 review 為準，不去看訊號 B（不能用 B 蓋掉 A）
        return out
    # 訊號 B（2026-10-06 Cowork 的裁決）：Codex 沒有意見時不發 review，只把它自己那則「Codex Review Summary」留言更新成 Completed
    if issue_comments is None:
        out["why"] = "%s；也查不到 PR 的留言（%s）" % (out["why"], err3)
        return out
    created = (pr or {}).get("created_at")
    if not created:
        obj, _e = gh_json(["api", "repos/%s/pulls/%s" % (s, pr_number)], runner=runner, hints=hints)
        created = (obj or {}).get("created_at") if isinstance(obj, dict) else None
    ok, why, detail = summary_verdict(issue_comments, ext, head, push_time(main_root, cfg, head, runner=runner), C.parse_iso(created))
    out["summary"] = detail
    if ok:
        out.update({"complete": True, "signal": "B", "why": None})
    else:
        out["why"] = "%s；%s" % (out["why"], why)
    return out


# ---- 訊號 B：Codex 自己那則進度留言

SUMMARY_TITLE = "Codex Review Summary"
_REL_TIME = re.compile(r'datetime="([^"]+)"')
_SHORT_SHA = re.compile(r"`([0-9a-fA-F]{7,40})`")


def parse_summary_table(body):
    """進度留言裡的那張表。回傳 (列的清單, None)；每一列是 {review, status, commit, trigger}（原文）。
    表頭少了 Review、Status、Commit 任何一欄，或有一列欄數不對 → 回 (None, 原因)：格式變了就不猜（裁決第四節）。"""
    header, rows = None, []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if not s.startswith("|"):
            if header is not None and rows:
                break                                               # 表格結束了
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if all(re.match(r"^:?-{3,}:?$", c) for c in cells):         # 表頭底下的分隔線
            continue
        rows.append(cells)
    if header is None:
        return None, "裡面沒有表格"
    idx = {}
    for want in ("review", "status", "commit"):
        if want not in header:
            return None, "表格少了 %s 欄" % want.capitalize()
        idx[want] = header.index(want)
    trig = header.index("review trigger") if "review trigger" in header else None
    out = []
    for cells in rows:
        if len(cells) != len(header):
            return None, "表格有一列的欄數跟表頭不一樣"
        out.append({"review": cells[idx["review"]], "status": cells[idx["status"]], "commit": cells[idx["commit"]],
                    "trigger": cells[trig] if trig is not None else ""})
    if not out:
        return None, "表格沒有任何一列"
    return out, None


def push_time(main_root, cfg, head, runner=None):
    """這個 commit 第一次被推上 GitHub 的時間：用驗收機「由 push 觸發」的那幾次執行裡最早的建立時間（GitHub 記的，不是本機說的）。查不到回 None。"""
    v = cfg.get("verify") or {}
    s = slug(main_root, cfg)
    if not v or not s:
        return None
    obj, _err = gh_json(["api", "repos/%s/actions/workflows/%s/runs?head_sha=%s&per_page=100" % (s, v["workflow"], head)], runner=runner, hints={"sha": head})
    want = ".github/workflows/" + v["workflow"]
    times = [C.parse_iso(r.get("created_at")) for r in ((obj or {}).get("workflow_runs") or [] if isinstance(obj, dict) else [])
             if str(r.get("head_sha") or "").lower() == head.lower() and r.get("path") == want and r.get("event") == "push"]
    times = [t for t in times if t is not None]
    return min(times) if times else None


def summary_verdict(comments, ext, head, t_push, pr_created):
    """訊號 B 成不成立。comments：PR 的一般留言（全部頁）。回傳 (成立, 不成立的原因, 細節)。
    成立的條件（2026-10-06 Cowork 的裁決，全部都要）：留言是機器人帳號發的那則「Codex Review Summary」；表格裡 Code Review 那一列的
    Commit 欄短 sha 對得上最新的 commit；Status 是 Completed 而且讀得出完成時間；完成時間晚於最後一次推送，
    也晚於最後一則「@codex review」留言（從來沒有人留過＝第一次自動審查，就要晚於開 PR 的時間）。
    表情（👍、👀）不看；一般留言裡出現 Completed 字樣不算；格式跟上面不一樣就當成還沒完成，不猜。"""
    bot = ext["botLogin"]
    mine = [c for c in comments if (c.get("user") or {}).get("login") == bot and SUMMARY_TITLE in (c.get("body") or "")]
    if not mine:
        return False, "也沒有它的進度留言（%s）" % SUMMARY_TITLE, None
    c = max(mine, key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""))
    rows, problem = parse_summary_table(c.get("body"))
    if rows is None:
        return False, "它的進度留言格式跟預期的不一樣（%s），不能當成完成的訊號，也不猜" % problem, {"format_problem": problem, "comment_id": c.get("id")}
    code = [r for r in rows if "code review" in re.sub(r"[*_`]", "", r["review"]).lower()]
    if not code:
        return False, "它的進度留言格式跟預期的不一樣（表格裡找不到 Code Review 那一列），不能當成完成的訊號，也不猜", {"format_problem": "沒有 Code Review 那一列",
                                                                                       "comment_id": c.get("id")}
    stale = None
    for r in code:
        m = _SHORT_SHA.search(r["commit"])
        if not m:
            return False, "它的進度留言格式跟預期的不一樣（Commit 欄讀不出 commit），不能當成完成的訊號，也不猜", {"format_problem": "Commit 欄讀不出 commit",
                                                                                           "comment_id": c.get("id")}
        short = m.group(1).lower()
        if not head.lower().startswith(short):
            stale = short
            continue
        plain = re.sub(r"<[^>]+>", " ", r["status"])
        plain = re.sub(r"[*_`]", "", plain).strip()
        if "completed" not in plain.lower():
            return False, "它的進度留言寫 commit %s 的狀態是「%s」，還不是 Completed" % (short, plain[:30] or "空白"), {"commit": short, "status": plain[:60]}
        t = _REL_TIME.search(r["status"])
        done = C.parse_iso(t.group(1)) if t else None
        if done is None:
            return False, "它的進度留言格式跟預期的不一樣（寫了 Completed 但讀不出完成時間），不能當成完成的訊號，也不猜", {"format_problem": "讀不出完成時間",
                                                                                               "comment_id": c.get("id")}
        detail = {"commit": short, "completed_at": C.iso(done), "trigger": re.sub(r"[*_`]", "", r["trigger"]).strip()[:40], "comment_id": c.get("id")}
        if t_push is None:
            return False, "查不到最後一次推送的時間（驗收機沒有這個 commit 由 push 觸發的執行紀錄），沒辦法確認進度留言是推送之後才完成的", detail
        if done <= t_push:
            return False, "它的進度留言裡的完成時間（%s）不晚於最後一次推送（%s）" % (C.iso(done), C.iso(t_push)), detail
        triggers = [C.parse_iso(x.get("created_at")) for x in comments if (x.get("body") or "").strip() == ext["trigger"]]
        triggers = [x for x in triggers if x is not None]
        t_trig = max(triggers) if triggers else None
        if t_trig is not None and done <= t_trig:
            return False, "它的進度留言裡的完成時間（%s）不晚於最後一則「%s」留言（%s）：那一次還沒審完" % (C.iso(done), ext["trigger"], C.iso(t_trig)), detail
        if t_trig is None and (pr_created is None or done <= pr_created):
            return False, "它的進度留言裡的完成時間（%s）不晚於開 PR 的時間（或查不到開 PR 的時間）" % C.iso(done), detail
        return True, None, detail
    return False, "它的進度留言還停在舊的 commit（%s），不是現在的 %s" % (stale, head[:7]), {"commit": stale}


RESPONSE_ADOPT, RESPONSE_REJECT = "採納並修", "不採納"


def blank_reason(text):
    """「理由或修在哪」那一欄算不算空的：沒有字，或只有空白、橫線、底線、刪節號這類佔位的符號。"""
    return not re.sub(r"[\s\-—–−_＿.。…．·、,，:：;；/／\\()（）\[\]【】「」『』*`~?？!！]+", "", text or "")


def parse_responses(path, rulings=None):
    """03_第三方審查.md 的表：| 留言 id | 等級 | 檔案:行 | 回覆 | 理由或修在哪 |。回傳 {id: {severity, response, reason, ruling, deferred_to}}。
    2026-10-05 Codex 的審查意見：原本回覆欄只要「包含」採納兩個字就算（「尚未採納」也算採納），第五欄也不看。現在：
    回覆欄只認兩種、而且要一字不差——「採納並修」或「不採納」；第五欄（理由或修在哪）不可以是空的；
    同一個編號出現兩次而且回覆不一樣，也不算。不符合的一律記成 other（＝還沒回覆）。
    rulings：這個階段 hook 存的裁決紀錄（ruling_records）；「不採納」那一列的 ruling 只有在理由欄指到其中一份、而且那一份點名了這一條時才有值。"""
    rows, conflict = {}, set()
    try:
        text = io.open(path, encoding="utf-8").read()
    except Exception:                                              # noqa: B902
        return rows
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4 or not re.match(r"^\d+(-\d+)?$", cells[0]):     # 行內留言的編號是數字；review 本文裡的意見是「review 編號-第幾條」
            continue
        resp = cells[3]
        reason = cells[4] if len(cells) > 4 else ""
        if resp == RESPONSE_REJECT:
            kind = "reject"
        elif resp == RESPONSE_ADOPT:
            kind = "adopt"
        else:
            kind = "other"
        if kind != "other" and blank_reason(reason):               # 沒寫理由、沒寫修在哪：不算回覆
            kind = "other"
        key = int(cells[0]) if cells[0].isdigit() else cells[0]
        if key in rows and rows[key]["response"] != kind:           # 同一條意見兩種回覆：看不出哪一個算數，當成沒回覆
            conflict.add(key)
        if key in conflict:
            kind = "other"
        ruling, deferred = (ruling_file(reason, os.path.dirname(os.path.abspath(path)), rulings, finding_id=cells[0])
                            if kind == "reject" else (None, None))
        rows[key] = {"severity": cells[1], "response": kind, "reason": reason, "raw": resp, "ruling": ruling, "deferred_to": deferred}
    return rows


_RULING = re.compile(r"^(?:延後到\s*(?P<stage>[^\s，,]+?)\s*[，,]\s*)?Cowork\s*裁決\s*[：:]\s*(?P<file>.+?)\s*$")


def ruling_records(st, stage):
    """這個階段可以拿來當「不採納」依據的裁決紀錄。只有 hook 存的那幾份算：David 第一行手打「裁決：<階段>」、下面貼 Cowork 寫的內容，
    hook 把原件存在狀態資料夾（施工的一方寫不到）、副本存在報告資料夾，各記一個內容雜湊；事後核對過那一則是人打的（verified）、沒有被判冒充。
    2026-10-06 Codex 的審查意見（P0）：原本只看報告資料夾裡有沒有一個檔名像裁決、不是空的檔——那個資料夾施工的一方本來就寫得到，
    自己建一個檔就能把 P1 標成「不採納」。回傳 [{name, original, sha256, copy_sha256}]。"""
    out = []
    for r in (st or {}).get("rulings") or []:
        if r.get("stage") != stage or r.get("forged") or r.get("verified") is not True:
            continue
        if not (r.get("name") and r.get("original") and r.get("sha256") and r.get("copy_sha256")):
            continue                                                # 舊版記的（沒有雜湊）不能當依據
        out.append({"name": r["name"], "original": r["original"], "sha256": r["sha256"], "copy_sha256": r["copy_sha256"]})
    return out


def ruling_file(reason, folder, records=None, finding_id=None):
    """「不採納」重大意見的理由欄（2026-10-06 裁決第三節）：只認「Cowork 裁決：<檔名>」或「延後到 <階段>，Cowork 裁決：<檔名>」，一字不差。
    <檔名>要是 hook 存的那份裁決的副本（見 ruling_records）；原件與副本現在的內容都要跟 hook 當時記的雜湊一樣；
    而且裁決的內容裡要寫得出這一條意見的留言編號——一份裁決只能拿來回答它點名的那幾條。檔名、檔案大小都不是依據。
    回傳 (檔名, 延後到哪個階段或 None)；不符合回 (None, None)。"""
    m = _RULING.match((reason or "").strip())
    if not m:
        return None, None
    name = m.group("file").strip().strip("`「」『』 ")
    if not name or name != os.path.basename(name) or "/" in name or "\\" in name or name.startswith("."):
        return None, None
    rec = next((x for x in (records or []) if x.get("name") == name), None)
    if rec is None:                                                 # 不是 hook 存的裁決（報告資料夾裡自己放的檔不算）
        return None, None
    if ST.file_sha256(rec.get("original")) != rec.get("sha256") or ST.file_sha256(os.path.join(folder, name)) != rec.get("copy_sha256"):
        return None, None                                           # 原件或副本被改過（或讀不到）
    if finding_id is not None:
        try:
            text = io.open(rec["original"], encoding="utf-8", errors="replace").read()
        except Exception:                                          # noqa: B902
            return None, None
        if not re.search(r"(?<![0-9-])%s(?![0-9-])" % re.escape(str(finding_id)), text):
            return None, None                                       # 這份裁決沒有點名這一條意見
    return name, m.group("stage")


def responses_problems(findings, rows):
    """回傳 (沒回覆的 id, 被判不採納的 P0, 判不採納卻沒有 Cowork 裁決檔的 P1)。2026-10-06 裁決第三節：
    P0 不能不採納，一定要修；P1 要不採納，理由欄得指到 Cowork 的裁決檔（見 ruling_file），沒有就當成還沒回覆；等級不到 P1 的可以直接不採納。"""
    missing = [f["id"] for f in findings if f.get("id") not in rows or rows[f["id"]]["response"] == "other"]
    rejected = [f for f in findings if f.get("severity") == "P0" and rows.get(f.get("id"), {}).get("response") == "reject"]
    unruled = [f for f in findings if f.get("severity") == "P1" and rows.get(f.get("id"), {}).get("response") == "reject"
               and not rows[f["id"]].get("ruling")]
    return missing, rejected, unruled


def unfixed_on_head(findings, rows):
    """針對最新 commit 的意見、回覆卻寫「採納並修」的那幾條。要修一條意見，一定得有比「被審的那個 commit」更新的 commit；
    最新的 commit 就是被審的那一個，表示修的東西還沒推上來（或根本沒修）。推上去之後它就變成舊 commit 的意見，新的 commit 再請 Codex 審。
    （這一條是施工時補的：只看「有沒有回覆」的話，把最新 commit 的 P1 寫成「採納並修」、什麼都不改，關卡也會過。）"""
    return [f for f in findings if f.get("on_head") and rows.get(f.get("id"), {}).get("response") == "adopt"]


def round_cap(cfg, stage):
    """這個階段最多請 Codex 審幾輪（2026-10-06 裁決第三節）。到了上限還沒達成完成條件＝停下來寄「要你決定」；上限不是自動放行。"""
    caps = (cfg.get("externalReview") or {}).get("maxRounds") or {}
    try:
        return int(caps.get(stage, caps.get("default", 4)))
    except Exception:                                              # noqa: B902
        return 4


# ---------------------------------------------------------------- 免外部審查

def waiver_problem(st, cfg, stage, head, now=None, check_transcript=True):
    """免外部審查現在能不能用在這個階段的這個 commit。可以回 None；不行回一句原因。"""
    w = st.get("external_waiver")
    hours = float((cfg.get("externalReview") or {}).get("waiverHours", 24))
    if not w or w.get("stage") != stage:
        return "沒有免外部審查的紀錄（要 David 手打「免外部審查 %s」）" % stage
    if w.get("revoked"):
        return "免外部審查已作廢（%s）" % w["revoked"]
    now = now or C.now()
    exp = C.parse_iso(w.get("expires_at"))
    if exp is None or now > exp:
        return "免外部審查已過期（%d 小時內有效）" % int(hours)
    if str(w.get("sha") or "").lower() != head.lower():
        return "免外部審查綁的是別的 commit（%s）；有新 commit 之後要重新免除" % str(w.get("sha") or "?")[:7]
    if check_transcript:
        text, human = ST.human_prompt(w.get("transcript_path"), w.get("prompt_id"))
        if text is None:
            return "對話紀錄裡找不到那一則「免外部審查」"
        if not human:
            return "對話紀錄顯示那一則「免外部審查」不是人打的"
        if ST.clean_prompt(text) != "免外部審查 " + stage:
            return "對話紀錄裡那一則的內容不是「免外部審查 %s」" % stage
    return None


def external_status(main_root, sd, cfg, stage, head, st, runner=None, now=None):
    """外部審查的狀態：mode 是 codex／waived／incomplete。"""
    ext = cfg.get("externalReview") or {}
    if not ext:
        return {"mode": "incomplete", "complete": False, "why": "設定裡沒有外部審查（externalReview）", "pr": None, "findings": [], "others": [], "major": []}
    branch = cfg["branchPrefix"] + stage
    pr, err = find_pr(main_root, cfg, branch, runner=runner, hints={"sha": head})
    wp = waiver_problem(st, cfg, stage, head, now=now)
    if pr is None:
        if wp is None:
            return {"mode": "waived", "complete": True, "why": None, "pr": None, "findings": [], "others": [], "major": [], "waiver": st.get("external_waiver")}
        return {"mode": "incomplete", "complete": False, "why": err, "pr": None, "findings": [], "others": [], "major": []}
    cs = codex_status(main_root, cfg, pr["number"], head, runner=runner, pr=pr)
    out = {"mode": "codex" if cs["complete"] else "incomplete", "complete": cs["complete"], "why": cs.get("why"), "pr": pr,
           "pr_url": pr.get("url"), "findings": cs["findings"], "others": cs["others"], "major": cs["major"], "reviews": cs["reviews"],
           "signal": cs.get("signal"), "summary": cs.get("summary"), "p0_on_head": cs.get("p0_on_head") or [],
           "rounds": cs.get("rounds"), "round_cap": round_cap(cfg, stage)}
    if not cs["complete"] and wp is None:
        out.update({"mode": "waived", "complete": True, "why": None, "waiver": st.get("external_waiver")})
    out["pr_head_matches"] = (pr.get("head") or "") == head.lower()
    return out


def foreign_commit_check(main_root, sd, cfg, stage, fetch=True):
    """PR 分支的遠端頭是不是自己推過的（第二道記的 branch_pushes）。不是＝有人（Codex？別的帳號？）動了分支：記下來、自動駕駛中暫停、寄短信。
    為什麼不只靠第二道：本機分支落後遠端時，git 自己就先拒收（non-fast-forward），pre-push 根本看不到那一筆；所以寄信、查驗收機、查審查時都再看一次。
    回傳問題（一句話）或 None。"""
    branch = cfg["branchPrefix"] + stage
    rref = "refs/heads/" + branch
    if fetch:
        C.git(["fetch", "-q", cfg["remote"], branch], main_root, timeout=45)
    rc, remote_sha = C.git(["rev-parse", "-q", "--verify", "refs/remotes/%s/%s" % (cfg["remote"], branch)], main_root)
    if rc != 0 or not remote_sha.strip():
        return None
    remote_sha = remote_sha.strip()
    st = ST.load(sd)
    pushes = (st.get("branch_pushes") or {}).get(rref) or []
    if not pushes or remote_sha in pushes:
        return None
    import iw_prepush
    iw_prepush.mark_foreign(main_root, sd, cfg, st, rref, remote_sha)
    return "遠端的 %s 上有不是你推的 commit（%s）；有人動了 PR 的分支，不要覆蓋、不要合併它" % (branch, remote_sha[:7])


# ---------------------------------------------------------------- 流程檔的事後偵測（每封停止信一行）

WORKFLOW_DIR = ".github/workflows"


def workflow_snapshot(main_root, cfg):
    """遠端每個分支上 .github/workflows/ 的樣子：{分支: {路徑: blob}}。看的是本機記的遠端位置（呼叫的人先 fetch --prune）。"""
    remote = cfg["remote"]
    prefix = "refs/remotes/%s/" % remote
    rc, out = C.git(["for-each-ref", "--format=%(refname)", prefix], main_root)
    snap = {}
    if rc != 0:
        return None
    for ref in out.split("\n"):
        ref = ref.strip()
        if not ref or ref.endswith("/HEAD"):
            continue
        rc, ls = C.git(["-c", "core.quotepath=false", "ls-tree", "-r", ref, "--", WORKFLOW_DIR], main_root, timeout=30)
        if rc != 0:
            continue
        files = {}
        for line in ls.split("\n"):
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) >= 3 and path:
                files[path] = parts[2]
        snap[ref[len(prefix):]] = files
    return snap


def workflow_changes(prev, cur, main_branch):
    """自上次停止信以來，哪個分支新增、修改或刪除了流程檔。回傳 [(分支, 路徑, 新增／修改／刪除)]。
    分支上的流程檔跟現在的正式版一樣＝沒事（只是跟上了 main）；跟上次停止信時一樣＝上次已經報過，不重報。
    prev 是 None（第一次檢查）：正式版只記基準；其他分支跟現在的正式版比。"""
    out = []
    main_now = (cur or {}).get(main_branch) or {}
    for name in sorted(cur or {}):
        files = cur[name]
        before = (prev or {}).get(name)
        if name == main_branch:
            if before is None:
                continue
            for p in sorted(set(files) | set(before)):
                if files.get(p) != before.get(p):
                    out.append((name, p, "新增" if p not in before else ("刪除" if p not in files else "修改")))
            continue
        for p in sorted(files):
            if main_now.get(p) == files[p]:
                continue
            if before is not None and before.get(p) == files[p]:
                continue
            out.append((name, p, "新增" if p not in main_now else "修改"))
        for p in sorted(before or {}):
            if p not in files and p in main_now:
                out.append((name, p, "刪除"))
    return out


def workflow_watch(main_root, cfg, st, stage=None, fetch=True):
    """回傳 (信裡的那一行, 新的快照或 None)。快照由呼叫的人在信寄出時存進狀態（下一封信跟它比）。"""
    if fetch:
        rc, _ = C.git(["fetch", "-q", "--prune", cfg["remote"]], main_root, timeout=60)
        if rc != 0:
            return "流程檔檢查：這次連不上 GitHub，沒有檢查各分支的流程檔。", None
    cur = workflow_snapshot(main_root, cfg)
    if cur is None:
        return "流程檔檢查：讀不到各分支的清單，沒有檢查。", None
    prev = st.get("workflow_snapshot")
    found = workflow_changes(prev, cur, cfg["mainBranch"])
    since = "自上次停止信以來" if prev is not None else "第一次檢查（跟正式版比）"
    if not found:
        return "流程檔檢查：%s，沒有任何分支新增或修改 %s/ 裡的檔（看了 %d 個分支）。" % (since, WORKFLOW_DIR, len(cur)), cur
    mine = (cfg["branchPrefix"] + stage) if stage else None
    lines = ["流程檔檢查：⚠ %s，有 %d 處流程檔的變更要你看一下——" % (since, len(found))]
    for name, path, what in found[:8]:
        lines.append("- 分支 %s：%s %s%s" % (name, what, path, "（這個階段自己的分支）" if name == mine else ""))
    if len(found) > 8:
        lines.append("- ……還有 %d 處" % (len(found) - 8))
    return "\n".join(lines), cur


def gate_line_verify(vs):
    if vs is None:
        return "驗收機：沒有紀錄"
    label = "綠" if vs.get("green") else ("還在跑" if vs.get("pending") else "紅／沒有紀錄")
    extra = "" if vs.get("green") else "（%s）" % vs.get("why")
    if vs.get("green") and vs.get("verifier_change_approved"):
        extra = "（這一段改的是驗收機本身；David 手打「驗收機變更」同意用這個 commit 的結果）"
    if int(vs.get("attempt") or 1) > 1:                             # 重跑過：照實寫出來（算數的是最後那一次嘗試）
        extra += "（重跑過 %d 次；這是第 %d 次嘗試的結果）" % (int(vs["attempt"]) - 1, int(vs["attempt"]))
    return "驗收機：%s%s%s" % (label, extra, "（%s）" % vs["url"] if vs.get("url") else "")


def gate_line_external(ext, rows=None):
    if ext is None:
        return "外部審查（GPT）：沒有紀錄"
    mode = ext.get("mode")
    findings = ext.get("findings") or []
    major = [f for f in findings if f.get("severity") in ("P0", "P1")]
    rows = rows or {}
    adopted = len([f for f in findings if rows.get(f.get("id"), {}).get("response") == "adopt"])
    rejected = len([f for f in findings if rows.get(f.get("id"), {}).get("response") == "reject"])
    if mode == "waived":
        who = "免除（David 親手免除%s）" % ("；本階段未經外部審查" if not findings else "")
    elif mode == "codex":
        who = "Codex（沒有意見；它的進度留言寫這個 commit 審完了）" if ext.get("signal") == "B" else "Codex"
    else:
        who = "未完成（%s）" % (ext.get("why") or "?")
    pr = ext.get("pr_url") or ((ext.get("pr") or {}).get("url") if isinstance(ext.get("pr"), dict) else None)
    others = "；別人的留言 %d 則（忽略）" % len(ext.get("others") or []) if ext.get("others") else ""
    # 不採納的重大意見：把理由欄指到的 Cowork 裁決檔列出來（那一份算不算數，David 一眼看得出來）；延後的另外標出來
    cited = sorted(set("%s%s" % (rows[f["id"]]["ruling"], "（延後到 %s）" % rows[f["id"]]["deferred_to"] if rows[f["id"]].get("deferred_to") else "")
                       for f in major if rows.get(f.get("id"), {}).get("response") == "reject" and rows[f["id"]].get("ruling")))
    ruled = "（依 Cowork 裁決：%s）" % "、".join(cited) if cited else ""
    rounds = "；審了 %d 輪（上限 %d 輪）" % (ext["rounds"], ext.get("round_cap") or 0) if ext.get("rounds") else ""
    return "外部審查（GPT）：%s；重大 %d 條；採納 %d、不採納 %d%s%s%s%s" % (who, len(major), adopted, rejected, ruled, others, rounds, "（%s）" % pr if pr else "")
