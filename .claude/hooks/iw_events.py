# -*- coding: utf-8 -*-
"""
iw_events.py — 各個 hook 事件要做的事（讀狀態 → 判斷或紀錄 → 寫狀態）。

  pretool        每個動作執行前：叫守門（iw_guard）判斷，擋就以結束碼 2 結束
  prompt         David 送出訊息：認指令詞（自動駕駛：／放行／修改／繼續／放行模型／結束自動駕駛）；只有這裡會開通行證
  permission     要跳「等你按批准」的視窗：自動駕駛期間一律代為拒絕（不停在那裡等）
  posttool       審查代理跑完：記下它實際用的模型
  subagentstop   審查代理結束：記下它的結論（結論是 hook 記的，不是 Claude 轉述的）
  stop           這一輪結束：自動駕駛進行中卻沒寄信就停了 → 寄一封「原因不明」的後援信
  stopfailure    因為 API 錯誤或用量上限結束：標成暫停、寄信（信裡寫額度何時恢復）
  notification   卡在權限視窗之類：寄信
  configchange   設定檔在工作階段中被改：不讓它套用
  modelswitch    模型被換：記下來；不是規定的模型就暫停、寄信
  sessionstart   開工作階段：檢查保護檔有沒有被動過；壓縮對話後提醒目前的狀態
  sessionend     工作階段結束：自動駕駛進行中就寄信
"""
import io
import json
import os
import re
import subprocess
import sys

import iw_common as C
import iw_guard as G
import iw_notify as N
import iw_state as ST

LIVE_PARTS = [".claude/settings.json", ".claude/hooks", ".claude/agents", ".claude/skills", ".claude/autopilot"]
PREPUSH_MARK = "iw_prepush.py"


class Env(object):
    """這一次 hook 執行的環境。測試可以換掉 runner（寄信）、git（查詢）、now。"""

    def __init__(self, root=None, runner=None, git=None, now=None, home=None):
        self.root = root or C.ROOT
        self.main_root = C.main_root(self.root)
        self.sd = os.path.join(C.common_dir(self.root), C.STATE_DIR_NAME)
        self.cfg = C.config(os.path.join(self.root, ".claude", "autopilot", "config.json"))
        self.allow = C.allowlist(os.path.join(self.root, ".claude", "autopilot", "allowlist.json"))
        self.runner = runner
        self.git = git or G.GitInfo(self.main_root)
        self.now = now
        self.home = home

    def t(self):
        return self.now or C.now()


def mine(st, inp):
    return bool(st.get("active")) and st.get("session_id") == inp.get("session_id")


def send_fallback(env, text, key):
    """寄一封後援信。平常另開一個程序去寄（hook 不可以卡住：逾時會變成放行）；測試裡直接呼叫。"""
    if env.runner is not None:
        return N.fallback(env.main_root, env.sd, env.cfg, text, runner=env.runner, min_gap_key=key)
    try:
        args = [sys.executable, "-X", "utf8", os.path.join(C.HOOKS_DIR, "iw_hook.py"), "send-fallback", key, text]
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
        if C.IS_WINDOWS:
            kw["creationflags"] = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        subprocess.Popen(args, **kw)
    except Exception as e:                                          # noqa: B902
        ST.log(env.sd, {"event": "fallback-spawn-failed", "error": repr(e)})
    return None


# ---------------------------------------------------------------- pretool

def pretool(inp, env):
    st = ST.load(env.sd)
    is_mine = mine(st, inp)
    tool = inp.get("tool_name") or ""
    if is_mine and tool == "SubagentHandback" and inp.get("agent_type") == env.cfg["reviewer"]["agentType"]:
        record_review_text(env, str((inp.get("tool_input") or {}).get("message") or ""), "handback")
        st = ST.load(env.sd)
    if st.get("cmd_checks"):
        verify_commands(env)
        st = ST.load(env.sd)
        is_mine = mine(st, inp)
        if any(c.get("transcript_path") == inp.get("transcript_path") for c in (st.get("cmd_checks") or [])):
            C.err("正在核對剛才那一句指令詞是不是 David 親手輸入的（對話紀錄還沒寫進去）。請隔幾秒再試一次。")
            return 2
    cred = st.get("credential") or {}
    if (cred.get("merge_attempt") and not cred.get("merge")) or (cred.get("docs_attempt") and not cred.get("docs")):
        reconcile_pushes(env)
        st = ST.load(env.sd)
    if is_mine:
        inp["_observed_model"] = ST.last_model(inp.get("transcript_path"))
    ctx = G.Ctx(env.main_root, st, env.cfg, env.allow, env.git, inp, home=env.home, now=env.t())
    block, effects = G.decide(inp, ctx)
    if effects:
        apply_effects(env, effects, inp)
    if block is not None or is_mine:
        ti = inp.get("tool_input") if isinstance(inp.get("tool_input"), dict) else {}
        ST.log(env.sd, {"event": "pretool", "tool": tool, "what": str(ti.get("command") or ti.get("file_path") or ti.get("subagent_type") or "")[:300],
                        "blocked": bool(block), "code": block.code if block else None, "reason": block.reason[:200] if block else None,
                        "agent": inp.get("agent_type")})
    if block is not None:
        C.err(block.reason)
        return 2
    return 0


