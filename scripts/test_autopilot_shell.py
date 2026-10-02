#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_autopilot_shell.py — 停點 P1「自動駕駛」：把一段 shell 指令拆成一個一個實際會執行的指令（.claude/hooks/iw_shell.py）

跑法：python -m unittest discover -s scripts -p "test_*.py"
純函式、離線。守門（iw_guard）的每一條規則都建立在「拆得對」上面，所以這裡單獨釘住：
引號與跳脫、`&&` `||` `;` `|` 換行、子殼、`$( )` 與反引號、heredoc、轉向、變數代回去、關鍵字；PowerShell；cmd /c。
原則：寧可多看（把不一定會執行的也列出來），不可以少看；看不懂就丟 ParseError。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".claude", "hooks"))
import iw_shell as S   # noqa: E402


def argvs(text, env=None):
    return [c.argv for c in S.parse_bash(text, env).cmds]


def ps(text):
    return [c.argv for c in S.parse_powershell(text).cmds]


class TestBash(unittest.TestCase):
    def test_plain_and_operators(self):
        self.assertEqual(argvs("git push origin main"), [["git", "push", "origin", "main"]])
        self.assertEqual(argvs("a && b || c ; d | e |& f & g\nh"), [["a"], ["b"], ["c"], ["d"], ["e"], ["f"], ["g"], ["h"]])
        self.assertEqual(argvs("( a ; b )"), [["a"], ["b"]])
        self.assertEqual(argvs("{ a; b; }"), [["a"], ["b"]])
        self.assertEqual(argvs("a  # 註解 && git push\nb"), [["a"], ["b"]])
        self.assertEqual(argvs("a \\\n  b c"), [["a", "b", "c"]])

    def test_quotes_and_escapes(self):
        self.assertEqual(argvs("echo 'a b' \"c d\" e\\ f"), [["echo", "a b", "c d", "e f"]])
        self.assertEqual(argvs("\"git\" 'push' or\"ig\"in ma'in'"), [["git", "push", "origin", "main"]])
        self.assertEqual(argvs("g\\it push"), [["git", "push"]])
        self.assertEqual(argvs("echo \"a \\\"b\\\" c\""), [["echo", "a \"b\" c"]])
        self.assertEqual(argvs("echo 'it''s'"), [["echo", "its"]])
        self.assertEqual(argvs("echo a#b"), [["echo", "a#b"]])

    def test_unterminated_things_raise(self):
        for bad in ("echo 'abc", "echo \"abc", "echo $(git status", "echo `git status", "cat <<EOF\nno end", "cat <<", "echo > "):
            with self.assertRaises(S.ParseError, msg=bad):
                S.parse_bash(bad)

    def test_substitutions_are_listed_as_commands_too(self):
        p = S.parse_bash("echo \"$(git rev-parse HEAD)\" `whoami` $(a $(b))")
        self.assertEqual([c.argv for c in p.cmds if not c.nested][0][0], "echo")
        self.assertEqual(sorted(c.argv[0] for c in p.cmds if c.nested), ["a", "b", "git", "whoami"])
        self.assertTrue(all(w.dynamic for w in p.cmds[0].words[1:]))
        p = S.parse_bash("diff <(git show a) <(git show b)")
        self.assertEqual([c.argv for c in p.cmds if c.nested], [["git", "show", "a"], ["git", "show", "b"]])
        p = S.parse_bash("echo ${X:-$(git push)}")
        self.assertIn(["git", "push"], [c.argv for c in p.cmds])
        p = S.parse_bash("echo $((1 + 2))")
        self.assertEqual(len(p.cmds), 1)

    def test_variables_assigned_in_the_same_command_are_substituted(self):
        got = argvs("PY=\"C:/Python/python.exe\"\n\"$PY\" -m unittest")
        self.assertEqual(got[-1], ["C:/Python/python.exe", "-m", "unittest"])
        got = argvs("G=git; $G push origin main")
        self.assertEqual(got[-1], ["git", "push", "origin", "main"])
        got = argvs("export A=1 B=two; echo $A ${B}")
        self.assertEqual(got[-1], ["echo", "1", "two"])
        p = S.parse_bash("X=$(date); echo $X")                      # 值是執行時才知道的：不代、標成動態
        self.assertTrue(p.cmds[1].words[1].dynamic)
        p = S.parse_bash("echo $UNKNOWN")
        self.assertTrue(p.cmds[0].words[1].dynamic)
        p = S.parse_bash("A=1; unset A; echo $A")
        self.assertTrue(p.cmds[-1].words[1].dynamic)
        p = S.parse_bash("echo '$A'")                               # 單引號裡的 $ 是字面
        self.assertFalse(p.cmds[0].words[1].dynamic)
        self.assertEqual(p.cmds[0].argv[1], "$A")

    def test_prefix_assignments_belong_to_one_command(self):
        p = S.parse_bash("FOO=bar GIT_DIR=/x git push")
        self.assertEqual(p.cmds[0].argv, ["git", "push"])
        self.assertEqual([n for n, _ in p.cmds[0].assign], ["FOO", "GIT_DIR"])
        p = S.parse_bash("\"A=b\" c")                               # 整個加引號就是一般的字
        self.assertEqual(p.cmds[0].argv, ["A=b", "c"])

    def test_home(self):
        self.assertEqual(argvs("ls ~/.claude/settings.json", {"HOME": "C:/Users/fake"}), [["ls", "C:/Users/fake/.claude/settings.json"]])
        self.assertEqual(argvs("ls $HOME/a", {"HOME": "C:/Users/fake"}), [["ls", "C:/Users/fake/a"]])
        self.assertEqual(argvs("ls a~b"), [["ls", "a~b"]])

    def test_redirects(self):
        c = S.parse_bash("python x.py > out.log 2>&1 < in.txt").cmds[0]
        self.assertEqual(c.argv, ["python", "x.py"])
        self.assertEqual([(op, w.text) for op, w in c.redirs], [(">", "out.log"), (">&", "1"), ("<", "in.txt")])
        c = S.parse_bash("echo x >> a.txt &> b.txt >| c.txt").cmds[0]
        self.assertEqual([op for op, _ in c.redirs], [">>", "&>", ">|"])
        c = S.parse_bash("> only.txt").cmds[0]
        self.assertEqual((c.argv, c.redirs[0][1].text), ([], "only.txt"))

    def test_heredocs_are_data_not_commands(self):
        p = S.parse_bash("python - <<'EOF'\nimport os\nos.system('git push origin main')\nEOF\necho done")
        self.assertEqual([c.argv for c in p.cmds], [["python", "-"], ["echo", "done"]])
        self.assertEqual(p.heredocs, ["import os\nos.system('git push origin main')"])
        self.assertTrue(p.cmds[0].heredoc)
        p = S.parse_bash("cat <<EOF > out.txt\nhello $(git push)\nEOF")       # 結束字沒加引號：裡面的 $( ) 會執行
        self.assertIn(["git", "push"], [c.argv for c in p.cmds])
        p = S.parse_bash("cat <<-EOF\n\tindented\n\tEOF\necho after")
        self.assertEqual([c.argv for c in p.cmds], [["cat"], ["echo", "after"]])
        p = S.parse_bash("cat <<A; cat <<B\none\nA\ntwo\nB\n")
        self.assertEqual(p.heredocs, ["one", "two"])
        self.assertEqual(argvs("cat <<< 'here string'"), [["cat"]])

    def test_keywords(self):
        self.assertEqual(argvs("if a; then b; elif c; then d; else e; fi"), [["a"], ["b"], ["c"], ["d"], ["e"]])
        self.assertEqual(argvs("for f in x y z; do git add \"$f\"; done")[0][:2], ["git", "add"])
        self.assertEqual(argvs("while read l; do echo $l; done < f.txt")[0], ["read", "l"])
        self.assertEqual(argvs("! git diff --quiet"), [["git", "diff", "--quiet"]])
        self.assertEqual(argvs("[[ -f a && -d b ]] && echo ok"), [["[[", "-f", "a", "-d", "b"], ["echo", "ok"]])
        self.assertEqual(argvs("[ \"$a\" = b ] || exit 1")[1], ["exit", "1"])
        self.assertEqual(argvs("echo if then fi"), [["echo", "if", "then", "fi"]])    # 不在指令開頭就只是字

    def test_complex_constructs_are_flagged(self):
        for text in ("case \"$x\" in a) git push;; esac", "f() { git push; }; f", "function f { git push; }", "(( i++ ))",
                     "for ((i=0;i<3;i++)); do echo $i; done", "coproc git push"):
            p = S.parse_bash(text)
            self.assertTrue(p.complex, text)
        self.assertIn(["git", "push"], argvs("f() { git push; }; f"))             # 看不完整，但裡面的指令還是列出來
        self.assertFalse(S.parse_bash("git status && ls").complex)

    def test_globs_and_dynamic_flags(self):
        w = S.parse_bash("ls *.py 'a*' \"$X\"").cmds[0].words
        self.assertTrue(w[1].glob)
        self.assertFalse(w[1].dynamic)
        self.assertFalse(w[2].glob)                                  # 加了引號就是字面的星號，shell 不會展開
        self.assertEqual(w[2].text, "a*")
        self.assertTrue(w[3].dynamic)

    def test_limits(self):
        with self.assertRaises(S.ParseError):
            S.parse_bash("echo " + "x" * 200001)
        deep = "echo " + "$(" * 10 + "x" + ")" * 10
        with self.assertRaises(S.ParseError):
            S.parse_bash(deep)


