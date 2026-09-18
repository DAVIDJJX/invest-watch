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

  var api = { classify: classify, taipei: taipei, fridayBeforeThisWeekend: fridayBeforeThisWeekend,
              shortTime: shortTime };
  root.Freshness = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : this);