def verify_commands(env):
    """hook 收到指令詞的當下分不出是不是人打的（官方文件：沒有這個欄位）。所以每一句都記下來，
    等下一個動作開始前回頭看對話紀錄：那一則訊息要是人打的、而且真的是那一句。
    不是的話＝有東西冒充 David：自動駕駛暫停、通行證作廢、寄信。回傳 True 表示還有沒核對完的（對話紀錄還沒寫進去）。"""
    st = ST.load(env.sd)
    pending, forged, still = list(st.get("cmd_checks") or []), [], []
    for c in pending:
        text, human = ST.human_prompt(c.get("transcript_path"), c.get("prompt_id"))
        if text is None:
            at = C.parse_iso(c.get("at"))
            if at is not None and (env.t() - at).total_seconds() > 120:
                forged.append(dict(c, why="對話紀錄裡找不到這一則訊息"))
            else:
                still.append(c)
            continue
        again = ST.parse_command(text)
        if human and again and again.get("kind") == c.get("kind") and again.get("stage") == c.get("stage"):
            if c.get("kind") == "approve":
                ST.append_approval(env.sd, {"stage": c.get("stage"), "candidate": c.get("candidate"), "tag_sha": c.get("tag_sha"),
                                            "prompt_id": c.get("prompt_id")})
            continue
        forged.append(dict(c, why="對話紀錄顯示這一則訊息不是人打的" if not human else "對話紀錄裡那一則訊息的內容對不上"))
    now = C.iso(env.t())

    def fn(s):
        s["cmd_checks"] = still
        if forged:
            if any(f.get("kind") == "end" for f in forged):
                s["active"] = True                                  # 冒充的「結束自動駕駛」：限制不可以因此解除
            s["status"] = "paused" if s.get("active") else s.get("status")
            s["pause"] = {"reason": "forged", "detail": "收到不是 David 親手輸入的指令詞（%s）" % "、".join(f.get("kind") for f in forged), "at": now}
            s["stop_required"] = {"code": 1, "reason": "收到不是 David 親手輸入的指令詞", "at": now}
            s["model_approved"] = []
            if s.get("credential"):
                s["credential"]["revoked"] = "收到不是 David 親手輸入的指令詞"
    ST.update(env.sd, fn)
    for f in forged:
        ST.log(env.sd, {"event": "forged-command", "kind": f.get("kind"), "why": f.get("why"), "prompt_id": f.get("prompt_id")})
    if forged:
        send_fallback(env, "收到一則不是你親手輸入的指令詞（%s），自動駕駛已暫停、通行證作廢。請看 Claude Code；確認沒事之後輸入「繼續 %s」。"
                      % ("、".join(str(f.get("kind")) for f in forged), st.get("stage")), "forged")
    return bool(still)


def apply_effects(env, effects, inp):
    mails = []
    now = C.iso(env.t())

    def fn(st):
        for e in effects:
            k = e[0]
            if k == "heartbeat":
                st["pretool_count"] = int(st.get("pretool_count") or 0) + 1
                st["last_pretool_at"] = now
                if e[1]:
                    d = st.setdefault("effort_seen", {})
                    d[e[1]] = int(d.get(e[1], 0)) + 1
                if e[2]:
                    d = st.setdefault("models_seen", {})
                    d[e[2]] = int(d.get(e[2], 0)) + 1
            elif k == "activate":
                st["status"] = "running"
                st["activated_at"] = now
            elif k == "deactivate":
                st["active"] = False
                st["status"] = None
                st["last_end"] = {"reason": e[1], "at": now}
            elif k == "model_violation":
                if not st.get("model_violation"):
                    st["model_violation"] = {"model": e[1], "at": now}
                    st["status"] = "paused"
                    if (st.get("pause") or {}).get("reason") != "forged":      # 「有東西冒充 David」比較嚴重，原因留著
                        st["pause"] = {"reason": "model", "detail": "模型被換成 %s" % e[1], "at": now}
                    d = st.setdefault("models_seen", {})
                    d[e[1]] = int(d.get(e[1], 0)) + 1
                    mails.append(("模型被換成 %s，已暫停。要用 %s 繼續請輸入「放行模型」；或等額度恢復後輸入「繼續 %s」。"
                                  % (e[1], e[1], st.get("stage")), "model"))
            elif k == "pause":
                if st.get("status") != "paused":
                    st["status"] = "paused"
                    st["pause"] = {"reason": e[1], "detail": e[2], "at": now}
                    mails.append(("%s，已暫停。處理好之後請輸入「繼續 %s」。" % (e[2], st.get("stage")), e[1]))
            elif k == "stop_required":
                if not st.get("stop_required"):
                    st["stop_required"] = {"code": e[1], "reason": e[2], "at": now}
            elif k == "tier2":
                t = st.setdefault("tier2", {}).setdefault("touched", {})
                cur = t.get(e[1])
                if not cur or cur.get("ack"):
                    t[e[1]] = {"at": now, "ack": False}
            elif k == "review_begin":
                st["review_pending"] = dict(e[1], at=now, tool_use_id=inp.get("tool_use_id"), epoch=st.get("epoch"))
    ST.update(env.sd, fn)
    for text, key in mails:
        send_fallback(env, text, key)


