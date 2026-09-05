#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report.py — 三時段報告產生器

由 GitHub Actions 在合併出 data/latest.json 之後呼叫（家用電腦不產報告，
兩邊都產會互相覆蓋同一份 data/report-latest.json）。依 slot 產生一份**結構化 JSON**：
  morning 晨報「了解今天狀況」
  midday  午盤「盤中整理」
  close   收盤「檢討與分析」
  manual  手動更新（全覽）

刻意不在這裡拼 HTML——JSON 交給前端 js/report.js 渲染，
這樣改版面不用動 Python，封存下來的報告未來也還讀得懂。

封存位置：
  data/archive/YYYY-MM-DD/<slot>.json   當天各時段的報告
  data/archive/YYYY-MM-DD/snapshot.json 當天最後一次的行情快照
  data/archive/index.json               日期清單（歷史頁用）
  data/report-latest.json               最新一份報告（首頁連結用）

隱私：本檔只讀公開市場資料（data/latest.json、data/history/），
      沒有任何管道接觸到個人持倉，報告內容不可能提到你的部位。

誠實：所有觀察句都是規則產生的客觀描述，不做買賣建議；
      抓取失敗的標的會在報告裡明確列出「更新失敗」，不拿舊資料充數。
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import indicators as ind

TPE = timezone(timedelta(hours=8))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
HIST_DIR = os.path.join(DATA_DIR, "history")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")
INDEX_FILE = os.path.join(ARCHIVE_DIR, "index.json")
REPORT_LATEST = os.path.join(DATA_DIR, "report-latest.json")

DISCLAIMER = ("本報告是把公開市場數據依固定規則整理成白話句子，"
              "所有描述都是客觀指標的陳述，未經歷史回測驗證，"
              "不構成投資建議，也不保證未來走勢。")

SLOT_TITLE = {
    "morning": "晨報：了解今天狀況",
    "midday": "午盤：盤中整理",
    "close": "收盤：檢討與分析",
    "manual": "手動更新：全覽",
}

# 報告裡的分組
TW_IDS = ["twii", "tw2330", "tw00646"]
INTL_IDS = ["nvda", "gspc", "btc", "wti"]
BOT_IDS = ["gold_twd", "gold_cny", "gold_bar", "fx_usd", "fx_cny"]

# 台銀實體條塊各規格的公克數（算每公克單價用）
BAR_GRAMS = {
    "1 公斤": 1000.0,
    "500 公克": 500.0,
    "250 公克": 250.0,
    "100 公克": 100.0,
    "金鑽 1 台兩": 37.5,
}


# ---------------------------------------------------------------- 小工具


def now_tpe():
    return datetime.now(TPE)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


def fmt(v, dec=2):
    """數字轉成有千分位的字串；None 回破折號。"""
    if v is None or not isinstance(v, (int, float)):
        return "—"
    return "{:,.{d}f}".format(v, d=dec)


def fmt_pct(v, dec=2):
    """帶正負號的百分比，用在「今日漲跌」這種需要看方向的欄位。"""
    if v is None or not isinstance(v, (int, float)):
        return "—"
    return "{}{:.{d}f}%".format("+" if v >= 0 else "", v, d=dec)


def fmt_pct_abs(v, dec=2):
    """只有大小、不帶正負號。句子裡已經有「上漲／下跌」時用這個，
    否則會寫出「下跌 +3.10%」這種自相矛盾的句子。"""
    if v is None or not isinstance(v, (int, float)):
        return "—"
    return "{:.{d}f}%".format(abs(v), d=dec)


def move_word(pct):
    """把漲跌幅轉成白話動詞。"""
    if pct is None:
        return "沒有資料"
    if pct > 0:
        return "上漲"
    if pct < 0:
        return "下跌"
    return "持平"


def load_history(asset_id):
    d = read_json(os.path.join(HIST_DIR, "%s.json" % asset_id), {}) or {}
    return d.get("points") or []


# ---------------------------------------------------------------- 市場狀態