class TestPowerShell(unittest.TestCase):
    def test_statements_and_quotes(self):
        self.assertEqual(ps("git push origin HEAD:main"), [["git", "push", "origin", "HEAD:main"]])
        self.assertEqual(ps("Set-Location 'D:\\a b'; git status | Select-Object -First 3"),
                         [["Set-Location", "D:\\a b"], ["git", "status"], ["Select-Object", "-First", "3"]])
        self.assertEqual(ps("a && b || c\nd"), [["a"], ["b"], ["c"], ["d"]])
        self.assertEqual(ps("Write-Output 'it''s' \"a `\"b`\"\""), [["Write-Output", "it's", "a \"b\""]])
        self.assertEqual(ps("git status # comment; git push"), [["git", "status"]])
        self.assertEqual(ps("<# block #> git status"), [["git", "status"]])

    def test_call_operator_assignment_and_blocks(self):
        self.assertEqual(ps("& \"C:\\Program Files\\Git\\cmd\\git.exe\" push origin main"),
                         [["C:\\Program Files\\Git\\cmd\\git.exe", "push", "origin", "main"]])
        self.assertEqual(ps("$x = git rev-parse HEAD"), [["git", "rev-parse", "HEAD"]])
        self.assertIn(["git", "push"], ps("if ($?) { git push }"))
        self.assertIn(["git", "push"], ps("foreach ($f in $files) { git push }"))
        self.assertIn(["git", "push"], ps("Write-Output \"$(git push)\""))
        self.assertIn(["git", "status"], ps("try { git status } catch { Write-Host 'x' }"))

    def test_redirects(self):
        c = S.parse_powershell("git push 2>&1 > out.txt").cmds[0]
        self.assertEqual(c.argv, ["git", "push"])
        self.assertEqual([(op, w.text) for op, w in c.redirs], [(">", "out.txt")])
        c = S.parse_powershell("'{}' > .claude\\settings.json").cmds[0]
        self.assertEqual(c.redirs[0][1].text, ".claude\\settings.json")

    def test_here_string(self):
        got = ps("$t = @'\ngit push origin main\n'@\nWrite-Output $t")
        self.assertEqual(got[-1], ["Write-Output", "$t"])
        self.assertNotIn(["git", "push", "origin", "main"], got)

    def test_unterminated(self):
        for bad in ("Write-Output 'abc", "Write-Output \"abc", "<# never closed", "@'\nno end"):
            with self.assertRaises(S.ParseError, msg=bad):
                S.parse_powershell(bad)