def reconcile_pushes(env):
    """推送前的檢查記下「試著推了什麼」；這裡看它有沒有真的上去（本機的 origin/main 有沒有包含它）。"""
    ref = "refs/remotes/%s/%s" % (env.cfg["remote"], env.cfg["mainBranch"])

    def landed(sha):
        rc, _ = C.git(["merge-base", "--is-ancestor", sha, ref], env.main_root)
        return rc == 0

    st = ST.load(env.sd)
    cred = st.get("credential") or {}
    ma, da = cred.get("merge_attempt"), cred.get("docs_attempt")
    got_merge = bool(ma and not cred.get("merge") and landed(ma["sha"]))
    got_docs = bool(da and not cred.get("docs") and landed(da["sha"]))
    if not got_merge and not got_docs:
        return
    now = C.iso(env.t())

    def fn(s):
        c = s.get("credential") or {}
        if got_merge:
            c["merge"] = {"sha": ma["sha"], "at": now}
            c["docs_window_from"] = now
            s["status"] = "merged"
            s["tripwire_baseline"] = ma["sha"]
        if got_docs:
            c["docs"] = {"sha": da["sha"], "at": now}
            s["status"] = "done"
            s["active"] = False
            s["tripwire_baseline"] = da["sha"]
        s["credential"] = c
    ST.update(env.sd, fn)
    if got_merge:
        ST.append_approval(env.sd, {"stage": cred.get("stage"), "merge": ma["sha"]})
    if got_docs:
        ST.append_approval(env.sd, {"stage": cred.get("stage"), "docs": da["sha"]})


# ---------------------------------------------------------------- 審查代理的結論

def parse_verdict(text):
    m = re.findall(r"^\s*VERDICT:\s*(APPROVE|REVISE|ESCALATE)\s*$", text or "", re.M)
    return m[-1] if m else None


def record_review_text(env, text, source):
    """把審查代理交回來的報告先存起來（結論在審查代理結束時才定案）。"""
    def fn(st):
        p = st.get("review_pending")
        if p is not None and text.strip():
            if source == "handback" or not p.get("report"):
                p["report"] = text
                p["report_source"] = source
    ST.update(env.sd, fn)


def finalize_review(env, models=None, resolved=None):
    """審查代理結束：把這一輪的結論寫進紀錄。回傳那一筆（沒有進行中的審查就回 None）。"""
    need = env.cfg["requiredModel"]
    out = {}

    def fn(st):
        p = st.get("review_pending")
        if p is None:
            return
        report = p.get("report") or ""
        verdict = parse_verdict(report) or "INVALID"
        ms = [ST.norm_model(m) for m in (models or []) if m]
        if resolved:
            r = ST.norm_model(resolved)
            if r not in ms:
                ms.append(r)
        model_ok = all(m == need for m in ms) if ms else None
        said = re.search(r"審查的 commit\s*[:：]\s*([0-9a-f]{7,40}|尚無|none)", report)
        want = p.get("commit") or "none"
        commit_ok = True
        if said and want not in ("none", "尚無"):
            commit_ok = want.startswith(said.group(1)) or said.group(1).startswith(want)
        if not commit_ok:
            verdict = "INVALID"
        if model_ok is False:
            verdict = "INVALID"
            if not st.get("stop_required"):
                st["stop_required"] = {"code": 9, "reason": "審查代理用的模型不是 %s（是 %s）" % (need, "、".join(ms)), "at": C.iso(env.t())}
        rec = {"kind": p.get("kind"), "stage": p.get("stage"), "commit": None if want in ("none", "尚無") else want, "verdict": verdict,
               "epoch": p.get("epoch"), "at": C.iso(env.t()), "models": ms, "model_ok": model_ok if model_ok is not None else True,
               "report_source": p.get("report_source")}
        rec["round"] = 1 + len([r for r in (st.get("reviews") or []) if r.get("kind") == rec["kind"] and r.get("epoch") == rec["epoch"]])
        st.setdefault("reviews", []).append(rec)
        st["review_pending"] = None
        out.update(rec)
        runs = os.path.join(env.main_root, *(env.cfg["runsDir"].split("/") + [str(p.get("stage"))]))
        try:
            os.makedirs(runs, exist_ok=True)
            with io.open(os.path.join(runs, "review-%s-%d-%d.md" % (rec["kind"], int(rec["epoch"] or 0), rec["round"])), "w", encoding="utf-8", newline="\n") as fh:
                fh.write("<!-- hook 記錄的審查結論：%s；模型：%s -->\n\n%s\n" % (verdict, "、".join(ms) or "沒有紀錄", report))
        except Exception:                                          # noqa: B902
            pass
    ST.update(env.sd, fn)
    if out:
        ST.log(env.sd, dict(out, event="review"))
    return out or None


def subagentstop(inp, env):
    st = ST.load(env.sd)
    if inp.get("agent_type") != env.cfg["reviewer"]["agentType"] or not st.get("review_pending"):
        return 0
    record_review_text(env, str(inp.get("last_assistant_message") or ""), "last_message")
    models = ST.models_in(inp.get("agent_transcript_path")) if inp.get("agent_transcript_path") else None
    finalize_review(env, models=models or [])
    return 0


def posttool(inp, env):
    """Agent 工具跑完（前景的審查代理）：補上它實際用的模型。背景的在 subagentstop 定案。"""
    if inp.get("tool_name") != "Agent":
        return 0
    ti = inp.get("tool_input") if isinstance(inp.get("tool_input"), dict) else {}
    if ti.get("subagent_type") != env.cfg["reviewer"]["agentType"]:
        return 0
    tr = inp.get("tool_response") if isinstance(inp.get("tool_response"), dict) else {}
    resolved = tr.get("resolvedModel")
    used = tr.get("modelsUsed") or []
    hb = (tr.get("handbackReport") or {}).get("text") if isinstance(tr.get("handbackReport"), dict) else None
    st = ST.load(env.sd)
    if st.get("review_pending"):
        if hb:
            record_review_text(env, hb, "handback")
        if tr.get("status") == "completed":
            finalize_review(env, models=list(used), resolved=resolved)
        return 0
    need = env.cfg["requiredModel"]
    bad = [ST.norm_model(m) for m in ([resolved] + list(used)) if m and ST.norm_model(m) != need]
    if bad:
        def fn(s):
            if s.get("reviews"):
                s["reviews"][-1]["model_ok"] = False
                s["reviews"][-1]["verdict"] = "INVALID"
                s["reviews"][-1]["models"] = sorted(set((s["reviews"][-1].get("models") or []) + bad))
            if not s.get("stop_required"):
                s["stop_required"] = {"code": 9, "reason": "審查代理用的模型不是 %s（是 %s）" % (need, "、".join(bad)), "at": C.iso(env.t())}
        ST.update(env.sd, fn)
    return 0