def detect_market(latest, now):
    """判斷台股今天有沒有開市、現在是盤前/盤中/盤後，以及台銀掛牌是不是今天的。"""
    today = now.strftime("%Y-%m-%d")
    assets = latest.get("assets") or {}
    twii = assets.get("twii") or {}

    # 台股是否為交易日：mis 回的日期就是最後一個交易日，
    # 等於今天＝今天有開市；停在前一天＝今天休市。
    trading = None
    note = None
    if now.weekday() >= 5:
        trading = False
        note = "週末，台股休市。"
    elif twii.get("status") == "ok" and twii.get("date"):
        trading = (twii["date"] == today)
        if not trading:
            note = "台股今日休市（最新報價仍停在 %s）。" % twii["date"]
    else:
        note = "台股報價來源沒有回應，今天是否開市無法確認。"

    mins = now.hour * 60 + now.minute
    if trading is False:
        phase = "holiday"
    elif mins < 9 * 60:
        phase = "pre"
    elif mins <= 13 * 60 + 30:
        phase = "open"
    else:
        phase = "closed"

    # 台銀黃金掛牌是不是今天的
    gold = assets.get("gold_twd") or {}
    gold_qt = gold.get("quoteTime") or ""
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})", gold_qt)
    gold_date = "-".join(m.groups()) if m else gold.get("date")
    gold_fresh = (gold_date == today)

    fx = assets.get("fx_usd") or {}
    fx_fresh = (fx.get("date") == today)

    return {
        "date": today,
        "twTradingDay": trading,
        "twPhase": phase,          # pre / open / closed / holiday
        "twNote": note,
        "goldQuoteTime": gold_qt or None,
        "goldQuoteDate": gold_date,
        "goldFresh": bool(gold_fresh),
        "fxDate": fx.get("date"),
        "fxFresh": bool(fx_fresh),
        "simplified": trading is False,   # 非交易日 → 精簡版
    }


# ---------------------------------------------------------------- 報價區塊


def quote_item(a, extra=None, compare=None):
    """把一個標的整理成報告裡的一列。compare = (標籤, 先前價格)。"""
    dec = a.get("decimals", 2)
    item = {
        "id": a.get("id"),
        "name": a.get("name"),
        "status": a.get("status"),
        "currency": a.get("currency"),
        "unit": a.get("unit"),
        "priceLabel": a.get("priceLabel"),
    }
    if a.get("status") != "ok":
        item["error"] = a.get("error")
        item["errorAt"] = a.get("errorAt") or a.get("fetchedAt")
        lg = a.get("lastGood") or {}
        if lg.get("price") is not None:
            item["lastGoodText"] = "上次成功 %s：%s" % (
                lg.get("date") or "—", fmt(lg.get("price"), dec))
        item["text"] = "資料更新失敗，這一項沒有今天的價格。"
        return item

    price = a.get("price")
    pct = a.get("changePct")
    item.update({
        "price": price,
        "priceText": fmt(price, dec),
        "change": a.get("change"),
        "changeText": fmt(a.get("change"), dec) if a.get("change") is not None else None,
        "changePct": pct,
        "changePctText": fmt_pct(pct),
        "dir": "up" if (pct or 0) > 0 else ("down" if (pct or 0) < 0 else "flat"),
        "date": a.get("date"),
    })
    item["text"] = "%s %s %s（%s）" % (
        a.get("name"), fmt(price, dec), move_word(pct), fmt_pct(pct))

    ex = []
    if a.get("type") == "bot_gold":
        ex.append({"k": "本行買入", "v": fmt(a.get("buy"), dec)})
        ex.append({"k": "本行賣出", "v": fmt(a.get("sell"), dec)})
        if a.get("quoteTime"):
            ex.append({"k": "掛牌時間", "v": a["quoteTime"]})
    elif a.get("type") == "bot_fx":
        ex.append({"k": "即期買入", "v": fmt(a.get("spotBuy"), dec)})
        ex.append({"k": "即期賣出", "v": fmt(a.get("spotSell"), dec)})
        ex.append({"k": "現金買入", "v": fmt(a.get("cashBuy"), dec)})
        ex.append({"k": "現金賣出", "v": fmt(a.get("cashSell"), dec)})
    if a.get("quoteTime") and a.get("type") not in ("bot_gold",):
        ex.append({"k": "報價時間", "v": a["quoteTime"]})
    if extra:
        ex.extend(extra)
    if ex:
        item["extra"] = ex

    if compare and compare[1]:
        label, before = compare
        diff = price - before
        dpct = diff / before * 100 if before else None
        item["compare"] = {
            "label": label,
            "before": before,
            "beforeText": fmt(before, dec),
            "diff": diff,
            "diffText": fmt(diff, dec),
            "diffPct": dpct,
            "diffPctText": fmt_pct(dpct),
            "dir": "up" if diff > 0 else ("down" if diff < 0 else "flat"),
            "text": ("與%s相同，沒有變動。" % label if diff == 0 else
                     "與%s相比%s %s（%s）" % (
                         label, move_word(dpct), fmt(abs(diff), dec),
                         fmt_pct_abs(dpct))),
        }
    return item


