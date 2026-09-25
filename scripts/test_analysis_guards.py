#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_analysis_guards.py — 分析系列的兩道自動守門：擋字串、隱私掃描

一、擋字串（誠實鐵則）：分析系列的程式、頁面、文件、輸出，永遠不出現投資判斷用語
   （這一份測試裡那五個詞是拆開拼起來的，不然掃描會掃到自己）。唯一例外是頁尾那句固定聲明，
   以及台銀牌價的欄位名稱（本行買入／本行賣出／即期買入／即期賣出／現金買入／現金賣出／掛牌賣出）——那是價格的名字，不是判斷。
二、隱私掃描（隱私鐵則）：要進公開倉庫的分析系列檔案裡，不能有個人持倉數字、具名基金、收入來源、房產與貸款：
   (a) 通用樣式：這些字眼本身，以及「持有 N 股／張」「成本／部位／曝險／權重 N%」這類寫法；
   (b) data/analysis 的 JSON 禁止用會裝個人資料的鍵名（quantity／shares／cost／amount／weight…）；
   (c) 具名字串用【加鹽的 HMAC】比對：鹽放在倉庫外（預設 ../iw-private/scan-salt.txt，可用環境變數 IW_SCAN_SALT_FILE 指定），
       倉庫裡只有 scripts/sensitive_terms_hmac.json（長度＋HMAC，看不出原文，沒有鹽也算不回來）。
       找不到鹽（例如 GitHub 的 runner）就只跳過 (c)，(a)(b) 永遠執行。
   重新產生 HMAC 清單（本機、鹽與清單都在時）：python scripts/test_analysis_guards.py --regen-digests

