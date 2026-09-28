/*
 * plain.js — 「一眼看懂」的翻譯層（分析系列 A1-6 起；A1-7 會把它推到全站）
 *
 * 原則（全文在 docs/ANALYSIS.md「呈現原則」）：
 *   1. 每個面向一個三態圖示＋一個狀態詞；圖示旁【永遠】印出判定規則與這次是哪個數字造成的。
 *      狀態詞用該面向自己的語言（位置：偏便宜／中間／偏貴；成本：便宜／正常／偏貴；風險：平靜／正常／劇烈）。
 *   2. 不加總：幾個圖示並排、各自獨立；不顯示任何「幾個面向怎樣」的計數。
 *   3. 每個數字下面一行白話（這裡的模板產生）；資料不足就輸出「資料不足」，不是空字串。
 *   圖示只是狀態的圖像（晴、多雲、雨），狀態詞一定並列——色弱或不懂隱喻的人只看字也懂。
 *
 * 純函式：不 fetch、不碰 DOM。測試：scripts/test_plain.html ＋ scripts/test_plain_js.py。
 */
(function (global) {
  'use strict';

  var NA = '資料不足';

  function isNum(x) { return typeof x === 'number' && isFinite(x); }
  function n(x, d) {
    if (!isNum(x)) return '—';
    return x.toLocaleString('zh-TW', { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* 面向、狀態詞、規則句。趨勢與情緒要等 A2、A3 才有資料，這裡先把詞定下來；
     它們的門檻與圖示到時候才定（目前沒有任何函式會產生這兩個面向的狀態）。 */
  var ASPECTS = {
    position: { name: '位置', words: { low: '偏便宜', mid: '中間', high: '偏貴' },
                rule: '規則：5 年百分位低於 40% 算偏便宜、40–60% 算中間、高於 60% 算偏貴' },
    cost:     { name: '成本', words: { low: '便宜', mid: '正常', high: '偏貴' },
                rule: '規則：比自己一年的中位數低一成以上算便宜、高一成以上算偏貴，其餘算正常' },
    risk:     { name: '風險', words: { low: '平靜', mid: '正常', high: '劇烈' },
                rule: '規則：1 年波動比 5 年波動低兩成以上算平靜、高兩成以上算劇烈，其餘算正常' },
    trend:    { name: '趨勢', words: { low: '向下', mid: '持平', high: '向上' }, rule: '規則：A2 起才有' },
    mood:     { name: '情緒', words: { low: '冷', mid: '中性', high: '熱' }, rule: '規則：A3 起才有' }
  };

  /* 三個圖示：晴、多雲、雨；資料不足是一個空圈。顏色跟著文字色，不靠紅綠分辨。
     位置、成本、風險三個面向：低的那一態（偏便宜／便宜／平靜）配晴，高的那一態（偏貴／偏貴／劇烈）配雨。 */
  var ICONS = {
    sun: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
         '<circle cx="12" cy="12" r="4.5"/><path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.2 5.2l1.8 1.8M17 17l1.8 1.8M5.2 18.8L7 17M17 7l1.8-1.8"/></svg>',
    cloud: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
           '<path d="M7 18.5h10a4 4 0 0 0 .6-7.96A5.5 5.5 0 0 0 7 9.6 4.5 4.5 0 0 0 7 18.5z"/></svg>',
    rain: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
          '<path d="M7 15h10a4 4 0 0 0 .6-7.96A5.5 5.5 0 0 0 7 6.1 4.5 4.5 0 0 0 7 15z"/><path d="M8.5 18l-1 3M12.5 18l-1 3M16.5 18l-1 3"/></svg>',
    none: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="7" stroke-dasharray="3 3"/></svg>'
  };
  var ICON_OF = { low: 'sun', mid: 'cloud', high: 'rain', na: 'none' };
  var ICON_NAME = { sun: '晴', cloud: '多雲', rain: '雨', none: '沒有資料' };

  function make(aspect, key, because) {
    var a = ASPECTS[aspect];
    var word = key === 'na' ? NA : a.words[key];
    return { aspect: aspect, name: a.name, key: key, word: word, icon: ICON_OF[key], iconName: ICON_NAME[ICON_OF[key]],
             because: because ? because + ' → ' + word : NA, rule: a.rule };
  }

  function positionState(pct) {
    if (!isNum(pct)) return make('position', 'na', '');
    return make('position', pct < 40 ? 'low' : (pct <= 60 ? 'mid' : 'high'), '5 年百分位 ' + n(pct, 1) + '%');
  }
  function costState(current, median) {
    if (!isNum(current) || !isNum(median) || median <= 0) return make('cost', 'na', '');
    var ratio = current / median;
    return make('cost', ratio < 0.9 ? 'low' : (ratio > 1.1 ? 'high' : 'mid'),
                '即期價差 ' + n(current, 2) + '%，自己一年的中位數 ' + n(median, 2) + '%');
  }
  function riskState(vol1y, vol5y) {
    if (!isNum(vol1y) || !isNum(vol5y) || vol5y <= 0) return make('risk', 'na', '');
    var ratio = vol1y / vol5y;
    return make('risk', ratio < 0.8 ? 'low' : (ratio > 1.2 ? 'high' : 'mid'),
                '1 年波動 ' + n(vol1y, 1) + '%，5 年波動 ' + n(vol5y, 1) + '%');
  }

  /* 圖示＋狀態詞＋這次的數字＋規則，四樣一起出現；少一樣就不是合格的呈現。 */
  function badge(state, title) {
    var s = state || make('position', 'na', '');
    return '<div class="glance" data-aspect="' + esc(s.aspect) + '" data-state="' + esc(s.key) + '">' +
             (title ? '<div class="glance-title">' + esc(title) + '</div>' : '') +
             '<div class="glance-main"><span class="glance-icon" role="img" aria-label="' + esc(s.iconName) + '">' + ICONS[s.icon] + '</span>' +
             '<span class="glance-word">' + esc(s.word) + '</span></div>' +
             '<div class="glance-because">' + esc(s.because) + '</div>' +
             '<div class="glance-rule">' + esc(s.rule) + '</div>' +
           '</div>';
  }

  /* 白話模板：每個數字下面一行「這代表什麼」。輸入不齊就回「資料不足」。 */
  var SENTENCES = {
    position: function (d) {
      if (!isNum(d.pct)) return NA;
      return '過去 ' + (d.years || 5) + ' 年裡，有 ' + n(d.pct, 0) + '% 的時候比現在便宜或一樣。';
    },
    distance: function (d) {
      if (!isNum(d.fromLowPct) || !isNum(d.fromHighPct)) return NA;
      return '比一年內最低的時候高 ' + n(d.fromLowPct, 1) + '%，比一年內最高的時候低 ' + n(Math.abs(d.fromHighPct), 1) + '%。';
    },
    spread: function (d) {
      if (!isNum(d.pct)) return NA;
      return '銀行掛的兩個價格差 ' + n(d.pct, 2) + '%：每換 100 元，來回一趟大約有 ' + n(d.pct, 2) + ' 元是這個差。';
    },
    cash: function (d) {
      if (!isNum(d.pct)) return NA;
      return '拿實體鈔票換，比用帳戶裡的錢換貴 ' + n(d.pct, 2) + '%。';
    },
    quota: function (d) {
      if (!isNum(d.pct) || !isNum(d.ratioPct)) return NA;
      return '5 年百分位 ' + n(d.pct, 1) + '% 落在「' + (d.bucket || '') + '」那一檔，規則試算的本月額度是月預算的 ' + n(d.ratioPct, 0) + '%' +
             (d.ratioPct === 0 ? '，規則表這一檔寫的是觀望' : '') + '。';
    },
    backtest: function (d) {
      if (!isNum(d.n) || !d.n || !isNum(d.winSharePct) || !isNum(d.medianImprovePct)) return NA;
      if (d.tieSharePct === 100) {
        return '拿過去的資料重演 ' + d.n + ' 個 ' + (d.months || 36) + ' 個月的期間：兩種做法每一次都換到一樣的匯率，沒有差別。';
      }
      return '拿過去的資料重演 ' + d.n + ' 個 ' + (d.months || 36) + ' 個月的期間：依位置調整換得比較便宜的有 ' + n(d.winSharePct, 1) +
             '%，一般的情況（中位數）便宜 ' + n(d.medianImprovePct, 2) + '%，最差的一次反而貴 ' + n(Math.abs(d.worstImprovePct), 2) + '%。';
    },
    drawdown: function (d) {
      if (!isNum(d.pct)) return NA;
      var p = Math.abs(d.pct);
      var how = p >= 45 ? '跌掉大約一半' : (p >= 28 ? '跌掉大約三分之一' : (p >= 18 ? '跌掉大約五分之一' : '跌了 ' + n(p, 0) + '%'));
      return '歷史上最慘的一次曾經' + how + (d.recoveredDate ? '，' + d.recoveredDate + ' 才漲回原來的高點' : '，到現在還沒漲回原來的高點') + '。';
    },
    volatility: function (d) {
      if (!isNum(d.pct)) return NA;
      return '一年裡漲跌 ' + n(d.pct, 0) + '% 左右算平常；數字越大，價格越會大起大落。';
    }
  };
  function sentence(kind, data) {
    var f = SENTENCES[kind];
    if (!f) return NA;
    var s = f(data || {});
    return s ? s : NA;
  }
  function line(kind, data) {
    return '<div class="plain-line">' + esc(sentence(kind, data)) + '</div>';
  }

  global.Plain = {
    NA: NA, ASPECTS: ASPECTS, ICONS: ICONS,
    positionState: positionState, costState: costState, riskState: riskState,
    badge: badge, sentence: sentence, line: line
  };
})(window);