# ---------------------------------------------------------------- 保護檔有沒有被動過

def integrity_problems(env):
    """生效中的保護檔跟「放行過的版本」一不一樣。回傳問題清單（空的＝沒問題）。
    放行過的版本＝主目錄 HEAD（main）上的那一份；P1 還沒合併時＝安裝腳本記下的那個 commit。"""
    problems = []
    ref = "HEAD"
    rc, out = C.git(["ls-tree", "-r", "--name-only", ref, "--"] + LIVE_PARTS, env.main_root)
    tracked = [f for f in out.split("\n") if f.strip()] if rc == 0 else []
    if not tracked:
        inst = os.path.join(env.sd, "install.json")
        if not os.path.exists(inst):
            return ["保護檔還沒有進 main，也沒有安裝紀錄（請跑 python scripts/autopilot_install.py）"]
        ref = C.load_json(inst).get("source_commit") or ""
        rc, out = C.git(["ls-tree", "-r", "--name-only", ref, "--"] + LIVE_PARTS, env.main_root)
        tracked = [f for f in out.split("\n") if f.strip()] if rc == 0 else []
        if not tracked:
            return ["找不到安裝時記下的那個版本（%s）" % ref[:7]]
    try:
        p = subprocess.run(["git", "cat-file", "--batch"], cwd=env.main_root, input="".join("%s:%s\n" % (ref, f) for f in tracked).encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env=C.git_env())
        data, blobs, i = p.stdout, {}, 0
        for f in tracked:
            j = data.index(b"\n", i)
            head = data[i:j].decode("ascii", "replace").split()
            size = int(head[2])
            blobs[f] = data[j + 1:j + 1 + size]
            i = j + 1 + size + 1
    except Exception as e:                                          # noqa: B902
        return ["讀不到放行過的版本來比對（%r）" % (e,)]
    for f in tracked:
        live = os.path.join(env.main_root, *f.split("/"))
        if not os.path.exists(live):
            problems.append("少了 %s" % f)
            continue
        with io.open(live, "rb") as fh:
            cur = fh.read()
        if cur.replace(b"\r\n", b"\n") != blobs[f].replace(b"\r\n", b"\n"):
            problems.append("%s 跟放行過的版本不一樣" % f)
    known = set(tracked)
    for part in LIVE_PARTS[1:]:
        base = os.path.join(env.main_root, *part.split("/"))
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), env.main_root).replace("\\", "/")
                if rel not in known and not name.endswith(".pyc"):
                    problems.append("多了一個沒放行過的檔：%s" % rel)
    local = os.path.join(env.main_root, ".claude", "settings.local.json")
    if os.path.exists(local):
        try:
            obj = C.load_json(local)
            extra = sorted(k for k in obj.keys() if k != "permissions")
            pk = sorted(k for k in (obj.get("permissions") or {}).keys() if k not in ("allow", "ask", "deny", "additionalDirectories"))
            if extra or pk:
                problems.append(".claude/settings.local.json 裡有會改變保護的設定（%s）" % "、".join(extra + pk))
        except Exception:                                           # noqa: B902
            problems.append(".claude/settings.local.json 讀不懂")
    hook = os.path.join(C.common_dir(env.main_root), "hooks", "pre-push")
    if not os.path.exists(hook):
        problems.append("推送前的檢查（.git/hooks/pre-push）沒有裝（請跑 python scripts/autopilot_install.py）")
    else:
        with io.open(hook, encoding="utf-8", errors="replace") as fh:
            if PREPUSH_MARK not in fh.read():
                problems.append(".git/hooks/pre-push 不是我們裝的那一份")
    return problems


# ---------------------------------------------------------------- prompt（David 的指令詞）

def _out(system=None, context=None):
    obj = {}
    if system:
        obj["systemMessage"] = system
    if context:
        obj["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": context}
    if obj:
        C.out_json(obj)
    return 0


