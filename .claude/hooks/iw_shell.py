# -*- coding: utf-8 -*-
"""
iw_shell.py — 把一段 shell 指令「拆開」成一個一個實際會執行的指令（純函式，不執行任何東西）。

為什麼自己拆：Claude Code 官方文件說權限規則「不是安全邊界」——`git -C . push`、`sh -c 'git push'`、
`$(git push)` 這些寫法規則比不到；要硬擋就得自己看整段指令。這裡做的事：

  * Bash：引號、跳脫、`&&` `||` `;` `|` 換行、子殼 `( )`、`$( )` 與反引號、heredoc、轉向、
    指令前面的變數指定、`if／for／while` 這些關鍵字；同一段指令裡指定過的變數（`PY="…"` 之後的 `"$PY"`）會代回去。
  * PowerShell：引號、`;` `|` `&&` `||` 換行、`&` 呼叫、`$( )` 與 `{ }`。
  * `cmd /c "…"` 的那一段字串。

原則：寧可多看（把不一定會執行的也當成會執行），不可以少看；看不懂就丟 ParseError，由呼叫的人決定（守門一律當成擋下）。
"""
import re


class ParseError(Exception):
    pass


class Word(object):
    """一個字。parts 是 [('lit', 字串) | ('var', 變數名) | ('qvar', 雙引號裡的變數名) | ('dyn', 原文)]。
    dynamic＝含有執行時才知道的東西。glob＝有沒加引號的萬用字元（* ? [）。brace＝有沒加引號的大括號展開（{a,b}、{1..3}）。
    lead_dyn＝這個字的開頭就是看不出來的東西（連「大概在哪個資料夾」都推不出來）。"""
    __slots__ = ("parts", "raw", "quoted", "glob", "brace")

    def __init__(self, parts, raw, quoted=False, glob=False, brace=False):
        self.parts = parts
        self.raw = raw
        self.quoted = quoted
        self.glob = glob
        self.brace = brace

    @property
    def dynamic(self):
        return any(k != "lit" for k, _ in self.parts)

    @property
    def opaque(self):
        """實際的字看不出來：有執行時才知道的東西，或是會被 shell 展開成別的字（大括號、萬用字元）。"""
        return self.dynamic or self.brace or self.glob

    @property
    def lead_dyn(self):
        for k, v in self.parts:
            if k == "lit":
                if v:
                    return False
                continue
            return True
        return False

    @property
    def text(self):
        out = []
        for k, v in self.parts:
            if k == "lit":
                out.append(v)
            elif k in ("var", "qvar"):
                out.append("$" + v)
            else:
                out.append(v)
        return "".join(out)

    def resolved(self, env):
        """把已知的變數代回去；回傳新的 Word。
        沒加引號的變數如果值裡有空白或萬用字元，bash 會把它拆成好幾個字、再展開——那就不算「知道」，留成看不出來的。"""
        parts = []
        for k, v in self.parts:
            if k == "qvar" and v in env:
                parts.append(("lit", env[v]))
            elif k == "var" and v in env:
                if re.search(r"[\s*?\[]", env[v]):
                    parts.append(("dyn", env[v]))
                else:
                    parts.append(("lit", env[v]))
            else:
                parts.append((k, v))
        return Word(parts, self.raw, self.quoted, self.glob, self.brace)

    def __repr__(self):
        return "Word(%r%s)" % (self.text, ", dynamic" if self.dynamic else "")


def lit(text):
    return Word([("lit", text)], text)


class Cmd(object):
    def __init__(self):
        self.assign = []          # [(名稱, Word)]：寫在指令前面的變數指定
        self.words = []           # [Word]：指令名稱與參數
        self.redirs = []          # [(運算子, Word)]
        self.heredoc = False      # 有 heredoc 餵給它
        self.heredoc_bodies = []
        self.nested = False       # 來自 $( )、反引號、sh -c 這類「裡面的指令」
        self.call = False         # PowerShell：用 & 或 . 呼叫的（後面那個字會被當成程式執行）
        self.pre_op = None        # 它前面的運算子（; && || | & ( ) 換行；None＝一開始）
        self.post_op = None       # 它後面的運算子
        self.depth = 0            # 在幾層 if／for／while／{ }／( ) 裡面

    @property
    def argv(self):
        return [w.text for w in self.words]

    @property
    def program_word(self):
        return self.words[0] if self.words else None

    def __repr__(self):
        return "Cmd(%r)" % (self.argv,)


