/* =====================================================================
 * InvestWatch — 卡片要自己說「這是舊資料」
 *
 * 這一支只做一件事：給它 latest.json 裡的一個標的和「現在」，
 * 它回答卡片上該掛什麼標示。純函式：不碰 DOM、不發請求、不讀時鐘以外的任何東西，
 * 所以可以在測試頁裡餵假資料、假時間去驗（scripts/test_freshness_js.py）。
 *
 * 【資料新不新，不在這裡判斷。】那是 scripts/merge_latest.py 依 data/schedule.json
 * 算好、寫進每一項的 freshness 欄位的（比的是「上一個排定的更新時間」，不是「距今多久」——
 * 實體條塊一天只前進三次，用「幾小時內算新」去比，它每天都會被誤判成舊的）。
 * 規則只有一份，前端照著 freshness 顯示，不自己再算一次。
 *
 * 前端只補一件後端不知道的事：台銀與台股【週末本來就不掛牌、不開盤】。
 * 週六看到週五的牌價，那就是現在的牌價，不是舊資料——所以顯示中性的灰色，不是黃色警告。
 * 判斷的依據是「牌價自己的日期（a.date）是不是剛過去的那個星期五」，不是「最後成功抓取的時間」：
 * 抓取時間是機器什麼時候去問的（筆電週日晚上才醒，它就是週日），牌價日期才是這個數字是哪一天的。
 *
 * 已知限制：只認得週六、週日。平日的國定假日沒有假日表，照樣會顯示黃色——
 * 那是多提醒一次，不是說謊。
 * ===================================================================== */

