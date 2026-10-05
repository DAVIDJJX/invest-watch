#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_verify_ci.py — 停點 P2「第三方審核」：驗收機（scripts/verify_ci.py）與突變執行器（scripts/mutations/）的離線測試

跑法：python -m unittest discover -s scripts -p "test_*.py"
全部離線、不需要 sudo、不連網：判定、比對、解析、掃描都是純函式；第 3 層封鎖（sitecustomize）用子程序實測；
取驗收程式那一步用暫存資料夾裡的小倉庫實測。

釘住的事（David 2026-10-04 的裁決）：
  1. 台銀與資料來源網域出現→紅；未知主機→紅；Chrome 自己的背景連線→列出、不紅；loopback 不算。
  2. 測試數或突變數變少→紅；被刪改的測試與突變逐條列出；存活的突變不在 main 的例外清單→紅；錨點錯誤→紅。
  3. 結果檔含不該公開的字串（本機路徑、權杖、信箱、個人資料樣式）→紅。
  4. 系統層封鎖拿不到→不紅，但結果寫封鎖層級 2／3。
  5. 分支改了驗收機本身→結果標 verifier_changed（判定照 main 的版本做；關卡那邊不認）。
  6. Python 層：任何對外的名稱解析與連線都被拒絕並記下來；loopback 照常。
  7. 突變清單：每個錨點在倉庫裡剛好出現一次；已知例外的鍵都是真的突變編號。
  8.（2026-10-05，Codex 對 P2 的意見）系統層擋下的連線只留下 IP、看不出是連誰→紅；突變分片也在封鎖裡跑，
     每一段的對外請求合起來判定，少一片的封鎖紀錄→紅；跟 main 比（會載入分支上的突變定義）也在封鎖裡跑。
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

os.environ["IW_TEST_NO_SIDE_EFFECTS"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "mutations"))
import verify_ci as V            # noqa: E402
import run_mutations as RM       # noqa: E402

ALLOWED = ["query1.finance.yahoo.com", "api.finmindtrade.com"]
GREEN_TESTS = {"defined": 830, "ran": 830, "passed": 828, "failed": [], "errors": [], "skipped": [{"name": "a.b", "reason": "沒有鹽"}], "unexpected_successes": [], "ok": True}
GREEN_CMP = {"tests_defined_main": 820, "mutations_total": 150, "mutations_main": 146, "mutation_anchor_problems": [], "verifier_changed": False}
GREEN_MUT = {"total_ran": 150, "red": 149, "survivors": ["M105"], "known_exceptions": ["M105"], "unexplained_survivors": [], "anchor_errors": [], "missing": 0}
GREEN_LOCK = {"layers": {"python": "sitecustomize", "browser": "x", "ip": "iptables"}, "level": "3/3"}


def egress(hosts=None, blocked=None, layers=None):
    raw = {"hosts": dict((h, {"count": 1, "via": ["python"]}) for h in (hosts or [])), "blocked_ips": blocked or [],
           "layers": layers or GREEN_LOCK["layers"], "level": "3/3", "python_events": len(hosts or [])}
    return V.judge_egress(raw, ALLOWED)


class TestHostClassification(unittest.TestCase):
    def test_classes(self):
        for host, want in (("rate.bot.com.tw", "bot"), ("www.bot.com.tw", "bot"), ("BOT.COM.TW.", "bot"),
                           ("query1.finance.yahoo.com", "source"), ("api.finmindtrade.com", "source"), ("api.github.com", "source"),
                           ("objects.githubusercontent.com", "source"), ("davidjjx.github.io", "source"),
                           ("update.googleapis.com", "browser"), ("clients2.google.com", "browser"), ("www.gstatic.com", "browser"),
                           ("edgedl.me.gvt1.com", "browser"), ("example.invalid", "unknown"), ("evil.example.net", "unknown"),
                           ("localhost", "loopback"), ("127.0.0.1", "loopback"), ("::1", "loopback"), ("lockdown-selftest.invalid", "loopback"), ("", "loopback")):
            self.assertEqual(V.classify_host(host, ALLOWED), want, host)

    def test_noise_list_is_not_everything(self):
        """對照組：噴音名單放寬成「所有主機都是噪音」→ 這一條會紅。"""
        self.assertEqual(V.classify_host("evil.example.net", ALLOWED), "unknown")
        self.assertEqual(V.classify_host("rate.bot.com.tw", ALLOWED), "bot")
        self.assertTrue(all(p.endswith("$") for p in V.BROWSER_NOISE))                  # 名單是尾碼比對，不是子字串


