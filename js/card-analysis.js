/*
 * card-analysis.js — 儀表板卡片裡的「分析」摺疊區（分析系列 A1-5）
 *
 * 只做一件事：從 data/analysis/risk.json 與 cost.json 挑出【這一張卡】要顯示的事實，畫成幾行小表。
 * 有什麼顯示什麼、沒有的項目不顯示；每個數字旁邊都有資料標籤與資料日期；不算任何加總、沒有任何判斷。
 * 「目前距高點」畫一條小橫條：刻度從 0 到該標的的歷史最大回檔——那是位置，不是評分，旁邊仍印數字。
 * 橫條的資料日期是最後一根完成週棒那天，跟卡片上方的日線報價日期不是同一天，兩個日期並列印出來。
 * A1-6：人民幣存摺卡多一列存摺價差（cost.json 的 goldSpreadCny）；人民幣匯率卡多一個「換匯助手 →」入口，連到分析分頁。
 * A1-7：每張卡的「面向」（位置、風險、成本）——aspects(asset, ctx) 決定這張卡的每個面向看哪個數字、是哪一個狀態，
 *       strip() 畫收合時就看得到的圖示列，detail() 畫點開後的數字、規則、白話。儀表板的卡片與分析分頁的總覽表用同一份。
 *       狀態的門檻與白話模板在 js/plain.js、位置的燈號在 js/indicators.js；這一支只負責「哪張卡的哪個面向看哪個數字」。
 *       摺疊區的每一列下面也多一行白話（plain.js 載入之後才有；沒載入就跟以前一樣只有數字）。
 *
 * 純函式：facts(asset, risk, cost) 回一個物件；render(facts) 回 HTML 字串。不 fetch、不碰 DOM。
 * 抓檔與掛進卡片的事在 js/app.js（首屏畫完之後才抓）。
 * 測試：scripts/test_card_analysis.html ＋ scripts/test_card_analysis_js.py；面向在 scripts/test_aspects.html ＋ scripts/test_aspects_js.py。
 */