class TestCmdString(unittest.TestCase):
    def test_split(self):
        self.assertEqual([c.argv for c in S.parse_cmd_string("git push origin main && echo ok & dir | findstr x").cmds],
                         [["git", "push", "origin", "main"], ["echo", "ok"], ["dir"], ["findstr", "x"]])
        self.assertEqual([c.argv for c in S.parse_cmd_string("\"C:\\Program Files\\Git\\cmd\\git.exe\" push").cmds],
                         [["C:\\Program Files\\Git\\cmd\\git.exe", "push"]])
        with self.assertRaises(S.ParseError):
            S.parse_cmd_string("echo \"abc")
        self.assertTrue(S.parse_cmd_string("echo %PATH%").cmds[0].words[1].dynamic)

    def test_redirects_parentheses_and_separators(self):
        """第二輪：cmd 的轉向、括號、@、逗號——第一版把 > 當成普通的字，「echo x > 保護檔」就看不出來。"""
        c = S.parse_cmd_string("echo x > .claude\\settings.json").cmds[0]
        self.assertEqual(c.argv, ["echo", "x"])
        self.assertEqual([(op, w.text) for op, w in c.redirs], [(">", ".claude\\settings.json")])
        c = S.parse_cmd_string("dir 2>&1 >> out.txt").cmds[0]
        self.assertEqual((c.argv, [(op, w.text) for op, w in c.redirs]), (["dir"], [(">>", "out.txt")]))
        self.assertEqual([c.argv for c in S.parse_cmd_string("(git push origin main)").cmds], [["git", "push", "origin", "main"]])
        self.assertEqual([c.argv for c in S.parse_cmd_string("@git push").cmds], [["git", "push"]])
        self.assertEqual(S.parse_cmd_string("del a,.claude\\settings.json").cmds[0].argv, ["del", "a", ".claude\\settings.json"])
        self.assertTrue(S.parse_cmd_string("echo a^&b").cmds[0].words[1].dynamic)            # ^ 是 cmd 的跳脫字元
        self.assertTrue(S.parse_cmd_string("del *.tmp").cmds[0].words[1].glob)