def prompt(inp, env):
    cmd = ST.parse_command(inp.get("prompt"))
    st = ST.load(env.sd)
    cfg = env.cfg
    if cmd is None:
        if mine(st, inp) and not str(inp.get("prompt") or "").lstrip().startswith("<"):
            return _out(context="自動駕駛的狀態（hook 提供）：階段 %s，狀態 %s。David 這一則不是指令詞；照他說的做，但自動駕駛的規則仍然有效。"
                                % (st.get("stage"), st.get("status")))
        return 0
    kind, stage = cmd["kind"], cmd.get("stage")
    now = C.iso(env.t())

    def remember(**extra):
        """記下這一句，等下一個動作前核對是不是人打的（verify_commands）。"""
        def fn0(s):
            s.setdefault("cmd_checks", []).append(dict(extra, kind=kind, stage=stage, prompt_id=inp.get("prompt_id"),
                                                       transcript_path=inp.get("transcript_path"), at=now))
        ST.update(env.sd, fn0)

    if stage is not None and not C.stage_ok(stage):
        return _out(system="（自動駕駛）階段名稱「%s」不合規則：只能用英數、點、底線、連字號，英數開頭。" % stage)

    if kind == "start":
        if inp.get("permission_mode") == "bypassPermissions":
            return _out(system="（自動駕駛）沒有啟動：現在是「略過權限（Bypass）」模式。請在輸入框旁的模式選單改成 Auto、Accept edits 或 Manual，再輸入一次。",
                        context="自動駕駛沒有啟動（權限模式是 bypassPermissions）。請告訴 David 怎麼切換模式；不要開始做這個階段。")
        if st.get("active") and st.get("stage") != stage and st.get("status") != "done":
            return _out(system="（自動駕駛）沒有啟動：另一個階段「%s」還在進行（狀態：%s）。要先結束它請輸入「結束自動駕駛」。" % (st.get("stage"), st.get("status")),
                        context="自動駕駛沒有啟動：階段 %s 還在進行。不要開始新的階段。" % st.get("stage"))
        problems = integrity_problems(env)
        if problems:
            return _out(system="（自動駕駛）沒有啟動：保護檔的檢查沒過——" + "；".join(problems[:4]),
                        context="自動駕駛沒有啟動：保護檔的檢查沒過（%s）。請把這件事原樣告訴 David，不要自己處理這些檔。" % "；".join(problems[:6]))
        same = st.get("stage") == stage
        # 啟動當下，這個階段的分支上已經有的「自動駕駛期間動不得」的變更（David 在場時做的；例如 P1 自己就是在改保護檔）。
        # 這個階段還在自動駕駛中又輸入一次啟動的話，沿用第一次記下的——不可以把自動駕駛中途動的也算進「原本就有」。
        still_running = same and bool(st.get("active")) and st.get("preexisting") is not None
        pre = st.get("preexisting") if still_running else N.preexisting_protected(env.main_root, cfg, stage)
        spec = cmd.get("rest") or ""
        spec_path = ST.save_spec(env.sd, stage, spec) if spec.strip() else (st.get("spec_path") if same else None)
        if spec.strip():
            try:                                                    # 給人看的副本；審查代理以 .git/iw-autopilot/specs/ 那一份為準
                runs = os.path.join(env.main_root, *(cfg["runsDir"].split("/") + [stage]))
                os.makedirs(runs, exist_ok=True)
                with io.open(os.path.join(runs, "00_規格.md"), "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(spec)
            except Exception:                                       # noqa: B902
                pass

        def fn(s):
            keep = dict((k, s.get(k)) for k in ("reviews", "candidate", "tier2", "tripwire_baseline", "tripwire_findings", "models_seen",
                                                "effort_seen", "fallbacks") if same and k in s)
            epoch = int(s.get("epoch") or 0) + 1 if same else 1
            base = s.get("tripwire_baseline")
            s.clear()
            s.update({"version": 1, "active": True, "stage": stage, "status": "pending", "session_id": inp.get("session_id"),
                      "started_at": now, "clock_started_at": now, "epoch": epoch, "reviews": [], "transcript_path": inp.get("transcript_path"),
                      "spec_path": spec_path, "stop_required": None, "pause": None, "model_violation": None, "model_approved": [],
                      "credential": None, "notified_epoch": None, "preexisting": pre})
            if base:
                s["tripwire_baseline"] = base
            s.update(keep)
            if s.get("candidate") and same:
                s["status_hint"] = "這個階段之前已經做到「登記要合併的 commit」；重新啟動後要重新驗收再寄信"
                s["candidate"] = None
        ST.update(env.sd, fn)
        remember()
        ST.log(env.sd, {"event": "start", "stage": stage, "session": inp.get("session_id"), "spec_chars": len(spec)})
        hours = cfg["maxHours"]
        note = ""
        if pre:
            note = ("\n注意：這個階段的分支在啟動之前就已經改了 %d 個「自動駕駛期間動不得」的檔（%s%s）。"
                    "它們會列在停止報告裡給你看；自動駕駛期間不能再改它們。"
                    % (len(pre), "、".join(sorted(pre)[:4]), "……" if len(pre) > 4 else ""))
        return _out(system="自動駕駛已啟動：%s（上限 %s 小時）。模型與思考強度在第一個動作時檢查，不對會馬上告訴你。%s%s"
                           % (stage, hours, "" if spec.strip() else "這次沒有貼規格。", note),
                    context=("David 親手啟動了自動駕駛：階段 %s。請用 Skill 工具載入 iw-autopilot，照它的流程做（先偵察、寫覆述、交給審查代理）。\n"
                             "規格原文（hook 存的，審查代理看這一份）：%s\n"
                             "這一輪的時間上限 %s 小時；模型必須是 %s、思考強度必須是 %s。%s"
                             % (stage, spec_path or "這次沒有貼；請看 .autopilot/runs/%s/ 裡有沒有之前的規格" % stage, hours, cfg["requiredModel"],
                                cfg["requiredEffort"],
                                ("\n這個階段的分支在啟動之前就已經改了 %d 個動不得的檔（David 在場時改的）。你不能再改它們；"
                                 "停止報告的「你要決定的事」第一件要寫明這次改了保護或排程相關的檔。" % len(pre)) if pre else "")))

    if kind == "end":
        if not st.get("active"):
            return _out(system="（自動駕駛）現在沒有在自動駕駛。")

        def fn(s):
            s["active"] = False
            s["last_end"] = {"reason": "David 輸入了結束自動駕駛", "at": now, "status_was": s.get("status")}
            s["status"] = None
            if s.get("credential"):
                s["credential"]["revoked"] = "David 結束了自動駕駛"
        ST.update(env.sd, fn)
        remember()
        return _out(system="自動駕駛已結束（階段 %s）。永遠有效的那幾條保護不受影響：合併進 main 仍然要你輸入「放行 <階段>」。" % st.get("stage"),
                    context="David 結束了自動駕駛。之後照一般的方式工作（他在場、照他說的做）；合併進 main 仍然需要他輸入「放行 <階段>」。")

    if kind == "approve_model":
        mv = st.get("model_violation")
        if not (st.get("active") and mv):
            return _out(system="（自動駕駛）現在沒有「模型被換掉」的暫停，這句話沒有作用。")

        def fn(s):
            m = (s.get("model_violation") or {}).get("model")
            if m and m not in (s.get("model_approved") or []):
                s.setdefault("model_approved", []).append(m)
            s["model_violation"] = None
            if s.get("status") == "paused" and (s.get("pause") or {}).get("reason") == "model":
                s["status"] = "running"
                s["pause"] = None
            s["clock_started_at"] = now
            s["epoch"] = int(s.get("epoch") or 0) + 1
            s["session_id"] = inp.get("session_id")
        ST.update(env.sd, fn)
        remember()
        ST.log(env.sd, {"event": "approve_model", "model": mv.get("model")})
        return _out(system="已放行模型：%s。自動駕駛（階段 %s）用這個模型繼續；報告會照實寫中途換過模型。" % (mv.get("model"), st.get("stage")),
                    context="David 親手輸入了「放行模型」：同意用 %s 繼續階段 %s。請接著做；停止報告的「中途是否切換」會由程式照實寫。" % (mv.get("model"), st.get("stage")))

    # 以下三種都要對得上現在的階段
    if st.get("stage") != stage:
        return _out(system="（自動駕駛）這句話指的是階段「%s」，但現在紀錄裡的階段是「%s」，沒有作用。" % (stage, st.get("stage") or "（沒有）"),
                    context="David 輸入了針對階段 %s 的指令詞，但 hook 紀錄的階段是 %s，所以沒有生效。請把這件事告訴他。" % (stage, st.get("stage")))

    if kind == "resume":
        if st.get("status") == "awaiting_approval":
            return _out(system="（自動駕駛）現在停在合併前：要合併請輸入「放行 %s」；要改請輸入「修改 %s：＿＿」。" % (stage, stage))
        if st.get("status") in ("done", None) and not st.get("active"):
            return _out(system="（自動駕駛）階段 %s 現在不在進行中。要重新開始請輸入「自動駕駛：%s」。" % (stage, stage))
        wt = N.stage_worktree(env.main_root, cfg, stage)
        base = "%s/%s" % (cfg["remote"], cfg["mainBranch"])
        hashes = {}
        for f in ((st.get("tier2") or {}).get("touched") or {}).keys():
            try:
                hashes[f] = N.diff_hash(wt, base, f)
            except Exception:                                       # noqa: B902
                hashes[f] = None

        def fn(s):
            s["active"] = True
            s["status"] = "pending"                                 # 重新檢查模型與強度
            s["pause"] = None
            s["stop_required"] = None
            s["clock_started_at"] = now
            s["epoch"] = int(s.get("epoch") or 0) + 1
            s["session_id"] = inp.get("session_id")
            s["transcript_path"] = inp.get("transcript_path")
            if s.get("model_violation"):
                s["model_violation"] = None
            for f, v in ((s.get("tier2") or {}).get("touched") or {}).items():
                if v.get("reported"):
                    v["ack"] = True
                    v["hash"] = hashes.get(f) or v.get("hash")
                    v["acked_at"] = now
        ST.update(env.sd, fn)
        remember()
        ST.log(env.sd, {"event": "resume", "stage": stage})
        return _out(system="自動駕駛繼續：%s（重新計時，上限 %s 小時）。" % (stage, cfg["maxHours"]),
                    context="David 親手輸入了「繼續 %s」。請從上次停下的地方接著做（先看 .autopilot/runs/%s/ 與 python .claude/hooks/iw_notify.py status）。"
                            "如果上一次是請他看 diff，他這句話就是看過了。" % (stage, stage))

    if kind == "revise":
        def fn(s):
            s["active"] = True
            s["status"] = "pending"
            s["pause"] = None
            s["stop_required"] = None
            s["candidate"] = None
            if s.get("credential"):
                s["credential"]["revoked"] = "David 輸入了修改"
            s["clock_started_at"] = now
            s["epoch"] = int(s.get("epoch") or 0) + 1
            s["session_id"] = inp.get("session_id")
            s["transcript_path"] = inp.get("transcript_path")
            s.setdefault("revisions", []).append({"at": now, "text": cmd.get("rest") or ""})
        ST.update(env.sd, fn)
        remember()
        ST.log(env.sd, {"event": "revise", "stage": stage})
        return _out(system="自動駕駛回到施工：%s（之前的通行證與「可以合併」的登記都作廢，改完會再寄一次信）。" % stage,
                    context="David 親手輸入了「修改 %s」，要改的事：%s\n請照做（修正做在標籤之後的 commit、標籤不動），改完重新驗收審查、再寄一次停止報告。"
                            % (stage, cmd.get("rest") or "（他沒有寫內容，請問他）"))

    if kind == "approve":
        cand = st.get("candidate") or {}
        if st.get("status") not in ("awaiting_approval", "merged") or not cand.get("sha"):
            return _out(system="（自動駕駛）還不能放行 %s：這個階段還沒有寄出「可以合併」的信（現在的狀態：%s）。" % (stage, st.get("status") or "沒有在進行"),
                        context="David 輸入了「放行 %s」，但這個階段還沒有登記要合併的 commit（沒有寄過「可以合併」的停止報告），所以通行證沒有開。請告訴他現在的狀態。" % stage)
        tag = cfg["tagPrefix"] + stage
        rc, tag_sha = C.git(["rev-parse", "-q", "--verify", "refs/tags/%s^{commit}" % tag], env.main_root)
        rc2 = 1
        if rc == 0:
            rc2, _ = C.git(["merge-base", "--is-ancestor", tag_sha, cand["sha"]], env.main_root)
        if rc != 0 or rc2 != 0:
            return _out(system="（自動駕駛）還不能放行 %s：標籤 %s 不在，或不在要合併的那個 commit 的歷史裡。" % (stage, tag),
                        context="David 輸入了「放行 %s」，但標籤 %s 檢查沒過，通行證沒有開。請查清楚再回報他。" % (stage, tag))

        def fn(s):
            ST.issue_credential(s, cfg, inp, env.t())
            if s.get("status") != "merged":
                s["status"] = "approved"
            s["session_id"] = inp.get("session_id")
            s["transcript_path"] = inp.get("transcript_path")
            s["clock_started_at"] = now
            s["stop_required"] = None
            s["pause"] = None
        new = ST.update(env.sd, fn)
        remember(candidate=cand["sha"], tag_sha=tag_sha)
        ST.log(env.sd, {"event": "approve", "stage": stage, "candidate": cand["sha"]})
        hours = cfg["credentialHours"]
        return _out(system="已放行 %s：通行證已開（只准合併 commit %s；%s 小時內有效；合併一次、文件一筆）。" % (stage, cand["sha"][:7], hours),
                    context=("David 親手輸入了「放行 %s」，hook 已經開了通行證：只准把 commit %s 用 --no-ff 合併進 main、推上去一次，"
                             "再推一筆只改 %s 的文件 commit（合併後 %s 分鐘內）。\n"
                             "請照 iw-autopilot 的「合併與收尾」做：暫時的 worktree（%s/%s%s-merge）以 origin/main 為底合併 → 合併後的樹上跑全套測試 → "
                             "git push origin HEAD:main → 主目錄 git merge --ff-only origin/main → 文件 commit → 收 worktree 與分支 → "
                             "python .claude/hooks/iw_notify.py close --stage %s。"
                             % (stage, cand["sha"][:7], "、".join(cfg["docsFollowupFiles"]), cfg["docsFollowupMinutes"],
                                cfg["worktreeDir"], cfg["tagPrefix"], stage, stage))
                    if new else None)
    return 0


# ---------------------------------------------------------------- 其他事件

def permission(inp, env):
    st = ST.load(env.sd)
    if not mine(st, inp):
        return 0
    ti = inp.get("tool_input") if isinstance(inp.get("tool_input"), dict) else {}
    what = "%s %s" % (inp.get("tool_name"), str(ti.get("command") or ti.get("file_path") or "")[:120])

    def fn(s):
        if not s.get("stop_required"):
            s["stop_required"] = {"code": 8, "reason": "這個動作需要你按批准：%s" % what, "at": C.iso(env.t())}
    ST.update(env.sd, fn)
    ST.log(env.sd, {"event": "permission-denied-by-hook", "what": what})
    C.out_json({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
        "behavior": "deny",
        "message": "自動駕駛期間不等「按批准」的視窗（David 不在電腦前），所以這個動作被拒絕了。這是停止條件 8：請寫停止報告、寄信，說明為什麼需要它；不要換別的寫法繞過。"}}})
    return 0


