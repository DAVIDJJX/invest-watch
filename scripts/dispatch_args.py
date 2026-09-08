#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dispatch_args.py — workflow 第一步：決定這一次的 mode 與 slot，不合法就結束碼 2

為什麼要有這一支？
    時段不再用時間猜，改由觸發者告訴 workflow。但「告訴」也可能告訴錯：
    cron-job.org 的 job 若填成 mode=light、slot=morning，就會產生一份沒有抓
    完整資料的晨報。合法的組合只有兩種：
        slot=light                      ⇔ mode=light   （盤中只更新現價）
        slot=四個報告時段或 manual       ⇔ mode=full    （抓完整資料＋產報告）
    不合法的組合在 workflow 第一步就擋下來，附說明，後面什麼都不跑。

    GitHub 自己的 cron 只是備援，而且量過它從來不準時（晚 2～5 小時），
    所以備援【只做 light 的資料更新、永遠不產報告】——不管 cron 幾點觸發，
    都不會產生一份時間標籤錯的報告。缺席的報告會被 freshness 與之後的 watchdog 抓到。

用法（給 workflow 用）：
    python scripts/dispatch_args.py --event "$GITHUB_EVENT_NAME" --mode "$MODE" --slot "$SLOT"
    成功：印出 mode=… 與 slot=… 兩行（直接 >> $GITHUB_OUTPUT）
    失敗：印 ::error:: 並回結束碼 2
"""

import argparse
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPORT_SLOTS = ("morning", "midmorning", "close", "review")
FULL_SLOTS = REPORT_SLOTS + ("manual",)
ALL_SLOTS = ("light",) + FULL_SLOTS
MODES = ("light", "full")


def resolve(event, mode=None, slot=None):
    """回傳 (mode, slot)；不合法就 raise ValueError（訊息給人看）。"""
    if event == "schedule":
        # 備援 cron：一律 light，忽略任何輸入。理由見檔頭。
        return "light", "light"
    if event != "workflow_dispatch":
        raise ValueError("不支援的觸發方式：%s（只接受 workflow_dispatch 與 schedule）" % event)

    mode = (mode or "").strip()
    slot = (slot or "").strip()
    if mode not in MODES:
        raise ValueError("mode 必須是 light 或 full，收到「%s」" % mode)
    if slot not in ALL_SLOTS:
        raise ValueError("slot 必須是 %s 之一，收到「%s」" % ("/".join(ALL_SLOTS), slot))
    if slot == "light" and mode != "light":
        raise ValueError("slot=light 只能配 mode=light（盤中更新不抓完整資料、不產報告）")
    if slot in FULL_SLOTS and mode != "full":
        raise ValueError("slot=%s 必須配 mode=full（報告要用完整資料）" % slot)
    return mode, slot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    ap.add_argument("--mode", default="")
    ap.add_argument("--slot", default="")
    args = ap.parse_args()
    try:
        mode, slot = resolve(args.event, args.mode, args.slot)
    except ValueError as e:
        print("::error::%s" % e)
        return 2
    print("mode=%s" % mode)
    print("slot=%s" % slot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
