# -*- coding: utf-8 -*-
"""
iw_state.py — 自動駕駛的狀態、放行通行證、對話紀錄核對。

東西放在共用 .git 目錄底下的 iw-autopilot/（不進版控、每個 worktree 共用、Claude Code 內建就把 .git 當受保護路徑）：
  state.json          現在是哪個階段、什麼狀態、通行證、審查紀錄、看過的模型……
  log.jsonl           每一次檢查的紀錄（只增不改，給技術報告與除錯用）
  specs/<階段>.md      你啟動時貼的規格原文（hook 存的，審查代理看這一份）
  approvals.jsonl     放行紀錄（事後偵測用）

這些檔只有 hook 與我們自己的腳本會寫；守門會擋下模型用改檔工具或指令寫這個資料夾。
"""
import io
import json
import os
import re
import time

import iw_common as C

STATUSES = ("pending", "running", "paused", "stopped", "awaiting_approval", "approved", "merged", "done")


def default_state():
    return {"version": 1, "active": False, "stage": None, "status": None}


def state_file(sd):
    return os.path.join(sd, "state.json")


def load(sd):
    """讀狀態。檔案不存在＝沒有在自動駕駛。檔案壞掉＝丟例外（呼叫的人要當成擋下，不可以當成沒事）。"""
    p = state_file(sd)
    if not os.path.exists(p):
        return default_state()
    with io.open(p, encoding="utf-8") as fh:
        st = json.load(fh)
    if not isinstance(st, dict) or "active" not in st:
        raise ValueError("state.json 的內容不對")
    return st


def save(sd, st):
    os.makedirs(sd, exist_ok=True)
    p = state_file(sd)
    tmp = p + ".tmp.%d" % os.getpid()
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, p)


class Lock(object):
    """多個 hook 會同時跑（官方文件：符合的 hook 平行執行）。改狀態前先拿鎖，免得互相蓋掉。
    用「建資料夾」當鎖：建得起來就是拿到；30 秒以上的舊鎖視為死的。拿不到就丟例外。"""

    def __init__(self, sd, wait=8.0):
        self.path = os.path.join(sd, "lock")
        self.sd = sd
        self.wait = wait

    def __enter__(self):
        os.makedirs(self.sd, exist_ok=True)
        t0 = time.time()
        while True:
            try:
                os.mkdir(self.path)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 30:
                        os.rmdir(self.path)
                        continue
                except OSError:
                    pass
                if time.time() - t0 > self.wait:
                    raise RuntimeError("拿不到狀態檔的鎖")
                time.sleep(0.05)

    def __exit__(self, *a):
        try:
            os.rmdir(self.path)
        except OSError:
            pass
        return False


def update(sd, fn):
    """拿鎖 → 讀 → fn(st) 改 → 存。回傳改完的狀態。"""
    with Lock(sd):
        st = load(sd)
        fn(st)
        save(sd, st)
        return st


