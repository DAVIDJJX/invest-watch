#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
indicators.py — 指標計算（Python 版）

這是 js/indicators.js 的對應實作，公式完全相同（PLAN.md 7.9）。
為什麼要兩份？
  * 網頁上的卡片是即時用瀏覽器算的（js/indicators.js）
  * 每天的報告是在抓完資料後用 Python 算的（本檔），要存成 JSON 封存起來
兩邊算出來的數字必須一致；scripts/check_indicators.py 會拿實際資料對答案。

誠實原則：資料不足時一律回 None，讓報告寫「—」或直接不提，
絕不用比較少的資料硬算出一個看起來很像樣的數字。
"""

import math

TRADING_DAYS_YEAR = 252   # 一年約幾個交易日


def closes(points):
    """從歷史點陣列取出收盤價序列（由舊到新）。"""
    out = []
    for p in points or []:
        c = (p or {}).get("c")
        if isinstance(c, (int, float)) and math.isfinite(c):
            out.append(float(c))
    return out


def ma(series, n):
    """簡單移動平均：最近 n 個收盤的平均。不足 n 點回 None。"""
    if not series or len(series) < n or n <= 0:
        return None
    return sum(series[-n:]) / n


def rsi(series, period=14):
    """RSI：近 period 日漲幅均值 G、跌幅均值 L，RSI = 100 - 100/(1+G/L)。

    L = 0 時 RSI = 100。資料不足 period + 1 點回 None。
    """
    if not series or len(series) < period + 1:
        return None
    gain = loss = 0.0
    for i in range(len(series) - period, len(series)):
        diff = series[i] - series[i - 1]
        if diff >= 0:
            gain += diff
        else:
            loss -= diff
    g, l = gain / period, loss / period
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + g / l)


def range_position(series, price):
    """52 週（約 252 個交易日）區間位置。

    資料不足一年時用手上全部的資料算，並回報實際用了幾個交易日，
    讓報告能誠實寫「近 N 個月區間」而不是謊稱 52 週。
    """
    if not series or price is None:
        return None
    window = series[-TRADING_DAYS_YEAR:]
    hi, lo = max(window), min(window)
    hi = max(hi, price)
    lo = min(lo, price)
    pct = 50.0 if hi == lo else (price - lo) / (hi - lo) * 100
    return {
        "high": hi,
        "low": lo,
        "percentile": pct,
        "days": len(window),
        "full": len(window) >= TRADING_DAYS_YEAR * 0.9,
    }


def bias(price, ma_value):
    """乖離率：(現價 - MA) / MA × 100。"""
    if price is None or not ma_value:
        return None
    return (price - ma_value) / ma_value * 100


def change_over(series, price, n):
    """近 n 個交易日漲跌%：(現價 - n 日前收盤) / n 日前收盤 × 100。"""
    if not series or len(series) < n + 1 or price is None:
        return None
    base = series[-1 - n]
    if not base:
        return None
    return (price - base) / base * 100


def volatility(series, n=30):
    """年化波動：日報酬標準差 × √252 × 100。僅供了解價格起伏大小。"""
    if not series or len(series) < n + 1:
        return None
    rets = []
    for i in range(len(series) - n, len(series)):
        if not series[i - 1]:
            continue
        rets.append(series[i] / series[i - 1] - 1)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS_YEAR) * 100


def position_signal(range_info):
    """位置燈號（PLAN 3.2）：只做狀態分類，不給任何買賣建議。"""
    if not range_info:
        return {"level": "unknown", "label": "資料不足",
                "rule": "歷史資料還不夠算出區間位置"}
    p = range_info["percentile"]
    window_text = "52 週" if range_info["full"] else \
        "近 %d 個月" % max(1, round(range_info["days"] / 21))
    rule = "位於%s區間第 %.0f 百分位" % (window_text, p)
    if p < 25:
        return {"level": "low", "label": "相對低檔區", "rule": rule + " → 低於 25%"}
    if p > 75:
        return {"level": "high", "label": "相對高檔區", "rule": rule + " → 高於 75%"}
    return {"level": "mid", "label": "中性", "rule": rule + " → 落在 25%～75% 之間"}


def compute_all(points, price=None):
    """一次算出一個標的的全部指標（對應 js 的 Indicators.computeAll）。"""
    series = closes(points)
    if price is None or not isinstance(price, (int, float)) or not math.isfinite(price):
        price = series[-1] if series else None

    # 現價視為序列的最後一點（盤中時比歷史檔更新）
    live = list(series)
    if price is not None and live:
        live[-1] = float(price)

    ma20 = ma(live, 20)
    ma60 = ma(live, 60)
    rng = range_position(live, price)

    return {
        "price": price,
        "count": len(live),
        "ma20": ma20,
        "ma60": ma60,
        "vsMa20": bias(price, ma20),
        "vsMa60": bias(price, ma60),
        "rsi14": rsi(live, 14),
        "range": rng,
        "signal": position_signal(rng),
        "change10d": change_over(live, price, 10),
        "volatility30": volatility(live, 30),
    }