class Parsed(object):
    def __init__(self):
        self.cmds = []
        self.complex = False      # 有看不完整的結構（case、函式定義、算術指令…）
        self.heredocs = []        # 所有 heredoc 的內容（當資料看）
        self.notes = []
        self.functions = False    # 定義了函式（函式可以偷改變數，之後的變數都當成不知道）


# ---------------------------------------------------------------- Bash：斷字

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_ASSIGN_RE = re.compile(r"^(" + _NAME + r")\+?=")
_WORD_BREAK = " \t\r\n;|&()<>"


def _find_backtick(s, i):
    n = len(s)
    while i < n:
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "`":
            return i
        i += 1
    raise ParseError("反引號沒有結束")


def _skip_dquote(s, i):
    """i 是開頭雙引號之後的位置；回傳結尾雙引號之後的位置。"""
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        if c == "$" and i + 1 < n and s[i + 1] == "(":
            i = _find_matching(s, i + 2, "(", ")") + 1
            continue
        if c == "`":
            i = _find_backtick(s, i + 1) + 1
            continue
        i += 1
    raise ParseError("雙引號沒有結束")


def _find_matching(s, i, opener, closer):
    """s[i] 是 opener 之後的第一個字元。回傳對應 closer 的位置（看得懂引號與巢狀）。"""
    depth, n = 1, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise ParseError("單引號沒有結束")
            i = j + 1
            continue
        if c == '"':
            i = _skip_dquote(s, i + 1)
            continue
        if c == "`":
            i = _find_backtick(s, i + 1) + 1
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ParseError("括號沒有結束")