def bar_item(a):
    """實體黃金條塊：換算每公克單價，附上與黃金存摺賣出價的價差。"""
    if a.get("status") != "ok" or not a.get("bars"):
        return quote_item(a)
    rows = []
    for b in a["bars"]:
        grams = BAR_GRAMS.get(b.get("spec"))
        per_g = (b["sell"] / grams) if (grams and b.get("sell")) else None
        rows.append({"spec": b.get("spec"), "sell": b.get("sell"),
                     "sellText": fmt(b.get("sell"), 0),
                     "perGram": per_g, "perGramText": fmt(per_g, 1)})
    item = {
        "id": a.get("id"),
        "name": a.get("name"),
        "status": "ok",
        "currency": "TWD",
        "bars": rows,
        "quoteTime": a.get("quoteTime"),
        "text": "台銀實體金條塊今日掛牌：1 公斤 %s 元（每公克 %s 元）。" % (
            rows[0]["sellText"], rows[0]["perGramText"]) if rows else "",
    }
    return item


# ---------------------------------------------------------------- 觀察句（規則產生）


def build_observations(latest, hist, market, prev_close_report):
    """依固定規則產生白話觀察句。全部是客觀描述，不做任何買賣建議。"""
    notes = []
    assets = latest.get("assets") or {}

    # (1) 抓取失敗要先講，這是誠實鐵則
    failed = [a for a in assets.values() if a.get("status") != "ok"]
    for a in failed:
        notes.append({
            "level": "warn",
            "id": a.get("id"),
            "text": "%s 這次資料更新失敗（%s），下面的內容不包含它的今日數據。" % (
                a.get("name"), a.get("error") or "原因不明"),
        })

    prev_signals = (prev_close_report or {}).get("signalSnapshot") or {}

    for aid, a in assets.items():
        if a.get("status") != "ok" or a.get("price") is None:
            continue
        points = hist.get(aid) or []
        if len(points) < 2:
            continue
        r = ind.compute_all(points, a.get("price"))
        dec = a.get("decimals", 2)
        name = a.get("name")

        # (2) 接近 52 週高/低點
        rng = r.get("range")
        if rng and rng.get("full"):
            p = rng["percentile"]
            if p >= 90:
                notes.append({"level": "up", "id": aid, "text":
                    "%s 位於 52 週區間第 %.0f 百分位，接近一年來的高點"
                    "（區間 %s ～ %s）。" % (name, p, fmt(rng["low"], dec),
                                          fmt(rng["high"], dec))})
            elif p <= 10:
                notes.append({"level": "down", "id": aid, "text":
                    "%s 位於 52 週區間第 %.0f 百分位，接近一年來的低點"
                    "（區間 %s ～ %s）。" % (name, p, fmt(rng["low"], dec),
                                          fmt(rng["high"], dec))})

        # (3) RSI 超買/超賣（只描述指標，不建議動作）
        rsi = r.get("rsi14")
        if rsi is not None:
            if rsi >= 70:
                notes.append({"level": "up", "id": aid, "text":
                    "%s 的 RSI(14) 為 %.1f，進入一般所稱的超買區（70 以上）。"
                    "這只是指標描述，不代表該賣。" % (name, rsi)})
            elif rsi <= 30:
                notes.append({"level": "down", "id": aid, "text":
                    "%s 的 RSI(14) 為 %.1f，進入一般所稱的超賣區（30 以下）。"
                    "這只是指標描述，不代表該買。" % (name, rsi)})

        # (4) 今天漲跌幅較大
        pct = a.get("changePct")
        if pct is not None and abs(pct) >= 2:
            notes.append({"level": "up" if pct > 0 else "down", "id": aid,
                          "text": "%s 今天%s %s，變動幅度較大。" % (
                              name, move_word(pct), fmt_pct_abs(pct))})

        # (5) 站上／跌破均線（比較今天與昨天相對均線的位置）
        closes = ind.closes(points)
        if len(closes) >= 21:
            prev_price = closes[-2]
            for n, label in ((20, "20 日均線"), (60, "60 日均線")):
                today_ma = ind.ma(closes[:-1] + [a["price"]], n)
                prev_ma = ind.ma(closes[:-1], n)
                if today_ma is None or prev_ma is None:
                    continue
                was_above = prev_price >= prev_ma
                now_above = a["price"] >= today_ma
                if now_above and not was_above:
                    notes.append({"level": "up", "id": aid, "text":
                        "%s 今天站上 %s（均線 %s，現價 %s）。" % (
                            name, label, fmt(today_ma, dec), fmt(a["price"], dec))})
                elif was_above and not now_above:
                    notes.append({"level": "down", "id": aid, "text":
                        "%s 今天跌破 %s（均線 %s，現價 %s）。" % (
                            name, label, fmt(today_ma, dec), fmt(a["price"], dec))})

        # (6) 位置燈號與上一份收盤報告相比有變化
        prev_level = prev_signals.get(aid)
        cur = r["signal"]
        if prev_level and prev_level != cur["level"]:
            names = {"low": "相對低檔區", "mid": "中性", "high": "相對高檔區",
                     "unknown": "資料不足"}
            notes.append({"level": "info", "id": aid, "text":
                "%s 的位置燈號從「%s」變成「%s」（%s）。" % (
                    name, names.get(prev_level, prev_level), cur["label"], cur["rule"])})

    if not notes:
        notes.append({"level": "info", "text":
                      "今天沒有觸發任何觀察規則，各項指標都在平常範圍內。"})
    return notes