class TestJudge(unittest.TestCase):
    def green(self, **over):
        kw = {"tests": GREEN_TESTS, "egress": egress(["update.googleapis.com", "127.0.0.1"]), "cmp_": GREEN_CMP, "mut": GREEN_MUT, "lockdown": GREEN_LOCK, "privacy": []}
        kw.update(over)
        return V.judge(kw["tests"], kw["egress"], kw["cmp_"], kw["mut"], kw["lockdown"], kw["privacy"])

    def test_green_when_everything_is_fine(self):
        self.assertEqual(self.green(), [])

    def test_bot_hits_are_red(self):
        """對照組：拿掉台銀那一條 → 紅。"""
        reasons = self.green(egress=egress(["rate.bot.com.tw"]))
        self.assertTrue(any("台銀" in r for r in reasons), reasons)

    def test_source_and_unknown_hosts_are_red_but_browser_noise_is_listed_only(self):
        self.assertTrue(any("資料來源" in r for r in self.green(egress=egress(["query1.finance.yahoo.com"]))))
        self.assertTrue(any("資料來源" in r for r in self.green(egress=egress(["api.github.com"]))))
        self.assertTrue(any("名單外" in r for r in self.green(egress=egress(["evil.example.net"]))))
        e = egress(["update.googleapis.com", "safebrowsing.googleapis.com"])
        self.assertEqual(self.green(egress=e), [])
        self.assertEqual(e["browser_hosts"], ["safebrowsing.googleapis.com", "update.googleapis.com"])

    def test_blocked_ips_are_red(self):
        """系統層擋下的連線（不是 Python 的名稱解析、也不是瀏覽器記到的，例如測試叫了 curl）只留下 IP，看不出是連誰：一律紅。
        原本只列出來、不算紅（Codex 對 P2 的意見）。對照組：不看 blocked_ips → 紅。"""
        e = egress([], blocked=[{"ip": "1.1.1.1", "proto": "TCP", "port": "443", "count": 3}])
        reasons = self.green(egress=e)
        self.assertTrue(any("系統層擋下了對外連線" in r and "1.1.1.1:443" in r for r in reasons), reasons)
        self.assertEqual(self.green(egress=egress([], blocked=[])), [])

    def test_failing_tests_and_no_tests_are_red(self):
        self.assertTrue(any("測試有 1 條紅" in r for r in self.green(tests=dict(GREEN_TESTS, failed=["x.y"], ok=False))))
        self.assertTrue(any("沒有跑到" in r for r in self.green(tests=dict(GREEN_TESTS, ran=0))))
        self.assertTrue(any("tests.json 不在" in r for r in self.green(tests={})))

    def test_fewer_tests_or_mutations_than_main_is_red(self):
        """對照組：拿掉「測試數不准少」→ 紅。"""
        self.assertTrue(any("測試數變少" in r for r in self.green(cmp_=dict(GREEN_CMP, tests_defined_main=831))))
        self.assertTrue(any("突變數變少" in r for r in self.green(cmp_=dict(GREEN_CMP, mutations_total=140))))
        self.assertEqual(self.green(cmp_=dict(GREEN_CMP, tests_defined_main=None, mutations_main=None)), [])      # P2 第一次：main 沒有可比的

    def test_unexplained_survivors_and_anchor_errors_are_red(self):
        self.assertTrue(any("例外清單" in r for r in self.green(mut=dict(GREEN_MUT, unexplained_survivors=["N09"]))))
        self.assertTrue(any("錨點" in r for r in self.green(mut=dict(GREEN_MUT, anchor_errors=["M01"]))))
        self.assertTrue(any("錨點" in r for r in self.green(cmp_=dict(GREEN_CMP, mutation_anchor_problems=["M01：錨點出現 2 次"]))))
        self.assertTrue(any("沒跑到" in r for r in self.green(mut=dict(GREEN_MUT, missing=3))))

    def test_privacy_hits_are_red(self):
        self.assertTrue(any("不該公開" in r for r in self.green(privacy=["verify-result：本機的絕對路徑"])))

    def test_missing_ip_layer_is_not_red_but_missing_python_layer_is(self):
        """David 的裁決 3：系統層拿不到可以，但要寫層級 2／3；Python 層沒有就是紅。"""
        self.assertEqual(self.green(lockdown={"layers": {"python": "sitecustomize", "browser": "x", "ip": None}, "level": "2/3"}), [])
        self.assertTrue(any("Python 層" in r for r in self.green(lockdown={"layers": {"python": None, "browser": "x", "ip": "y"}, "level": "2/3"})))

    def test_verifier_changed_is_a_flag_not_a_reason(self):
        self.assertEqual(self.green(cmp_=dict(GREEN_CMP, verifier_changed=True)), [])