class _Lexer(object):
    def __init__(self, s):
        self.s = s
        self.i = 0
        self.toks = []           # ('word', Word) | ('op', 字串) | ('redir', 字串) | ('hd', 內容, 有沒有引號)
        self.subs = []           # 裡面的指令字串：$( )、反引號、<( )
        self.pending = []        # 等下一個換行才開始的 heredoc：[(結束字, 去掉前導 tab, 有引號)]
        self.assigned = set()    # 用 ${名稱:=值} 這種寫法偷偷指定的變數（值看不出來）

    # -- $ 與反引號
    def _dollar(self, parts, var_kind="var"):
        s, i, n = self.s, self.i, len(self.s)
        if s[i] == "`":
            j = _find_backtick(s, i + 1)
            inner = s[i + 1:j].replace("\\`", "`")
            self.subs.append(inner)
            parts.append(("dyn", s[i:j + 1]))
            self.i = j + 1
            return
        # s[i] == '$'
        if i + 1 >= n:
            parts.append(("lit", "$"))
            self.i = i + 1
            return
        d = s[i + 1]
        if d == "(":
            if i + 2 < n and s[i + 2] == "(":                      # $(( 算術 ))
                j = _find_matching(s, i + 3, "(", ")")
                if j + 1 < n and s[j + 1] == ")":
                    parts.append(("dyn", s[i:j + 2]))
                    self.i = j + 2
                    return
            j = _find_matching(s, i + 2, "(", ")")
            self.subs.append(s[i + 2:j])
            parts.append(("dyn", s[i:j + 1]))
            self.i = j + 1
            return
        if d == "{":
            j = _find_matching(s, i + 2, "{", "}")
            inner = s[i + 2:j]
            if re.match(r"^" + _NAME + r"$", inner):
                parts.append((var_kind, inner))
            else:
                parts.append(("dyn", s[i:j + 1]))
                self._nested_subs(inner)
                m = re.match(r"^(" + _NAME + r"):?=", inner)
                if m:
                    self.assigned.add(m.group(1))                  # ${X:=值} 會順便指定 X，值看不出來
            self.i = j + 1
            return
        m = re.match(_NAME, s[i + 1:])
        if m:
            parts.append((var_kind, m.group(0)))
            self.i = i + 1 + len(m.group(0))
            return
        if d in "0123456789@*#?$!-":
            parts.append(("dyn", s[i:i + 2]))
            self.i = i + 2
            return
        parts.append(("lit", "$"))
        self.i = i + 1

    def _nested_subs(self, text):
        """${…} 裡面如果還有 $( ) 或反引號，也挑出來。"""
        j = 0
        while j < len(text):
            if text[j] == "$" and j + 1 < len(text) and text[j + 1] == "(":
                k = _find_matching(text, j + 2, "(", ")")
                self.subs.append(text[j + 2:k])
                j = k + 1
            elif text[j] == "`":
                k = _find_backtick(text, j + 1)
                self.subs.append(text[j + 1:k])
                j = k + 1
            else:
                j += 1

    # -- 一個字
    def _word(self):
        s, n = self.s, len(self.s)
        start = self.i
        parts, quoted, glob = [], False, False
        bare = []                        # 只留「沒加引號的字面字元」，其他的用 \x00 佔位——找大括號展開用

        def add(t):
            if parts and parts[-1][0] == "lit":
                parts[-1] = ("lit", parts[-1][1] + t)
            else:
                parts.append(("lit", t))

        while self.i < n:
            c = s[self.i]
            if c in _WORD_BREAK:
                break
            if c == "\\":
                if self.i + 1 < n:
                    if s[self.i + 1] != "\n":
                        add(s[self.i + 1])
                        bare.append("\x00")
                    self.i += 2
                else:
                    self.i += 1
                continue
            if c == "'":
                j = s.find("'", self.i + 1)
                if j < 0:
                    raise ParseError("單引號沒有結束")
                add(s[self.i + 1:j])
                bare.append("\x00")
                self.i = j + 1
                quoted = True
                continue
            if c == '"':
                self.i += 1
                quoted = True
                bare.append("\x00")
                while True:
                    if self.i >= n:
                        raise ParseError("雙引號沒有結束")
                    d = s[self.i]
                    if d == '"':
                        self.i += 1
                        break
                    if d == "\\" and self.i + 1 < n and s[self.i + 1] in '"\\$`\n':
                        if s[self.i + 1] != "\n":
                            add(s[self.i + 1])
                        self.i += 2
                        continue
                    if d == "$" or d == "`":
                        self._dollar(parts, "qvar")
                        continue
                    add(d)
                    self.i += 1
                continue
            if c == "$":
                bare.append("\x00")
                if s[self.i:self.i + 2] in ("$'", '$"'):            # $'…'（ANSI-C 引號）、$"…"（翻譯字串）：內容當成執行時才知道
                    q = s[self.i + 1]
                    j = self.i + 2
                    while j < n and s[j] != q:
                        j += 2 if s[j] == "\\" else 1
                    if j >= n:
                        raise ParseError("$%s…%s 沒有結束" % (q, q))
                    parts.append(("dyn", s[self.i:j + 1]))
                    self.i = j + 1
                    quoted = True
                    continue
                self._dollar(parts)
                continue
            if c == "`":
                bare.append("\x00")
                self._dollar(parts)
                continue
            if c in "*?[":
                glob = True
            if c == "~" and self.i == start:
                if self.i + 1 >= n or s[self.i + 1] in "/" + _WORD_BREAK:
                    parts.append(("qvar", "HOME"))                  # ~ 與 ~/…：家目錄（展開後不會再被拆字）
                    self.i += 1
                else:                                               # ~+、~-、~某人、~2：執行時才知道指到哪裡
                    j = self.i + 1
                    while j < n and s[j] not in "/" + _WORD_BREAK:
                        j += 1
                    parts.append(("dyn", s[self.i:j]))
                    self.i = j
                bare.append("\x00")
                continue
            add(c)
            bare.append(c)
            self.i += 1
        if not parts:
            parts = [("lit", "")]
        w = Word(parts, s[start:self.i], quoted, glob)
        if w.raw in ("[", "[[", "]", "]]"):
            w.glob = False                                          # test 指令的方括號，不是萬用字元
        w.brace = bool(re.search(r"\{.*(,|\.\.).*\}", "".join(bare), re.S))
        return w

    def _heredoc_bodies(self):
        s, n = self.s, len(self.s)
        for delim, strip, quoted in self.pending:
            lines = []
            while True:
                if self.i >= n:
                    raise ParseError("heredoc 沒有結束（找不到 %s）" % delim)
                j = s.find("\n", self.i)
                line = s[self.i:] if j < 0 else s[self.i:j]
                nxt = n if j < 0 else j + 1
                cmp_line = line.rstrip("\r")
                if strip:
                    cmp_line = cmp_line.lstrip("\t")
                self.i = nxt
                if cmp_line == delim:
                    break
                lines.append(line)
            body = "\n".join(lines)
            self.toks.append(("hd", body, quoted))
            if not quoted:
                self._nested_subs(body)
        self.pending = []

    def run(self):
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            if c == "\\" and self.i + 1 < n and s[self.i + 1] == "\n":
                self.i += 2
                continue
            if c in " \t\r":
                self.i += 1
                continue
            if c == "\n":
                self.toks.append(("op", "\n"))
                self.i += 1
                if self.pending:
                    self._heredoc_bodies()
                continue
            if c == "#":
                j = s.find("\n", self.i)
                self.i = n if j < 0 else j
                continue
            three, two = s[self.i:self.i + 3], s[self.i:self.i + 2]
            if three == "&>>":
                self.toks.append(("redir", "&>>"))
                self.i += 3
                continue
            if two == "&>":
                self.toks.append(("redir", "&>"))
                self.i += 2
                continue
            if two in ("&&", "||", ";;", "|&"):
                self.toks.append(("op", two))
                self.i += 2
                continue
            if c in ";|&":
                self.toks.append(("op", c))
                self.i += 1
                continue
            if c in "<>" and self.i + 1 < n and s[self.i + 1] == "(":      # <( ) 與 >( )
                j = _find_matching(s, self.i + 2, "(", ")")
                self.subs.append(s[self.i + 2:j])
                self.toks.append(("word", Word([("dyn", s[self.i:j + 1])], s[self.i:j + 1])))
                self.i = j + 1
                continue
            if c in "()":
                self.toks.append(("op", c))
                self.i += 1
                continue
            if three == "<<<":
                self.toks.append(("redir", "<<<"))
                self.i += 3
                continue
            if two == "<<":
                strip = three == "<<-"
                self.i += 3 if strip else 2
                while self.i < n and s[self.i] in " \t":
                    self.i += 1
                if self.i >= n:
                    raise ParseError("heredoc 沒有結束字")
                w = self._word()
                if w.dynamic and not w.quoted:
                    raise ParseError("heredoc 的結束字看不懂")
                self.pending.append((w.text, strip, w.quoted))
                self.toks.append(("redir", "<<hd"))
                continue
            if two in (">>", ">&", "<&", ">|", "<>"):
                self.toks.append(("redir", two))
                self.i += 2
                continue
            if c in "<>":
                self.toks.append(("redir", c))
                self.i += 1
                continue
            w = self._word()
            if w.raw.isdigit() and self.i < n and s[self.i] in "<>":         # 2> 的那個 2
                continue
            self.toks.append(("word", w))
        if self.pending:
            raise ParseError("heredoc 沒有結束")
        return self


