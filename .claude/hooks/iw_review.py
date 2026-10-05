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
    if res.get("red"):
        return _vs(False, "驗收機的結果檔標紅：%s" % "；".join(res.get("reasons") or [])[:200], url=url, result=res)
    return _vs(True, "綠", url=url, result=res, ran=(res.get("tests") or {}).get("ran"), verifier_changed=bool(changed),
               verifier_change_approved=approved, changed_files=changed, first_time=first)


# ---------------------------------------------------------------- Codex（外部審查）

def severity_of(body, ext):
    m = re.search(ext.get("severityPattern") or r"\[?P([01])\]?", body or "")
    return "P%s" % m.group(1) if m else "P1"


def find_pr(main_root, cfg, branch, runner=None, hints=None):
    s = slug(main_root, cfg)
    if not s:
        return None, "看不出這個倉庫在 GitHub 的名字"
    owner = s.split("/")[0]
    obj, err = gh_json(["api", "repos/%s/pulls?head=%s:%s&state=open&per_page=5" % (s, owner, branch)], runner=runner, hints=hints)
    if obj is None:
        return None, err
    prs = obj if isinstance(obj, list) else []
    if not prs:
        return None, "找不到分支 %s 的 PR（還沒開？）" % branch
    pr = prs[0]
    return {"number": pr.get("number"), "url": pr.get("html_url"), "head": ((pr.get("head") or {}).get("sha") or "").lower(), "title": pr.get("title")}, None


def codex_status(main_root, cfg, pr_number, head, runner=None):
    ext = cfg["externalReview"]
    bot = ext["botLogin"]
    s = slug(main_root, cfg)
    hints = {"sha": head}
    revs, err1 = gh_json(["api", "repos/%s/pulls/%s/reviews?per_page=100" % (s, pr_number)], runner=runner, hints=hints)
    coms, err2 = gh_json(["api", "repos/%s/pulls/%s/comments?per_page=100" % (s, pr_number)], runner=runner, hints=hints)
    if revs is None or coms is None:
        return {"complete": False, "why": "查不到 PR 的審查（%s）" % (err1 or err2), "reviews": [], "findings": [], "others": [], "major": []}
    reviews, others = [], set()
    for r in (revs if isinstance(revs, list) else []):
        if (r.get("user") or {}).get("login") != bot:
            others.add((r.get("user") or {}).get("login") or "?")
            continue
        if r.get("state") == "PENDING":
            continue
        if str(r.get("commit_id") or "").lower() != head.lower():
            continue
        reviews.append(r)
    findings = []
    for c in (coms if isinstance(coms, list) else []):
        login = (c.get("user") or {}).get("login")
        if login != bot:
            others.add(login or "?")
            continue
        cid, oid = str(c.get("commit_id") or "").lower(), str(c.get("original_commit_id") or "").lower()
        if head.lower() not in (cid, oid):
            continue
        findings.append({"id": c.get("id"), "severity": severity_of(c.get("body") or "", ext), "path": c.get("path"),
                         "line": c.get("line") or c.get("original_line"), "excerpt": (c.get("body") or "")[:160]})
    return {"complete": bool(reviews),
            "why": None if reviews else "Codex（%s）還沒有針對 commit %s 發出 review" % (bot, head[:7]),
            "reviews": [{"id": r.get("id"), "state": r.get("state"), "body": (r.get("body") or "")[:200], "submitted_at": r.get("submitted_at")} for r in reviews],
            "findings": findings, "others": sorted(others), "major": [f for f in findings if f["severity"] in ("P0", "P1")]}


def parse_responses(path):
    """03_第三方審查.md 的表：| 留言 id | 等級 | 檔案:行 | 回覆 | 理由或修在哪 |。回傳 {id: {severity, response, reason}}。"""
    rows = {}
    try:
        text = io.open(path, encoding="utf-8").read()
    except Exception:                                              # noqa: B902
        return rows
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4 or not re.match(r"^\d+$", cells[0]):
            continue
        resp = cells[3]
        if "不採納" in resp:
            kind = "reject"
        elif "採納" in resp:
            kind = "adopt"
        else:
            kind = "other"
        rows[int(cells[0])] = {"severity": cells[1], "response": kind, "reason": cells[4] if len(cells) > 4 else ""}
    return rows


def responses_problems(findings, rows):
    """回傳 (沒回覆的 id, 被判不採納的重大意見)。"""
    missing = [f["id"] for f in findings if f.get("id") not in rows or rows[f["id"]]["response"] == "other"]
    rejected = [f for f in findings if f.get("severity") in ("P0", "P1") and rows.get(f.get("id"), {}).get("response") == "reject"]
    return missing, rejected


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
    cs = codex_status(main_root, cfg, pr["number"], head, runner=runner)
    out = {"mode": "codex" if cs["complete"] else "incomplete", "complete": cs["complete"], "why": cs.get("why"), "pr": pr,
           "pr_url": pr.get("url"), "findings": cs["findings"], "others": cs["others"], "major": cs["major"], "reviews": cs["reviews"]}
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
        who = "Codex"
    else:
        who = "未完成（%s）" % (ext.get("why") or "?")
    pr = ext.get("pr_url") or ((ext.get("pr") or {}).get("url") if isinstance(ext.get("pr"), dict) else None)
    others = "；別人的留言 %d 則（忽略）" % len(ext.get("others") or []) if ext.get("others") else ""
    return "外部審查（GPT）：%s；重大 %d 條；採納 %d、不採納 %d%s%s" % (who, len(major), adopted, rejected, others, "（%s）" % pr if pr else "")
