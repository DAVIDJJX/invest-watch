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
  4. （2026-10-06 的裁決，取代 10/4 的「系統層拿不到→不紅，只寫層級 2／3」）全套測試與每一片突變都要有系統層封鎖，
     IPv4 與 IPv6 的規則都要；哪一段缺了→紅，原因以「封鎖沒設成」開頭，跟測試紅分開；結果檔照樣寫每一段的層級。
     建使用者與設防火牆那幾步逾時 300 秒、失敗後自動再試一次。
  5. 分支改了驗收機本身→結果標 verifier_changed（判定照 main 的版本做；關卡那邊不認）。
  6. Python 層：任何對外的名稱解析與連線都被拒絕並記下來；loopback 照常。
  7. 突變清單：每個錨點在倉庫裡剛好出現一次；已知例外的鍵都是真的突變編號。
  8.（2026-10-05，Codex 對 P2 的意見）系統層擋下的連線只留下 IP、看不出是連誰→紅；突變分片也在封鎖裡跑，
     每一段的對外請求合起來判定，少一片的封鎖紀錄→紅；跟 main 比（會載入分支上的突變定義）也在封鎖裡跑。
  9.（2026-10-05，Codex 對 P2 的第二次審查）每一段在上傳之前先過隱私掃描、只帶判定需要的檔；完整的詳細輸出與瀏覽器的 netlog 不上傳；
     有一段命中、或有一段沒有掃描紀錄→紅。掃描的回報只寫類別與處數，不帶命中的字串本身；子程序的輸出印進執行紀錄之前也先掃。
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
GREEN_LOCK = {"layers": {"python": "sitecustomize", "browser": "x", "ip": "iptables"}, "level": "3/3",
              "iptables": {"ipv4": True, "ipv6": True},
              "selftest": {"python": "blocked+logged", "ip": "blocked", "checkout": "readonly", "results": "readonly", "scratch": "writable"}}
# 每一步跑完之後的封存紀錄（結果檔由原本的使用者收好、受測的 checkout 跟 commit 一樣）
GOOD_STAGE = {"sealed": ["x.json"], "missing": [], "leftovers_stopped": False, "tree_clean": True, "tree_changes": [], "head": "a" * 40}
GREEN_SEAL_MAIN = {"stages": {"run-tests": GOOD_STAGE, "compare": GOOD_STAGE}}
GREEN_SEAL_SHARD = {"stages": {"mutations": GOOD_STAGE}}
# 沒有系統層的那一種（10/6 凌晨真的發生過：執行機太慢，建使用者逾時，那一片只剩 Python 與瀏覽器兩層）
NO_IP_LOCK = {"layers": {"python": "sitecustomize", "browser": "x", "ip": None}, "level": "2/3", "selftest": {"python": "blocked+logged"},
              "notes": ["建不出使用者 iwtest：執行失敗"]}


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

    def test_missing_system_layer_is_red_and_the_reason_says_lockdown_not_tests(self):
        """2026-10-06 的裁決（取代 10/4 裁決三.3「系統層拿不到→不紅」）：缺系統層就是紅；原因以「封鎖沒設成」開頭，跟測試紅分開。
        Python 層沒有也是紅（原本就是）。對照組：缺系統層不判紅 → 紅。"""
        reasons = self.green(lockdown=NO_IP_LOCK)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertTrue(reasons[0].startswith(V.LOCKDOWN_RED + "："), reasons)
        self.assertIn("全套測試那一段沒有完整的系統層封鎖（沒有系統層）", reasons[0])
        self.assertIn("重跑", reasons[0])
        self.assertNotIn("測試有", reasons[0])
        py = self.green(lockdown=dict(GREEN_LOCK, layers={"python": None, "browser": "x", "ip": "y"}, level="2/3"))
        self.assertTrue(any("Python 層" in r and r.startswith(V.LOCKDOWN_RED) for r in py), py)
        both = self.green(lockdown=NO_IP_LOCK, tests=dict(GREEN_TESTS, failed=["x.y"], ok=False))        # 兩種紅各自一條，分得開
        self.assertEqual(sorted(r.startswith(V.LOCKDOWN_RED) for r in both), [False, True], both)

    def test_the_system_layer_needs_ipv4_and_ipv6_and_a_passing_selftest(self):
        """IPv4 與 IPv6 的防火牆規則都要、自我測試要真的被擋下。對照組：只看 IPv4、或不看自我測試 → 紅。"""
        self.assertIsNone(V.system_layer_gap(GREEN_LOCK))
        self.assertEqual(V.system_layer_gap(None), "沒有系統層")
        self.assertEqual(V.system_layer_gap(NO_IP_LOCK), "沒有系統層")
        for ipt, want in (({"ipv4": True, "ipv6": False}, "IPv6"), ({"ipv4": False, "ipv6": True}, "IPv4"), ({"ipv4": True}, "IPv6"), ({}, "IPv4、IPv6"),
                          ({"ipv4": True, "ipv6": "yes"}, "IPv6")):
            gap = V.system_layer_gap(dict(GREEN_LOCK, iptables=ipt))
            self.assertEqual(gap, "防火牆少了 %s 的規則" % want, ipt)
            self.assertTrue(any(gap in r and r.startswith(V.LOCKDOWN_RED) for r in self.green(lockdown=dict(GREEN_LOCK, iptables=ipt))), ipt)
        for st in ({"python": "blocked+logged", "ip": "NOT blocked"}, {"python": "blocked+logged"}):
            self.assertEqual(V.system_layer_gap(dict(GREEN_LOCK, selftest=st)), "系統層的自我測試沒過")
            self.assertTrue(self.green(lockdown=dict(GREEN_LOCK, selftest=st)))
        self.assertTrue(V.firewall_complete({"ipv4": True, "ipv6": True}))
        for ipt in ({"ipv4": True, "ipv6": False}, {"ipv4": False, "ipv6": True}, {"ipv4": True}, {}, None):
            self.assertFalse(V.firewall_complete(ipt), ipt)

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

    def test_a_mutation_shard_without_the_system_layer_is_red(self):
        """2026-10-06 的裁決：每一片突變也都要有系統層封鎖（IPv4 與 IPv6）。10/6 凌晨重跑的那兩片就是這樣：只有 2／3、當時卻是綠的。
        對照組：突變分片缺系統層不判紅 → 紅。"""
        mut = dict(GREEN_MUT, shards=["1/2", "2/2"])
        reasons = V.judge(GREEN_TESTS, egress([]), GREEN_CMP, mut, GREEN_LOCK, [],
                          mut_lockdowns=[dict(GREEN_LOCK, job="verify-mutations-1"), dict(NO_IP_LOCK, job="verify-mutations-2")])
        self.assertEqual(len(reasons), 1, reasons)
        self.assertTrue(reasons[0].startswith(V.LOCKDOWN_RED + "：有 1 片突變沒有完整的系統層封鎖：verify-mutations-2（沒有系統層）"), reasons)
        no6 = dict(GREEN_LOCK, iptables={"ipv4": True, "ipv6": False})
        reasons = V.judge(GREEN_TESTS, egress([]), GREEN_CMP, mut, GREEN_LOCK, [], mut_lockdowns=[no6, NO_IP_LOCK])
        self.assertTrue(any("有 2 片" in r and "第 1 片（防火牆少了 IPv6 的規則）" in r and "第 2 片（沒有系統層）" in r for r in reasons), reasons)
        self.assertEqual(V.judge(GREEN_TESTS, egress([]), GREEN_CMP, mut, GREEN_LOCK, [], mut_lockdowns=[GREEN_LOCK, GREEN_LOCK]), [])

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

    def collect(self, shard_egress=None, shard2_lock=True, export_hits=None, shard2_export=True, main_lock=None, main_seal=GREEN_SEAL_MAIN,
                shard2_seal=GREEN_SEAL_SHARD, main_cmp=None):
        """shard2_lock：True＝第 2 片的封鎖紀錄是完整的；False＝沒有紀錄；給一個 dict＝用那一份紀錄。main_lock：全套測試那一段的封鎖紀錄。
        main_seal／shard2_seal：那一段的封存紀錄（None＝沒有 seal.json）。main_cmp：加進 compare.json 的欄位。"""
        inp = os.path.join(self.tmp, "in")
        shutil.rmtree(inp, True)
        raw = {"hosts": {}, "blocked_ips": [], "python_events": 0, "browser_netlogs": 0}
        main = os.path.join(inp, "verify-partial")
        os.makedirs(main)
        V.write_json(os.path.join(main, "tests.json"), GREEN_TESTS)
        V.write_json(os.path.join(main, "compare.json"), dict(GREEN_CMP, mutations_total=2, mutations_main=None, **(main_cmp or {})))
        V.write_json(os.path.join(main, "lockdown.json"), main_lock or GREEN_LOCK)
        V.write_json(os.path.join(main, "egress.json"), raw)
        if main_seal is not None:
            V.write_json(os.path.join(main, "seal.json"), main_seal)
        V.write_json(os.path.join(main, "export.json"), {"privacy_hits": [], "exported": ["tests.json"]})
        for i in (1, 2):
            d = os.path.join(inp, "verify-mutations-%d" % i)
            os.makedirs(d)
            V.write_json(os.path.join(d, "mut-%d-of-2.json" % i), {"shard": "%d/2" % i, "results": [{"id": "M0%d" % i, "ok": True, "red": ["x"], "ran": 1}]})
            V.write_json(os.path.join(d, "egress.json"), shard_egress if (i == 1 and shard_egress) else raw)
            if i == 1 or shard2_lock:
                V.write_json(os.path.join(d, "lockdown.json"), shard2_lock if (i == 2 and isinstance(shard2_lock, dict)) else GREEN_LOCK)
            if i == 1 or shard2_export:
                V.write_json(os.path.join(d, "export.json"), {"privacy_hits": (export_hits or []) if i == 1 else [], "exported": []})
            seal = GREEN_SEAL_SHARD if i == 1 else shard2_seal
            if seal is not None:
                V.write_json(os.path.join(d, "seal.json"), seal)
        out = tempfile.mkdtemp(prefix="out-", dir=self.tmp)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = V.cmd_collect(argparse.Namespace(repo=ROOT, out=out, inputs=inp, main_ref="origin/main", summary=None))
        with io.open(os.path.join(out, "summary.md"), encoding="utf-8") as fh:
            self.summary = fh.read()
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

    def test_collect_reds_when_any_job_lacks_the_system_layer_and_still_writes_every_level(self):
        """2026-10-06 的裁決：哪一段缺系統層，那一次驗收就是紅；結果檔照樣寫每一段的層級；紅的種類要分得出「封鎖沒設成」與其他。
        對照組：判定那一段不看突變分片的系統層 → 紅。"""
        rc, res = self.collect()
        self.assertEqual((rc, res["red_kind"], res["lockdown_reasons"]), (0, None, []))
        self.assertIn("每一段的封鎖層級", self.summary)
        self.assertNotIn(V.LOCKDOWN_RED, self.summary)
        rc, res = self.collect(shard2_lock=NO_IP_LOCK)                                   # 第 2 片突變只有 2／3
        self.assertEqual((rc, res["red"], res["red_kind"], len(res["lockdown_reasons"])), (1, True, "lockdown", 1), res["reasons"])
        self.assertEqual([(x["job"], x["level"], x["system_layer_gap"]) for x in res["lockdown"]["mutation_shards"]],
                         [("verify-mutations-1", "3/3", None), ("verify-mutations-2", "2/3", "沒有系統層")])
        self.assertEqual((res["lockdown"]["job"], res["lockdown"]["level"], res["lockdown"]["system_layer_gap"]), ("verify-partial", "3/3", None))
        self.assertEqual(res["egress"]["level"], "2/3")
        self.assertEqual(res["tests"]["failed"], [])                                     # 測試本身沒有紅
        self.assertIn("verify-mutations-2 2/3（沒有系統層）", self.summary)
        self.assertIn("這一次紅是因為%s，不是測試紅" % V.LOCKDOWN_RED, self.summary)
        rc, res = self.collect(main_lock=NO_IP_LOCK)                                     # 全套測試那一段只有 2／3
        self.assertEqual((rc, res["red_kind"]), (1, "lockdown"))
        self.assertTrue(any("全套測試那一段沒有完整的系統層封鎖" in r for r in res["reasons"]), res["reasons"])
        rc, res = self.collect(shard2_lock=dict(GREEN_LOCK, iptables={"ipv4": True, "ipv6": False}))   # 有 IPv4、沒有 IPv6：也不算
        self.assertEqual((rc, res["red_kind"]), (1, "lockdown"))
        self.assertEqual(res["lockdown"]["mutation_shards"][1]["ipv6"], False)
        rc, res = self.collect(shard2_lock=NO_IP_LOCK, shard_egress={"hosts": {"rate.bot.com.tw": {"count": 1, "via": ["python"]}}, "blocked_ips": []})
        self.assertEqual((rc, res["red_kind"], len(res["lockdown_reasons"])), (1, "other", 1))             # 兩種紅都有：不能只當成重跑就好
        self.assertNotIn("這一次紅是因為", self.summary)