# ---------------------------------------------------------------- Bash：組成指令

_SKIP_AT_START = set(["if", "then", "elif", "else", "fi", "do", "done", "while", "until", "!", "{", "}", "esac"])
_DECL = set(["export", "declare", "local", "readonly", "typeset"])


def _build(toks, parsed, nested):
    cmds, cur = [], [None]
    i, n = 0, len(toks)
    awaiting_hd = []                     # 有 heredoc 轉向、還沒拿到內容的指令
    depth = [0]                          # 在幾層 if／for／while／{ }／( ) 裡面
    last_op = [None]
    cases = [0]                          # 在幾層 case 裡面
    pattern = [False]                    # 下一個「指令開頭」其實是 case 的比對樣式（a|b) 那一段），不是指令

    def cmd():
        if cur[0] is None:
            cur[0] = Cmd()
            cur[0].nested = nested
            cur[0].pre_op = last_op[0]
            cur[0].depth = depth[0]
        return cur[0]

    def flush(op=None):
        c = cur[0]
        if c is not None and (c.words or c.assign or c.redirs):
            c.post_op = op
            cmds.append(c)
        cur[0] = None

    while i < n:
        t = toks[i]
        kind = t[0]
        if pattern[0] and (cur[0] is None or not cur[0].words) and kind in ("word", "op"):
            if kind == "op" and t[1] == "\n":
                i += 1
                continue
            if kind == "word" and t[1].text == "esac" and not t[1].quoted:
                pattern[0] = False                                 # 最後一段後面直接接 esac
            else:
                while i < n and toks[i] != ("op", ")"):            # 樣式：一路跳到右括號
                    i += 1
                i += 1
                pattern[0] = False
                last_op[0] = ";"
                continue
        if kind == "op":
            if t[1] == "(" and i + 1 < n and toks[i + 1] == ("op", "(") and (cur[0] is None or not cur[0].words):
                parsed.complex = True                              # (( 算術指令 ))
            flush(t[1])
            if t[1] == "(":
                depth[0] += 1
            elif t[1] == ")":
                depth[0] = max(0, depth[0] - 1)
            elif t[1] == ";;" and cases[0] > 0:
                pattern[0] = True
            last_op[0] = t[1]
            i += 1
            continue
        if kind == "hd":
            parsed.heredocs.append(t[1])
            if awaiting_hd:
                awaiting_hd.pop(0).heredoc_bodies.append(t[1])
            i += 1
            continue
        if kind == "redir":
            op = t[1]
            if op == "<<hd":
                c = cmd()
                c.heredoc = True
                awaiting_hd.append(c)
                i += 1
                continue
            if i + 1 < n and toks[i + 1][0] == "word":
                cmd().redirs.append((op, toks[i + 1][1]))
                i += 2
                continue
            raise ParseError("轉向沒有目標")
        w = t[1]
        at_start = cur[0] is None or not cur[0].words
        if at_start and not w.quoted and not w.dynamic:
            txt = w.text
            if txt in _SKIP_AT_START:
                if txt in ("if", "while", "until", "{"):
                    depth[0] += 1
                elif txt in ("fi", "done", "}", "esac"):
                    depth[0] = max(0, depth[0] - 1)
                    if txt == "esac":
                        cases[0] = max(0, cases[0] - 1)
                        pattern[0] = False
                i += 1
                continue
            if txt in ("for", "select"):
                depth[0] += 1
                i += 1
                while i < n and toks[i][0] != "op":
                    i += 1
                continue
            if txt == "case":
                parsed.complex = True
                depth[0] += 1
                cases[0] += 1
                i += 1
                while i < n and not (toks[i][0] == "word" and toks[i][1].text == "in" and not toks[i][1].quoted):
                    i += 1                                         # case 字 in：中間那個字是被比對的值，不是指令
                i += 1
                pattern[0] = True
                continue
            if txt == "function":
                parsed.complex = True
                parsed.functions = True
                i += 2
                continue
            if txt == "coproc":
                parsed.complex = True
                i += 1
                continue
            if i + 2 < n and toks[i + 1] == ("op", "(") and toks[i + 2] == ("op", ")"):
                parsed.complex = True                              # 函式定義 name() { … }
                parsed.functions = True
                i += 3
                continue
            if txt == "[[":
                c = cmd()
                c.words.append(w)
                i += 1
                while i < n and not (toks[i][0] == "word" and toks[i][1].text == "]]"):
                    if toks[i][0] == "word":
                        c.words.append(toks[i][1])
                    i += 1
                i += 1
                flush()
                continue
        if at_start:
            m = _ASSIGN_RE.match(w.raw)                            # 名稱那一段沒有引號才算指定（"A=b" 整個加引號是一般的字）
            if m:
                name = m.group(1)
                cut = len(m.group(0))
                value = _slice_word(w, cut)
                cmd().assign.append((name, value))
                i += 1
                continue
        cmd().words.append(w)
        i += 1
    flush()
    return cmds