def stop(inp, env):
    try:
        N.retry_outbox(env.main_root, env.cfg, env.runner)
    except Exception:                                               # noqa: B902
        pass
    st = ST.load(env.sd)
    if not mine(st, inp):
        return 0
    if inp.get("background_tasks"):
        return 0                                                    # 還有背景工作在跑：不是真的停，等它回來
    if st.get("status") in ("running", "pending", "approved", "merged") and st.get("notified_epoch") != st.get("epoch"):
        text = "自動駕駛停了，原因不明，請看 Claude Code"
        if st.get("status") in ("approved", "merged"):
            text = "自動駕駛在合併收尾的途中停了，請看 Claude Code"
        if st.get("stop_required"):
            text = "自動駕駛停了（停止條件 %s：%s），但沒有寄出正式的停止報告，請看 Claude Code" % (st["stop_required"].get("code"), st["stop_required"].get("reason"))
        N.fallback(env.main_root, env.sd, env.cfg, text, runner=env.runner, min_gap_key="stop")

        def fn(s):
            if s.get("status") in ("running", "pending"):
                s["status"] = "stopped"
                s["pause"] = {"reason": "unknown", "detail": "沒有寄停止報告就停了", "at": C.iso(env.t())}
        ST.update(env.sd, fn)
    return 0


