#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_mutations.py — 突變對照的執行器：把保護改壞一處 → 只跑對應的測試 → 必須紅 → 還原。

停點 P2 把 P1／P1-1 放在 .autopilot/runs/ 裡的 mut.py 搬進倉庫，驗收機（.github/workflows/verify.yml）才跑得到。
定義（哪一處改成什麼、跑哪些測試）在同資料夾的 autopilot_mutations.py；已知不會紅的例外在 known_survivors.json。
驗收機一律用 main 上的這一支執行器與例外清單，突變的定義用分支上的（新規則要有新突變）——分支改了執行器或例外清單，
結果會標「驗收機本身有改」（見 scripts/verify_ci.py）。

在「複製出來的副本」上做，不動倉庫本身。每一筆定義：
    (編號, 白話說明, 檔案, 原文, 改成, [unittest 的參數…])
原文必須剛好出現一次（不然就是錨點寫錯了，直接算錯誤、不算紅）；原文／改成可以各給一串（同一個檔改幾處）。

  python scripts/mutations/run_mutations.py                      全部跑（先跑五組基準，要全綠）
  python scripts/mutations/run_mutations.py M07 N01              只跑指定的（基準只跑快的三組）
  python scripts/mutations/run_mutations.py --list               只列出來
  python scripts/mutations/run_mutations.py --check-anchors      只核對每個錨點剛好出現一次（不跑測試）
  python scripts/mutations/run_mutations.py --shard 2/6 --no-baseline --out r.json     驗收機分片用