# ---------------------------------------------------------------- 表格區塊


def build_signal_table(latest, hist, market):
    """全部標的的燈號一覽表。多一欄「資料日期」，休市或抓取落後時一眼看得出來。"""
    today = market.get("date")
    rows = []
    assets = latest.get("assets") or {}
    for aid, a in assets.items():
        if a.get("type") == "bot_gold_bar":
            continue
        dec = a.get("decimals", 2)
        if a.get("status") != "ok" or a.get("price") is None:
            rows.append([{"text": a.get("name")}, {"text": "—"},
                         {"text": "更新失敗", "cls": "err"},
                         {"text": "—"}, {"text": "—"}])
            continue
        r = ind.compute_all(hist.get(aid) or [], a.get("price"))
        sig = r["signal"]
        rng = r.get("range")
        fresh = (a.get("date") == today)
        rows.append([
            {"text": a.get("name")},
            {"text": fmt(a.get("price"), dec)},
            {"text": fmt_pct(a.get("changePct")) if fresh else "—",
             "cls": ("up" if (a.get("changePct") or 0) > 0
                     else ("down" if (a.get("changePct") or 0) < 0 else "")) if fresh else ""},
            {"text": sig["label"], "cls": "sig-" + sig["level"]},
            {"text": (a.get("date") or "—") + ("" if fresh else "（非今日）"),
             "cls": "" if fresh else "stale"},
        ])
    return {
        "type": "table", "id": "signals", "title": "全部標的燈號一覽",
        "subtitle": "燈號只是把 52 週區間位置分成三段的客觀分類，不是買賣訊號。"
                    "「資料日期」不是今天，代表該標的今天沒有新報價。",
        "columns": ["標的", "價格", "今日漲跌", "位置燈號", "資料日期"],
        "align": ["left", "right", "right", "left", "right"],
        "rows": rows,
    }


def build_rank_table(latest, market):
    """今日漲跌排行。

    只收「資料日期就是今天」的標的——休市日的舊報價帶著的是上一個交易日的
    漲跌幅，把它列進「今日漲跌」會變成假的今日數據（誠實鐵則）。
    """
    today = market.get("date")
    items, stale = [], []
    for a in (latest.get("assets") or {}).values():
        if a.get("status") != "ok" or a.get("changePct") is None:
            continue
        if a.get("date") == today:
            items.append(a)
        else:
            stale.append(a)
    items.sort(key=lambda x: x["changePct"], reverse=True)

    rows = []
    for a in items:
        pct = a["changePct"]
        rows.append([
            {"text": a.get("name")},
            {"text": fmt(a.get("price"), a.get("decimals", 2))},
            {"text": fmt_pct(pct), "cls": "up" if pct > 0 else ("down" if pct < 0 else "")},
        ])

    sub = "各標的的計價幣別不同，這裡只比漲跌幅百分比。"
    if stale:
        sub += "　未列入：%s——它們今天沒有新報價（最新資料停在 %s），" \
               "顯示的漲跌會是上一個交易日的，不是今天的。" % (
                   "、".join(a.get("name") for a in stale),
                   "／".join(sorted(set(a.get("date") or "—" for a in stale))))
    return {
        "type": "table", "id": "rank", "title": "今日漲跌排行",
        "subtitle": sub,
        "columns": ["標的", "價格", "今日漲跌"],
        "align": ["left", "right", "right"],
        "rows": rows,
    }