def log(sd, rec):
    try:
        os.makedirs(sd, exist_ok=True)
        rec = dict(rec)
        rec.setdefault("t", C.iso())
        with io.open(os.path.join(sd, "log.jsonl"), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:                                              # 紀錄寫不進去不可以讓守門壞掉
        pass


def append_approval(sd, rec):
    os.makedirs(sd, exist_ok=True)
    rec = dict(rec)
    rec.setdefault("t", C.iso())
    with io.open(os.path.join(sd, "approvals.jsonl"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def approvals(sd):
    p = os.path.join(sd, "approvals.jsonl")
    out = []
    if os.path.exists(p):
        with io.open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    return out


def save_spec(sd, stage, text):
    d = os.path.join(sd, "specs")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, stage + ".md")
    with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return p


# ---------------------------------------------------------------- 對話紀錄

def _tail_lines(path, max_bytes):
    size = os.path.getsize(path)
    with io.open(path, "rb") as fh:
        fh.seek(max(0, size - max_bytes))
        raw = fh.read().decode("utf-8", errors="replace")
    lines = raw.split("\n")
    if size > max_bytes:
        lines = lines[1:]                                          # 第一行可能被切到一半
    return lines


def norm_model(m):
    """模型名稱後面的 [1m] 只是上下文長度的標記，拿掉再比（有沒有它都是同一個模型）。"""
    return re.sub(r"\[[0-9a-z]+\]$", "", str(m or "").strip())


def last_model(transcript_path, max_bytes=600000):
    """對話紀錄裡最新一則回覆的模型。系統自己產生的訊息（<synthetic>）不算。讀不到回 None。
    官方文件：這個檔是非同步寫的，可能慢一步——所以這裡看到的可能是上一則。"""
    try:
        if not transcript_path or not os.path.exists(transcript_path):
            return None
        found = None
        for line in _tail_lines(transcript_path, max_bytes):
            if '"assistant"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("type") != "assistant" or o.get("isSidechain"):
                continue
            m = (o.get("message") or {}).get("model")
            if m and m != "<synthetic>":
                found = norm_model(m)
        return found
    except Exception:
        return None


def models_in(transcript_path, max_bytes=20000000):
    """一份對話紀錄（例如審查代理的）裡出現過的所有模型。"""
    seen = []
    try:
        for line in _tail_lines(transcript_path, max_bytes):
            if '"assistant"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("type") != "assistant":
                continue
            m = (o.get("message") or {}).get("model")
            if m and m != "<synthetic>":
                m = norm_model(m)
                if m not in seen:
                    seen.append(m)
    except Exception:
        return None
    return seen


def _user_text(o):
    c = (o.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return None


def human_prompt(transcript_path, prompt_id, max_bytes=30000000):
    """在對話紀錄裡找這個 prompt_id 的那一則使用者訊息。
    回傳 (文字, 是不是人打的)；找不到回 (None, None)。「人打的」＝ origin.kind 是 human。
    這個欄位不在官方文件裡（2026-10-01 實測 Claude Code 2.1.284 有）；改版拿掉的話會回 False——寧可不認，不可以亂認。"""
    try:
        if not transcript_path or not prompt_id or not os.path.exists(transcript_path):
            return None, None
        hit = (None, None)
        for line in _tail_lines(transcript_path, max_bytes):
            if prompt_id not in line or '"user"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("type") != "user" or o.get("promptId") != prompt_id or o.get("isMeta"):
                continue
            text = _user_text(o)
            if text is None:
                continue
            origin = o.get("origin") or {}
            is_human = isinstance(origin, dict) and origin.get("kind") == "human"
            if o.get("turnOrigin") not in (None, "human"):
                is_human = False
            hit = (text, bool(is_human))
            if is_human:
                return hit
        return hit
    except Exception:
        return None, None


# ---------------------------------------------------------------- 指令詞

_ZERO_WIDTH = re.compile("[" + chr(0x200B) + chr(0x200C) + chr(0x200D) + chr(0xFEFF) + "]")


def clean_prompt(text):
    """去掉頭尾空白、零寬字元；全形空白當成一般空白；全形冒號當成半形。只用在比對指令詞，不改你貼的內容。"""
    t = _ZERO_WIDTH.sub("", text or "").replace(chr(0x3000), " ").replace("\r\n", "\n").replace("\r", "\n")
    return t.strip()


def parse_command(prompt):
    """看一則訊息是不是你的指令詞。回傳 dict(kind=…, stage=…, rest=…) 或 None。

    整則訊息完全相符：放行 <階段>、繼續 <階段>、放行模型、結束自動駕駛
    第一行相符：      自動駕駛：<階段>（下面是規格）、修改 <階段>：＿＿
    為什麼不能用「訊息裡有這幾個字就算」：規格本身就寫著「放行 P1」；代理回報、背景工作通知也會經過同一個 hook（外面包著標籤）。"""
    t = clean_prompt(prompt)
    if not t or t.startswith("<"):
        return None
    first, _, rest = t.partition("\n")
    first = first.strip()
    colon = "[:" + chr(0xFF1A) + "]"
    m = re.match(r"^自動駕駛\s*" + colon + r"\s*(\S+)$", first)
    if m:
        return {"kind": "start", "stage": m.group(1), "rest": rest.strip("\n")}
    m = re.match(r"^修改\s+(\S+?)\s*" + colon + r"(.*)$", first)
    if m:
        return {"kind": "revise", "stage": m.group(1), "rest": (m.group(2) + ("\n" + rest if rest else "")).strip()}
    if "\n" in t:
        return None
    m = re.match(r"^放行\s+(\S+)$", t)
    if m and m.group(1) != "模型":
        return {"kind": "approve", "stage": m.group(1), "rest": ""}
    if t == "放行模型":
        return {"kind": "approve_model", "stage": None, "rest": ""}
    m = re.match(r"^繼續\s+(\S+)$", t)
    if m:
        return {"kind": "resume", "stage": m.group(1), "rest": ""}
    if t == "結束自動駕駛":
        return {"kind": "end", "stage": None, "rest": ""}
    return None


# 機器包起來的訊息（代理回報、背景工作通知…）也會經過同一個 hook。它們裡面出現指令字不提示——
# 不然審查代理每回報一次「請輸入 放行 X」就跳一行。這張表只影響「要不要提示」，不影響認不認指令詞（那邊是以 < 開頭一律不認）。
_MACHINE_WRAPPERS = ("agent-message", "task-notification", "system-reminder", "teammate-message", "local-command", "command-name",
                     "command-message", "command-args", "bash-input", "bash-stdout", "bash-stderr", "ci-monitor-event")
_TAG_ONLY_LINE = re.compile(r"^</?[A-Za-z][\w-]*(\s[^<>]*)?>$")
_PASTE_TAG_LINE = re.compile(r"^</?pasted[_-]content\b[^<>]*>$", re.I)
_SHORT = 60                                                        # 「一句話」的長度上限：超過就當成文件，不用「有提到就提示」


def strip_paste_wrapper(text):
    """桌面 App 會把貼上的長文字包成 <pasted_content …>…</pasted_content …>。存規格時把那兩行標籤拿掉，內容一個字不動。"""
    lines = [x for x in (text or "").split("\n") if not _PASTE_TAG_LINE.match(x.strip())]
    return "\n".join(lines).strip("\n")


def near_miss(prompt, stage=None):
    """parse_command 不認的訊息裡，有沒有「長得像指令詞」的東西。有就回 dict(kind=…, where=…)，沒有回 None。

    只用來給 David 一句看得到的提示（2026-10-02 的事故：整段貼上時指令詞在貼上的區塊裡，hook 靜悄悄沒反應）。
    這個函式不啟動、不放行、不改任何狀態；回傳的東西也不可以拿去當指令用。
    where：pasted（在貼上的區塊裡）／line（單獨一行是指令詞，但不在第一行或前後還有別的行）／shape（像指令詞，但寫法不對）。
    stage：現在紀錄裡的階段（有的話，「繼續 <階段> 吧」這種也提示；沒有的話「繼續」「修改」開頭的句子太常見，不提示）。"""
    t = clean_prompt(prompt)
    if not t or parse_command(t) is not None:
        return None
    wrapped = t.startswith("<")
    if wrapped:
        m = re.match(r"^<([A-Za-z][\w-]*)", t)
        if not m or m.group(1).lower().startswith(_MACHINE_WRAPPERS):
            return None
    lines = [x.strip() for x in t.split("\n")]
    for line in lines:
        if not line or _TAG_ONLY_LINE.match(line):
            continue
        cmd = parse_command(line)
        if cmd and (cmd.get("stage") is None or C.stage_ok(cmd["stage"])):
            return {"kind": cmd["kind"], "where": "pasted" if wrapped else "line"}
    if wrapped:
        return None
    first = lines[0]
    colon = "[:" + chr(0xFF1A) + "]"
    shapes = [("start", r"^自動駕駛\s*" + colon + r"?\s*[A-Za-z0-9]"), ("approve_model", r"^放行\s*模型"),
              ("approve", r"^放行(\s*[A-Za-z0-9]|$)"), ("end", r"^結束自動駕駛")]
    if stage:
        end = r"(?![A-Za-z0-9._-])"                                 # 階段名稱要完整（P1 不可以對到 P10）；後面接中文字沒關係
        shapes += [("resume", r"^繼續\s*" + re.escape(stage) + end), ("revise", r"^修改\s*" + re.escape(stage) + end)]
    for kind, pat in shapes:
        if re.search(pat, first):
            return {"kind": kind, "where": "shape"}
    if len(lines) == 1 and len(first) <= _SHORT:                    # 一句話裡提到了（「請幫我 放行 P1」「我想結束自動駕駛」）
        for kind, pat in (("approve_model", r"放行\s*模型"), ("approve", r"放行\s*[A-Za-z0-9]"),
                          ("start", r"自動駕駛\s*" + colon + r"\s*[A-Za-z0-9]"), ("end", r"結束自動駕駛")):
            if re.search(pat, first):
                return {"kind": kind, "where": "shape"}
    return None


def approval_text(stage):
    return "放行 " + stage


# ---------------------------------------------------------------- 通行證

def credential_problem(st, cfg, purpose, now=None, check_transcript=True):
    """通行證現在能不能用在 purpose（'merge' 或 'docs'）。可以回 None；不行回一句白話原因。"""
    cred = st.get("credential")
    if not cred:
        return "沒有通行證（要 David 輸入「放行 <階段>」才有）"
    if cred.get("stage") != st.get("stage"):
        return "通行證是別的階段的"
    now = now or C.now()
    exp = C.parse_iso(cred.get("expires_at"))
    if exp is None or now > exp:
        return "通行證過期了（%d 小時內有效）" % int(cfg.get("credentialHours", 24))
    if cred.get("revoked"):
        return "通行證已經作廢（%s）" % cred.get("revoked")
    if purpose == "merge":
        if cred.get("merge"):
            return "這張通行證的合併已經用過了"
    elif purpose == "docs":
        if not cred.get("merge"):
            return "還沒有合併，不能先推文件"
        if cred.get("docs"):
            return "這張通行證的文件那一筆已經用過了"
        at = C.parse_iso(cred.get("docs_window_from") or cred["merge"].get("at"))
        if at is None or (now - at).total_seconds() > 60 * float(cfg.get("docsFollowupMinutes", 60)):
            return "文件那一筆要在合併之後 %d 分鐘內推（要重推請 David 再輸入一次放行）" % int(cfg.get("docsFollowupMinutes", 60))
    else:
        return "不認得的用途"
    if check_transcript:
        text, human = human_prompt(cred.get("transcript_path"), cred.get("prompt_id"))
        if text is None:
            return "對話紀錄裡找不到那一則放行訊息"
        if not human:
            return "對話紀錄顯示那一則放行訊息不是人打的"
        if clean_prompt(text) != approval_text(cred.get("stage")):
            return "對話紀錄裡那一則訊息的內容不是「%s」" % approval_text(cred.get("stage"))
    return None


def issue_credential(st, cfg, inp, now=None):
    """UserPromptSubmit hook 開通行證。只有這裡會開。"""
    now = now or C.now()
    cand = st.get("candidate") or {}
    import datetime
    again = st.get("status") == "merged"                           # 合併已經推上去、只差文件那一筆：再放行一次＝重開文件的時間窗
    old = st.get("credential") or {}
    st["credential"] = {
        "stage": st.get("stage"),
        "candidate": cand.get("sha"),
        "tag_sha": cand.get("tag_sha"),
        "issued_at": C.iso(now),
        "expires_at": C.iso(now + datetime.timedelta(hours=float(cfg.get("credentialHours", 24)))),
        "prompt_id": inp.get("prompt_id"),
        "transcript_path": inp.get("transcript_path"),
        "session_id": inp.get("session_id"),
        "merge": old.get("merge") if again else None,
        "docs": None,
        "docs_window_from": C.iso(now) if again else None,
    }
    return st["credential"]