結果寫到 --out（預設同資料夾的 mut_result.json）；--md 另外寫一張表。
"""
import argparse
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
DEFAULT_DEFS = os.path.join(HERE, "autopilot_mutations.py")
DEFAULT_KNOWN = os.path.join(HERE, "known_survivors.json")
IGNORE = (".git", "__pycache__", ".autopilot", "node_modules", "worktrees", ".verify-out", ".verify-in", "mutcopy")
# 這兩條測試檢查的是「清單裡每個錨點在倉庫裡剛好出現一次」。任何突變把原文改掉之後它們一定會紅，
# 所以不能拿來當「這個突變被測試抓到」的證據（2026-10-05 發現；當時沒有任何一個突變是只靠它們紅的）。
ANCHOR_SELF_TESTS = ("test_the_real_definitions_load_and_their_anchors_are_unique", "test_mutation_list_lives_in_the_repo_and_its_anchors_hold")


def load_defs(path):
    """讀定義檔：回傳 (MUTATIONS, 基準清單)。用檔案路徑載入，所以 main 上的那一份也能這樣讀。"""
    spec = importlib.util.spec_from_file_location("iw_mutation_defs_%d" % abs(hash(path)), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.MUTATIONS), list(getattr(mod, "BASELINES", []))


def load_known(path):
    if not path or not os.path.exists(path):
        return {}
    with io.open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    return dict((k, v) for k, v in obj.items() if not k.startswith("_"))


def pairs_of(m):
    old, new = m[3], m[4]
    return list(zip(old, new)) if isinstance(old, (list, tuple)) else [(old, new)]


def read_text(path):
    with io.open(path, encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


def check_anchors(repo, defs):
    """每個錨點在 repo 裡剛好出現一次，而且改壞之後的檔還讀得懂（.py 編譯得過、.json 解析得過）。回傳問題清單（空的＝都對）。
    為什麼連語法也查：2026-10-04 有一個突變的錨點切在註解中間，改完變成語法錯——測試一條都沒跑到，看起來像「沒有紅」，其實是定義寫錯。"""
    problems = []
    ids = [m[0] for m in defs]
    if len(ids) != len(set(ids)):
        problems.append("編號重複：%s" % "、".join(sorted(set(i for i in ids if ids.count(i) > 1))))
    for m in defs:
        path = os.path.join(repo, *m[2].split("/"))
        if not os.path.exists(path):
            problems.append("%s：檔案不存在 %s" % (m[0], m[2]))
            continue
        text = read_text(path)
        mutated, ok = text, True
        for old, new in pairs_of(m):
            n = text.count(old)
            if n != 1:
                problems.append("%s：錨點在 %s 出現 %d 次（要剛好 1 次）" % (m[0], m[2], n))
                ok = False
                continue
            if old == new:
                problems.append("%s：改成的內容跟原文一樣" % m[0])
                ok = False
            mutated = mutated.replace(old, new)
        if not ok:
            continue
        try:
            if m[2].endswith(".py"):
                compile(mutated, m[2], "exec")
            elif m[2].endswith(".json"):
                json.loads(mutated)
        except (SyntaxError, ValueError) as e:
            problems.append("%s：改壞之後 %s 讀不懂了（%s）——這是突變定義寫錯，不是「改壞→紅」" % (m[0], m[2], str(e)[:80]))
        if not [t for t in m[5] if t.endswith(".py")]:
            problems.append("%s：沒有指定要跑哪個測試檔" % m[0])
    return problems


def shard_of(items, spec):
    """--shard i/n：第 i 片（1 起算）。"""
    m = re.match(r"^(\d+)/(\d+)$", spec or "")
    if not m:
        raise SystemExit("--shard 要寫成 i/n，例如 2/6")
    i, n = int(m.group(1)), int(m.group(2))
    if not (1 <= i <= n):
        raise SystemExit("--shard 的 i 要在 1 到 n 之間")
    return [x for idx, x in enumerate(items) if idx % n == i - 1]


def fresh_copy(src, dst):
    if os.path.isdir(dst):
        shutil.rmtree(dst)

    def ignore(d, names):
        return [n for n in names if n in IGNORE]
    shutil.copytree(src, dst, ignore=ignore)


def run_tests(copy, python, args, browser=None, timeout=1800):
    """unittest 的命令列：測試檔要排在 -k 選項前面（幾組清單串起來時路徑會夾在 -k 中間，argparse 會說 unrecognized arguments）；同一個檔只列一次。"""
    env = dict(os.environ, IW_TEST_NO_SIDE_EFFECTS="1", PYTHONIOENCODING="utf-8")
    if browser:
        env["IW_BROWSER"] = browser
    env.pop("IW_SCAN_SALT_FILE", None)
    env.pop("IW_SCAN_TERMS_FILE", None)
    paths, opts = [], []
    for a in args:
        if a.endswith(".py"):
            if a not in paths:
                paths.append(a)
        else:
            opts.append(a)
    cmd = list(python) + ["-X", "utf8", "-W", "ignore", "-m", "unittest"] + paths + opts
    p = subprocess.run(cmd, cwd=copy, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    ran = re.search(r"^Ran (\d+) tests?", out, re.M)
    fails = re.findall(r"^(?:FAIL|ERROR): (\S+)", out, re.M)
    tail = out.strip().split("\n")[-1] if out.strip() else ""
    return p.returncode, int(ran.group(1)) if ran else 0, sorted(set(fails)), tail, out


def run_one(m, repo, copy, python, browser, timeout):
    mid, desc, rel = m[0], m[1], m[2]
    tests = m[5]
    src = os.path.join(repo, *rel.split("/"))
    dst = os.path.join(copy, *rel.split("/"))
    text = read_text(src)
    pairs = pairs_of(m)
    counts = [text.count(o) for o, _ in pairs]
    if any(c != 1 for c in counts):
        n = [c for c in counts if c != 1][0]
        return {"id": mid, "desc": desc, "file": rel, "ok": False, "error": "錨點出現 %d 次" % n, "red": [], "ran": 0}
    t0 = time.time()
    mutated = text
    for o, nw in pairs:
        mutated = mutated.replace(o, nw)
    with io.open(dst, "w", encoding="utf-8", newline="") as fh:
        fh.write(mutated)
    try:
        rc, ran, fails, tail, out = run_tests(copy, python, tests, browser, timeout)
    finally:
        shutil.copyfile(src, dst)
    incidental = [f for f in fails if f in ANCHOR_SELF_TESTS]
    fails = [f for f in fails if f not in ANCHOR_SELF_TESTS]
    red = rc != 0 and bool(fails)
    rec = {"id": mid, "desc": desc, "file": rel, "rc": rc, "ran": ran, "red": fails, "ok": red, "tail": tail,
           "seconds": round(time.time() - t0, 1)}
    if incidental:
        rec["incidental"] = incidental
    if ran == 0:                                                   # 一條測試都沒跑到：篩選字寫錯、或改壞之後連載入都失敗——算定義錯誤，不算紅也不算存活
        rec.update({"ok": False, "error": "沒有跑到任何測試（%s）" % (tail or "?")[:80]})
    if rc != 0 and not fails and not incidental:
        rec["output_tail"] = out[-1500:]
    return rec


def write_md(path, results):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("| # | 改壞的方式 | 結果 |\n|---|---|---|\n")
        for r in results:
            if r["id"].startswith("基準"):
                continue
            if r.get("error"):
                fh.write("| %s | %s | 錨點錯誤：%s |\n" % (r["id"], r["desc"], r["error"]))
            else:
                names = "、".join(sorted(set(x.split(".")[-1] if "." in x else x for x in r["red"]))[:3])
                fh.write("| %s | %s | %s |\n" % (r["id"], r["desc"], ("%d 條紅（%s%s）" % (len(r["red"]), names, "…" if len(r["red"]) > 3 else ""))
                                                 if r["ok"] else "**沒有紅**"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="突變對照")
    ap.add_argument("ids", nargs="*", help="只跑這些編號")
    ap.add_argument("--defs", default=DEFAULT_DEFS, help="定義檔（預設同資料夾的 autopilot_mutations.py）")
    ap.add_argument("--known", default=DEFAULT_KNOWN, help="已知不會紅的例外清單（JSON）")
    ap.add_argument("--repo", default=REPO, help="要改壞的倉庫（預設這個檔所在的倉庫）")
    ap.add_argument("--copy", default=None, help="副本放哪裡（預設 --out 旁邊的 mutcopy）")
    ap.add_argument("--out", default=os.path.join(HERE, "mut_result.json"))
    ap.add_argument("--md", default=None, help="另外寫一張 markdown 表")
    ap.add_argument("--python", default=None, help="跑測試用的 Python（預設跟這一支一樣）")
    ap.add_argument("--browser", default=os.environ.get("IW_BROWSER"), help="IW_BROWSER（瀏覽器那幾組用；突變不會跑到它們）")
    ap.add_argument("--shard", default=None, help="i/n：只跑第 i 片")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check-anchors", action="store_true", dest="check_anchors")
    ap.add_argument("--no-baseline", action="store_true", dest="no_baseline")
    ap.add_argument("--full-baseline", action="store_true", dest="full_baseline")
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:                                              # noqa: B902
        pass
    defs, baselines = load_defs(a.defs)
    known = load_known(a.known)
    if a.list:
        for m in defs:
            print(m[0], m[1])
        return 0
    problems = check_anchors(a.repo, defs)
    if a.check_anchors:
        for p in problems:
            print("✗", p)
        print("錨點 %d 個，%s" % (sum(len(pairs_of(m)) for m in defs), "都剛好出現一次" if not problems else "%d 個有問題" % len(problems)))
        return 1 if problems else 0
    python = [a.python] if a.python else [sys.executable]
    copy = a.copy or os.path.join(os.path.dirname(os.path.abspath(a.out)), "mutcopy")
    want = set(a.ids)
    todo = [m for m in defs if not want or m[0] in want]
    if a.shard:
        todo = shard_of(todo, a.shard)
    fresh_copy(a.repo, copy)
    results, bad = [], 0
    if not a.no_baseline:
        print("基準（沒改壞）：先確認全綠")
        for name, tests in baselines:
            if (want or a.shard) and name in ("prepush", "flow") and not a.full_baseline:
                continue
            rc, ran, fails, tail, out = run_tests(copy, python, tests, a.browser, a.timeout)
            print("  %-8s rc=%d ran=%d %s" % (name, rc, ran, tail))
            results.append({"id": "基準-" + name, "desc": "沒有改壞", "rc": rc, "ran": ran, "red": fails, "ok": rc == 0})
            if rc != 0:
                print(out[-3000:])
                _dump(a, defs, results, known, baseline_failed=True)
                return 2
    for m in todo:
        rec = run_one(m, a.repo, copy, python, a.browser, a.timeout)
        results.append(rec)
        if rec.get("error"):
            bad += 1
            print("✗ %s %s：%s" % (rec["id"], rec["error"], rec["desc"]))
            continue
        if not rec["ok"]:
            if rec["id"] in known:
                print("○ %s 沒有紅（已知例外：%s）" % (rec["id"], known[rec["id"]][:60]))
            else:
                bad += 1
        mark = "✓" if rec["ok"] else ("○ 沒有紅（已知例外）" if rec["id"] in known else "✗ 沒有紅")
        print("%s %s %-58s 跑 %d 條、紅 %d 條%s" % (mark, rec["id"], rec["desc"][:58], rec["ran"], len(rec["red"]),
                                                 "" if rec["ok"] else "  ← " + rec.get("tail", "")))
    _dump(a, defs, results, known)
    n = len([r for r in results if not r["id"].startswith("基準")])
    print("突變 %d 個，不符預期 %d 個（已知例外不算）" % (n, bad))
    return 1 if bad else 0


def _dump(a, defs, results, known, baseline_failed=False):
    out = {"defs": os.path.abspath(a.defs), "total_defined": len(defs), "shard": a.shard, "known_survivors": known,
           "baseline_failed": baseline_failed, "results": results}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    if a.md:
        write_md(a.md, results)


if __name__ == "__main__":
    sys.exit(main())