def reset_time(text):
    """從錯誤訊息裡找「額度何時恢復」。Claude Code 的訊息長這樣：You've hit your session limit · resets 3:20am (Asia/Taipei)"""
    m = re.search(r"resets?\s+(?:at\s+)?([0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?(?:\s*\([^)]+\))?)", text or "", re.I)
    return m.group(1).strip() if m else None


def stopfailure(inp, env):
    st = ST.load(env.sd)
    if not mine(st, inp):
        return 0
    err = str(inp.get("error") or "unknown")
    raw = " ".join(str(inp.get(k) or "") for k in ("last_assistant_message", "error_details")).strip()
    when = reset_time(raw)
    names = {"rate_limit": "用量上限", "overloaded": "伺服器忙線", "billing_error": "帳務問題", "authentication_failed": "登入過期",
             "server_error": "伺服器錯誤", "max_output_tokens": "單次輸出太長", "model_not_found": "找不到模型"}
    what = names.get(err, "API 錯誤（%s）" % err)
    text = ("自動駕駛因為%s停了，已暫停。\n額度何時恢復：%s\n訊息原文：%s\n恢復之後請回到 Claude Code 輸入「繼續 %s」（不會自己換模型繼續，也不會自己接著做）。"
            % (what, when or "訊息裡沒有寫（請看 Claude Code 畫面或 claude.ai 的用量頁）", raw[:300] or "（沒有）", st.get("stage")))

    def fn(s):
        s["status"] = "paused"
        s["pause"] = {"reason": "api:" + err, "detail": "%s%s" % (what, "，%s 恢復" % when if when else ""), "at": C.iso(env.t())}
    ST.update(env.sd, fn)
    N.fallback(env.main_root, env.sd, env.cfg, text, runner=env.runner, min_gap_key="stopfailure")
    return 0