(function (global) {
  'use strict';

  var INVERSE_THRESHOLD = -0.3;        // 「反向最強」：相關係數低於這個才顯示（分散與對沖是兩回事，分開列）
  // 同一個東西的兩個版本不互相當「最同向」：S&P 500 指數與它的總報酬版本（追蹤差用的基準）相關是 1.00，列出來沒有資訊，取下一個
  var SAME_THING = [['gspc', 'sp500tr']];
  function sameThing(a, b) {
    return SAME_THING.some(function (p) { return (p[0] === a && p[1] === b) || (p[0] === b && p[1] === a); });
  }
  var PREMIUM_MIN_DAYS = 20;           // 折溢價累積滿幾個交易日才有中位數（跟 analyze.py 一樣）

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function num(x, nd) {
    if (typeof x !== 'number' || isNaN(x)) return '—';
    return x.toLocaleString('zh-TW', { minimumFractionDigits: nd, maximumFractionDigits: nd });
  }
  function pctText(x) { return typeof x === 'number' ? num(x, 2) + '%' : '—'; }
  function rText(r) { return typeof r === 'number' ? r.toFixed(3) : '—'; }

  /* ------------------------------------------------------------ 風險：波動、最大回檔、目前距高點 */
  function riskFacts(id, risk) {
    var a = risk && risk.assets && risk.assets[id];
    if (!a) return null;
    var v = a.volatility || {};
    var out = { label: a.dataLabel, lastBar: a.lastBar, items: [], current: null };
    ['1y', '5y'].forEach(function (k) {
      var x = v[k] || {};
      var ok = typeof x.pct === 'number';
      out.items.push({
        key: 'vol' + k, name: '年化波動 ' + (k === '1y' ? '1 年' : '5 年'),
        valueText: ok ? pctText(x.pct) : '資料不足',
        label: x.label || a.dataLabel, date: ok ? x.through : (a.lastBar || ''),
        note: ok ? (x.from + '～' + x.through) : (x.reason || ''),
        plain: ['volatility', { pct: x.pct, window: k === '1y' ? '1 年' : '5 年' }]
      });
    });
    var m = a.maxDrawdown;
    if (m && typeof m.pct === 'number') {
      out.items.push({
        key: 'maxdd', name: '最大回檔', valueText: pctText(m.pct), label: m.label || a.dataLabel, date: m.troughDate,
        note: '高點 ' + m.peakDate + '，低點 ' + m.troughDate + '，' + (m.recoveredDate ? m.recoveredDate + ' 回到高點' : '尚未回到高點'),
        plain: ['drawdown', { pct: m.pct, recoveredDate: m.recoveredDate }]
      });
    }
    var c = a.currentDrawdown;
    if (c && typeof c.pct === 'number') {
      var scale = (m && typeof m.pct === 'number' && m.pct < 0) ? m.pct : null;
      out.current = {
        pct: c.pct, highDate: c.highDate, dataThrough: c.dataThrough, label: c.label || a.dataLabel,
        scaleMaxPct: scale,
        // 橫條的長度＝目前回檔 ÷ 歷史最大回檔（0～100%）；沒有最大回檔就不畫橫條、只印數字
        fillPct: scale === null ? null : Math.max(0, Math.min(100, Math.abs(c.pct) / Math.abs(scale) * 100))
      };
    }
    return out;
  }

  /* ------------------------------------------------------------ 相關：最同向、最不相關、反向最強 */
  function correlationFacts(id, corr, assets) {
    if (!corr || !corr.matrix || !corr.matrix[id]) return null;
    var row = corr.matrix[id], rows = [];
    Object.keys(row).forEach(function (other) {
      if (other === id) return;
      var cell = row[other] || {};
      if (typeof cell.r !== 'number') return;
      var name = assets && assets[other] && assets[other].name;
      rows.push({ id: other, name: name || '', r: cell.r, n: cell.n, through: cell.through || '' });
    });
    if (!rows.length) return { available: false, reason: '相關矩陣裡沒有可用的成對資料' };
    var byR = rows.slice().sort(function (x, y) { return y.r - x.r; });
    var byAbs = rows.slice().sort(function (x, y) { return Math.abs(x.r) - Math.abs(y.r); });
    var lowest = byR[byR.length - 1];
    var aligned = byR.filter(function (x) { return !sameThing(id, x.id); });
    if (!aligned.length) return { available: false, reason: '相關矩陣裡沒有可用的成對資料' };
    return {
      available: true, window: corr.window || '', label: corr.label || '',
      mostAligned: aligned[0],                               // r 最高（同一個東西的另一個版本不算）
      leastRelated: byAbs[0],                                // |r| 最接近 0
      strongestInverse: lowest.r < INVERSE_THRESHOLD ? lowest : null,   // r 低於 −0.3 才有
      threshold: INVERSE_THRESHOLD
    };
  }

  /* ------------------------------------------------------------ 成本：追蹤差、折溢價、存摺價差、條塊溢價 */
  function costFacts(id, cost) {
    var items = [];
    if (!cost) return items;
    var td = cost.trackingDifference && cost.trackingDifference.primary;
    if (id === 'tw00646' && td && td.available) {
      var w = (td.windows || {})['3y'];
      if (w && typeof w.annualizedDiffPct === 'number') {
        items.push({ key: 'tracking', name: '追蹤差（主口徑 ' + (td.benchmark || '') + '，3 年年化）', valueText: pctText(w.annualizedDiffPct),
                     label: td.label || '估算', date: w.through, note: w.from + '～' + w.through + '，累計 ' + pctText(w.diffPct),
                     plain: ['tracking', { annualPct: w.annualizedDiffPct, years: 3 }] });
      }
    }
    var p = cost.premium && cost.premium[id];
    if (p && p.latest && p.latest.length) {
      p.latest.forEach(function (l) {
        var s = (l.tag === '預估' ? p.estimated : p.official) || {};
        var acc = '';
        if (typeof s.n === 'number' && s.n < PREMIUM_MIN_DAYS) acc = '累積中 ' + s.n + '/' + PREMIUM_MIN_DAYS;
        else if (typeof s.medianPct === 'number') acc = s.n + ' 個交易日中位數 ' + pctText(s.medianPct);
        items.push({ key: 'premium-' + l.tag, name: '折溢價（' + l.tag + '）', valueText: pctText(l.premiumPct),
                     label: l.tag === '預估' ? '估算' : '單一來源', date: l.d, note: acc, plain: ['premium', { pct: l.premiumPct }] });
      });
    } else if (p && p.status && p.status.indexOf('未接') === 0) {
      items.push({ key: 'premium', name: '折溢價', valueText: p.status, label: p.label || '', date: '', note: p.reason || '' });
    }
    // 黃金存摺的價差：台幣那張看 goldSpread，人民幣那張看 goldSpreadCny（A1-6），算法一樣
    var g = id === 'gold_twd' ? cost.goldSpread : (id === 'gold_cny' ? cost.goldSpreadCny : null);
    if (g && g.latest) {
      items.push({ key: 'spread', name: '存摺價差（本行賣出 − 本行買入）÷ 中價', valueText: pctText(g.latest.spreadPct),
                   label: g.label || '單一來源', date: g.latest.d,
                   note: g.summary && typeof g.summary.medianPct === 'number' ? g.summary.n + ' 天中位數 ' + pctText(g.summary.medianPct) : '',
                   plain: ['spread', { pct: g.latest.spreadPct, kind: 'gold', unit: id === 'gold_cny' ? '人民幣' : '元' }] });
    }
    var b = cost.barPremium;
    if (id === 'gold_bar' && b && b.latest && b.latest.rows) {
      b.latest.rows.forEach(function (r) {
        items.push({ key: 'bar-' + r.spec, name: '條塊相對存摺溢價 ' + r.spec, valueText: pctText(r.premiumPct),
                     label: b.label || '單一來源', date: b.latest.d, note: '本站自行累積 ' + b.n + ' 天',
                     plain: ['barPremium', { pct: r.premiumPct, spec: r.spec }] });
      });
    }
    return items;
  }

  /* ------------------------------------------------------------ 連到分析分頁的入口（A1-6）：人民幣匯率卡連到換匯助手 */
  var LINKS = {
    fx_cny: [{ key: 'fx-helper', text: '換匯助手 →', href: 'analysis.html#fx',
               note: '人民幣現在的位置、換匯成本、分批表與歷史模擬' }]
  };

  function facts(asset, risk, cost) {
    var id = asset && asset.id;
    var r = riskFacts(id, risk);
    var c = correlationFacts(id, risk && risk.correlation, risk && risk.assets);
    var k = costFacts(id, cost);
    return {
      id: id, quoteDate: (asset && asset.date) || '', risk: r, correlation: c, cost: k,
      links: LINKS[id] || [],
      any: !!(r || (c && c.available) || k.length),
      notInMatrix: r ? null : '不在相關矩陣（沒有週線長歷史）',
      generatedAt: (risk && risk.generatedAt) || ''
    };
  }

  /* ------------------------------------------------------------ 畫 */
  function tag(label, date) {
    return '<span class="ana-tag">' + esc(label || '—') + (date ? ' · ' + esc(date) : '') + '</span>';
  }
  /* 每個數字下面一行白話：plain.js 載入了才印（儀表板是首屏畫完之後才載入它）；沒有就只有數字，跟 A1-5 一樣 */
  function plainLine(p) {
    if (!p || !global.Plain) return '';
    return global.Plain.line(p[0], p[1]);
  }
  function row(it) {
    return '<div class="ana-row"><span class="ana-k">' + esc(it.name) + '</span><span class="ana-v">' + esc(it.valueText) + '</span>' +
           tag(it.label, it.date) + (it.note ? '<span class="ana-note">' + esc(it.note) + '</span>' : '') + plainLine(it.plain) + '</div>';
  }
  function bar(cu) {
    if (!cu || cu.fillPct === null) return '';
    return '<div class="ana-bar" role="img" aria-label="目前距高點 ' + esc(pctText(cu.pct)) + '，刻度 0 到歷史最大回檔 ' + esc(pctText(cu.scaleMaxPct)) + '">' +
             '<span class="ana-bar-fill" style="width:' + cu.fillPct.toFixed(1) + '%"></span></div>' +
           '<div class="ana-bar-scale"><span>0%</span><span>歷史最大回檔 ' + esc(pctText(cu.scaleMaxPct)) + '</span></div>';
  }

  function linksHtml(links) {
    if (!links || !links.length) return '';
    return '<div class="ana-group ana-links">' + links.map(function (l) {
      return '<div class="ana-linkrow"><a class="ana-link" href="' + esc(l.href) + '">' + esc(l.text) + '</a>' +
             (l.note ? '<span class="ana-note">' + esc(l.note) + '</span>' : '') + '</div>';
    }).join('') + '</div>';
  }

  /* unavailable＝有分析檔沒抓到（A1-7）：照實寫「分析資料暫時讀不到」，不要讓人以為這個標的本來就沒有分析項目 */
  function render(f, unavailable) {
    if (!f) return '<p class="ana-empty">讀不到分析資料。</p>';
    var miss = unavailable ? '<p class="ana-empty ana-missing">分析資料暫時讀不到</p>' : '';
    if (!f.any) return (miss || '<p class="ana-empty">這個標的目前沒有分析項目。</p>') + linksHtml(f.links);
    var h = miss;
    if (f.risk) {
      h += '<div class="ana-group"><div class="ana-title">風險</div>';
      f.risk.items.forEach(function (it) { h += row(it); });
      var cu = f.risk.current;
      if (cu) {
        h += '<div class="ana-row"><span class="ana-k">目前距高點</span><span class="ana-v">' + esc(pctText(cu.pct)) + '</span>' +
             tag(cu.label, cu.dataThrough) + '<span class="ana-note">高點 ' + esc(cu.highDate || '—') + '</span>' +
             plainLine(['currentDrawdown', { pct: cu.pct, highDate: cu.highDate }]) + '</div>';
        h += bar(cu);
        h += '<div class="ana-dates">橫條與上面的風險數字：資料到 <b>' + esc(cu.dataThrough || '—') + '</b>（最後一根完成週棒）。' +
             '卡片上方的報價是 <b>' + esc(f.quoteDate || '—') + '</b> 的日線，兩個日期不一樣。</div>';
      }
      h += '</div>';
    } else if (f.notInMatrix) {
      h += '<div class="ana-group"><div class="ana-title">風險</div><div class="ana-empty">' + esc(f.notInMatrix) + '</div></div>';
    }
    var c = f.correlation;
    if (c && c.available) {
      h += '<div class="ana-group"><div class="ana-title">相關（' + esc(c.window) + '，週報酬，各自幣別）</div>';
      h += corrRow('最同向', c.mostAligned, c.label);
      h += corrRow('最不相關', c.leastRelated, c.label);
      if (c.strongestInverse) h += corrRow('反向最強', c.strongestInverse, c.label);
      h += plainLine(['correlation', { most: c.mostAligned, least: c.leastRelated, inverse: c.strongestInverse }]);   // 相關：一個標的一句
      h += '</div>';
    }
    if (f.cost.length) {
      h += '<div class="ana-group"><div class="ana-title">成本</div>';
      f.cost.forEach(function (it) { h += row(it); });
      h += '</div>';
    }
    h += linksHtml(f.links);
    h += '<div class="ana-foot">資料標籤：估算／單一來源／有對照／多來源一致；日期是資料自己的日期。這裡只有事實，沒有任何判斷。</div>';
    return h;
  }
  function corrRow(name, x, label) {
    return '<div class="ana-row"><span class="ana-k">' + esc(name) + '</span>' +
           '<span class="ana-v">' + esc(x.id) + (x.name ? '　' + esc(x.name) : '') + '　' + esc(rText(x.r)) + '</span>' +
           tag(label, x.through) + '<span class="ana-note">重疊 ' + esc(x.n) + ' 週</span></div>';
  }

  /* ============================================================ 面向（A1-7）：位置、風險、成本
   *
   * aspects(asset, ctx) 回一個陣列，一個面向一個物件：{ aspect, state, facts, extra, dates }。
   *   state 是 js/plain.js 做出來的狀態（圖示、狀態詞、這次是哪個數字、規則）；facts 是點開後要列的數字，每個數字帶一行白話。
   *   ctx：{ points, historyFailed, risk, cost, fx }——points 是日線歷史的點（data/history/<id>.json）；
   *        risk／cost／fx 是 data/analysis 的三個檔，null 表示那一檔讀不到（顯示「分析資料暫時讀不到」，不是資料不足）。
   * 三種「沒有狀態」要分清楚：資料不足＝之後會有（虛線圓圈＋原因）；不適用＝這一類標的沒有這個面向（卡片不畫、總覽表寫灰字）；
   * 暫時讀不到＝檔案沒抓到。幾個面向各看各的：這裡沒有任何把它們加起來或數一數的函式。
   * 之後 A2／A3 要加趨勢、情緒、估值：在 ASPECT_ORDER 加一個名字、在 BUILDERS 加一個函式，卡片與總覽表就會自己多一格。
   */
  var ASPECT_ORDER = ['position', 'risk', 'cost'];
  var COST_MIN_DAYS = PREMIUM_MIN_DAYS;          // 折溢價與條塊溢價：自己的歷史滿幾個交易日才跟自己比
  var COST_PREMIUM_TAG = '確定';                 // 折溢價的狀態看哪一個口徑：確定（收盤對官方淨值，資料標籤「單一來源」）；預估口徑並列在細節裡
  /* 這幾類標的沒有「比平常貴或便宜」可比的成本 → 成本這個面向不適用 */
  var COST_NOT_APPLICABLE = {
    index: '指數只是一個數字，不是可以直接交易的東西，沒有成本可比',
    stock: '股票的交易成本是固定費率，沒有「比平常貴或便宜」可比',
    crypto: '這裡的報價只是參考價，沒有固定的交易管道可以算價差',
    commodity: '這裡的報價是期貨的參考價，不是可以直接交易的東西，沒有成本可比'
  };
  var COST_NOT_APPLICABLE_DEFAULT = '這一類標的沒有「比平常貴或便宜」可比的成本';
  var FX_TYPES = { finmind_fx: true, bot_fx: true };

  function isNum(x) { return typeof x === 'number' && isFinite(x); }
  function median(xs) {
    var v = xs.filter(isNum).sort(function (a, b) { return a - b; });
    if (!v.length) return null;
    return v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2;
  }
  function fxOf(asset, fx) {
    var cs = (fx && fx.currencies) || {}, found = null;
    Object.keys(cs).forEach(function (k) { if (cs[k] && cs[k].assetId === asset.id) found = cs[k]; });
    return found;
  }
  function medianRule(span) {
    return '規則：比自己的中位數（' + span + '）低一成以上算便宜、高一成以上算偏貴，其餘算正常';
  }

  /* ---- 位置：沿用卡片的燈號（js/indicators.js 的 computeAll → positionSignal），這裡不重算 */
  function positionAspect(asset, ctx) {
    var Pl = global.Plain, I = global.Indicators;
    var out = { aspect: 'position', facts: [], extra: [], dates: '' };
    var points = ctx.points || [];
    if (asset.type === 'bot_gold_bar') {
      out.state = Pl.insufficient('position', '台銀不公布條塊的歷史牌價；本站自己累積的只有 ' + points.length + ' 個交易日，不夠算區間位置');
      return out;
    }
    if (asset.status !== 'ok') {
      out.state = Pl.insufficient('position', '這一項資料更新失敗，沒有現價可以算位置');
      return out;
    }
    if (ctx.historyFailed || !I) {
      out.state = Pl.insufficient('position', '歷史資料讀取失敗，無法計算區間位置');
      return out;
    }
    var ind = I.computeAll(points, asset.price);
    var r = ind.range;
    if (!r) { out.state = Pl.lampState(ind.signal); return out; }
    var first = I.windowStart(points, r.days);
    var win = I.windowName(r), since = I.windowSince(r, first);
    out.state = Pl.lampState(ind.signal, I.signalRule(r, first));
    out.facts.push({ term: '區間位置', k: win + '區間位置', v: '第 ' + r.percentile.toFixed(0) + ' 百分位', label: '日線', date: asset.date || '',
                     note: '最低 ' + num(r.low, asset.decimals || 0) + '、最高 ' + num(r.high, asset.decimals || 0) + (since ? '；' + since : ''),
                     plain: Pl.sentence('lamp', { pct: r.percentile, window: win }) });
    out.dates = '位置的資料日期：' + (asset.date || '—') + '（日線）。';
    if (FX_TYPES[asset.type]) out.extra.push(fxFiveYear(asset, ctx));
    return out;
  }
  /* 匯率卡的位置只掛 52 週燈號；5 年的位置是換匯助手那一套（偏便宜／中間／偏貴），在細節裡列一行並連過去 */
  function fxFiveYear(asset, ctx) {
    var Pl = global.Plain;
    var link = { href: 'analysis.html#fx', text: '見換匯助手' };
    if (ctx.fx === null) return { text: '5 年位置：' + Pl.UNAVAILABLE, link: link };
    var cur = fxOf(asset, ctx.fx);
    var p5 = cur && cur.percentiles && cur.percentiles['5y'];
    return { text: '5 年位置：' + Pl.fxPositionState(p5 && p5.pct).word, link: link };
  }

  /* ---- 風險：1 年波動相對 5 年波動 */
  function riskAspect(asset, ctx) {
    var Pl = global.Plain;
    var out = { aspect: 'risk', facts: [], extra: [], dates: '' };
    if (ctx.risk === null) { out.state = Pl.unavailable('risk'); return out; }
    var a = ctx.risk && ctx.risk.assets && ctx.risk.assets[asset.id];
    if (!a) { out.state = Pl.insufficient('risk', '沒有週線長歷史，算不出 1 年與 5 年的波動'); return out; }
    var v = a.volatility || {}, v1 = v['1y'] || {}, v5 = v['5y'] || {};
    out.state = Pl.riskState(v1.pct, v5.pct, (!isNum(v1.pct) ? v1.reason : v5.reason) || '波動的資料不足');
    [['1y', '1 年', v1], ['5y', '5 年', v5]].forEach(function (x) {
      var ok = isNum(x[2].pct);
      out.facts.push({ term: '年化波動', k: '年化波動 ' + x[1], v: ok ? pctText(x[2].pct) : '資料不足', label: x[2].label || a.dataLabel,
                       date: ok ? x[2].through : (a.lastBar || ''), note: ok ? (x[2].from + '～' + x[2].through) : (x[2].reason || ''),
                       plain: Pl.sentence('volatility', { pct: x[2].pct, window: x[1] }) });
    });
    out.facts.push({ k: '1 年相對 5 年', v: isNum(v1.pct) && isNum(v5.pct) && v5.pct > 0 ? num(v1.pct / v5.pct, 2) + ' 倍' : '資料不足',
                     label: a.dataLabel, date: a.lastBar || '', note: '', plain: Pl.sentence('volCompare', { vol1y: v1.pct, vol5y: v5.pct }) });
    var m = a.maxDrawdown;
    if (m && isNum(m.pct)) {
      out.facts.push({ term: '回檔', k: '最大回檔', v: pctText(m.pct), label: m.label || a.dataLabel, date: m.troughDate,
                       note: '高點 ' + m.peakDate + '，低點 ' + m.troughDate, plain: Pl.sentence('drawdown', { pct: m.pct, recoveredDate: m.recoveredDate }) });
    }
    var c = a.currentDrawdown;
    if (c && isNum(c.pct)) {
      out.facts.push({ term: '目前距高點', k: '目前距高點', v: pctText(c.pct), label: c.label || a.dataLabel, date: c.dataThrough,
                       note: '高點 ' + (c.highDate || '—'), plain: Pl.sentence('currentDrawdown', { pct: c.pct, highDate: c.highDate }) });
    }
    out.dates = '風險的資料日期：' + (a.lastBar || '—') + '（最後一根完成的週棒）；跟上面日線報價的日期不是同一天。';
    return out;
  }

  /* ---- 成本：存摺價差、條塊溢價、換匯價差跟自己的中位數比（±一成）；折溢價跟自己歷史的第 25／75 百分位比 */
  function costAspect(asset, ctx) {
    var Pl = global.Plain;
    var out = { aspect: 'cost', facts: [], extra: [], dates: '' };
    var id = asset.id, cost = ctx.cost;
    if (FX_TYPES[asset.type]) return fxCost(asset, ctx, out);
    if (asset.type === 'bot_gold' || asset.type === 'bot_gold_bar') {
      if (cost === null) { out.state = Pl.unavailable('cost'); return out; }
      return asset.type === 'bot_gold' ? goldCost(asset, cost || {}, out) : barCost(cost || {}, out);
    }
    // 其餘的要知道它是哪一類：類別（assetClass）寫在 risk.json 的每個標的上；有折溢價資料的就是 ETF
    var cls = ctx.risk && ctx.risk.assets && ctx.risk.assets[id] && ctx.risk.assets[id].assetClass;
    var p = cost && cost.premium && cost.premium[id];
    if (p || cls === 'index_etf' || cls === 'bond_etf') {
      if (cost === null) { out.state = Pl.unavailable('cost'); return out; }
      return premiumCost(asset, cost || {}, p, out);
    }
    if (!cls) {
      // 不知道是哪一類就不能說「不適用」：檔案沒讀到是暫時讀不到；讀到了但裡面沒有這個標的是資料不足
      out.state = (ctx.risk === null || cost === null) ? Pl.unavailable('cost')
                                                       : Pl.insufficient('cost', '分析檔裡還沒有這個標的，不知道它是哪一類');
      return out;
    }
    // 走到這裡：不是存摺、條塊、匯率，也不是 ETF → 這一類沒有可比的成本
    out.state = Pl.notApplicable('cost', COST_NOT_APPLICABLE[cls] || COST_NOT_APPLICABLE_DEFAULT);
    // 不適用＝沒有「比平常貴或便宜」可比的動態成本，不是沒有成本：靜態費用表裡有這個標的的項目就列出來
    out.facts = staticFacts(asset, ctx.staticCosts);
    if (out.facts.length) {
      out.factsTitle = '固定的費用（不隨時間變動，所以沒有狀態）';
      var checked = out.facts.map(function (f) { return f.date; }).filter(function (d) { return d; }).sort();
      out.dates = '費用的查核日期：' + (checked.length ? checked[checked.length - 1] : '—') + '（抄自官方公告，出處在分析分頁的靜態成本表）。';
    }
    return out;
  }
  /* 靜態費用表（data/analysis/static-costs.json）裡屬於這個標的的項目。數值是單一個百分比才寫白話；分級費率是一段文字，不是一個數字。
     儀表板不載這一檔（首屏之後只多五個請求），所以卡片上拿不到；分析分頁的總覽表有。 */
  function staticFacts(asset, sc) {
    var Pl = global.Plain;
    return ((sc && sc.entries) || []).filter(function (e) { return e && e.asset === asset.id; }).map(function (e) {
      var has = e.value !== null && e.value !== undefined;
      var single = has && /^\s*\d+(\.\d+)?\s*$/.test(String(e.value)) && /^%/.test(String(e.unit || ''));
      return { k: e.item || '', v: has ? e.value + (e.unit ? ' ' + e.unit : '') : '沒有查到', label: e.label || '官方公告', date: e.checkedOn || '',
               note: has ? (e.note || '') : (e.nullReason || ''),
               plain: single ? Pl.sentence('fee', { pct: parseFloat(e.value), kind: /稅/.test(String(e.item || '')) ? 'tax' : 'fee' }) : null };
    });
  }
  function goldCost(asset, cost, out) {
    var Pl = global.Plain;
    var g = asset.id === 'gold_cny' ? cost.goldSpreadCny : cost.goldSpread;
    var unit = asset.id === 'gold_cny' ? '人民幣' : '元';
    if (!g || !g.latest || !isNum(g.latest.spreadPct)) {
      out.state = Pl.insufficient('cost', (g && g.reason) || '沒有這一本存摺的價差資料');
      return out;
    }
    var s = g.summary || {};
    out.state = Pl.costState(g.latest.spreadPct, s.medianPct, { what: '存摺價差', base: '自己 ' + (s.n || '—') + ' 天的中位數', rule: medianRule((s.n || '—') + ' 天'),
                                                                 reason: '還沒有可以比的中位數' });
    out.facts.push({ term: '價差', k: '存摺價差', v: pctText(g.latest.spreadPct), label: g.label || '單一來源', date: g.latest.d,
                     note: '（本行賣出 − 本行買入）÷ 中價', plain: Pl.sentence('spread', { pct: g.latest.spreadPct, kind: 'gold', unit: unit }) });
    out.facts.push({ term: '中位數', k: '自己的中位數', v: isNum(s.medianPct) ? pctText(s.medianPct) : '資料不足', label: g.label || '單一來源', date: s.through || '',
                     note: isNum(s.medianPct) ? s.n + ' 天（' + s.from + '～' + s.through + '）' : '',
                     plain: Pl.sentence('spreadCompare', { current: g.latest.spreadPct, median: s.medianPct }) });
    out.dates = '成本的資料日期：' + g.latest.d + '（日線）。';
    return out;
  }
  function barCost(cost, out) {
    var Pl = global.Plain;
    var b = cost.barPremium;
    var rows = (b && b.latest && b.latest.rows) || [];
    var kg = rows.filter(function (r) { return r.grams === 1000; })[0];
    if (!b || !kg || !isNum(kg.premiumPct)) {
      out.state = Pl.insufficient('cost', (b && b.reason) || '沒有 1 公斤條塊的溢價資料');
      return out;
    }
    var days = b.n || 0;
    var med = median((b.daily || []).map(function (r) { return r.g1000Pct; }));
    if (days < COST_MIN_DAYS) {
      out.state = Pl.accumulating('cost', days, COST_MIN_DAYS, medianRule('滿 ' + COST_MIN_DAYS + ' 個交易日才比'),
                                  '條塊溢價是本站自己每天記的，滿 ' + COST_MIN_DAYS + ' 個交易日才跟自己比');
    } else {
      out.state = Pl.costState(kg.premiumPct, med, { what: '1 公斤條塊溢價', base: '自己 ' + days + ' 天的中位數', rule: medianRule(days + ' 天'),
                                                     reason: '還沒有可以比的中位數' });
    }
    out.facts.push({ term: '條塊溢價', k: '條塊溢價（1 公斤）', v: pctText(kg.premiumPct), label: b.label || '單一來源', date: b.latest.d,
                     note: '每公克單價 ÷ 黃金存摺的本行賣出 − 1；其他規格在下面的「分析」裡', plain: Pl.sentence('barPremium', { pct: kg.premiumPct, spec: '1 公斤' }) });
    out.facts.push({ k: '自己的歷史', v: days + ' 天', label: b.label || '單一來源', date: b.through || b.latest.d, note: '自 ' + (b.from || '—') + ' 起本站自己累積',
                     plain: days < COST_MIN_DAYS ? Pl.sentence('accumulating', { n: days, min: COST_MIN_DAYS })
                                                 : Pl.sentence('spreadCompare', { current: kg.premiumPct, median: med }) });
    out.dates = '成本的資料日期：' + b.latest.d + '（日線）。';
    return out;
  }
  function premiumCost(asset, cost, p, out) {
    var Pl = global.Plain;
    var latest = (p && p.latest) || [];
    if (!p || !latest.length) {
      out.state = Pl.insufficient('cost', p && p.status ? '折溢價這一輪沒有資料（' + p.status + '）' : '這一檔還沒有接折溢價的資料', Pl.PREMIUM_RULE);
      return out;
    }
    var off = latest.filter(function (l) { return l.tag === COST_PREMIUM_TAG; })[0];
    var s = p.official || {};
    out.state = Pl.premiumState({ pct: off && off.premiumPct, p25: s.p25Pct, p75: s.p75Pct, n: s.n, min: COST_MIN_DAYS, tag: COST_PREMIUM_TAG,
                                  reason: !off ? '還沒有「確定」口徑的折溢價'
                                        : (!isNum(s.n) ? '這一檔還沒有自己累積的折溢價歷史' : '分析檔裡沒有第 25／75 百分位') });
    latest.forEach(function (l) {
      out.facts.push({ term: '折溢價', k: '折溢價（' + l.tag + '）', v: pctText(l.premiumPct), label: l.tag === '預估' ? '估算' : '單一來源', date: l.d,
                       note: '成交 ' + num(l.price, 2) + '、淨值 ' + num(l.nav, 4), plain: Pl.sentence('premium', { pct: l.premiumPct }) });
    });
    out.facts.push({ k: '自己的歷史（' + COST_PREMIUM_TAG + '）', v: isNum(s.n) ? s.n + ' 個交易日' : '資料不足', label: s.label || '單一來源',
                     date: s.through || (off && off.d) || '', note: isNum(s.p25Pct) && isNum(s.p75Pct) ? '第 25 百分位 ' + pctText(s.p25Pct) + '、第 75 百分位 ' + pctText(s.p75Pct) : '',
                     plain: Pl.sentence('premiumRank', { pct: off && off.premiumPct, p25: s.p25Pct, p75: s.p75Pct, n: s.n, min: COST_MIN_DAYS }) });
    var td = cost.trackingDifference && cost.trackingDifference.primary;
    var w = asset.id === 'tw00646' && td && td.available && (td.windows || {})['3y'];
    if (w && isNum(w.annualizedDiffPct)) {
      out.facts.push({ term: '追蹤差', k: '追蹤差（3 年年化）', v: pctText(w.annualizedDiffPct), label: td.label || '估算', date: w.through,
                       note: '基準 ' + (td.benchmark || '') + '；不參與上面的狀態', plain: Pl.sentence('tracking', { annualPct: w.annualizedDiffPct, years: 3 }) });
    }
    out.dates = '成本的資料日期：' + latest.map(function (l) { return l.d + '（' + l.tag + '）'; }).join('、') + '。';
    return out;
  }
  function fxCost(asset, ctx, out) {
    var Pl = global.Plain;
    if (ctx.fx === null) { out.state = Pl.unavailable('cost'); return out; }
    var cur = fxOf(asset, ctx.fx);
    var sp = (cur && cur.spreads) || {}, spot = sp.spot || {}, m = sp.spotSpread1y || {};
    if (!cur || !isNum(spot.spreadPct)) {
      out.state = Pl.insufficient('cost', (spot && spot.reason) || '沒有這個幣別的即期價差資料');
      return out;
    }
    out.state = Pl.costState(spot.spreadPct, m.medianPct, { reason: m.reason || '還沒有可以比的中位數' });
    out.facts.push({ term: '價差', k: '即期價差', v: pctText(spot.spreadPct), label: sp.label || cur.label || '', date: spot.d,
                     note: '（即期賣出 − 即期買入）÷ 中價', plain: Pl.sentence('spread', { pct: spot.spreadPct }) });
    out.facts.push({ term: '中位數', k: '自己一年的中位數', v: isNum(m.medianPct) ? pctText(m.medianPct) : '資料不足', label: sp.label || cur.label || '', date: m.through || '',
                     note: isNum(m.medianPct) ? m.n + ' 個交易日（' + m.from + '～' + m.through + '）' : (m.reason || ''),
                     plain: Pl.sentence('spreadCompare', { current: spot.spreadPct, median: m.medianPct }) });
    out.extra.push({ text: '現鈔的價差、歷史模擬與分批表', link: { href: 'analysis.html#fx', text: '見換匯助手' } });
    out.dates = '成本的資料日期：' + spot.d + '（台銀每日牌價）。';
    return out;
  }

  var BUILDERS = { position: positionAspect, risk: riskAspect, cost: costAspect };

  function aspects(asset, ctx) {
    if (!global.Plain || !asset) return null;                     // 白話層沒載入就什麼都不畫（由呼叫的人顯示「分析資料暫時讀不到」）
    var c = ctx || {};
    return ASPECT_ORDER.map(function (name) {
      var a = BUILDERS[name](asset, c);
      a.name = a.state.name;
      return a;
    });
  }

  /* ---- 畫：圖示列（收合時就看得到）與點開後的細節 */
  function iconSpan(s) {
    return '<span class="asp-icon" role="img" aria-label="' + esc(s.iconName) + '">' + global.Plain.ICONS[s.icon] + '</span>';
  }
  function termLabel(f) {
    return f.term && global.Glossary ? global.Glossary.term(f.term, f.k) : esc(f.k);
  }
  function detailId(id, aspect) { return 'asp-' + String(id).replace(/[^A-Za-z0-9_-]/g, '') + '-' + aspect; }

  /* 一個面向的圖示：圖示＋面向名稱＋狀態詞（三樣並列；只有圖示不算數）。是按鈕，點了展開細節。 */
  function chip(a, id) {
    var s = a.state;
    return '<button type="button" class="asp" data-aspect="' + esc(a.aspect) + '" data-state="' + esc(s.key) + '" aria-expanded="false"' +
           ' aria-controls="' + esc(detailId(id, a.aspect)) + '" aria-label="' + esc(s.name + '：' + s.word + (s.note ? '（' + s.note + '）' : '') + '。點一下看數字與規則') + '"' +
           ' title="' + esc(s.because) + '">' +
           iconSpan(s) + '<span class="asp-name">' + esc(s.name) + '</span><span class="asp-word">' + esc(s.word) + '</span>' +
           (s.note ? '<span class="asp-note">' + esc(s.note) + '</span>' : '') + '</button>';
  }
  /* 細節：狀態、這次是哪個數字、規則（一定有），再來是每個數字與它的一行白話、資料日期。
     不適用的面向沒有圖示也沒有規則——這一類標的沒有這個面向，只寫原因。 */
  function detail(a) {
    var s = a.state, applies = s.key !== 'none';
    var h = '<div class="asp-head" data-state="' + esc(s.key) + '">' + (applies ? iconSpan(s) : '') + '<b class="asp-title">' + esc(s.name) + '：' + esc(s.word) + '</b>' +
            (s.note ? '<span class="asp-note">' + esc(s.note) + '</span>' : '') + '</div>';
    h += '<div class="asp-because">' + esc(s.because) + '</div>';
    if (applies) h += '<div class="asp-rule">' + esc(s.rule) + '</div>';
    if (a.factsTitle) h += '<div class="asp-facts-title">' + esc(a.factsTitle) + '</div>';
    (a.facts || []).forEach(function (f) {
      // plain 是 null＝這一項不是一個數字（例如分級費率的文字），不寫白話；其餘一律有一行，資料不足就寫資料不足
      h += '<div class="asp-fact"><span class="asp-k">' + termLabel(f) + '</span><span class="asp-v">' + esc(f.v) + '</span>' +
           tag(f.label, f.date) + (f.note ? '<span class="asp-fnote">' + esc(f.note) + '</span>' : '') +
           (f.plain === null ? '' : '<div class="plain-line">' + esc(f.plain || global.Plain.NA) + '</div>') + '</div>';
    });
    (a.extra || []).forEach(function (x) {
      h += '<div class="asp-extra">' + esc(x.text) + (x.link ? '（<a class="ana-link" href="' + esc(x.link.href) + '">' + esc(x.link.text) + '</a>）' : '') + '</div>';
    });
    if (a.dates) h += '<div class="asp-dates">' + esc(a.dates) + '</div>';
    return h;
  }
  /* 卡片的圖示列：不適用的面向不畫；每個圖示後面跟著它的細節（預設收合） */
  function strip(asset, list) {
    if (!list) return '<div class="asp-missing">' + esc((global.Plain && global.Plain.UNAVAILABLE_NOTE) || '分析資料暫時讀不到') + '</div>';
    var shown = list.filter(function (a) { return a.state.key !== 'none'; });
    var h = '<div class="asp-row" role="group" aria-label="各面向，各看各的">' + shown.map(function (a) { return chip(a, asset.id); }).join('') + '</div>';
    shown.forEach(function (a) {
      h += '<div class="asp-detail" id="' + esc(detailId(asset.id, a.aspect)) + '" data-aspect="' + esc(a.aspect) + '" hidden>' + detail(a) + '</div>';
    });
    if (shown.some(function (a) { return a.state.key === 'error'; })) h += '<div class="asp-missing">' + esc(global.Plain.UNAVAILABLE_NOTE) + '</div>';
    return h;
  }
  /* 總覽表的一格：圖示＋狀態詞；不適用是灰字、沒有圖示 */
  function cell(a) {
    var s = a.state;
    if (s.key === 'none') return '<span class="asp-cell" data-state="none"><span class="asp-word">' + esc(s.word) + '</span></span>';
    return '<span class="asp-cell" data-state="' + esc(s.key) + '">' + iconSpan(s) + '<span class="asp-word">' + esc(s.word) + '</span>' +
           (s.note ? '<span class="asp-note">' + esc(s.note) + '</span>' : '') + '</span>';
  }

  global.CardAnalysis = {
    facts: facts, render: render,
    riskFacts: riskFacts, correlationFacts: correlationFacts, costFacts: costFacts,
    INVERSE_THRESHOLD: INVERSE_THRESHOLD, PREMIUM_MIN_DAYS: PREMIUM_MIN_DAYS,
    // A1-7：面向
    aspects: aspects, strip: strip, chip: chip, detail: detail, cell: cell, detailId: detailId,
    ASPECT_ORDER: ASPECT_ORDER, COST_MIN_DAYS: COST_MIN_DAYS, COST_PREMIUM_TAG: COST_PREMIUM_TAG, COST_NOT_APPLICABLE: COST_NOT_APPLICABLE,
    SAME_THING: SAME_THING
  };
})(window);
