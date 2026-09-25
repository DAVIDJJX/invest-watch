/*
 * analysis.js — 「分析」分頁（A1-5）：狀態、風險總表（可排序）、相關矩陣（色階，數字為主）、成本、拆解、集中度、試算
 *
 * 只做一件事：把 data/analysis 裡的檔案（status／risk／decompose／cost／static-costs）用純表格列出來，
 * 每一格數字旁邊都帶資料日期與資料標籤。不算任何東西、不做任何判斷、不花時間做樣式。
 *
 * 最上面一定先顯示「分析上次成功時間」與最新錯誤：分析壞掉時要一眼看得出來，
 * 不能讓人以為下面的數字是新的（誠實資料鐵則）。
 *
 * 集中度（A1-2）：使用者按一下按鈕才從【私人】倉庫讀設定檔，在瀏覽器裡算三個事實、只顯示、不上傳、不寫進任何公開檔；
 * 算法、驗證、設定檔的鍵名全部在 js/concentration.js，這一支只拿算好的結果去畫，表單欄位的 id 也不用設定檔的鍵名。
 * 這一支永遠不 console.log 任何權重。
 *
 * 試算（A1-3）：結果在使用者自己的私人倉庫 adhoc/ 底下，按一下才讀：先讀 adhoc/index.json（1 個請求）列表，
 * 點某個代號才讀那一檔；沒有 index 時用 listDir 列目錄當備援。代號只出現在瀏覽器裡。
 *
 * 測試：scripts/test_analysis_debug.html 把假資料塞進 render()，再由 scripts/test_analysis_debug_js.py
 * 用無頭瀏覽器檢查表格有渲染、日期欄非空、頁面上沒有不該出現的字與鍵名。
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
  function dateSpan(d) { return '<span class="date">' + esc(d || '—') + '</span>'; }
  function table(headers, rows) {
    var h = '<table><thead><tr>' + headers.map(function (x) { return '<th>' + esc(x) + '</th>'; }).join('') + '</tr></thead><tbody>';
    rows.forEach(function (r) { h += '<tr>' + r.join('') + '</tr>'; });
    return h + '</tbody></table>';
  }
  function td(x) { return '<td>' + esc(x) + '</td>'; }

  /* 可排序的表（風險總表用）：表頭可以點；「資料不足」與「—」永遠排最後 */
  function sortableTable(headers, rows) {
    var h = '<div class="table-wrap"><table class="sortable"><thead><tr>' +
      headers.map(function (x, i) { return '<th class="sortable" data-col="' + i + '">' + esc(x) + '</th>'; }).join('') + '</tr></thead><tbody>';
    rows.forEach(function (r) { h += '<tr>' + r.join('') + '</tr>'; });
    return h + '</tbody></table></div>';
  }
  function cellKey(cell) {
    var t = (cell && cell.textContent || '').trim();
    if (!t || t === '—' || t.indexOf('資料不足') === 0) return { nan: true, num: null, text: t };
    var m = /^[-−+]?\d[\d,]*(\.\d+)?/.exec(t);
    if (m) return { nan: false, num: parseFloat(m[0].replace('−', '-').replace(/,/g, '')), text: t };
    return { nan: false, num: null, text: t };
  }
  function sortTable(table, col, dir) {
    var tbody = table.tBodies[0];
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (ra, rb) {
      var a = cellKey(ra.cells[col]), b = cellKey(rb.cells[col]);
      if (a.nan !== b.nan) return a.nan ? 1 : -1;
      if (a.nan) return 0;
      var c = (a.num !== null && b.num !== null) ? a.num - b.num : a.text.localeCompare(b.text, 'zh-Hant');
      return dir === 'desc' ? -c : c;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
    Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th, i) {
      th.classList.remove('asc', 'desc');
      if (i === col) th.classList.add(dir);
    });
  }
  function wireSorting(root) {
    Array.prototype.forEach.call((root || document).querySelectorAll('table.sortable th.sortable'), function (th) {
      th.addEventListener('click', function () {
        var table = th.closest('table');
        var col = parseInt(th.getAttribute('data-col'), 10);
        sortTable(table, col, th.classList.contains('desc') ? 'asc' : 'desc');
      });
    });
  }
  /* 相關矩陣的底色：負相關藍、接近 0 中性、正相關橙；顏色只是輔助，格內數字才是訊息 */
  function corrColor(r) {
    if (typeof r !== 'number') return 'transparent';
    var a = Math.min(1, Math.abs(r));
    if (a < 0.05) return 'transparent';
    var alpha = (0.12 + 0.5 * a).toFixed(2);
    return r < 0 ? 'rgba(91, 156, 248, ' + alpha + ')' : 'rgba(255, 159, 67, ' + alpha + ')';
  }
  function tdRaw(x) { return '<td>' + x + '</td>'; }
  function notesList(notes) {
    if (!notes || !notes.length) return '';
    return '<ul class="muted">' + notes.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>';
  }

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
    return '<td>' + num(v.pct, 2) + '%<div class="muted">' + esc(v.window) + '，至 ' + dateSpan(v.through) + '　' + esc(v.label) + '</div></td>';
  }
  function renderRisk(rk) {
    var h = '<h2>風險（risk.json）</h2>';
    if (!rk) return h + '<p class="warn">讀不到 data/analysis/risk.json。</p>';
    h += '<p class="muted">產生時間 ' + dateSpan(rk.generatedAt) + '。' + esc((rk.notes || []).join(' ')) + '</p>';
    var ids = Object.keys(rk.assets || {}).sort();
    h += sortableTable(['標的', '類別', '幣別', '最後完成週棒', '週數', '年化波動 1 年', '年化波動 5 年', '最大回檔', '目前距高點', '10 年視窗', '資料標籤'],
      ids.map(function (id) {
        var a = rk.assets[id];
        var dd = a.maxDrawdown || {};
        var cd = a.currentDrawdown || {};
        var ten = a.tenYearWindow || {};
        return [
          td(id + (a.name ? '　' + a.name : '')), td(a.assetClass), td(a.currency),
          '<td>' + dateSpan(a.lastBar) + '<div class="muted">' + esc(a.lastWeek) + '</div></td>',
          td(a.bars),
          volCell(a.volatility && a.volatility['1y']), volCell(a.volatility && a.volatility['5y']),
          '<td>' + pctText(dd.pct) + '<div class="muted">高點 ' + dateSpan(dd.peakDate) + ' → 低點 ' + dateSpan(dd.troughDate) +
            (dd.recoveredDate ? '，' + dateSpan(dd.recoveredDate) + ' 回到高點' : '，尚未回到高點') + '</div></td>',
          '<td>' + pctText(cd.pct) + '<div class="muted">高點 ' + dateSpan(cd.highDate) + '，資料至 ' + dateSpan(cd.dataThrough) + '</div></td>',
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
          if (typeof cell.r !== 'number') return '<td class="corr muted">資料不足<div>' + esc(cell.n || 0) + ' 週</div></td>';
          return '<td class="corr" style="background:' + corrColor(cell.r) + '">' + cell.r.toFixed(3) + '<div class="muted">n=' + esc(cell.n) + '</div></td>';
        }));
      });
      h += '<div class="matrix-wrap">' + table([''].concat(cids), rows) + '</div>';
      h += '<p class="matrix-legend"><span><i style="background:' + corrColor(-0.8) + '"></i>負相關（藍）</span>' +
           '<span><i style="background:' + corrColor(0) + '"></i>接近 0（中性）</span>' +
           '<span><i style="background:' + corrColor(0.8) + '"></i>正相關（橙）</span>' +
           '<span>格內數字才是訊息，顏色只是輔助；手機上整張可以左右捲動</span></p>';
      h += '<p class="muted">' + esc(c.note || '') + '　資料標籤：' + esc(c.label) + '</p>';
    }
    if (rk.problems && rk.problems.length) {
      h += '<ul>' + rk.problems.map(function (p) { return '<li class="warn">' + esc(p) + '</li>'; }).join('') + '</ul>';
    }
    return h;
  }

  /* ------------------------------------------------------------ 拆解 */
  function summaryLine(title, s) {
    if (!s) return '';
    if (typeof s.n !== 'number' || !s.n) return '<p class="muted">' + esc(title) + '：資料不足</p>';
    return '<p>' + esc(title) + '（' + esc(s.window) + '，' + dateSpan(s.from) + '～' + dateSpan(s.through) + '）：' +
      '中位數 ' + pctText(s.medianPct) + '、平均 ' + pctText(s.meanPct) + '、最小 ' + pctText(s.minPct) + '、最大 ' + pctText(s.maxPct) + '</p>';
  }
  function renderDecompose(dc) {
    var h = '<h2>拆解（decompose.json）</h2>';
    if (!dc) return h + '<p class="warn">讀不到 data/analysis/decompose.json。</p>';
    h += '<p class="muted">產生時間 ' + dateSpan(dc.generatedAt) + '。' + esc((dc.notes || []).join(' ')) + '</p>';

    var g = dc.gold;
    h += '<h3>台銀金價 ＝ 國際金價 × 匯率 ÷ 31.1035 ＋ 殘差</h3>';
    if (!g) {
      h += '<p class="warn">沒有黃金拆解。</p>';
    } else if (g.reason) {
      h += '<p class="warn">這一段沒算出來：' + esc(g.reason) + '</p>';
    } else {
      h += '<p>' + esc(g.formula) + '　資料標籤：' + esc(g.label) + '</p>' + notesList(g.notes);
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
      h += summaryLine('殘差（同一天口徑，GC=F 期貨）', g.summarySameDay);
      h += summaryLine('殘差（前一日口徑，GC=F 期貨）', g.summaryPrevDay);
      if (g.spot) {
        if (g.spot.reason) {
          h += '<p class="muted">現貨口徑：' + esc(g.spot.reason) + '</p>';
        } else {
          h += summaryLine('殘差（同一天口徑，現貨 ' + (g.spot.symbol || '') + '）', g.spot.summarySameDay);
          h += summaryLine('殘差（前一日口徑，現貨 ' + (g.spot.symbol || '') + '）', g.spot.summaryPrevDay);
          if (g.spot.basis) h += '<p>' + esc(g.spot.basis.note || '') + '　最近 60 日中位數：' + pctText(g.spot.basis.medianPct) + '</p>';
          h += notesList(g.spot.notes);
        }
      }
      var daily = (g.daily || []).slice(-10).reverse();
      h += '<h4>最近 ' + daily.length + ' 天</h4>' + table(
        ['日期', '黃金存摺（本行賣出）', 'GC=F 同日', '同日公式值', '同日殘差', 'GC=F 前一日（日期）', '前一日公式值', '前一日殘差', '匯率中價（日期）'],
        daily.map(function (r) {
          return [dateCell(r.d), td(num(r.goldSell, 0)), td(num(r.gc, 2)), td(num(r.impliedSameDay, 2)), td(pctText(r.residualSameDayPct)),
            tdRaw(num(r.gcPrev, 2) + '（' + dateSpan(r.gcPrevDate) + '）'), td(num(r.impliedPrevDay, 2)), td(pctText(r.residualPrevDayPct)),
            tdRaw(num(r.fxMid, 4) + '（' + dateSpan(r.fxDate) + '）')];
        }));
    }

    var t = dc.tw00646;
    h += '<h3>00646 ＝ S&P 500 × 匯率 ＋ 殘差（月）</h3>';
    if (!t) {
      h += '<p class="warn">沒有 00646 拆解。</p>';
    } else if (t.reason) {
      h += '<p class="warn">這一段沒算出來：' + esc(t.reason) + '</p>';
    } else {
      h += '<p>' + esc(t.formula) + '　資料標籤：' + esc(t.label) + '</p>' + notesList(t.notes);
      [['最近 12 個月', t.summaryLast12], ['全部', t.summaryAll]].forEach(function (pair) {
        var s = pair[1];
        if (!s || !s.months) { h += '<p class="muted">' + esc(pair[0]) + '：資料不足</p>'; return; }
        h += '<p>' + esc(pair[0]) + '（' + s.months + ' 個月，' + dateSpan(s.from) + '～' + dateSpan(s.through) + '）：' +
          '殘差累計 ' + pctText(s.residualSumPct) + '、平均 ' + pctText(s.residualMeanPct) + '、標準差 ' + pctText(s.residualStdPct) + '</p>';
      });
      var monthly = (t.monthly || []).slice(-12).reverse();
      h += '<h4>最近 ' + monthly.length + ' 個月</h4>' + table(
        ['月份', '00646（週棒日期）', 'S&P 500（週棒日期）', 'USD/TWD（日期）', '合成', '殘差'],
        monthly.map(function (r) {
          return [dateCell(r.month), tdRaw(pctText(r.r646Pct) + '（' + dateSpan(r.d646) + '）'),
            tdRaw(pctText(r.rGspcPct) + '（' + dateSpan(r.dGspc) + '）'),
            tdRaw(pctText(r.rFxPct) + '（' + dateSpan(r.dFx) + '）'), td(pctText(r.combinedPct)), td(pctText(r.residualPct))];
        }));
    }
    return h;
  }

  /* ------------------------------------------------------------ 成本 */
  function tdWindow(w) {
    if (!w) return td('—');
    if (w.reason) return '<td>資料不足<div class="muted">' + esc(w.reason) + '</div></td>';
    return '<td>累計 ' + pctText(w.diffPct) + '，年化 ' + pctText(w.annualizedDiffPct) +
      '<div class="muted">00646 ' + pctText(w.r646Pct) + '、基準（台幣）' + pctText(w.rBenchTwdPct) + '；' + dateSpan(w.from) + '～' + dateSpan(w.through) + '（' + esc(w.weeks) + ' 週）</div></td>';
  }
  function renderPremium(id, p) {
    var h = '<h4>' + esc(id) + '</h4>';
    if (!p) return h + '<p class="muted">沒有資料。</p>';
    h += '<p>狀態：<b>' + esc(p.status) + '</b>' + (p.reason ? '　' + esc(p.reason) : '') + '　資料標籤：' + esc(p.label || '') + '</p>';
    if (p.latest) {
      h += table(['日期', '收盤／成交', '淨值', '折溢價', '標籤'], (p.latest || []).map(function (r) {
        return [dateCell(r.d), td(num(r.price, 2)), td(num(r.nav, 4)), td(pctText(r.premiumPct)), td(r.tag)];
      }));
    }
    ['estimated', 'official', 'short'].forEach(function (k) {
      var s = p[k];
      if (!s) return;
      var title = k === 'estimated' ? '預估口徑' : k === 'official' ? '確定口徑' : s.title || '短期';
      if (typeof s.medianPct === 'number') {
        h += '<p>' + esc(title) + '：' + s.n + ' 個交易日（' + dateSpan(s.from) + '～' + dateSpan(s.through) + '），中位數 ' + pctText(s.medianPct) +
          '、最小 ' + pctText(s.minPct) + '、最大 ' + pctText(s.maxPct) + '</p>';
      } else {
        h += '<p class="muted">' + esc(title) + '：' + esc(s.reason || '資料不足') + (typeof s.n === 'number' ? '（目前 ' + s.n + ' 個交易日）' : '') + '</p>';
      }
    });
    return h + notesList(p.notes);
  }
  function renderCost(cost, sc) {
    var h = '<h2>成本（cost.json）</h2>';
    if (!cost) {
      h += '<p class="warn">讀不到 data/analysis/cost.json。</p>';
    } else {
      h += '<p class="muted">產生時間 ' + dateSpan(cost.generatedAt) + '。' + esc((cost.notes || []).join(' ')) + '</p>';
      var tdiff = cost.trackingDifference;
      h += '<h3>00646 追蹤差（00646 報酬 − 基準換算台幣的報酬）</h3>';
      if (!tdiff) {
        h += '<p class="warn">沒有追蹤差。</p>';
      } else {
        [['primary', '主口徑'], ['reference', '對照口徑']].forEach(function (pair) {
          var o = tdiff[pair[0]];
          if (!o) return;
          h += '<h4>' + esc(pair[1]) + '：基準 ' + esc(o.benchmark) + '　資料標籤：' + esc(o.label || '') + '</h4>';
          if (!o.available) { h += '<p class="muted">' + esc(o.reason || '資料不足') + '</p>'; return; }
          h += table(['視窗', '追蹤差'], [['1y', '3y'].map(function (k) { return td(k === '1y' ? '1 年（52 週）' : '3 年（156 週）'); }).map(function (cell, i) {
            return [cell, tdWindow((o.windows || {})[i === 0 ? '1y' : '3y'])];
          })[0], [td('3 年（156 週）'), tdWindow((o.windows || {})['3y'])]]);
          h += notesList(o.notes);
        });
      }
      h += '<h3>折溢價</h3>';
      var pm = cost.premium || {};
      Object.keys(pm).sort().forEach(function (id) { h += renderPremium(id, pm[id]); });
      if (!Object.keys(pm).length) h += '<p class="muted">沒有折溢價資料。</p>';
      var gs = cost.goldSpread;
      h += '<h3>黃金存摺價差（本行賣出 − 本行買入）÷ 中價</h3>';
      if (!gs || gs.reason) {
        h += '<p class="muted">' + esc((gs && gs.reason) || '沒有資料') + '</p>';
      } else {
        h += '<p>最新 ' + dateSpan(gs.latest.d) + '：本行買入 ' + num(gs.latest.buy, 0) + '、本行賣出 ' + num(gs.latest.sell, 0) + '，價差 ' + pctText(gs.latest.spreadPct) +
          '　資料標籤：' + esc(gs.label) + '</p>';
        if (gs.summary && gs.summary.n) {
          h += '<p>' + gs.summary.n + ' 天（' + dateSpan(gs.summary.from) + '～' + dateSpan(gs.summary.through) + '）：中位數 ' + pctText(gs.summary.medianPct) +
            '、最小 ' + pctText(gs.summary.minPct) + '、最大 ' + pctText(gs.summary.maxPct) + '</p>';
        }
      }
      var bp = cost.barPremium;
      h += '<h3>實體條塊相對存摺的溢價（每公克 ÷ 存摺本行賣出 − 1）</h3>';
      if (!bp || bp.reason) {
        h += '<p class="muted">' + esc((bp && bp.reason) || '沒有資料') + '</p>';
      } else {
        h += '<p>最新 ' + dateSpan(bp.latest.d) + '（存摺本行賣出 ' + num(bp.latest.goldSell, 0) + '）　共 ' + esc(bp.n) + ' 天，自 ' + dateSpan(bp.from) + '　資料標籤：' + esc(bp.label) + '</p>';
        h += table(['規格', '公克', '整條價', '每公克', '溢價'], (bp.latest.rows || []).map(function (r) {
          return [td(r.spec), td(num(r.grams, 1)), td(num(r.price, 0)), td(num(r.perGram, 1)), td(pctText(r.premiumPct))];
        }));
        h += notesList(bp.notes);
      }
    }
    h += '<h3>靜態成本表（static-costs.json）</h3>';
    if (!sc) {
      h += '<p class="warn">讀不到 data/analysis/static-costs.json。</p>';
    } else {
      h += '<p class="muted">' + esc(sc.note || '') + '</p>';
      h += table(['標的', '項目', '數值', '標籤', '出處', '查核日期', '備註'], (sc.entries || []).map(function (e) {
        return [td(e.asset), td(e.item), td(e.value === null || e.value === undefined ? 'null（' + (e.nullReason || '沒有查到') + '）' : e.value + (e.unit ? ' ' + e.unit : '')),
          td(e.label), tdRaw(e.source ? '<a href="' + esc(e.source) + '" rel="noopener">' + esc(e.sourceTitle || e.source) + '</a>' : '—'), dateCell(e.checkedOn), td(e.note || '')];
      }));
    }
    return h;
  }

  /* ------------------------------------------------------------ 集中度（只在瀏覽器） */
  var C = function () { return window.Concentration; };

  function renderFacts(f) {
    if (!f) return '';
    if (!f.ok) {
      return '<p class="warn">設定檔有問題，沒有算：</p><ul>' + (f.errors || []).map(function (e) { return '<li class="warn">' + esc(e) + '</li>'; }).join('') + '</ul>';
    }
    var a = f.aligned;
    var h = '<p class="muted">設定檔月份 ' + dateSpan(f.month || '—') + '；八類合計 ' + num(f.sum, 1) + '%。這三個數字只在你的瀏覽器裡算，不上傳、不寫進任何公開檔。</p>';
    h += table(['事實', '數值', '說明'], [
      [td('最大單一類別'), td(pctText(f.maxClass.pct)), td(f.maxClass.label)],
      [td('前二類合計'), td(pctText(f.topTwo.pct)), td(f.topTwo.classes.map(function (c) { return c.label; }).join('＋'))],
      [td('與收入來源代理同向的合計'), td(a.available ? pctText(a.pct) : '資料不足'),
        td(a.available ? '代理標的 ' + a.proxy + '；3 年週報酬相關係數 > ' + a.threshold + ' 視為同向（下表列出每個係數本身）' : '沒有可用的代理標的')]
    ]);
    if (a.available) {
      h += table(['類別', '權重', '類別代理', '與收入來源代理的相關係數', '同向？'], a.rows.map(function (r) {
        return [td(r.label), td(pctText(r.pct)), td(r.proxy), td(typeof r.r === 'number' ? r.r.toFixed(3) + '（>' + a.threshold + ' 視為同向）' : '資料不足'), td(r.aligned ? '同向' : '—')];
      }));
    }
    if (f.warnings && f.warnings.length) h += '<ul>' + f.warnings.map(function (w) { return '<li class="warn">' + esc(w) + '</li>'; }).join('') + '</ul>';
    h += notesList(f.notes);
    return h;
  }

  /* 表單：欄位 id 用 w_<類別>、proxy_sel、profile_month——刻意不用設定檔的鍵名。 */
  function renderProfileForm(values, canEdit) {
    var c = C();
    var v = values || {};
    var h = '<form id="profile-form" onsubmit="return false;"><table><thead><tr><th>類別</th><th>權重（%）</th></tr></thead><tbody>';
    c.CLASSES.forEach(function (cls) {
      h += '<tr><td><label for="w_' + esc(cls) + '">' + esc(c.LABELS[cls]) + '</label></td>' +
        '<td><input type="number" min="0" max="100" step="0.1" id="w_' + esc(cls) + '" name="w_' + esc(cls) + '" value="' + esc(v[cls] === undefined ? '' : v[cls]) + '"' + (canEdit ? '' : ' disabled') + '></td></tr>';
    });
    h += '<tr><td><label for="proxy_sel">收入來源代理標的</label></td><td><select id="proxy_sel" name="proxy_sel"' + (canEdit ? '' : ' disabled') + '>' +
      '<option value="">（未選）</option>' +
      c.PROXY_CHOICES.map(function (x) { return '<option value="' + esc(x.id) + '"' + (v.proxy === x.id ? ' selected' : '') + '>' + esc(x.label) + '</option>'; }).join('') +
      '</select></td></tr>';
    h += '<tr><td><label for="profile_month">設定檔月份（YYYY-MM）</label></td><td><input type="text" id="profile_month" name="profile_month" placeholder="2026-09" value="' + esc(v.month || '') + '"' + (canEdit ? '' : ' disabled') + '></td></tr>';
    h += '</tbody></table>';
    h += canEdit
      ? '<p><button type="button" id="profile-save">儲存到私人倉庫</button>　<span class="muted">只寫到你自己的私人倉庫；commit 訊息不含任何數字。</span></p>'
      : '<p class="muted">要編輯：先到設定頁貼上同步金鑰並開啟雲端同步。</p>';
    return h + '<div id="profile-msg"></div></form>';
  }

  function readFormValues(root) {
    var c = C();
    var r = root || document;
    var out = {};
    c.CLASSES.forEach(function (cls) {
      var el = r.querySelector('#w_' + cls);
      out[cls] = el ? el.value : '';
    });
    var sel = r.querySelector('#proxy_sel'), mo = r.querySelector('#profile_month');
    out.proxy = sel ? sel.value : '';
    out.month = mo ? mo.value.trim() : '';
    return out;
  }

  function renderConcentration(state) {
    var s = state || { status: 'unset' };
    var h = '<h2>個人集中度（只在瀏覽器端）</h2>';
    h += '<p class="muted">資料只從你的私人倉庫讀進瀏覽器計算，不上傳、不寫進任何公開檔。</p>';
    h += '<p><button type="button" id="conc-load">讀取我的集中度設定（從私人倉庫）</button>　<span id="conc-status" class="muted"></span></p>';
    if (s.status === 'loading') {
      h += '<p class="muted">讀取中…</p>';
    } else if (s.status === 'error') {
      h += '<p class="warn">讀不到：' + esc(s.reason || '') + '</p>';
    } else if (s.status === 'set') {
      h += '<div id="conc-facts">' + renderFacts(s.facts) + '</div>';
      h += '<h3>設定檔</h3><div id="conc-form">' + renderProfileForm(s.formValues, !!s.canEdit) + '</div>';
    } else {
      h += '<p id="conc-unset"><b>未設定</b>' + (s.reason ? '　<span class="muted">' + esc(s.reason) + '</span>' : '') + '</p>';
      if (s.showForm) h += '<h3>建立設定檔</h3><div id="conc-form">' + renderProfileForm(s.formValues, !!s.canEdit) + '</div>';
    }
    return h;
  }

  /* ------------------------------------------------------------ 試算（A1-3）：結果在私人倉庫，按一下才讀 */
  var ADHOC_INDEX = 'adhoc/index.json';
  var ADHOC_MAX = 20;
  var ADHOC_TAX = '註冊地造成的稅務差異（股息預扣稅、遺產稅）本系統不計算，需另查最新規定';

  function dateOrDash(d) { return d ? dateCell(d) : td('—'); }
  function rText(r) { return typeof r === 'number' ? r.toFixed(3) : '—'; }

  function renderAdhocDetail(r) {
    if (!r) return '<p class="warn">讀不到這一筆試算結果。</p>';
    var h = '<h3>' + esc(r.symbol) + (r.name ? '　<span class="muted">' + esc(r.name) + '</span>' : '') + '</h3>';
    h += '<p class="muted">類別 ' + esc(r.assetClass) + '；幣別 ' + esc(r.currency || '—') + '；資料截止 ' + dateSpan(r.dataThrough) +
         '；第一根週棒 ' + dateSpan(r.firstBar) + '；' + esc(r.bars) + ' 根；執行日 ' + dateSpan(r.runDate) +
         '；資料標籤：' + esc(r.dataLabel || '') + '</p>';
    if (r.priceUnitNote) h += '<p class="warn">' + esc(r.priceUnitNote) + '</p>';
    var er = r.expenseRatio || {};
    h += '<p>費用率：' + (typeof er.pct === 'number' ? pctText(er.pct) : '—（' + esc(er.reason || '沒有輸入') + '）') +
         '　<span class="muted">標籤：' + esc(er.label || '') + '</span></p>';
    var risk = r.risk || {}, vol = risk.volatility || {}, v1 = vol['1y'] || {}, v5 = vol['5y'] || {};
    var mdd = risk.maxDrawdown || {}, cdd = risk.currentDrawdown || {};
    function volRow(name, v) {
      return [td(name), td(typeof v.pct === 'number' ? pctText(v.pct) : '資料不足（' + (v.reason || '') + '）'),
              td(v.from ? v.from + '～' + v.through : '—'), td(v.label || '')];
    }
    h += table(['項目', '數值', '區間', '資料標籤'], [
      volRow('年化波動 1 年', v1), volRow('年化波動 5 年', v5),
      [td('最大回檔'), td(typeof mdd.pct === 'number' ? pctText(mdd.pct) : '資料不足'),
       td(mdd.peakDate ? '高點 ' + mdd.peakDate + ' → 低點 ' + mdd.troughDate + (mdd.recoveredDate ? '，' + mdd.recoveredDate + ' 回到高點' : '，尚未回到高點') : '—'), td(mdd.label || '')],
      [td('目前距高點'), td(typeof cdd.pct === 'number' ? pctText(cdd.pct) : '資料不足'),
       td(cdd.highDate ? '高點 ' + cdd.highDate + '，資料到 ' + cdd.dataThrough : '—'), td(cdd.label || '')]
    ]);
    var c = r.correlation || {};
    h += '<h4>與公開標的的 3 年週報酬相關（' + esc(c.window || '') + '）</h4>';
    if (c.reason) {
      h += '<p class="warn">' + esc(c.reason) + '</p>';
    } else {
      var top = c.top || [];
      h += '<p class="muted">相關最高的前三名（顯示係數本身；各自幣別、不含匯率換算）：</p>';
      h += table(['標的', '類別', '相關係數', '重疊週數', '區間'], top.map(function (x) {
        return [td(x.id + (x.name ? '　' + x.name : '')), td(x.assetClass || ''), td(rText(x.r)), td(x.n), td(x.from ? x.from + '～' + x.through : '—')];
      }));
      if ((c.all || []).length > top.length) {
        h += '<details><summary class="muted">全部 ' + (c.all || []).length + ' 個標的</summary>' +
             table(['標的', '相關係數', '重疊週數'], (c.all || []).map(function (x) { return [td(x.id), td(rText(x.r)), td(x.n)]; })) + '</details>';
      }
      if ((c.insufficient || []).length) {
        h += '<p class="muted">資料不足的標的：' + esc((c.insufficient || []).map(function (x) { return x.id + '（' + (x.reason || '') + '）'; }).join('、')) + '</p>';
      }
      if ((c.skippedSameSymbol || []).length) h += '<p class="muted">同一個代號已在公開清單（' + esc(c.skippedSameSymbol.join('、')) + '），不跟自己算相關。</p>';
    }
    h += '<p class="muted">估值：' + esc((r.valuation || {}).reason || '資料不足') + '；折溢價：' + esc((r.premium || {}).reason || '資料不足') +
         '；趨勢：' + esc((r.trend || {}).reason || '—') + '</p>';
    h += notesList(r.notes);
    return h;
  }

  function renderAdhoc(state) {
    var s = state || { status: 'idle' };
    var h = '<h2>試算（A1-3）</h2>';
    h += '<p class="muted">臨時分析不在清單上的標的。計算在你自己的私人倉庫的 Actions 裡跑，結果只放在私人倉庫的 adhoc/ 底下；這一頁按一下才去讀，不上傳、不寫進任何公開檔。</p>';
    h += '<p><button type="button" id="adhoc-load">讀取試算清單（從私人倉庫）</button>　<span id="adhoc-status" class="muted"></span></p>';
    if (s.status === 'loading') {
      h += '<p class="muted">讀取中…</p>';
    } else if (s.status === 'error') {
      h += '<p class="warn">讀不到：' + esc(s.reason || '') + '</p>';
    } else if (s.status === 'unset') {
      h += '<p id="adhoc-unset"><b>未設定</b>　<span class="muted">' + esc(s.reason || '') + '</span></p>';
    } else if (s.status === 'empty') {
      h += '<p id="adhoc-empty"><b>還沒有任何試算</b>　<span class="muted">' + esc(s.reason || '私人倉庫的 adhoc/ 底下沒有結果；到 invest-data 的 Actions 跑一次 adhoc-analyze') + '</span></p>';
    } else if (s.status === 'index') {
      var syms = Object.keys((s.index || {}).symbols || {}).sort();
      h += '<p class="muted">來自 adhoc/index.json（更新 ' + dateSpan(s.index.updatedAt) + '）；' + syms.length + ' 個代號。點「看詳細」才讀那一檔。</p>';
      h += table(['代號', '類別', '幣別', '資料截止', '1 年波動', '最大回檔', '距高點', '相關最高', ''], syms.map(function (sym) {
        var e = s.index.symbols[sym] || {};
        return [td(sym), td(e.assetClass || ''), td(e.currency || ''), dateOrDash(e.dataThrough), td(pctText(e.vol1yPct)),
                td(pctText(e.maxDrawdownPct)), td(pctText(e.currentDrawdownPct)),
                td(e.top1 ? e.top1.id + ' ' + rText(e.top1.r) : '—'),
                tdRaw('<button type="button" class="adhoc-open" data-path="' + esc(e.latest) + '">看詳細</button>')];
      }));
    } else if (s.status === 'list') {
      h += '<p class="muted">私人倉庫裡沒有 adhoc/index.json，改用目錄清單當備援（每個代號只看檔名最新的一筆）。</p>';
      h += table(['代號', '最新一筆', ''], (s.entries || []).map(function (e) {
        return [td(e.symbol), td(e.latest || '—'),
                tdRaw(e.path ? '<button type="button" class="adhoc-open" data-path="' + esc(e.path) + '">看詳細</button>' : '—')];
      }));
    }
    if (s.detail) h += '<div id="adhoc-detail">' + renderAdhocDetail(s.detail) + '</div>';
    h += '<p class="muted">' + esc(ADHOC_TAX) + '</p>';
    return h;
  }

  /* ------------------------------------------------------------ 組合 */
  function render(data, targets) {
    targets = targets || {};
    var pick = function (key) { return targets[key] || document.getElementById(key); };
    var s = pick('status'), r = pick('risk'), d = pick('decompose'), c = pick('cost'), k = pick('concentration');
    if (s) s.innerHTML = renderStatus(data.status);
    if (r) r.innerHTML = renderRisk(data.risk);
    if (d) d.innerHTML = renderDecompose(data.decompose);
    if (c) c.innerHTML = renderCost(data.cost, data.staticCosts);
    if (k) k.innerHTML = renderConcentration(data.concentration);
    var x = pick('adhoc');
    if (x && data.adhoc) x.innerHTML = renderAdhoc(data.adhoc);
  }

  function fetchJSON(url) {
    return fetch(url, { cache: 'no-cache' }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).catch(function () { return null; });                 // 讀不到就交給 render 顯示「讀不到」，不吞成功
  }

  /* 集中度的互動：按一下才去私人倉庫讀；儲存前驗證、確認倉庫是私人的。這裡不 console.log 任何值。 */
  function wireConcentration(risk) {
    var section = document.getElementById('concentration');
    if (!section) return;
    var c = C();
    function setState(state) { section.innerHTML = renderConcentration(state); wire(); }
    function corr() { return risk && risk.correlation; }
    function canEdit() { return !!(window.Storage && window.Storage.getMode() === 'github' && window.Storage.hasPat()); }
    function loadProfile() {
      setState({ status: 'loading' });
      return window.Storage.loadFile(c.FILE).then(function (r) {
        if (!r.data) {
          var reason = r.reason === 'not-github' ? '本機模式或還沒貼同步金鑰：到設定頁開啟雲端同步後再讀' : '私人倉庫裡還沒有 ' + c.FILE;
          setState({ status: 'unset', reason: reason, showForm: canEdit(), canEdit: canEdit(), formValues: c.toFormValues(c.emptyTemplate()) });
          return;
        }
        setState({ status: 'set', facts: c.facts(r.data, corr()), formValues: c.toFormValues(r.data), canEdit: canEdit() });
      }).catch(function (e) { setState({ status: 'error', reason: e && e.message }); });
    }
    function saveProfile() {
      var msg = document.getElementById('profile-msg');
      var values = readFormValues(section);
      var profile = c.fromForm(values);
      var v = c.validate(profile);
      if (!v.ok) { msg.innerHTML = '<p class="warn">沒有存：' + v.errors.map(esc).join('；') + '</p>'; return; }
      msg.innerHTML = '<p class="muted">確認倉庫是私人的、寫入中…' + (v.warnings.length ? '（提醒：' + esc(v.warnings.join('；')) + '）' : '') + '</p>';
      window.Storage.testConnection().then(function () {
        var stamp = new Date().toISOString().slice(0, 16).replace('T', ' ');
        return window.Storage.saveFile(c.FILE, profile, 'analysis-profile 更新（' + stamp + '）');
      }).then(function () {
        msg.innerHTML = '<p class="ok">已存到私人倉庫。</p>';
        setState({ status: 'set', facts: c.facts(profile, corr()), formValues: c.toFormValues(profile), canEdit: true });
        var m2 = document.getElementById('profile-msg');
        if (m2) m2.innerHTML = '<p class="ok">已存到私人倉庫，上面是用新設定算的。</p>';
      }).catch(function (e) { msg.innerHTML = '<p class="warn">沒有存：' + esc(e && e.message) + '</p>'; });
    }
    function wire() {
      var b = document.getElementById('conc-load');
      if (b) b.addEventListener('click', function () {
        if (!window.Storage || !window.Lock) { setState({ status: 'error', reason: '這一頁沒有載到 storage.js／lock.js' }); return; }
        window.Lock.gate(function () { window.Storage.init().then(loadProfile); });
      });
      var sv = document.getElementById('profile-save');
      if (sv) sv.addEventListener('click', saveProfile);
    }
    wire();
  }

  /* 試算的互動：按一下才去私人倉庫讀；先讀 index.json，沒有才列目錄。代號只出現在瀏覽器裡。 */
  function wireAdhoc() {
    var section = document.getElementById('adhoc');
    if (!section) return;
    var current = { status: 'idle' };
    function setState(state) { current = state; section.innerHTML = renderAdhoc(state); wire(); }
    function fail(e) { setState({ status: 'error', reason: e && e.message }); }
    function latestOf(entries) {
      var files = (entries || []).filter(function (e) { return e.type === 'file' && /[.]json$/.test(e.name); })
        .map(function (e) { return e.name; }).sort();
      return files.length ? files[files.length - 1] : null;
    }
    function loadList() {
      setState({ status: 'loading' });
      return window.Storage.loadFile(ADHOC_INDEX).then(function (r) {
        if (r.reason === 'not-github') { setState({ status: 'unset', reason: '本機模式或還沒貼同步金鑰：到設定頁開啟雲端同步後再讀' }); return null; }
        if (r.data && r.data.symbols) { setState({ status: 'index', index: r.data }); return null; }
        return window.Storage.listDir('adhoc').then(function (d) {
          if (!d.entries || !d.entries.length) { setState({ status: 'empty' }); return null; }
          var dirs = d.entries.filter(function (e) { return e.type === 'dir'; }).slice(0, ADHOC_MAX);
          return Promise.all(dirs.map(function (e) {
            return window.Storage.listDir('adhoc/' + e.name).then(function (sub) {
              var latest = latestOf(sub.entries);
              return { symbol: e.name, latest: latest, path: latest ? 'adhoc/' + e.name + '/' + latest : null };
            });
          })).then(function (entries) { setState({ status: 'list', entries: entries }); });
        });
      }).catch(fail);
    }
    function openDetail(path) {
      window.Storage.loadFile(path).then(function (r) {
        if (!r.data) { fail(new Error('讀不到 ' + path)); return; }
        var next = {};
        Object.keys(current).forEach(function (k) { next[k] = current[k]; });
        next.detail = r.data;
        setState(next);
        var d = document.getElementById('adhoc-detail');
        if (d && d.scrollIntoView) d.scrollIntoView();
      }).catch(fail);
    }
    function wire() {
      var b = document.getElementById('adhoc-load');
      if (b) b.addEventListener('click', function () {
        if (!window.Storage || !window.Lock) { fail(new Error('這一頁沒有載到 storage.js／lock.js')); return; }
        window.Lock.gate(function () { window.Storage.init().then(loadList); });
      });
      Array.prototype.forEach.call(section.querySelectorAll('.adhoc-open'), function (btn) {
        btn.addEventListener('click', function () { openDetail(btn.getAttribute('data-path')); });
      });
    }
    wire();
  }

  function boot() {
    Promise.all([fetchJSON('data/analysis/status.json'), fetchJSON('data/analysis/risk.json'), fetchJSON('data/analysis/decompose.json'),
                 fetchJSON('data/analysis/cost.json'), fetchJSON('data/analysis/static-costs.json')])
      .then(function (all) {
        render({ status: all[0], risk: all[1], decompose: all[2], cost: all[3], staticCosts: all[4], concentration: { status: 'unset' },
                 adhoc: { status: 'idle' } });
        wireConcentration(all[1]);
        wireAdhoc();
        wireSorting();
      });
  }

  window.AnalysisDebug = { boot: boot, render: render, renderStatus: renderStatus, renderRisk: renderRisk,
                           renderDecompose: renderDecompose, renderCost: renderCost, renderConcentration: renderConcentration,
                           renderProfileForm: renderProfileForm, readFormValues: readFormValues, renderAdhoc: renderAdhoc,
                           renderAdhocDetail: renderAdhocDetail, esc: esc, num: num,
                           sortTable: sortTable, wireSorting: wireSorting, corrColor: corrColor };
})();