def _slice_word(w, cut):
    """把 NAME=value 這個字切掉前面 NAME=（cut 個字元都是字面值）。"""
    parts, left = [], cut
    for k, v in w.parts:
        if left <= 0:
            parts.append((k, v))
        elif k == "lit":
            if len(v) > left:
                parts.append(("lit", v[left:]))
            left -= min(left, len(v))
        else:
            parts.append((k, v))
            left = 0
    if not parts:
        parts = [("lit", "")]
    return Word(parts, w.raw[cut:], w.quoted, w.glob, w.brace)


def _resolve_words(words, env):
    out = []
    for w in words:
        r = w.resolved(env)
        if w.dynamic and not r.dynamic and r.text == "" and not r.quoted:
            continue                                               # 沒加引號的空變數：bash 會把整個字拿掉
        out.append(r)
    return out


# 這些指令會用看不到的方式改變數（執行別的檔、執行一段字串、每個指令之前偷跑一段）：出現之後，變數一律當成不知道
_POISON = set(["source", ".", "eval", "trap", "alias", "unalias", "shopt", "enable", "bind", "complete"])


def _apply_vars(cmds, seed=None, never=(), functions=False):
    """同一段指令裡指定過的變數代回去。只代「一定會執行到、而且整個值都是字面」的指定：

      PY="…"; "$PY" x.py                 代（前面是 ; 或換行、不在 if／for／( ) 裡）
      cd x && PY="…" && "$PY" x.py       代，但只在這一串 && 裡面有效
      G=git; false && G=echo; $G …       第二個指定不一定會執行 → 之後 G 當成不知道（不可以當成 echo）
      X=1 | cmd、( X=1 )、if …; then X=1  在子殼或條件裡 → 當成不知道

    seed 是一開始就知道的變數（例如 HOME）。never：用 ${X:=值} 這類寫法動過的變數，整段都當成不知道。"""
    env = dict(seed or {})
    never = set(never)
    for name in never:
        env.pop(name, None)
    poisoned = [bool(functions)]
    if poisoned[0]:
        env.clear()
    chain_local, chain_ok = [], True

    for c in cmds:
        starts_chain = c.pre_op not in ("&&", "||")
        if starts_chain:
            for name in chain_local:
                env.pop(name, None)
            chain_local, chain_ok = [], True
        elif c.pre_op == "||":
            chain_ok = False
        in_pipe = c.pre_op in ("|", "|&") or c.post_op in ("|", "|&")
        certain = c.depth == 0 and not in_pipe and c.post_op != "&" and chain_ok

        def learn(name, v):
            if poisoned[0] or name in never or v.dynamic or not certain:
                env.pop(name, None)
                return
            env[name] = v.text
            if not starts_chain:
                chain_local.append(name)

        c.assign = [(name, v.resolved(env)) for name, v in c.assign]
        c.words = _resolve_words(c.words, env)
        c.redirs = [(op, w.resolved(env)) for op, w in c.redirs]
        if not c.words:
            for name, v in c.assign:
                learn(name, v)
            continue
        prog = c.words[0].text if not c.words[0].dynamic else None
        if prog in _DECL:
            for w in c.words[1:]:
                m = _ASSIGN_RE.match(w.raw)
                if not m:
                    continue
                learn(m.group(1), _slice_word(w, len(m.group(0))).resolved(env))
        elif prog in ("unset", "read", "mapfile", "readarray", "getopts", "printf", "let"):
            for w in c.words[1:]:
                env.pop(w.text.split("=", 1)[0], None)
        elif prog in _POISON:
            poisoned[0] = True
            env.clear()
    return cmds


