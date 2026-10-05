# -*- coding: utf-8 -*-
"""
iw_prepush.py — 第二道保護：git push 之前，看「實際要推上去的內容」。

第一道（iw_guard）看的是指令的文字，寫法千變萬化；這一道裝在 git 裡，不管指令怎麼寫、是誰下的，
只要是從這台電腦的這個倉庫推東西，git 自己會先來問。git 從標準輸入給我們每一個要更新的遠端 ref：
    <本機 ref> <本機 commit> <遠端 ref> <遠端現在的 commit>

規則：
  main      * 往回退、改寫、刪除：一律擋（有沒有通行證都一樣）
            * 有通行證、合併還沒上去：必須剛好是「以遠端現在的 main 為第一個 parent、放行的那個 commit 為第二個 parent」的合併
            * 有通行證、合併已經上去：只准再推一筆只改 README.md／docs/CHANGELOG.md 的 commit（合併後一小時內）
            * 沒有通行證：只放行筆電黃金排程的形狀——每個 commit 都不是合併、只動那五個檔、而且不是從 Claude Code 裡推的
  標籤      * 刪除、移動既有的：擋。新的標籤：放行
  其他分支  * 改寫歷史（強推）：擋。其他：放行

通行證使用時會回頭核對對話紀錄（那一則「放行 <階段>」是不是人打的）。
這支程式自己出錯時：在 Claude Code 裡＝擋；筆電排程或你自己的終端機＝放行（不可以把既有的排程弄壞）。
你自己要手動推程式：git push --no-verify（Claude 用這個旗標會被第一道擋下）。

結束碼：0＝可以推；10＝規則擋下。其他數字＝這支程式沒跑完（語法錯、少了檔……），
由 .git/hooks/pre-push 的入口決定：在 Claude Code 裡擋，其他情況放行。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import iw_common as C      # noqa: E402
    import iw_state as ST      # noqa: E402
    IMPORT_ERROR = None
except Exception as _e:        # noqa: B902   另外兩個檔壞掉時，下面的 main() 照「出錯」處理，不可以讓 Python 自己用結束碼 1 收場
    C = ST = None
    IMPORT_ERROR = _e

ZERO = "0" * 40
BLOCKED = 10


def has(repo, sha):
    rc, _ = C.git(["cat-file", "-e", sha + "^{commit}"], repo)
    return rc == 0


def is_ancestor(repo, a, b):
    rc, _ = C.git(["merge-base", "--is-ancestor", a, b], repo)
    return rc == 0


def parents(repo, sha):
    rc, out = C.git(["rev-list", "--parents", "-n", "1", sha], repo)
    return out.split()[1:] if rc == 0 and out else None


def new_commits(repo, old, new):
    rc, out = C.git(["rev-list", "%s..%s" % (old, new)], repo)
    return out.split() if rc == 0 else None


def files_of(repo, sha):
    rc, out = C.git(["-c", "core.quotepath=false", "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha], repo)
    return [f for f in out.split("\n") if f.strip()] if rc == 0 else None


def clean_merge_tree(repo, ours, theirs):
    """把 theirs「原封不動」合併進 ours 會得到的內容（樹的編號）。有衝突、或這台電腦的 git 算不出來，回 None。
    git merge-tree --write-tree 只算結果，不動任何分支與工作區。"""
    rc, out = C.git(["merge-tree", "--write-tree", ours, theirs], repo, timeout=90)
    if rc != 0 or not out:
        return None
    tree = out.split("\n")[0].strip()
    return tree if len(tree) >= 40 and all(ch in "0123456789abcdef" for ch in tree) else None


def all_plain(repo, shas, allowed=None, prefix=None):
    """每個 commit 都只有一個 parent，而且動到的檔都在 allowed 裡（或都以 prefix 開頭）。"""
    for sha in shas:
        ps = parents(repo, sha)
        files = files_of(repo, sha)
        if ps is None or len(ps) != 1 or not files:
            return False
        if allowed is not None and not set(files) <= allowed:
            return False
        if prefix is not None and not all(f.startswith(prefix) for f in files):
            return False
    return True


MAX_SCAN_COMMITS = 2000


def commit_message_problem(repo, main_root, cfg, lsha, rsha):
    """這次推送會公開的新 commit，訊息裡有沒有不該公開的東西（個人資料的字樣、權杖、信箱、本機路徑）。可以推回 None；不行回一句原因。
    commit 訊息一推上公開倉庫就收不回來，驗收機事後判紅也來不及（2026-10-06 Codex 的審查意見；原本推送前完全不看訊息）。
    用主目錄上的隱私掃描器（＝main 的版本）。列不出要推的 commit、讀不到掃描器（那會算成每一筆都有問題）、筆數多到看不完——一律擋。
    只在 Claude Code 裡的推送做（呼叫的人判斷）；筆電排程與 David 自己終端機的推送不經過這裡。"""
    import iw_notify as N      # noqa: E402   用到才載入：排程的推送不必付這個成本
    if rsha != ZERO and has(repo, rsha):
        rng = ["%s..%s" % (rsha, lsha)]
    else:                                                           # 新的分支或標籤：還不在遠端任何分支上的那些 commit
        rng = [lsha, "--not", "--remotes=%s" % cfg["remote"]]
    rc, out = C.git(["log", "--format=%H%x00%B%x01", "--max-count=%d" % (MAX_SCAN_COMMITS + 1)] + rng, repo, timeout=60)
    if rc != 0:
        return "列不出這次要推的 commit，沒辦法檢查 commit 訊息，先擋下（%s）。" % (out or "")[:80]
    bad, n = [], 0
    for chunk in out.split("\x01"):
        if "\x00" not in chunk:
            continue
        sha, msg = chunk.strip("\n").split("\x00", 1)
        n += 1
        problems = N.text_privacy_problems(main_root, msg)
        if problems:
            bad.append("%s（%s）" % (sha.strip()[:7], "、".join(problems)))
    if n > MAX_SCAN_COMMITS:
        return "這次要推的 commit 超過 %d 筆，沒辦法逐筆檢查訊息，先擋下。" % MAX_SCAN_COMMITS
    if bad:
        return ("commit 訊息裡有不該公開的東西——一推上公開倉庫就收不回來：%s。請把那幾筆的訊息改掉再推"
                "（訊息裡不要放個人資料、權杖、電子郵件、本機的絕對路徑）。" % "；".join(bad[:8]))
    return None


def verify_gate(main_root, sd, cfg, stage, sha, now):
    """P2 第 3 節：合併前再查一次驗收機（放行時已查過一次）。設定裡沒有驗收機（舊版）就不查；查不到＝不綠＝擋。"""
    if not (cfg.get("verify") and cfg.get("externalReview")):
        return None
    try:
        import iw_review as R
        vs = R.verify_status(main_root or C.main_root(), sd, cfg, stage, sha, now=now)
    except Exception as e:                                          # noqa: B902
        return "查驗收機時出錯（%r）" % (e,)
    return None if vs.get("green") else vs.get("why")


def protected_push_problem(repo, lsha, st, cfg):
    """自動駕駛期間，推上去的東西（分支或標籤）相對於正式版不可以動到流程檔、驗收程式、突變與已知例外清單、AGENTS.md（2026-10-05 裁決二）。
    啟動之前、David 在場時就改好、而且內容到現在沒變的不算（跟第一道同一條規則）。可以推回 None；不行回一句原因。"""
    pats = (cfg.get("verify") or {}).get("protected") or []
    if not pats or not st.get("active"):
        return None
    base = "refs/remotes/%s/%s" % (cfg["remote"], cfg["mainBranch"])
    rc, out = C.git(["-c", "core.quotepath=false", "diff", "--name-only", "%s...%s" % (base, lsha)], repo, timeout=60)
    if rc != 0:
        return "查不出這次推送相對於正式版動了哪些檔；自動駕駛期間先不推。"
    files = [f for f in out.split("\n") if f.strip() and C.glob_match(f.strip(), pats)]
    pre = st.get("preexisting") or {}
    blobs = C.head_blobs(repo, lsha) or {}
    hot = [f for f in files if not (f in pre and blobs.get(f, "(deleted)") == pre[f])]
    if hot:
        return ("自動駕駛期間不能推動到流程檔、驗收程式、突變與已知例外清單或 AGENTS.md 的東西（%s）。這是停止條件 3：寫停止報告、寄信，由 David 決定。"
                % "、".join(hot[:6]))
    return None


def foreign_ok(st, rref, rsha):
    """P2 第 2 節：feat 分支的遠端頭要是自己上一次推的（任何一次都算）；第一次推、或還沒有紀錄就放行。"""
    pushes = (st.get("branch_pushes") or {}).get(rref) or []
    return (not pushes) or rsha in pushes


def remember_push(sd, rref, lsha):
    def fn(s):
        lst = s.setdefault("branch_pushes", {}).setdefault(rref, [])
        if lsha not in lst:
            lst.append(lsha)
        del lst[:-30]
    ST.update(sd, fn)


def mark_foreign(main_root, sd, cfg, st, rref, rsha):
    """分支上出現不是自己推的 commit：記下來；自動駕駛中立刻暫停（等級「要你決定」）、寄短信。"""
    now = C.iso()
    detail = "遠端的 %s 上有不是你推的 commit（%s）" % (rref[11:], rsha[:7])

    def fn(s):
        s["foreign_commit"] = {"ref": rref, "sha": rsha, "at": now}
        if s.get("active") and not s.get("pause"):
            s["pause"] = {"reason": "foreign-commit", "detail": detail, "code": None, "at": now, "retries": 0}
    ST.update(sd, fn)
    ST.log(sd, {"event": "foreign-commit", "ref": rref, "sha": rsha[:12], "active": bool(st.get("active"))})
    if st.get("active"):
        try:
            import iw_notify as N
            N.fallback(main_root or C.main_root(), sd, cfg, detail + "。有人（Codex？別的帳號？）動了這個分支，自動駕駛已暫停；請看 PR，確認沒事之後輸入「繼續 %s」。"
                       % st.get("stage"), min_gap_key="foreign")
        except Exception:                                           # noqa: B902
            pass


def check_main(repo, lsha, rsha, st, cfg, environ, now, sd, main_root=None):
    """回傳 (可以推, 白話說明)。"""
    if lsha == ZERO:
        return False, "不能刪除遠端的 main。"
    if rsha == ZERO or not has(repo, rsha):
        return False, "遠端的 main 有這台電腦還沒有的 commit。先 git fetch 再試。"
    if not is_ancestor(repo, rsha, lsha):
        return False, "這個推送會讓 main 往回退或改寫它的歷史，不允許。"
    commits = new_commits(repo, rsha, lsha)
    if commits is None:
        return False, "查不出要推上去的是哪些 commit。"
    if not commits:
        return True, "沒有新的 commit。"
    in_claude = C.in_claude_session(environ)
    gold = set(cfg["goldJob"]["files"])
    if all_plain(repo, commits, allowed=gold) and not in_claude:
        return True, "筆電黃金排程的資料推送（只動 %d 個指定的檔）。" % len(gold)

    cred = st.get("credential") or {}
    cand = cred.get("candidate")
    stage = cred.get("stage") or st.get("stage") or "<階段>"
    att = (cred.get("merge") or cred.get("merge_attempt") or {}).get("sha")
    landed = bool(att) and has(repo, att) and is_ancestor(repo, att, rsha)
    view = dict(st)
    c2 = dict(cred)
    view["credential"] = c2
    if not landed:
        c2["merge"] = None
        problem = ST.credential_problem(view, cfg, "merge", now) if cred else "沒有通行證"
        if problem:
            return False, "推上 main 要 David 親手輸入「放行 %s」。（%s）" % (stage, problem)
        ps = parents(repo, lsha)
        if ps is None or len(ps) != 2 or ps[0] != rsha or ps[1] != cand:
            return False, ("通行證只准推「以現在的 main（%s）為底、把放行的 commit（%s）用 --no-ff 合併進來」的那一個合併；這次要推的不是它。"
                           "（main 剛好又有新的資料 commit 的話：以新的 origin/main 為底重做一次合併。）" % (rsha[:7], (cand or "?")[:7]))
        # 兩個 parent 對了還不夠：合併 commit 自己可以夾帶別的改動。內容必須跟「原封不動合併」算出來的一模一樣。
        want = clean_merge_tree(repo, rsha, cand)
        rc, got = C.git(["rev-parse", "-q", "--verify", lsha + "^{tree}"], repo)
        if want is None:
            return False, ("把放行的 commit（%s）合併進現在的 main 會有衝突（或這台電腦的 git 算不出合併結果）。"
                           "有衝突的合併不能自己解了就推：停下來寄信，由 David 決定。" % cand[:7])
        if rc != 0 or got != want:
            return False, ("這個合併 commit 的內容，跟「把放行的 commit（%s）原封不動合併進現在的 main」算出來的不一樣"
                           "——合併的時候多改了東西。請重做一次乾淨的合併（git merge --no-ff，不要再動任何檔）。" % cand[:7])
        problem = verify_gate(main_root, sd, cfg, stage, cand, now)
        if problem:
            return False, "合併前再查一次驗收機：%s。查不到也算不綠，不能推。" % problem

        def fn(s):
            if s.get("credential"):
                s["credential"]["merge_attempt"] = {"sha": lsha, "at": C.iso(now)}
        ST.update(sd, fn)
        return True, "David 放行的合併（%s）。" % lsha[:7]

    if not c2.get("merge"):
        c2["merge"] = dict(cred.get("merge_attempt") or {})
    problem = ST.credential_problem(view, cfg, "docs", now)
    if problem:
        return False, "合併已經推上去了；再推 main 要有效的通行證。（%s）" % problem
    between = new_commits(repo, att, rsha)
    if between is None or not all_plain(repo, between, prefix=cfg["cloudJob"]["pathPrefix"]):
        return False, "合併之後 main 上有不是資料更新的變更，文件那一筆不能這樣推。"
    ps = parents(repo, lsha)
    files = files_of(repo, lsha)
    if len(commits) != 1 or ps is None or len(ps) != 1 or ps[0] != rsha or not files or not set(files) <= set(cfg["docsFollowupFiles"]):
        return False, "通行證的第二次推送只能是一筆、只改 %s 的 commit。" % "、".join(cfg["docsFollowupFiles"])

    def fn2(s):
        if s.get("credential"):
            s["credential"]["docs_attempt"] = {"sha": lsha, "at": C.iso(now)}
    ST.update(sd, fn2)
    return True, "合併後的那一筆文件（%s）。" % lsha[:7]


def check(lines, repo, environ=None, now=None, main_root=None, cfg=None):
    """回傳 (可以推, [訊息])。"""
    environ = os.environ if environ is None else environ
    now = now or C.now()
    main_root = main_root or C.main_root(repo)
    sd = os.path.join(C.common_dir(repo), C.STATE_DIR_NAME)
    cfg = cfg or C.config(os.path.join(main_root, ".claude", "autopilot", "config.json"))
    st = ST.load(sd)
    main_ref = "refs/heads/" + cfg["mainBranch"]
    ok_all, msgs = True, []
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        lref, lsha, rref, rsha = parts
        ok, why = True, None
        msg_problem = None
        if lsha != ZERO and C.in_claude_session(environ) and not rref.startswith("refs/remotes/"):
            # 先看 commit 訊息（分支、標籤、main 都看）：排在通行證的檢查之前，訊息有問題就不會白白用掉一張通行證
            msg_problem = commit_message_problem(repo, main_root, cfg, lsha, rsha)
        if msg_problem:
            ok, why = False, msg_problem
        elif rref.startswith("refs/tags/"):
            if lsha == ZERO:
                ok, why = False, "不能刪除遠端的標籤 %s（停止條件 4）。" % rref[10:]
            elif rsha != ZERO and rsha != lsha:
                ok, why = False, "不能移動既有的標籤 %s（停止條件 4）。" % rref[10:]
            else:
                problem = protected_push_problem(repo, lsha, st, cfg)
                if problem:
                    ok, why = False, problem
        elif rref == main_ref:
            ok, why = check_main(repo, lsha, rsha, st, cfg, environ, now, sd, main_root)
            st = ST.load(sd)
        elif rref.startswith("refs/remotes/"):
            ok, why = False, "把東西推進 refs/remotes/（用來記遠端位置的記號）不是正常的推送，不允許。"
        elif rref.startswith("refs/heads/"):
            if lsha != ZERO and rsha != ZERO:
                ok_foreign = foreign_ok(st, rref, rsha)
                if not ok_foreign:
                    ok, why = False, ("遠端的 %s 上有不是你推的 commit（%s）。有人（Codex？別的帳號？）動了這個分支：停下來寄「要你決定」的信，"
                                      "不要覆蓋、不要合併它。" % (rref[11:], rsha[:7]))
                    mark_foreign(main_root, sd, cfg, st, rref, rsha)
                    st = ST.load(sd)
                elif not has(repo, rsha):
                    ok, why = False, "遠端的 %s 有這台電腦還沒有的 commit。先 git fetch 再試。" % rref[11:]
                elif not is_ancestor(repo, rsha, lsha):
                    ok, why = False, "這個推送會改寫遠端 %s 的歷史（強推），不允許。" % rref[11:]
            if ok and lsha != ZERO:
                problem = protected_push_problem(repo, lsha, st, cfg)
                if problem:
                    ok, why = False, problem
            if ok and lsha != ZERO:
                remember_push(sd, rref, lsha)
        ST.log(sd, {"event": "prepush", "ref": rref, "local": lsha[:12], "remote": rsha[:12], "ok": ok, "why": why,
                    "claude": C.in_claude_session(environ)})
        if not ok:
            ok_all = False
            msgs.append("iw pre-push：擋下 %s —— %s" % (rref, why))
        elif why and rref == main_ref:
            msgs.append("iw pre-push：%s" % why)
    return ok_all, msgs


def say(text):
    try:
        sys.stderr.buffer.write((text + "\n").encode("utf-8", errors="replace"))
        sys.stderr.buffer.flush()
    except Exception:                                               # noqa: B902
        pass


def main(argv=None):
    repo = os.getcwd()
    in_claude = bool(os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_CHILD_SESSION"))
    try:
        if IMPORT_ERROR is not None:
            raise IMPORT_ERROR
        lines = sys.stdin.read().splitlines()
        ok, msgs = check(lines, repo)
    except Exception as e:                                          # noqa: B902
        if in_claude:
            say("iw pre-push：檢查程式出錯（%r），先擋下這次推送。請把這段訊息原樣告訴 David。" % (e,))
            return BLOCKED
        say("iw pre-push：檢查程式出錯（%r）。這不是從 Claude Code 推的，照常放行。" % (e,))
        return 0
    for m in msgs:
        say(m)
    return 0 if ok else BLOCKED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
