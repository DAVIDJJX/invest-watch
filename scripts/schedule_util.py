#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
schedule_util.py — 依 data/schedule.json 算出「應該更新的時間點」

為什麼要有這一支？
    要判斷一項資料算不算過期，最直覺的做法是看「距今幾分鐘」。但這在這個專案
    會誤判：實體金條塊在盤中的輕量更新會被刻意跳過（fetch_data.py 的
    handle_bot_gold_bar 遇到輕量模式且上次成功時會 raise SkipAsset），
    所以它一天只有 3 次會前進。用固定分鐘數去比，它每天都會被說成過期。

    正確的比法是比「上一個排定的更新時間」：
        週一 09:30 看實體金條塊 → 上一個排定點是週日 15:05，它那時抓到了 → 正常
        週一 10:40 看實體金條塊 → 上一個排定點是週一 10:05，沒抓到 → 真的有問題

    這支就是負責回答「上一個/下一個排定點是什麼時候」。
    merge_latest.py、之後的 watchdog、前端顯示都會用到它。

名詞：
    owner    誰負責抓：cloud（GitHub Actions）或 local（家用電腦）
    cadence  這一項走哪一種排程：
               "full"  只算 full.at 那幾個時間點
               "light" full.at 再聯集盤中時窗內每 everyMinutes 一個點
             預設值：該 owner 有 light 排程就用 light，沒有就用 full。
             個別標的可在 data/assets.json 用 cadence 欄位覆寫。

時區：一律 Asia/Taipei（UTC+8），不吃系統時區——雲端跑在 UTC，不釘死會全錯。
"""

import json
import os
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

TPE = timezone(timedelta(hours=8))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEDULE_FILE = os.path.join(ROOT, "data", "schedule.json")

# 往回／往前最多找幾天。往回 7 天是規格要求：再久遠的排定點拿來比也沒有意義，
# 那種程度的落後早就該由 watchdog 開 Issue，而不是靠 stale 標記。
MAX_LOOKBACK_DAYS = 7
MAX_LOOKAHEAD_DAYS = 7

_cache = {}


# --------------------------------------------------------------------------
# 讀設定
# --------------------------------------------------------------------------

def load_schedule(path=None):
    """讀 data/schedule.json。同一個路徑只讀一次，之後從快取拿。"""
    path = path or SCHEDULE_FILE
    if path not in _cache:
        with open(path, encoding="utf-8") as fh:
            _cache[path] = json.load(fh)
    return _cache[path]


def grace_minutes(schedule=None):
    sch = schedule if schedule is not None else load_schedule()
    return int(sch.get("graceMinutes", 30))


def default_cadence(owner, schedule=None):
    """這個 owner 底下沒特別指定的標的走哪一種排程。

    有 light 排程就用 light（大部分標的盤中每半小時會更新），
    沒有就用 full（例如目前的雲端，盤中輕量排程要等停點 5 才加）。
    """
    sch = schedule if schedule is not None else load_schedule()
    return "light" if (sch.get(owner) or {}).get("light") else "full"


CADENCES = ("full", "light")


def has_schedule(owner, schedule=None):
    """schedule.json 裡到底有沒有這個 owner 的排程區塊。

    這件事要單獨問，因為「算不出上一個排定點」有兩種完全不同的原因：
      設定漏了整個 owner  → 是設定錯誤，資料再舊都會被判成 fresh，必須大聲抱怨
      設定有但這一週沒排到 → 才是規格說的「還沒有任何排定時間過去」
    """
    sch = schedule if schedule is not None else load_schedule()
    conf = sch.get(owner)
    return bool(conf and (conf.get("full") or conf.get("light")))


def cadence_of(asset, schedule=None):
    """一個標的（data/assets.json 裡的一筆）實際走哪一種排程。

    cadence 只認 "full" 和 "light"。寫錯字（例如 "Light"）時退回該 owner 的預設值，
    【不是】退回 full——full 的排定點少很多，是比較寬鬆的那一邊，
    悄悄用寬鬆的規則會把真正過期的資料判成 fresh，正好踩到誠實鐵則。
    寫錯字這件事由 merge_latest.py 印成 ::warning::，不會被吃掉。
    """
    sch = schedule if schedule is not None else load_schedule()
    owner = asset.get("owner") or "cloud"
    want = asset.get("cadence")
    if want in CADENCES:
        return want
    return default_cadence(owner, sch)


def cadence_is_valid(asset):
    """assets.json 的 cadence 欄位有沒有寫錯（沒寫也算合法）。"""
    want = asset.get("cadence")
    return want is None or want in CADENCES


# --------------------------------------------------------------------------
# days / 時間字串解析
# --------------------------------------------------------------------------

def parse_days(spec):
    """把 cron 的星期寫法轉成集合。0=週日、1=週一 … 6=週六。

    支援 "*"、"0-6"、"1-5"、"1,3,5"，以及跨週末的 "5-1"（週五、六、日、一）。
    """
    if spec is None or spec in ("*", ""):
        return set(range(7))
    out = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a) % 7, int(b) % 7
            if a <= b:
                out.update(range(a, b + 1))
            else:
                # 跨過週末的寫法，例如 5-1 = 週五、六、日、一
                out.update(list(range(a, 7)) + list(range(0, b + 1)))
        else:
            out.add(int(part) % 7)
    return out


def cron_dow(day):
    """Python 的 weekday() 是週一=0，cron 是週日=0，這裡換算過去。"""
    return (day.weekday() + 1) % 7


def day_matches(spec, day):
    return cron_dow(day) in parse_days(spec)


def parse_hhmm(s):
    h, m = str(s).split(":")
    return int(h), int(m)


def _at(day, hhmm):
    h, m = parse_hhmm(hhmm)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TPE)


# --------------------------------------------------------------------------
# 核心：某一天有哪些排定時間點
# --------------------------------------------------------------------------

def expected_times(owner, cadence, day, schedule=None):
    """那一天所有排定的更新時間點（台北時間，已排序去重）。

    cadence="full"  → 只算 full.at
    cadence="light" → full.at 聯集 light 時窗內每 everyMinutes 一個點

    注意 full 與 light 各自有自己的 days：目前 full 是每天（含週末）、
    light 只有平日，所以週末只會剩下 full 那三個時間點。
    """
    sch = schedule if schedule is not None else load_schedule()
    conf = sch.get(owner) or {}
    if isinstance(day, datetime):
        day = day.date()
    elif not isinstance(day, date_cls):
        raise TypeError("day 要是 date 或 datetime")

    out = set()

    full = conf.get("full") or {}
    if full.get("at") and day_matches(full.get("days"), day):
        for hhmm in full["at"]:
            out.add(_at(day, hhmm))

    if cadence == "light":
        light = conf.get("light") or {}
        if light.get("from") and light.get("to") and day_matches(light.get("days"), day):
            step = int(light.get("everyMinutes") or 30)
            if step > 0:
                t = _at(day, light["from"])
                end = _at(day, light["to"])
                # 含結束時間：Windows 排程「09:00 起每 30 分鐘、持續 8 小時」
                # 最後一次就是落在 17:00，不含的話會少算一個點。
                while t <= end:
                    out.add(t)
                    t += timedelta(minutes=step)

    return sorted(out)


def last_expected(owner, cadence, now, schedule=None):
    """now（含）之前最後一個排定時間點；最多往回找 7 天，找不到回 None。

    「含 now」是刻意的：排程 10:05 觸發時，10:05 這個點就已經到期了。
    抓資料到寫檔中間那幾十秒的落差由 graceMinutes 吸收。
    """
    for back in range(MAX_LOOKBACK_DAYS + 1):
        day = (now - timedelta(days=back)).date()
        times = [t for t in expected_times(owner, cadence, day, schedule) if t <= now]
        if times:
            return times[-1]
    return None


def next_expected(owner, cadence, now, schedule=None):
    """now 之後下一個排定時間點；最多往前找 7 天，找不到回 None。"""
    for fwd in range(MAX_LOOKAHEAD_DAYS + 1):
        day = (now + timedelta(days=fwd)).date()
        times = [t for t in expected_times(owner, cadence, day, schedule) if t > now]
        if times:
            return times[0]
    return None


# --------------------------------------------------------------------------
# 過期判定
# --------------------------------------------------------------------------

def parse_iso(s):
    """把 latest.json / 分片裡的 ISO 時間字串轉成有時區的 datetime。

    沒有時區資訊的舊資料一律當成台北時間——這個專案從頭到尾只寫台北時間，
    當成 UTC 會憑空多出 8 小時的落差，把正常的資料判成過期。
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt.replace(tzinfo=TPE) if dt.tzinfo is None else dt