# ---------------------------------------------------------------- 各時段報告


def quotes_section(latest, ids, sid, title, subtitle=None, compares=None):
    assets = latest.get("assets") or {}
    items = []
    for aid in ids:
        a = assets.get(aid)
        if not a:
            continue
        if a.get("type") == "bot_gold_bar":
            items.append(bar_item(a))
        else:
            cmp_ = (compares or {}).get(aid)
            items.append(quote_item(a, compare=cmp_))
    if not items:
        return None
    s = {"type": "quotes", "id": sid, "title": title, "items": items}
    if subtitle:
        s["subtitle"] = subtitle
    return s


def build_sections(slot, latest, hist, market, today_dir, prev_close_report):
    sections = []
    simplified = market["simplified"]

    tw_sub = {
        "pre": "台股尚未開盤，以下是上一個交易日的收盤價。",
        "open": "台股盤中即時報價。",
        "closed": "台股已收盤，以下是今日收盤價。",
        "holiday": "台股今日休市，以下是上一個交易日的收盤價。",
    }[market["twPhase"]]

    gold_sub = None if market["goldFresh"] else \
        "台銀今日尚未掛牌（或今日無掛牌），以下是最近一次掛牌的牌價。"

    if slot == "morning":
        sections.append(quotes_section(
            latest, INTL_IDS, "intl", "隔夜美股與國際行情",
            "台灣早上看到的美股是前一個交易日的收盤；比特幣則是 24 小時連續交易。"))
        sections.append(quotes_section(
            latest, BOT_IDS, "bot", "今早台銀金價與匯率掛牌", gold_sub))
        if not simplified:
            sections.append(quotes_section(latest, TW_IDS, "tw", "台股開盤狀況", tw_sub))
        else:
            sections.append({"type": "text", "id": "tw", "title": "台股",
                             "paragraphs": [market["twNote"] or "台股今日休市。"]})

    elif slot == "midday":
        morning = read_json(os.path.join(today_dir, "morning.json"), {}) or {}
        prev_q = morning.get("quoteSnapshot") or {}
        compares = {aid: ("今早", prev_q.get(aid)) for aid in TW_IDS if prev_q.get(aid)}
        sections.append(quotes_section(
            latest, TW_IDS, "tw", "台股盤中",
            tw_sub + ("　（已和今早的晨報做比較）" if compares else
                      "　（今天還沒有晨報可以比較）"),
            compares=compares))
        bot_cmp = {aid: ("今早", prev_q.get(aid)) for aid in BOT_IDS if prev_q.get(aid)}
        sections.append(quotes_section(
            latest, BOT_IDS, "bot", "金價與匯率",
            (gold_sub or "") + ("　掛牌時間 %s" % market["goldQuoteTime"]
                                if market["goldQuoteTime"] else ""),
            compares=bot_cmp))

    elif slot == "close":
        if not simplified:
            sections.append(quotes_section(latest, TW_IDS, "tw", "台股收盤總結", tw_sub))
        else:
            sections.append({"type": "text", "id": "tw", "title": "台股",
                             "paragraphs": [market["twNote"] or "台股今日休市。"]})
        sections.append(quotes_section(latest, BOT_IDS, "bot", "台銀金價與匯率", gold_sub))
        sections.append(quotes_section(latest, INTL_IDS, "intl", "國際行情"))
        sections.append(build_rank_table(latest, market))
        sections.append(build_signal_table(latest, hist, market))

    else:  # manual：全覽
        sections.append(quotes_section(latest, TW_IDS, "tw", "台股", tw_sub))
        sections.append(quotes_section(latest, BOT_IDS, "bot", "台銀金價與匯率", gold_sub))
        sections.append(quotes_section(latest, INTL_IDS, "intl", "國際行情"))
        sections.append(build_signal_table(latest, hist, market))

    title = {"morning": "今日觀察", "midday": "盤中觀察",
             "close": "今日變化提醒", "manual": "觀察"}[slot]
    sections.append({
        "type": "notes", "id": "observations", "title": title,
        "subtitle": "以下每一句都是依固定規則自動產生的客觀描述，不是投資建議。",
        "items": build_observations(latest, hist, market, prev_close_report),
    })

    return [s for s in sections if s]


# ---------------------------------------------------------------- 封存


