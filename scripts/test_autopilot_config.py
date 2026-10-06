#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_autopilot_config.py — 停點 P1「自動駕駛」：設定檔、審查代理、清單、文件之間要一致；新檔裡沒有私人資訊

跑法：python -m unittest discover -s scripts -p "test_*.py"
只讀檔，不連網。

釘住的事：
  1. 模型與思考強度這兩個常數只有一個來源（.claude/autopilot/config.json）；設定檔與審查代理的值要跟它相等，程式裡不可以另外寫死。
     對照組：把設定裡的模型改掉、把審查代理改成 inherit → 這裡會紅。
  2. hook 的指令：要擋的事件一定以「|| exit 2」結尾（官方文件：只有結束碼 2 會擋）；讀檔與搜尋以外的工具都經過檢查。
  3. 審查代理是唯讀的（只有 Read、Grep、Glob）。
  4. 黃金排程的例外清單等於 data/assets.json 裡 owner 是 local 的標的（單一真相來源）。
  5. 自動駕駛的新檔：沒有個人資料樣式、沒有本機的絕對路徑、沒有像權杖的字串（沿用 test_analysis_guards.py 的掃描器）。
"""
import glob
import io
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".claude", "hooks"))
sys.path.insert(0, HERE)
import iw_common as C            # noqa: E402
import iw_events as E            # noqa: E402
import iw_hook as H              # noqa: E402
import iw_notify as N            # noqa: E402
import autopilot_install as INST  # noqa: E402
import test_analysis_guards as G  # noqa: E402


def read(rel):
    with io.open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


def load(rel):
    return json.loads(read(rel))


CFG = load(".claude/autopilot/config.json")
ALLOW = load(".claude/autopilot/allowlist.json")
SETTINGS = load(".claude/settings.json")


def frontmatter(text):
    """很簡單的 frontmatter 讀法：只認「鍵: 值」一行一個。"""
    lines = text.split("\n")
    assert lines[0].strip() == "---", "第一行必須是 ---（否則 Claude Code 會把整個檔當成內文）"
    end = lines.index("---", 1)
    out = {}
    for line in lines[1:end]:
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out, "\n".join(lines[end + 1:])


def autopilot_files():
    files = []
    for base, _dirs, names in os.walk(os.path.join(ROOT, ".claude")):
        rel_base = os.path.relpath(base, ROOT).replace("\\", "/")
        if "/worktrees" in "/" + rel_base or "__pycache__" in rel_base or rel_base.startswith(".claude/screens") or rel_base.startswith(".claude/sites"):
            continue
        for n in names:
            if n in ("launch.json", "settings.local.json") or n.endswith(".pyc"):
                continue
            files.append(rel_base + "/" + n)
    files += ["docs/AUTOPILOT.md", "docs/notify-workflow.example.yml", "scripts/autopilot_install.py"]
    files += sorted("scripts/" + os.path.basename(p) for p in glob.glob(os.path.join(HERE, "test_autopilot_*.py")))
    # P2：驗收機、突變清單、給 Codex 的說明也是公開的保護檔
    files += ["AGENTS.md", "scripts/verify_ci.py", "scripts/test_verify_ci.py"]
    files += sorted("scripts/mutations/" + os.path.basename(p) for p in glob.glob(os.path.join(HERE, "mutations", "*.*")))
    return sorted(set(f for f in files if os.path.exists(os.path.join(ROOT, *f.split("/")))))


class TestModelAndEffortAreWrittenOnce(unittest.TestCase):
    def test_settings_match_the_config(self):
        """對照組（規格第 11 節）：把設定裡的模型改掉 → 這一條會紅。"""
        self.assertEqual(CFG["requiredModel"], "claude-opus-5-5")               # 2026-10-05 的模型分工：施工＝Opus 5.5、Extra high
        self.assertEqual(CFG["requiredEffort"], "xhigh")
        self.assertEqual(CFG["requiredModelLabel"], "Opus 5.5")
        self.assertEqual(CFG["approvableEfforts"], ["max"])                      # 中途能由「放行模型」臨時放行的強度只有 Max
        self.assertEqual(SETTINGS["model"], CFG["requiredModel"])
        self.assertEqual(SETTINGS["effortLevel"], CFG["requiredEffort"])
        self.assertIs(SETTINGS["switchModelsOnFlag"], False)                     # 訊息被標記時暫停，不自動換模型
        self.assertNotIn("fallbackModel", SETTINGS)                              # 沒有備援模型＝出錯就停，不改用別的模型
        self.assertNotIn("disableAllHooks", SETTINGS)
        self.assertEqual(sorted(SETTINGS.keys()), ["effortLevel", "hooks", "model", "permissions", "switchModelsOnFlag"])

    def test_reviewer_agent_is_pinned_and_read_only(self):
        """對照組（規格第 11 節）：把審查代理改成 inherit → 這一條會紅；多給它改檔或執行指令的工具 → 也會紅。"""
        fm, body = frontmatter(read(".claude/agents/iw-reviewer.md"))
        self.assertEqual(fm["name"], CFG["reviewer"]["agentType"])
        self.assertEqual(CFG["reviewer"]["model"], "claude-fable-5-1")           # 審查代理固定 Fable 5.1，不跟著施工模型換
        self.assertEqual(fm["model"], CFG["reviewer"]["model"])
        self.assertNotEqual(fm["model"], CFG["requiredModel"])                   # 施工與審查不是同一個模型
        self.assertNotIn(fm["model"], ("inherit", "fable", "opus", "sonnet", "haiku", "best", "default"))
        self.assertEqual(CFG["reviewer"]["effort"], "xhigh")
        self.assertEqual(fm["effort"], CFG["reviewer"]["effort"])
        self.assertEqual([t.strip() for t in fm["tools"].split(",")], ["Read", "Grep", "Glob"])
        for key in ("permissionMode", "hooks", "skills", "mcpServers", "disallowedTools", "isolation", "background"):
            self.assertNotIn(key, fm)
        self.assertIn("VERDICT: APPROVE", body)
        self.assertIn("review-criteria.md", body)
        self.assertEqual(ALLOW["tools"]["agentTypes"], [CFG["reviewer"]["agentType"]])
        self.assertEqual(CFG["reviewer"]["maxRounds"], 2)

    def test_the_constants_are_not_repeated_in_the_code(self):
        for path in sorted(glob.glob(os.path.join(ROOT, ".claude", "hooks", "*.py"))):
            text = io.open(path, encoding="utf-8").read()
            for literal in ("claude-fable", "claude-opus", "xhigh", "invest-data"):
                self.assertFalse(literal in text, "%s 裡寫死了 %s（應該從 config.json 讀）" % (os.path.basename(path), literal))


class TestHookWiring(unittest.TestCase):
    def commands(self):
        out = {}
        for event, groups in SETTINGS["hooks"].items():
            for g in groups:
                for h in g["hooks"]:
                    out.setdefault(event, []).append((g.get("matcher"), h))
        return out

    def test_every_event_we_rely_on_is_registered(self):
        need = {"PreToolUse": "pretool", "UserPromptSubmit": "prompt", "PermissionRequest": "permission", "PostToolUse": "posttool",
                "SubagentStop": "subagentstop", "Stop": "stop", "StopFailure": "stopfailure", "Notification": "notification",
                "ConfigChange": "configchange", "PreModelSwitch": "modelswitch", "PostModelSwitch": "modelswitch",
                "SessionStart": "sessionstart", "SessionEnd": "sessionend"}
        cmds = self.commands()
        self.assertEqual(sorted(cmds), sorted(need))
        for event, name in need.items():
            self.assertEqual(len(cmds[event]), 1, event)
            h = cmds[event][0][1]
            self.assertEqual(h["type"], "command")
            self.assertNotIn("args", h)                                          # shell 形式：才有「|| exit 2」可用
            self.assertEqual(h["command"].count('"${CLAUDE_PROJECT_DIR}/.claude/hooks/iw_hook.py" ' + name), 3, event)   # py／python3／python 三條路
            self.assertIn(name, E.HANDLERS)
            self.assertLessEqual(int(h["timeout"]), 120)

    def test_blocking_events_fail_closed(self):
        """對照組：守門的指令少了「|| exit 2」→ 這一條會紅（Python 自己出錯的結束碼是 1，官方文件說 1 不擋）。"""
        closed = set()
        for event, lst in self.commands().items():
            for _m, h in lst:
                if h["command"].rstrip().endswith("|| exit 2"):
                    closed.add(event)
        self.assertEqual(closed, set(["PreToolUse", "ConfigChange"]))
        self.assertEqual(set(E.FAIL_CLOSED), set(["pretool", "configchange"]))
        self.assertEqual(set(H.FAIL_CLOSED), set(E.FAIL_CLOSED))

    def test_pretooluse_covers_everything_except_plain_reads(self):
        matcher = self.commands()["PreToolUse"][0][0]
        rx = re.compile(matcher)                                                 # JS 的寫法，Python 也看得懂
        for tool in ("Bash", "PowerShell", "Edit", "Write", "NotebookEdit", "Agent", "Workflow", "SubagentHandback", "CronCreate",
                     "ScheduleWakeup", "SendMessage", "AskUserQuestion", "WebFetch", "mcp__claude-in-chrome__navigate",
                     "mcp__ccd_session_mgmt__set_session_model", "ReadSomethingElse", "Grepper"):
            self.assertTrue(rx.search(tool), tool)
        for tool in ("Read", "Grep", "Glob"):
            self.assertFalse(rx.search(tool), tool)
        self.assertEqual(self.commands()["SubagentStop"][0][0], CFG["reviewer"]["agentType"])
        self.assertEqual(self.commands()["PostToolUse"][0][0], "Agent")

    def test_permission_lists(self):
        deny, allow = SETTINGS["permissions"]["deny"], SETTINGS["permissions"]["allow"]
        for must in ("Bash(git push *--force*)", "Bash(git push *--no-verify*)", "Bash(git tag -d *)", "Bash(gh pr merge *)", "Bash(gh auth token*)"):
            self.assertIn(must, deny)
        for rule in allow:
            self.assertNotIn(rule, ("Bash", "Bash(*)", "PowerShell", "PowerShell(*)", "Bash(git *)", "Bash(git push *)", "Bash(python *)", "Bash(gh *)"), rule)
            self.assertFalse(rule.startswith("Edit(") or rule.startswith("Write("), rule)
        self.assertIn("Bash(gh workflow run %s -R %s *)" % (CFG["notify"]["workflow"], CFG["notify"]["repo"]), allow)
        self.assertNotIn("defaultMode", SETTINGS["permissions"])                 # 不替你決定權限模式（bypass 由檢查程式拒絕）

    def test_installer_and_events_agree(self):
        self.assertEqual(INST.LIVE_PARTS, E.LIVE_PARTS)
        self.assertIn(E.PREPUSH_MARK, INST.SHIM)
        self.assertIn("exit 0", INST.SHIM)
        self.assertNotIn("\r", INST.SHIM)


class TestListsMakeSense(unittest.TestCase):
    def test_gold_job_files_follow_the_asset_list(self):
        """架構鐵則：owner 的唯一真相來源是 data/assets.json。例外清單要等於它推出來的；owner 改了這裡會紅，提醒清單要跟著改（改清單要放行）。"""
        assets = load("data/assets.json")
        items = assets["assets"] if isinstance(assets, dict) else assets
        local = sorted("data/history/%s.json" % a["id"] for a in items if (a.get("owner") or "cloud") == "local")
        self.assertEqual(sorted(CFG["goldJob"]["files"]), sorted(["data/latest.json", "data/sources/local.json"] + local))
        self.assertEqual(len(local), 3)

    def test_subject_patterns(self):
        gold = [re.compile(p) for p in CFG["goldJob"]["subjectPatterns"]]
        cloud = [re.compile(p) for p in CFG["cloudJob"]["subjectPatterns"]]
        for s in ("data: 2026-10-01 10:00 盤中輕量更新（本機：台銀黃金現價）", "data: 2026-10-01 本機補抓（台銀黃金）",
                  "data: 2026-10-01 本機補抓（台銀黃金）（第 1 次重試）"):
            self.assertTrue(any(p.match(s) for p in gold), s)
            self.assertFalse(any(p.match(s) for p in cloud), s)
        for s in ("data: 2026-10-01 10:40 盤中更新（雲端）", "data: 2026-09-30 review 更新（雲端，15:30）",
                  "data: 2026-09-30 morning 更新（雲端，09:30）（第 2 次重試）"):
            self.assertTrue(any(p.match(s) for p in cloud), s)
            self.assertFalse(any(p.match(s) for p in gold), s)
        for s in ("data: 手動修一下", "fix: x", "data: 2026-10-01 10:00 盤中輕量更新（本機：台銀黃金現價） 順便改程式"):
            self.assertFalse(any(p.match(s) for p in gold + cloud), s)
        # 這兩種寫法真的是排程在用的（抄自腳本與流程檔；它們是動不得的檔，這裡只讀）
        self.assertIn("盤中輕量更新（本機：台銀黃金現價）", read("scripts/update_local.ps1"))
        self.assertIn("本機補抓（台銀黃金）", read("scripts/update_local.ps1"))
        self.assertIn("盤中更新（雲端）", read(".github/workflows/update-data.yml"))

    def test_tier_lists_point_at_real_files(self):
        for tier in ("tier1", "tier2", "selfFiles"):
            for pat in CFG[tier]["paths"]:
                hits = glob.glob(os.path.join(ROOT, *pat.replace("/**", "/*").split("/")))
                if pat == ".claude/settings.local.json":
                    continue
                self.assertTrue(hits, "%s 的 %s 在倉庫裡找不到對應的檔" % (tier, pat))
        t1, t2, sf = set(CFG["tier1"]["paths"]), set(CFG["tier2"]["paths"]), set(CFG["selfFiles"]["paths"])
        self.assertFalse(t1 & t2)
        self.assertFalse((t1 | t2) & sf)
        for must in (".github/workflows/**", "data/schedule.json", "data/assets.json", "scripts/update_local.ps1", "scripts/publish.py",
                     "scripts/net_policy.py", "scripts/sensitive_terms_hmac.json", ".gitignore"):
            self.assertIn(must, t1)
        for must in ("scripts/fetch_data.py", "scripts/merge_latest.py", "scripts/test_analysis_guards.py", "js/storage.js", "js/fxplan.js",
                     "js/concentration.js", "settings.html"):
            self.assertIn(must, t2)

    def test_numbers(self):
        self.assertEqual((CFG["maxHours"], CFG["credentialHours"], CFG["docsFollowupMinutes"]), (8, 24, 60))
        self.assertEqual(CFG["docsFollowupFiles"], ["README.md", "docs/CHANGELOG.md"])
        self.assertEqual((CFG["remote"], CFG["mainBranch"], CFG["tagPrefix"]), ("origin", "main", "stop"))

    def test_allowlist_has_no_escape_hatches(self):
        programs = ALLOW["programs"]["allow"]
        for bad in ("bash", "sh", "zsh", "powershell", "pwsh", "cmd", "eval", "source", ".", "exec", "node", "npm", "npx", "pip", "ssh", "scp",
                    "wget", "sudo", "chmod", "ln", "mklink", "schtasks", "taskkill", "reg", "start", "rsync", "dd", "tar", "unzip", "perl", "ruby"):
            self.assertNotIn(bad, programs, bad)
        tools = ALLOW["tools"]["allow"]
        for bad in ("PowerShell", "Agent", "Workflow", "AskUserQuestion", "CronCreate", "ScheduleWakeup", "SendMessage", "RemoteTrigger",
                    "NotebookEdit", "Artifact", "EnterWorktree", "PushNotification"):
            self.assertNotIn(bad, tools, bad)
        self.assertFalse([t for t in tools if t.startswith("mcp__") and not (t.startswith("mcp__Claude_Browser__") or t in
                                                                              ("mcp__ccd_session__mark_chapter", "mcp__ccd_host__request_keep_awake"))])
        self.assertEqual(sorted(ALLOW["tools"]["browserHosts"]), ["127.0.0.1", "localhost"])
        self.assertEqual(ALLOW["programs"]["ghAllowed"][0], ["workflow", "run", CFG["notify"]["workflow"]])


class TestDocsAndSkill(unittest.TestCase):
    def test_skill(self):
        text = read(".claude/skills/iw-autopilot/SKILL.md")
        fm, body = frontmatter(text)
        self.assertEqual(fm["name"], "iw-autopilot")
        self.assertTrue(fm["description"])
        # 壓縮對話後每個 skill 只保留前 5,000 個 token：鐵則與停止條件要在最前面，整份不能太長
        self.assertLess(len(text), 6500)
        head = text[:3000]
        for must in ("合併進 main 之前一律停", "先寄信再停", "被擋下＝停止條件", "九個停止條件"):
            self.assertIn(must, head, must)
        for n in range(1, 10):
            self.assertRegex(body, r"(?m)^%d\. " % n)
        self.assertIn("iw_notify.py send", body)
        self.assertIn("REVIEW-KIND: restatement", body)
        self.assertIn("REVIEW-KIND: acceptance", body)
        self.assertTrue(os.path.exists(os.path.join(ROOT, ".claude", "skills", "iw-autopilot", "report-template.md")))
        self.assertTrue(os.path.exists(os.path.join(ROOT, ".claude", "skills", "iw-autopilot", "review-criteria.md")))

    def test_the_test_environment_is_written_down_once(self):
        """2026-10-02：實戰第一次重跑測試，python 指到沒裝 requests 的版本（55 條假紅）、Edge 的無頭模式回空白頁（11 組假紅）。
        跑測試的環境寫死在 config.json 的 testEnv；hook 每次啟動時照它告訴 Claude（test_autopilot_flow.py 釘住），
        程式與流程檔裡不另外寫一份。對照組：把設定裡的 py -3.12 改成 python → 這一條會紅。"""
        env = CFG["testEnv"]
        self.assertEqual(sorted(env), ["about", "browser", "browserEnv", "python"])
        self.assertEqual((env["python"], env["browserEnv"]), ("py -3.12", "IW_BROWSER"))
        self.assertIn("Chrome", env["browser"])
        self.assertNotIn("Edge", env["browser"])
        for path in sorted(glob.glob(os.path.join(ROOT, ".claude", "hooks", "*.py"))):         # 同一個常數只寫一次：程式從設定讀
            text = io.open(path, encoding="utf-8").read()
            for literal in (env["python"], "chrome.exe"):
                self.assertFalse(literal in text, "%s 裡寫死了 %s（應該從 config.json 的 testEnv 讀）" % (os.path.basename(path), literal))
        body = read(".claude/skills/iw-autopilot/SKILL.md")
        self.assertNotRegex(body, r"(?m)^\s+python3? .*-m unittest")                # 流程裡不可以另外寫一條「用 python 跑測試」的指令
        guide = read("docs/AUTOPILOT.md")
        self.assertIn(env["python"], guide)
        self.assertIn(env["browserEnv"], guide)
        allow = SETTINGS["permissions"]["allow"]
        for rule in ("Bash(%s -m unittest *)" % env["python"], "Bash(%s -W ignore -m unittest *)" % env["python"], "Bash(%s scripts/*)" % env["python"],
                     "Bash(%s --version)" % env["python"], "Bash(python --version)", "Bash(py --list)", "Bash(command -v *)"):
            self.assertIn(rule, allow, rule)

    def test_skill_and_config_agree_on_the_test_environment_and_the_merge_steps(self):
        """P1-1 第 5 節：SKILL.md 補上測試環境那一段，值要跟 config.json 的 testEnv 一致（兩邊只能一起改）；合併固定分兩個指令。
        對照組：改掉 config 裡的 Python 版本而不改 SKILL → 這一條會紅。"""
        env = CFG["testEnv"]
        text = read(".claude/skills/iw-autopilot/SKILL.md")
        for must in (env["python"], env["browserEnv"], "Chrome", ".autopilot/local-env.txt", "分成兩個指令", "暫停", "裁決：", "等級"):
            self.assertIn(must, text, must)
        self.assertIn(env["browser"], text)                                                # 瀏覽器的路徑也要一致（Edge 只准以「不要用」出現）
        self.assertNotIn("msedge", text.lower())
        self.assertNotRegex(text, r"(?m)^\s*python3? 腳本")                           # 第一條改成 py -3.12
        for m in re.finditer(r"\[([^\]]+\.md)\]\(([^)]+\.md)\)", text):                # SKILL.md 連到的同資料夾檔要在
            self.assertTrue(os.path.exists(os.path.join(ROOT, ".claude", "skills", "iw-autopilot", m.group(2))), m.group(2))
        steps = read(".claude/skills/iw-autopilot/merge-steps.md")                      # 拆出去的檔也有上限（David 的裁決 8），隱私掃描另外涵蓋
        self.assertLess(len(steps), 4000)
        for must in ("兩個指令", "--no-verify", "finish-docs", "60 分鐘", "放行 <階段>"):
            self.assertIn(must, steps, must)

    def test_stop_levels_and_the_protection_version_are_written_once(self):
        """P1-1 第 3 節：三個等級的字串只寫在 config.json；hook 不另外寫死；說明文件照它寫。對照組：hook 裡寫死標籤 → 紅。"""
        levels = CFG["stopLevels"]
        self.assertEqual(sorted(levels), ["about", "approve", "decide", "minor"])
        labels = [levels[k] for k in ("minor", "decide", "approve")]
        self.assertEqual(len(set(labels)), 3)
        for path in sorted(glob.glob(os.path.join(ROOT, ".claude", "hooks", "*.py"))):
            text = io.open(path, encoding="utf-8").read()
            for label in labels:                                                     # 當成字串常數寫死（"…"）才算；句子裡提到不算
                self.assertNotIn('"%s"' % label, text, "%s 裡寫死了等級「%s」（應該從 config.json 的 stopLevels 讀）" % (os.path.basename(path), label))
                self.assertNotIn("【%s】" % label, text, os.path.basename(path))
        self.assertEqual(CFG["protectionVersion"], "P2")
        guide = read("docs/AUTOPILOT.md")
        for must in labels + ["裁決：", "暫停", "P1-1", "P2", "先退", "--no-verify"]:
            self.assertIn(must, guide, must)
        skill = read(".claude/skills/iw-autopilot/SKILL.md")
        for label in labels:
            self.assertIn(label, skill, label)

    def test_report_template_has_the_eight_sections_in_order(self):
        import iw_notify as N
        text = read(".claude/skills/iw-autopilot/report-template.md")
        pos = [text.index("**%s**" % s) for s in N.SECTIONS]
        self.assertEqual(pos, sorted(pos))
        self.assertEqual(len(N.SECTIONS), 8)

    def test_review_criteria_cover_what_the_spec_lists(self):
        text = read(".claude/skills/iw-autopilot/review-criteria.md")
        for must in ("同一個門檻只能有一個常數", "不開豁免", "一看就是假的", "突變對照", "重疊視窗", "跨市場相關", "不同口徑不混比",
                     "「資料不足」與「不適用」分開", "DOM 只增不減", "首屏請求數", "docs 大小上限", "日期與來源",
                     "呈現原則六條", "升級給 David", "信（停止報告）的可讀性", "VERDICT"):
            self.assertIn(must, text, must)
        self.assertEqual(len(re.findall(r"(?m)^\d+\. ", text.split("## A. ")[1].split("## B. ")[0])), 23)

    def test_the_guide_for_david(self):
        text = read("docs/AUTOPILOT.md")
        for must in ("自動駕駛：", "放行 <階段>", "修改 <階段>：", "繼續 <階段>", "放行模型", "結束自動駕駛",
                     "三道保護", "擋不住的事", "出問題的時候", "名詞解釋", "第一次設定",
                     "Opus 5.5＋Extra high 用得比較兇", "不會偷偷換模型", "模型分工",
                     "額度幾點恢復", "disableAllHooks", "--no-verify", "autopilot_install.py --uninstall"):
            self.assertIn(must, text, must)
        for path in CFG["tier1"]["paths"] + CFG["tier2"]["paths"]:
            self.assertIn(path.replace("/**", "/"), text, "說明文件沒有列出 %s" % path)
        self.assertIn(str(CFG["maxHours"]) + " 小時", text)

    def test_notify_workflow_template(self):
        text = read("docs/notify-workflow.example.yml")
        self.assertIn("workflow_dispatch", text)
        for name in ("title:", "body:", "stage:", "action:"):
            self.assertIn(name, text)
        self.assertRegex(text, r"(?m)^permissions:\n  issues: write\n")
        self.assertNotIn("contents: write", text)
        self.assertNotIn("secrets.", text)                                       # 不用任何自己存的密鑰
        self.assertIn("github.token", text)
        self.assertNotRegex(text, r"run:.*\$\{\{\s*inputs\.")                    # 輸入不直接寫進指令列

    def test_gitignore(self):
        gi = read(".gitignore")
        for line in (".claude/*", "!.claude/settings.json", "!.claude/hooks/", "!.claude/agents/", "!.claude/skills/", "!.claude/autopilot/", ".autopilot/"):
            self.assertIn("\n" + line + "\n", "\n" + gi.replace("\r\n", "\n"), line)
        self.assertNotIn("\n.claude/\n", "\n" + gi.replace("\r\n", "\n"))


class TestNewFilesHaveNoPrivateInformation(unittest.TestCase):
    """隱私鐵則：公開倉庫不放私人資訊。自動駕駛的新檔沿用分析系列的掃描器（個人資料樣式、兩份設定檔的鍵名、加鹽 HMAC 的具名字串），
    另外擋本機的絕對路徑與像權杖的字串。"""

    def setUp(self):
        self.files = autopilot_files()
        self.assertGreaterEqual(len(self.files), 20)

    def test_generic_private_patterns_and_setting_keys(self):
        bad = {}
        for rel in self.files:
            text = read(rel)
            hits = G.privacy_hits(text) + G.profile_key_hits(text) + G.fxplan_key_hits(text)
            if hits:
                bad[rel] = hits
        self.assertEqual(bad, {})

    def test_no_local_paths_tokens_or_mail_addresses(self):
        bad = {}
        for rel in self.files:
            text = read(rel)
            hits = []
            if re.search(r"(?i)[a-z]:[\\/]+users[\\/]+(?!fake\b)[a-z0-9_.-]+", text):
                hits.append("本機的使用者資料夾")
            if re.search(r"(?i)[a-z]:[\\/]+claude_use", text):
                hits.append("本機的絕對路徑")
            if any(re.search(p, text) for p in N.SECRET_PATTERNS):                  # 清單跟公開文字、驗收機那兩邊的一樣（原本只認兩種 GitHub 權杖的開頭）
                hits.append("像權杖的字串")
            for mm in re.finditer(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
                m = mm.group(0)
                # git 遠端網址的兩種寫法不是誰的信箱：ssh 的「git@主機」，以及「https://帳號:密碼@主機」裡 @ 前後那一段（測試資料裡有這兩種假網址）
                before = re.split(r"[\s\"'<>()]", text[max(0, mm.start() - 120):mm.start()])[-1]
                if m.lower().startswith("git@") or "://" in before:
                    continue
                # 其餘可以出現的只有：確切的系統地址（清單在 iw_notify.PUBLIC_MAIL_*）與保留給測試用的網域。
                # 原本寫成「地址裡有 noreply 就放行」、@github.com 整個網域也放行（Codex 對 P2 的第六次審查指出同一種寫法的洞）
                if not (N.public_mail_ok(m) or m.lower().rsplit("@", 1)[-1] in ("example.com", "example.invalid")):
                    hits.append("電子郵件：" + m)
            if hits:
                bad[rel] = hits
        self.assertEqual(bad, {})

    def test_named_terms(self):
        salt, entries = G.load_salt(), G.load_digests()
        if not salt:
            self.skipTest("沒有鹽（runner 上就是這樣）；通用樣式照樣跑了")
        bad = {}
        for rel in self.files:
            hits = G.named_term_hits(read(rel), salt, entries)
            if hits:
                bad[rel] = hits
        self.assertEqual(bad, {})

    def test_scanner_is_really_looking_at_the_new_files(self):
        self.assertIn(".claude/hooks/iw_guard.py", self.files)
        self.assertIn(".claude/settings.json", self.files)
        self.assertIn(".claude/skills/iw-autopilot/review-criteria.md", self.files)
        self.assertIn("docs/AUTOPILOT.md", self.files)
        self.assertIn("scripts/test_autopilot_config.py", self.files)
        self.assertIn("AGENTS.md", self.files)
        self.assertIn("scripts/verify_ci.py", self.files)
        self.assertIn("scripts/mutations/autopilot_mutations.py", self.files)
        self.assertTrue(G.privacy_hits("持有 " + "1,000 股"))                    # 掃描器本身抓得到東西


class TestThirdPartyGate(unittest.TestCase):
    """P2：驗收機與外部審查的設定、保護清單、文件、突變清單都要在、而且互相一致。"""

    def test_every_job_that_runs_branch_code_is_locked_down(self):
        """全套測試、每一片突變、跟 main 比（會載入分支上的突變定義）都會執行分支上的程式碼：流程檔裡這幾步都要排在「封鎖」之後、
        「解除封鎖並收集」之前（Codex 對 P2 的意見：突變分片原本沒有封鎖，對外請求也沒有收集）。對照組：拿掉突變分片的封鎖 → 紅。"""
        text = read(".github/workflows/verify.yml")
        verify_job = text[text.index("\n  verify:"):text.index("\n  mutations:")]
        mut_job = text[text.index("\n  mutations:"):text.index("\n  collect:")]

        def pos(job, word):
            self.assertEqual(job.count('verify_ci.py" %s ' % word), 1, word)
            return job.index('verify_ci.py" %s ' % word)
        self.assertLess(pos(verify_job, "lockdown"), pos(verify_job, "run-tests"))
        self.assertLess(pos(verify_job, "run-tests"), pos(verify_job, "compare"))
        self.assertLess(pos(verify_job, "compare"), pos(verify_job, "unlock"))
        self.assertLess(pos(mut_job, "lockdown"), pos(mut_job, "mutations"))
        self.assertLess(pos(mut_job, "mutations"), pos(mut_job, "unlock"))

    def test_every_job_scans_before_it_uploads(self):
        """Codex 對 P2 的第二次審查：原本整個結果資料夾先上傳、到最後一段才掃描，就算判紅，詳細輸出與瀏覽器的 netlog 也已經留在 artifact 裡。
        現在每一段在上傳之前先「整理」（掃描、只帶必要的檔），上傳的是整理過的資料夾。對照組：上傳整個結果資料夾 → 紅。"""
        text = read(".github/workflows/verify.yml")
        verify_job = text[text.index("\n  verify:"):text.index("\n  mutations:")]
        mut_job = text[text.index("\n  mutations:"):text.index("\n  collect:")]
        for job in (verify_job, mut_job):
            self.assertEqual(job.count('verify_ci.py" export '), 1)
            self.assertLess(job.index('verify_ci.py" unlock '), job.index('verify_ci.py" export '))
            self.assertLess(job.index('verify_ci.py" export '), job.index("uses: actions/upload-artifact"))
            self.assertEqual(job.count("uses: actions/upload-artifact"), 1)
            self.assertIn("path: ${{ runner.temp }}/verify-export\n", job)
            self.assertNotIn("path: ${{ runner.temp }}/verify-out", job)

    def test_the_real_gitignore_ignores_python_bytecode(self):
        """hook 是用子程序跑的，會在主目錄的保護程式資料夾裡留下 __pycache__/；寄「可以合併」前「主目錄要乾淨」那一關靠 .gitignore 忽略它。
        沙盒測試把寫快取關掉了（沙盒的 .gitignore 沒有這一行），所以這件事在這裡另外釘住。第一次在 GitHub 的執行機上跑才發現的。"""
        lines = [l.strip() for l in read(".gitignore").splitlines()]
        self.assertIn("__pycache__/", lines)

    def test_config_has_the_gate_and_the_lists_point_at_real_files(self):
        v, e = CFG["verify"], CFG["externalReview"]
        self.assertEqual((v["workflow"], v["artifact"]), ("verify.yml", "verify-result"))
        self.assertEqual(sorted(v["files"]), [".github/workflows/verify.yml", "scripts/mutations/known_survivors.json", "scripts/mutations/run_mutations.py",
                                              "scripts/verify_ci.py"])
        for f in v["files"]:
            self.assertTrue(os.path.exists(os.path.join(ROOT, *f.split("/"))), f)
        # 自動駕駛期間擋推送的範圍：都在第一層裡（守門擋寫入），而且蓋住「比 blob」的那幾個檔
        self.assertEqual(sorted(v["protected"]), [".github/workflows/**", "AGENTS.md", "scripts/mutations/**", "scripts/verify_ci.py"])
        for pat in v["protected"]:
            self.assertIn(pat, CFG["tier1"]["paths"], pat)
        for f in v["files"]:
            self.assertTrue(C.glob_match(f, v["protected"]), f)
        self.assertEqual(e["botLogin"], "chatgpt-codex-connector[bot]")
        self.assertEqual(e["trigger"], "@codex review")
        self.assertEqual((e["timeoutMinutes"], e["waiverHours"], e["responsesFile"]), (60, 24, "03_第三方審查.md"))
        self.assertEqual(re.compile(e["severityPattern"]).search("**[P1]** x").group(1), "1")
        for must in ("AGENTS.md", "scripts/verify_ci.py", "scripts/mutations/**"):
            self.assertIn(must, CFG["tier1"]["paths"])
        for pat in (["pr", "view"], ["pr", "list"], ["pr", "diff"], ["pr", "checks"], ["run", "download"]):
            self.assertIn(pat, ALLOW["programs"]["ghAllowed"])
        for pat in (["pr", "create"], ["pr", "comment"], ["pr", "edit"], ["pr", "review"], ["api"]):
            self.assertNotIn(pat, ALLOW["programs"]["ghAllowed"])
        self.assertEqual(CFG["protectionVersion"], "P2")

    def test_the_no_review_flag_is_gone(self):
        import iw_notify as N
        with self.assertRaises(SystemExit):
            N.main(["send", "--stage", "X1", "--kind", "ready", "--report", "x.md", "--no-review"], main_root=ROOT)
        for rel in (".claude/skills/iw-autopilot/SKILL.md", ".claude/skills/iw-autopilot/pr-steps.md", ".claude/skills/iw-autopilot/merge-steps.md"):
            self.assertNotIn("no-review", read(rel), rel)

    def test_agents_md_is_public_safe_and_has_the_review_rules(self):
        """給 Codex 的審查準則（公開檔）：官方文件要的段落名、繁體中文、只審不改，以及 2026-10-05 裁決七點名的八點。
        隱私掃描另外涵蓋這個檔（autopilot_files）；所以第 1 點裡個人資料的類別用不會被掃描擋下的講法寫。
        第 2 點不列那幾個詞（見下一條測試）：只說「不得出現擋字串」，清單指到掃描器那個檔。"""
        text = read("AGENTS.md")
        for must in ("## Code Review Rules", "繁體中文", "P0", "P1", "只做程式碼審查", "scripts/net_policy.py", "scripts/verify_ci.py", "scripts/mutations/"):
            self.assertIn(must, text, must)
        self.assertLess(len(text.encode("utf-8")), 32 * 1024)                    # Codex 合併 AGENTS.md 的上限是 32 KiB
        rules = text.split("## Code Review Rules", 1)[1]
        for n, must in enumerate(("隱私", "擋字串", "門檻常數", "DOM 與請求數", "測試數與突變數", "資料狀態", "保護範圍", "對外請求"), 1):
            self.assertRegex(rules, r"(?m)^%d\. \*\*%s\*\*" % (n, re.escape(must)))
        for must in ("首屏請求數", "資料不足", "不適用", "PR 內文", "bot.com.tw", "commit 訊息", "fixture"):
            self.assertIn(must, rules, must)
        self.assertEqual(G.privacy_hits(text) + G.profile_key_hits(text) + G.fxplan_key_hits(text), [])

    def test_agents_md_passes_the_banned_word_scan(self):
        """AGENTS.md 通過擋字串掃描（2026-10-06 裁決第二節）。原本第 2 點把那八個詞一個一個列出來給 Codex 看、這個檔也因此不在擋字串的掃描範圍——
        Codex 的第四次審查指出：禁止的詞出現在公開檔裡，而且沒有人掃。現在不列詞（清單與唯一的例外句以掃描器那個檔為準），這個檔跟別的公開檔一樣要掃、沒有例外。
        對照組：把八個詞的任何一個寫回 AGENTS.md → 紅。"""
        text = read("AGENTS.md")
        self.assertEqual(G.judgement_hits(text), [])
        self.assertEqual(len(G.JUDGEMENT_WORDS), 8)
        rule2 = [l for l in text.split("\n") if l.startswith("2. **擋字串**")]
        self.assertEqual(len(rule2), 1)
        self.assertIn("不得出現擋字串。清單與唯一的例外句在 `scripts/test_analysis_guards.py`，審查時請以該檔為準。", rule2[0])
        for word in G.JUDGEMENT_WORDS:                                             # 掃描器真的抓得到：把任何一個詞寫回第 2 點，掃描就會命中
            self.assertEqual(G.judgement_hits(text.replace("不得出現擋字串。", "不得出現擋字串：" + word + "。")), [word])
        # 突變清單裡「把詞寫回 AGENTS.md」那八個突變用的詞，要跟掃描器的清單一樣（清單只有掃描器那一份算數）
        import importlib.util
        spec = importlib.util.spec_from_file_location("iw_mutation_defs_for_test", os.path.join(HERE, "mutations", "autopilot_mutations.py"))
        defs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(defs)
        self.assertEqual(defs.BANNED_WORDS, G.JUDGEMENT_WORDS)
        back = [m for m in defs.MUTATIONS if m[2] == "AGENTS.md" and "寫回" in m[1]]
        self.assertEqual(len(back), 8)
        self.assertEqual(sorted(w for m in back for w in G.JUDGEMENT_WORDS if "（" + w + "）" in m[4]), sorted(G.JUDGEMENT_WORDS))

    def test_the_runner_image_is_pinned(self):
        """2026-10-06 裁決第一節：驗收機的執行機版本寫死 ubuntu-24.04，不用 ubuntu-latest——GitHub 換預設版本的時候（2026-10-19 起分批換成 26.04），
        三層封鎖不可以跟著不知不覺地變。升級是一件記在待辦裡的事：先開丟棄分支試跑，再走「驗收機變更」。對照組：改回 ubuntu-latest → 紅。"""
        text = read(".github/workflows/verify.yml")
        self.assertEqual(re.findall(r"(?m)^\s*runs-on:\s*(\S+)\s*$", text), ["ubuntu-24.04"] * 3)
        self.assertNotIn("runs-on: ubuntu-latest", text)
        self.assertIn("驗收機升到 Ubuntu 26.04", read("docs/AUTOPILOT.md"))

    def test_config_has_the_rerun_limit_and_the_round_caps(self):
        """2026-10-06 裁決：同一個 commit 的驗收最多重跑 2 次；Codex 的審查一般的階段最多 4 輪、P2 自己 6 輪（之後加開 2 輪、再加開 1 輪，共 9）。
        數字只寫在設定檔。"""
        self.assertEqual(CFG["verify"]["maxReruns"], 2)
        self.assertEqual(CFG["externalReview"]["maxRounds"], {"default": 4, "P2": 9})
        self.assertIn(["run", "rerun"], ALLOW["programs"]["ghAllowed"])
        guide = read("docs/AUTOPILOT.md")
        for must in ("gh run rerun", "最多重跑 2 次", "封鎖沒設成", "IPv4 與 IPv6", "ubuntu-24.04", "P2 是 6 輪", "一般的階段 4 輪", "上限不是自動放行",
                     "Cowork 裁決：<檔名>", "延後到 <階段>，Cowork 裁決：<檔名>", "不分是哪個 commit", "Cowork裁決", "2027-04-06",
                     "到了輪數上限怎麼收", "加過一次就不再加", "6＋2", "公開文字掃描統一", "4191058953", "測試在自己的程序裡說謊",
                     "誰觸發審查不影響完成訊號；程式只認機器人對最新 commit 的 review 或進度留言", "驗收機不做具名字串掃描", "未跑（無鹽）",
                     "6＋2＋1", "由檢查程式自己帶鹽掃", "每一筆要推的 commit 改到的檔", "Cowork 裁決：裁決-<時間>.md", "P0 不能不採納，有裁決也一樣"):
            self.assertIn(must, guide, must)
        steps = read(".claude/skills/iw-autopilot/pr-steps.md")
        for must in ("gh run rerun", "輪", "Cowork 裁決："):
            self.assertIn(must, steps, must)

    def test_docs_and_skill_cover_the_third_party(self):
        skill = read(".claude/skills/iw-autopilot/SKILL.md")
        for must in ("驗收機", "@codex review", "免外部審查", "pr-steps.md", "03_第三方審查.md"):
            self.assertIn(must, skill, must)
        steps = read(".claude/skills/iw-autopilot/pr-steps.md")
        self.assertLess(len(steps), 6000)
        for must in ("iw_notify.py pr open", "request-review", "verify --stage", "review-status", "免外部審查", "不採納", "resolve", "03_第三方審查.md"):
            self.assertIn(must, steps, must)
        guide = read("docs/AUTOPILOT.md")
        for must in ("驗收機", "Codex", "免外部審查 <階段>", "AGENTS.md", "scripts/verify_ci.py", "scripts/mutations/", "互動限制", "Run failed",
                     "@codex review", "本階段未經外部審查", "九句話", "驗收機變更 <階段>", "模型分工", "Cowork回覆",
                     "https://chatgpt.com/settings/code-review", "流程檔檢查", "推得動流程檔"):
            self.assertIn(must, guide, must)
        for must in (CFG["requiredModelLabel"], CFG["requiredModel"], CFG["reviewer"]["model"], "Max"):          # 模型分工表跟設定檔一致
            self.assertIn(must, guide, must)
        self.assertIn(CFG["requiredModel"], skill)
        self.assertIn("驗收機變更", skill)
        criteria = read(".claude/skills/iw-autopilot/review-criteria.md")
        for must in ("驗收機", "外部審查（GPT）", "03_第三方審查.md"):
            self.assertIn(must, criteria, must)
        self.assertIn("驗收機", read(".claude/skills/iw-autopilot/report-template.md"))

    def test_mutation_list_lives_in_the_repo_and_its_anchors_hold(self):
        """突變清單搬進倉庫（驗收機才跑得到）：每個錨點在現在的程式裡剛好出現一次；已知例外都是真的編號；有 P2 的 Q 系列。"""
        sys.path.insert(0, os.path.join(HERE, "mutations"))
        import run_mutations as RM
        defs, baselines = RM.load_defs(RM.DEFAULT_DEFS)
        self.assertEqual(RM.check_anchors(ROOT, defs), [])
        ids = [m[0] for m in defs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(set(RM.load_known(RM.DEFAULT_KNOWN)) <= set(ids))
        self.assertTrue(any(i.startswith("Q") for i in ids))
        self.assertIn("verify", [b[0] for b in baselines])

    def test_the_verify_workflow_when_present_is_read_only_and_never_runs_on_main(self):
        """驗收機的流程檔：只讀、不觸發於 main（黃金排程與資料更新不受影響）、沒有 secret、沒有 models 權限（GitHub Models 已退役）、
        不用 pull_request_target、判定用 main 上的驗收程式。"""
        text = read(".github/workflows/verify.yml")
        self.assertRegex(text, r'(?m)^  push:\n    branches: \["feat/\*\*"\]\n    tags: \["stop\*"\]\n  workflow_dispatch:')
        self.assertNotIn("schedule:", text)
        self.assertNotRegex(text, r"(?m)^\s*pull_request")
        self.assertIn("--main-ref refs/remotes/origin/main", text)
        self.assertEqual(sorted(set(re.findall(r"uses: (\S+)", text))),
                         ["actions/checkout@v4", "actions/download-artifact@v4", "actions/setup-python@v5", "actions/upload-artifact@v4"])   # 只用 GitHub 自家的 action
        self.assertNotIn("github.event.", text)                                    # 不把事件裡的字串塞進指令列
        self.assertRegex(text, r"(?m)^permissions:\n  contents: read\n")
        for bad in ("models:", "secrets.", "pull_request_target", "contents: write"):
            self.assertNotIn(bad, text, bad)
        self.assertIn('branches: ["feat/**"]', text)
        self.assertNotRegex(text, r"(?m)^\s*-\s*main\s*$")
        self.assertEqual(text.count("extract-verifier"), 3)                         # 三個 job 各取一次

    def test_the_verifier_bootstrap_comes_from_main_and_runs_isolated(self):
        """Codex 對 P2 的第九次審查（P0）：「取 main 上的驗收程式」那一步不可以先跑到分支的東西。三個 job 的那一步都要：
        啟動用的那一份用 git 從 main 取到 checkout 外面（main 上還沒有的建立期才複製分支的），用 -I 執行；
        流程檔裡沒有任何一步直接執行 checkout 裡的 scripts/verify_ci.py。實際擋不擋得住在 test_verify_ci 另外有一條做給它看。
        對照組：有一個 job 改回直接執行 checkout 裡的那一份 → 紅。"""
        text = read(".github/workflows/verify.yml")
        self.assertNotRegex(text, r"(?m)^[^#\n]*python[^\n]*\sscripts/verify_ci\.py")
        for must in ('boot="$RUNNER_TEMP/bootstrap/verify_ci.py"',
                     'git show refs/remotes/origin/main:scripts/verify_ci.py > "$boot" 2>/dev/null || cp scripts/verify_ci.py "$boot"',
                     'python -I "$boot" extract-verifier --repo "$GITHUB_WORKSPACE" --main-ref refs/remotes/origin/main --dest "$RUNNER_TEMP/verifier" | tee -a "$GITHUB_OUTPUT"'):
            self.assertEqual(text.count(must), 3, must)
        for job in ("verify", "mutations", "collect"):
            body = text[text.index("\n  %s:" % job):]
            self.assertLess(body.index('python -I "$boot" extract-verifier'), body.index("steps.verifier.outputs.dir"), job)   # 先取、後面每一步才用取出來的那一份
            self.assertLess(body.index("          set -o pipefail\n"), body.index('python -I "$boot" extract-verifier'), job)  # Python 失敗那一步自己要紅（後面接了 tee）
        self.assertEqual(text.count("          set -o pipefail\n"), 3)
        self.assertRegex(read("docs/AUTOPILOT.md"), r"啟動用的那一份[^\n]*-I")

    def test_the_main_branch_on_the_remote_is_always_named_in_full(self):
        """git 解析短名 origin/main 的順序是標籤 → 本機分支 → 遠端的記號；驗收機會把標籤全部抓下來，所以推一個同名的標籤就能換掉「main 上的驗收程式」
        （2026-10-06 審查代理指出，Cowork 裁決在 P2 修）。流程檔裡指 main 的每一處都寫全名，跟檢查程式、驗收程式用的是同一個字串；
        檢查程式裡不再自己用「遠端/分支」組短名。對照組：流程檔有一處改回短名、檢查程式有一處改回短名 → 紅。"""
        sys.path.insert(0, HERE)
        import verify_ci as V
        full = C.remote_main_ref(CFG)
        self.assertEqual(full, "refs/remotes/origin/main")
        self.assertEqual((V.MAIN_REF, C.REMOTE_REF_FORMAT % ("origin", "main")), (full, full))
        text = read(".github/workflows/verify.yml")
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))      # 說明文字裡提到短名不算
        self.assertEqual(code.count(full), 9)                                                 # 取啟動檔 3 處＋--main-ref 6 處
        self.assertEqual(code.count("origin/main"), 9)                                        # 沒有任何一處是不帶 refs/remotes/ 的短名
        self.assertEqual(code.count("--main-ref " + full), 6)
        for ref, want in (("origin/main", full), ("refs/remotes/origin/main", full), ("", full), (None, full), ("  origin/main ", full),
                          ("refs/heads/main", "refs/heads/main"), ("upstream/dev", "refs/remotes/upstream/dev")):
            self.assertEqual(V.full_main_ref(ref), want, ref)
        for name in ("iw_notify.py", "iw_review.py", "iw_events.py", "iw_prepush.py", "iw_guard.py"):
            src = read(".claude/hooks/" + name)
            self.assertNotRegex(src, r'"%s/%s(\.\.\.HEAD)?" % \((env\.)?cfg\["remote"\], (env\.)?cfg\["mainBranch"\]\)', name)   # 不自己組短名
            self.assertNotIn('"origin/" + MAIN + "...HEAD"', src, name)
            self.assertNotIn('"%s/%s"', src, name)                                            # 用區域變數組「遠端/分支」的寫法也沒有（審查代理抓到漏了一處）
            self.assertNotIn('"refs/remotes/%s/%s" % (', src, name)                           # 全名也只從 iw_common 那一個地方來
        guide = read("docs/AUTOPILOT.md")
        for must in ("`refs/remotes/origin/main`", "`set -o pipefail`", "名字以 `origin/`、`refs/`、`remotes/` 開頭的分支與標籤",
                     "既有的測試與突變有沒有被刪改", "4195162361、4195162374", "後路拿掉"):
            self.assertIn(must, guide, must)


if __name__ == "__main__":
    unittest.main()
