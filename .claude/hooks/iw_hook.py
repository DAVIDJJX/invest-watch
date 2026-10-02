# -*- coding: utf-8 -*-
"""
iw_hook.py — 所有 hook 的入口。settings.json 的每個 hook 都是：python iw_hook.py <事件>

這一支刻意寫得很短，而且除了 sys 之外的 import 都包在 try 裡面：
官方文件說 hook 只有結束碼 2 會擋，其他非 0（包括 Python 自己出錯的 1）都是「不擋的錯誤」。
所以在「要擋」的事件（pretool、configchange）上，其他檔案只要有任何問題——語法錯、少了檔、設定讀不到、
狀態檔壞掉、沒有輸入、卡住——一律以結束碼 2 結束。其他事件出錯就安靜地結束，不可以卡住 David 的訊息。
"""
import sys

FAIL_CLOSED = ("pretool", "configchange")


def main(argv):
    event = argv[1] if len(argv) > 1 else ""
    closed = event in FAIL_CLOSED
    try:
        import json
        import os
        import threading

        def give_up():
            try:
                sys.stderr.write("iw_hook: timed out\n")
            finally:
                os._exit(2 if closed else 0)

        t = threading.Timer(20.0 if closed else 100.0, give_up)   # 官方文件：hook 逾時不會擋，所以自己先結束
        t.daemon = True
        t.start()

        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import iw_common as C
        import iw_events as E
        import iw_notify as N
        import iw_state as ST

        if event == "send-fallback":                                # 守門另開程序寄後援信用的
            env = E.Env()
            N.fallback(env.main_root, env.sd, env.cfg, argv[3] if len(argv) > 3 else "自動駕駛停了，請看 Claude Code",
                       min_gap_key=argv[2] if len(argv) > 2 else "x")
            return 0
        handler = E.HANDLERS.get(event)
        if handler is None:
            C.err("iw_hook：不認得的事件「%s」" % event)
            return 2 if closed else 0
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        if not raw.strip():
            if closed:
                C.err("自動駕駛的檢查程式沒有收到輸入，先擋下這個動作。")
                return 2
            return 0
        inp = json.loads(raw)
        env = E.Env()
        try:
            return int(handler(inp, env) or 0)
        except Exception as e:                                      # noqa: B902
            import traceback
            ST.log(env.sd, {"event": "hook-error", "hook": event, "error": repr(e), "trace": traceback.format_exc()[-1500:]})
            raise
    except SystemExit:
        raise
    except BaseException as e:                                      # noqa: B902
        try:
            msg = "自動駕駛的檢查程式出錯（%s：%r）。" % (event, e)
            if closed:
                msg += "為了安全，先擋下這個動作。請把這段訊息原樣告訴 David；這不是可以自己修的東西。"
            sys.stderr.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
            sys.stderr.buffer.flush()
        except Exception:                                           # noqa: B902
            pass
        return 2 if closed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
