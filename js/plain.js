/*
 * plain.js — 「一眼看懂」的翻譯層（分析系列 A1-6 起；A1-7 推到全站）
 *
 * 原則（全文在 docs/ANALYSIS.md「呈現原則」）：
 *   1. 每個面向一個三態圖示＋一個狀態詞；圖示旁【永遠】印出判定規則與這次是哪個數字造成的。
 *      狀態詞用該面向自己的語言（位置：相對低檔區／中性／相對高檔區；匯率位置：偏便宜／中間／偏貴；
 *      成本：便宜／正常／偏貴；風險：平靜／正常／劇烈）。
 *   2. 不加總：幾個圖示並排、各自獨立；不顯示任何「幾個面向怎樣」的計數。
 *   3. 每個數字下面一行白話（這裡的模板產生）；資料不足就輸出「資料不足」，不是空字串。
 *   圖示只是狀態的圖像，狀態詞一定並列——色弱或不懂隱喻的人只看字也懂。
 *   風險與成本用天氣（晴、多雲、雨）；位置用色點，不用天氣——低檔不等於好，不該配一個晴天。
 *
 * 「資料不足」與「不適用」是兩回事：資料不足＝之後會有（虛線圓圈＋原因）；不適用＝這一類標的沒有這個面向。
 *
 * 純函式：不 fetch、不碰 DOM。測試：scripts/test_plain.html ＋ scripts/test_plain_js.py。
 */
