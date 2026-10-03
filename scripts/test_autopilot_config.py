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
    return sorted(set(files))


class TestModelAndEffortAreWrittenOnce(unittest.TestCase):
    def test_settings_match_the_config(self):
        """對照組（規格第 11 節）：把設定裡的模型改掉 → 這一條會紅。"""
        self.assertEqual(CFG["requiredModel"], "claude-fable-5-1")
        self.assertEqual(CFG["requiredEffort"], "xhigh")
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
        self.assertEqual(fm["model"], CFG["requiredModel"])
        self.assertNotIn(fm["model"], ("inherit", "fable", "opus", "sonnet", "haiku", "best", "default"))
        self.assertEqual(fm["effort"], CFG["requiredEffort"])
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
            for literal in ("claude-fable", "xhigh", "invest-data"):
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
        self.assertEqual(CFG["protectionVersion"], "P1-1")
        guide = read("docs/AUTOPILOT.md")
        for must in labels + ["裁決：", "暫停", "P1-1", "先退", "--no-verify"]:
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
                     "Fable 5.1＋Extra high 用得比較兇", "不會偷偷換模型",
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
            if re.search(r"gh[opsu]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}", text):
                hits.append("像權杖的字串")
            for m in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
                # 測試資料用的假地址、git 遠端網址裡的「git@主機」與「@github.com」前面那一段，都不是誰的信箱
                if not (m.endswith(("@example.com", "@example.invalid", "@github.com")) or "noreply" in m or m.startswith("git@")):
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
        self.assertTrue(G.privacy_hits("持有 " + "1,000 股"))                    # 掃描器本身抓得到東西


if __name__ == "__main__":
    unittest.main()