def parse_bash(text, env=None, _depth=0):
    """回傳 Parsed。裡面的指令（$( )、反引號、<( )）也一併放進 cmds，nested=True。
    env：一開始就知道的變數（例如 {"HOME": 家目錄}）；`~` 會當成 $HOME。"""
    if _depth > 8:
        raise ParseError("巢狀太深")
    if len(text) > 200000:
        raise ParseError("指令太長")
    parsed = Parsed()
    lx = _Lexer(text).run()
    built = _build(lx.toks, parsed, _depth > 0)
    parsed.cmds = _apply_vars(built, env, lx.assigned, parsed.functions)
    for sub in lx.subs:
        inner = parse_bash(sub, env, _depth + 1)
        for c in inner.cmds:
            c.nested = True
        parsed.cmds.extend(inner.cmds)
        parsed.complex = parsed.complex or inner.complex
        parsed.functions = parsed.functions or inner.functions
        parsed.heredocs.extend(inner.heredocs)
    return parsed


# ---------------------------------------------------------------- PowerShell

_PS_KEYWORDS = set(["if", "else", "elseif", "foreach", "for", "while", "do", "switch", "try", "catch", "finally", "function",
                    "param", "return", "begin", "process", "end", "until", "in", "throw", "trap", "filter", "break", "continue"])
_PS_BREAK = " \t\r\n;|&(){}<>,"