(function (global) {
  'use strict';

  var NA = '資料不足';
  var NOT_APPLICABLE = '不適用';
  var UNAVAILABLE = '暫時讀不到';
  var UNAVAILABLE_NOTE = '分析資料暫時讀不到';

  function isNum(x) { return typeof x === 'number' && isFinite(x); }
  function n(x, d) {
    if (!isNum(x)) return '—';
    return x.toLocaleString('zh-TW', { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* 面向、狀態詞、規則句。
     位置的三個詞就是卡片燈號的三個詞（js/indicators.js 的 positionSignal，A1-5 釘住）；這裡只是抄一份給圖例與測試對照，
     真正顯示的字一律從燈號函式的輸出拿（lampState），不在這裡另外判定。
     趨勢、情緒、估值要等 A2、A3 才有資料，這裡先把詞定下來；它們的門檻到時候才定（目前沒有任何函式會產生這三個面向的狀態）。 */
  var ASPECTS = {
    position:   { name: '位置', icon: 'dot', words: { low: '相對低檔區', mid: '中性', high: '相對高檔區' },
                  rule: '規則：區間位置低於 25% 算相對低檔區、25%～75% 算中性、高於 75% 算相對高檔區' },
    fxPosition: { name: '匯率位置', words: { low: '偏便宜', mid: '中間', high: '偏貴' },
                  rule: '規則：5 年百分位低於 40% 算偏便宜、40–60% 算中間、高於 60% 算偏貴' },
    cost:       { name: '成本', words: { low: '便宜', mid: '正常', high: '偏貴' },
                  rule: '規則：比自己一年的中位數低一成以上算便宜、高一成以上算偏貴，其餘算正常' },
    risk:       { name: '風險', words: { low: '平靜', mid: '正常', high: '劇烈' },
                  rule: '規則：1 年波動比 5 年波動低兩成以上算平靜、高兩成以上算劇烈，其餘算正常' },
    valuation:  { name: '估值', words: { low: '偏便宜', mid: '中間', high: '偏貴' }, rule: '規則：A3 起才有' },
    trend:      { name: '趨勢', words: { low: '向下', mid: '持平', high: '向上' }, rule: '規則：A2 起才有' },
    mood:       { name: '情緒', words: { low: '冷', mid: '中性', high: '熱' }, rule: '規則：A3 起才有' }
  };
  /* 折溢價不跟中位數比，跟自己歷史的第 25／75 百分位比（折溢價會是負的，「高一成」這種比法不適用） */
  var PREMIUM_RULE = '規則：跟自己的歷史比——低於第 25 百分位＝便宜、25–75＝正常、高於第 75＝偏貴';

  /* 圖示：晴、多雲、雨（風險、成本、匯率位置）；色點（位置）；資料不足是一個虛線空圈。顏色跟著文字色，不靠紅綠分辨。 */
  var ICONS = {
    sun: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
         '<circle cx="12" cy="12" r="4.5"/><path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.2 5.2l1.8 1.8M17 17l1.8 1.8M5.2 18.8L7 17M17 7l1.8-1.8"/></svg>',
    cloud: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
           '<path d="M7 18.5h10a4 4 0 0 0 .6-7.96A5.5 5.5 0 0 0 7 9.6 4.5 4.5 0 0 0 7 18.5z"/></svg>',
    rain: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
          '<path d="M7 15h10a4 4 0 0 0 .6-7.96A5.5 5.5 0 0 0 7 6.1 4.5 4.5 0 0 0 7 15z"/><path d="M8.5 18l-1 3M12.5 18l-1 3M16.5 18l-1 3"/></svg>',
    dot: '<svg viewBox="0 0 24 24" width="22" height="22"><circle cx="12" cy="12" r="6.5" fill="currentColor"/></svg>',
    none: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="7" stroke-dasharray="3 3"/></svg>'
  };
  var ICON_OF = { low: 'sun', mid: 'cloud', high: 'rain' };
  var ICON_NAME = { sun: '晴', cloud: '多雲', rain: '雨', none: '沒有資料' };
  var DOT_NAME = { low: '綠色圓點', mid: '灰色圓點', high: '橘色圓點' };

  /* 一個狀態：key 是 low／mid／high（三態）、na（資料不足）、none（不適用）、error（分析資料暫時讀不到）。
     opts：word（位置用燈號自己的字）、rule（折溢價用自己的規則）、reason（資料不足或不適用的原因）、note（例如「累積中 3/20」）。 */
  function make(aspect, key, because, opts) {
    var a = ASPECTS[aspect], o = opts || {};
    var three = key === 'low' || key === 'mid' || key === 'high';
    var word = three ? (o.word || a.words[key]) : (key === 'none' ? NOT_APPLICABLE : (key === 'error' ? UNAVAILABLE : NA));
    var icon = three ? (a.icon === 'dot' ? 'dot' : ICON_OF[key]) : 'none';
    var text;
    if (three) text = because ? because + ' → ' + word : NA;
    else if (key === 'error') text = UNAVAILABLE_NOTE;
    else text = o.reason || (key === 'none' ? '這一類標的沒有這個面向' : NA);
    return { aspect: aspect, name: a.name, key: key, word: word, icon: icon,
             iconName: icon === 'dot' ? DOT_NAME[key] : ICON_NAME[icon],
             because: text, rule: o.rule || a.rule, note: o.note || '' };
  }

  /* 位置：燈號函式說什麼就是什麼。sig 是 Indicators.positionSignal 的輸出（level、label、rule），這裡不重算、不換字。 */
  function lampState(sig, because) {
    if (!sig || !ASPECTS.position.words[sig.level]) return make('position', 'na', '', { reason: (sig && sig.rule) || '' });
    return make('position', sig.level, because || sig.rule, { word: sig.label });
  }
  /* 匯率位置（換匯助手）：5 年百分位 */
  function fxPositionState(pct) {
    if (!isNum(pct)) return make('fxPosition', 'na', '');
    return make('fxPosition', pct < 40 ? 'low' : (pct <= 60 ? 'mid' : 'high'), '5 年百分位 ' + n(pct, 1) + '%');
  }
  /* 成本（跟自己的中位數比）：opts.what 是比的是什麼（預設即期價差）、opts.base 是跟什麼比（預設自己一年的中位數）、
     opts.rule 是規則句（存摺與條塊的中位數不是剛好一年，規則句要照實寫天數）、opts.reason 是資料不足的原因 */
  function costState(current, median, opts) {
    var o = opts || {};
    if (!isNum(current) || !isNum(median) || median <= 0) return make('cost', 'na', '', { rule: o.rule, reason: o.reason });
    var ratio = current / median;
    return make('cost', ratio < 0.9 ? 'low' : (ratio > 1.1 ? 'high' : 'mid'),
                (o.what || '即期價差') + ' ' + n(current, 2) + '%，' + (o.base || '自己一年的中位數') + ' ' + n(median, 2) + '%', { rule: o.rule });
  }
  /* 成本（折溢價）：跟自己歷史的第 25／75 百分位比；d.n 不到 d.min 個交易日就是資料不足（累積中 n/min） */
  function premiumState(d) {
    d = d || {};
    if (isNum(d.n) && isNum(d.min) && d.n < d.min) return accumulating('cost', d.n, d.min, PREMIUM_RULE, '折溢價的歷史還在累積，滿 ' + d.min + ' 個交易日才跟自己比');
    if (!isNum(d.pct) || !isNum(d.p25) || !isNum(d.p75)) return make('cost', 'na', '', { rule: PREMIUM_RULE, reason: d.reason || '' });
    return make('cost', d.pct < d.p25 ? 'low' : (d.pct > d.p75 ? 'high' : 'mid'),
                '折溢價' + (d.tag ? '（' + d.tag + '）' : '') + ' ' + n(d.pct, 2) + '%，自己歷史的第 25 百分位 ' + n(d.p25, 2) + '%、第 75 百分位 ' + n(d.p75, 2) + '%',
                { rule: PREMIUM_RULE });
  }
  function riskState(vol1y, vol5y, reason) {
    if (!isNum(vol1y) || !isNum(vol5y) || vol5y <= 0) return make('risk', 'na', '', { reason: reason || '' });
    var ratio = vol1y / vol5y;
    return make('risk', ratio < 0.8 ? 'low' : (ratio > 1.2 ? 'high' : 'mid'),
                '1 年波動 ' + n(vol1y, 1) + '%，5 年波動 ' + n(vol5y, 1) + '%');
  }
  /* 資料不足的三種說法：還在累積（之後會有）、沒有這個面向（不適用）、檔案暫時讀不到 */
  function accumulating(aspect, have, need, rule, reason) {
    return make(aspect, 'na', '', { rule: rule, reason: reason || '還在累積', note: '累積中 ' + have + '/' + need });
  }
  function insufficient(aspect, reason, rule) { return make(aspect, 'na', '', { reason: reason || '', rule: rule }); }
  function notApplicable(aspect, reason) { return make(aspect, 'none', '', { reason: reason || '' }); }
  function unavailable(aspect) { return make(aspect, 'error', ''); }

  function iconHtml(s) {
    return '<span class="glance-icon" role="img" aria-label="' + esc(s.iconName) + '">' + ICONS[s.icon] + '</span>';
  }
  /* 圖示＋狀態詞＋這次的數字＋規則，四樣一起出現；少一樣就不是合格的呈現。 */
  function badge(state, title) {
    var s = state || make('fxPosition', 'na', '');
    return '<div class="glance" data-aspect="' + esc(s.aspect) + '" data-state="' + esc(s.key) + '">' +
             (title ? '<div class="glance-title">' + esc(title) + '</div>' : '') +
             '<div class="glance-main">' + iconHtml(s) +
             '<span class="glance-word">' + esc(s.word) + '</span>' +
             (s.note ? '<span class="glance-note">' + esc(s.note) + '</span>' : '') + '</div>' +
             '<div class="glance-because">' + esc(s.because) + '</div>' +
             '<div class="glance-rule">' + esc(s.rule) + '</div>' +
           '</div>';
  }

  /* 白話模板：每個數字下面一行「這代表什麼」。輸入不齊就回「資料不足」。只說數字的意思，不說該怎麼做。 */
  function upDown(x, up, down) { return x >= 0 ? up : down; }
  function tiny(x) { return Math.abs(x) < 0.005; }
  function who(x) { return (x && (x.name || x.id)) || ''; }
  /* 相關係數印兩位小數；-0.004 這種四捨五入之後是 0 的，印 0.00，不印 -0.00 */
  function corr(x) {
    var v = Math.round(x * 100) / 100;
    return n(v === 0 ? 0 : v, 2);
  }
  var SENTENCES = {
    position: function (d) {
      if (!isNum(d.pct)) return NA;
      return '過去 ' + (d.years || 5) + ' 年裡，有 ' + n(d.pct, 0) + '% 的時候比現在便宜或一樣。';
    },
    /* 卡片燈號的位置：那是「最低到最高之間的哪裡」，不是「有幾成的日子比現在低」，兩句話不一樣 */
    lamp: function (d) {
      if (!isNum(d.pct)) return NA;
      var w = d.window || '這段期間';
      return (/^[0-9]/.test(w) ? '把 ' : '把') + w + '的最低價當 0、最高價當 100，現在在 ' + n(d.pct, 0) + ' 的位置。';
    },
    distance: function (d) {
      if (!isNum(d.fromLowPct) || !isNum(d.fromHighPct)) return NA;
      return '比一年內最低的時候高 ' + n(d.fromLowPct, 1) + '%，比一年內最高的時候低 ' + n(Math.abs(d.fromHighPct), 1) + '%。';
    },
    /* 價差：預設是換匯；kind 是 gold 時說的是黃金存摺（unit 是幣別，台幣那一本寫「元」） */
    spread: function (d) {
      if (!isNum(d.pct)) return NA;
      if (d.kind === 'gold') {
        var u = d.unit || '元';
        return '銀行掛的兩個價格差 ' + n(d.pct, 2) + '%：今天存進 100 ' + u + '的黃金、馬上再換回現金，大約會少 ' + n(d.pct, 2) + ' ' + u + '。';
      }
      return '銀行掛的兩個價格差 ' + n(d.pct, 2) + '%：每換 100 元，來回一趟大約有 ' + n(d.pct, 2) + ' 元是這個差。';
    },
    /* 現在的價差（或溢價）跟自己平常的水準比 */
    spreadCompare: function (d) {
      if (!isNum(d.current) || !isNum(d.median) || d.median <= 0) return NA;
      var diff = (d.current / d.median - 1) * 100;
      if (Math.abs(diff) < 1) return '跟自己平常的水準（中位數 ' + n(d.median, 2) + '%）差不多。';
      return '比自己平常的水準（中位數 ' + n(d.median, 2) + '%）' + upDown(diff, '高', '低') + ' ' + n(Math.abs(diff), 0) + '%。';
    },
    cash: function (d) {
      if (!isNum(d.pct)) return NA;
      return '拿實體鈔票換，比用帳戶裡的錢換貴 ' + n(d.pct, 2) + '%。';
    },
    quota: function (d) {
      if (!isNum(d.pct) || !isNum(d.ratioPct)) return NA;
      return '5 年百分位 ' + n(d.pct, 1) + '% 落在「' + (d.bucket || '') + '」那一檔，規則額度是預算池的 ' + n(d.ratioPct, 0) + '%' +
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
    /* 歷史模擬的結論：勝率、中位數、跟一次價差比、贏得多的那幾次是不是同一次事件、所以預設哪一種。全部由數字決定。 */
    backtestStory: function (d) {
      var th = d.thresholds || {};
      if (!isNum(d.winSharePct) || !isNum(d.medianImprovePct) || !isNum(th.winSharePct) || !isNum(th.medianImprovePct)) return NA;
      var s;
      if (d.tieSharePct === 100) {
        s = '歷史上依位置分批跟每個月換一樣多，每一個期間換到的匯率都一樣';
      } else {
        s = d.winSharePct >= th.winSharePct
          ? '歷史上依位置分批在大多數期間（' + n(d.winSharePct, 1) + '%）略勝'
          : '歷史上依位置分批只在 ' + n(d.winSharePct, 1) + '% 的期間換得比較便宜';
        if (d.medianImprovePct >= th.medianImprovePct) s += '，中位數多 ' + n(d.medianImprovePct, 2) + '%';
        else if (d.medianImprovePct > 0) s += '，但中位數只多 ' + n(d.medianImprovePct, 2) + '%';
        else s += '，中位數沒有比較便宜（' + n(d.medianImprovePct, 2) + '%）';
        if (isNum(d.spreadPct) && d.medianImprovePct > 0) {
          s += (d.medianImprovePct < d.spreadPct ? '，比換一次的價差（' : '，超過換一次的價差（') + n(d.spreadPct, 2) + (d.medianImprovePct < d.spreadPct ? '%）還小' : '%）');
        }
        var b = d.bigWins;
        if (b && isNum(b.n) && isNum(b.thresholdPct)) {
          var ev = b.events || [], big = '便宜 ' + n(b.thresholdPct, 1) + '% 以上';
          if (b.n === 0) s += '；沒有任何一個期間' + big;
          else if (b.n === 1) s += '；' + big + '的期間只有 1 個' + (ev[0] ? '（大筆換匯在 ' + ev[0].month + '）' : '');
          else if (ev.length === 1) s += '；贏得多的那 ' + b.n + ' 個期間（' + big + '），大筆換匯都落在同一次事件（' + ev[0].month + '），等於同一件事被重複算了 ' + b.n + ' 次';
          else s += '；贏得多的那 ' + b.n + ' 個期間（' + big + '），大筆換匯分散在 ' + ev.length + ' 個不同的月份';
        }
      }
      return s + (d.method === 'B' ? '。兩個門檻都過了，所以預設顯示依位置調整。' : '。所以預設顯示固定分批。');
    },
    drawdown: function (d) {
      if (!isNum(d.pct)) return NA;
      var p = Math.abs(d.pct);
      var how = p >= 80 ? '跌掉八成以上'
        : p >= 72 ? '跌掉大約四分之三'
        : p >= 60 ? '跌掉大約三分之二'
        : p >= 45 ? '跌掉大約一半'
        : p >= 38 ? '跌掉大約四成'
        : p >= 30 ? '跌掉大約三分之一'
        : p >= 23 ? '跌掉大約四分之一'
        : p >= 18 ? '跌掉大約五分之一'
        : '跌了 ' + n(p, 0) + '%';
      if (d.unknownRecovery) return '歷史上最慘的一次曾經' + how + '。';              // 只知道跌幅、不知道後來漲回了沒
      return '歷史上最慘的一次曾經' + how + (d.recoveredDate ? '，' + d.recoveredDate + ' 才漲回原來的高點' : '，到現在還沒漲回原來的高點') + '。';
    },
    /* 目前距高點：0 就是正在最高點 */
    currentDrawdown: function (d) {
      if (!isNum(d.pct)) return NA;
      if (tiny(d.pct)) return '現在就在這段資料的最高點' + (d.highDate ? '（' + d.highDate + '）' : '') + '。';
      return '比' + (d.highDate ? ' ' + d.highDate + ' 的' : '先前的') + '高點低 ' + n(Math.abs(d.pct), 1) + '%。';
    },
    /* 波動：有給 window（1 年／5 年）就說是用哪一段資料算的 */
    volatility: function (d) {
      if (!isNum(d.pct)) return NA;
      if (d.window) return '照最近 ' + d.window + '的起伏，一年裡漲跌 ' + n(d.pct, 0) + '% 左右算平常。';
      return '一年裡漲跌 ' + n(d.pct, 0) + '% 左右算平常；數字越大，價格越會大起大落。';
    },
    volCompare: function (d) {
      if (!isNum(d.vol1y) || !isNum(d.vol5y) || d.vol5y <= 0) return NA;
      var diff = (d.vol1y / d.vol5y - 1) * 100;
      if (Math.abs(diff) < 1) return '最近 1 年的起伏跟過去 5 年差不多。';
      return '最近 1 年的起伏比過去 5 年' + upDown(diff, '大', '小') + ' ' + n(Math.abs(diff), 0) + '%。';
    },
    /* 相關：一個標的一句（不是一格一句）。most＝最同向、least＝最沒有關係、inverse＝反向最強（沒有就是 null） */
    correlation: function (d) {
      var a = d.most, b = d.least, c = d.inverse;
      if (!a || !isNum(a.r) || !b || !isNum(b.r) || !who(a) || !who(b)) return NA;
      return '最常跟它同方向走的是 ' + who(a) + '（' + corr(a.r) + '）；走勢最沒有關係的是 ' + who(b) + '（' + corr(b.r) + '）；' +
             (c && isNum(c.r) && who(c) ? '常常跟它反方向的是 ' + who(c) + '（' + corr(c.r) + '）。' : '沒有哪一檔常常跟它反方向。');
    },
    /* 試算：跟現有標的最像的幾檔 */
    similar: function (d) {
      var top = (d.top || []).filter(function (x) { return x && isNum(x.r) && who(x); });
      if (!top.length) return NA;
      return '現有的標的裡，走勢跟它最像的是 ' + top.map(function (x) { return who(x) + '（' + corr(x.r) + '）'; }).join('、') + '。';
    },
    /* 追蹤差：年化的百分點；years 是視窗長度 */
    tracking: function (d) {
      if (!isNum(d.annualPct)) return NA;
      var y = d.years || 3;
      var head = y === 1 ? '過去 1 年的報酬' : '過去 ' + y + ' 年，平均每年的報酬';
      if (tiny(d.annualPct)) return head + '跟它追蹤的指數幾乎一樣。';
      return head + '比它追蹤的指數' + upDown(d.annualPct, '多', '少') + ' ' + n(Math.abs(d.annualPct), 2) + ' 個百分點。';
    },
    /* lead 是開頭（例如「一般的情況（中位數）」）；沒有就是講最新那一筆 */
    premium: function (d) {
      if (!isNum(d.pct)) return NA;
      var head = d.lead ? d.lead + '：' : '';
      if (tiny(d.pct)) return head + '成交價跟淨值幾乎一樣。';
      return head + (d.pct > 0 ? '成交價比淨值貴 ' + n(d.pct, 2) + '%（溢價）。' : '成交價比淨值便宜 ' + n(Math.abs(d.pct), 2) + '%（折價）。');
    },
    /* 折溢價在自己歷史裡排哪裡；還沒滿就說還在累積 */
    premiumRank: function (d) {
      if (isNum(d.n) && isNum(d.min) && d.n < d.min) return SENTENCES.accumulating(d);
      if (!isNum(d.pct) || !isNum(d.p25) || !isNum(d.p75)) return NA;
      var span = isNum(d.n) ? '自己過去 ' + d.n + ' 個交易日' : '自己的歷史';
      if (d.pct < d.p25) return '比' + span + '裡四分之三的日子都低。';
      if (d.pct > d.p75) return '比' + span + '裡四分之三的日子都高。';
      return '落在' + span + '中間那一半的範圍裡（' + n(d.p25, 2) + '%～' + n(d.p75, 2) + '%）。';
    },
    accumulating: function (d) {
      if (!isNum(d.n) || !isNum(d.min)) return NA;
      return '還在累積：' + d.n + '/' + d.min + ' 個交易日；滿 ' + d.min + ' 個交易日才有自己的歷史可以比。';
    },
    barPremium: function (d) {
      if (!isNum(d.pct)) return NA;
      if (tiny(d.pct)) return '同樣重量的黃金，' + (d.spec || '') + '條塊跟黃金存摺幾乎一樣價。';
      return '同樣重量的黃金，' + (d.spec || '') + '條塊比黃金存摺' + upDown(d.pct, '貴', '便宜') + ' ' + n(Math.abs(d.pct), 2) + '%' +
             (d.pct > 0 ? '（鑄造與加工的費用）' : '') + '。';
    },
    /* 拆解的殘差：kind 是 gold（黃金）、etf（00646）、cny（人民幣）；lead 是開頭（例如「一般的情況（中位數）」「平均每個月」） */
    residual: function (d) {
      if (!isNum(d.pct)) return NA;
      var head = d.lead ? d.lead + '：' : '';
      var x = Math.abs(d.pct);
      if (d.kind === 'gold') {
        return head + (tiny(d.pct) ? '台銀的牌價跟「國際金價 × 匯率」算出來的幾乎一樣。'
          : '台銀的牌價比「國際金價 × 匯率」算出來的' + upDown(d.pct, '高', '低') + ' ' + n(x, 2) + '%；這個差來自銀行的價差、期貨與現貨的價差，以及兩邊報價的時間不一樣。');
      }
      if (d.kind === 'etf') {
        return head + (tiny(d.pct) ? '00646 的報酬跟「指數 × 匯率」合起來算的幾乎一樣。'
          : '00646 的報酬比「指數 × 匯率」合起來算的' + upDown(d.pct, '多', '少') + ' ' + n(x, 2) + ' 個百分點；差在費用、股息與換匯的時間點。');
      }
      if (d.kind === 'cny') {
        return head + (tiny(d.pct) ? '人民幣兌台幣實際的變動，跟用美元換算出來的幾乎一樣。'
          : '人民幣兌台幣實際的變動，比用美元換算出來的' + upDown(d.pct, '多', '少') + ' ' + n(x, 2) + ' 個百分點。');
      }
      return head + (tiny(d.pct) ? '實際的數字跟公式算出來的幾乎一樣。' : '實際的數字比公式算出來的' + upDown(d.pct, '高', '低') + ' ' + n(x, 2) + '%。');
    },
    /* 人民幣兌台幣的變動拆成兩段 */
    fxLeg: function (d) {
      if (!isNum(d.total) || !isNum(d.usdTwd) || !isNum(d.usdCny)) return NA;
      function move(x) { return tiny(x) ? '幾乎沒動' : upDown(x, '漲了 ', '跌了 ') + n(Math.abs(x), 2) + '%'; }
      return '這段期間人民幣兌台幣' + move(d.total) + '：美元兌台幣' + move(d.usdTwd) + '，美元兌人民幣' + move(d.usdCny) +
             (d.usdCny < 0 && !tiny(d.usdCny) ? '（人民幣對美元變貴）' : (d.usdCny > 0 && !tiny(d.usdCny) ? '（人民幣對美元變便宜）' : '')) + '。';
    },
    /* 靜態成本：每年的費用率，或每次成交的稅率（kind 是 tax） */
    fee: function (d) {
      if (!isNum(d.pct)) return NA;
      if (d.kind === 'tax') {
        return d.pct === 0 ? '目前這一項是 0。' : '每 10,000 元的成交金額，這一項是 ' + n(d.pct * 100, 0) + ' 元。';
      }
      return '每放 10,000 元，一年大約有 ' + n(d.pct * 100, 0) + ' 元是這一項費用；已經算在淨值裡，不會另外扣。';
    },
    /* 集中度的三個事實：只在瀏覽器裡、用使用者自己的設定算；這裡只有句型，沒有任何數字 */
    concMax: function (d) {
      if (!isNum(d.pct) || !d.label) return NA;
      return '放最多的一類是「' + d.label + '」，佔全部的 ' + n(d.pct, 0) + '%。';
    },
    concTopTwo: function (d) {
      var labels = (d.labels || []).filter(function (x) { return x; });
      if (!isNum(d.pct) || !labels.length) return NA;
      return '最大的兩類（' + labels.join('＋') + '）合起來佔全部的 ' + n(d.pct, 0) + '%。';
    },
    concAligned: function (d) {
      if (!isNum(d.pct) || !d.proxy) return NA;
      return '跟收入來源的代理標的（' + d.proxy + '）常常同方向走的類別，合起來佔全部的 ' + n(d.pct, 0) + '%。';
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
    NA: NA, NOT_APPLICABLE: NOT_APPLICABLE, UNAVAILABLE: UNAVAILABLE, UNAVAILABLE_NOTE: UNAVAILABLE_NOTE,
    ASPECTS: ASPECTS, ICONS: ICONS, PREMIUM_RULE: PREMIUM_RULE, KINDS: Object.keys(SENTENCES),
    lampState: lampState, fxPositionState: fxPositionState, costState: costState, premiumState: premiumState, riskState: riskState,
    accumulating: accumulating, insufficient: insufficient, notApplicable: notApplicable, unavailable: unavailable,
    badge: badge, iconHtml: iconHtml, sentence: sentence, line: line
  };
})(window);
