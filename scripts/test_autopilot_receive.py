#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_autopilot_receive.py — 停點 P3：接收模式、status.json、用量記錄器、三個誤擋修正

跑法：python -m unittest discover -s scripts -p "test_*.py"
全部離線：守門的規則用假的 git 回答（同 test_autopilot_guard.py）；驗包、套包用暫存資料夾裡真的倉庫做出來的 bundle
（同 test_autopilot_prepush.py 的沙盒）；寄信換成只記錄的替身。數字一看就是假的。

釘住的事（規格 P3、Cowork 裁決 02_Cowork裁決_覆述14件）：
  1. 接收模式由程式判定：改檔工具只准寫報告資料夾（放行後的文件那一筆例外）；git 只准唯讀、fetch origin、推這個階段的分支與標籤、
     建標籤、放行後的 merge-steps；commit／add／apply／reset／從檔案 fetch／merge 一律擋；寫檔指令只准寫報告資料夾；
     Python 只准寫死的全套測試與主目錄那支 iw_notify.py；推分支要收包程式套上、本機全套與完整性檢查都過。
  2. 驗包八條各有「過」與「拒」；拒收寫 rejected.md、seq 佔號、寄小事、不暫停。
  3. 套包只收剛驗過、雜湊沒變的那一包；暫停中不套。
  4. status.json：欄位、原子寫入、不帶本機路徑；Claude 寫不了它、usage.json、收件匣。
  5. 用量：5 小時到 90% 自動駕駛暫停並寄小事；過期、一般模式不擋；7 天到 70% 信多一行。
  6. 三個誤擋：worktree add 的 -b、tier2 已讀綁指紋、ReadNotifications。
  7. 模型：接收模式看 receiveModel／receiveEffort（守門、換模型、開工作階段、啟動訊息、信裡那一行）。