(function (root) {
  'use strict';

  var BOT_TYPES = ['bot_gold', 'bot_gold_bar', 'bot_fx', 'finmind_fx'];   // 台銀的牌價：週末不掛牌
  var TW_TYPES = ['twse_index', 'twse_stock'];                            // 台股：週末不開盤
  var DAY_MS = 86400 * 1000;
  var TPE_MS = 8 * 3600 * 1000;      // 台北固定 UTC+8，沒有夏令時間

  /** 「現在」在台北是哪一天、星期幾。不管使用者的瀏覽器在哪個時區，一律換算成台北。 */
  function taipei(now) {
    var ms = now instanceof Date ? now.getTime() : new Date(now || Date.now()).getTime();
    if (isNaN(ms)) ms = Date.now();
    var shifted = new Date(ms + TPE_MS);
    return { ms: ms, date: shifted.toISOString().slice(0, 10), dow: shifted.getUTCDay() };
  }

  /** 今天是週六或週日時，回傳剛過去的那個星期五（YYYY-MM-DD）；平日回傳 null。 */
  function fridayBeforeThisWeekend(tp) {
    var back = tp.dow === 6 ? 1 : (tp.dow === 0 ? 2 : 0);
    if (!back) return null;
    return new Date(tp.ms + TPE_MS - back * DAY_MS).toISOString().slice(0, 10);
  }

  /** 2026-09-13T23:21:11+08:00 → 09-13 23:21。只切字串：那個時間本來就是台北時間，不要再換算一次。 */
  function shortTime(iso) {
    var m = /^\d{4}-(\d{2}-\d{2})T(\d{2}:\d{2})/.exec(String(iso || ''));
    return m ? m[1] + ' ' + m[2] : String(iso || '—');
  }

  /**
   * 回傳：
   *   badge            null 或 { kind: 'error' | 'weekend' | 'stale', text }
   *   hidePrice        true = 不要顯示價格數字
   *   carried          null 或一行小字（這一輪刻意沒有重抓）
   *   suppressDateNote true = 「今天還沒有新報價」那段黃色說明不要再出現（灰色標示已經講了）
   */
  function classify(a, now) {
    var out = { badge: null, hidePrice: false, carried: null, suppressDateNote: false };
    if (!a) return out;

    if (a.carriedOver) {
      out.carried = '這一輪未更新，沿用上次結果' +
        (a.carriedReason ? '（' + a.carriedReason + '）'
                         : (a.fetchedAt ? '（' + shortTime(a.fetchedAt) + ' 抓的）' : ''));
    }

    // 這一輪抓失敗：卡片本來就有紅框、「更新失敗」與「上次成功…僅供參考」，這裡不重複掛標示
    if (a.status !== 'ok') {
      out.hidePrice = true;
      return out;
    }

    // 從來沒有成功紀錄，或排程設定裡缺了這一邊：後端說「無法判斷」。不知道新舊的數字不該被當成現價
    if (a.freshness === 'error') {
      out.hidePrice = true;
      out.badge = { kind: 'error', text: '無法判斷這筆資料的新舊（沒有成功抓取的紀錄，或排程設定缺了這一邊），先不顯示價格' };
      return out;
    }

    var tp = taipei(now);
    var friday = fridayBeforeThisWeekend(tp);
    var isBot = BOT_TYPES.indexOf(a.type) >= 0;
    var isTw = TW_TYPES.indexOf(a.type) >= 0;
    if (friday && (isBot || isTw) && a.date === friday) {
      out.suppressDateNote = true;
      out.badge = {
        kind: 'weekend',
        text: isTw ? '週末不開盤，沿用週五 ' + a.date + ' 的收盤價'
                   : '週末不掛牌，沿用週五 ' + (a.quoteTime || a.date) + ' 的牌價'
      };
      return out;
    }

    if (a.freshness === 'stale') {
      out.badge = {
        kind: 'stale',
        text: '沿用 ' + shortTime(a.lastSuccessAt) + ' 的資料' +
              (a.date ? '（牌價日期 ' + a.date + '）' : '') + '——排定的更新沒有跑到'
      };
    }
    return out;
  }

  /* ---------------------------------------------------------- G8B：台銀黃金被擋時的國際換算估算
   * 估算（latest.json 裡 estimateOf 有值的那一項）不自成一張卡，只出現在它估算的那一項台銀黃金卡裡（Cowork 裁決 5(a)）。
   * 台銀最後一筆牌告落後超過 est.switchAfterTradingDays 個交易日（週一～五；國定假日不判斷）時，
   * 那張卡的走勢線、燈號、展開後的指標改用估算序列，並在畫面上寫出來。
   * 門檻由 scripts/fetch_data.py 從 data/assets.json 每輪抄進 latest.json（前端不另外抓 assets.json，首屏請求不變）；
   * scripts/analyze.py 的 trading_days_behind 是同一條規則（測試餵同一組日期釘住兩邊一樣）。 */

  /** lastDay（YYYY-MM-DD）之後、到「現在」的台北日期（含）為止有幾個週一～五；日期讀不懂回 null。 */
  function tradingDaysBehind(lastDay, now) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(lastDay || ''))) return null;
    var end = new Date(taipei(now).date + 'T00:00:00Z').getTime();
    var n = 0;
    for (var t = new Date(lastDay + 'T00:00:00Z').getTime() + DAY_MS; t <= end; t += DAY_MS) {
      var w = new Date(t).getUTCDay();
      if (w !== 0 && w !== 6) n++;
    }
    return n;
  }

  /** 這一項台銀黃金的國際換算估算（latest.json 裡 estimateOf 指向它的那一項）；沒有回 null。 */
  function estimateFor(a, latest) {
    var assets = (latest && latest.assets) || {};
    var found = null;
    Object.keys(assets).forEach(function (id) {
      if (a && assets[id] && assets[id].estimateOf === a.id) found = assets[id];
    });
    return found;
  }

  /**
   * 回傳 { est, lastDay, behind, switched }：
   *   est      估算那一項（沒有就是 null；條塊不做估算）
   *   lastDay  台銀最後一筆牌告的日期（更新失敗時取 lastGood.date）
   *   behind   落後幾個交易日
   *   switched true＝台銀這一輪失敗、落後超過門檻、估算這一輪是好的：指標改用估算序列
   * 門檻沒有抄過來（舊的 latest.json）就不切——寧可不切，不可以亂切。
   */
  function estimateView(a, latest, now) {
    var out = { est: null, lastDay: null, behind: null, switched: false };
    if (!a || a.type !== 'bot_gold') return out;
    var est = estimateFor(a, latest);
    if (!est) return out;
    out.est = est;
    out.lastDay = a.status === 'ok' ? (a.date || null) : ((a.lastGood || {}).date || null);
    out.behind = tradingDaysBehind(out.lastDay, now);
    var gate = est.switchAfterTradingDays;
    out.switched = a.status !== 'ok' && est.status === 'ok' && typeof gate === 'number' &&
                   typeof out.behind === 'number' && out.behind > gate && (est.points || 0) > 1;
    return out;
  }

  /**
   * 切到估算序列時，那一項「算指標用的那一份」：名稱、分組、type、id、單位照台銀那一項；狀態、現價、漲跌、日期、走勢線、點數換成估算的；
   * 標記 estimateView（估算那一項的 id、台銀最後的日期、落後幾天、白話要用的數字）——卡片、圖示列、分析頁的總覽都看這個標記照實寫。
   * 儀表板（js/app.js）與分析頁（js/analysis.js）共用這一份，兩頁的算法只有一個。
   */
  function estimateCard(a, ev) {
    var est = ev.est, v = {};
    Object.keys(a).forEach(function (k) { v[k] = a[k]; });
    ['status', 'price', 'change', 'changePct', 'prevClose', 'date', 'spark', 'points'].forEach(function (k) { v[k] = est[k]; });
    v.priceLabel = '國際換算估算';
    v.estimateView = { id: est.id, lastDay: ev.lastDay, behind: ev.behind, typicalErrorPct: est.typicalErrorPct,
                       backfillThrough: est.backfillThrough };
    return v;
  }

  var api = { classify: classify, taipei: taipei, fridayBeforeThisWeekend: fridayBeforeThisWeekend,
              shortTime: shortTime, tradingDaysBehind: tradingDaysBehind, estimateFor: estimateFor,
              estimateView: estimateView, estimateCard: estimateCard };
  root.Freshness = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : this);