def notification(inp, env):
    st = ST.load(env.sd)
    if not mine(st, inp):
        return 0
    t = str(inp.get("notification_type") or "")
    ST.log(env.sd, {"event": "notification", "type": t, "message": str(inp.get("message") or "")[:200]})
    if t in ("permission_prompt", "agent_needs_input", "elicitation_dialog"):
        N.fallback(env.main_root, env.sd, env.cfg,
                   "自動駕駛卡在一個要你回應的視窗（%s），請看 Claude Code" % (str(inp.get("message") or t)[:80]),
                   runner=env.runner, min_gap_key="prompt")
    return 0


def configchange(inp, env):
    """設定檔在工作階段中被改：專案層級的一律不套用；你個人層級的只在自動駕駛期間不套用。"""
    src = str(inp.get("source") or "")
    st = ST.load(env.sd)
    block = src in ("project_settings", "local_settings", "skills") or (src == "user_settings" and mine(st, inp))
    ST.log(env.sd, {"event": "configchange", "source": src, "file": inp.get("file_path"), "blocked": block})
    if block:
        C.err("自動駕駛：工作階段進行中改設定檔（%s）不會生效；要生效請重開工作階段。" % src)
        return 2
    return 0


def modelswitch(inp, env):
    st = ST.load(env.sd)
    if not mine(st, inp):
        return 0
    to = ST.norm_model(inp.get("to_model"))
    ST.log(env.sd, {"event": "modelswitch", "from": inp.get("from_model"), "to": to, "source": inp.get("source"), "hook": inp.get("hook_event_name")})
    if inp.get("hook_event_name") != "PostModelSwitch" or not to:
        return 0
    need = env.cfg["requiredModel"]
    if to != need and to not in (st.get("model_approved") or []):
        apply_effects(env, [("model_violation", to)], inp)
    return 0


def sessionstart(inp, env):
    st = ST.load(env.sd)
    msgs, ctx = [], []
    if os.path.exists(os.path.join(env.main_root, ".claude", "settings.json")):
        problems = integrity_problems(env)
        if problems:
            msgs.append("（自動駕駛）保護檔的檢查沒過——" + "；".join(problems[:4]) + "。在處理好之前，自動駕駛不會啟動。")
            ctx.append("保護檔的檢查沒過：%s。請把這件事原樣告訴 David；不要自己處理這些檔。" % "；".join(problems[:6]))
    if st.get("active"):
        if st.get("session_id") == inp.get("session_id"):
            ctx.append("自動駕駛進行中（hook 提供）：階段 %s，狀態 %s。流程在 iw-autopilot 這個 skill；紀錄在 .autopilot/runs/%s/；"
                       "python .claude/hooks/iw_notify.py status 可以看完整狀態。" % (st.get("stage"), st.get("status"), st.get("stage")))
        else:
            ctx.append("注意：另一個工作階段正在自動駕駛（階段 %s，狀態 %s）。這個工作階段不受自動駕駛的清單限制，但「合併進 main 要 David 放行」等保護仍然有效；"
                       "不要動那個階段的 worktree。" % (st.get("stage"), st.get("status")))
    m = ST.norm_model(inp.get("model"))
    if m and mine(st, inp) and m != env.cfg["requiredModel"] and m not in (st.get("model_approved") or []):
        apply_effects(env, [("model_violation", m)], inp)
    if msgs or ctx:
        obj = {}
        if msgs:
            obj["systemMessage"] = "\n".join(msgs)
        if ctx:
            obj["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": "\n".join(ctx)}
        C.out_json(obj)
    return 0


def sessionend(inp, env):
    st = ST.load(env.sd)
    if mine(st, inp) and st.get("status") in ("running", "pending", "approved", "merged") and st.get("notified_epoch") != st.get("epoch"):
        N.fallback(env.main_root, env.sd, env.cfg, "自動駕駛所在的工作階段結束了（%s），事情還沒做完，請看 Claude Code" % (inp.get("reason") or "原因不明"),
                   runner=env.runner, min_gap_key="sessionend")
    return 0


HANDLERS = {"pretool": pretool, "prompt": prompt, "permission": permission, "posttool": posttool, "subagentstop": subagentstop,
            "stop": stop, "stopfailure": stopfailure, "notification": notification, "configchange": configchange,
            "modelswitch": modelswitch, "sessionstart": sessionstart, "sessionend": sessionend}

# 出錯時要當成「擋下」的事件（其餘的出錯就算了，不可以卡住 David 的訊息或讓工作階段結束不了）
FAIL_CLOSED = set(["pretool", "configchange"])