class TestRound2(unittest.TestCase):
    """第二輪補的：檢查程式看到的字，要跟 shell 實際執行的一樣；不一樣的地方要標出來（由守門決定擋不擋）。"""

    def words(self, text, env=None):
        return S.parse_bash(text, env).cmds[-1].words

    def test_brace_expansion_is_flagged(self):
        """{git,push} 會被 bash 展開成兩個字。對照組：不標 brace → 這一條會紅。"""
        for text in ("{git,push,origin,main}", "a{b,c}d", "x{1..3}", "{a,{b,c}}", "pre{,x}"):
            self.assertTrue(self.words("echo " + text)[1].brace, text)
        for text in ("{}", "'{a,b}'", "\"{a,b}\"", "\\{a,b\\}", "${X}", "HEAD@{1}", "@{u}", "{a}", "a,b", "${X:-a,b}"):
            self.assertFalse(self.words("echo " + text)[1].brace, text)
        self.assertTrue(S.parse_bash("{git,push,origin,main}").cmds[0].words[0].opaque)

    def test_unquoted_variables_split_into_words(self):
        """沒加引號的變數，值裡有空白或萬用字元時 bash 會拆字、展開：不可以當成一個已知的字。"""
        w = self.words("X=\"push origin main\"; git $X")
        self.assertEqual(len(w), 2)
        self.assertTrue(w[1].dynamic)
        self.assertEqual(w[1].text, "push origin main")
        w = self.words("X=\"push origin main\"; git \"$X\"")                       # 加了引號：就是一個字
        self.assertFalse(w[1].dynamic)
        w = self.words("PY=\"C:/Program Files/Python/python.exe\"; \"$PY\" x.py")   # 路徑有空白、加了引號：照樣代回去
        self.assertEqual((w[0].text, w[0].dynamic), ("C:/Program Files/Python/python.exe", False))
        w = self.words("PY=\"C:/Program Files/Python/python.exe\"; $PY x.py")       # 沒加引號：bash 自己也會拆壞
        self.assertTrue(w[0].dynamic)
        w = self.words("X='*.py'; rm $X")
        self.assertTrue(w[1].dynamic)

    def test_empty_unquoted_variable_disappears(self):
        self.assertEqual(argvs("X=; git $X push origin main")[-1], ["git", "push", "origin", "main"])
        self.assertEqual(argvs("X=; git \"$X\" push")[-1], ["git", "", "push"])                 # 加了引號：留著一個空字

    def test_only_assignments_that_certainly_ran_are_substituted(self):
        """對照組：把「不一定會執行的指定不算數」拿掉 → 這一條會紅（G 會被當成 echo）。"""
        for text in ("G=git; false && G=echo; $G push",              # && 後面：不一定會執行
                     "G=git; true || G=echo; $G push",
                     "G=echo; if true; then G=git; fi; $G push",     # if 裡面
                     "G=echo; for i in 1; do G=git; done; $G push",
                     "G=echo; { G=git; }; $G push",
                     "G=echo; ( G=git ); $G push",                   # 子殼
                     "G=echo; G=git | cat; $G push",                 # 管線裡
                     "G=echo; G=git & wait; $G push",                # 丟到背景
                     "G=; : ${G:=git}; $G push",                     # ${G:=…} 會順便指定
                     "f() { G=git; }; G=echo; f; $G push",           # 函式
                     "G=echo; trap 'G=git' DEBUG; $G push",
                     "G=echo; eval G=git; $G push",
                     "G=echo; source ./x.sh; $G push",
                     "G=echo; read G; $G push",
                     "G=echo; unset G; $G push"):
            w = self.words(text)
            self.assertTrue(w[0].dynamic, "%s → %r" % (text, [x.text for x in w]))
        for text in ("G=git; $G push", "G=git\n$G push", "export G=git; $G push", "cd x && G=git && $G push",
                     "G=git; if true; then $G push; fi", "A=gi; B=t; $A$B push"):
            w = self.words(text)
            self.assertEqual((w[0].text, w[0].dynamic), ("git", False), text)
        self.assertTrue(self.words("cd x && G=git && true; $G push")[0].dynamic)             # 那一串 && 結束之後就不算數

    def test_tilde_forms(self):
        env = {"HOME": "C:/Users/fake"}
        self.assertEqual(self.words("ls ~/x", env)[1].text, "C:/Users/fake/x")
        self.assertEqual(self.words("ls ~", {"HOME": "C:/Users/fake name"})[1].text, "C:/Users/fake name")     # 家目錄有空白也不拆
        for text in ("~-/x", "~+/x", "~someone/x", "~2"):
            w = self.words("ls " + text, env)[1]
            self.assertTrue(w.dynamic and w.lead_dyn, text)

    def test_dollar_quotes_are_dynamic(self):
        for text in ("$'main'", "$\"main\"", "$'\\x6dain'"):
            self.assertTrue(self.words("git push origin " + text)[3].dynamic, text)

    def test_lead_dyn(self):
        self.assertTrue(self.words("rm \"$X/a\"")[1].lead_dyn)
        self.assertFalse(self.words("rm a/$X")[1].lead_dyn)
        self.assertFalse(self.words("rm a")[1].lead_dyn)

    def test_case_patterns_are_not_commands(self):
        got = argvs("case \"$f\" in *.py) echo py;; a|b) echo ab;; *) echo other;; esac; echo done")
        self.assertEqual(got, [["echo", "py"], ["echo", "ab"], ["echo", "other"], ["echo", "done"]])
        got = argvs("case x in\n  git) git push origin main;;\nesac")                         # 樣式不是指令，後面的指令照樣列出來
        self.assertEqual(got, [["git", "push", "origin", "main"]])

    def test_where_each_command_sits(self):
        p = S.parse_bash("a && b | c; if d; then e; fi; ( f )")
        info = dict((c.argv[0], (c.pre_op, c.post_op, c.depth)) for c in p.cmds)
        self.assertEqual(info["a"], (None, "&&", 0))
        self.assertEqual(info["b"], ("&&", "|", 0))
        self.assertEqual(info["c"][0], "|")
        self.assertEqual(info["e"][2], 1)
        self.assertEqual(info["f"][2], 1)
        self.assertTrue(S.parse_bash("f() { a; }; f").functions)
        self.assertFalse(S.parse_bash("a; b").functions)

    def test_powershell_calls_wildcards_and_splatting(self):
        c = S.parse_powershell("& $g push origin main").cmds[0]
        self.assertTrue(c.call and c.words[0].dynamic)
        c = S.parse_powershell("$x | Out-File y").cmds[0]                                     # 運算式開頭：不是在執行程式
        self.assertFalse(c.call)
        c = S.parse_powershell(". .\\profile.ps1").cmds[0]
        self.assertTrue(c.call)
        self.assertTrue(S.parse_powershell("Remove-Item '.cl*'").cmds[0].words[1].glob)       # 加了引號照樣展開
        self.assertTrue(S.parse_powershell("Remove-Item a?b").cmds[0].words[1].glob)
        self.assertFalse(S.parse_powershell("[System.IO.File]::Exists('x')").cmds[0].words[0].glob)
        self.assertTrue(S.parse_powershell("Set-Content @p").cmds[0].words[1].dynamic)        # 一包參數攤開
        cmds = S.parse_powershell("Get-ChildItem x | Remove-Item").cmds
        self.assertEqual((cmds[0].post_op, cmds[1].pre_op), ("|", "|"))
        self.assertEqual(S.parse_powershell("Set-Content -Path (Join-Path a b) -Value c").cmds[0].post_op, "(")


if __name__ == "__main__":
    unittest.main()
