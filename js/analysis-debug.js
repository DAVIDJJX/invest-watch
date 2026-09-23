/*
 * analysis-debug.js — 分析系列 A1 的暫時檢視頁（A4 會用正式的面向卡片取代）
 *
 * 只做一件事：把 data/analysis/status.json、risk.json、decompose.json 用純表格列出來，
 * 每一格數字旁邊都帶資料日期與資料標籤。不算任何東西、不做任何判斷、不花時間做樣式。
 *
 * 最上面一定先顯示「分析上次成功時間」與最新錯誤：分析壞掉時要一眼看得出來，
 * 不能讓人以為下面的數字是新的（誠實資料鐵則）。
 *
 * 測試：scripts/test_analysis_debug.html 把假資料塞進 render()，再由 scripts/test_analysis_debug_js.py
 * 用無頭瀏覽器檢查表格有渲染、日期欄非空、頁面上沒有不該出現的字。
 */
(function () {
  'use strict';

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function num(x, nd) {
    if (typeof x !== 'number' || isNaN(x)) return '—';
    return x.toLocaleString('zh-TW', { minimumFractionDigits: nd, maximumFractionDigits: nd });
  }
  function pctText(x) { return typeof x === 'number' ? num(x, 2) + '%' : '—'; }
  function dateCell(d) { return '<td class="date">' + esc(d || '—') + '</td>'; }
  function table(headers, rows) {
    var h = '<table><thead><tr>' + headers.map(function (x) { return '<th>' + esc(x) + '</th>'; }).join('') + '</tr></thead><tbody>';
    rows.forEach(function (r) { h += '<tr>' + r.join('') + '</tr>'; });
    return h + '</tbody></table>';
  }
  function td(x) { return '<td>' + esc(x) + '</td>'; }
  function tdRaw(x) { return '<td>' + x + '</td>'; }

  /* ------------------------------------------------------------ 狀態 */
  function renderStatus(st) {
    var h = '<h2>分析狀態</h2>';
    if (!st) {
      return h + '<p class="warn">讀不到 data/analysis/status.json——分析還沒跑過，或檔案缺失。下面的數字若有，也不知道是幾時算的。</p>';
    }
    h += '<p>上次執行：<b class="date">' + esc(st.lastRun || st.generatedAt) + '</b>（時段 ' + esc(st.slot) + '，' + esc(st.mode) + '，對外請求 ' + esc(st.requests) + ' 次）</p>';
    if (st.ok) {
      h += '<p class="ok">全部產出：' + esc((st.produced || []).join('、')) + '</p>';
    } else {
      h += '<p class="warn">有錯，下面的數字可能是舊的或不完整：</p><ul>' +
        (st.errors || []).map(function (e) { return '<li class="warn">' + esc(e) + '</li>'; }).join('') + '</ul>';
      if (st.produced && st.produced.length) h += '<p>這次仍有產出：' + esc(st.produced.join('、')) + '</p>';
    }
    if (st.warnings && st.warnings.length) {
      h += '<ul>' + st.warnings.map(function (w) { return '<li class="muted">' + esc(w) + '</li>'; }).join('') + '</ul>';
    }
    var lh = st.longHistory || {};
    var ids = Object.keys(lh).sort();
    if (ids.length) {
      h += '<h3>週線長歷史（data/history-long）</h3>' + table(['標的', '週數', '最後一根', '這次做了什麼'], ids.map(function (id) {
        var e = lh[id];
        return [td(id), td(e.points), dateCell(e.lastDate), td(e.action)];
      }));
    }
    return h;
  }

  /* ------------------------------------------------------------ 風險 */
  function volCell(v) {
    if (!v) return td('—');
    if (typeof v.pct !== 'number') return '<td>資料不足<div class="muted">' + esc(v.reason || '') + '</div></td>';
    return '<td>' + num(v.pct, 2) + '%<div class="muted">' + esc(v.window) + '，至 <span class="date">' + esc(v.through) + '</span>　' + esc(v.label) + '</div></td>';
  }
  function renderRisk(rk) {
    var h = '<h2>風險（risk.json）</h2>';
    if (!rk) return h + '<p class="warn">讀不到 data/analysis/risk.json。</p>';
    h += '<p class="muted">產生時間 <span class="date">' + esc(rk.generatedAt) + '</span>。' +
      esc((rk.notes || []).join(' ')) + '</p>';
    var ids = Object.keys(rk.assets || {}).sort();
    h += table(['標的', '類別', '幣別', '最後完成週棒', '週數', '年化波動 1 年', '年化波動 5 年', '最大回檔', '目前距高點', '10 年視窗', '資料標籤'],
      ids.map(function (id) {
        var a = rk.assets[id];
        var dd = a.maxDrawdown || {};
        var cd = a.currentDrawdown || {};
        var ten = a.tenYearWindow || {};
        return [
          td(id + (a.name ? '　' + a.name : '')), td(a.assetClass), td(a.currency),
          '<td><span class="date">' + esc(a.lastBar) + '</span><div class="muted">' + esc(a.lastWeek) + '</div></td>',
          td(a.bars),
          volCell(a.volatility && a.volatility['1y']), volCell(a.volatility && a.volatility['5y']),
          '<td>' + pctText(dd.pct) + '<div class="muted">高點 <span class="date">' + esc(dd.peakDate) + '</span> → 低點 <span class="date">' + esc(dd.troughDate) + '</span>' +
            (dd.recoveredDate ? '，<span class="date">' + esc(dd.recoveredDate) + '</span> 回到高點' : '，尚未回到高點') + '</div></td>',
          '<td>' + pctText(cd.pct) + '<div class="muted">高點 <span class="date">' + esc(cd.highDate) + '</span>，資料至 <span class="date">' + esc(cd.asOf) + '</span></div></td>',
          td(ten.available ? '夠（' + ten.bars + ' 週）' : (ten.reason || '資料不足')),
          td(a.dataLabel)
        ];
      }));
    var c = rk.correlation;
    if (c && c.matrix) {
      h += '<h3>相關係數矩陣（' + esc(c.window) + '，週報酬，成對可用；格內小字是重疊週數）</h3>';
      var cids = c.ids || Object.keys(c.matrix);
      var rows = cids.map(function (a) {
        return [td(a)].concat(cids.map(function (b) {
          var cell = (c.matrix[a] || {})[b] || {};
          if (typeof cell.r !== 'number') return '<td class="muted">資料不足<div>' + esc(cell.n || 0) + ' 週</div></td>';
          return '<td title="' + esc((cell.from || '') + '～' + (cell.through || '')) + '">' + cell.r.toFixed(3) + '<div class="muted">n=' + esc(cell.n) + '</div></td>';
        }));
      });
      h += table([''].concat(cids), rows);
      h += '<p class="muted">' + esc(c.note || '') + '　資料標籤：' + esc(c.label) + '</p>';
    }
    if (rk.problems && rk.problems.length) {
      h += '<ul>' + rk.problems.map(function (p) { return '<li class="warn">' + esc(p) + '</li>'; }).join('') + '</ul>';
    }
    return h;
  }

  /* ------------------------------------------------------------ 拆解 */
  function summaryTable(title, s) {
    if (!s) return '';
    if (typeof s.n !== 'number' || !s.n) return '<p class="muted">' + esc(title) + '：資料不足</p>';
    return '<p>' + esc(title) + '（' + esc(s.window) + '，<span class="date">' + esc(s.from) + '</span>～<span class="date">' + esc(s.through) + '</span>）：' +
      '中位數 ' + pctText(s.medianPct) + '、平均 ' + pctText(s.meanPct) + '、最小 ' + pctText(s.minPct) + '、最大 ' + pctText(s.maxPct) + '</p>';
  }
  function renderDecompose(dc) {
    var h = '<h2>拆解（decompose.json）</h2>';
    if (!dc) return h + '<p class="warn">讀不到 data/analysis/decompose.json。</p>';
    h += '<p class="muted">產生時間 <span class="date">' + esc(dc.generatedAt) + '</span>。' + esc((dc.notes || []).join(' ')) + '</p>';

    var g = dc.gold;
    h += '<h3>台銀金價 ＝ 國際金價 × 匯率 ÷ 31.1035 ＋ 殘差</h3>';
    if (!g) {
      h += '<p class="warn">沒有黃金拆解。</p>';
    } else if (g.reason) {
      h += '<p class="warn">這一段沒算出來：' + esc(g.reason) + '</p>';
    } else {
      h += '<p>' + esc(g.formula) + '　資料標籤：' + esc(g.label) + '</p>';
      h += '<ul class="muted">' + (g.notes || []).map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>';
      if (g.live) {
        if (g.live.reason) {
          h += '<p class="muted">現在這一刻：' + esc(g.live.reason) + '</p>';
        } else {
          h += table(['現在這一刻', '黃金存摺（本行賣出）', '牌價日期', '國際金價', '金價日期', '匯率中價', '匯率日期', '公式值', '殘差'], [[
            td(g.live.label), td(num(g.live.goldSell, 0)), dateCell(g.live.goldDate), td(num(g.live.gcPrice, 2)), dateCell(g.live.gcDate),
            td(num(g.live.fxMid, 4)), dateCell(g.live.fxDate), td(num(g.live.implied, 2)), td(pctText(g.live.residualPct))]]);
          h += '<p class="muted">' + esc(g.live.gcNote || '') + '</p>';
        }
      }
      h += summaryTable('殘差（同一天口徑）', g.summarySameDay);
      h += summaryTable('殘差（前一日口徑）', g.summaryPrevDay);
      var daily = (g.daily || []).slice(-10).reverse();
      h += '<h4>最近 ' + daily.length + ' 天</h4>' + table(
        ['日期', '黃金存摺（本行賣出）', 'GC=F 同日', '同日公式值', '同日殘差', 'GC=F 前一日（日期）', '前一日公式值', '前一日殘差', '匯率中價（日期）'],
        daily.map(function (r) {
          return [dateCell(r.d), td(num(r.goldSell, 0)), td(num(r.gc, 2)), td(num(r.impliedSameDay, 2)), td(pctText(r.residualSameDayPct)),
            tdRaw(num(r.gcPrev, 2) + '（<span class="date">' + esc(r.gcPrevDate) + '</span>）'), td(num(r.impliedPrevDay, 2)), td(pctText(r.residualPrevDayPct)),
            tdRaw(num(r.fxMid, 4) + '（<span class="date">' + esc(r.fxDate) + '</span>）')];
        }));
    }

    var t = dc.tw00646;
    h += '<h3>00646 ＝ S&P 500 × 匯率 ＋ 殘差（月）</h3>';
    if (!t) {
      h += '<p class="warn">沒有 00646 拆解。</p>';
    } else if (t.reason) {
      h += '<p class="warn">這一段沒算出來：' + esc(t.reason) + '</p>';
    } else {
      h += '<p>' + esc(t.formula) + '　資料標籤：' + esc(t.label) + '</p>';
      h += '<ul class="muted">' + (t.notes || []).map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>';
      [['最近 12 個月', t.summaryLast12], ['全部', t.summaryAll]].forEach(function (pair) {
        var s = pair[1];
        if (!s || !s.months) { h += '<p class="muted">' + esc(pair[0]) + '：資料不足</p>'; return; }
        h += '<p>' + esc(pair[0]) + '（' + s.months + ' 個月，<span class="date">' + esc(s.from) + '</span>～<span class="date">' + esc(s.through) + '</span>）：' +
          '殘差累計 ' + pctText(s.residualSumPct) + '、平均 ' + pctText(s.residualMeanPct) + '、標準差 ' + pctText(s.residualStdPct) + '</p>';
      });
      var monthly = (t.monthly || []).slice(-12).reverse();
      h += '<h4>最近 ' + monthly.length + ' 個月</h4>' + table(
        ['月份', '00646（週棒日期）', 'S&P 500（週棒日期）', 'USD/TWD（日期）', '合成', '殘差'],
        monthly.map(function (r) {
          return [dateCell(r.month), tdRaw(pctText(r.r646Pct) + '（<span class="date">' + esc(r.d646) + '</span>）'),
            tdRaw(pctText(r.rGspcPct) + '（<span class="date">' + esc(r.dGspc) + '</span>）'),
            tdRaw(pctText(r.rFxPct) + '（<span class="date">' + esc(r.dFx) + '</span>）'), td(pctText(r.combinedPct)), td(pctText(r.residualPct))];
        }));
    }
    return h;
  }

  /* ------------------------------------------------------------ 組合 */
  function render(data, targets) {
    targets = targets || {};
    var s = targets.status || document.getElementById('status');
    var r = targets.risk || document.getElementById('risk');
    var d = targets.decompose || document.getElementById('decompose');
    if (s) s.innerHTML = renderStatus(data.status);
    if (r) r.innerHTML = renderRisk(data.risk);
    if (d) d.innerHTML = renderDecompose(data.decompose);
  }

  function fetchJSON(url) {
    return fetch(url, { cache: 'no-cache' }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).catch(function () { return null; });                 // 讀不到就交給 render 顯示「讀不到」，不吞成功
  }

  function boot() {
    Promise.all([fetchJSON('data/analysis/status.json'), fetchJSON('data/analysis/risk.json'), fetchJSON('data/analysis/decompose.json')])
      .then(function (all) { render({ status: all[0], risk: all[1], decompose: all[2] }); });
  }

  window.AnalysisDebug = { boot: boot, render: render, renderStatus: renderStatus, renderRisk: renderRisk,
                           renderDecompose: renderDecompose, esc: esc, num: num };
})();