class TestCollectChecksTheSeals(unittest.TestCase):
    """判定那一段把每一段的封存紀錄算進去（Codex 對 P2 的第五次審查）：少一步的紀錄、或受測的 checkout 被動過，就是紅。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-seals-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def collect(self, **kw):
        return TestEveryJobRunsUnderLockdown.collect(self, **kw)

    def test_collect_reds_when_a_step_was_not_sealed_or_the_checkout_changed(self):
        """對照組：判定那一段不讀封存紀錄 → 紅。"""
        rc, res = self.collect()
        self.assertEqual((rc, [s["job"] for s in res["seals"]]), (0, ["verify-partial", "verify-mutations-1", "verify-mutations-2"]), res.get("reasons"))
        self.assertIn(res["named_term_scan"], (V.NAMED_SCAN_NOT_RUN, V.NAMED_SCAN_RAN))   # 結果檔一定寫明具名字串有沒有掃（驗收機上沒有鹽：未跑）
        self.assertIn("具名字串掃描：", self.summary)
        self.assertEqual(sorted(res["seals"][0]["stages"]), ["compare", "run-tests"])
        self.assertIn("3 段都正常", self.summary)
        rc, res = self.collect(main_seal=None)                                           # 全套測試那一段沒有封存紀錄
        self.assertEqual(rc, 1)
        for stage in ("run-tests", "compare"):
            self.assertTrue(any("verify-partial：沒有「%s」那一步的封存紀錄" % stage in r for r in res["reasons"]), res["reasons"])
        dirty = {"stages": {"mutations": dict(GOOD_STAGE, tree_clean=False, tree_changes=[" M scripts/x.py"])}}
        rc, res = self.collect(shard2_seal=dirty)                                        # 第 2 片突變跑完之後，受測的 checkout 跟 commit 不一樣
        self.assertEqual((rc, res["red_kind"]), (1, "other"))                            # 這不是重跑就好的那一種紅
        self.assertTrue(any("verify-mutations-2：受測的 checkout 在「mutations」那一步之後跟 commit 不一樣" in r for r in res["reasons"]), res["reasons"])
        self.assertIn("有問題（verify-mutations-2）", self.summary)
        rc, res = self.collect(shard2_seal={"stages": {}})
        self.assertEqual(rc, 1)
        rc, res = self.collect(main_cmp={"tests_defined": 831})                          # 測試那一邊回報跑了 830 條，這個 commit 裡定義了 831 條
        self.assertEqual(rc, 1)
        self.assertTrue(any("有測試沒有被跑到" in r for r in res["reasons"]), res["reasons"])
        rc, res = self.collect(main_cmp={"tests_defined": 830})
        self.assertEqual(rc, 0, res.get("reasons"))


class TestLockdownStepsRetry(unittest.TestCase):
    """2026-10-06 的裁決：建立測試用的使用者與設防火牆那幾步，逾時從 120 秒放寬到 300 秒，失敗（含逾時）之後自動再試一次。
    10/6 凌晨有兩片突變因為執行機太慢、建使用者逾時，只剩 2／3。sudo 用替身，不需要真的有權限。"""

    def setUp(self):
        saved = V.sudo
        self.addCleanup(lambda: setattr(V, "sudo", saved))
        self.calls = []

    def fake(self, answers):
        """answers：依序回給每一次呼叫的 (rc, 輸出)；用完之後一律 (0, "")。"""
        left = list(answers)

        def sudo(cmd, timeout=120):
            self.calls.append((list(cmd), timeout))
            return left.pop(0) if left else (0, "")
        V.sudo = sudo

    def test_the_timeout_is_300_seconds_and_a_failed_step_is_tried_once_more(self):
        """對照組：逾時改回 120 秒、或失敗不再試 → 紅。"""
        self.assertEqual((V.LOCKDOWN_STEP_TIMEOUT, V.LOCKDOWN_TRIES), (300, 2))
        self.fake([(99, "執行失敗：TimeoutExpired"), (0, "")])
        retried = []
        self.assertEqual(V.sudo_retry(["useradd", "-m", "iwtest"], retried=retried, label="建立使用者 iwtest"), (0, ""))
        self.assertEqual([c[0] for c in self.calls], [["useradd", "-m", "iwtest"]] * 2)
        self.assertEqual([c[1] for c in self.calls], [300, 300])
        self.assertEqual(retried, ["建立使用者 iwtest"])
        self.calls[:] = []
        self.fake([(0, "")])                                                              # 第一次就成：不重試、不記
        retried = []
        self.assertEqual(V.sudo_retry(["-v"], retried=retried), (0, ""))
        self.assertEqual((len(self.calls), retried), (1, []))
        self.calls[:] = []
        self.fake([(99, "x"), (99, "y"), (0, "")])                                        # 兩次都不成：就是不成，不會試第三次
        self.assertEqual(V.sudo_retry(["useradd", "iwtest"]), (99, "y"))
        self.assertEqual(len(self.calls), 2)

    def test_a_user_that_already_exists_on_the_second_try_counts_as_made(self):
        self.fake([(99, "執行失敗：TimeoutExpired"), (9, "useradd: user 'iwtest' already exists")])
        rc, o = V.sudo_retry(["useradd", "iwtest"], good=V.user_made)
        self.assertTrue(V.user_made(rc, o))
        self.assertFalse(V.user_made(99, "執行失敗：TimeoutExpired"))
        self.assertFalse(V.user_made(1, None))

    def test_a_firewall_rule_is_retried_but_never_added_twice(self):
        rule = ["-A", "OUTPUT", "-m", "owner", "--uid-owner", "iwtest", "-j", "DROP"]
        self.fake([(99, "逾時"), (1, "不在"), (0, "")])                                    # 加：失敗 → 查：不在 → 再加：成
        retried = []
        self.assertTrue(V.add_rule("iptables", rule, retried))
        self.assertEqual([c[0][:2] for c in self.calls], [["iptables", "-A"], ["iptables", "-C"], ["iptables", "-A"]])
        self.assertEqual(self.calls[1][0], ["iptables", "-C", "OUTPUT", "-m", "owner", "--uid-owner", "iwtest", "-j", "DROP"])
        self.assertEqual(set(c[1] for c in self.calls), set([300]))
        self.assertEqual(retried, ["iptables DROP"])
        self.calls[:] = []
        self.fake([(99, "逾時"), (0, "")])                                                # 加：逾時 → 查：其實已經在了 → 不再加
        self.assertTrue(V.add_rule("ip6tables", rule))
        self.assertEqual([c[0][:2] for c in self.calls], [["ip6tables", "-A"], ["ip6tables", "-C"]])
        self.calls[:] = []
        self.fake([(1, "x"), (1, "不在"), (1, "x")])                                      # 兩次都不成
        self.assertFalse(V.add_rule("iptables", rule))
        self.assertEqual(len(self.calls), 3)
        self.calls[:] = []
        insert = ["-I", "OUTPUT", "1", "-m", "owner", "--uid-owner", "iwtest", "-o", "lo", "-j", "ACCEPT"]
        self.fake([(1, "x"), (1, "不在"), (0, "")])
        self.assertTrue(V.add_rule("iptables", insert))
        self.assertEqual(self.calls[1][0], ["iptables", "-C", "OUTPUT", "-m", "owner", "--uid-owner", "iwtest", "-o", "lo", "-j", "ACCEPT"])   # 查的時候沒有位置那個數字

    def test_the_firewall_needs_both_families(self):
        """IPv6 的規則設不成 → ipv6 是 False → 不算有系統層。對照組：只設 IPv4 → 紅。"""
        self.fake([])
        ipt = V.set_firewall("iwtest")
        self.assertEqual((ipt["ipv4"], ipt["ipv6"], ipt["log"]), (True, True, True))
        self.assertEqual(sorted(set(c[0][0] for c in self.calls)), ["ip6tables", "iptables"])
        self.assertEqual(len(self.calls), 6)                                              # 兩種位址各三條：放行 loopback、記錄、丟掉
        self.assertTrue(V.firewall_complete(ipt))

        def no_v6(cmd, timeout=120):
            return (1, "ip6tables: 不能用") if cmd[0] == "ip6tables" else (0, "")
        V.sudo = no_v6
        ipt = V.set_firewall("iwtest")
        self.assertEqual((ipt["ipv4"], ipt["ipv6"]), (True, False))
        self.assertFalse(V.firewall_complete(ipt))
        self.assertEqual(V.system_layer_gap({"layers": {"ip": "x"}, "iptables": ipt, "selftest": {"ip": "blocked"}}), "防火牆少了 IPv6 的規則")


class TestTheTestUserCannotTouchTheEvidence(unittest.TestCase):
    """Codex 對 P2 的第五次審查（P0）：原本封鎖時把整個 checkout 與結果資料夾改成所有使用者可寫，而分支上的測試就是用測試用的使用者跑的——
    新增的測試可以先把受測的程式改掉，或留一個背景程序事後覆寫 tests.json／compare.json／突變結果，讓判定對著原來的 commit 出綠燈。
    現在：checkout 與結果資料夾對那個使用者唯讀（用它自己實際找一次來證明）；它只寫得到兩個資料夾；每一個會執行分支程式碼的步驟跑完，
    由原本的使用者停掉它留下的程序、把結果收進它寫不到的地方、核對 checkout 跟 commit 一模一樣；判定時少一步的紀錄或 checkout 被動過就是紅。
    擋不住的（照實寫在文件裡）：測試在自己的程序裡說謊。那一種靠外部審查看 diff。"""

    def git(self, args, cwd):
        env = dict((k, v) for k, v in os.environ.items() if not k.startswith("GIT_"))
        env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        p = subprocess.run(["git"] + args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))

    def put(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-seal-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo, self.out = os.path.join(self.tmp, "repo"), os.path.join(self.tmp, "out")
        os.makedirs(self.repo)
        self.git(["init", "-q", "-b", "main"], self.repo)
        for k, v in (("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            self.git(["config", k, v], self.repo)
        self.put(os.path.join(self.repo, "scripts", "x.py"), "X = 1\n")
        self.put(os.path.join(self.repo, ".gitignore"), "__pycache__/\n")
        self.git(["add", "-A"], self.repo)
        self.git(["commit", "-q", "-m", "c"], self.repo)
        self.inner = os.path.join(self.out, V.INNER_DIR)
        os.makedirs(self.inner)
        saved = (V.sudo, V.run, V.is_linux, V.SEAL_MAX_BYTES)

        def restore():
            V.sudo, V.run, V.is_linux, V.SEAL_MAX_BYTES = saved
        self.addCleanup(restore)
        self.calls = []
        self.dmesg = ""
        self.find_says = {}                                                              # 假的 find：{資料夾: 它說找到的第一個寫得到的路徑}
        V.sudo = self.fake_sudo
        self.lock = {"user": "iwtest", "tree_changes_at_lockdown": []}

    def fake_sudo(self, cmd, timeout=120):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:1] == ["find"] and "-delete" in cmd:                                     # 清空測試那一邊的資料夾：替身真的清
            for name in os.listdir(cmd[1]):
                p = os.path.join(cmd[1], name)
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            return 0, ""
        if "find" in cmd:
            return 0, self.find_says.get(cmd[cmd.index("find") + 1], "")
        if "curl" in cmd:
            return 7, "curl: (7) Failed to connect"                                      # 被防火牆擋下
        if cmd[:1] == ["dmesg"]:
            return 0, self.dmesg                                                         # 核心的紀錄（防火牆擋下的封包記在這裡）
        if cmd[:1] == ["pkill"]:
            return 1, ""                                                                 # 沒有留下來的程序
        return 0, ""

    def test_a_finished_step_is_sealed_by_the_trusted_side(self):
        """對照組：不停掉留下來的程序、不核對 checkout、捷徑也照讀 → 紅。"""
        self.put(os.path.join(self.inner, "tests.json"), json.dumps(GREEN_TESTS))
        self.put(os.path.join(self.inner, "tests-verbose.txt"), "ok\n")
        rec = V.seal_stage(self.out, self.lock, self.repo, "run-tests", ["tests.json", "tests-verbose.txt", "nope.json"])
        self.assertEqual((rec["sealed"], rec["missing"], rec["tree_clean"], rec["tree_changes"], rec["leftovers_stopped"]),
                         (["tests.json", "tests-verbose.txt"], ["nope.json"], True, [], False))
        self.assertEqual(self.calls[0], ["pkill", "-KILL", "-u", "iwtest"])               # 先停掉它留下來的程序，才去讀它寫的檔
        self.assertEqual(V.read_json(os.path.join(self.out, "tests.json"), {})["ran"], GREEN_TESTS["ran"])   # 收進它寫不到的結果資料夾
        self.assertEqual(V.read_json(os.path.join(self.out, V.SEAL_FILE), {})["stages"]["run-tests"]["tree_clean"], True)
        self.assertEqual(len(rec["head"]), 40)
        self.put(os.path.join(self.repo, "scripts", "x.py"), "X = 2  # 測試把受測的程式改掉了\n")
        self.put(os.path.join(self.repo, "scripts", "new.py"), "Y = 1\n")
        self.put(os.path.join(self.repo, "scripts", "__pycache__", "x.pyc"), "忽略的檔不算")
        rec = V.seal_stage(self.out, self.lock, self.repo, "compare", [])
        self.assertIs(rec["tree_clean"], False)
        self.assertEqual(sorted(c.split()[-1] for c in rec["tree_changes"]), ["scripts/new.py", "scripts/x.py"])
        self.assertEqual(sorted(V.read_json(os.path.join(self.out, V.SEAL_FILE), {})["stages"]), ["compare", "run-tests"])   # 每一步各記一筆
        rec = V.seal_stage(self.out, {"user": "iwtest", "tree_changes_at_lockdown": [" M scripts/x.py", "?? scripts/new.py"]}, self.repo, "mutations", [])
        self.assertIs(rec["tree_clean"], True)                                           # 封鎖之前就有的差異不算在這一步頭上
        self.assertIs(V.seal_stage(self.out, self.lock, os.path.join(self.tmp, "not-a-repo"), "x", [])["tree_clean"], False)   # 查不出來＝不算乾淨
        V.SEAL_MAX_BYTES = 10
        self.put(os.path.join(self.inner, "big.json"), "x" * 50)
        self.assertEqual(V.seal_stage(self.out, self.lock, self.repo, "y", ["big.json"])["missing"], ["big.json"])            # 太大的不收
        self.assertIsNone(V.read_plain(self.inner))                                      # 資料夾不讀
        self.assertIsNone(V.read_plain(os.path.join(self.inner, "沒有這個檔")))
        link = os.path.join(self.inner, "link.json")
        try:
            os.symlink(os.path.join(self.out, V.SEAL_FILE), link)
        except (OSError, NotImplementedError, AttributeError):
            link = None                                                                  # 這台電腦不能建捷徑（Windows 沒有權限）：這一小段跳過
        if link:
            self.assertIsNone(V.read_plain(link))                                        # 捷徑不讀（它可以指到任何地方）
        fake_link = os.path.join(self.inner, "fake-link.json")                           # 不能建捷徑的電腦上也要測得到：讓系統說這個檔是捷徑
        self.put(fake_link, "{}")
        self.assertEqual(V.read_plain(fake_link), "{}")
        real_islink = os.path.islink
        os.path.islink = lambda p: str(p).endswith("fake-link.json") or real_islink(p)
        try:
            self.assertIsNone(V.read_plain(fake_link))
            self.assertEqual(V.seal_stage(self.out, self.lock, self.repo, "z", ["fake-link.json"])["missing"], ["fake-link.json"])
        finally:
            os.path.islink = real_islink
        self.assertIsNone(V.stop_leftovers({}))                                          # 沒有測試用的使用者：沒有東西可停
        self.assertIsNone(V.checkout_changes(os.path.join(self.tmp, "not-a-repo")))

    def test_judge_wants_a_clean_seal_for_every_step_of_every_job(self):
        """對照組：判定不看封存紀錄 → 紅。"""
        self.assertEqual(V.seal_problems("verify-partial", GREEN_SEAL_MAIN, ["run-tests", "compare"]), [])
        p = V.seal_problems("verify-partial", {"stages": {"run-tests": GOOD_STAGE}}, ["run-tests", "compare"])
        self.assertEqual(len(p), 1)
        self.assertIn("verify-partial：沒有「compare」那一步的封存紀錄", p[0])
        dirty = dict(GOOD_STAGE, tree_clean=False, tree_changes=[" M scripts/x.py"])
        p = V.seal_problems("verify-mutations-2", {"stages": {"mutations": dirty}}, ["mutations"])
        self.assertIn("受測的 checkout 在「mutations」那一步之後跟 commit 不一樣（ M scripts/x.py）——驗的內容不是這個 commit", p[0])
        self.assertTrue(V.seal_problems("x", {"stages": {"mutations": dict(GOOD_STAGE, tree_clean=None)}}, ["mutations"]))   # 查不出來也算
        p = V.seal_problems("x", {"stages": {"mutations": dict(GOOD_STAGE, missing=["mut-1-of-6.json"])}}, ["mutations"])
        self.assertIn("沒有交出結果檔（mut-1-of-6.json）", p[0])
        self.assertEqual(len(V.seal_problems("x", None, ["run-tests", "compare"])), 2)
        mut = dict(GREEN_MUT, shards=["1/1"])

        def j(seals):
            return V.judge(GREEN_TESTS, egress([]), GREEN_CMP, mut, GREEN_LOCK, [], mut_lockdowns=[GREEN_LOCK], seals=seals)
        self.assertEqual(j([("verify-partial", GREEN_SEAL_MAIN, ["run-tests", "compare"]), ("verify-mutations-1", GREEN_SEAL_SHARD, ["mutations"])]), [])
        self.assertEqual(len(j([("verify-partial", GREEN_SEAL_MAIN, ["run-tests", "compare"]), ("verify-mutations-1", {}, ["mutations"])])), 1)
        self.assertTrue(any("驗的內容不是這個 commit" in r for r in j([("verify-partial", {"stages": {"run-tests": dirty, "compare": GOOD_STAGE}}, ["run-tests", "compare"])])))
        self.assertEqual(j(None), [])                                                    # 舊的呼叫方式（沒給這一項）不檢查

    def test_fewer_tests_ran_than_the_commit_defines_is_red(self):
        """「這個 commit 定義了幾條測試」是原本的使用者從檔案數的（不執行分支的程式碼）；「跑了幾條」是測試那一邊回報的。回報的比較少＝有測試沒被跑到。
        對照組：不比 → 紅。"""
        def j(ran, defined):
            return V.judge(dict(GREEN_TESTS, ran=ran), egress([]), dict(GREEN_CMP, tests_defined=defined), GREEN_MUT, GREEN_LOCK, [])
        self.assertEqual(j(830, 830), [])
        self.assertEqual(j(831, 830), [])                                                # 動態產生的測試會讓跑的比數的多：可以
        r = j(829, 830)
        self.assertEqual(len(r), 1)
        self.assertIn("跑的測試（829 條）比這個 commit 裡定義的（830 條）少", r[0])
        self.assertEqual(V.judge(GREEN_TESTS, egress([]), GREEN_CMP, GREEN_MUT, GREEN_LOCK, []), [])     # 沒有靜態計數（舊的結果）：不比

    def test_the_isolation_selftest_asks_the_test_user_what_it_can_write(self):
        """對照組：不看隔離的自我測試、或找的時候沒有略過放行的兩個資料夾 → 紅。"""
        res = V.isolation_selftest(self.repo, self.out, "iwtest")
        self.assertEqual(res, {"checkout": "readonly", "results": "readonly", "scratch": "writable"})
        finds = [c for c in self.calls if "find" in c]
        self.assertEqual(finds[0], ["-u", "iwtest", "find", self.repo, "-writable", "-print", "-quit"])
        self.assertEqual(finds[1][:4], ["-u", "iwtest", "find", self.out])
        self.assertIn(os.path.join(self.out, "egress"), finds[1])                         # 放行的兩個資料夾略過不找
        self.assertIn(os.path.join(self.out, V.INNER_DIR), finds[1])
        self.assertIn("-prune", finds[1])
        self.find_says = {self.repo: os.path.join(self.repo, "scripts") + "\n"}
        self.assertEqual(V.isolation_selftest(self.repo, self.out, "iwtest")["checkout"], "WRITABLE")
        self.find_says = {self.out: os.path.join(self.out, "lockdown.json") + "\n"}
        self.assertEqual(V.isolation_selftest(self.repo, self.out, "iwtest")["results"], "WRITABLE")
        V.sudo = lambda cmd, timeout=120: (1, "find: Permission denied")                 # 找的過程出錯：也算寫得到（寧可紅）
        self.assertEqual(V.isolation_selftest(self.repo, self.out, "iwtest"), {"checkout": "WRITABLE", "results": "WRITABLE", "scratch": "NOT writable"})
        ok = GREEN_LOCK["selftest"]
        self.assertIsNone(V.system_layer_gap(GREEN_LOCK))
        self.assertEqual(V.system_layer_gap(dict(GREEN_LOCK, selftest=dict(ok, checkout="WRITABLE"))), "測試用的使用者改得到受測的 checkout")
        self.assertEqual(V.system_layer_gap(dict(GREEN_LOCK, selftest=dict(ok, results="WRITABLE"))), "測試用的使用者改得到結果檔")
        self.assertEqual(V.system_layer_gap(dict(GREEN_LOCK, selftest={"python": "blocked+logged", "ip": "blocked"})), "測試用的使用者改得到受測的 checkout")
        reasons = V.judge(GREEN_TESTS, egress([]), GREEN_CMP, GREEN_MUT, dict(GREEN_LOCK, selftest=dict(ok, results="WRITABLE")), [])
        self.assertTrue(reasons and reasons[0].startswith(V.LOCKDOWN_RED), reasons)

    def test_lockdown_makes_the_checkout_and_the_results_read_only_for_the_test_user(self):
        """封鎖那一步實際下的權限指令（sudo 用替身）：checkout 與結果資料夾都是「讀得到、改不到」，只有兩個資料夾交給那個使用者。
        對照組：改回所有使用者可寫 → 紅。"""
        V.is_linux = lambda: True
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(V.cmd_lockdown(argparse.Namespace(out=self.out, repo=self.repo)), 0)
        chmods = [c for c in self.calls if c[:1] == ["chmod"]]
        self.assertIn(["chmod", "-R", "a+rX,go-w", self.repo], chmods)
        self.assertIn(["chmod", "-R", "a+rX,go-w", self.out], chmods)
        self.assertFalse([c for c in self.calls if any("a+rwX" in str(x) or "o+w" in str(x) or "a+w" in str(x) for x in c)], "不可以有任何「所有人可寫」的權限指令")
        egress_dir, inner = os.path.join(self.out, "egress"), self.inner
        self.assertIn(["chown", "-R", "iwtest", egress_dir, inner], self.calls)           # 只有這兩個資料夾交給它
        self.assertLess(self.calls.index(["chmod", "-R", "a+rX,go-w", self.out]), self.calls.index(["chown", "-R", "iwtest", egress_dir, inner]))
        info = V.read_json(os.path.join(self.out, "lockdown.json"), {})
        self.assertEqual((info["user"], info["tree_changes_at_lockdown"]), ("iwtest", []))
        self.assertEqual((info["selftest"]["checkout"], info["selftest"]["results"], info["selftest"]["scratch"], info["selftest"]["ip"]),
                         ("readonly", "readonly", "writable", "blocked"))
        self.assertIsNone(V.system_layer_gap(info))
        self.calls[:] = []
        self.find_says = {self.repo: os.path.join(self.repo, ".git") + "\n"}             # 那個使用者說它在 checkout 裡找到寫得到的東西
        with contextlib.redirect_stdout(io.StringIO()) as said:
            V.cmd_lockdown(argparse.Namespace(out=self.out, repo=self.repo))
        info = V.read_json(os.path.join(self.out, "lockdown.json"), {})
        self.assertEqual(info["selftest"]["checkout"], "WRITABLE")
        self.assertEqual(V.system_layer_gap(info), "測試用的使用者改得到受測的 checkout")
        self.assertIn("隔離的自我測試沒過", said.getvalue())

    def test_only_the_packets_of_the_selftest_itself_are_set_aside(self):
        """Codex 對 P2 的第六次審查（P1）：封鎖時驗收機自己故意往一個固定的位址連一次，證明「擋下而且記到了」；收集時原本把所有連到那個位址的紀錄
        都當成自我測試丟掉——受測的程式如果也連同一個位址，會一起消失、驗收照樣綠。現在封鎖當下先數好自我測試留下幾筆，收集時只扣那幾筆。
        對照組：全部丟掉、或封鎖時不數 → 紅。"""
        def packets(n, proto="TCP", port="80"):
            return "\n".join(["kernel: IWEGRESS IN= OUT=eth0 SRC=10.0.0.2 DST=%s LEN=60 PROTO=%s SPT=5 DPT=%s" % (V.SELFTEST_IP, proto, port)] * n)

        def unlock():
            with contextlib.redirect_stdout(io.StringIO()):
                V.cmd_unlock(argparse.Namespace(out=self.out))
            return V.read_json(os.path.join(self.out, "egress.json"), {})
        V.is_linux = lambda: True
        self.dmesg = packets(3)                                                          # 自我測試那一次連線留下 3 筆（重送也算）
        with contextlib.redirect_stdout(io.StringIO()):
            V.cmd_lockdown(argparse.Namespace(out=self.out, repo=self.repo))
        info = V.read_json(os.path.join(self.out, "lockdown.json"), {})
        self.assertEqual((info["selftest"]["ip"], info["selftest"]["ip_packets"]), ("blocked", 3))
        res = unlock()
        self.assertEqual((res["blocked_ips"], res["selftest"]["ip_logged"]), ([], True))  # 只有自我測試那 3 筆：不列
        self.dmesg = packets(5)                                                          # 跑測試的時候，同一個位址又多了 2 筆
        self.assertEqual(unlock()["blocked_ips"], [{"ip": V.SELFTEST_IP, "proto": "TCP", "port": "80", "count": 2}])
        self.dmesg = packets(3) + "\n" + packets(1, proto="UDP") + "\n" + packets(2, port="443")
        self.assertEqual(sorted((b["proto"], b["port"], b["count"]) for b in unlock()["blocked_ips"]), [("TCP", "443", 2), ("UDP", "80", 1)])   # 同一個位址、別的埠或別的協定：照列
        self.dmesg = packets(2)                                                          # 紀錄比封鎖當下少（被擠掉了）：不會變成負的
        self.assertEqual(unlock()["blocked_ips"], [])
        e = V.judge_egress({"hosts": {}, "blocked_ips": [{"ip": V.SELFTEST_IP, "proto": "TCP", "port": "80", "count": 2}]}, ALLOWED)
        self.assertTrue(any("系統層擋下了對外連線" in r for r in V.judge(GREEN_TESTS, e, GREEN_CMP, GREEN_MUT, GREEN_LOCK, [])))             # 多出來的那 2 筆會讓判定變紅
        self.assertEqual(V.selftest_packets([{"ip": V.SELFTEST_IP, "proto": "TCP", "port": "80", "count": 3}, {"ip": V.SELFTEST_IP, "proto": "UDP", "port": "80", "count": 9},
                                             {"ip": "9.9.9.9", "proto": "TCP", "port": "80", "count": 4}]), 3)

    def test_every_step_writes_through_the_scratch_folder_and_unlock_only_opens_the_logs(self):
        """全套測試、跟 main 比、每一片突變：測試用的使用者那一邊只寫得到 inner；跑完由原本的使用者收進結果資料夾。
        解除封鎖時只把紀錄那個資料夾放寬成可讀（原本是把整個結果資料夾改成所有人可寫）。對照組：直接寫進結果資料夾 → 紅。"""
        V.write_json(os.path.join(self.out, "lockdown.json"), dict(GREEN_LOCK, user="iwtest", browser="/x/w.sh", tree_changes_at_lockdown=[]))
        ran = []

        def fake_run(cmd, cwd=None, timeout=120, env=None, inp=None):
            cmd = list(cmd)
            ran.append(cmd)
            if "run-tests-inner" in cmd:                                                 # 測試那一邊：把結果寫到它拿到的路徑
                V.write_json(cmd[cmd.index("--out") + 1], GREEN_TESTS)
                self.put(cmd[cmd.index("--log") + 1], "ok\n")
            elif "--inner" in cmd:                                                       # 跟 main 比裡面、要載入分支定義的那一塊
                V.write_json(os.path.join(self.inner, V.COMPARE_INNER), {"mutations_total": 2, "items": {"M01": "aaa", "M02": "bbb"}, "mutation_anchor_problems": []})
            elif any(str(x).endswith("run_mutations.py") for x in cmd):
                V.write_json(cmd[cmd.index("--out") + 1], {"shard": "1/2", "results": []})
            return 0, ""
        V.run = fake_run
        self.put(os.path.join(self.inner, "tests.json"), json.dumps(dict(GREEN_TESTS, ran=1)))            # 開始之前就躺在那個資料夾裡的假結果
        self.put(os.path.join(self.inner, "mut-1-of-2.json"), json.dumps({"shard": "1/2", "results": [{"id": "M01", "ok": True}]}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(V.cmd_run_tests(argparse.Namespace(out=self.out, repo=self.repo, timeout=5)), 0)
            self.assertIn(["find", self.inner, "-mindepth", "1", "-delete"], self.calls)  # 每一步開始前先把那個資料夾清空
            self.assertFalse(os.path.exists(os.path.join(self.inner, "mut-1-of-2.json")))
            cmd = ran[-1]
            self.assertEqual(cmd[cmd.index("--out") + 1], os.path.join(self.inner, "tests.json"))
            self.assertEqual(cmd[cmd.index("--log") + 1], os.path.join(self.inner, "tests-verbose.txt"))
            self.assertEqual(V.read_json(os.path.join(self.out, "tests.json"), {})["ran"], GREEN_TESTS["ran"])
            V.cmd_compare(argparse.Namespace(out=self.out, repo=self.repo, main_ref="origin/main", inner=False))
            cmp_ = V.read_json(os.path.join(self.out, "compare.json"), {})
            self.assertEqual((cmp_["mutations_total"], cmp_["mutation_anchor_problems"], cmp_["tests_defined"]), (2, [], 0))
            self.assertEqual(len(cmp_["head_sha"]), 40)                                  # git 的比對是原本的使用者這一邊做的
            V.cmd_mutations(argparse.Namespace(out=self.out, repo=self.repo, shard="1/2", main_ref="origin/main", copy_dir=self.tmp, timeout=5))
            cmd = ran[-1]
            self.assertEqual(cmd[cmd.index("--out") + 1], os.path.join(self.inner, "mut-1-of-2.json"))
            self.assertTrue(os.path.exists(os.path.join(self.out, "mut-1-of-2.json")))
            seal = V.read_json(os.path.join(self.out, V.SEAL_FILE), {})["stages"]
            self.assertEqual(sorted(seal), ["compare", "mutations", "run-tests"])
            self.assertEqual([seal[s]["sealed"] for s in ("run-tests", "compare", "mutations")],
                             [["tests.json", "tests-verbose.txt"], [V.COMPARE_INNER], ["mut-1-of-2.json"]])
            self.assertTrue(all(seal[s]["tree_clean"] for s in seal))
            self.assertEqual(len([c for c in self.calls if c[:1] == ["pkill"]]), 6)       # 每一步：開始前清空時停一次、收結果之前再停一次
            self.assertEqual(len([c for c in self.calls if c[:1] == ["find"] and "-delete" in c]), 3)
            # 測試那一邊算「跟 main 比」的那一塊：只寫 inner，不寫正式的 compare.json
            os.remove(os.path.join(self.out, "compare.json"))
            V.cmd_compare(argparse.Namespace(out=self.out, repo=self.repo, main_ref="origin/main", inner=True))
            self.assertFalse(os.path.exists(os.path.join(self.out, "compare.json")))
            self.assertIn("mutations_total", V.read_json(os.path.join(self.inner, V.COMPARE_INNER), {}))
            # 那一邊這一次什麼都沒交出來（上面那一份還躺在資料夾裡）：開始前會先清空，所以舊的那一份不會被當成這一次的結果；
            # 突變那一塊記成有問題（判定會紅），不是沿用舊的、也不是當成 0 個沒事
            self.assertTrue(os.path.exists(os.path.join(self.inner, V.COMPARE_INNER)))
            V.run = lambda cmd, cwd=None, timeout=120, env=None, inp=None: (0, "")
            V.cmd_compare(argparse.Namespace(out=self.out, repo=self.repo, main_ref="origin/main", inner=False))
            self.assertTrue(V.read_json(os.path.join(self.out, "compare.json"), {})["mutation_anchor_problems"])
            self.assertFalse(os.path.exists(os.path.join(self.inner, V.COMPARE_INNER)))
            # 沒有測試用的使用者（本機）：一樣先清空
            self.put(os.path.join(self.inner, "old.json"), "{}")
            os.makedirs(os.path.join(self.inner, "sub"))
            V.clear_inner(self.out, {})
            self.assertEqual(os.listdir(self.inner), [])
            self.calls[:] = []
            V.cmd_unlock(argparse.Namespace(out=self.out))
        self.assertEqual(self.calls[0], ["pkill", "-KILL", "-u", "iwtest"])
        self.assertIn(["chmod", "-R", "a+rX", os.path.join(self.out, "egress")], self.calls)
        self.assertFalse([c for c in self.calls if c[:1] == ["chmod"] and (self.out in c or any("w" in str(x) for x in c[2:3]))], self.calls)   # 不動整個結果資料夾、不給寫
        self.assertIn(V.SEAL_FILE, V.EXPORT_FILES)                                       # 封存紀錄跟著結果一起交給判定那一段


class TestScanBeforeUpload(unittest.TestCase):
    """2026-10-05 Codex 對 P2 的第二次審查：原本整個結果資料夾先上傳、到最後一段才掃描。現在每一段在上傳之前先掃描，
    只帶判定需要的檔；命中的檔不帶出去、只記檔名；判定那一段把每一段的掃描結果算進去。"""

    TOKEN = "gh" + "p_" + "A" * 30                                                       # 拼接：這個檔自己也在隱私掃描範圍裡
    LOCAL = "C:" + "\\Users\\" + "someone" + "\\x"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iw-export-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def collect(self, **kw):
        return TestEveryJobRunsUnderLockdown.collect(self, **kw)                         # 同一個假的「各段結果」資料夾

    def export(self, verbose, tests=None):
        out, dest = os.path.join(self.tmp, "out"), os.path.join(self.tmp, "export")
        shutil.rmtree(out, True)
        for sub in ("egress", "pyguard"):
            os.makedirs(os.path.join(out, sub))
        V.write_json(os.path.join(out, "tests.json"), tests or GREEN_TESTS)
        V.write_json(os.path.join(out, "compare.json"), GREEN_CMP)
        V.write_json(os.path.join(out, "lockdown.json"), GREEN_LOCK)
        V.write_json(os.path.join(out, "egress.json"), {"hosts": {}, "blocked_ips": []})
        V.write_json(os.path.join(out, "mut-1-of-2.json"), {"shard": "1/2", "results": []})
        for rel, text in (("tests-verbose.txt", verbose), ("egress/chrome-netlog-1.json", "{}"), ("egress/python.jsonl", ""),
                          ("pyguard/sitecustomize.py", "# x\n"), ("chrome-wrapper.sh", "#!/bin/sh\n")):
            with io.open(os.path.join(out, *rel.split("/")), "w", encoding="utf-8") as fh:
                fh.write(text)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(V.cmd_export(argparse.Namespace(out=out, export=dest)), 0)
        return dest, V.read_json(os.path.join(dest, "export.json"), {})

    def read(self, dest, name):
        with io.open(os.path.join(dest, name), encoding="utf-8") as fh:
            return fh.read()

    def verbose(self, detail):
        return ("test_a (x.T.test_a) ... ok\n" * 30 + "test_b (x.T.test_b) ... FAIL\n\n" + "=" * 70 + "\nFAIL: test_b (x.T.test_b)\n" + "-" * 70 +
                "\nTraceback (most recent call last):\nAssertionError: " + detail + "\n\n" + "-" * 70 + "\nRan 31 tests in 1.0s\n\nFAILED (failures=1)\n")

    def test_only_the_needed_files_leave_the_job(self):
        """對照組：把完整的詳細輸出也列進要上傳的檔 → 紅。"""
        dest, rep = self.export(self.verbose("1 != 2"))
        self.assertEqual(sorted(os.listdir(dest)), sorted(["tests.json", "compare.json", "lockdown.json", "egress.json", "mut-1-of-2.json", "failures.txt", "export.json"]))
        self.assertEqual((rep["privacy_hits"], rep["dropped"]), ([], []))
        failures = self.read(dest, "failures.txt")
        self.assertIn("FAIL: test_b", failures)                                          # 看得出為什麼紅
        self.assertIn("AssertionError: 1 != 2", failures)
        self.assertNotIn("test_a (x.T.test_a) ... ok", failures)                         # 通過的那幾百行不帶出去
        dest, rep = self.export("test_a (x.T.test_a) ... ok\n\n" + "-" * 70 + "\nRan 1 test in 0.1s\n\nOK\n")
        self.assertNotIn("failures.txt", os.listdir(dest))                               # 全綠：沒有失敗的段落可帶

    def test_a_file_with_private_things_stays_in_the_job_and_only_its_name_is_reported(self):
        """對照組：上傳前不掃描 → 紅。"""
        dest, rep = self.export(self.verbose("token " + self.TOKEN))
        self.assertNotIn("failures.txt", os.listdir(dest))
        self.assertEqual((sorted(rep["privacy_hits"]), rep["dropped"]), (["failures.txt", "tests-verbose.txt"], ["failures.txt"]))
        for name in os.listdir(dest):
            self.assertNotIn(self.TOKEN, self.read(dest, name), name)
        dest, rep = self.export("ok\n", tests=dict(GREEN_TESTS, skipped=[{"name": "a.b", "reason": "看這裡 " + self.LOCAL}]))
        self.assertNotIn("tests.json", os.listdir(dest))                                 # 連判定要用的檔命中也不帶出去（判定會因為缺它而紅）
        self.assertEqual(rep["privacy_hits"], ["tests.json"])
        self.assertNotIn("someone", self.read(dest, "export.json"))

    def test_nothing_leaves_the_job_when_the_scanner_cannot_be_read(self):
        """讀不到 main 上的隱私掃描器時，只剩幾條通用的樣式，專案自己的規則掃不到：什麼都不帶出去，這一步以失敗結束，判定是紅
        （2026-10-06 Codex 的審查意見：原本照樣把檔案交出去上傳）。對照組：讀不到也照樣帶出去 → 紅。"""
        saved = list(V._GUARDS)

        def restore():
            V._GUARDS[:] = saved
        self.addCleanup(restore)
        V.main_guards()
        V._GUARDS[:] = [None]                                                            # 假裝讀不到
        out, dest = os.path.join(self.tmp, "out"), os.path.join(self.tmp, "export")
        os.makedirs(out)
        V.write_json(os.path.join(out, "tests.json"), GREEN_TESTS)
        V.write_json(os.path.join(out, "mut-1-of-2.json"), {"shard": "1/2", "results": []})
        with contextlib.redirect_stdout(io.StringIO()):
            rc = V.cmd_export(argparse.Namespace(out=out, export=dest))
        self.assertEqual(rc, 1)
        self.assertEqual(os.listdir(dest), ["export.json"])
        self.assertTrue(V.read_json(os.path.join(dest, "export.json"), {})["scanner_missing"])
        V._GUARDS[:] = saved
        inp = os.path.join(self.tmp, "in")                                                 # 判定那一段：有一段說讀不到掃描器 → 紅
        rc, res = self.collect()
        self.assertEqual(rc, 0, res.get("reasons"))
        V.write_json(os.path.join(inp, "verify-mutations-1", "export.json"), {"scanner_missing": True, "privacy_hits": [], "exported": []})
        with contextlib.redirect_stdout(io.StringIO()):
            rc = V.cmd_collect(argparse.Namespace(repo=ROOT, out=tempfile.mkdtemp(prefix="out-", dir=self.tmp), inputs=inp, main_ref="origin/main", summary=None))
        self.assertEqual(rc, 1)

    def test_scan_reports_never_carry_the_matched_text(self):
        """掃描的回報會進紅的原因、結果檔、摘要與執行紀錄，所以只寫類別與處數。對照組：把命中的字帶出來 → 紅。"""
        planted = "持有 " + "10 股"

        class Guards(object):
            def privacy_hits(self, t):
                return [planted] if planted in t else []

            def profile_key_hits(self, t):
                return []

            def fxplan_key_hits(self, t):
                return []
        hits = V.privacy_scan({"x": "前面 " + planted + " 後面"}, Guards())
        self.assertEqual(len(hits), 1)
        self.assertNotIn(planted, hits[0])
        self.assertIn("1 處", hits[0])
        reasons = V.judge(GREEN_TESTS, egress([]), GREEN_CMP, GREEN_MUT, GREEN_LOCK, hits)
        self.assertFalse(any(planted in r for r in reasons))

    def test_output_is_scanned_before_it_is_printed_to_the_public_log(self):
        """受測程序（全套測試、跟 main 比、突變）的輸出要印進公開的執行紀錄之前先掃；命中就不印。main 上的隱私掃描器讀不到時也不印
        （2026-10-06 Codex 的審查意見；原本讀不到時只過通用樣式就印）。對照組：不掃就印、讀不到掃描器也照印 → 紅。"""
        self.assertEqual(V.safe_tail("普通的輸出", 4000), "普通的輸出")
        said = V.safe_tail("前面\n" + self.TOKEN + "\n後面", 4000)
        self.assertNotIn(self.TOKEN, said)
        self.assertIn("沒有印出來", said)
        self.assertEqual(V.safe_tail("0123456789", 4), "6789")
        planted = "持有 " + "1,000 股"                                                     # 專案自己的規則（main 的掃描器）才認得的字樣
        self.assertNotIn(planted, V.safe_tail("輸出：" + planted, 4000))
        saved = list(V._GUARDS)

        def restore():
            V._GUARDS[:] = saved
        self.addCleanup(restore)
        V.main_guards()
        V._GUARDS[:] = [None]                                                            # 假裝讀不到 main 上的掃描器：再乾淨的輸出也不印
        for text in ("普通的輸出", "輸出：" + planted):
            said = V.safe_tail(text, 4000)
            self.assertNotIn(text, said)
            self.assertIn("讀不到 main 上的隱私掃描器", said)

    def test_collect_reds_when_a_job_held_something_back_or_never_scanned(self):
        """對照組：判定不看各段上傳前的掃描結果 → 紅。"""
        rc, res = self.collect()
        self.assertEqual((rc, res["red"]), (0, False), res.get("reasons"))
        rc, res = self.collect(export_hits=["failures.txt"])
        self.assertEqual((rc, res["red"]), (1, True))
        self.assertIn("verify-mutations-1", res["privacy_hits"])
        self.assertTrue(any("上傳前就擋下了" in r for r in res["reasons"]), res["reasons"])
        rc, res = self.collect(shard2_export=False)
        self.assertEqual((rc, res["red"]), (1, True))
        self.assertTrue(any("沒有上傳前的掃描紀錄" in r for r in res["reasons"]), res["reasons"])


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

    def test_home_folders_on_unix_and_mac_are_caught_but_the_runner_own_paths_are_not(self):
        """Codex 對 P2 的第六次審查：路徑原本只認 Windows 的兩種寫法。驗收機的結果裡本來就有執行機自己的路徑，所以有一份排除清單——
        只准列執行機固定的帳號與這支程式建的測試用使用者。對照組：不掃這一種、或排除清單變寬 → 紅。"""
        home, users = "/ho" + "me/", "/Us" + "ers/"                                      # 拼接：測試的名字與失敗訊息會進驗收機的紀錄，不可以自己踩到
        for i, text in enumerate(("log at " + home + "alice/notes.txt", "open(" + users + "alice/x.py)", "x=" + home + "bob", "見 " + users + "someone/Desktop/a.md")):
            self.assertEqual(V.privacy_scan({"t": text}), ["t：本機的家目錄路徑"], i)
        for i, text in enumerate(('File "' + home + 'runner/work/invest-watch/invest-watch/scripts/test_x.py", line 3',
                                  "HOME=" + home + "runner", home + V.TEST_USER + "/.cache/x", "cwd " + home + "runner/work/_temp/verify-out",
                                  "C:" + users + "fake/x",                                # Windows 的假路徑由另外兩條管，這一條不重複算
                                  "https://example.invalid" + users + "alice", "scripts" + home + "x.py", "看 docs/home/page.md")):
            self.assertEqual(V.privacy_scan({"t": text}), [], i)
        self.assertEqual(V.RUNNER_HOME_PREFIXES, (home + "runner", home + "iwtest"))       # 排除清單就這兩個，不可以隨手加
        self.assertTrue(V.unix_home_hit(home + "runner2/x"))                              # 名字只是開頭一樣的不算在清單裡
        self.assertTrue(V.unix_home_hit(home + "iwtest-other/x"))

    def test_only_exact_system_mail_addresses_and_fixture_domains_pass(self):
        """同一次審查：信箱的豁免原本寫成「地址裡有 noreply 就放行」，@github.com 整個網域也放行。現在只放行確切的系統地址與保留給測試用的網域。
        對照組：有 noreply 就放行 → 紅。"""
        at = "@"
        for i, addr in enumerate(("someone+noreply" + at + "mail.example.org", "noreply" + at + "mail.example.org", "someone" + at + "github.com",
                                  "noreply.someone" + at + "mail.example.org", "someone" + at + "users.noreply.github.com.example.org")):
            self.assertEqual(V.privacy_scan({"t": "contact " + addr}), ["t：電子郵件"], i)
            self.assertFalse(V.mail_ok(addr), i)
        for addr in ("noreply@anthropic.com", "noreply@github.com", "git@github.com", "12345+someone@users.noreply.github.com", "NoReply@GitHub.com",
                     "t@example.invalid", "x@example.com"):
            self.assertEqual(V.privacy_scan({"t": "by " + addr}), [], addr)
        sys.path.insert(0, os.path.join(ROOT, ".claude", "hooks"))
        import iw_notify as N                                                             # 兩邊的系統地址清單要一樣（各寫一份，靠這一條釘住）
        self.assertEqual((V.SYSTEM_MAIL_EXACT, V.SYSTEM_MAIL_DOMAINS), (N.PUBLIC_MAIL_EXACT, N.PUBLIC_MAIL_DOMAINS))
        self.assertEqual(V.FIXTURE_MAIL_DOMAINS, ("example.com", "example.invalid"))

    def test_common_secret_formats_are_caught_in_the_results(self):
        """Codex 對 P2 的第七次審查：像密鑰的東西原本只認兩種 GitHub 權杖的開頭。清單跟公開文字那一邊的一樣（各寫一份，這一條釘住）。
        對照組：只認 GitHub 那兩種 → 紅。"""
        b = "b" * 24
        for i, s in enumerate(("gh" + "s_" + "A" * 30, "AK" + "IA" + "ABCDEFGHIJKLMNOP", "xo" + "xb-" + "1234567890-abcdefghij",
                               "ey" + "JhbGciOiJIUzI1NiJ9" + "." + "ey" + "JzdWIiOiIxMjM0In0" + "." + "abcDEF123456", "-----BEGIN " + "EC PRIVATE KEY-----",
                               "s" + "k-" + b, "AI" + "za" + "B" * 35, "r" + "k_test_" + b, "Authorization: Bea" + "rer " + "abc" * 8)):
            self.assertEqual(V.privacy_scan({"t": "log: " + s}), ["t：像權杖的字串"], i)
        for i, text in enumerate(("task-notification、risk-level", "token 的檢查", "commit a8ac92093e50992b732beecb13741334de2d10d2", "IWEGRESS IN= OUT=eth0 PROTO=TCP")):
            self.assertEqual(V.privacy_scan({"t": text}), [], i)
        sys.path.insert(0, os.path.join(ROOT, ".claude", "hooks"))
        import iw_notify as N
        self.assertEqual(V.SECRET_PATTERNS, N.SECRET_PATTERNS)

    def test_the_result_says_plainly_whether_named_terms_were_scanned(self):
        """Cowork 2026-10-06 的裁決：驗收機不放鹽、不放任何密鑰，具名字串那幾條在驗收機上是 skipped。結果檔要明白寫「未跑（無鹽）」，
        不可以讓人以為驗收機掃過；這一種由施工那台電腦上的檢查程式自己帶鹽掃。對照組：沒有鹽也說掃過了 → 紅。"""
        class Guards(object):
            def __init__(self, salt, boom=False):
                self.salt, self.boom = salt, boom

            def load_salt(self):
                if self.boom:
                    raise RuntimeError("x")
                return self.salt
        self.assertEqual(V.named_term_scan_status(Guards(None)), "未跑（無鹽）")
        self.assertEqual(V.named_term_scan_status(Guards(b"")), "未跑（無鹽）")
        self.assertEqual(V.named_term_scan_status(Guards(None, boom=True)), "未跑（無鹽）")
        self.assertEqual(V.named_term_scan_status(None), "未跑（無鹽）")
        self.assertEqual(V.named_term_scan_status(Guards(b"salt")), "有跑（有鹽）")
        res = {"red": False, "commit": "a" * 40, "ref": "feat/stopX", "tests": GREEN_TESTS, "egress": egress([]), "compare": GREEN_CMP, "mutations": GREEN_MUT, "reasons": []}
        md = V.summary_md(dict(res, named_term_scan=V.NAMED_SCAN_NOT_RUN))
        self.assertIn("具名字串掃描：未跑（無鹽）", md)
        self.assertIn("由施工那台電腦上的檢查程式自己帶鹽掃", md)
        self.assertNotIn("檢查程式自己帶鹽掃", V.summary_md(dict(res, named_term_scan=V.NAMED_SCAN_RAN)))

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

    def test_a_tag_or_branch_named_like_the_remote_main_does_not_replace_the_main_copy(self):
        """git 解析短名 origin/main 的順序是標籤 → 本機分支 → 遠端的記號；執行機會把標籤全部抓下來。有人推一個同名的標籤、指到自己的 commit，
        寫短名的話「main 上的驗收程式」就變成它的（2026-10-06 審查代理指出，Cowork 裁決在 P2 修）。驗收程式把 --main-ref 一律換成全名：
        這裡真的建同名的標籤與分支，取到的還是 main 的那一份，「驗收機本身有沒有改」也照樣看得出來。對照組：不換成全名 → 紅。"""
        self.git(["checkout", "-q", "main"], self.repo)
        self.write(self.repo, "scripts/verify_ci.py", "# main verifier\n")
        self.git(["add", "-A"], self.repo)
        self.git(["commit", "-q", "-m", "main2"], self.repo)
        self.git(["update-ref", "refs/remotes/origin/main", "HEAD"], self.repo)
        self.git(["checkout", "-q", "feat/stopX"], self.repo)
        self.write(self.repo, "scripts/verify_ci.py", "# tampered\n")
        self.git(["add", "-A"], self.repo)
        self.git(["commit", "-q", "-m", "tamper"], self.repo)
        for n, kind in enumerate(("tag", "branch")):
            self.git([kind, "origin/main", "HEAD"], self.repo)                              # 同名的標籤／本機分支，指到分支的頂端
            self.assertEqual(V.git_show(self.repo, "origin/main", "scripts/verify_ci.py"), "# tampered\n", kind)      # 短名真的被蓋過了
            self.assertEqual(V.git_show(self.repo, V.MAIN_REF, "scripts/verify_ci.py"), "# main verifier\n", kind)    # 全名沒有
            for ref in ("origin/main", V.MAIN_REF):
                dest = os.path.join(self.tmp, "verifier-%d-%s" % (n, ref.replace("/", "_")))
                self.assertEqual(V.main(["extract-verifier", "--repo", self.repo, "--main-ref", ref, "--dest", dest]), 0)
                self.assertEqual(io.open(os.path.join(dest, "verify_ci.py"), encoding="utf-8").read(), "# main verifier\n", (kind, ref))
                src = json.load(io.open(os.path.join(dest, "verifier-source.json"), encoding="utf-8"))
                self.assertEqual((src["source"], src["main_ref"]), ("main", V.MAIN_REF), (kind, ref))
            out = os.path.join(self.tmp, "out-%d" % n)
            self.assertEqual(V.main(["compare", "--repo", self.repo, "--main-ref", "origin/main", "--out", out]), 0)
            cmp_ = json.load(io.open(os.path.join(out, "compare.json"), encoding="utf-8"))
            self.assertEqual(cmp_["main_ref"], V.MAIN_REF, kind)
            self.assertTrue(cmp_["verifier_changed"], kind)                                 # 分支改了驗收程式：照樣看得出來
            self.assertFalse(cmp_["verifier_first_time"], kind)
            self.git([kind, "-d" if kind == "tag" else "-D", "origin/main"], self.repo)

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

    def test_result_files_do_not_carry_absolute_paths(self):
        """突變的結果檔會上傳：定義檔只寫相對於倉庫的路徑，倉庫以外的只留檔名。對照組：寫絕對路徑 → 紅。"""
        tmp = tempfile.mkdtemp(prefix="iw-mut-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, "scripts", "mutations"))
        defs = os.path.join(repo, "scripts", "mutations", "autopilot_mutations.py")
        self.assertEqual(RM.public_path(defs, repo), "scripts/mutations/autopilot_mutations.py")
        self.assertEqual(RM.public_path(os.path.join(tmp, "elsewhere", "defs.py"), repo), "defs.py")
        out = os.path.join(tmp, "r.json")
        RM._dump(argparse.Namespace(defs=defs, repo=repo, shard="1/1", out=out, md=None), [], [], {})
        with io.open(out, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(json.loads(text)["defs"], "scripts/mutations/autopilot_mutations.py")
        self.assertNotIn(os.path.basename(tmp), text)

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

    def test_the_bootstrap_does_not_run_a_branch_file_that_shadows_the_standard_library(self):
        """Codex 對 P2 的第九次審查（P0）：流程檔「取 main 上的驗收程式」那一步原本直接執行 checkout 裡的 scripts/verify_ci.py。
        Python 會把腳本所在的資料夾排在匯入路徑最前面，所以分支只要在 scripts/ 放一個跟標準函式庫同名的檔（例如 argparse.py），
        它就會在封鎖之前、用原本的使用者執行，可以冒充那一步的輸出（dir=…），把後面每一步換成它指定的驗收程式；
        本機比對 blob 的那四個檔裡沒有它。現在啟動用的那一份放在 checkout 外面、用 -I 執行。這裡實際做一次。
        對照組：照原本的跑法，那個同名的檔真的會先被執行。"""
        tmp = tempfile.mkdtemp(prefix="iw-boot-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo, dest, marker = os.path.join(tmp, "repo"), os.path.join(tmp, "verifier"), os.path.join(tmp, "branch-code-ran")
        os.makedirs(os.path.join(repo, "scripts"))
        shutil.copyfile(os.path.join(HERE, "verify_ci.py"), os.path.join(repo, "scripts", "verify_ci.py"))
        with io.open(os.path.join(repo, "scripts", "argparse.py"), "w", encoding="utf-8", newline="\n") as fh:       # 分支放的同名檔：留記號、冒充輸出、成功結束
            fh.write("import os\nopen(os.environ['IW_SHADOW_MARK'], 'w').close()\nprint('dir=/forged')\nraise SystemExit(0)\n")
        env = dict(os.environ, IW_SHADOW_MARK=marker, PYTHONIOENCODING="utf-8")
        args = ["extract-verifier", "--repo", repo, "--main-ref", "origin/main", "--dest", dest]
        old = subprocess.run([sys.executable, os.path.join("scripts", "verify_ci.py")] + args, cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertTrue(os.path.exists(marker))                                    # 原本的跑法：分支的檔先被執行，輸出是它冒充的
        self.assertIn("dir=/forged", old.stdout.decode("utf-8", "replace"))
        os.remove(marker)
        boot = os.path.join(tmp, "bootstrap", "verify_ci.py")                      # 流程檔現在的做法：啟動檔放在 checkout 外面，用 -I 執行
        os.makedirs(os.path.dirname(boot))
        shutil.copyfile(os.path.join(HERE, "verify_ci.py"), boot)
        new = subprocess.run([sys.executable, "-I", boot] + args, cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        said = new.stdout.decode("utf-8", "replace")
        self.assertEqual(new.returncode, 0, said[-400:])
        self.assertFalse(os.path.exists(marker))                                   # 分支的同名檔沒有被執行
        self.assertNotIn("forged", said)
        self.assertIn("dir=%s" % os.path.abspath(dest), said)
        inside = subprocess.run([sys.executable, "-I", os.path.join("scripts", "verify_ci.py")] + args, cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertFalse(os.path.exists(marker))                                   # 建立期 main 上還沒有驗收程式、只能用分支那一份時，-I 一樣擋得住同名的檔
        self.assertNotIn("forged", inside.stdout.decode("utf-8", "replace"))

    def test_a_leftover_bytecode_cache_cannot_hide_a_mutation(self):
        """2026-10-06 驗收機上的 R144：改壞前後一樣長的突變，又剛好跟上一次寫檔落在同一秒，Python 會把上一次留下的快取檔（__pycache__）
        當成還能用——跑的其實是沒改壞的程式，測試照樣綠，突變看起來「沒有紅」。執行器跑測試時一律從原始碼重新編譯，不讀也不寫原始檔旁邊的快取。
        這裡直接做出那個情況：先替「沒改壞的」留一份快取，再把檔換成一樣長的改壞版、修改時間設成一樣。
        對照組：照一般的方式跑會被騙（綠）；拿掉執行器的那兩個設定 → 這一條會紅。"""
        import py_compile
        tmp = tempfile.mkdtemp(prefix="iw-mut-")
        self.addCleanup(shutil.rmtree, tmp, True)
        copy = os.path.join(tmp, "copy")
        os.makedirs(copy)
        src, when = os.path.join(copy, "a.py"), 1700000000
        with io.open(src, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("X = 1\n")
        with io.open(os.path.join(copy, "t.py"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("import unittest\nimport a\n\n\nclass T(unittest.TestCase):\n    def test_real(self):\n        self.assertEqual(a.X, 1)\n")
        os.utime(src, (when, when))
        py_compile.compile(src, doraise=True)                                    # 沒改壞的那一版留下快取檔
        with io.open(src, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("X = 2\n")                                                  # 改壞：一樣長
        os.utime(src, (when, when))                                              # 修改時間也一樣（在執行機上是「同一秒」）
        plain = dict(os.environ)
        plain.pop("PYTHONPYCACHEPREFIX", None)
        fooled = subprocess.run([sys.executable, "-m", "unittest", "t.py"], cwd=copy, env=plain, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(fooled.returncode, 0, fooled.stdout.decode("utf-8", "replace")[-400:])   # 一般的跑法：讀到舊快取，以為沒改壞
        rc, ran, fails, _tail, _out = RM.run_tests(copy, [sys.executable], ["t.py"])
        self.assertEqual((ran, fails), (1, ["test_real"]))                       # 執行器：從原始碼重新編譯，改壞的那一版真的被跑到
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(copy, ".no-pycache")))      # 也不留新的快取

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