def _ps_tokens(s):
    toks, subs = [], []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "`" and i + 1 < n and s[i + 1] == "\n":
            i += 2
            continue
        if c in " \t\r,":
            i += 1
            continue
        if c == "\n":
            toks.append(("op", "\n"))
            i += 1
            continue
        if c == "#":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if s[i:i + 2] == "<#":
            j = s.find("#>", i + 2)
            if j < 0:
                raise ParseError("PowerShell 註解沒有結束")
            i = j + 2
            continue
        if s[i:i + 2] in ("@'", '@"') and i + 2 < n and s[i + 2] in "\r\n":       # here-string
            q = s[i + 1]
            end = "\n" + q + "@"
            j = s.find(end, i + 2)
            if j < 0:
                raise ParseError("PowerShell here-string 沒有結束")
            body = s[i + 3:j]
            toks.append(("word", Word([("lit" if q == "'" else "dyn", body)], s[i:j + len(end)], True)))
            i = j + len(end)
            continue
        if s[i:i + 2] in ("&&", "||"):
            toks.append(("op", s[i:i + 2]))
            i += 2
            continue
        if c in ";|":
            toks.append(("op", c))
            i += 1
            continue
        if c == "&":
            toks.append(("call", "&"))
            i += 1
            continue
        if c in "(){}":
            toks.append(("op", c))
            i += 1
            continue
        if c in "<>":
            j = i
            while j < n and s[j] in "<>&12*":
                j += 1
            op = s[i:j]
            toks.append(("dup" if re.search(r"&\d$", op) else "redir", op))      # 2>&1 這種沒有目標檔
            i = j
            continue
        if s[i:i + 2] in ("$(", "@(", "@{"):
            toks.append(("op", "("))
            i += 2
            continue
        # 一個字
        start, parts, quoted = i, [], False

        def add(t):
            if parts and parts[-1][0] == "lit":
                parts[-1] = ("lit", parts[-1][1] + t)
            else:
                parts.append(("lit", t))

        while i < n:
            c = s[i]
            if c in _PS_BREAK:
                break
            if c == "`":
                if i + 1 < n:
                    if s[i + 1] != "\n":
                        add(s[i + 1])
                    i += 2
                else:
                    i += 1
                continue
            if c == "'":
                j = i + 1
                buf = []
                while True:
                    if j >= n:
                        raise ParseError("PowerShell 單引號沒有結束")
                    if s[j] == "'":
                        if j + 1 < n and s[j + 1] == "'":
                            buf.append("'")
                            j += 2
                            continue
                        break
                    buf.append(s[j])
                    j += 1
                add("".join(buf))
                i = j + 1
                quoted = True
                continue
            if c == '"':
                j = i + 1
                quoted = True
                while True:
                    if j >= n:
                        raise ParseError("PowerShell 雙引號沒有結束")
                    d = s[j]
                    if d == "`" and j + 1 < n:
                        add(s[j + 1])
                        j += 2
                        continue
                    if d == '"':
                        if j + 1 < n and s[j + 1] == '"':
                            add('"')
                            j += 2
                            continue
                        break
                    if d == "$" and j + 1 < n and s[j + 1] == "(":
                        k = _find_matching(s, j + 2, "(", ")")
                        subs.append(s[j + 2:k])
                        parts.append(("dyn", s[j:k + 1]))
                        j = k + 1
                        continue
                    if d == "$" and j + 1 < n and re.match(r"[A-Za-z_{]", s[j + 1]):
                        m = re.match(r"\$(\{[^}]*\}|[A-Za-z_][A-Za-z0-9_:]*)", s[j:])
                        parts.append(("dyn", m.group(0)))
                        j += len(m.group(0))
                        continue
                    add(d)
                    j += 1
                i = j + 1
                continue
            if c == "$":
                m = re.match(r"\$(\{[^}]*\}|[A-Za-z_?][A-Za-z0-9_:.]*)?", s[i:])
                parts.append(("dyn", m.group(0)))
                i += max(1, len(m.group(0)))
                continue
            add(c)
            i += 1
        if not parts:
            parts = [("lit", "")]
        raw = s[start:i]
        if raw in ("1", "2", "3", "4", "5", "6", "*") and i < n and s[i] == ">":      # 2> 的那個 2
            continue
        if not quoted and re.match(r"^@[A-Za-z_]", raw):            # @參數表：把一包參數攤開傳進去，內容看不到
            parts = [("dyn", raw)]
        w = Word(parts, raw, quoted)
        txt = w.text
        # PowerShell 的萬用字元是指令自己展開的，加了引號也一樣會展開（Remove-Item '.cl*'）
        w.glob = "*" in txt or "?" in txt or (not txt.startswith("[") and bool(re.search(r"\[[^\]]+\]", txt)))
        toks.append(("word", w))
    return toks, subs