def previous_close_report(date_str):
    """找出最近一份（不含今天）的收盤報告，用來比較燈號變化。"""
    idx = read_json(INDEX_FILE, {}) or {}
    for day in idx.get("days") or []:
        d = day.get("date")
        if not d or d >= date_str:
            continue
        for slot in ("close", "manual", "midday", "morning"):
            if slot in (day.get("slots") or []):
                r = read_json(os.path.join(ARCHIVE_DIR, d, "%s.json" % slot))
                if r:
                    return r
    return None


def update_index(date_str, slot, report, market):
    idx = read_json(INDEX_FILE, {}) or {}
    days = idx.get("days") or []
    entry = None
    for d in days:
        if d.get("date") == date_str:
            entry = d
            break
    if entry is None:
        entry = {"date": date_str, "slots": []}
        days.append(entry)

    if slot not in entry["slots"]:
        entry["slots"].append(slot)
    entry["slots"].sort(key=lambda s: ["morning", "midday", "close", "manual"].index(s)
                        if s in ("morning", "midday", "close", "manual") else 9)

    # 記下每份報告的產生時間，歷史頁才知道當天哪一份最新、要預設打開哪一份
    times = entry.get("slotTimes") or {}
    times[slot] = report.get("generatedAtText", "")[11:]   # 只留 HH:MM
    entry["slotTimes"] = times
    entry["twTradingDay"] = market.get("twTradingDay")
    entry["weekday"] = ["一", "二", "三", "四", "五", "六", "日"][
        datetime.strptime(date_str, "%Y-%m-%d").weekday()]
    entry["dataStatus"] = report.get("dataStatus")
    entry["updatedAt"] = report.get("generatedAt")

    days.sort(key=lambda d: d.get("date", ""), reverse=True)
    write_json(INDEX_FILE, {
        "updatedAt": report.get("generatedAt"),
        "count": len(days),
        "days": days,
    })


# ---------------------------------------------------------------- 進入點


def generate(slot, latest=None):
    """產生並封存一份報告。回傳報告 dict。"""
    now = now_tpe()
    latest = latest or read_json(os.path.join(DATA_DIR, "latest.json"), {}) or {}
    assets = latest.get("assets") or {}
    if not assets:
        raise RuntimeError("latest.json 沒有任何標的資料，無法產生報告")

    date_str = now.strftime("%Y-%m-%d")
    today_dir = os.path.join(ARCHIVE_DIR, date_str)
    market = detect_market(latest, now)

    hist = {aid: load_history(aid) for aid in assets}
    prev_close = previous_close_report(date_str)

    summary = latest.get("summary") or {}
    report = {
        "date": date_str,
        "slot": slot,
        "slotLabel": latest.get("slotLabel") or slot,
        "title": SLOT_TITLE.get(slot, slot),
        "generatedAt": now.isoformat(timespec="seconds"),
        "generatedAtText": now.strftime("%Y-%m-%d %H:%M"),
        "market": market,
        "dataStatus": {
            "total": summary.get("total"),
            "ok": summary.get("ok"),
            "error": summary.get("error"),
            "errorNames": [assets[i]["name"] for i in (summary.get("errorIds") or [])
                           if i in assets],
        },
        "sections": build_sections(slot, latest, hist, market, today_dir, prev_close),
        # 給之後的報告拿來做比較用（不對外顯示）
        "quoteSnapshot": {aid: a.get("price") for aid, a in assets.items()
                          if a.get("status") == "ok" and a.get("price") is not None},
        "signalSnapshot": {
            aid: ind.compute_all(hist.get(aid) or [], a.get("price"))["signal"]["level"]
            for aid, a in assets.items()
            if a.get("status") == "ok" and a.get("price") is not None
            and len(hist.get(aid) or []) >= 2
        },
        "disclaimer": DISCLAIMER,
    }

    write_json(os.path.join(today_dir, "%s.json" % slot), report)
    write_json(os.path.join(today_dir, "snapshot.json"), latest)
    write_json(REPORT_LATEST, report)
    update_index(date_str, slot, report, market)
    return report


if __name__ == "__main__":
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="產生某個時段的報告（用現有的 latest.json）")
    ap.add_argument("--slot", required=True,
                    choices=["morning", "midday", "close", "manual"])
    args = ap.parse_args()
    r = generate(args.slot)
    print("已產生 %s 報告：%s %s" % (r["slot"], r["date"], r["generatedAtText"]))
    for s in r["sections"]:
        n = len(s.get("items") or s.get("rows") or s.get("paragraphs") or [])
        print("  - [%s] %s（%d 項）" % (s["type"], s["title"], n))
