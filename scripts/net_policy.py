# -*- coding: utf-8 -*-
"""
net_policy.py — 分析系列共用的「哪些主機可以連」規則

停點 A0 把這套規則寫在探測腳本裡；A1 起正式的分析程式（scripts/analyze.py）也要用同一份，
所以抽出來放這裡，探測腳本改成 import。規則只有兩條、都很短，但它們是這個系列的底線：

  1. 白名單：不在 ALLOWED_HOSTS 裡的主機一律不送、連線都不發；轉址也要逐跳檢查。
  2. 台銀永遠拒絕：就算哪天有人手滑把 bot.com.tw 加進白名單，FORBIDDEN_HOST_PARTS 照樣擋。
     這個系列「不增加對台銀的任何請求」，台銀黃金 3 項是家用電腦的 fetch_data.py 在抓，跟這裡無關。

host_of() 看的是網址【真正會連到】的主機：https://好主機@壞主機/ 這種帶帳密的寫法看起來像好主機、
實際連到壞主機，一律當成「沒有主機」讓白名單拒絕。

測試：scripts/test_probe_analysis.py（TestHosts）與 scripts/test_analyze.py 都釘著這兩條。
"""
from urllib.parse import urlsplit

# 分析系列到目前為止用過、也只准用的主機。要加新主機：先照 A0 的方式實測、寫進 PLAN.md 第 7 章，再加進來。
ALLOWED_HOSTS = (
    "query1.finance.yahoo.com",
    "api.finmindtrade.com",
    "fred.stlouisfed.org",
    "www.multpl.com",
    "www.econ.yale.edu", "shillerdata.com", "img1.wsimg.com",       # 只在 multpl 失敗時才會用到，而且只做 HEAD
    "data.gov.tw", "ws.dgbas.gov.tw", "nstatdb.dgbas.gov.tw",
    "mis.twse.com.tw", "www.twse.com.tw", "info.tpex.org.tw",
)
FORBIDDEN_HOST_PARTS = ("bot.com.tw",)


class HostNotAllowed(Exception):
    pass


def host_of(url):
    """網址真正會連到的主機（小寫）；看不出來或帶帳密就回空字串。"""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return ""
    return (parts.hostname or "").lower()


def assert_host_allowed(url):
    """不准連就丟 HostNotAllowed；准連就回主機名。"""
    host = host_of(url)
    for bad in FORBIDDEN_HOST_PARTS:
        if bad in host:
            raise HostNotAllowed("這個系列不准對台銀發任何請求（%s）" % host)
    if host not in ALLOWED_HOSTS:
        raise HostNotAllowed("主機不在白名單裡，拒絕送出（%s）" % (host or "?"))
    return host


def response_hook(response, *args, **kwargs):
    """給 requests.Session 的 hooks['response'] 用：每一個回應（含轉址的每一跳）都檢查它的主機。
    轉址把請求帶去名單外的主機時，這裡會丟例外，整個請求就失敗——不會安靜地連過去。"""
    assert_host_allowed(response.url)
    for hop in getattr(response, "history", None) or ():
        assert_host_allowed(hop.url)
    return response