對照組：fixture 塞一個假持倉數字 → 掃描必須紅；塞一個判斷用語 → 擋字串必須紅（見 docs/CHANGELOG.md 分析系列 A1-1）。
"""
import glob
import hashlib
import hmac
import io
import json
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIGEST_FILE = os.path.join(HERE, "sensitive_terms_hmac.json")
SALT_FILE = os.environ.get("IW_SCAN_SALT_FILE") or os.path.join(ROOT, "..", "iw-private", "scan-salt.txt")
TERMS_FILE = os.environ.get("IW_SCAN_TERMS_FILE") or os.path.join(ROOT, "..", "iw-private", "sensitive-terms.txt")

# 分析系列要進公開倉庫的檔案。核心檔一定要存在（不存在＝測試紅）；data 底下的看有什麼就掃什麼。
CORE_FILES = [
    "scripts/analyze.py", "scripts/net_policy.py", "scripts/test_analyze.py", "scripts/test_analysis_guards.py",
    "scripts/test_analysis_page.html", "scripts/test_analysis_page_js.py",
    "scripts/test_card_analysis.html", "scripts/test_card_analysis_js.py", "scripts/test_indicators.html",
    "analysis.html", "analysis-debug.html", "js/analysis.js", "js/card-analysis.js", "js/concentration.js", "docs/ANALYSIS.md",
    "docs/adhoc-workflow.example.yml",
]
# 集中度設定檔的鍵名：只准出現在 js/concentration.js（讀設定檔的那一支）。公開輸出、頁面、其他 JS 一律零命中。
PROFILE_KEYS = ("wei" + "ghts", "salaryProxy" + "AssetId", "as" + "Of")
PROFILE_KEY_HOME = "js/concentration.js"
DATA_GLOBS = ["data/analysis/*.json", "data/analysis/**/*.json", "data/history-long/*.json"]

# 判斷用語（拆開拼，免得掃到這一行）
JUDGEMENT_WORDS = ["買" + "進", "賣" + "出", "建" + "議", "總" + "分", "分" + "數"]
EXEMPT_PHRASES = ["不構成投資" + "建" + "議", "本行" + "賣" + "出", "本行買入", "即期" + "賣" + "出", "即期買入",
                  "現金" + "賣" + "出", "現金買入", "掛牌" + "賣" + "出"]

# 隱私：通用樣式（字眼本身就不該出現在公開的分析檔裡）
PRIVATE_WORDS = ["薪" + "資", "薪" + "水", "房" + "貸", "不動" + "產", "房地" + "產"]
PRIVATE_PATTERNS = [
    r"持有\s*[\d,\.]+\s*(股|張|單位|盎司|公克|克|枚|顆)",
    r"(成本|持倉|部位|曝險|權重|比重|市值|損益)\s*[:：]?\s*[\d,\.]+\s*(%|％|元|萬|股|張)",
    r"[\d,\.]+\s*(萬元|萬台幣|萬美元)",
]
# 隱私：data/analysis 的 JSON 不准有這些鍵（會裝個人資料的名字）
FORBIDDEN_JSON_KEYS = {"quantity", "shares", "holding", "holdings", "cost", "costbasis", "amount", "weight", "wei" + "ghts",
                       "portfolio", "salary", "mortgage", "position", "positions", "exposure",
                       "salaryproxy" + "assetid", "as" + "of"}


def repo_files():
    out = []
    for rel in CORE_FILES:
        out.append(rel)
    for pat in DATA_GLOBS:
        for p in glob.glob(os.path.join(ROOT, pat), recursive=True):
            out.append(os.path.relpath(p, ROOT).replace("\\", "/"))
    return sorted(set(out))


def read_text(rel):
    with io.open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ---------------------------------------------------------------- 掃描器本體（純函式，測試也拿假文字餵它）

def judgement_hits(text):
    t = text
    for e in EXEMPT_PHRASES:
        t = t.replace(e, "")
    return [w for w in JUDGEMENT_WORDS if w in t]


def profile_key_hits(text):
    """集中度設定檔的鍵名只准在 PROFILE_KEY_HOME 出現；其他地方出現就是漏了。"""
    return [k for k in PROFILE_KEYS if k in text]


def privacy_hits(text):
    hits = [w for w in PRIVATE_WORDS if w in text]
    for pat in PRIVATE_PATTERNS:
        m = re.search(pat, text)
        if m:
            hits.append(m.group(0))
    return hits


def json_key_hits(obj, path=""):
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_JSON_KEYS:
                hits.append("%s.%s" % (path, k))
            hits += json_key_hits(v, "%s.%s" % (path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            hits += json_key_hits(v, "%s[%d]" % (path, i))
    return hits


def load_salt():
    if not os.path.exists(SALT_FILE):
        return None
    with io.open(SALT_FILE, encoding="utf-8") as fh:
        s = fh.read().strip()
    return s.encode("utf-8") if s else None


def load_digests():
    if not os.path.exists(DIGEST_FILE):
        return []
    with io.open(DIGEST_FILE, encoding="utf-8") as fh:
        return json.load(fh).get("entries") or []


def hmac_hex(salt, term):
    return hmac.new(salt, term.encode("utf-8"), hashlib.sha256).hexdigest()


def text_for_named_terms(rel):
    """資料檔（data/ 底下的 JSON）只掃字串值——數字、日期、時間戳不可能是具名字串，
    而週線長歷史一檔十萬個字元，逐位置算 HMAC 會讓這條測試跑上一分多鐘。其他檔整份掃。"""
    text = read_text(rel)
    if rel.startswith("data/") and rel.endswith(".json"):
        return strings_only(text)
    return text


def strings_only(text):
    """把 JSON 文字裡的鍵名與字串值挑出來（純數字、日期、時間戳丟掉）；不是 JSON 就原樣回。"""
    try:
        obj = json.loads(text)
    except ValueError:
        return text
    out, seen = [], set()

    def keep(s):
        if s not in seen:                        # 去重：長歷史每一點都有同樣的三個鍵名，掃一次就夠
            seen.add(s)
            out.append(s)

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                keep(str(k))
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and not re.match(r"^[0-9T:+.\-]*$", o):
            keep(o)
    walk(obj)
    return "\n".join(out)


def has_cjk(s):
    return any(chr(0x4E00) <= ch <= chr(0x9FFF) for ch in s)


def named_term_hits(text, salt, entries):
    """對每一個長度 L，把文字的每一個長度 L 的片段算 HMAC 比對。
    提速（2026-09-25）：清單裡某個長度的字串都含中文字時（digest 清單的 cjk 旗標），只對「蓋到中文字」的片段算——
    程式碼幾十萬個純 ASCII 位置直接跳過，從兩分多鐘變幾秒；舊格式沒有旗標就照舊整段掃。"""
    if not salt or not entries:
        return []
    want = {}
    for e in entries:
        L = int(e["len"])
        d = want.setdefault(L, [set(), True])
        d[0].add(e["hmac"])
        d[1] = d[1] and bool(e.get("cjk", False))
    hits = []
    compact = re.sub(r"\s+", "", text)          # 去空白再比，「第一金 AI」與「第一金AI」都算同一個
    cjk_pos = [i for i, ch in enumerate(compact) if chr(0x4E00) <= ch <= chr(0x9FFF)]
    for L, (digests, need_cjk) in want.items():
        if L <= 0 or L > len(compact):
            continue
        if need_cjk:
            starts = set()
            for i in cjk_pos:
                for st in range(max(0, i - L + 1), min(i, len(compact) - L) + 1):
                    starts.add(st)
            starts = sorted(starts)
        else:
            starts = range(len(compact) - L + 1)
        for i in starts:
            frag = compact[i:i + L]
            if hmac_hex(salt, frag) in digests:
                hits.append("具名字串（長度 %d，位置 %d）" % (L, i))
                break
    return hits


def regen_digests():
    """本機用：從倉庫外的字串清單重新算 HMAC 清單（只存長度與 HMAC，原文不進倉庫）。"""
    salt = load_salt()
    if not salt or not os.path.exists(TERMS_FILE):
        print("找不到鹽或字串清單（%s／%s），沒有重算。" % (SALT_FILE, TERMS_FILE))
        return 1
    terms = []
    with io.open(TERMS_FILE, encoding="utf-8") as fh:
        for line in fh:
            t = re.sub(r"\s+", "", line.split("#", 1)[0])
            if t:
                terms.append(t)
    entries = sorted({(len(t), hmac_hex(salt, t), has_cjk(t)) for t in terms})
    with io.open(DIGEST_FILE, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"algorithm": "HMAC-SHA256(salt, term)；salt 在倉庫外，term 先去空白",
                   "note": "只存長度、HMAC 與「有沒有中文字」的旗標；沒有鹽算不回原文，有鹽也只能驗證「是不是那幾個字串」",
                   "entries": [{"len": L, "hmac": h, "cjk": c} for L, h, c in entries]}, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print("寫入 %d 筆到 %s" % (len(entries), os.path.relpath(DIGEST_FILE, ROOT)))
    return 0


# ---------------------------------------------------------------- 測試

class TestScannerItself(unittest.TestCase):
    """掃描器要抓得到東西：假文字裡塞一個假持倉數字／判斷用語，必須紅。對照組就是把上面的樣式改壞。"""

    def test_judgement_words_are_caught_but_the_footer_sentence_is_not(self):
        self.assertEqual(judgement_hits("以上為量化整理，未經回測驗證，不構成投資" + "建" + "議。"), [])
        self.assertEqual(judgement_hits("黃金存摺 本行" + "賣" + "出 4,415"), [])
        self.assertTrue(judgement_hits("這檔可以" + "買" + "進"))
        self.assertTrue(judgement_hits("綜合" + "分" + "數 87"))
        self.assertTrue(judgement_hits("我" + "建" + "議加碼"))

    def test_fake_holding_numbers_are_caught(self):
        self.assertTrue(privacy_hits("持有 " + "1,000 股"))
        self.assertTrue(privacy_hits("成本：" + "123,456 元"))
        self.assertTrue(privacy_hits("曝險 " + "48%"))
        self.assertTrue(privacy_hits("大約 250" + " 萬元"))
        self.assertTrue(privacy_hits("每月" + "房" + "貸"))
        self.assertEqual(privacy_hits("相關係數 0.62；年化波動 12.3%；權重的定義見文件"), [])

    def test_profile_key_scanner_catches_a_planted_key(self):
        self.assertEqual(profile_key_hits("x " + "wei" + "ghts" + " y"), ["wei" + "ghts"])
        self.assertEqual(profile_key_hits("nothing here"), [])

    def test_named_term_scan_of_data_files_keeps_strings_but_drops_numbers(self):
        t = strings_only(json.dumps({"note": "原始出處：某某", "points": [{"d": "2026-09-14", "c": 123.4, "t": "2026-09-14T15:30:00+08:00"}]},
                                    ensure_ascii=False))
        self.assertIn("原始出處：某某", t)
        self.assertIn("points", t)                                                                # 鍵名也掃
        self.assertNotIn("2026-09-14", t)
        self.assertNotIn("123.4", t)
        self.assertEqual(strings_only("not json {"), "not json {")

    def test_forbidden_json_keys_are_caught(self):
        self.assertEqual(json_key_hits({"gspc": {"volatility": {"pct": 12.3}}}), [])
        self.assertTrue(json_key_hits({"assets": [{"id": "x", "shares": 1000}]}))
        self.assertTrue(json_key_hits({"profile": {"wei" + "ghts": {"stock": 30}}}))

    def test_named_terms_via_hmac(self):
        salt = b"test-salt"
        entries = [{"len": 5, "hmac": hmac_hex(salt, "假基金五字")}]
        self.assertTrue(named_term_hits("……裡面提到 假基金 五字 這個東西", salt, entries))
        self.assertEqual(named_term_hits("完全無關的一段話", salt, entries), [])
        self.assertEqual(named_term_hits("有字串但沒有鹽 假基金五字", None, entries), [])
        cjk = [{"len": 5, "hmac": hmac_hex(salt, "假基金五字"), "cjk": True}]                 # 提速路徑：只掃蓋到中文字的片段
        self.assertTrue(named_term_hits("x = 1  # 假基金五字 在註解裡", salt, cjk))
        self.assertTrue(named_term_hits("假基金五字", salt, cjk))
        self.assertEqual(named_term_hits("pure ascii code without any cjk at all " * 50, salt, cjk), [])
        self.assertEqual(named_term_hits("有中文但不是那個字串", salt, cjk), [])

    def test_local_terms_are_caught_when_salt_is_present(self):
        """本機才會跑的正向對照：倉庫外清單裡的第一個字串，掃描要抓得到；runner 上沒有鹽就跳過這一條。"""
        salt = load_salt()
        if not salt or not os.path.exists(TERMS_FILE):
            self.skipTest("沒有鹽或字串清單（runner 上就是這樣）")
        with io.open(TERMS_FILE, encoding="utf-8") as fh:
            terms = [re.sub(r"\s+", "", l.split("#", 1)[0]) for l in fh if l.strip() and not l.startswith("#")]
        if not terms:
            self.skipTest("字串清單是空的")
        entries = load_digests()
        self.assertTrue(entries, "scripts/sensitive_terms_hmac.json 是空的：請跑 --regen-digests")
        self.assertTrue(named_term_hits("這段文字裡有 " + terms[0] + " 這幾個字", salt, entries))


class TestPublicFilesAreClean(unittest.TestCase):
    def setUp(self):
        self.files = repo_files()
        for rel in CORE_FILES:
            self.assertTrue(os.path.exists(os.path.join(ROOT, rel)), "核心檔不存在：%s" % rel)

    def test_no_judgement_words_in_analysis_files(self):
        bad = {}
        for rel in self.files:
            hits = judgement_hits(read_text(rel))
            if hits:
                bad[rel] = hits
        self.assertEqual(bad, {}, "分析系列的檔案裡出現判斷用語：%s" % bad)

    def test_no_private_words_or_holding_numbers(self):
        bad = {}
        for rel in self.files:
            hits = privacy_hits(read_text(rel))
            if hits:
                bad[rel] = hits
        self.assertEqual(bad, {}, "分析系列的檔案裡出現個人資料樣式：%s" % bad)

    def test_no_forbidden_keys_in_analysis_json(self):
        bad = {}
        for rel in self.files:
            if not rel.endswith(".json") or not rel.startswith("data/"):
                continue
            try:
                obj = json.loads(read_text(rel))
            except ValueError:
                bad[rel] = ["不是合法 JSON"]
                continue
            hits = json_key_hits(obj)
            if hits:
                bad[rel] = hits
        self.assertEqual(bad, {}, "data/analysis 裡有會裝個人資料的鍵名：%s" % bad)

    def test_no_named_terms(self):
        salt, entries = load_salt(), load_digests()
        if not salt:
            self.skipTest("沒有鹽（runner 上就是這樣）；通用樣式與鍵名檢查照樣跑了")
        self.assertTrue(entries, "有鹽卻沒有 HMAC 清單：請跑 --regen-digests")
        # 快取（放在鹽旁邊、倉庫外）：同一份內容、同一份清單、同一把鹽掃過就不重掃；改過的檔才重掃
        cache_file = os.path.join(os.path.dirname(SALT_FILE), "scan-cache.json")
        try:
            with io.open(cache_file, encoding="utf-8") as fh:
                cache = json.load(fh) or {}
        except Exception:                                 # noqa: B902
            cache = {}
        stamp = hashlib.sha256(salt + json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        bad, changed = {}, False
        for rel in self.files:
            text = text_for_named_terms(rel)
            key = rel + "@" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] + "@" + stamp
            if cache.get(key) == "clean":
                continue
            hits = named_term_hits(text, salt, entries)
            if hits:
                bad[rel] = hits
            else:
                cache[key] = "clean"
                changed = True
        if changed:
            try:
                keep = dict(list(cache.items())[-400:])
                with io.open(cache_file, "w", encoding="utf-8") as fh:
                    json.dump(keep, fh, indent=0)
            except Exception:                             # noqa: B902
                pass
        self.assertEqual(bad, {}, "分析系列的檔案裡出現具名字串：%s" % bad)

    def test_digest_file_has_no_plaintext(self):
        text = read_text("scripts/sensitive_terms_hmac.json")
        obj = json.loads(text)
        for e in obj.get("entries") or []:
            self.assertEqual(set(e.keys()), {"len", "hmac", "cjk"})                        # cjk 只是「有沒有中文字」的旗標
            self.assertIsInstance(e["cjk"], bool)
            self.assertRegex(e["hmac"], r"^[0-9a-f]{64}$")
        self.assertNotIn("基金", text)


class TestProducedOutputsAreClean(unittest.TestCase):
    """不只掃倉庫裡現成的檔案：把 analyze.py 離線跑一次到暫存目錄，產出的每一個 JSON 也掃（禁用鍵名、判斷用語、設定檔鍵名）。
    倉庫裡的 data/analysis 是雲端產的，程式改壞了要等隔天才看得到；這一條在測試時就看得到。"""

    def test_offline_run_outputs_have_no_forbidden_keys_or_words(self):
        import test_analyze as T
        w = T.World(patch=False)
        try:
            T.fill_world(w)
            with T.redirect_stdout(io.StringIO()):
                _status, code = T.A.run("review", offline=True, now=T.NOW, paths=w.paths)
            self.assertEqual(code, 0)
            bad = {}
            files = glob.glob(os.path.join(w.data, "analysis", "**", "*.json"), recursive=True)
            self.assertGreaterEqual(len(files), 4)                                            # status／risk／decompose／cost
            for p in files:
                text = io.open(p, encoding="utf-8").read()
                hits = judgement_hits(text) + json_key_hits(json.loads(text)) + profile_key_hits(text)
                if hits:
                    bad[os.path.relpath(p, w.data).replace(os.sep, "/")] = hits
            self.assertEqual(bad, {}, "離線產出的分析檔裡有不該有的東西：%s" % bad)
        finally:
            w.close()


class TestAdhocStaysOutOfThePublicRepo(unittest.TestCase):
    """A1-3：試算只在私人倉庫的 Actions 跑；公開倉庫的 workflow 不得出現 --adhoc；fixture 代號一律 FAKE 開頭；
    離線跑一次 adhoc 到暫存目錄，產出的檔也掃。"""

    def test_public_workflows_never_run_adhoc(self):
        files = glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))
        self.assertTrue(files)
        for p in files:
            self.assertNotIn("--adhoc", read_text(os.path.relpath(p, ROOT).replace(os.sep, "/")), p)

    def test_fixture_symbols_are_obviously_fake(self):
        for rel in ("scripts/test_analysis_page.html", "scripts/test_analyze.py"):
            for m in re.findall(r"adhoc/([A-Za-z0-9.^=\-]+)/", read_text(rel)):
                self.assertTrue(m.startswith("FAKE"), "%s 裡的試算 fixture 代號不是 FAKE 開頭：%s" % (rel, m))

    def test_offline_adhoc_outputs_are_clean(self):
        import tempfile
        import shutil
        import test_analyze as T
        w = T.World(patch=False)
        out = tempfile.mkdtemp(prefix="iw-adhoc-guard-")
        try:
            T.fill_world(w)
            f = T.FakeFetcher(body=T.adhoc_body(currency="GBp"))
            with T.redirect_stdout(io.StringIO()):
                _status, code = T.A.adhoc_run("FAKE.L", "index_etf", "0.07", out, now=T.NOW, fetcher=f, paths=w.paths)
            self.assertEqual(code, 0)
            bad = {}
            files = glob.glob(os.path.join(out, "**", "*.json"), recursive=True)
            self.assertGreaterEqual(len(files), 3)                                            # 結果、index、status
            for p in files:
                text = io.open(p, encoding="utf-8").read()
                hits = judgement_hits(text) + json_key_hits(json.loads(text)) + profile_key_hits(text)
                if hits:
                    bad[os.path.relpath(p, out).replace(os.sep, "/")] = hits
            self.assertEqual(bad, {}, "試算產出的檔裡有不該有的東西：%s" % bad)
        finally:
            T.A.WRITE_ROOTS = None
            w.close()
            shutil.rmtree(out, ignore_errors=True)


class TestProfileKeyNamesStayInOnePlace(unittest.TestCase):
    """集中度設定檔的鍵名只准在 js/concentration.js 出現；data/analysis、data/history-long、頁面靜態內容、其他 JS 零命中。
    （渲染出來的 DOM 由 test_analysis_debug_js.py 另外掃。）對照組：把鍵名當表單 id 或印進 cost.json → 紅。"""

    def test_keys_absent_from_public_outputs_and_pages(self):
        bad = {}
        for rel in repo_files():
            if rel == PROFILE_KEY_HOME:
                continue
            text = read_text(rel)
            hits = profile_key_hits(text)
            if hits:
                bad[rel] = hits
        self.assertEqual(bad, {}, "設定檔鍵名出現在不該出現的地方：%s" % bad)

    def test_the_home_file_really_is_the_one_that_uses_them(self):
        text = read_text(PROFILE_KEY_HOME)
        for k in PROFILE_KEYS:
            self.assertIn(k, text)


# docs/ 底下每個 .md 的大小上限（位元組）。2026-09-25 A1-3 的文件腳本把 ANALYSIS.md 塞成 24 MB、守門沒擋下（它只掃字），所以加這一條。
# 上限取當時實際大小的 5 倍左右（ANALYSIS.md 24 KB → 120 KB；CHANGELOG.md 101 KB → 512 KB；scheduler-setup.md 6 KB → 32 KB；
# 其他 .md 預設 128 KB）：正常改一次文件不會長 5 倍，接近上限時要有意識地調高這裡的數字，而不是靜靜地長過去。
DOC_SIZE_LIMITS = {"docs/ANALYSIS.md": 120 * 1024, "docs/CHANGELOG.md": 512 * 1024, "docs/scheduler-setup.md": 32 * 1024}
DOC_SIZE_DEFAULT = 128 * 1024


def oversized_docs(root, limits=None, default=None):
    limits = DOC_SIZE_LIMITS if limits is None else limits
    default = DOC_SIZE_DEFAULT if default is None else default
    bad = []
    for path in sorted(glob.glob(os.path.join(root, "docs", "*.md"))):
        rel = "docs/" + os.path.basename(path)
        size, cap = os.path.getsize(path), limits.get(rel, default)
        if size > cap:
            bad.append("%s：%d 位元組，超過上限 %d" % (rel, size, cap))
    return bad


class TestDocsStaySmall(unittest.TestCase):
    """文件不會一夕長 5 倍：超過上限就是腳本出事（A1-3 的 24 MB 事故），不是文件寫太多。"""

    def test_every_markdown_under_docs_is_within_its_size_cap(self):
        self.assertTrue(glob.glob(os.path.join(ROOT, "docs", "*.md")))
        self.assertEqual(oversized_docs(ROOT), [], "docs/ 底下有檔案超過大小上限（腳本出事？）：%s" % oversized_docs(ROOT))

    def test_size_cap_scanner_catches_an_oversized_file(self):
        import tempfile
        import shutil
        d = tempfile.mkdtemp(prefix="iw-docs-cap-")
        try:
            os.makedirs(os.path.join(d, "docs"))
            with io.open(os.path.join(d, "docs", "big.md"), "w", encoding="utf-8") as fh:
                fh.write("x" * 2000)
            with io.open(os.path.join(d, "docs", "small.md"), "w", encoding="utf-8") as fh:
                fh.write("ok")
            self.assertEqual(oversized_docs(d, {}, 1000), ["docs/big.md：2000 位元組，超過上限 1000"])
            self.assertEqual(oversized_docs(d, {"docs/big.md": 5000}, 1000), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestPrivateSpecStaysOut(unittest.TestCase):
    """分析方法目錄的原始版本（含個人資產配置）永遠不進公開倉庫。"""

    def test_gitignore_blocks_the_private_catalog(self):
        gi = read_text(".gitignore")
        self.assertIn("docs/InvestWatch_分析方法目錄_*.md", gi)

    def test_private_catalog_is_not_tracked(self):
        try:
            out = subprocess.run(["git", "ls-files", "docs"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        except Exception:
            self.skipTest("這裡沒有 git")
        tracked = out.stdout.decode("utf-8", "replace")
        self.assertNotIn("分析方法目錄", tracked)
        self.assertNotIn("InvestWatch_", tracked)


if __name__ == "__main__":
    if "--regen-digests" in sys.argv:
        sys.exit(regen_digests())
    unittest.main(verbosity=2)