def freshness(last_success_at, now, owner, cadence, schedule=None):
    """回傳 (verdict, last_due, next_due)。

    verdict：
      "error"  lastSuccessAt 不存在 → 從來沒抓成功過。這不是「舊」，是「壞」。
      "fresh"  沒有任何排定時間點過去過 → 沒有理由說它舊。
      "fresh"  lastSuccessAt >= lastDue - graceMinutes
      "stale"  其餘
    """
    sch = schedule if schedule is not None else load_schedule()
    last_due = last_expected(owner, cadence, now, sch)
    next_due = next_expected(owner, cadence, now, sch)

    # lastSuccessAt 只接受 ISO 字串或已經有時區的 datetime。
    # 數字、字典、沒有時區的 datetime 一律當成「讀不懂」→ 視同從來沒成功過。
    # 直接拿去比大小會丟 TypeError，那會讓整支 merge 掛掉、latest.json 產不出來，
    # 網站上所有卡片都會消失——只因為某一項的時間欄位型別怪怪的。
    if isinstance(last_success_at, str):
        ok_at = parse_iso(last_success_at)
    elif isinstance(last_success_at, datetime):
        ok_at = last_success_at if last_success_at.tzinfo else \
            last_success_at.replace(tzinfo=TPE)
    else:
        ok_at = None
    if ok_at is None:
        return "error", last_due, next_due
    if last_due is None:
        return "fresh", last_due, next_due

    deadline = last_due - timedelta(minutes=grace_minutes(sch))
    return ("fresh" if ok_at >= deadline else "stale"), last_due, next_due


def iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None
