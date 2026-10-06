#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_autopilot_prepush.py — 停點 P1「自動駕駛」：第二道保護（推送前的檢查，.claude/hooks/iw_prepush.py）

跑法：python -m unittest discover -s scripts -p "test_*.py"
全部離線：測試自己在暫存資料夾建一個假的「遠端」（bare 倉庫）與一個假的主目錄，真的下 git push。不碰真的倉庫、不連網。

釘住的事：
  1. 筆電黃金排程形狀的推送（只動那五個檔、不是合併、不是從 Claude Code 裡推的）不需要通行證——既有排程一字不改也照跑。
  2. 同樣的推送只要夾一個別的檔、或從 Claude Code 裡推、或是合併，就要通行證。
  3. 有通行證：只准「以現在的 main 為底、把放行的那個 commit 用 --no-ff 合併進來」；其他形狀都擋。
     之後只准再推一筆只改 README.md／docs/CHANGELOG.md 的 commit。
  4. 往回退、強推、刪或移動標籤：有沒有通行證都擋。
  5. 邏輯檔不在（P1 被退回）時入口自動放行；檢查程式自己出錯時，Claude Code 裡＝擋、排程＝放行。
"""
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

os.environ["IW_TEST_NO_SIDE_EFFECTS"] = "1"      # 測試不對外寄信、不在桌面跳通知；用子程序跑的 hook 也會繼承
# 沙盒裡的 hook 是用子程序跑的（git push 會叫到 pre-push）：Python 預設會在沙盒主目錄的 .claude/hooks/ 留下 __pycache__/，
# 「主目錄要乾淨」那一關就會擋。正式倉庫的 .gitignore 有 __pycache__/（test_autopilot_config 釘住），沙盒的沒有，所以這裡關掉。
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".claude", "hooks"))
sys.path.insert(0, HERE)
import iw_common as C            # noqa: E402
import iw_prepush as P           # noqa: E402
import iw_state as ST            # noqa: E402
import autopilot_install as INST  # noqa: E402

NOW = datetime.datetime(2026, 10, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
GOLD_SUBJECT = "data: 2026-10-01 10:00 盤中輕量更新（本機：台銀黃金現價）"


def run_git(args, cwd, env=None, check=True):
    e = dict(os.environ)
    for k in list(e.keys()):
        if k.startswith("GIT_") or k in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION"):
            e.pop(k)
    e.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
              "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_TERMINAL_PROMPT": "0"})
    if env:
        e.update(env)
    p = subprocess.run(["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
    out = p.stdout.decode("utf-8", "replace").strip()
    err = p.stderr.decode("utf-8", "replace").strip()
    if check and p.returncode != 0:
        raise AssertionError("git %s 失敗（%d）：%s" % (" ".join(args), p.returncode, err))
    return p.returncode, out, err


BOT_LOGIN = "chatgpt-codex-connector[bot]"
FAKE_GH_ENV = "IW_TEST_FAKE_GH"


def fake_gh_entries(green=True, runs=True, pending=False, result_commit="{sha}", red_reasons=None, pr=True, codex=True, review_commit="{sha}",
                    comments=None, others=None, ran=830, event="push", path=".github/workflows/verify.yml", extra_runs=None, review_body=None, issue_comments=None,
                    attempt=1, result_attempt=None):
    """IW_TEST_FAKE_GH 的內容（P2）：gh 的回答，{sha} 會換成查詢的 commit。預設＝驗收機綠、Codex 已審、0 條意見。
    沙盒裡的 pre-push 是另一個程序，所以用檔案＋環境變數，不用 monkeypatch。
    event／path：那一次執行是怎麼觸發的、流程檔在哪（只認 push＋.github/workflows/verify.yml）。extra_runs：同一個 commit 的其他執行。
    attempt：那一次執行現在是第幾次嘗試（重跑過就大於 1）；result_attempt：結果檔是第幾次嘗試寫的（預設跟 attempt 一樣）。"""
    run = {"id": 1, "status": "in_progress" if pending else "completed", "conclusion": None if pending else ("success" if green else "failure"),
           "head_sha": "{sha}", "html_url": "https://example.invalid/actions/runs/1", "run_attempt": attempt, "created_at": "2026-10-04T00:00:00Z",
           "event": event, "path": path}
    result = {"commit": result_commit, "red": bool(red_reasons), "reasons": red_reasons or [], "run_attempt": str(result_attempt or attempt),
              "tests": {"ran": ran, "defined": ran, "passed": ran, "failed": [], "errors": [], "skipped": []}, "egress": {"bot_hits": 0}, "schema": 1}
    reviews = []
    if codex:
        reviews.append({"id": 1, "user": {"login": BOT_LOGIN}, "state": "COMMENTED", "commit_id": review_commit,
                        "body": review_body or "Codex Review: Didn't find any major issues.", "submitted_at": "2026-10-04T00:10:00Z"})
    for login in (others or []):
        reviews.append({"id": 90 + len(reviews), "user": {"login": login}, "state": "COMMENTED", "commit_id": "{sha}", "body": "drive-by",
                        "submitted_at": "2026-10-04T00:11:00Z"})
    prs = [{"number": 7, "html_url": "https://example.invalid/pull/7", "head": {"sha": "{sha}"}, "title": "x", "created_at": "2026-10-03T23:00:00Z"}] if pr else []
    return [{"match": "actions/workflows/verify.yml/runs", "rc": 0, "text": json.dumps({"workflow_runs": ([run] if runs else []) + list(extra_runs or [])})},
            {"match": "run download", "rc": 0, "text": json.dumps(result)},
            {"match": "pulls?head=", "rc": 0, "text": json.dumps(prs)},
            {"match": "/pulls/7/reviews", "rc": 0, "text": json.dumps(reviews)},
            {"match": "/pulls/7/comments", "rc": 0, "text": json.dumps(comments or [])},
            {"match": "/issues/7/comments", "rc": 0, "text": json.dumps(issue_comments or [])},        # PR 的一般留言（Codex 的進度留言在這裡）
            {"match": "pr comment", "rc": 0, "text": "https://example.invalid/pull/7#issuecomment-1"},
            {"match": "pr create", "rc": 0, "text": "https://example.invalid/pull/7"},
            {"match": "pr edit", "rc": 0, "text": ""}]


def write_fake_gh(path, entries):
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False)
    return path


class Sandbox(object):
    """暫存資料夾裡的一個假遠端＋假主目錄（main）＋功能分支的 worktree。"""

    def __init__(self, tracked=False):
        """tracked=False：保護檔在主目錄裡是被忽略的檔（P1 合併之前的樣子）。
        tracked=True：保護檔已經在 main 上（合併之後的樣子）。"""
        self.tracked = tracked
        self.tmp = tempfile.mkdtemp(prefix="iw-prepush-")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.main = os.path.join(self.tmp, "main")
        self.wt = os.path.join(self.main, ".claude", "worktrees", "stopX1")
        self.fake_gh = write_fake_gh(os.path.join(self.tmp, "fake-gh.json"), fake_gh_entries())      # P2：驗收機與 Codex 的假回答（預設全綠）
        os.environ[FAKE_GH_ENV] = self.fake_gh
        run_git(["init", "-q", "--bare", "-b", "main", self.remote], self.tmp)
        run_git(["init", "-q", "-b", "main", self.main], self.tmp)
        for k, v in (("core.autocrlf", "false"), ("commit.gpgsign", "false"), ("core.quotepath", "false")):
            run_git(["config", k, v], self.main)
        self.write("README.md", "readme\n")
        self.write("docs/CHANGELOG.md", "changelog\n")
        self.write("js/app.js", "// app\n")
        self.write(".gitignore", ".autopilot/\n.claude/worktrees/\n" if tracked else ".claude/\n.autopilot/\n")
        os.makedirs(os.path.join(self.main, "scripts"), exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, "scripts", "test_analysis_guards.py"), os.path.join(self.main, "scripts", "test_analysis_guards.py"))   # PR 文字的隱私掃描要用
        if tracked:
            self.copy_protection()
        for f in C.load_json(os.path.join(ROOT, ".claude", "autopilot", "config.json"))["goldJob"]["files"]:
            self.write(f, "{}\n")
        self.write("data/history/btc.json", "{}\n")
        self.commit("init")
        run_git(["remote", "add", "origin", self.remote], self.main)
        run_git(["push", "-q", "origin", "main"], self.main)
        if not tracked:
            self.copy_protection()          # 被忽略的檔：跟 P1 合併之前的真實情況一樣
        INST.install_prepush(self.main, quiet=True)
        self.base = self.rev("HEAD")
        self.sd = os.path.join(self.main, ".git", C.STATE_DIR_NAME)
        run_git(["worktree", "add", "-q", "--no-track", "-b", "feat/stopX1", self.wt, "origin/main"], self.main)
        self.write("js/app.js", "// app v2\n", self.wt)
        self.commit("feat: x", self.wt)
        self.cand = self.rev("HEAD", self.wt)
        run_git(["tag", "stopX1"], self.wt)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def copy_protection(self):
        for sub in ("hooks", "autopilot", "agents", "skills"):
            src = os.path.join(ROOT, ".claude", sub)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(self.main, ".claude", sub), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(os.path.join(ROOT, ".claude", "settings.json"), os.path.join(self.main, ".claude", "settings.json"))

    def write(self, rel, text, root=None):
        p = os.path.join(root or self.main, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def commit(self, msg, root=None, files=None):
        root = root or self.main
        run_git(["add", "-A"] if files is None else ["add", "--"] + files, root)
        run_git(["commit", "-q", "-m", msg], root)
        return self.rev("HEAD", root)

    def rev(self, ref, root=None):
        return run_git(["rev-parse", ref], root or self.main)[1]

    def reset(self):
        """回到一開始：遠端 main、主目錄、狀態都還原。"""
        run_git(["update-ref", "refs/heads/main", self.base], self.remote)
        run_git(["checkout", "-q", "main"], self.main)
        run_git(["reset", "-q", "--hard", self.base], self.main)
        run_git(["clean", "-fdq", "--", ".claude"], self.main)             # 測試丟進去的多餘檔（worktrees 是被忽略的，不會被清掉）
        run_git(["update-ref", "refs/remotes/origin/main", self.base], self.main)
        shutil.rmtree(self.sd, ignore_errors=True)
        mw = os.path.join(self.main, ".claude", "worktrees", "merge")
        if os.path.isdir(mw):
            run_git(["worktree", "remove", "--force", mw], self.main)

    def land(self, sha, repo=None):
        """測試準備情境用：直接讓遠端的 main 變成 sha（跳過檢查），本機的 origin/main 也跟上。"""
        run_git(["push", "-q", "--no-verify", "--force", "origin", "%s:refs/heads/main" % sha], repo or self.main)
        run_git(["update-ref", "refs/remotes/origin/main", sha], self.main)

    def transcript(self, prompt_id="p-1", text="放行 X1", human=True):
        p = os.path.join(self.tmp, "transcript-%s.jsonl" % prompt_id)
        with io.open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "promptId": prompt_id, "message": {"role": "user", "content": text},
                                 "origin": {"kind": "human" if human else "task-notification"}}, ensure_ascii=False) + "\n")
        return p

    def credential(self, **kw):
        cred = {"stage": "X1", "candidate": self.cand, "tag_sha": self.cand, "issued_at": C.iso(C.now()),
                "expires_at": C.iso(C.now() + datetime.timedelta(hours=23)), "prompt_id": "p-1", "transcript_path": self.transcript(),
                "session_id": "S1", "merge": None, "docs": None, "docs_window_from": None}
        cred.update(kw)
        st = {"version": 1, "active": False, "stage": "X1", "status": "approved", "credential": cred}
        ST.save(self.sd, st)
        return cred

    def merge_worktree(self, branch="feat/stopX1", no_ff=True):
        mw = os.path.join(self.main, ".claude", "worktrees", "merge")
        run_git(["worktree", "add", "-q", "--detach", mw, "origin/main"], self.main)
        run_git(["merge", "-q", "--no-ff" if no_ff else "--ff", "-m", "Merge", branch], mw)
        return mw

    def push(self, cwd, args, claude):
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1"} if claude else {}
        rc, out, err = run_git(["push"] + list(args), cwd, env=env, check=False)
        return rc, err

    def lines(self, local_sha, remote_ref="refs/heads/main", remote_sha=None, local_ref="HEAD"):
        return ["%s %s %s %s" % (local_ref, local_sha, remote_ref, remote_sha or self.base)]

    def check(self, lines, repo=None, claude=True, now=None):
        env = {"CLAUDECODE": "1"} if claude else {}
        return P.check(lines, repo or self.main, environ=env, now=now or C.now())


class TestPrePush(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox()

    @classmethod
    def tearDownClass(cls):
        cls.sb.close()

    def setUp(self):
        self.sb.reset()

    def gold_commit(self, extra=None, subject=GOLD_SUBJECT):
        sb = self.sb
        sb.write("data/latest.json", '{"t": "%s"}\n' % subject)
        sb.write("data/sources/local.json", '{"t": 1}\n')
        sb.write("data/history/gold_twd.json", '{"t": 1}\n')
        for rel in (extra or []):
            sb.write(rel, "changed\n")
        return sb.commit(subject)

    # ---- 1. 黃金排程
    def test_the_gold_job_push_needs_no_credential(self):
        """真的 push：筆電排程的形狀（不是從 Claude Code 裡推的）要通過。對照組：把例外放寬或拿掉 → 相關測試會紅。"""
        self.gold_commit()
        rc, err = self.sb.push(self.sb.main, ["origin", "HEAD:main"], claude=False)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.sb.rev("refs/heads/main", self.sb.remote), self.sb.rev("HEAD"))

    def test_two_gold_commits_in_a_row_also_pass(self):
        self.gold_commit()
        sha = self.gold_commit(subject="data: 2026-10-01 本機補抓（台銀黃金）")
        ok, msgs = self.sb.check(self.sb.lines(sha), claude=False)
        self.assertTrue(ok, msgs)

    def test_the_same_push_from_inside_claude_code_needs_a_credential(self):
        self.gold_commit()
        rc, err = self.sb.push(self.sb.main, ["origin", "HEAD:main"], claude=True)
        self.assertNotEqual(rc, 0)
        self.assertIn("放行", err)
        self.assertEqual(self.sb.rev("refs/heads/main", self.sb.remote), self.sb.base)                 # 遠端沒有被動到

    def test_gold_shape_with_one_extra_file_is_blocked(self):
        for extra in ("js/app.js", "data/history/btc.json", "data/assets.json", "README.md", "scripts/x.py"):
            self.sb.reset()
            sha = self.gold_commit(extra=[extra])
            ok, msgs = self.sb.check(self.sb.lines(sha), claude=False)
            self.assertFalse(ok, extra)

    def test_a_merge_of_data_is_not_the_gold_job(self):
        sb = self.sb
        run_git(["checkout", "-q", "-b", "side"], sb.main)
        sb.write("data/latest.json", '{"side": 1}\n')
        sb.commit(GOLD_SUBJECT)
        run_git(["checkout", "-q", "main"], sb.main)
        run_git(["merge", "-q", "--no-ff", "-m", GOLD_SUBJECT, "side"], sb.main)
        ok, msgs = sb.check(sb.lines(sb.rev("HEAD")), claude=False)
        run_git(["branch", "-D", "side"], sb.main)
        self.assertFalse(ok)

    def test_a_code_commit_is_blocked_everywhere(self):
        sb = self.sb
        sb.write("js/app.js", "// hotfix\n")
        sb.commit("fix: straight to main")
        for claude in (True, False):
            rc, err = sb.push(sb.main, ["origin", "HEAD:main"], claude=claude)
            self.assertNotEqual(rc, 0, "claude=%s" % claude)
        self.assertEqual(sb.rev("refs/heads/main", sb.remote), sb.base)

    def test_other_spellings_reach_the_same_check(self):
        """第二道不看指令怎麼寫：這些寫法推的是同一個東西，一樣被擋。"""
        sb = self.sb
        sb.write("js/app.js", "// hotfix\n")
        sb.commit("fix: straight to main")
        for args in (["origin", "main"], ["origin", "HEAD:refs/heads/main"], ["origin"], [], [sb.remote, "HEAD:main"]):
            rc, err = sb.push(sb.main, args, claude=True)
            self.assertNotEqual(rc, 0, args)
        rc, err = sb.push(sb.wt, ["origin", "feat/stopX1:main"], claude=True)
        self.assertNotEqual(rc, 0)
        self.assertEqual(sb.rev("refs/heads/main", sb.remote), sb.base)

    # ---- 2. 有通行證
    def test_the_approved_merge_goes_through_for_real(self):
        """正向測試（規格第 3 節）：David 放行之後，合併可以通過。"""
        sb = self.sb
        sb.credential()
        mw = sb.merge_worktree()
        rc, err = sb.push(mw, ["origin", "HEAD:main"], claude=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(sb.rev("refs/heads/main", sb.remote), sb.rev("HEAD", mw))
        st = ST.load(sb.sd)
        self.assertEqual(st["credential"]["merge_attempt"]["sha"], sb.rev("HEAD", mw))

    def test_only_the_exact_merge_shape_is_accepted(self):
        sb = self.sb
        sb.credential()
        ok, _ = sb.check(sb.lines(sb.cand))                                           # 直接快轉（沒有合併 commit）
        self.assertFalse(ok)
        mw = sb.merge_worktree()
        sb.write("js/app.js", "// extra\n", mw)
        extra = sb.commit("extra on top of the merge", mw)
        ok, _ = sb.check(sb.lines(extra), repo=mw)                                    # 合併之後又多一筆
        self.assertFalse(ok)
        sb.reset()
        sb.credential(candidate="1" * 40)                                              # 通行證指的是別的 commit
        mw = sb.merge_worktree()
        ok, _ = sb.check(sb.lines(sb.rev("HEAD", mw)), repo=mw)
        self.assertFalse(ok)

    def test_the_merge_must_sit_on_the_current_main(self):
        """main 在合併之後又多了一筆資料：舊的合併不能推（要以新的 main 為底重做）；重做的那一個可以。"""
        sb = self.sb
        sb.credential()
        mw = sb.merge_worktree()
        old_merge = sb.rev("HEAD", mw)
        data = self.gold_commit()
        sb.land(data, mw)
        ok, msgs = sb.check(sb.lines(old_merge, remote_sha=data), repo=mw)
        self.assertFalse(ok)
        run_git(["checkout", "-q", "--detach", "origin/main"], mw)
        run_git(["merge", "-q", "--no-ff", "-m", "Merge again", "feat/stopX1"], mw)
        ok, msgs = sb.check(sb.lines(sb.rev("HEAD", mw), remote_sha=data), repo=mw)
        self.assertTrue(ok, msgs)

    def test_a_failed_push_can_be_retried_but_a_landed_merge_cannot_be_repeated(self):
        sb = self.sb
        sb.credential()
        mw = sb.merge_worktree()
        merge = sb.rev("HEAD", mw)
        ok, _ = sb.check(sb.lines(merge), repo=mw)
        self.assertTrue(ok)
        ok, _ = sb.check(sb.lines(merge), repo=mw)                                    # 同一個合併再推一次（上一次沒上去）
        self.assertTrue(ok)
        sb.land(merge, mw)
        sb.write("js/app.js", "// another\n", sb.wt)
        other = sb.commit("another", sb.wt)
        run_git(["checkout", "-q", "--detach", merge], mw)
        run_git(["merge", "-q", "--no-ff", "-m", "second merge", other], mw)
        ok, msgs = sb.check(sb.lines(sb.rev("HEAD", mw), remote_sha=merge), repo=mw)
        self.assertFalse(ok, "合併已經用過了，不能再合併別的東西")

    def test_docs_follow_up(self):
        sb = self.sb
        mw = sb.merge_worktree()
        merge = sb.rev("HEAD", mw)
        sb.land(merge, mw)
        sb.credential(merge={"sha": merge, "at": C.iso(C.now())})
        sb.write("README.md", "readme + rollback row\n", mw)
        sb.write("docs/CHANGELOG.md", "changelog + merge record\n", mw)
        docs = sb.commit("docs: merge record", mw)
        rc, err = sb.push(mw, ["origin", "HEAD:main"], claude=True)                  # 真的推
        self.assertEqual(rc, 0, err)
        self.assertEqual(ST.load(sb.sd)["credential"]["docs_attempt"]["sha"], docs)
        # 第二筆文件：不行
        sb.credential(merge={"sha": merge, "at": C.iso(C.now())}, docs={"sha": docs, "at": C.iso(C.now())})
        sb.write("README.md", "again\n", mw)
        again = sb.commit("docs: again", mw)
        ok, _ = sb.check(sb.lines(again, remote_sha=docs), repo=mw)
        self.assertFalse(ok)

    def test_docs_follow_up_must_be_docs_only_single_and_in_time(self):
        sb = self.sb
        mw = sb.merge_worktree()
        merge = sb.rev("HEAD", mw)
        sb.land(merge, mw)
        sb.credential(merge={"sha": merge, "at": C.iso(C.now())})
        sb.write("README.md", "x\n", mw)
        sb.write("js/app.js", "// sneaked in\n", mw)
        bad = sb.commit("docs: plus code", mw)
        ok, _ = sb.check(sb.lines(bad, remote_sha=merge), repo=mw)
        self.assertFalse(ok, "文件那一筆夾了程式")
        run_git(["reset", "-q", "--hard", merge], mw)
        sb.write("README.md", "a\n", mw)
        sb.commit("docs: one", mw)
        sb.write("docs/CHANGELOG.md", "b\n", mw)
        two = sb.commit("docs: two", mw)
        ok, _ = sb.check(sb.lines(two, remote_sha=merge), repo=mw)
        self.assertFalse(ok, "文件只能一筆")
        run_git(["reset", "-q", "--hard", merge], mw)
        sb.write("README.md", "a\n", mw)
        one = sb.commit("docs: one", mw)
        late = C.now() + datetime.timedelta(minutes=61)
        ok, _ = sb.check(sb.lines(one, remote_sha=merge), repo=mw, now=late)
        self.assertFalse(ok, "超過時間")
        ok, msgs = sb.check(sb.lines(one, remote_sha=merge), repo=mw)
        self.assertTrue(ok, msgs)

    def test_docs_follow_up_survives_a_data_commit_in_between(self):
        """合併推上去之後、文件推上去之前，雲端剛好推了一筆資料（真的發生過）：文件那一筆接在資料後面，照樣可以。"""
        sb = self.sb
        mw = sb.merge_worktree()
        merge = sb.rev("HEAD", mw)
        sb.write("data/history/btc.json", '{"cloud": 1}\n', mw)
        data = sb.commit("data: 2026-10-01 10:40 盤中更新（雲端）", mw)
        sb.land(data, mw)
        sb.credential(merge={"sha": merge, "at": C.iso(C.now())})
        sb.write("README.md", "row\n", mw)
        docs = sb.commit("docs: merge record", mw)
        ok, msgs = sb.check(sb.lines(docs, remote_sha=data), repo=mw)
        self.assertTrue(ok, msgs)
        run_git(["reset", "-q", "--hard", merge], mw)
        sb.write("js/app.js", "// someone pushed code\n", mw)
        code = sb.commit("fix: unapproved", mw)
        sb.write("README.md", "row\n", mw)
        docs2 = sb.commit("docs: merge record", mw)
        ok, _ = sb.check(sb.lines(docs2, remote_sha=code), repo=mw)
        self.assertFalse(ok, "合併之後 main 上有程式變更")

    def test_bad_credentials(self):
        sb = self.sb
        mw = sb.merge_worktree()
        merge = sb.rev("HEAD", mw)
        for kw, why in (({"expires_at": C.iso(C.now() - datetime.timedelta(minutes=1))}, "過期"),
                        ({"revoked": "David 輸入了修改"}, "作廢"),
                        ({"stage": "Y9"}, "別的階段"),
                        ({"transcript_path": sb.transcript("p-2", human=False), "prompt_id": "p-2"}, "不是人打的"),
                        ({"transcript_path": sb.transcript("p-3", text="請幫我 放行 X1 好嗎"), "prompt_id": "p-3"}, "內容不是"),
                        ({"prompt_id": "p-none"}, "找不到")):
            sb.credential(**kw)
            ok, msgs = sb.check(sb.lines(merge), repo=mw)
            self.assertFalse(ok, kw)
            self.assertIn(why, " ".join(msgs), kw)
        shutil.rmtree(sb.sd, ignore_errors=True)                                      # 完全沒有通行證
        ok, msgs = sb.check(sb.lines(merge), repo=mw)
        self.assertFalse(ok)
        self.assertIn("沒有通行證", " ".join(msgs))

    # ---- 3. 不管有沒有通行證都擋
    def test_rewinding_or_deleting_main_is_always_blocked(self):
        sb = self.sb
        sb.credential()
        data = self.gold_commit()
        sb.land(data)
        ok, _ = sb.check(sb.lines(sb.base, remote_sha=data))                          # 往回退
        self.assertFalse(ok)
        ok, _ = sb.check(["(delete) %s refs/heads/main %s" % (P.ZERO, data)])         # 刪除
        self.assertFalse(ok)
        rc, err = sb.push(sb.wt, ["--force", "origin", "feat/stopX1:main"], claude=False)   # 真的強推，連排程的身分也不行
        self.assertNotEqual(rc, 0)
        self.assertEqual(sb.rev("refs/heads/main", sb.remote), data)

    def test_tags(self):
        sb = self.sb
        rc, err = sb.push(sb.wt, ["origin", "stopX1"], claude=True)                   # 新標籤：可以
        self.assertEqual(rc, 0, err)
        rc, err = sb.push(sb.wt, ["origin", ":refs/tags/stopX1"], claude=True)        # 刪除：不行
        self.assertNotEqual(rc, 0)
        rc, err = sb.push(sb.wt, ["--delete", "origin", "stopX1"], claude=False)
        self.assertNotEqual(rc, 0)
        run_git(["tag", "-f", "stopX1", sb.base], sb.wt)
        rc, err = sb.push(sb.wt, ["--force", "origin", "stopX1"], claude=True)        # 移動：不行
        self.assertNotEqual(rc, 0)
        run_git(["tag", "-f", "stopX1", sb.cand], sb.wt)
        self.assertEqual(run_git(["rev-parse", "refs/tags/stopX1"], sb.remote)[1], sb.cand)

    def test_feature_branches(self):
        sb = self.sb
        rc, err = sb.push(sb.wt, ["-u", "origin", "feat/stopX1"], claude=True)        # 一般的推：可以
        self.assertEqual(rc, 0, err)
        run_git(["commit", "-q", "--amend", "-m", "feat: rewritten"], sb.wt)
        rc, err = sb.push(sb.wt, ["--force", "origin", "feat/stopX1"], claude=True)   # 改寫歷史：不行
        self.assertNotEqual(rc, 0)
        self.assertIn("強推", err)
        run_git(["reset", "-q", "--hard", sb.cand], sb.wt)
        rc, err = sb.push(sb.wt, ["origin", "--delete", "feat/stopX1"], claude=True)  # 收分支：可以
        self.assertEqual(rc, 0, err)

    def test_a_merge_that_carries_extra_changes_is_blocked(self):
        """兩個 parent 都對，但合併 commit 自己多改了東西（合併的時候夾帶）：內容跟「原封不動合併」算出來的不一樣 → 擋。
        對照組：第二道只看 parent、不比對內容 → 這一條會紅。"""
        sb = self.sb
        sb.credential()
        mw = os.path.join(sb.main, ".claude", "worktrees", "merge")
        run_git(["worktree", "add", "-q", "--detach", mw, "origin/main"], sb.main)
        run_git(["merge", "-q", "--no-ff", "--no-commit", sb.cand], mw)             # 合併的確實是放行的那個 commit
        sb.write("README.md", "readme, plus something nobody approved\n", mw)
        run_git(["add", "--", "README.md"], mw)
        run_git(["commit", "-q", "-m", "Merge (with a stowaway)"], mw)
        rc, err = sb.push(mw, ["origin", "HEAD:main"], claude=True)
        self.assertNotEqual(rc, 0)
        self.assertIn("多改了東西", err)
        self.assertEqual(sb.rev("refs/heads/main", sb.remote), sb.base)

    def test_a_different_commit_with_the_same_content_is_not_the_approved_one(self):
        """內容一模一樣、但不是 David 放行的那個 commit（訊息或歷史被改寫過的另一個 commit）：不行——放行綁的是那一個 commit。
        對照組：第二道不核對合併的第二個 parent → 這一條會紅（內容比對那一關分不出這兩個 commit）。"""
        sb = self.sb
        sb.credential()
        twin = run_git(["commit-tree", sb.cand + "^{tree}", "-p", sb.base, "-m", "same content, different commit"], sb.wt)[1]
        self.assertNotEqual(twin, sb.cand)
        mw = os.path.join(sb.main, ".claude", "worktrees", "merge")
        run_git(["worktree", "add", "-q", "--detach", mw, "origin/main"], sb.main)
        run_git(["merge", "-q", "--no-ff", "-m", "Merge the twin", twin], mw)
        rc, err = sb.push(mw, ["origin", "HEAD:main"], claude=True)
        self.assertNotEqual(rc, 0)
        self.assertIn("這次要推的不是它", err)
        self.assertEqual(sb.rev("refs/heads/main", sb.remote), sb.base)

    def test_a_merge_with_hand_resolved_conflicts_is_blocked(self):
        """有衝突的合併不能自己解了就推：停下來寄信，由 David 決定。"""
        sb = self.sb
        sb.write("js/app.js", "// app changed on main too\n")                      # main 上先有一筆改同一行的變更 → 合併會衝突
        conflict = sb.commit("fix: on main")
        sb.land(conflict)
        sb.credential()
        mw = os.path.join(sb.main, ".claude", "worktrees", "merge")
        run_git(["worktree", "add", "-q", "--detach", mw, "origin/main"], sb.main)
        rc, _out, _err = run_git(["merge", "-q", "--no-ff", "--no-commit", sb.cand], mw, check=False)
        self.assertNotEqual(rc, 0)                                                  # 真的衝突了
        sb.write("js/app.js", "// resolved by hand\n", mw)
        run_git(["add", "--", "js/app.js"], mw)
        run_git(["commit", "-q", "-m", "Merge (conflict resolved by hand)"], mw)
        rc, err = sb.push(mw, ["origin", "HEAD:main"], claude=True)
        self.assertNotEqual(rc, 0)
        self.assertIn("衝突", err)
        self.assertEqual(sb.rev("refs/heads/main", sb.remote), conflict)

    # ---- 4. 入口與出錯
    def test_the_shim_lets_everything_through_when_the_logic_file_is_gone(self):
        """P1 被退回之後，主目錄沒有 iw_prepush.py：留在 .git/hooks 的入口不可以把推送卡死。"""
        sb = self.sb
        logic = os.path.join(sb.main, ".claude", "hooks", "iw_prepush.py")
        os.replace(logic, logic + ".away")
        try:
            sb.write("js/app.js", "// no gate now\n")
            sb.commit("fix: after rollback")
            rc, err = sb.push(sb.main, ["origin", "HEAD:main"], claude=True)
            self.assertEqual(rc, 0, err)
        finally:
            os.replace(logic + ".away", logic)

    def test_internal_error_blocks_inside_claude_but_not_the_scheduler(self):
        sb = self.sb
        os.makedirs(sb.sd, exist_ok=True)
        with io.open(os.path.join(sb.sd, "state.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        self.gold_commit()
        rc, err = sb.push(sb.main, ["origin", "HEAD:main"], claude=True)
        self.assertNotEqual(rc, 0)
        self.assertIn("出錯", err)
        rc, err = sb.push(sb.main, ["origin", "HEAD:main"], claude=False)
        self.assertEqual(rc, 0, err)

    def test_a_broken_logic_file_blocks_inside_claude_but_not_the_scheduler(self):
        """檢查程式本身壞掉（語法錯）時 Python 自己的結束碼是 1，跟「規則擋下」要分得出來：
        筆電排程的推送不可以被我們弄壞（放行）；Claude Code 裡的推送一律擋。對照組：入口把所有非 0 都當成擋下 → 這一條會紅。"""
        sb = self.sb
        for rel in (".claude/hooks/iw_prepush.py", ".claude/hooks/iw_state.py", ".claude/hooks/iw_common.py"):
            path = os.path.join(sb.main, *rel.split("/"))
            keep = io.open(path, encoding="utf-8").read()
            try:
                with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write("def broken(:\n")
                self.sb.reset()
                self.gold_commit()
                rc, err = sb.push(sb.main, ["origin", "HEAD:main"], claude=True)
                self.assertNotEqual(rc, 0, rel)
                self.assertEqual(sb.rev("refs/heads/main", sb.remote), sb.base, rel)
                rc, err = sb.push(sb.main, ["origin", "HEAD:main"], claude=False)
                self.assertEqual(rc, 0, "%s：%s" % (rel, err))
                self.assertEqual(sb.rev("refs/heads/main", sb.remote), sb.rev("HEAD"), rel)
            finally:
                with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(keep)

    def test_a_rule_block_is_still_a_block_for_everyone(self):
        """入口分得出兩種非 0：規則擋下（10）對誰都擋，不因為「不是 Claude」就放行。"""
        sb = self.sb
        sb.write("js/app.js", "// changed by hand\n")
        sb.commit("fix: by hand")
        for claude in (True, False):
            rc, err = sb.push(sb.main, ["origin", "HEAD:main"], claude=claude)
            self.assertNotEqual(rc, 0, "claude=%s" % claude)
            self.assertIn("放行", err)
        self.assertEqual(P.BLOCKED, 10)
        self.assertIn("10) exit 1 ;;", INST.SHIM)

    def test_installer(self):
        sb = self.sb
        p = os.path.join(sb.main, ".git", "hooks", "pre-push")
        self.assertEqual(io.open(p, encoding="utf-8").read(), INST.SHIM)
        self.assertIn("iw_prepush.py", INST.SHIM)
        INST.uninstall_prepush(sb.main, quiet=True)
        self.assertFalse(os.path.exists(p))
        with io.open(p, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho someone else\n")
        INST.install_prepush(sb.main, quiet=True)
        self.assertTrue(os.path.exists(p + ".before-iw"))                             # 別人的 pre-push 先備份
        self.assertEqual(io.open(p, encoding="utf-8").read(), INST.SHIM)
        os.remove(p + ".before-iw")

    def test_a_revert_pushed_from_the_terminal_is_blocked_unless_no_verify(self):
        """David 的補充 6：回滾表的 `git revert -m 1 … && git push`。第二道對「沒有通行證的 main 推送」對誰都擋——連 David 的終端機也擋。
        退回 P1 本身時 revert 會把邏輯檔一起拿掉、入口自動放行（test_the_shim_lets_everything_through_when_the_logic_file_is_gone）；
        退回 P1-1 這類「保護檔還在」的合併，David 要用 `git push --no-verify`（git 根本不叫 hook）。"""
        sb = self.sb
        mw = sb.merge_worktree()
        merge = sb.rev("HEAD", mw)
        sb.land(merge, mw)
        run_git(["merge", "-q", "--ff-only", "origin/main"], sb.main)
        run_git(["revert", "-m", "1", "--no-edit", merge], sb.main)
        rc, err = sb.push(sb.main, ["origin", "HEAD:main"], claude=False)       # 終端機（不是 Claude Code）：照樣擋
        self.assertNotEqual(rc, 0)
        self.assertIn("放行", err)
        self.assertEqual(sb.rev("refs/heads/main", sb.remote), merge)
        rc, err = sb.push(sb.main, ["--no-verify", "origin", "HEAD:main"], claude=False)
        self.assertEqual(rc, 0, err)
        self.assertEqual(sb.rev("refs/heads/main", sb.remote), sb.rev("HEAD"))

    def test_a_reopened_docs_window_is_bound_to_the_stage_and_the_merge(self):
        """P1-1 第 6 節：David 再放行一次重開的時間窗，只能用在同一個階段、同一個合併 commit。對照組：第二道不看通行證的階段或合併 → 紅。"""
        sb = self.sb
        mw = sb.merge_worktree()
        merge = sb.rev("HEAD", mw)
        sb.land(merge, mw)
        old = C.iso(C.now() - datetime.timedelta(hours=2))                       # 合併是兩小時前的事；時間窗是剛剛重開的
        sb.credential(merge={"sha": merge, "at": old}, docs_window_from=C.iso(C.now()))
        sb.write("README.md", "row\n", mw)
        docs = sb.commit("docs: merge record", mw)
        ok, msgs = sb.check(sb.lines(docs, remote_sha=merge), repo=mw)
        self.assertTrue(ok, msgs)                                                  # 同階段、同合併：可以
        sb.credential(merge={"sha": merge, "at": old}, docs_window_from=C.iso(C.now()), stage="Y9")
        ok, msgs = sb.check(sb.lines(docs, remote_sha=merge), repo=mw)
        self.assertFalse(ok)                                                       # 別的階段
        self.assertIn("別的階段", " ".join(msgs))
        sb.credential(merge={"sha": "f" * 40, "at": old}, docs_window_from=C.iso(C.now()))
        ok, msgs = sb.check(sb.lines(docs, remote_sha=merge), repo=mw)
        self.assertFalse(ok)                                                       # 通行證記的合併不存在：這筆不是合併、也不是那個合併後的文件
        sb.credential(merge={"sha": merge, "at": old})                             # 沒有重開（docs_window_from 空）：照合併時間算，過期
        ok, msgs = sb.check(sb.lines(docs, remote_sha=merge), repo=mw)
        self.assertFalse(ok)
        self.assertIn("分鐘", " ".join(msgs))
        # 別的合併：通行證綁的是 merge，但 main 上之後又有另一個合併（別的階段的）上去了；文件那一筆接在那個合併後面 → 擋
        sb.write("js/app.js", "// another stage\n", sb.wt)
        other = sb.commit("another stage", sb.wt)
        run_git(["checkout", "-q", "--detach", merge], mw)
        run_git(["merge", "-q", "--no-ff", "-m", "Merge another stage", other], mw)
        merge2 = sb.rev("HEAD", mw)
        sb.land(merge2, mw)
        sb.write("README.md", "row for the first merge\n", mw)
        docs2 = sb.commit("docs: first merge record", mw)
        sb.credential(merge={"sha": merge, "at": old}, docs_window_from=C.iso(C.now()))
        ok, msgs = sb.check(sb.lines(docs2, remote_sha=merge2), repo=mw)
        self.assertFalse(ok)
        self.assertIn("不是資料更新", " ".join(msgs))
        run_git(["reset", "-q", "--hard", sb.cand], sb.wt)

    def test_installer_keeps_the_main_checkout_clean_before_the_merge(self):
        """P1 合併之前，main 上的 .gitignore 還沒有「.autopilot/」：安裝時寫進這台電腦的 .git/info/exclude，
        自動駕駛在主目錄建報告資料夾才不會讓 git status 變髒（筆電排程要求主目錄乾淨）。"""
        repo = os.path.join(self.sb.tmp, "plain")
        run_git(["init", "-q", "-b", "main", repo], self.sb.tmp)
        with io.open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("a\n")
        run_git(["add", "a.txt"], repo)
        run_git(["commit", "-q", "-m", "init"], repo)
        os.makedirs(os.path.join(repo, ".autopilot", "runs", "X1"))
        with io.open(os.path.join(repo, ".autopilot", "runs", "X1", "00.md"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        self.assertNotEqual(run_git(["status", "--porcelain"], repo)[1], "")           # 沒裝之前：看得到沒進版控的資料夾
        p = INST.ensure_local_ignore(repo, quiet=True)
        self.assertEqual(run_git(["status", "--porcelain"], repo)[1], "")
        before = io.open(p, encoding="utf-8").read()
        INST.ensure_local_ignore(repo, quiet=True)                                    # 再跑一次不會重複加
        self.assertEqual(io.open(p, encoding="utf-8").read(), before)
        self.assertEqual(before.count(INST.LOCAL_IGNORE + "\n"), 1)

    # ---- P2：合併前再查一次驗收機；feat 分支上出現不是自己推的 commit
    def test_the_merge_is_blocked_when_the_verifier_is_red_or_unreachable(self):
        """P2 第 3 節：有通行證、形狀也對，但驗收機紅、還在跑、沒有紀錄、查不到——都擋。對照組：拿掉 verify_gate → 這一條會紅。"""
        sb = self.sb
        for entries, want in ((fake_gh_entries(green=False), "紅"), (fake_gh_entries(pending=True), "還在跑"), (fake_gh_entries(runs=False), "沒有這個 commit"),
                              (fake_gh_entries(result_commit="0" * 40), "綁的 commit"), ([], "查不到")):
            sb.reset()
            sb.credential()
            write_fake_gh(sb.fake_gh, entries)
            mw = sb.merge_worktree()
            rc, err = sb.push(mw, ["origin", "HEAD:main"], claude=True)
            self.assertNotEqual(rc, 0, want)
            self.assertIn("合併前再查一次驗收機", err, want)
            self.assertIn(want, err)
            self.assertEqual(sb.rev("refs/heads/main", sb.remote), sb.base)
        sb.reset()
        sb.credential()
        write_fake_gh(sb.fake_gh, fake_gh_entries())                                             # 綠：照常通過
        mw = sb.merge_worktree()
        rc, err = sb.push(mw, ["origin", "HEAD:main"], claude=True)
        self.assertEqual(rc, 0, err)

    def test_a_foreign_commit_on_the_branch_blocks_the_next_push_and_pauses_autopilot(self):
        """P2 第 2 節：PR 分支上出現不是自己推的 commit（Codex 或別的帳號推的）→ 下一次推被擋、自動駕駛中立刻暫停（要你決定）。
        對照組：拿掉 foreign_ok 那一道 → 這一條會紅。"""
        sb = self.sb
        run_git(["reset", "-q", "--hard", sb.cand], sb.wt)                                       # 前面的測試可能動過 worktree 與遠端的這個分支
        run_git(["push", "-q", "--no-verify", "origin", "--delete", "feat/stopX1"], sb.wt, check=False)
        self.addCleanup(lambda: run_git(["push", "-q", "--no-verify", "origin", "--delete", "feat/stopX1"], sb.wt, check=False))
        self.addCleanup(lambda: run_git(["reset", "-q", "--hard", sb.cand], sb.wt))
        self.addCleanup(lambda: ST.update(sb.sd, lambda s: s.update({"active": False, "pause": None})))
        rc, err = sb.push(sb.wt, ["origin", "feat/stopX1"], claude=True)                        # 第一次推：記下來
        self.assertEqual(rc, 0, err)
        st = ST.load(sb.sd)
        self.assertEqual(st["branch_pushes"]["refs/heads/feat/stopX1"], [sb.cand])
        other = os.path.join(sb.tmp, "other-clone")
        shutil.rmtree(other, ignore_errors=True)
        run_git(["clone", "-q", sb.remote, other], sb.tmp)
        run_git(["checkout", "-q", "feat/stopX1"], other)
        sb.write("js/app.js", "// someone else\n", other)
        foreign = sb.commit("drive-by", other)
        run_git(["push", "-q", "--no-verify", "origin", "feat/stopX1"], other)                     # 別人繞過本機的 hook 推上去
        run_git(["fetch", "-q", "origin"], sb.wt)
        run_git(["merge", "-q", "--no-edit", "origin/feat/stopX1"], sb.wt)                           # 本機把它接進來（落後時 git 自己就先拒收，pre-push 看不到）
        sb.write("js/app.js", "// app v3\n", sb.wt)
        mine = sb.commit("feat: more", sb.wt)
        ST.update(sb.sd, lambda s: s.update({"active": True, "stage": "X1", "status": "running", "session_id": "S1", "epoch": 1}))
        rc, err = sb.push(sb.wt, ["origin", "feat/stopX1"], claude=True)
        self.assertNotEqual(rc, 0)
        self.assertIn("不是你推的 commit", err)
        self.assertIn(foreign[:7], err)
        st = ST.load(sb.sd)
        self.assertEqual(st["pause"]["reason"], "foreign-commit")                                # 自動駕駛中：暫停（等級會是「要你決定」）
        self.assertEqual(st["foreign_commit"]["sha"], foreign)
        self.assertEqual(sb.rev("refs/heads/feat/stopX1", sb.remote), foreign)                   # 沒有覆蓋別人的 commit
        ok, msgs = sb.check(["refs/heads/feat/stopX1 %s refs/heads/feat/stopX1 %s" % (mine, sb.cand)])    # 遠端頭是自己推過的：放行
        self.assertTrue(ok, msgs)

    def test_commit_messages_are_scanned_before_claude_pushes_anything(self):
        """commit 訊息一推上公開倉庫就收不回來，驗收機事後判紅也來不及（2026-10-06 Codex 的審查意見；原本推送前完全不看訊息）。
        從 Claude Code 推的每一筆新 commit，訊息都先過主目錄上的隱私掃描器；接在已推過的後面、新的分支、標籤都看。
        命中、讀不到掃描器 → 擋，而且擋下的訊息不重複那段不該公開的字。筆電排程與 David 自己終端機的推送不經過這一道。
        對照組：拿掉這一道、新分支與標籤不掃、掃描器讀不到也放行 → 紅。"""
        sb = self.sb
        self.addCleanup(lambda: run_git(["reset", "-q", "--hard", sb.cand], sb.wt))
        zero = "0" * 40
        cases = (("gh" + "p_" + "A" * 30, "像權杖的字串"), ("C:" + "\\Users\\" + "someone" + "\\notes.txt", "本機的絕對路徑"),
                 ("聯絡 someone" + "@" + "mail.example.org", "電子郵件"), ("持有 " + "1,000 股", "隱私掃描命中"))
        for bad, word in cases:
            run_git(["reset", "-q", "--hard", sb.cand], sb.wt)
            sb.write("js/app.js", "// app v3\n", sb.wt)
            run_git(["add", "--", "js/app.js"], sb.wt)
            run_git(["commit", "-q", "-m", "feat: y\n\n說明：" + bad], sb.wt)
            sha = sb.rev("HEAD", sb.wt)
            for line in ("refs/heads/feat/stopX1 %s refs/heads/feat/stopX1 %s" % (sha, sb.cand),              # 接在遠端現在的頭後面
                         "refs/heads/feat/stopX9 %s refs/heads/feat/stopX9 %s" % (sha, zero),                # 新的分支
                         "refs/tags/stopX1-msg %s refs/tags/stopX1-msg %s" % (sha, zero)):                   # 標籤
                ok, msgs = sb.check([line])
                said = " ".join(msgs)
                self.assertFalse(ok, (word, line))
                self.assertIn("commit 訊息", said)
                self.assertIn(word, said)
                self.assertIn(sha[:7], said)
                self.assertNotIn(bad, said)
            ok, msgs = sb.check(["refs/heads/feat/stopX9 %s refs/heads/feat/stopX9 %s" % (sha, zero)], claude=False)
            self.assertTrue(ok, msgs)                                                                         # 不是從 Claude Code 推的：這一道不管
        run_git(["reset", "-q", "--hard", sb.cand], sb.wt)
        clean = "refs/heads/feat/stopX9 %s refs/heads/feat/stopX9 %s" % (sb.cand, zero)
        guards = os.path.join(sb.main, "scripts", "test_analysis_guards.py")
        os.rename(guards, guards + ".off")
        try:
            ok, msgs = sb.check([clean])                                                                      # 讀不到掃描器：寧可擋
            self.assertFalse(ok)
            self.assertIn("隱私掃描器讀不到", " ".join(msgs))
        finally:
            os.rename(guards + ".off", guards)
        ok, msgs = sb.check([clean])                                                                          # 訊息乾淨、掃描器在：照常
        self.assertTrue(ok, msgs)

    def test_public_text_scan_catches_every_absolute_path_form_and_passes_only_system_mail(self):
        """Codex 對 P2 的第六次審查（兩條 P0）：公開文字（PR 內文、commit 訊息、標籤訊息）的掃描，本機路徑原本只認兩種寫法，
        Unix 與 macOS 的家目錄、別的磁碟機路徑都掃不到；信箱的豁免寫成「地址裡有 noreply 就放行」，連整個 github.com 網域都放行。
        現在任何絕對路徑的寫法都擋；信箱只放行確切的幾個系統地址。對照組：少認一種路徑、有 noreply 就放行 → 紅。"""
        import iw_notify as N
        main = self.sb.main
        sl, bs, at = "/", "\\", "@"                                                       # 拼接：這個檔自己也在隱私掃描的範圍裡，失敗訊息也會進驗收機的紀錄
        paths = (sl + "home" + sl + "alice/notes.txt", sl + "Users" + sl + "alice/Desktop/x.md", "D:" + bs + "work" + bs + "notes.txt", "E:" + sl + "data/x.csv",
                 sl + "mnt" + sl + "c/proj/x", sl + "d" + sl + "proj/x.py", "~" + sl + "secret.txt", bs + bs + "nas" + bs + "share" + bs + "x", "file:" + sl + sl + sl + "x/y",
                 sl + "root" + sl + ".ssh/id", sl + "Volumes" + sl + "disk/x")
        for i, p in enumerate(paths):
            for j, text in enumerate(("說明：放在 " + p, p, "見（" + p + "）", "path=" + p)):
                self.assertIn("有本機的絕對路徑", N.text_privacy_problems(main, text), (i, j))
        fine = ("PR：https://github.com/DAVIDJJX/invest-watch/pull/1", "改了 scripts/verify_ci.py 與 .claude/hooks/iw_guard.py", "docs/a/b.md 與 js/app.js",
                "比例 3:1、時間 09:00、10/5～10/6", "P0／P1、A/B 測試、和/或", "見 README.md：第 3 節", "git@github.com:davidjjx/invest-watch.git",
                "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", "流程檔 .github/workflows/verify.yml（home/ 底下沒有東西）",
                "🤖 Generated with [Claude Code](https://claude.com/claude-code)")
        for i, text in enumerate(fine):
            self.assertEqual(N.text_privacy_problems(main, text), [], i)
        for i, addr in enumerate(("someone+noreply" + at + "mail.example.org", "noreply" + at + "mail.example.org", "someone" + at + "github.com",
                                  "x" + at + "example.com", "t" + at + "example.invalid", "someone" + at + "users.noreply.github.com.example.org")):
            self.assertEqual(N.text_privacy_problems(main, "聯絡 " + addr), ["有電子郵件地址"], i)
            self.assertFalse(N.public_mail_ok(addr), i)
        for addr in N.PUBLIC_MAIL_EXACT + ("12345+someone@users.noreply.github.com", "NoReply@GitHub.com"):
            self.assertEqual(N.text_privacy_problems(main, "署名 " + addr), [], addr)
        self.assertEqual(N.PUBLIC_MAIL_EXACT, ("noreply@anthropic.com", "noreply@github.com", "git@github.com"))    # 放行清單就這幾個，寫在一個地方
        self.assertEqual(N.PUBLIC_MAIL_DOMAINS, ("users.noreply.github.com",))

    def test_the_message_of_an_annotated_tag_is_scanned_before_it_is_pushed(self):
        """Codex 對 P2 的第六次審查（P0）：推送前只掃 commit 的訊息。帶訊息的標籤（annotated tag），訊息存在標籤物件裡，原本完全沒看——
        守門允許建立與推送新標籤，所以標籤訊息裡的個人資料、權杖、信箱、本機路徑會直接公開。現在標籤的訊息照 commit 訊息的規矩掃；讀不到就擋。
        對照組：不掃標籤的訊息、看不出要推的是什麼也放行 → 紅。"""
        sb = self.sb
        zero = "0" * 40
        names = ("stopX1-ann-bad", "stopX1-ann-ok", "stopX1-light", "stopX1-ann-outer")
        self.addCleanup(lambda: [run_git(["tag", "-d", n], sb.wt, check=False) for n in names])

        def annotated(name, message, target):
            run_git(["tag", "-a", name, "-m", message, target], sb.wt)
            return run_git(["rev-parse", "refs/tags/" + name], sb.wt)[1].strip()          # 帶訊息的標籤：推的是標籤物件，不是 commit

        def push(name, sha, **kw):
            return sb.check(["refs/tags/%s %s refs/tags/%s %s" % (name, sha, name, zero)], **kw)
        cases = (("gh" + "p_" + "A" * 30, "像權杖的字串"), ("/ho" + "me/" + "someone/notes.txt", "本機的絕對路徑"),
                 ("聯絡 someone+noreply" + "@" + "mail.example.org", "電子郵件"), ("持有 " + "1,000 股", "隱私掃描命中"))
        for i, (bad, word) in enumerate(cases):
            run_git(["tag", "-d", "stopX1-ann-bad"], sb.wt, check=False)
            tag_sha = annotated("stopX1-ann-bad", "停點 X1\n\n說明：" + bad, sb.cand)
            self.assertNotEqual(tag_sha, sb.cand)
            ok, msgs = push("stopX1-ann-bad", tag_sha)
            said = " ".join(msgs)
            self.assertFalse(ok, i)
            self.assertIn("標籤的訊息", said)
            self.assertIn(word, said)
            self.assertNotIn(bad, said)                                                   # 擋下的訊息不重複那段不該公開的字
            ok, msgs = push("stopX1-ann-bad", tag_sha, claude=False)
            self.assertTrue(ok, msgs)                                                     # 不是從 Claude Code 推的：這一道不管
        clean = annotated("stopX1-ann-ok", "停點 X1：做完了", sb.cand)
        ok, msgs = push("stopX1-ann-ok", clean)
        self.assertTrue(ok, msgs)                                                         # 訊息乾淨的帶訊息標籤：照常
        run_git(["tag", "stopX1-light", sb.cand], sb.wt)
        ok, msgs = push("stopX1-light", sb.cand)
        self.assertTrue(ok, msgs)                                                         # 不帶訊息的標籤：沒有標籤訊息，照常
        bad_inner = run_git(["rev-parse", "refs/tags/stopX1-ann-bad"], sb.wt)[1].strip()
        outer = annotated("stopX1-ann-outer", "外面這一層是乾淨的", bad_inner)             # 標籤指到另一個標籤：裡面那一層的訊息也要掃
        ok, msgs = push("stopX1-ann-outer", outer)
        self.assertFalse(ok)
        self.assertIn("標籤的訊息", " ".join(msgs))
        ok, msgs = push("stopX1-ghost", "f" * 40)                                         # 看不出要推的是什麼：寧可擋
        self.assertFalse(ok)
        self.assertIn("看不出這次要推的東西", " ".join(msgs))
        self.assertEqual(P.MAX_TAG_DEPTH, 5)

    def test_autopilot_cannot_push_anything_that_touches_the_verifier_or_the_workflows(self):
        """2026-10-05 裁決二：自動駕駛期間，推送前的檢查擋下所有動到流程檔、驗收程式、突變與已知例外清單、AGENTS.md 的推送（分支與標籤都算）。
        啟動之前、David 在場時改好而且沒再變的不算；不在自動駕駛就不管（一般模式另有 blob 比對把關）。對照組：拿掉這一道 → 這一條會紅。"""
        sb = self.sb
        self.addCleanup(lambda: run_git(["reset", "-q", "--hard", sb.cand], sb.wt))
        self.addCleanup(lambda: run_git(["tag", "-d", "stopX1-probe"], sb.wt, check=False))
        active = {"version": 1, "active": True, "stage": "X1", "status": "running", "session_id": "S1", "epoch": 1}
        for rel in (".github/workflows/verify.yml", ".github/workflows/other.yml", "scripts/verify_ci.py", "scripts/mutations/known_survivors.json",
                    "scripts/mutations/autopilot_mutations.py", "AGENTS.md"):
            run_git(["reset", "-q", "--hard", sb.cand], sb.wt)
            sb.write(rel, "changed\n", sb.wt)
            sha = sb.commit("touch " + rel, sb.wt)
            ST.save(sb.sd, dict(active))
            ok, msgs = sb.check(["refs/heads/feat/stopX1 %s refs/heads/feat/stopX1 %s" % (sha, "0" * 40)])
            self.assertFalse(ok, rel)
            self.assertIn("自動駕駛期間不能推", " ".join(msgs))
            self.assertIn(rel, " ".join(msgs))
            ok, msgs = sb.check(["refs/tags/stopX1-probe %s refs/tags/stopX1-probe %s" % (sha, "0" * 40)])          # 標籤也一樣
            self.assertFalse(ok, rel)
            blob = run_git(["rev-parse", "%s:%s" % (sha, rel)], sb.wt)[1]
            ST.save(sb.sd, dict(active, preexisting={rel: blob}))                                # 啟動前就改好、內容沒變：不算
            ok, msgs = sb.check(["refs/heads/feat/stopX1 %s refs/heads/feat/stopX1 %s" % (sha, "0" * 40)])
            self.assertTrue(ok, msgs)
            ST.save(sb.sd, dict(active, preexisting={rel: "0" * 40}))                            # 啟動後又改過：算
            ok, msgs = sb.check(["refs/heads/feat/stopX1 %s refs/heads/feat/stopX1 %s" % (sha, "0" * 40)])
            self.assertFalse(ok, rel)
            ST.save(sb.sd, dict(active, active=False))                                           # 不在自動駕駛：這一道不管
            ok, msgs = sb.check(["refs/heads/feat/stopX1 %s refs/heads/feat/stopX1 %s" % (sha, "0" * 40)])
            self.assertTrue(ok, msgs)
        run_git(["reset", "-q", "--hard", sb.cand], sb.wt)
        ST.save(sb.sd, dict(active))
        ok, msgs = sb.check(["refs/heads/feat/stopX1 %s refs/heads/feat/stopX1 %s" % (sb.cand, "0" * 40)])       # 沒動到那些檔：照常
        self.assertTrue(ok, msgs)


if __name__ == "__main__":
    unittest.main()