class TestEveryJobRunsUnderLockdown(unittest.TestCase):
    """2026-10-05 Codex 對 P2 的意見：突變分片原本沒有封鎖、對外請求也沒有收集。凡是會執行分支上程式碼的步驟
    （全套測試、每一片突變、跟 main 比時載入突變定義）都要在封鎖裡跑；判定要看每一段。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-collect-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = (V.sudo, V.run)

        def restore():
            V.sudo, V.run = saved
        self.addCleanup(restore)

    def test_the_locked_command_carries_the_guard_and_uses_the_test_user(self):
        V.sudo = lambda cmd, timeout=120: (0, "")
        out = os.path.join(self.tmp, "out")
        cmd, env = V.locked_command(out, {"user": "iwtest", "browser": "/x/wrapper.sh"}, ["python", "x.py"])
        self.assertEqual(cmd[:6], ["sudo", "-n", "-u", "iwtest", "-H", "env"])
        self.assertEqual(cmd[-2:], ["python", "x.py"])
        self.assertIn("PYTHONPATH=" + os.path.join(out, "pyguard"), cmd)
        self.assertIn("IW_EGRESS_LOG=" + os.path.join(out, "egress", "python.jsonl"), cmd)
        self.assertIn("IW_BROWSER=/x/wrapper.sh", cmd)
        self.assertIsNone(env)
        cmd, env = V.locked_command(out, {"user": None, "browser": "/x/wrapper.sh"}, ["python", "x.py"])     # 沒有測試用的使用者：Python 層與瀏覽器層照樣帶上
        self.assertEqual(cmd, ["python", "x.py"])
        self.assertEqual((env["PYTHONPATH"], env["IW_BROWSER"]), (os.path.join(out, "pyguard"), "/x/wrapper.sh"))

    def test_mutations_and_compare_run_inside_the_lockdown(self):
        """對照組：突變不進封鎖、或跟 main 比不進封鎖 → 紅。"""
        ran = []
        V.sudo = lambda cmd, timeout=120: (0, "")
        V.run = lambda cmd, cwd=None, timeout=120, env=None, inp=None: ran.append(list(cmd)) or (0, "")
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        a = argparse.Namespace(repo=self.tmp, out=out, shard="1/2", main_ref="origin/main", copy_dir=self.tmp, timeout=5, inner=False)
        with contextlib.redirect_stdout(io.StringIO()) as said:
            V.cmd_mutations(a)                                                           # 沒有封鎖紀錄：照樣跑，但會說出來（判定時這一片算紅）
            self.assertIn("沒有封鎖紀錄", said.getvalue())
            self.assertNotEqual(ran[-1][0], "sudo")
            V.write_json(os.path.join(out, "lockdown.json"), {"user": "iwtest", "level": "3/3", "layers": GREEN_LOCK["layers"], "browser": "/x/w.sh"})
            V.cmd_mutations(a)
            cmd = ran[-1]
            self.assertEqual(cmd[:4], ["sudo", "-n", "-u", "iwtest"])
            self.assertTrue(any(str(x).endswith("run_mutations.py") for x in cmd))
            self.assertIn("PYTHONPATH=" + os.path.join(out, "pyguard"), cmd)
            n = len(ran)
            V.cmd_compare(a)                                                             # 會載入分支上的突變定義：也進封鎖，再用 --inner 真的做
            self.assertEqual(len(ran), n + 1)
            cmd = ran[-1]
            self.assertEqual(cmd[:4], ["sudo", "-n", "-u", "iwtest"])
            self.assertIn("compare", cmd)
            self.assertEqual(cmd[-1], "--inner")

    def test_judge_wants_a_lockdown_record_for_every_mutation_shard(self):
        mut = dict(GREEN_MUT, shards=["1/2", "2/2"])

        def j(mut_lockdowns):
            return V.judge(GREEN_TESTS, egress([]), GREEN_CMP, mut, GREEN_LOCK, [], mut_lockdowns=mut_lockdowns)
        self.assertEqual(j([GREEN_LOCK, GREEN_LOCK]), [])
        self.assertTrue(any("1 片突變沒有封鎖紀錄" in r for r in j([GREEN_LOCK])))
        self.assertTrue(any("Python 層封鎖沒有生效" in r for r in j([GREEN_LOCK, {"layers": {"python": None}, "level": "0/3"}])))
        self.assertEqual(V.judge(GREEN_TESTS, egress([]), GREEN_CMP, mut, GREEN_LOCK, []), [])               # 舊的呼叫方式（沒給這一項）不檢查

    def test_egress_from_every_job_is_merged(self):
        a = {"hosts": {"update.googleapis.com": {"count": 2, "via": ["browser"]}}, "blocked_ips": [], "python_events": 1, "browser_netlogs": 3}
        b = {"hosts": {"rate.bot.com.tw": {"count": 1, "via": ["python"]}, "update.googleapis.com": {"count": 1, "via": ["python"]}},
             "blocked_ips": [{"ip": "9.9.9.9", "proto": "TCP", "port": "443", "count": 2}], "python_events": 2}
        c = {"hosts": {}, "blocked_ips": [{"ip": "9.9.9.9", "proto": "TCP", "port": "443", "count": 1}]}
        m = V.merge_egress_raw([a, b, {}, c])
        self.assertEqual(m["jobs"], 3)
        self.assertEqual(m["hosts"]["update.googleapis.com"], {"count": 3, "via": ["browser", "python"]})
        self.assertEqual(m["blocked_ips"], [{"ip": "9.9.9.9", "proto": "TCP", "port": "443", "count": 3}])
        self.assertEqual((m["python_events"], m["browser_netlogs"]), (3, 3))
        self.assertEqual(V.judge_egress(m, ALLOWED)["bot_hits"], 1)                                          # 突變那一片裡的一次台銀，合起來之後照樣算
        self.assertEqual(V.weakest_level([{"level": "3/3"}, {"level": "2/3"}]), "2/3")
        self.assertEqual(V.weakest_level([{"level": "3/3"}, None]), "0/3")

    def collect(self, shard_egress=None, shard2_lock=True):
        inp = os.path.join(self.tmp, "in")
        shutil.rmtree(inp, True)
        raw = {"hosts": {}, "blocked_ips": [], "python_events": 0, "browser_netlogs": 0}
        main = os.path.join(inp, "verify-partial")
        os.makedirs(main)
        V.write_json(os.path.join(main, "tests.json"), GREEN_TESTS)
        V.write_json(os.path.join(main, "compare.json"), dict(GREEN_CMP, mutations_total=2, mutations_main=None))
        V.write_json(os.path.join(main, "lockdown.json"), GREEN_LOCK)
        V.write_json(os.path.join(main, "egress.json"), raw)
        for i in (1, 2):
            d = os.path.join(inp, "verify-mutations-%d" % i)
            os.makedirs(d)
            V.write_json(os.path.join(d, "mut-%d-of-2.json" % i), {"shard": "%d/2" % i, "results": [{"id": "M0%d" % i, "ok": True, "red": ["x"], "ran": 1}]})
            V.write_json(os.path.join(d, "egress.json"), shard_egress if (i == 1 and shard_egress) else raw)
            if i == 1 or shard2_lock:
                V.write_json(os.path.join(d, "lockdown.json"), GREEN_LOCK)
        out = tempfile.mkdtemp(prefix="out-", dir=self.tmp)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = V.cmd_collect(argparse.Namespace(repo=ROOT, out=out, inputs=inp, main_ref="origin/main", summary=None))
        return rc, V.read_json(os.path.join(out, "verify-result.json"), {})

    def test_collect_reads_every_job_and_reds_on_a_shard_that_went_out_or_had_no_lockdown(self):
        """對照組：判定只看全套測試那一段、或少一片封鎖紀錄也不紅 → 紅。"""
        rc, res = self.collect()
        self.assertEqual((rc, res["red"], res["egress"]["jobs"], len(res["lockdown"]["mutation_shards"]), res["egress"]["level"]),
                         (0, False, 3, 2, "3/3"), res.get("reasons"))
        rc, res = self.collect(shard_egress={"hosts": {"rate.bot.com.tw": {"count": 1, "via": ["python"]}}, "blocked_ips": [], "python_events": 1})
        self.assertEqual(rc, 1)                                                          # 突變那一片連了台銀
        self.assertTrue(any("台銀" in r for r in res["reasons"]), res["reasons"])
        rc, res = self.collect(shard_egress={"hosts": {}, "blocked_ips": [{"ip": "9.9.9.9", "proto": "TCP", "port": "443", "count": 1}]})
        self.assertEqual(rc, 1)                                                          # 突變那一片有一個系統層擋下的連線
        self.assertTrue(any("系統層擋下了對外連線" in r for r in res["reasons"]), res["reasons"])
        rc, res = self.collect(shard2_lock=False)                                        # 有一片沒有封鎖紀錄
        self.assertEqual(rc, 1)
        self.assertTrue(any("1 片突變沒有封鎖紀錄" in r for r in res["reasons"]), res["reasons"])
        self.assertEqual(res["egress"]["level"], "0/3")


class TestCompareHelpers(unittest.TestCase):
    SRC = "import unittest\nclass TestA(unittest.TestCase):\n    def test_one(self):\n        self.assertTrue(1)\n\n    def test_two(self):\n        pass\n\n    def helper(self):\n        pass\n"

    def test_static_test_counting(self):
        funcs = V.test_functions(self.SRC)
        self.assertEqual(sorted(funcs), ["TestA.test_one", "TestA.test_two"])
        self.assertEqual(V.test_functions("def broken(:\n"), {})
        tests = V.collect_tests({"scripts/test_a.py": self.SRC})
        self.assertEqual(len(tests), 2)

    def test_removed_and_modified_are_listed(self):
        main = V.collect_tests({"scripts/test_a.py": self.SRC})
        branch = V.collect_tests({"scripts/test_a.py": self.SRC.replace("def test_two(self):\n        pass", "def test_two(self):\n        self.assertEqual(1, 1)").replace(
            "    def test_one(self):\n        self.assertTrue(1)\n\n", "")})
        removed, modified = V.diff_items(main, branch)
        self.assertEqual(removed, ["scripts/test_a.py::TestA.test_one"])
        self.assertEqual(modified, ["scripts/test_a.py::TestA.test_two"])

    def test_mutation_items_follow_file_and_anchor(self):
        a = [("M01", "x", "f.py", "old", "new", ["t"])]
        b = [("M01", "x", "f.py", "old", "NEW", ["t"])]
        self.assertEqual(V.diff_items(V.mutation_items(a), V.mutation_items(b)), ([], ["M01"]))
        self.assertEqual(V.diff_items(V.mutation_items(a), V.mutation_items([])), (["M01"], []))

    def test_glob_match_like_the_hooks(self):
        self.assertTrue(V.glob_match(".github/workflows/verify.yml", [".github/workflows/**"]))
        self.assertTrue(V.glob_match("scripts/mutations/run_mutations.py", ["scripts/mutations/**"]))
        self.assertTrue(V.glob_match("AGENTS.md", ["AGENTS.md"]))
        self.assertFalse(V.glob_match("scripts/x.py", ["scripts/mutations/**", "AGENTS.md"]))


class TestParsers(unittest.TestCase):
    def test_python_log(self):
        text = "\n".join(json.dumps(x) for x in ({"kind": "dns", "target": "rate.bot.com.tw"}, {"kind": "connect", "target": "1.2.3.4:443"},
                                                 {"kind": "dns", "target": "rate.bot.com.tw"})) + "\nnot json\n"
        hosts, events = V.parse_python_log(text)
        self.assertEqual(hosts, {"rate.bot.com.tw": 2, "1.2.3.4": 1})
        self.assertEqual(events, 3)

    def test_chrome_netlog_hosts(self):
        text = '{"events":[{"params":{"host":"update.googleapis.com:443"}},{"params":{"url":"https://clients2.google.com/x"}},{"params":{"host":"Rate.bot.com.tw"}}]}'
        self.assertEqual(V.parse_netlog_hosts(text), {"update.googleapis.com": 1, "clients2.google.com": 1, "rate.bot.com.tw": 1})

    def test_iptables_log(self):
        text = ("[  12.3] IWEGRESS IN= OUT=eth0 SRC=10.1.0.4 DST=142.250.72.14 LEN=60 TOS=0x00 PREC=0x00 TTL=64 ID=1 DF PROTO=TCP SPT=43210 DPT=443 WINDOW=1 RES=0x00 SYN URGP=0\n"
                "[  12.4] IWEGRESS IN= OUT=eth0 SRC=10.1.0.4 DST=142.250.72.14 LEN=60 PROTO=TCP SPT=43211 DPT=443\n"
                "[  12.5] IWEGRESS IN= OUT=eth0 SRC=10.1.0.4 DST=8.8.8.8 LEN=60 PROTO=UDP SPT=5 DPT=53\nsomething else\n")
        self.assertEqual(V.parse_iptables_log(text), [{"ip": "142.250.72.14", "proto": "TCP", "port": "443", "count": 2},
                                                      {"ip": "8.8.8.8", "proto": "UDP", "port": "53", "count": 1}])

    def test_merge_mutation_shards(self):
        shards = [{"shard": "1/2", "results": [{"id": "基準-guard", "ok": True}, {"id": "M01", "ok": True}, {"id": "M105", "ok": False}]},
                  {"shard": "2/2", "results": [{"id": "N09", "ok": False}, {"id": "M02", "ok": False, "error": "錨點出現 2 次"}]}]
        m = V.merge_mutations(shards, {"M105": "known"}, expected_total=5)
        self.assertEqual((m["total_ran"], m["red"], m["survivors"], m["known_exceptions"], m["unexplained_survivors"], m["anchor_errors"], m["missing"]),
                         (4, 1, ["M105", "N09"], ["M105"], ["N09"], ["M02"], 1))


class TestPrivacyScan(unittest.TestCase):
    def test_catches_local_paths_tokens_and_mail(self):
        # 這幾個「不該出現的東西」用拼接組出來：這個測試檔自己也在自動駕駛檔的隱私掃描範圍裡，寫成字面值會被掃到
        hits = V.privacy_scan({"a": "see D:" + "\\Claude_" + "use\\x", "b": "C:/Us" + "ers/someone/x", "c": "ghp_" + "A" * 30,
                               "d": "mail someone" + "@" + "example.org", "e": "fine t@example.invalid noreply@github.com 1.2.3"})
        self.assertEqual(sorted(h.split("：")[0] for h in hits), ["a", "b", "c", "d"])

    def test_uses_the_guards_module_when_given(self):
        planted = "持有 " + "10 股"                                                      # 拼接：這個檔自己也在隱私掃描範圍裡

        class Guards(object):
            def privacy_hits(self, t):
                return ["持倉"] if planted in t else []

            def profile_key_hits(self, t):
                return []

            def fxplan_key_hits(self, t):
                return []
        self.assertTrue(V.privacy_scan({"x": planted}, Guards()))
        self.assertEqual(V.privacy_scan({"x": "ok"}, Guards()), [])

    def test_summary_is_markdown_without_private_things(self):
        res = {"red": False, "commit": "a" * 40, "ref": "feat/stopX", "tests": GREEN_TESTS, "egress": egress(["update.googleapis.com"]), "compare": GREEN_CMP,
               "mutations": GREEN_MUT, "reasons": []}
        md = V.summary_md(res)
        self.assertIn("驗收機：🟢 綠", md)
        self.assertIn("skipped", md)
        self.assertEqual(V.privacy_scan({"summary": md}), [])


class TestPythonLayer(unittest.TestCase):
    """第 3 層：sitecustomize 載進任何 Python 程序後，對外的名稱解析與連線都被拒絕並記下來；loopback 照常。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-pyguard-")
        os.makedirs(os.path.join(self.tmp, "pyguard"))
        with io.open(os.path.join(self.tmp, "pyguard", "sitecustomize.py"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(V.SITECUSTOMIZE)
        self.log = os.path.join(self.tmp, "python.jsonl")
        self.env = dict(os.environ, PYTHONPATH=os.path.join(self.tmp, "pyguard"), IW_EGRESS_LOG=self.log)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_py(self, code):
        p = subprocess.run([sys.executable, "-c", code], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        return p.returncode, p.stderr.decode("utf-8", "replace")

    def test_outbound_names_and_connections_are_refused_and_logged(self):
        rc, err = self.run_py("import socket; socket.getaddrinfo('rate.bot.com.tw', 443)")
        self.assertNotEqual(rc, 0)
        self.assertIn("iw-verify", err)
        rc, err = self.run_py("import socket; s=socket.socket(); s.settimeout(2); s.connect(('1.1.1.1', 80))")
        self.assertNotEqual(rc, 0)
        self.assertIn("iw-verify", err)
        rc, err = self.run_py("import urllib.request; urllib.request.urlopen('http://example.invalid/', timeout=3)")
        self.assertNotEqual(rc, 0)
        hosts, events = V.parse_python_log(io.open(self.log, encoding="utf-8").read())
        self.assertIn("rate.bot.com.tw", hosts)
        self.assertIn("1.1.1.1", hosts)
        self.assertIn("example.invalid", hosts)
        self.assertGreaterEqual(events, 3)

    def test_loopback_still_works(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        got = []

        def accept():
            try:
                c, _ = srv.accept()
                got.append(1)
                c.close()
            except Exception:                                      # noqa: B902
                pass
        t = threading.Thread(target=accept)
        t.daemon = True
        t.start()
        rc, err = self.run_py("import socket; s=socket.create_connection(('127.0.0.1', %d), timeout=5); s.close(); socket.getaddrinfo('localhost', 80)" % port)
        t.join(5)
        srv.close()
        self.assertEqual(rc, 0, err)
        self.assertEqual(got, [1])
        self.assertFalse(os.path.exists(self.log))                                     # 沒有對外，就沒有紀錄


class TestExtractVerifier(unittest.TestCase):
    """取驗收程式：main 上有就用 main 的；還沒有（P2 第一次）就用分支的、標 source=branch。用暫存資料夾裡的小倉庫實測。"""

    def git(self, args, cwd):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        for k in list(env):
            if k.startswith("GIT_") and k not in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
                env.pop(k)
        p = subprocess.run(["git"] + args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        return p.stdout.decode("utf-8", "replace").strip()

    def write(self, repo, rel, text):
        p = os.path.join(repo, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-verifier-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git(["init", "-q", "-b", "main"], self.repo)
        self.git(["config", "commit.gpgsign", "false"], self.repo)
        self.write(self.repo, "scripts/net_policy.py", "ALLOWED_HOSTS = ('a.example',)\n")
        self.write(self.repo, ".claude/autopilot/config.json", json.dumps({"tier1": {"paths": ["AGENTS.md"]}, "selfFiles": {"paths": []}, "tier2": {"paths": []}}))
        self.write(self.repo, "scripts/test_analysis_guards.py", "def privacy_hits(t):\n    return []\ndef profile_key_hits(t):\n    return []\ndef fxplan_key_hits(t):\n    return []\n")
        self.write(self.repo, "scripts/test_a.py", TestCompareHelpers.SRC)
        self.git(["add", "-A"], self.repo)
        self.git(["commit", "-q", "-m", "main"], self.repo)
        self.git(["update-ref", "refs/remotes/origin/main", "HEAD"], self.repo)
        self.git(["checkout", "-q", "-b", "feat/stopX"], self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_time_uses_the_branch_copy_and_says_so(self):
        self.write(self.repo, "scripts/verify_ci.py", "# branch verifier\n")
        self.write(self.repo, "scripts/mutations/run_mutations.py", "MUTATIONS = []\n")
        self.write(self.repo, "scripts/mutations/known_survivors.json", "{}\n")
        self.git(["add", "-A"], self.repo)
        self.git(["commit", "-q", "-m", "branch"], self.repo)
        dest = os.path.join(self.tmp, "verifier")
        rc = V.main(["extract-verifier", "--repo", self.repo, "--main-ref", "origin/main", "--dest", dest])
        self.assertEqual(rc, 0)
        src = json.load(io.open(os.path.join(dest, "verifier-source.json"), encoding="utf-8"))
        self.assertEqual(src["source"], "branch")
        self.assertEqual(src["files"]["scripts/verify_ci.py"], "branch")
        self.assertEqual(src["files"]["scripts/net_policy.py"], "main")                    # 白名單還是 main 的
        self.assertEqual(io.open(os.path.join(dest, "verify_ci.py"), encoding="utf-8").read(), "# branch verifier\n")
        self.assertEqual(src["files"][".github/workflows/verify.yml"], "missing")

    def test_main_copy_wins_over_the_branch(self):
        self.git(["checkout", "-q", "main"], self.repo)
        self.write(self.repo, "scripts/verify_ci.py", "# main verifier\n")
        self.git(["add", "-A"], self.repo)
        self.git(["commit", "-q", "-m", "main2"], self.repo)
        self.git(["update-ref", "refs/remotes/origin/main", "HEAD"], self.repo)
        self.git(["checkout", "-q", "feat/stopX"], self.repo)
        self.write(self.repo, "scripts/verify_ci.py", "# tampered\n")
        self.git(["add", "-A"], self.repo)
        self.git(["commit", "-q", "-m", "tamper"], self.repo)
        dest = os.path.join(self.tmp, "verifier")
        self.assertEqual(V.main(["extract-verifier", "--repo", self.repo, "--main-ref", "origin/main", "--dest", dest]), 0)
        self.assertEqual(io.open(os.path.join(dest, "verify_ci.py"), encoding="utf-8").read(), "# main verifier\n")
        self.assertEqual(json.load(io.open(os.path.join(dest, "verifier-source.json"), encoding="utf-8"))["source"], "main")

    def test_compare_lists_removed_tests_and_touched_protected_files_and_flags_the_verifier(self):
        self.write(self.repo, "scripts/test_a.py", "import unittest\nclass TestA(unittest.TestCase):\n    def test_two(self):\n        pass\n")
        self.write(self.repo, "AGENTS.md", "x\n")
        self.write(self.repo, "scripts/verify_ci.py", "# branch verifier\n")
        self.git(["add", "-A"], self.repo)
        self.git(["commit", "-q", "-m", "branch"], self.repo)
        out = os.path.join(self.tmp, "out")
        self.assertEqual(V.main(["compare", "--repo", self.repo, "--main-ref", "origin/main", "--out", out]), 0)
        cmp_ = json.load(io.open(os.path.join(out, "compare.json"), encoding="utf-8"))
        self.assertEqual((cmp_["tests_defined"], cmp_["tests_defined_main"]), (1, 2))
        self.assertEqual(cmp_["tests_removed"], ["scripts/test_a.py::TestA.test_one"])
        self.assertEqual(cmp_["protected_touched"]["tier1"], ["AGENTS.md"])
        self.assertTrue(cmp_["verifier_changed"])
        self.assertTrue(cmp_["verifier_first_time"])
        self.assertIsNone(cmp_["mutations_main"])
        self.assertEqual(cmp_["mutations_total"], 0)


class TestMutationRunnerHelpers(unittest.TestCase):
    def test_anchor_check_and_sharding(self):
        tmp = tempfile.mkdtemp(prefix="iw-anchors-")
        try:
            with io.open(os.path.join(tmp, "f.py"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write("a = 1\nb = 2\nb = 2\n")
            defs = [("X1", "ok", "f.py", "a = 1", "a = 0", ["t.py"]), ("X2", "twice", "f.py", "b = 2", "b = 0", ["t.py"]),
                    ("X3", "missing file", "g.py", "x", "y", ["t.py"]), ("X1", "dup id", "f.py", "a = 1", "a = 9", ["t.py"])]
            problems = RM.check_anchors(tmp, defs)
            self.assertTrue(any("X2" in p and "2 次" in p for p in problems), problems)
            self.assertTrue(any("X3" in p for p in problems))
            self.assertTrue(any("編號重複" in p for p in problems))
            self.assertEqual(RM.check_anchors(tmp, defs[:1]), [])
            # 改壞之後連語法都不對＝定義寫錯（2026-10-04 的 Q18：錨點切在註解中間，測試一條都沒跑到，看起來像「沒有紅」）
            broken = RM.check_anchors(tmp, [("X5", "syntax", "f.py", "a = 1", "a = (", ["t.py"])])
            self.assertTrue(any("X5" in p and "讀不懂" in p for p in broken), broken)
            self.assertTrue(any("X6" in p and "一樣" in p for p in RM.check_anchors(tmp, [("X6", "noop", "f.py", "a = 1", "a = 1", ["t.py"])])))
            self.assertTrue(any("X7" in p and "測試檔" in p for p in RM.check_anchors(tmp, [("X7", "no tests", "f.py", "a = 1", "a = 0", ["-k", "x"])])))
            with io.open(os.path.join(tmp, "c.json"), "w", encoding="utf-8") as fh:
                fh.write('{"a": [1]}\n')
            self.assertTrue(any("讀不懂" in p for p in RM.check_anchors(tmp, [("X8", "json", "c.json", "[1]", "[1", ["t.py"])])))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        items = list(range(10))
        self.assertEqual(RM.shard_of(items, "1/3"), [0, 3, 6, 9])
        self.assertEqual(RM.shard_of(items, "3/3"), [2, 5, 8])
        with self.assertRaises(SystemExit):
            RM.shard_of(items, "4/3")

    def test_the_anchor_checks_do_not_count_as_catching_a_mutation(self):
        """檢查錨點的那兩條測試在任何突變底下都會紅（原文被改掉了），不能算成證據：只有它們紅＝這個突變沒有被抓到。
        對照組：把它們也算進去 → 這一條會紅。"""
        tmp = tempfile.mkdtemp(prefix="iw-mut-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo, copy = os.path.join(tmp, "repo"), os.path.join(tmp, "copy")
        os.makedirs(repo)
        with io.open(os.path.join(repo, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("X = 1\nY = 1\n")
        with io.open(os.path.join(repo, "t.py"), "w", encoding="utf-8") as fh:
            fh.write("import unittest\nimport a\n\n\nclass T(unittest.TestCase):\n"
                     "    def %s(self):\n        self.assertEqual((a.X, a.Y), (1, 1))\n\n"
                     "    def test_real(self):\n        self.assertEqual(a.Y, 1)\n" % RM.ANCHOR_SELF_TESTS[0])
        RM.fresh_copy(repo, copy)
        only_anchor = RM.run_one(("Z1", "只有檢查錨點的測試會紅", "a.py", "X = 1", "X = 2", ["t.py"]), repo, copy, [sys.executable], None, 120)
        self.assertEqual((only_anchor["ok"], only_anchor["red"], only_anchor.get("error")), (False, [], None))
        self.assertEqual(only_anchor["incidental"], [RM.ANCHOR_SELF_TESTS[0]])
        real = RM.run_one(("Z2", "真的測試也紅", "a.py", "Y = 1", "Y = 2", ["t.py"]), repo, copy, [sys.executable], None, 120)
        self.assertEqual((real["ok"], real["red"]), (True, ["test_real"]))

    def test_known_survivors_file_ignores_the_about_key(self):
        tmp = tempfile.mkdtemp(prefix="iw-known-")
        try:
            p = os.path.join(tmp, "k.json")
            with io.open(p, "w", encoding="utf-8") as fh:
                json.dump({"_about": "x", "M105": "reason"}, fh)
            self.assertEqual(RM.load_known(p), {"M105": "reason"})
            self.assertEqual(RM.load_known(os.path.join(tmp, "none.json")), {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_real_definitions_load_and_their_anchors_are_unique(self):
        """倉庫裡的突變清單：每個錨點在現在的程式裡剛好出現一次（錨點跟著程式漂掉，這一條會紅）；已知例外的鍵都是真的編號。"""
        defs, baselines = RM.load_defs(RM.DEFAULT_DEFS)
        self.assertGreaterEqual(len(defs), 146 + 30)
        self.assertEqual(RM.check_anchors(ROOT, defs), [])
        ids = set(m[0] for m in defs)
        for k in RM.load_known(RM.DEFAULT_KNOWN):
            self.assertIn(k, ids, k)
        self.assertTrue(all(len(b) == 2 and b[1] for b in baselines))


if __name__ == "__main__":
    unittest.main()