"""
import datetime
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

os.environ["IW_TEST_NO_SIDE_EFFECTS"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".claude", "hooks"))
sys.path.insert(0, os.path.join(ROOT, ".claude", "autopilot"))
sys.path.insert(0, HERE)
import iw_common as C            # noqa: E402
import iw_events as E            # noqa: E402
import iw_guard as G             # noqa: E402
import iw_notify as N            # noqa: E402
import iw_receive as RC          # noqa: E402
import iw_state as ST            # noqa: E402
import usage_status as US        # noqa: E402
from test_autopilot_guard import FakeGit, MAIN, WT, MERGE_WT, HOME, SCRATCH, NOW, CAND, TIP   # noqa: E402
from test_autopilot_prepush import Sandbox, run_git, SANDBOX_NAMED_TERM   # noqa: E402

CFG = C.load_json(os.path.join(ROOT, ".claude", "autopilot", "config.json"))
ALLOW = C.load_json(os.path.join(ROOT, ".claude", "autopilot", "allowlist.json"))
RUNS = MAIN + "/.autopilot/runs/X1"
RM, RE = CFG["receiveModel"], CFG["receiveEffort"]
CLOUD = CFG["cloudAuthor"]
# 拿來測「會被擋、會被遮掉」的假路徑與假信箱：拆開拼，這個檔自己才不會被隱私掃描當成真的（同 test_autopilot_prepush 的做法）
LOCAL_PATH = "D" + ":/" + "Projects" + "/someone/x"
SOMEONE_MAIL = "someone" + "@" + "example.org"


# ============================================================ 1. 守門：接收模式（假的 git 回答）

class GuardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-receive-guard-")
        self.git = FakeGit()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transcript(self, text="放行 X1"):
        p = os.path.join(self.tmp, "t.jsonl")
        with io.open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "promptId": "p-1", "message": {"role": "user", "content": text},
                                 "origin": {"kind": "human"}, "turnOrigin": "human"}, ensure_ascii=False) + "\n")
        return p

    def state(self, mode="receive", status="running", **kw):
        st = {"version": 1, "active": True, "stage": "X1", "status": status, "session_id": "S1", "epoch": 1, "mode": mode,
              "started_at": C.iso(NOW), "clock_started_at": C.iso(NOW - datetime.timedelta(minutes=5)), "reviews": [],
              "receive": {"started_at": C.iso(NOW - datetime.timedelta(hours=1))}}
        st.update(kw)
        return st

    def cred(self, st, **kw):
        c = {"stage": "X1", "candidate": CAND, "tag_sha": "b" * 40, "issued_at": C.iso(NOW - datetime.timedelta(minutes=2)),
             "expires_at": C.iso(NOW + datetime.timedelta(hours=23)), "prompt_id": "p-1", "transcript_path": self.transcript(),
             "session_id": "S1", "merge": None, "docs": None, "docs_window_from": None}
        c.update(kw)
        st["credential"] = c
        return st

    def run_tool(self, tool, ti, state=None, cwd=WT, model=None, effort=None, usage="read", session="S1", now=NOW):
        st = state if state is not None else self.state()
        receive = ST.mode_of(st) == "receive"
        inp = {"session_id": session, "cwd": cwd, "tool_name": tool, "tool_input": ti, "scratchpad_dir": SCRATCH,
               "permission_mode": "auto", "hook_event_name": "PreToolUse",
               "_observed_model": model or (RM if receive else CFG["requiredModel"])}
        inp["effort"] = {"level": effort or (RE if receive else CFG["requiredEffort"])}
        ctx = G.Ctx(MAIN, st, CFG, ALLOW, self.git, inp, home=HOME, now=now, usage=usage)
        return G.decide(inp, ctx)

    def bash(self, cmd, **kw):
        return self.run_tool("Bash", {"command": cmd}, **kw)

    def blocked(self, res, code=None, has=None, msg=""):
        block, _ = res
        self.assertIsNotNone(block, "應該被擋下，卻放行了：%s" % msg)
        if code is not None:
            self.assertEqual(block.code, code, "%s：%s" % (msg, block.reason))
        if has:
            self.assertIn(has, block.reason, msg)
        return block

    def allowed(self, res, msg=""):
        block, effects = res
        self.assertIsNone(block, "不該被擋：%s → %s" % (msg, block.reason if block else ""))
        return effects


class TestReceiveGateEdits(GuardCase):
    def test_edit_tools_only_write_the_report_folder(self):
        """對照組（規格 4 第 5 項）：接收模式下 Edit js/app.js 被擋；報告資料夾可以。"""
        for path in (WT + "/js/app.js", WT + "/scripts/analyze.py", WT + "/docs/ANALYSIS.md", MAIN + "/js/app.js", WT + "/README.md"):
            self.blocked(self.run_tool("Edit", {"file_path": path}), msg=path)
            self.blocked(self.run_tool("Write", {"file_path": path}), msg=path)
        self.allowed(self.run_tool("Write", {"file_path": RUNS + "/02_驗收報告.md"}))
        self.allowed(self.run_tool("Edit", {"file_path": RUNS + "/03_第三方審查.md"}))

    def test_evidence_files_are_program_only_in_both_modes(self):
        """裁決 10：status.json、usage.json、收件匣只准程式寫——兩種模式都擋 Claude 直接寫。"""
        for mode in ("receive", "build"):
            st = self.state(mode=mode)
            for path in (RUNS + "/status.json", MAIN + "/.autopilot/runs/usage.json", RUNS + "/inbox/01/manifest.json",
                         RUNS + "/inbox/02/bundle.git", MAIN + "/.autopilot/runs/P9/status.json"):
                self.blocked(self.run_tool("Write", {"file_path": path}, state=st), code=3, msg="%s %s" % (mode, path))
            self.blocked(self.bash("echo {} > " + RUNS + "/status.json", state=st), code=3, msg=mode)
            self.blocked(self.bash("cp x.json " + RUNS + "/inbox/01/manifest.json", state=st), code=3, msg=mode)
        self.allowed(self.run_tool("Write", {"file_path": RUNS + "/status-notes.md"}, state=self.state(mode="build")))

    def test_docs_window_allows_only_the_two_docs_files(self):
        """裁決 4：放行後、合併推上去到文件推上去之間，改檔工具准改合併用 worktree 裡的 README.md 與 docs/CHANGELOG.md。"""
        merged = {"sha": "d" * 40, "at": C.iso(NOW - datetime.timedelta(minutes=3))}
        st = self.cred(self.state(status="merged"), merge=merged)
        for rel in ("README.md", "docs/CHANGELOG.md"):
            self.allowed(self.run_tool("Edit", {"file_path": MERGE_WT + "/" + rel}, state=st, cwd=MERGE_WT), msg=rel)
        for rel in ("js/app.js", "docs/ANALYSIS.md"):
            self.blocked(self.run_tool("Edit", {"file_path": MERGE_WT + "/" + rel}, state=st, cwd=MERGE_WT), msg=rel)
        before = self.cred(self.state(status="approved"))                   # 還沒合併：文件那一筆的時間窗還沒開
        self.blocked(self.run_tool("Edit", {"file_path": MERGE_WT + "/README.md"}, state=before, cwd=MERGE_WT))
        late = self.cred(self.state(status="merged"), merge={"sha": "d" * 40, "at": C.iso(NOW - datetime.timedelta(hours=3))})
        self.blocked(self.run_tool("Edit", {"file_path": MERGE_WT + "/README.md"}, state=late, cwd=MERGE_WT))
        self.blocked(self.bash("echo x >> " + MERGE_WT + "/README.md", state=st, cwd=MERGE_WT))   # 指令不算改檔工具：文件那一筆也不准用轉向寫

    def test_tools_excluded_in_receive_mode(self):
        self.blocked(self.run_tool("WebFetch", {"url": "https://example.com"}), code=8)
        self.blocked(self.run_tool("WebSearch", {"query": "x"}), code=8)
        self.allowed(self.run_tool("WebFetch", {"url": "https://example.com"}, state=self.state(mode="build")))


class TestReceiveGateCommands(GuardCase):
    def test_git_that_changes_code_is_blocked(self):
        """對照組（規格 4 第 6 項）：接收模式下 git commit 被擋；其他會改程式的寫法也擋。"""
        for cmd in ("git commit -q -m x", "git commit -am x", "git add js/app.js", "git add -A", "git am x.patch", "git apply x.patch",
                    "git cherry-pick " + "e" * 40, "git checkout -- js/app.js", "git checkout feat/other", "git restore js/app.js",
                    "git stash", "git stash push -m x", "git reset --hard HEAD~1", "git reset -q " + "e" * 40, "git switch -c x",
                    "git merge --ff-only FETCH_HEAD", "git pull --ff-only", "git rm js/app.js", "git mv js/app.js js/b.js", "git clean -fd",
                    "git revert HEAD", "git commit-tree HEAD^{tree} -m x", "git hash-object -w js/app.js", "git update-index --add js/app.js",
                    "git read-tree HEAD", "git bundle create x.bundle HEAD", "git notes add -m x", "git tag -a stopX1 -m x"):
            self.blocked(self.bash(cmd), msg=cmd)

    def test_fetch_only_from_origin(self):
        self.allowed(self.bash("git fetch origin"))
        self.allowed(self.bash("git fetch --prune origin"))
        self.allowed(self.bash("git fetch origin main"))
        for cmd in ("git fetch " + RUNS + "/inbox/01/bundle.git feat/stopX1", "git fetch ../cloud.bundle feat/stopX1",
                    "git fetch https://github.com/someone/else main", "git fetch origin feat/stopX1:feat/stopX1",
                    "git fetch origin feat/other:refs/heads/feat/stopX1"):
            self.blocked(self.bash(cmd), code=8, msg=cmd)

    def test_copy_with_a_target_directory_option_is_blocked_in_both_modes(self):
        """cp／mv -t：目的地寫在選項裡，只看最後一個參數會看錯位置（P3 審查時找到；施工模式原本也有這個洞）。"""
        for mode in ("receive", "build"):
            for cmd in ("cp -t " + WT + "/js " + RUNS + "/x.js", "cp --target-directory=" + WT + "/js " + RUNS + "/x.js",
                        "mv -t " + MAIN + "/js " + RUNS + "/x.js", "cp -rt " + WT + "/js " + RUNS + "/d"):
                self.blocked(self.bash(cmd, state=self.state(mode=mode)), code=8, msg="%s %s" % (mode, cmd))
        self.allowed(self.bash("cp " + RUNS + "/a.md " + RUNS + "/b.md"))
        self.allowed(self.bash("cp -r " + RUNS + "/a " + RUNS + "/b"))

    def test_readonly_git_still_works(self):
        for cmd in ("git status -sb", "git log --oneline -5", "git diff --stat origin/main...HEAD", "git show --stat HEAD", "git rev-parse HEAD",
                    "git log -1 --format=\"commit %H\" > " + RUNS + "/tests.txt", "git worktree list", "git branch --show-current", "git tag --list",
                    "git config --get core.autocrlf", "git remote -v"):
            self.allowed(self.bash(cmd), msg=cmd)

    def test_push_needs_the_applied_and_tested_head(self):
        bare = self.state()
        self.blocked(self.bash("git push origin feat/stopX1", state=bare), code=8, has="收包程式套上")
        applied = self.state(receive={"started_at": C.iso(NOW), "applied_head": CAND})
        self.blocked(self.bash("git push origin feat/stopX1", state=applied), code=8, has="receive --tested")
        ok = self.state(receive={"started_at": C.iso(NOW), "applied_head": CAND, "tested_head": CAND})
        self.allowed(self.bash("git push origin feat/stopX1", state=ok))
        self.allowed(self.bash("git push -u origin feat/stopX1", state=ok))
        self.allowed(self.bash("git push origin stopX1", state=ok))
        for cmd in ("git push origin HEAD:feat/other", "git push origin feat/stopX1 stopX1", "git push", "git push origin",
                    "git push --tags origin", "git push origin --delete feat/stopX1"):
            self.blocked(self.bash(cmd, state=ok), msg=cmd)
        self.blocked(self.bash("git push origin feat/stopX1", state=ok, cwd=MAIN))          # 要在這個階段的 worktree 裡推
        other = self.state(receive={"started_at": C.iso(NOW), "applied_head": "e" * 40, "tested_head": "e" * 40})
        self.blocked(self.bash("git push origin feat/stopX1", state=other), code=8)         # HEAD 不是套上的那一個

    def test_tag_only_the_stage_tag_on_the_applied_head(self):
        self.git.tags.discard("stopX1")
        st = self.state(receive={"started_at": C.iso(NOW), "applied_head": CAND})
        self.allowed(self.bash("git tag stopX1", state=st))
        self.blocked(self.bash("git tag stopX2", state=st), code=8)
        self.blocked(self.bash("git tag -f stopX1", state=st))
        self.blocked(self.bash("git tag stopX1", state=self.state()), code=8)               # 還沒套任何包
        self.git.tags.add("stopX1")
        self.blocked(self.bash("git tag stopX1", state=st), code=4)                         # 不動既有標籤

    def test_file_commands_only_touch_the_report_folder(self):
        for cmd in ("cp " + RUNS + "/inbox/01/x.js " + WT + "/js/app.js", "mv " + WT + "/js/app.js " + RUNS + "/x.js", "rm " + WT + "/js/app.js",
                    "touch " + WT + "/js/new.js", "mkdir " + WT + "/js/x", "echo x > " + WT + "/js/app.js", "echo x | tee " + WT + "/js/app.js",
                    "sed -i s/a/b/ " + WT + "/js/app.js", "sort -o " + WT + "/x.txt " + WT + "/y.txt"):
            self.blocked(self.bash(cmd), msg=cmd)
        for cmd in ("cp " + WT + "/js/app.js " + RUNS + "/app-copy.js", "mkdir " + RUNS + "/notes", "touch " + RUNS + "/x.md",
                    "echo hi > " + RUNS + "/x.md", "rm " + RUNS + "/x.md", "ls " + WT + "/js", "cat " + WT + "/js/app.js | head -5"):
            self.allowed(self.bash(cmd), msg=cmd)

    def test_python_only_runs_the_full_suite_and_the_notifier(self):
        notify = "py -3.12 " + MAIN + "/.claude/hooks/iw_notify.py"
        for cmd in ("cd " + WT + " && py -3.12 -W ignore -m unittest discover -s scripts -p \"test_*.py\"",
                    "cd " + WT + " && py -3.12 -W ignore -m unittest discover -s scripts -p \"test_*.py\" >> " + RUNS + "/tests.txt 2>&1",
                    notify + " receive --check " + RUNS + "/inbox/01", notify + " receive --apply " + RUNS + "/inbox/01",
                    notify + " wait-inbox --stage X1", notify + " status-write --stage X1", notify + " status", "py -3.12 --version"):
            self.allowed(self.bash(cmd), msg=cmd)
        for cmd in ("py -3.12 " + RUNS + "/fix.py", "py -3.12 " + WT + "/scripts/backfill_nav.py", "py -3.12 -m http.server 8000",
                    "cd " + MAIN + " && py -3.12 -W ignore -m unittest discover -s scripts -p \"test_*.py\"",
                    "cd " + WT + " && py -3.12 -m unittest scripts.test_x", "cd " + WT + " && py -3.12 -m unittest discover -s js -p \"*.py\"",
                    "py -3.12 " + WT + "/.claude/hooks/iw_notify.py status", "bash -c 'ls'", "sh x.sh"):
            self.blocked(self.bash(cmd), msg=cmd)

    def test_curl_can_look_but_not_save(self):
        self.allowed(self.bash("curl -s http://localhost:8000/index.html"))
        self.allowed(self.bash("curl -sS -w \"%{http_code}\" http://localhost:8000/"))
        for cmd in ("curl -o " + WT + "/js/app.js http://localhost:8000/x.js", "curl -O http://localhost:8000/x.js",
                    "curl -K cfg.txt http://localhost:8000/"):
            self.blocked(self.bash(cmd), msg=cmd)

    def test_merge_steps_after_the_credential(self):
        """放行後 merge-steps 本來就准的那些照樣准；放行前一律擋。"""
        st = self.cred(self.state(status="approved"))
        self.allowed(self.bash("git worktree add --detach .claude/worktrees/stopX1-merge origin/main", state=st, cwd=MAIN))
        self.allowed(self.bash("git merge --no-ff --no-commit " + CAND, state=st, cwd=MERGE_WT))
        self.allowed(self.bash("git checkout --detach origin/main", state=st, cwd=MERGE_WT))
        self.git.merging = CAND
        self.allowed(self.bash("git commit -F " + RUNS + "/merge-msg.txt", state=st, cwd=MERGE_WT))
        self.allowed(self.bash("git push origin HEAD:main", state=st, cwd=MERGE_WT))
        self.allowed(self.bash("git merge --ff-only origin/main", state=st, cwd=MAIN))
        pre = self.state()
        self.blocked(self.bash("git worktree add --detach .claude/worktrees/stopX1-merge origin/main", state=pre, cwd=MAIN))
        self.blocked(self.bash("git merge --no-ff --no-commit " + CAND, state=pre, cwd=MERGE_WT))
        merged = self.cred(self.state(status="merged"), merge={"sha": "d" * 40, "at": C.iso(NOW - datetime.timedelta(minutes=3))})
        self.git.merging = None
        self.git.staged = ["README.md", "docs/CHANGELOG.md"]
        self.allowed(self.bash("git add -- README.md docs/CHANGELOG.md", state=merged, cwd=MERGE_WT))
        self.allowed(self.bash("git commit -F " + RUNS + "/docs-msg.txt", state=merged, cwd=MERGE_WT))
        self.git.staged = ["README.md", "js/app.js"]
        self.blocked(self.bash("git commit -F " + RUNS + "/docs-msg.txt", state=merged, cwd=MERGE_WT))
        self.blocked(self.bash("git add -- js/app.js", state=merged, cwd=MERGE_WT))
        self.allowed(self.bash("git worktree remove .claude/worktrees/stopX1", state=merged, cwd=MAIN))
        self.allowed(self.bash("git branch -d feat/stopX1", state=merged, cwd=MAIN))
        self.allowed(self.bash("git push origin --delete feat/stopX1", state=merged, cwd=MAIN))
        self.blocked(self.bash("git branch -D feat/stopX1", state=merged, cwd=MAIN))

    def test_build_mode_is_unchanged(self):
        """接收模式的限制只加在 mode=receive：施工模式照舊可以改程式、commit。"""
        st = self.state(mode="build")
        self.allowed(self.run_tool("Edit", {"file_path": WT + "/js/app.js"}, state=st))
        self.allowed(self.bash("git commit -q -m x", state=st))
        self.allowed(self.bash("cp " + RUNS + "/x.js " + WT + "/js/app.js", state=st))


class TestReceiveModelAndClock(GuardCase):
    def test_receive_mode_checks_the_receive_model(self):
        """對照組：接收模式的模型檢查看 receiveModel／receiveEffort，不看 requiredModel。"""
        first = self.state(status="pending")
        effects = self.allowed(self.bash("ls", state=first, model=RM, effort=RE))
        self.assertIn("activate", [e[0] for e in effects])
        blk = self.blocked(self.bash("ls", state=self.state(status="pending"), model=CFG["requiredModel"], effort=CFG["requiredEffort"]), code=9)
        self.assertIn("接收：X1", blk.reason)
        self.blocked(self.bash("ls", state=self.state(status="pending"), model=RM, effort=CFG["requiredEffort"]), code=9)
        self.allowed(self.bash("ls", state=self.state(mode="build", status="pending"), model=CFG["requiredModel"], effort=CFG["requiredEffort"]))
        self.blocked(self.bash("ls", state=self.state(mode="build", status="pending"), model=RM, effort=RE), code=9)

    def test_after_eight_hours_only_receiving_is_allowed(self):
        old = self.state(clock_started_at=C.iso(NOW - datetime.timedelta(hours=9)))
        notify = "py -3.12 " + MAIN + "/.claude/hooks/iw_notify.py"
        for cmd in (notify + " wait-inbox --stage X1", notify + " receive --check " + RUNS + "/inbox/03", notify + " receive --apply " + RUNS + "/inbox/03",
                    notify + " status-write --stage X1", "git log --oneline -3"):
            effects = self.allowed(self.bash(cmd, state=old), msg=cmd)
            self.assertNotIn("stop_required", [e[0] for e in effects], cmd)     # 不是停止條件：等包不計時
        self.blocked(self.bash("git fetch origin", state=old))
        build_old = self.state(mode="build", clock_started_at=C.iso(NOW - datetime.timedelta(hours=9)))
        _b, effects = self.bash("git log --oneline -3", state=build_old)
        self.assertIsNone(_b)
        _b, effects = self.bash("git fetch origin", state=build_old)
        self.assertIn("stop_required", [e[0] for e in effects])

    def test_seven_days_is_an_absolute_limit(self):
        old = self.state(receive={"started_at": C.iso(NOW - datetime.timedelta(days=8))})
        block, effects = self.bash("git fetch origin", state=old)
        self.assertIsNotNone(block)
        self.assertIn(("stop_required", 9), [e[:2] for e in effects])
        resumed = self.state(receive={"started_at": C.iso(NOW - datetime.timedelta(days=8)), "last_resume_at": C.iso(NOW - datetime.timedelta(days=1))})
        self.allowed(self.bash("git fetch origin", state=resumed))

    def test_paused_or_waiting_for_approval_does_not_apply_packages(self):
        notify = "py -3.12 " + MAIN + "/.claude/hooks/iw_notify.py"
        paused = self.state(pause={"reason": "blocked", "code": 8, "detail": "x", "at": C.iso(NOW), "retries": 0})
        self.blocked(self.bash(notify + " receive --apply " + RUNS + "/inbox/03", state=paused))
        self.blocked(self.bash(notify + " receive --tested --stage X1", state=paused))
        self.allowed(self.bash(notify + " receive --check " + RUNS + "/inbox/03", state=paused))
        self.allowed(self.bash(notify + " status-write --stage X1", state=paused))
        waiting = self.state(status="awaiting_approval")
        self.blocked(self.bash(notify + " receive --apply " + RUNS + "/inbox/03", state=waiting))


class TestUsageRule(GuardCase):
    def usage(self, five, seconds_ago=5, seven=10.0):
        return {"updated_at": C.iso(NOW - datetime.timedelta(seconds=seconds_ago)), "five_hour_pct": five, "seven_day_pct": seven, "model": RM}

    def test_ninety_percent_pauses_both_modes(self):
        """對照組（規格 4 第 9 項的守門那一半）：usage.json 寫 95% 時自動駕駛暫停（build 與 receive 都是），信由程式寄。"""
        for mode in ("build", "receive"):
            block, effects = self.bash("git fetch origin", state=self.state(mode=mode), usage=self.usage(95.0))
            self.assertIsNotNone(block, mode)
            self.assertEqual(block.code, 9)
            self.assertIn("usage_pause", [e[0] for e in effects], mode)
            self.assertIn("繼續 X1", block.reason)

    def test_the_limit_comes_from_the_config(self):
        limit = CFG["usage"]["pausePct"]
        self.assertEqual((limit, CFG["usage"]["warnPct"], CFG["usage"]["freshSeconds"]), (90, 70, 60))
        self.allowed(self.bash("git fetch origin", usage=self.usage(limit - 0.5)))
        self.blocked(self.bash("git fetch origin", usage=self.usage(limit)), code=9)

    def test_stale_missing_or_general_mode_does_not_block(self):
        self.allowed(self.bash("git fetch origin", usage=self.usage(99.0, seconds_ago=61)))
        self.allowed(self.bash("git fetch origin", usage=None))
        self.allowed(self.bash("git fetch origin", usage={"updated_at": C.iso(NOW), "five_hour_pct": None}))
        self.allowed(self.bash("git fetch origin", usage={"updated_at": C.iso(NOW), "five_hour_pct": True}))
        self.allowed(self.bash("git fetch origin", usage=self.usage(99.0), session="S-other"))   # 一般模式（不是自動駕駛的工作階段）只顯示

    def test_writing_the_report_is_still_allowed(self):
        _b, effects = self.run_tool("Write", {"file_path": RUNS + "/03_停止報告.md"}, usage=self.usage(95.0))
        self.assertIsNone(_b)
        self.assertIn("usage_pause", [e[0] for e in effects])


class TestThreeFalseBlocks(GuardCase):
    def test_worktree_add_with_a_branch_name(self):
        """誤擋 (a)：A1-4 被擋的那一行原文現在能過；路徑在外面照樣擋。"""
        st = self.state(mode="build")
        for cmd in ("git worktree add --no-track -b feat/stopA1-4 .claude/worktrees/stopA1-4 origin/main",
                    "git worktree add -b feat/stopX2 .claude/worktrees/stopX2 origin/main",
                    "git worktree add -bfeat/stopX2 .claude/worktrees/stopX2 origin/main",
                    "git worktree add --lock --reason 'x y' -B feat/stopX2 .claude/worktrees/stopX2 origin/main",
                    "git worktree add --detach .claude/worktrees/stopX1-merge origin/main"):
            self.allowed(self.bash(cmd, state=st, cwd=MAIN), msg=cmd)
        for cmd in ("git worktree add --no-track -b feat/stopX2 ../elsewhere origin/main", "git worktree add -b .claude/worktrees/x ../elsewhere origin/main"):
            self.blocked(self.bash(cmd, state=st, cwd=MAIN), code=8, msg=cmd)

    def test_read_notifications_is_allowed_even_while_paused(self):
        """誤擋 (c)：ReadNotifications 唯讀，自動駕駛中與暫停中都不擋（暫停中也不算「又試了不准的動作」）。"""
        for mode in ("build", "receive"):
            self.allowed(self.run_tool("ReadNotifications", {}, state=self.state(mode=mode)), msg=mode)
            paused = self.state(mode=mode, pause={"reason": "blocked", "code": 8, "detail": "x", "at": C.iso(NOW), "retries": 0})
            effects = self.allowed(self.run_tool("ReadNotifications", {}, state=paused), msg=mode)
            self.assertNotIn("pause_retry", [e[0] for e in effects])
        self.assertIn("ReadNotifications", ALLOW["tools"]["allow"])
        self.assertEqual(G.NOTICE_TOOLS, ("ReadNotifications",))

    def test_tier2_acknowledgement_is_tied_to_the_diff(self):
        """誤擋 (b)：同一份 diff 看過就不再要求；內容變了才要求。"""
        self.git.changed = ["js/app.js", "scripts/fetch_data.py"]
        acked = self.state(mode="build", tier2={"touched": {"scripts/fetch_data.py": {"ack": True, "hash": "h1", "reported": True}}})
        self.git.t2hash["scripts/fetch_data.py"] = "h1"
        effects = self.allowed(self.bash("git push origin feat/stopX1", state=acked))
        self.allowed(self.bash("git push origin feat/stopX1", state=acked))                 # 推第二次：同一份，不用再看
        self.assertIn(("tier2", "scripts/fetch_data.py"), effects)                           # 照樣記「動過」，但不清掉已讀（見 flow 那一組）
        self.git.t2hash["scripts/fetch_data.py"] = "h2"
        self.blocked(self.bash("git push origin feat/stopX1", state=acked), code=3, has="還沒看過現在這一版")
        no_hash = self.state(mode="build", tier2={"touched": {"scripts/fetch_data.py": {"ack": True}}})
        self.blocked(self.bash("git push origin feat/stopX1", state=no_hash), code=3)        # 沒有指紋的已讀不算
        self.git.changed = ["js/app.js"]
        touched_only = self.state(mode="build", tier2={"touched": {"scripts/fetch_data.py": {"ack": False}}})
        self.allowed(self.bash("git push origin feat/stopX1", state=touched_only))          # 動過、但現在跟正式版一樣：沒有 diff 可看


# ============================================================ 2. 驗包與套包（真的倉庫做出來的 bundle）

class Cloud(object):
    """雲端那一側（Cowork）：從遠端複製一份，用雲端作者做 commit、打包。"""

    def __init__(self, sb):
        self.sb = sb
        self.dir = os.path.join(tempfile.mkdtemp(prefix="cloud-", dir=sb.tmp), "repo")
        # 換行照原樣（Windows 的預設會把檔案換成 CRLF，之後 add -A 就把每個檔都當成改過）
        run_git(["clone", "-q", "-c", "core.autocrlf=false", "-c", "core.quotepath=false", sb.remote, self.dir], sb.tmp)
        run_git(["config", "commit.gpgsign", "false"], self.dir)

    def who(self, name=None, email=None, cname=None, cemail=None):
        return {"GIT_AUTHOR_NAME": name or CLOUD["name"], "GIT_AUTHOR_EMAIL": email or CLOUD["email"],
                "GIT_COMMITTER_NAME": cname or name or CLOUD["name"], "GIT_COMMITTER_EMAIL": cemail or email or CLOUD["email"]}

    def start(self, base, branch="feat/stopR1"):
        run_git(["fetch", "-q", "origin"], self.dir)
        run_git(["checkout", "-q", "-B", branch, base], self.dir)

    def commit(self, files, msg="feat: 改一行文字", delete=(), raw=None, **who):
        for rel, text in files.items():
            p = os.path.join(self.dir, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with io.open(p, "wb") as fh:
                fh.write(text if isinstance(text, bytes) else text.encode("utf-8"))
        for rel in delete:
            os.remove(os.path.join(self.dir, *rel.split("/")))
        run_git(["add", "-A"], self.dir)
        run_git(["commit", "-q", "-m", msg], self.dir, env=self.who(**who))
        return run_git(["rev-parse", "HEAD"], self.dir)[1]

    def package(self, base, n, branch="feat/stopR1", extra_refs=(), **over):
        """做一個包放進收件匣 inbox/<n>/：bundle.git＋manifest.json（manifest 照實際內容算；over 可以改掉某一欄做壞的包）。"""
        d = os.path.join(self.sb.main, ".autopilot", "runs", "R1", "inbox", "%02d" % n)
        os.makedirs(d, exist_ok=True)
        bundle = os.path.join(d, "bundle.git")
        run_git(["bundle", "create", "-q", bundle, "%s..%s" % (base, branch)] + list(extra_refs), self.dir)
        head = run_git(["rev-parse", branch], self.dir)[1]
        shas = run_git(["rev-list", "--reverse", "%s..%s" % (base, head)], self.dir)[1].split()
        files, commits = set(), []
        for s in shas:
            files |= set(x for x in run_git(["diff-tree", "--no-commit-id", "-r", "--no-renames", "--name-only", s + "^", s], self.dir)[1].split("\n") if x)
            commits.append({"sha": s, "subject": run_git(["log", "-1", "--format=%s", s], self.dir)[1]})
        with io.open(bundle, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        m = {"schema": 1, "stage": "R1", "seq": n, "base": base, "head": head, "bundle_sha256": digest, "files": sorted(files),
             "commits": commits, "author": dict(CLOUD), "created_at": "2026-11-07T10:00:00Z", "note": "測試用的包"}
        m.update(over)
        with io.open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(m, fh, ensure_ascii=False)
        return d, m


class ReceiveBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox(tracked=True)

    @classmethod
    def tearDownClass(cls):
        cls.sb.close()

    def setUp(self):
        self.sb.reset()
        wt = os.path.join(self.sb.main, ".claude", "worktrees", "stopR1")
        if os.path.isdir(wt):
            run_git(["worktree", "remove", "--force", wt], self.sb.main)
        run_git(["branch", "-D", "feat/stopR1"], self.sb.main, check=False)
        shutil.rmtree(os.path.join(self.sb.main, ".autopilot"), ignore_errors=True)
        self.sent = []
        ST.save(self.sb.sd, {"version": 1, "active": True, "stage": "R1", "status": "running", "mode": "receive", "session_id": "S1", "epoch": 1,
                             "receive": {"started_at": C.iso()}})
        self.cloud = Cloud(self.sb)
        self.base = self.sb.base
        self.cloud.start(self.base)

    def runner(self, args):
        self.sent.append(args)
        return True, "已觸發（測試替身）"

    def check(self, d):
        return RC.check_package(self.sb.main, self.sb.sd, CFG, "R1", d)

    def rejected(self, d, rule):
        with self.assertRaises(RC.Reject) as cm:
            self.check(d)
        self.assertEqual(cm.exception.rule, rule, cm.exception.reason)
        return cm.exception

    def receive_cmd(self, *args):
        return N.main(["receive"] + list(args) + ["--stage", "R1"], runner=self.runner, main_root=self.sb.main)


class TestPackageRules(ReceiveBase):
    def test_a_good_package_is_accepted_and_applied(self):
        """規格 4 第 1 項：好包被接受並套上。"""
        self.cloud.commit({"js/app.js": "// app v3\n"})
        d, m = self.cloud.package(self.base, 1)
        info = self.check(d)
        self.assertEqual((info["seq"], info["head"], info["files"]), (1, m["head"], ["js/app.js"]))
        self.assertEqual(self.receive_cmd("--check", d), 0)
        self.assertEqual(self.receive_cmd("--apply", d), 0)
        wt = os.path.join(self.sb.main, ".claude", "worktrees", "stopR1")
        self.assertEqual(run_git(["rev-parse", "HEAD"], wt)[1], m["head"])
        self.assertEqual(run_git(["rev-parse", "--abbrev-ref", "HEAD"], wt)[1], "feat/stopR1")
        r = ST.load(self.sb.sd)["receive"]
        self.assertEqual((r["last_seq"], r["last_result"], r["applied_head"], r.get("tested_head")), (1, "accepted", m["head"], None))
        self.assertEqual(self.receive_cmd("--apply", d), 3)                                      # 同一包不套第二次
        # 第二包：base＝分支現在的頭
        self.cloud.commit({"js/app.js": "// app v4\n"})
        d2, m2 = self.cloud.package(m["head"], 2)
        self.assertEqual(self.receive_cmd("--check", d2), 0)
        self.assertEqual(self.receive_cmd("--apply", d2), 0)
        self.assertEqual(run_git(["rev-parse", "HEAD"], wt)[1], m2["head"])

    def test_rule1_format_stage_and_seq(self):
        self.cloud.commit({"js/app.js": "// v3\n"})
        d, _m = self.cloud.package(self.base, 1, schema=2)
        self.rejected(d, 1)
        d, _m = self.cloud.package(self.base, 1, stage="R1", seq=2)
        self.rejected(d, 1)
        d, _m = self.cloud.package(self.base, 2)                                                # 跳號
        self.rejected(d, 1)
        d, _m = self.cloud.package(self.base, 1, stage="OTHER")
        self.rejected(d, 1)

    def test_rule2_hash_refs_and_prerequisites(self):
        self.cloud.commit({"js/app.js": "// v3\n"})
        d, _m = self.cloud.package(self.base, 1, bundle_sha256="0" * 64)
        self.rejected(d, 2)
        run_git(["branch", "extra"], self.cloud.dir)
        d, _m = self.cloud.package(self.base, 1, extra_refs=["extra"])
        self.rejected(d, 2)

    def test_rule3_base(self):
        """規格 4 第 3 項：base 錯的包被拒。裁決 2：第一包的 base 可以比正式版舊，只要之後只有排程的資料 commit。"""
        self.cloud.commit({"js/app.js": "// v3\n"})
        d, m = self.cloud.package(self.base, 1)
        # 正式版之後多了一筆雲端的資料更新：照樣收
        self.sb.write("data/latest.json", "{\"x\": 10}\n")
        run_git(["add", "--", "data/latest.json"], self.sb.main)
        run_git(["commit", "-q", "-m", "data: 2026-11-07 10:40 盤中更新（雲端）"], self.sb.main,
                env={"GIT_AUTHOR_NAME": CFG["cloudJob"]["author"], "GIT_AUTHOR_EMAIL": "bot@example.invalid"})
        self.sb.land(self.sb.rev("HEAD"))
        self.assertEqual(self.check(d)["base"], self.base)
        # 多了一筆不是資料更新的：拒
        self.sb.write("js/app.js", "// main changed\n")
        tip2 = self.sb.commit("feat: 正式版改了程式")
        self.sb.land(tip2)
        self.rejected(d, 3)

    def test_rule3_second_package_must_start_at_the_branch_head(self):
        self.cloud.commit({"js/app.js": "// v3\n"})
        d, m = self.cloud.package(self.base, 1)
        self.assertEqual(self.receive_cmd("--check", d), 0)
        self.assertEqual(self.receive_cmd("--apply", d), 0)
        run_git(["checkout", "-q", "-B", "feat/stopR1", self.base], self.cloud.dir)             # 雲端從舊的 base 重做
        self.cloud.commit({"js/app.js": "// v3b\n"})
        d2, _m = self.cloud.package(self.base, 2)
        self.rejected(d2, 3)

    def test_rule4_author_committer_message(self):
        """規格 4 第 4 項：作者錯的包被拒；提交者錯、訊息有本機路徑、標題像資料更新也拒。"""
        for kw, msg in (({"name": "Someone"}, "feat: x"), ({"name": "Someone", "cname": CLOUD["name"]}, "feat: x"),
                        ({"email": "someone@example.invalid"}, "feat: x"),
                        ({"cemail": "other@example.invalid"}, "feat: x"), ({}, "feat: 修正 " + LOCAL_PATH + " 的路徑"),
                        ({}, "data: 2026-11-07 10:40 盤中更新（雲端）")):
            run_git(["checkout", "-q", "-B", "feat/stopR1", self.base], self.cloud.dir)
            self.cloud.commit({"js/app.js": "// v3 %s\n" % msg[:3]}, msg=msg, **kw)
            d, _m = self.cloud.package(self.base, 1)
            self.rejected(d, 4)

    def test_rule4_merge_commits_are_not_accepted(self):
        self.cloud.commit({"js/app.js": "// v3\n"})
        run_git(["checkout", "-q", "-b", "side", self.base], self.cloud.dir)
        self.cloud.commit({"js/other.js": "// side\n"})
        run_git(["checkout", "-q", "feat/stopR1"], self.cloud.dir)
        run_git(["merge", "-q", "--no-ff", "-m", "merge side", "side"], self.cloud.dir, env=self.cloud.who())
        d, _m = self.cloud.package(self.base, 1)
        self.rejected(d, 4)

    def test_rule4_manifest_commits_must_match(self):
        self.cloud.commit({"js/app.js": "// v3\n"})
        d, m = self.cloud.package(self.base, 1, commits=[{"sha": "e" * 40, "subject": "feat: 改一行文字"}])
        self.rejected(d, 4)

    def test_rule5_files_must_match(self):
        self.cloud.commit({"js/app.js": "// v3\n", "js/b.js": "// b\n"})
        d, _m = self.cloud.package(self.base, 1, files=["js/app.js"])
        self.rejected(d, 5)
        d, _m = self.cloud.package(self.base, 1, files=["js/app.js", "js/b.js", "js/c.js"])
        self.rejected(d, 5)

    def test_rule6_protected_and_never_paths(self):
        """規格 4 第 2 項：碰 selfFiles 的包被拒；裁決 5 的「一律不收」清單（不分大小寫、任何資料夾的 CLAUDE.md／AGENTS.md）。"""
        for rel in (".claude/hooks/iw_guard.py", ".Claude/Hooks/iw_guard.py", "scripts/test_autopilot_guard.py", "docs/AUTOPILOT.md",
                    ".github/workflows/x.yml", ".GitHub/dependabot.yml", "data/schedule.json", "scripts/mutations/autopilot_mutations.py",
                    "AGENTS.md", "js/AGENTS.md", "docs/CLAUDE.md", "CLAUDE.local.md", ".mcp.json", ".gitattributes"):
            run_git(["checkout", "-q", "-B", "feat/stopR1", self.base], self.cloud.dir)
            self.cloud.commit({rel: "x\n"})
            d, _m = self.cloud.package(self.base, 1)
            self.rejected(d, 6)

    def test_path_rules_directly(self):
        """不分大小寫、Windows 會當成別的檔的寫法、.autopilot 底下、短檔名：路徑那一關（這幾種在 Windows 上做不出真的 commit，直接測函式）。"""
        for p in (".Claude/Hooks/iw_guard.py", ".CLAUDE/settings.json", "Docs/AutoPilot.md", ".autopilot/runs/R1/status.json", "js/app.js.",
                  "js/app.js ", "js/../.claude/x", "CLAUDE~1/x.py", "js/x:stream", "js\\app.js", "/js/app.js", ".git/config", ".GIT/hooks/x",
                  "js/sub/Claude.MD", "x/agents.md"):
            self.assertIsNotNone(RC.path_problem(p, CFG), p)
        for p in ("js/app.js", "docs/ANALYSIS.md", "data/history/btc.json", "js/settings.js", "README.md", "docs/CHANGELOG.md"):
            self.assertIsNone(RC.path_problem(p, CFG), p)

    def test_rule6_a_protected_file_touched_and_reverted_still_counts(self):
        self.cloud.commit({".claude/hooks/iw_guard.py": "# 改壞\n"})
        run_git(["checkout", "-q", "HEAD~1", "--", ".claude/hooks/iw_guard.py"], self.cloud.dir)
        self.cloud.commit({}, msg="feat: 改回來")
        self.cloud.commit({"js/app.js": "// v3\n"})
        d, m = self.cloud.package(self.base, 1)
        self.assertIn(".claude/hooks/iw_guard.py", m["files"])
        self.rejected(d, 6)

    def test_rule6_symlinks(self):
        self.cloud.commit({"js/app.js": "// v3\n"})
        blob = run_git(["hash-object", "-w", os.path.join(self.cloud.dir, "js", "app.js")], self.cloud.dir)[1]
        run_git(["update-index", "--add", "--cacheinfo", "120000,%s,js/link" % blob], self.cloud.dir)
        run_git(["commit", "-q", "-m", "feat: link"], self.cloud.dir, env=self.cloud.who())
        run_git(["reset", "-q", "--hard"], self.cloud.dir)
        d, _m = self.cloud.package(self.base, 1)
        self.rejected(d, 6)

    def test_rule7_tier2_is_accepted_and_flagged(self):
        self.cloud.commit({"js/settings.js": "// 10\n"})
        d, _m = self.cloud.package(self.base, 1)
        self.assertEqual(self.check(d)["tier2"], ["js/settings.js"])

    def test_rule8_big_binary_files(self):
        limit = CFG["receive"]["binaryMaxBytes"]
        self.cloud.commit({"img/big.bin": b"\0" * (limit + 10)})
        d, _m = self.cloud.package(self.base, 1)
        self.rejected(d, 8)
        run_git(["checkout", "-q", "-B", "feat/stopR1", self.base], self.cloud.dir)
        self.cloud.commit({"img/small.bin": b"\0" * 30})
        d, _m = self.cloud.package(self.base, 1)
        self.check(d)

    def test_named_terms_in_file_contents(self):
        self.cloud.commit({"js/app.js": "// %s\n" % SANDBOX_NAMED_TERM})
        d, _m = self.cloud.package(self.base, 1)
        self.rejected(d, 4)

    def test_replies_format(self):
        self.cloud.commit({"js/app.js": "// v3\n"})
        d, _m = self.cloud.package(self.base, 1)
        with io.open(os.path.join(d, "replies.md"), "w", encoding="utf-8") as fh:
            fh.write("| 留言 id | 等級 | 檔案:行 | 回覆 | 理由或修在哪 |\n|---|---|---|---|---|\n| 10 | P1 | js/app.js:1 | 部分採納 | 改了 |\n")
        self.rejected(d, 1)
        with io.open(os.path.join(d, "replies.md"), "w", encoding="utf-8") as fh:
            fh.write("| 留言 id | 等級 | 檔案:行 | 回覆 | 理由或修在哪 |\n|---|---|---|---|---|\n| 10 | P1 | js/app.js:1 | 採納並修 | commit 在這一包 |\n")
        self.assertTrue(self.check(d)["replies"])


class TestRejectAndApply(ReceiveBase):
    def test_reject_writes_the_reason_takes_the_number_and_does_not_pause(self):
        self.cloud.commit({".claude/hooks/iw_guard.py": "x\n"})
        d, _m = self.cloud.package(self.base, 1)
        self.assertEqual(self.receive_cmd("--check", d), 1)
        with io.open(os.path.join(d, "rejected.md"), encoding="utf-8") as fh:
            self.assertIn("第 6 條", fh.read())
        st = ST.load(self.sb.sd)
        self.assertEqual((st["receive"]["last_seq"], st["receive"]["last_result"]), (1, "rejected"))
        self.assertIsNone(st.get("pause"))
        self.assertTrue(self.sent)
        title = self.sent[-1][self.sent[-1].index("-f") + 1]
        self.assertIn("【%s】" % CFG["stopLevels"]["minor"], title)
        status = C.load_json(os.path.join(self.sb.main, ".autopilot", "runs", "R1", "status.json"))
        self.assertEqual((status["inbox"]["last_seq"], status["inbox"]["last_result"]), (1, "rejected"))
        # 修正做成下一個 seq 的包（被拒的佔號）
        run_git(["checkout", "-q", "-B", "feat/stopR1", self.base], self.cloud.dir)
        self.cloud.commit({"js/app.js": "// v3\n"})
        d2, _m = self.cloud.package(self.base, 2)
        self.assertEqual(self.receive_cmd("--check", d2), 0)

    def test_apply_only_the_package_just_checked(self):
        self.cloud.commit({"js/app.js": "// v3\n"})
        d, _m = self.cloud.package(self.base, 1)
        ok, why = RC.apply_package(self.sb.main, self.sb.sd, CFG, "R1", d)                     # 沒驗過
        self.assertFalse(ok)
        self.assertIn("先 receive --check", why)
        self.assertEqual(self.receive_cmd("--check", d), 0)
        st = ST.load(self.sb.sd)
        copy = st["receive"]["accepted"]["copy"]
        with io.open(copy, "ab") as fh:
            fh.write(b"tamper")
        self.assertEqual(self.receive_cmd("--apply", d), 3)                                     # 驗過的那一份被改過
        self.assertFalse(os.path.isdir(os.path.join(self.sb.main, ".claude", "worktrees", "stopR1")))

    def test_apply_refuses_while_paused(self):
        self.cloud.commit({"js/app.js": "// v3\n"})
        d, _m = self.cloud.package(self.base, 1)
        self.assertEqual(self.receive_cmd("--check", d), 0)
        ST.update(self.sb.sd, lambda s: s.__setitem__("pause", {"reason": "blocked", "code": 8, "detail": "x", "at": C.iso(), "retries": 0}))
        self.assertEqual(self.receive_cmd("--apply", d), 3)

    def test_only_in_receive_mode(self):
        ST.update(self.sb.sd, lambda s: s.__setitem__("mode", "build"))
        self.cloud.commit({"js/app.js": "// v3\n"})
        d, _m = self.cloud.package(self.base, 1)
        self.assertEqual(self.receive_cmd("--check", d), 3)

    def test_tested_needs_the_record_and_the_integrity_check(self):
        self.cloud.commit({"js/app.js": "// v3\n"})
        d, m = self.cloud.package(self.base, 1)
        self.receive_cmd("--check", d)
        self.receive_cmd("--apply", d)
        runs = os.path.join(self.sb.main, ".autopilot", "runs", "R1")
        tests = os.path.join(runs, "tests.txt")
        with io.open(tests, "w", encoding="utf-8") as fh:
            fh.write("commit %s\nRan 10 tests in 1.0s\n\nOK\n" % ("e" * 40))
        self.assertEqual(self.receive_cmd("--tested"), 3)                                       # 不是這個 commit 跑的
        with io.open(tests, "w", encoding="utf-8") as fh:
            fh.write("commit %s\nRan 10 tests in 1.0s\n\nFAILED (failures=1)\n" % m["head"])
        self.assertEqual(self.receive_cmd("--tested"), 3)
        with io.open(tests, "w", encoding="utf-8") as fh:
            fh.write("commit %s\nRan 10 tests in 1.0s\n\nOK\n" % m["head"])
        self.assertEqual(self.receive_cmd("--tested"), 0)
        self.assertEqual(ST.load(self.sb.sd)["receive"]["tested_head"], m["head"])
        # 測試程序動到了生效中的保護檔：暫停、要你決定
        ST.update(self.sb.sd, lambda s: s["receive"].__setitem__("tested_head", None))
        guard = os.path.join(self.sb.main, ".claude", "hooks", "iw_guard.py")
        with io.open(guard, "a", encoding="utf-8") as fh:
            fh.write("\n# 測試程序改了這裡\n")
        try:
            self.assertEqual(self.receive_cmd("--tested"), 3)
            st = ST.load(self.sb.sd)
            self.assertIsNone(st["receive"].get("tested_head"))
            self.assertEqual((st["stop_required"]["code"], st["pause"]["reason"]), (3, "integrity"))
        finally:
            run_git(["checkout", "--", ".claude/hooks/iw_guard.py"], self.sb.main)


class TestWaitInbox(ReceiveBase):
    def test_waits_until_both_files_stop_growing(self):
        t = [0.0]
        d = os.path.join(self.sb.main, ".autopilot", "runs", "R1", "inbox", "01")
        os.makedirs(d)
        for name in ("manifest.json", "bundle.git"):
            with io.open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write("10")
        ok, where = RC.wait_inbox(self.sb.main, self.sb.sd, CFG, "R1", minutes=5, poll=15.0,
                                  sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
        self.assertTrue(ok)
        self.assertGreaterEqual(t[0], CFG["receive"]["stableSeconds"])
        self.assertEqual(os.path.normpath(where), os.path.normpath(d))

    def test_times_out(self):
        t = [0.0]
        ok, why = RC.wait_inbox(self.sb.main, self.sb.sd, CFG, "R1", minutes=1, poll=15.0,
                                sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
        self.assertFalse(ok)
        self.assertIn("01", why)
        self.assertTrue(os.path.exists(os.path.join(self.sb.main, ".autopilot", "runs", "R1", "status.json")))


class TestStatusJson(ReceiveBase):
    def test_fields_and_no_local_paths(self):
        ST.update(self.sb.sd, lambda s: s.update({"pause": {"reason": "blocked", "code": 8, "detail": "擋下：%s 與 %s" % (LOCAL_PATH, SOMEONE_MAIL),
                                                           "at": C.iso(), "retries": 0},
                                                  "status_cache": {"verify": {"state": "green", "run_url": "https://github.com/o/r/actions/runs/10",
                                                                              "tests": 30, "red_kind": None, "commit": "a" * 40},
                                                                   "codex": {"commit": "a" * 40, "rounds": 2, "complete": True, "p0_on_head": False,
                                                                             "comments": [{"id": 10, "path": "js/app.js", "line": 3, "severity": "P1",
                                                                                           "body": "x" * 900}]}},
                                                  "reviews": [{"kind": "acceptance", "verdict": "APPROVE", "commit": "a" * 40}]}))
        p = RC.write_status(self.sb.main, self.sb.sd, CFG, "R1")
        text = io.open(p, encoding="utf-8").read()
        s = json.loads(text)
        self.assertEqual(sorted(s), ["branch", "codex", "credential", "inbox", "mode", "pause", "reviewer", "schema", "stage", "status", "updated_at", "verify"])
        self.assertEqual((s["schema"], s["stage"], s["mode"], s["verify"]["state"], s["verify"]["tests"], s["reviewer"]["verdict"]),
                         (1, "R1", "receive", "green", 30, "APPROVE"))
        self.assertEqual(len(s["codex"]["comments"][0]["body"]), CFG["receive"]["commentMaxChars"])
        self.assertFalse(s["codex"]["comments"][0]["replied"])
        self.assertNotIn(LOCAL_PATH.split("/", 1)[1], text)
        self.assertNotIn(SOMEONE_MAIL, text)
        self.assertEqual(s["pause"]["code"], 8)
        self.assertFalse([f for f in os.listdir(os.path.dirname(p)) if ".tmp." in f])           # 暫存檔換名之後就不在了

    def test_build_mode_writes_the_same_fields(self):
        ST.update(self.sb.sd, lambda s: s.update({"mode": "build"}))
        s = json.loads(io.open(RC.write_status(self.sb.main, self.sb.sd, CFG, "R1"), encoding="utf-8").read())
        self.assertEqual((s["mode"], s["inbox"]), ("build", None))

    def test_other_stage_is_not_written(self):
        self.assertIsNone(RC.write_status(self.sb.main, self.sb.sd, CFG, "Q9"))


# ============================================================ 3. 指令詞、啟動、模型、用量的信（事件層）

class TestCommandAndEvents(unittest.TestCase):
    def test_parse_receive(self):
        P = ST.parse_command
        self.assertEqual(P("接收：A2a"), {"kind": "receive", "stage": "A2a", "rest": ""})
        self.assertEqual(P("接收: A2a\n規格內容"), {"kind": "receive", "stage": "A2a", "rest": "規格內容"})
        self.assertIsNone(P("<pasted_content>\n接收：A2a\n</pasted_content>"))
        self.assertIsNone(P("請你 接收：A2a"))
        self.assertEqual(ST.near_miss("<pasted_content id=\"1\">\n接收：A2a\n規格\n</pasted_content id=\"1\">")["kind"], "receive")
        self.assertIn("接收：", G.APPROVAL_WORDS)

    def test_required_model_by_mode(self):
        self.assertEqual(ST.required_model({"mode": "receive"}, CFG)[:2], (RM, RE))
        self.assertEqual(ST.required_model({}, CFG)[:2], (CFG["requiredModel"], CFG["requiredEffort"]))
        self.assertEqual(ST.mode_of({"mode": "weird"}), "build")
        self.assertEqual((RM, RE, CFG["receiveMaxDays"]), ("claude-sonnet-5-5", "high", 7))
        self.assertEqual(CFG["cloudAuthor"], {"name": "InvestWatch Cloud", "email": "invest-watch-cloud@example.invalid"})

    def test_model_line_compares_with_the_mode_model(self):
        """第一輪審查的必改：信裡「中途是否切換」那一行接收模式要比 receiveModel（不然每封信都寫「是」）。"""
        st = {"mode": "receive", "models_seen": {RM: 10}, "effort_seen": {RE: 10}}
        self.assertIn("中途是否切換：否", N.model_line(st, CFG))
        st = {"mode": "receive", "models_seen": {RM: 10, CFG["requiredModel"]: 1}, "effort_seen": {RE: 10}}
        self.assertIn("中途是否切換：是", N.model_line(st, CFG))
        self.assertIn("中途是否切換：是", N.model_line({"models_seen": {RM: 3}, "effort_seen": {"xhigh": 3}}, CFG))

    def test_no_model_names_are_written_in_the_hooks(self):
        import glob
        for path in glob.glob(os.path.join(ROOT, ".claude", "hooks", "*.py")):
            text = io.open(path, encoding="utf-8").read()
            self.assertNotIn("claude-sonnet", text, path)
            self.assertNotIn("example.invalid", text, path)

    def test_usage_level_is_minor(self):
        st = {"stage": "X1", "pause": {"reason": "usage", "code": 9, "detail": "5 小時的用量到了 95%（門檻 90%）", "retries": 0}}
        self.assertEqual(N.level_for(st, CFG)[0], "minor")
        st["pause"]["retries"] = 1
        self.assertEqual(N.level_for(st, CFG)[0], "decide")


class EventsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-receive-events-")
        self.main = os.path.join(self.tmp, "main")
        run_git(["init", "-q", "-b", "main", self.main], self.tmp)
        os.makedirs(os.path.join(self.main, ".claude", "autopilot"))
        for name in ("config.json", "allowlist.json"):
            shutil.copyfile(os.path.join(ROOT, ".claude", "autopilot", name), os.path.join(self.main, ".claude", "autopilot", name))
        self.sd = os.path.join(self.main, ".git", C.STATE_DIR_NAME)
        self.sent, self.outs = [], []
        self._orig = (C.out_json, C.err, E.integrity_problems)
        C.out_json = lambda obj: self.outs.append(obj)
        C.err = lambda text: None
        E.integrity_problems = lambda env: []                                               # 這一組只看指令詞與事件；保護檔的檢查在 flow 那一組
        self.tp = os.path.join(self.tmp, "t.jsonl")
        io.open(self.tp, "w").close()

    def tearDown(self):
        C.out_json, C.err, E.integrity_problems = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def runner(self, args):
        self.sent.append(args)
        return True, "已觸發（測試替身）"

    def env(self):
        return E.Env(root=self.main, runner=self.runner)

    def say(self, text):
        with io.open(self.tp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "promptId": "p-%d" % len(self.outs), "message": {"role": "user", "content": text},
                                 "origin": {"kind": "human"}, "turnOrigin": "human"}, ensure_ascii=False) + "\n")
        E.prompt({"session_id": "S1", "transcript_path": self.tp, "cwd": self.main, "permission_mode": "auto", "prompt": text,
                  "prompt_id": "p-%d" % len(self.outs), "hook_event_name": "UserPromptSubmit"}, self.env())
        return self.outs[-1] if self.outs else None


class TestReceiveStart(EventsBase):
    def test_start_records_the_mode_and_keeps_coworks_spec(self):
        runs = os.path.join(self.main, ".autopilot", "runs", "A2a")
        os.makedirs(runs)
        with io.open(os.path.join(runs, "00_規格.md"), "w", encoding="utf-8") as fh:
            fh.write("Cowork 放的規格\n")
        out = self.say("接收：A2a\n貼上的規格")
        self.assertIn("接收模式已啟動", out["systemMessage"])
        self.assertIn(CFG["receiveModelLabel"], out["systemMessage"])
        self.assertIn("receive-steps.md", out["hookSpecificOutput"]["additionalContext"])
        st = ST.load(self.sd)
        self.assertEqual((st["mode"], st["stage"], st["status"], st["active"]), ("receive", "A2a", "pending", True))
        self.assertTrue(st["receive"]["started_at"])
        self.assertEqual(io.open(os.path.join(runs, "00_規格.md"), encoding="utf-8").read(), "Cowork 放的規格\n")
        self.assertTrue(os.path.exists(os.path.join(runs, "status.json")))
        out = self.say("自動駕駛：B1\n規格")                                                    # 另一個階段還在進行：不啟動
        self.assertIn("沒有啟動", out["systemMessage"])

    def test_build_start_is_build_mode(self):
        self.say("自動駕駛：B1\n規格")
        st = ST.load(self.sd)
        self.assertEqual((st["mode"], st.get("receive")), ("build", None))

    def test_resume_renews_the_seven_days(self):
        self.say("接收：A2a\n規格")
        ST.update(self.sd, lambda s: s.__setitem__("pause", {"reason": "blocked", "code": 8, "detail": "x", "at": C.iso(), "retries": 0}))
        self.say("繼續 A2a")
        self.assertTrue(ST.load(self.sd)["receive"].get("last_resume_at"))

    def test_modelswitch_uses_the_mode_model(self):
        self.say("接收：A2a\n規格")
        E.modelswitch({"session_id": "S1", "hook_event_name": "PostModelSwitch", "to_model": RM}, self.env())
        self.assertIsNone(ST.load(self.sd).get("model_violation"))
        E.modelswitch({"session_id": "S1", "hook_event_name": "PostModelSwitch", "to_model": CFG["requiredModel"]}, self.env())
        self.assertEqual(ST.load(self.sd)["model_violation"]["model"], CFG["requiredModel"])


class TestEffects(EventsBase):
    def test_tier2_touch_does_not_clear_the_acknowledgement(self):
        """誤擋 (b)：同一個檔再動一次，只記「動過」，已讀（連指紋）留著；看不看得過由推送時比指紋決定。"""
        ST.save(self.sd, {"version": 1, "active": True, "stage": "X1", "status": "running", "session_id": "S1", "epoch": 1,
                          "tier2": {"touched": {"js/settings.js": {"ack": True, "hash": "h1", "reported": True}}}})
        E.apply_effects(self.env(), [("tier2", "js/settings.js"), ("tier2", "js/lock.js")], {"session_id": "S1"})
        t = ST.load(self.sd)["tier2"]["touched"]
        self.assertEqual((t["js/settings.js"]["ack"], t["js/settings.js"]["hash"]), (True, "h1"))
        self.assertFalse(t["js/lock.js"]["ack"])

    def test_usage_pause_sends_one_minor_mail(self):
        """規格 4 第 9 項：usage.json 寫 95% 時 build 模式暫停並寄信（寄信換成替身）。"""
        ST.save(self.sd, {"version": 1, "active": True, "stage": "X1", "status": "running", "session_id": "S1", "epoch": 3, "mode": "build"})
        E.apply_effects(self.env(), [("usage_pause", 95.0, 90.0)], {"session_id": "S1"})
        st = ST.load(self.sd)
        self.assertEqual((st["pause"]["reason"], st["pause"]["code"]), ("usage", 9))
        self.assertEqual(len(self.sent), 1)
        args = self.sent[0]
        title, body = args[args.index("-f") + 1], args[args.index("-f", args.index("-f") + 1) + 1]
        self.assertIn("【%s】" % CFG["stopLevels"]["minor"], title)
        self.assertIn("繼續 X1", body)
        self.assertEqual(st["notified_epoch"], 3)                                               # 算這一輪的通知：stop hook 不再補寄


class TestUsageRecorder(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="iw-usage-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_it(self, stdin_text):
        out = io.StringIO()
        US.main(stdin=io.StringIO(stdin_text), stdout=out, root=self.root, now=NOW)
        return out.getvalue()

    def test_records_both_numbers(self):
        line = self.run_it(json.dumps({"model": {"id": RM}, "rate_limits": {"five_hour": {"used_percentage": 42.4}, "seven_day": {"used_percentage": 31}}}))
        self.assertEqual(line, "5h 42% ｜ 7d 31%")
        rec = C.load_json(os.path.join(self.root, ".autopilot", "runs", "usage.json"))
        self.assertEqual(rec, {"updated_at": C.iso(NOW), "five_hour_pct": 42.4, "seven_day_pct": 31.0, "model": RM})

    def test_missing_fields_are_null(self):
        self.assertEqual(self.run_it(json.dumps({"model": {"id": RM}})), "5h — ｜ 7d —")
        rec = C.load_json(os.path.join(self.root, ".autopilot", "runs", "usage.json"))
        self.assertEqual((rec["five_hour_pct"], rec["seven_day_pct"]), (None, None))
        self.run_it(json.dumps({"rate_limits": {"five_hour": {"used_percentage": "42"}, "seven_day": {"used_percentage": True}}}))
        rec = C.load_json(os.path.join(self.root, ".autopilot", "runs", "usage.json"))
        self.assertEqual((rec["five_hour_pct"], rec["seven_day_pct"]), (None, None))              # 不是數字的值也當成沒有

    def test_bad_input_prints_an_empty_string(self):
        for bad in ("not json", "[1, 2]", ""):
            self.assertIn(self.run_it(bad), ("", "5h — ｜ 7d —"), bad)
        self.assertEqual(self.run_it("{"), "")

    def test_main_root_is_found_from_a_worktree(self):
        repo = os.path.join(self.root, "repo")
        run_git(["init", "-q", "-b", "main", repo], self.root)
        with io.open(os.path.join(repo, "a.txt"), "w") as fh:
            fh.write("10\n")
        run_git(["add", "a.txt"], repo)
        run_git(["commit", "-q", "-m", "x"], repo)
        wt = os.path.join(repo, ".claude", "worktrees", "stopX1")
        run_git(["worktree", "add", "-q", "--detach", wt], repo)
        self.assertEqual(os.path.normcase(os.path.abspath(US._main_root(os.path.join(wt, ".claude", "autopilot")))), os.path.normcase(os.path.abspath(repo)))

    def test_settings_point_at_the_recorder(self):
        settings = C.load_json(os.path.join(ROOT, ".claude", "settings.json"))
        self.assertEqual(settings["statusLine"], {"type": "command", "command": "py -3.12 -X utf8 .claude/autopilot/usage_status.py"})
        self.assertTrue(os.path.exists(os.path.join(ROOT, ".claude", "autopilot", "usage_status.py")))

    def test_the_seven_day_warning_line(self):
        main = os.path.join(self.root, "m")
        os.makedirs(os.path.join(main, ".autopilot", "runs"))
        with io.open(os.path.join(main, ".autopilot", "runs", "usage.json"), "w", encoding="utf-8") as fh:
            json.dump({"updated_at": C.iso(), "five_hour_pct": 10, "seven_day_pct": 75}, fh)
        self.assertIn("75%", N.usage_warning_line(main, CFG))
        with io.open(os.path.join(main, ".autopilot", "runs", "usage.json"), "w", encoding="utf-8") as fh:
            json.dump({"updated_at": C.iso(), "five_hour_pct": 10, "seven_day_pct": 69}, fh)
        self.assertEqual(N.usage_warning_line(main, CFG), "")


class TestDocs(unittest.TestCase):
    """文件跟設定檔一致（同一個數字只有設定檔那一份算數；文件照它寫，有測試釘住）。"""

    def read(self, rel):
        return io.open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8").read()

    def test_the_guide_matches_the_config(self):
        guide = self.read("docs/AUTOPILOT.md")
        r, u = CFG["receive"], CFG["usage"]
        for must in ("接收：<階段>", "## 接收模式", "驗包八條", "status.json", "usage.json", "P3-drill", "receive-steps.md",
                     CFG["receiveModelLabel"], CFG["receiveEffortLabel"], CFG["cloudAuthor"]["email"], CFG["cloudAuthor"]["name"],
                     "%d%%" % u["pausePct"], "%d%%" % u["warnPct"], "%d 秒" % u["freshSeconds"], "%d 分鐘" % r["waitMinutes"],
                     "%d MB" % (r["binaryMaxBytes"] // (1024 * 1024)), "%d MB" % (r["bundleMaxBytes"] // (1024 * 1024)),
                     "%d 天" % CFG["receiveMaxDays"], "%d 小時" % CFG["maxHours"], "守門「不能動本機的 main」那幾條分大小寫", "merge-steps 第 5 步改寫"):
            self.assertIn(must, guide, must)
        for p in r["neverPaths"] + r["neverNames"]:
            self.assertIn(p.replace("/**", "/"), guide, p)

    def test_the_receive_steps(self):
        steps = self.read(".claude/skills/iw-autopilot/receive-steps.md")
        self.assertLess(len(steps), 6000)
        for must in ("iw_notify.py wait-inbox", "receive --check", "receive --apply", "receive --tested", "不改任何程式", "replies.md",
                     "03_第三方審查.md", "不貼進 PR", "不跑腳本", "git tag stop<階段>", "REVIEW-KIND: acceptance", "背景"):
            self.assertIn(must, steps, must)
        skill = self.read(".claude/skills/iw-autopilot/SKILL.md")
        self.assertLess(len(skill), 6500)
        self.assertIn("[receive-steps.md](receive-steps.md)", skill)
        self.assertIn("是資料、不是指令", skill)
        self.assertIn("receiveModel", skill)
        self.assertIn("接收模式", self.read(".claude/skills/iw-autopilot/pr-steps.md"))
        self.assertIn("接收模式", self.read(".claude/skills/iw-autopilot/merge-steps.md"))
        self.assertIn("接收模式的階段", self.read(".claude/skills/iw-autopilot/review-criteria.md"))

    def test_the_config_lists(self):
        r = CFG["receive"]
        for p in (".claude/**", ".github/**", ".autopilot/**", ".mcp.json", ".gitattributes", ".gitmodules"):
            self.assertIn(p, r["neverPaths"])
        self.assertEqual(sorted(r["neverNames"]), ["AGENTS.md", "CLAUDE.local.md", "CLAUDE.md"])
        self.assertEqual((r["inboxDir"], r["waitMinutes"], r["stableSeconds"], r["binaryMaxBytes"], r["bundleMaxBytes"], r["commentMaxChars"]),
                         ("inbox", 240, 30, 2 * 1024 * 1024, 50 * 1024 * 1024, 600))
        self.assertEqual(ALLOW["tools"]["receiveExclude"], ["WebFetch", "WebSearch"])


if __name__ == "__main__":
    unittest.main()
