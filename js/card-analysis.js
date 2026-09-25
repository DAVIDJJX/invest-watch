/*
 * card-analysis.js — 儀表板卡片裡的「分析」摺疊區（分析系列 A1-5）
 *
 * 只做一件事：從 data/analysis/risk.json 與 cost.json 挑出【這一張卡】要顯示的事實，畫成幾行小表。
 * 有什麼顯示什麼、沒有的項目不顯示；每個數字旁邊都有資料標籤與資料日期；不算任何加總、沒有任何判斷。
 * 「目前距高點」畫一條小橫條：刻度從 0 到該標的的歷史最大回檔——那是位置，不是評分，旁邊仍印數字。
 * 橫條的資料日期是最後一根完成週棒那天，跟卡片上方的日線報價日期不是同一天，兩個日期並列印出來。
 *
 * 純函式：facts(asset, risk, cost) 回一個物件；render(facts) 回 HTML 字串。不 fetch、不碰 DOM。
 * 抓檔與掛進卡片的事在 js/app.js（第一次展開才抓）。
 * 測試：scripts/test_card_analysis.html ＋ scripts/test_card_analysis_js.py。
 */
(function (global) {
  'use strict';

  var INVERSE_THRESHOLD = -0.3;        // 「反向最強」：相關係數低於這個才顯示（分散與對沖是兩回事，分開列）
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
        note: ok ? (x.from + '～' + x.through) : (x.reason || '')
      });
    });
    var m = a.maxDrawdown;
    if (m && typeof m.pct === 'number') {
      out.items.push({
        key: 'maxdd', name: '最大回檔', valueText: pctText(m.pct), label: m.label || a.dataLabel, date: m.troughDate,
        note: '高點 ' + m.peakDate + '，低點 ' + m.troughDate + '，' + (m.recoveredDate ? m.recoveredDate + ' 回到高點' : '尚未回到高點')
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
    return {
      available: true, window: corr.window || '', label: corr.label || '',
      mostAligned: byR[0],                                   // r 最高
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
                     label: td.label || '估算', date: w.through, note: w.from + '～' + w.through + '，累計 ' + pctText(w.diffPct) });
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
                     label: l.tag === '預估' ? '估算' : '單一來源', date: l.d, note: acc });
      });
    } else if (p && p.status && p.status.indexOf('未接') === 0) {
      items.push({ key: 'premium', name: '折溢價', valueText: p.status, label: p.label || '', date: '', note: p.reason || '' });
    }
    var g = cost.goldSpread;
    if (id === 'gold_twd' && g && g.latest) {
      items.push({ key: 'spread', name: '存摺價差（本行賣出 − 本行買入）÷ 中價', valueText: pctText(g.latest.spreadPct),
                   label: g.label || '單一來源', date: g.latest.d,
                   note: g.summary && typeof g.summary.medianPct === 'number' ? g.summary.n + ' 天中位數 ' + pctText(g.summary.medianPct) : '' });
    }
    var b = cost.barPremium;
    if (id === 'gold_bar' && b && b.latest && b.latest.rows) {
      b.latest.rows.forEach(function (r) {
        items.push({ key: 'bar-' + r.spec, name: '條塊相對存摺溢價 ' + r.spec, valueText: pctText(r.premiumPct),
                     label: b.label || '單一來源', date: b.latest.d, note: '本站自行累積 ' + b.n + ' 天' });
      });
    }
    return items;
  }

  function facts(asset, risk, cost) {
    var id = asset && asset.id;
    var r = riskFacts(id, risk);
    var c = correlationFacts(id, risk && risk.correlation, risk && risk.assets);
    var k = costFacts(id, cost);
    return {
      id: id, quoteDate: (asset && asset.date) || '', risk: r, correlation: c, cost: k,
      any: !!(r || (c && c.available) || k.length),
      notInMatrix: r ? null : '不在相關矩陣（沒有週線長歷史）',
      generatedAt: (risk && risk.generatedAt) || ''
    };
  }

  /* ------------------------------------------------------------ 畫 */
  function tag(label, date) {
    return '<span class="ana-tag">' + esc(label || '—') + (date ? ' · ' + esc(date) : '') + '</span>';
  }
  function row(it) {
    return '<div class="ana-row"><span class="ana-k">' + esc(it.name) + '</span><span class="ana-v">' + esc(it.valueText) + '</span>' +
           tag(it.label, it.date) + (it.note ? '<span class="ana-note">' + esc(it.note) + '</span>' : '') + '</div>';
  }
  function bar(cu) {
    if (!cu || cu.fillPct === null) return '';
    return '<div class="ana-bar" role="img" aria-label="目前距高點 ' + esc(pctText(cu.pct)) + '，刻度 0 到歷史最大回檔 ' + esc(pctText(cu.scaleMaxPct)) + '">' +
             '<span class="ana-bar-fill" style="width:' + cu.fillPct.toFixed(1) + '%"></span></div>' +
           '<div class="ana-bar-scale"><span>0%</span><span>歷史最大回檔 ' + esc(pctText(cu.scaleMaxPct)) + '</span></div>';
  }

  function render(f) {
    if (!f) return '<p class="ana-empty">讀不到分析資料。</p>';
    if (!f.any) return '<p class="ana-empty">這個標的目前沒有分析項目。</p>';
    var h = '';
    if (f.risk) {
      h += '<div class="ana-group"><div class="ana-title">風險</div>';
      f.risk.items.forEach(function (it) { h += row(it); });
      var cu = f.risk.current;
      if (cu) {
        h += '<div class="ana-row"><span class="ana-k">目前距高點</span><span class="ana-v">' + esc(pctText(cu.pct)) + '</span>' +
             tag(cu.label, cu.dataThrough) + '<span class="ana-note">高點 ' + esc(cu.highDate || '—') + '</span></div>';
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
      h += '</div>';
    }
    if (f.cost.length) {
      h += '<div class="ana-group"><div class="ana-title">成本</div>';
      f.cost.forEach(function (it) { h += row(it); });
      h += '</div>';
    }
    h += '<div class="ana-foot">資料標籤：估算／單一來源／有對照／多來源一致；日期是資料自己的日期。這裡只有事實，沒有任何判斷。</div>';
    return h;
  }
  function corrRow(name, x, label) {
    return '<div class="ana-row"><span class="ana-k">' + esc(name) + '</span>' +
           '<span class="ana-v">' + esc(x.id) + (x.name ? '　' + esc(x.name) : '') + '　' + esc(rText(x.r)) + '</span>' +
           tag(label, x.through) + '<span class="ana-note">重疊 ' + esc(x.n) + ' 週</span></div>';
  }

  global.CardAnalysis = {
    facts: facts, render: render,
    riskFacts: riskFacts, correlationFacts: correlationFacts, costFacts: costFacts,
    INVERSE_THRESHOLD: INVERSE_THRESHOLD, PREMIUM_MIN_DAYS: PREMIUM_MIN_DAYS
  };
})(window);