def parse_powershell(text, _depth=0):
    if _depth > 8:
        raise ParseError("巢狀太深")
    if len(text) > 200000:
        raise ParseError("指令太長")
    parsed = Parsed()
    toks, subs = _ps_tokens(text)
    cmds, cur = [], [None]
    last_op = [None]

    def flush(op=None):
        c = cur[0]
        if c is not None and (c.words or c.redirs):
            c.post_op = op
            cmds.append(c)
        cur[0] = None

    i, n = 0, len(toks)
    call_next = False
    while i < n:
        t = toks[i]
        if t[0] == "op":
            flush(t[1])
            last_op[0] = t[1]
            call_next = False
            i += 1
            continue
        if t[0] == "call":
            if cur[0] is not None and cur[0].words:
                flush("&")                                         # 結尾的 & ＝丟到背景
                last_op[0] = "&"
            else:
                call_next = True                                   # & 程式 參數：後面那個字會被當成程式執行
            i += 1
            continue
        if t[0] == "dup":
            i += 1
            continue
        if t[0] == "redir":
            if i + 1 < n and toks[i + 1][0] == "word":
                if cur[0] is None:
                    cur[0] = Cmd()
                    cur[0].pre_op = last_op[0]
                cur[0].redirs.append((t[1], toks[i + 1][1]))
                i += 2
            else:
                i += 1
            continue
        w = t[1]
        at_start = cur[0] is None or not cur[0].words
        if at_start and not w.quoted:
            low = w.text.lower()
            if low in _PS_KEYWORDS:
                i += 1
                continue
            if low == ".":                                         # dot-sourcing：. script.ps1
                call_next = True
                i += 1
                continue
            if w.text.startswith("$") and i + 1 < n and toks[i + 1][0] == "word" and toks[i + 1][1].text in ("=", "+="):
                i += 2                                             # $x = <指令>
                continue
            if "=" in w.text and w.text.startswith("$"):
                i += 1
                continue
        if cur[0] is None:
            cur[0] = Cmd()
            cur[0].nested = _depth > 0
            cur[0].call = call_next
            cur[0].pre_op = last_op[0]
            call_next = False
        cur[0].words.append(w)
        i += 1
    flush()
    parsed.cmds = cmds
    for sub in subs:
        inner = parse_powershell(sub, _depth + 1)
        for c in inner.cmds:
            c.nested = True
        parsed.cmds.extend(inner.cmds)
    return parsed


# ---------------------------------------------------------------- cmd.exe 的 /c 字串

def parse_cmd_string(text):
    """cmd /c "…" 裡面那一段：用 & && || | ( ) 切開，雙引號是引號，> >> < 是轉向。
    有 % ! ^ 的字當成執行時才知道（變數、延遲展開、跳脫）。"""
    parsed = Parsed()
    segs, buf, q = [], [], False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            q = not q
            buf.append(c)
            i += 1
            continue
        if not q and text[i:i + 2] in ("&&", "||"):
            segs.append("".join(buf))
            buf = []
            i += 2
            continue
        if not q and c in "&|()\n":
            if c == "&" and buf and buf[-1] == ">":                # 2>&1
                buf.append(c)
                i += 1
                continue
            segs.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    if q:
        raise ParseError("cmd 字串的引號沒有結束")
    segs.append("".join(buf))
    for seg in segs:
        toks, wbuf, quoted, inq = [], [], False, False             # toks：('w', 字, 有引號) | ('r', 運算子)
        j, m = 0, len(seg)
        while j <= m:
            ch = seg[j] if j < m else " "
            if ch == '"':
                inq = not inq
                quoted = True
                j += 1
                continue
            if not inq and ch in " \t\r,;":                        # cmd 的內建指令把逗號、分號也當成分隔
                if wbuf or quoted:
                    toks.append(("w", "".join(wbuf), quoted))
                wbuf, quoted = [], False
                j += 1
                continue
            if not inq and ch in "<>":
                if len(wbuf) == 1 and wbuf[0].isdigit() and not quoted:
                    wbuf = []                                      # 2> 的那個 2
                if wbuf or quoted:
                    toks.append(("w", "".join(wbuf), quoted))
                wbuf, quoted = [], False
                op = ">>" if seg[j:j + 2] == ">>" else ch
                j += len(op)
                if seg[j:j + 1] == "&":                            # 2>&1：沒有目標檔
                    j += 1
                    while j < m and seg[j].isdigit():
                        j += 1
                    continue
                toks.append(("r", op))
                continue
            wbuf.append(ch)
            j += 1
        c = Cmd()
        c.nested = True
        pending = None
        for t in toks:
            if t[0] == "r":
                pending = t[1]
                continue
            txt = t[1]
            if not c.words and not pending and txt.startswith("@"):
                txt = txt.lstrip("@")                              # @指令：只是不回顯
                if not txt:
                    continue
            dyn = "%" in txt or "!" in txt or "^" in txt
            w = Word([("dyn" if dyn else "lit", txt)], txt, t[2])
            w.glob = "*" in txt or "?" in txt
            if pending:
                c.redirs.append((pending, w))
                pending = None
            else:
                c.words.append(w)
        if c.words or c.redirs:
            parsed.cmds.append(c)
    return parsed
