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
 * 換匯助手（A1-6）：公開的位置、成本、規則表、歷史模擬來自 data/analysis/fx.json；計畫起始月、每月預算、目標總額、期限、已換紀錄
 * 按一下才從私人倉庫讀，算法與設定檔的鍵名全部在 js/fxplan.js。這一段是全站第一個照「呈現原則」做的畫面：
 * 圖示＋狀態詞＋規則＋白話（js/plain.js）、名詞點一下有解釋（js/glossary.js）、幾個圖示各看各的不加總。
 *
 * 小白呈現（A1-7）：同一套呈現推到整個分頁——
 *   最上面一行分析狀態（上次執行時間、有沒有錯），接著是總覽表：每個標的一列、每個面向一格（圖示＋狀態詞），
 *   跟儀表板卡片上的圖示列是同一份判定（js/card-analysis.js 的 aspects；位置用 js/indicators.js 的同一個燈號函式），
 *   所以這一頁多讀 data/latest.json 與每個標的的日線。預設順序跟儀表板一樣，可以照某一個面向排序，也可以回到預設。
 *   既有各區：每個數字下面一行白話、表頭的名詞可以點；相關矩陣改成每個標的一句，逐日／逐週／逐月的明細只在摘要寫白話。
 *   這一頁不顯示任何計數或加總。
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
  /* 表頭可以放名詞按鈕的版本：表頭不跳脫，呼叫的人自己負責（名詞按鈕由 term() 產生，裡面的字已經跳脫過） */
  function tableRaw(headers, rows) {
    var h = '<table><thead><tr>' + headers.map(function (x) { return '<th>' + x + '</th>'; }).join('') + '</tr></thead><tbody>';
    rows.forEach(function (r) { h += '<tr>' + r.join('') + '</tr>'; });
    return h + '</tbody></table>';
  }

  /* 可排序的表（風險總表用）：表頭可以點；「資料不足」與「—」永遠排最後。raw＝表頭不跳脫（裡面有名詞按鈕） */
  function sortableTable(headers, rows, raw) {
    var h = '<div class="table-wrap"><table class="sortable"><thead><tr>' +
      headers.map(function (x, i) { return '<th class="sortable" data-col="' + i + '">' + (raw ? x : esc(x)) + '</th>'; }).join('') + '</tr></thead><tbody>';
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
      th.addEventListener('click', function (ev) {
        // 點到表頭裡的名詞或它展開的解釋，不是要排序
        if (ev && ev.target && ev.target.closest && ev.target.closest('button.term, .term-def')) return;
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
  function volCell(v, years) {
    if (!v) return td('—');
    if (typeof v.pct !== 'number') return '<td>資料不足<div class="muted">' + esc(v.reason || '') + '</div></td>';
    return '<td>' + num(v.pct, 2) + '%<div class="muted">' + esc(v.window) + '，至 ' + dateSpan(v.through) + '　' + esc(v.label) + '</div>' +
      plain('volatility', { pct: v.pct, window: years }) + '</td>';
  }
  function renderRisk(rk) {
    var h = '<h2>風險（risk.json）</h2>';
    if (!rk) return h + '<p class="warn">讀不到 data/analysis/risk.json。</p>';
    h += '<p class="muted">產生時間 ' + dateSpan(rk.generatedAt) + '。' + esc((rk.notes || []).join(' ')) + '</p>';
    var ids = Object.keys(rk.assets || {}).sort();
    h += sortableTable(['標的', '類別', '幣別', '最後完成週棒', '週數', term('年化波動', '年化波動 1 年'), term('年化波動', '年化波動 5 年'),
                        term('回檔', '最大回檔'), term('目前距高點'), '10 年視窗', term('資料標籤')],
      ids.map(function (id) {
        var a = rk.assets[id];
        var dd = a.maxDrawdown || {};
        var cd = a.currentDrawdown || {};
        var ten = a.tenYearWindow || {};
        return [
          td(id + (a.name ? '　' + a.name : '')), td(a.assetClass), td(a.currency),
          '<td>' + dateSpan(a.lastBar) + '<div class="muted">' + esc(a.lastWeek) + '</div></td>',
          td(a.bars),
          volCell(a.volatility && a.volatility['1y'], '1 年'), volCell(a.volatility && a.volatility['5y'], '5 年'),
          '<td>' + pctText(dd.pct) + '<div class="muted">高點 ' + dateSpan(dd.peakDate) + ' → 低點 ' + dateSpan(dd.troughDate) +
            (dd.recoveredDate ? '，' + dateSpan(dd.recoveredDate) + ' 回到高點' : '，尚未回到高點') + '</div>' +
            (typeof dd.pct === 'number' ? plain('drawdown', { pct: dd.pct, recoveredDate: dd.recoveredDate }) : '') + '</td>',
          '<td>' + pctText(cd.pct) + '<div class="muted">高點 ' + dateSpan(cd.highDate) + '，資料至 ' + dateSpan(cd.dataThrough) + '</div>' +
            (typeof cd.pct === 'number' ? plain('currentDrawdown', { pct: cd.pct, highDate: cd.highDate }) : '') + '</td>',
          td(ten.available ? '夠（' + ten.bars + ' 週）' : (ten.reason || '資料不足')),
          td(a.dataLabel)
        ];
      }), true);
    var c = rk.correlation;
    if (c && c.matrix) {
      h += '<h3>' + term('相關係數') + '矩陣（' + esc(c.window) + '，週報酬，成對可用；格內小字是重疊週數）</h3>';
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
      h += corrSentences(c, rk.assets || {}, cids);
    }
    if (rk.problems && rk.problems.length) {
      h += '<ul>' + rk.problems.map(function (p) { return '<li class="warn">' + esc(p) + '</li>'; }).join('') + '</ul>';
    }
    return h;
  }

  /* 相關矩陣的白話：一格一句會是一百多句，所以改成每個標的一句（最同向、最沒有關係、有沒有常常反方向的） */
  function corrSentences(c, assets, cids) {
    var CA = window.CardAnalysis;
    if (!CA || !CA.correlationFacts || !window.Plain) return '';
    return '<h4>每個標的一句</h4><ul class="plain-list" id="corr-plain">' + cids.map(function (id) {
      var f = CA.correlationFacts(id, c, assets);
      var name = (assets[id] || {}).name;
      return '<li><b>' + esc(id) + (name ? '　' + esc(name) : '') + '</b>' +
        plain('correlation', f && f.available ? { most: f.mostAligned, least: f.leastRelated, inverse: f.strongestInverse } : {}) + '</li>';
    }).join('') + '</ul>';
  }

  /* ------------------------------------------------------------ 拆解 */
  var DETAIL_NOTE = '<span class="muted">　明細只列數字，白話寫在上面的摘要</span>';
  function summaryLine(title, s, kind) {
    if (!s) return '';
    if (typeof s.n !== 'number' || !s.n) return '<p class="muted">' + esc(title) + '：資料不足</p>';
    return '<p>' + esc(title) + '（' + esc(s.window) + '，' + dateSpan(s.from) + '～' + dateSpan(s.through) + '）：' +
      '中位數 ' + pctText(s.medianPct) + '、平均 ' + pctText(s.meanPct) + '、最小 ' + pctText(s.minPct) + '、最大 ' + pctText(s.maxPct) + '</p>' +
      plain('residual', { pct: s.medianPct, kind: kind, lead: '一般的情況（中位數）' });
  }
  function renderDecompose(dc) {
    var h = '<h2>拆解（decompose.json）</h2>';
    if (!dc) return h + '<p class="warn">讀不到 data/analysis/decompose.json。</p>';
    h += '<p class="muted">產生時間 ' + dateSpan(dc.generatedAt) + '。' + esc((dc.notes || []).join(' ')) + '</p>';

    var g = dc.gold;
    h += '<h3>台銀金價 ＝ 國際金價 × 匯率 ÷ 31.1035 ＋ ' + term('殘差') + '</h3>';
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
            td(num(g.live.fxMid, 4)), dateCell(g.live.fxDate), td(num(g.live.implied, 2)),
            tdRaw(esc(pctText(g.live.residualPct)) + plain('residual', { pct: g.live.residualPct, kind: 'gold' }))]]);
          h += '<p class="muted">' + esc(g.live.gcNote || '') + '</p>';
        }
      }
      h += summaryLine('殘差（同一天口徑，GC=F 期貨）', g.summarySameDay, 'gold');
      h += summaryLine('殘差（前一日口徑，GC=F 期貨）', g.summaryPrevDay, 'gold');
      if (g.spot) {
        if (g.spot.reason) {
          h += '<p class="muted">現貨口徑：' + esc(g.spot.reason) + '</p>';
        } else {
          h += summaryLine('殘差（同一天口徑，現貨 ' + (g.spot.symbol || '') + '）', g.spot.summarySameDay, 'gold');
          h += summaryLine('殘差（前一日口徑，現貨 ' + (g.spot.symbol || '') + '）', g.spot.summaryPrevDay, 'gold');
          if (g.spot.basis) h += '<p>' + esc(g.spot.basis.note || '') + '　最近 60 日中位數：' + pctText(g.spot.basis.medianPct) + '</p>';
          h += notesList(g.spot.notes);
        }
      }
      var daily = (g.daily || []).slice(-10).reverse();
      h += '<h4>最近 ' + daily.length + ' 天' + DETAIL_NOTE + '</h4>' + table(
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
          '殘差累計 ' + pctText(s.residualSumPct) + '、平均 ' + pctText(s.residualMeanPct) + '、標準差 ' + pctText(s.residualStdPct) + '</p>' +
          plain('residual', { pct: s.residualMeanPct, kind: 'etf', lead: '平均每個月' });
      });
      var monthly = (t.monthly || []).slice(-12).reverse();
      h += '<h4>最近 ' + monthly.length + ' 個月' + DETAIL_NOTE + '</h4>' + table(
        ['月份', '00646（週棒日期）', 'S&P 500（週棒日期）', 'USD/TWD（日期）', '合成', '殘差'],
        monthly.map(function (r) {
          return [dateCell(r.month), tdRaw(pctText(r.r646Pct) + '（' + dateSpan(r.d646) + '）'),
            tdRaw(pctText(r.rGspcPct) + '（' + dateSpan(r.dGspc) + '）'),
            tdRaw(pctText(r.rFxPct) + '（' + dateSpan(r.dFx) + '）'), td(pctText(r.combinedPct)), td(pctText(r.residualPct))];
        }));
    }

    // A1-6：人民幣對台幣的變動，拆成「美元對台幣」與「美元對人民幣」兩塊
    var y = dc.cny;
    h += '<h3>人民幣兌台幣 ＝ 美元兌台幣 ÷ 美元兌人民幣 ＋ 殘差（週）</h3>';
    if (!y) {
      h += '<p class="muted">沒有人民幣拆解。</p>';
    } else if (y.reason) {
      h += '<p class="warn">這一段沒算出來：' + esc(y.reason) + '</p>';
    } else {
      h += '<p>' + esc(y.formula) + '　資料標籤：' + esc(y.label) + '</p>' + notesList(y.notes);
      var wins = y.windows || {};
      h += table(['視窗', '人民幣兌台幣', '美元兌台幣', '美元兌人民幣', '合成', '殘差'], ['1y', '3y'].map(function (k) {
        var w = wins[k];
        var name = k === '1y' ? '1 年（52 週）' : '3 年（156 週）';
        if (!w || typeof w.rCnyTwdPct !== 'number') return [td(name), '<td colspan="5">資料不足<div class="muted">' + esc((w && w.reason) || '') + '</div></td>'];
        return ['<td>' + esc(name) + '<div class="muted">' + dateSpan(w.from) + '～' + dateSpan(w.through) + '</div></td>',
          tdRaw(esc(pctText(w.rCnyTwdPct)) + plain('fxLeg', { total: w.rCnyTwdPct, usdTwd: w.rUsdTwdPct, usdCny: w.rUsdCnyPct })),
          td(pctText(w.rUsdTwdPct)), td(pctText(w.rUsdCnyPct)), td(pctText(w.combinedPct)),
          tdRaw(esc(pctText(w.residualPct)) + plain('residual', { pct: w.residualPct, kind: 'cny' }))];
      }));
      h += '<p class="muted">怎麼讀：合成＝(1＋美元兌台幣的變動) ÷ (1＋美元兌人民幣的變動) − 1；美元兌人民幣是負的，表示人民幣對美元變貴。</p>';
      var s52 = y.summary52;
      if (s52 && s52.n) {
        h += '<p>每週殘差（最近 ' + esc(s52.n) + ' 週，' + dateSpan(s52.from) + '～' + dateSpan(s52.through) + '）：中位數 ' + pctText(s52.medianPct) +
             '、最小 ' + pctText(s52.minPct) + '、最大 ' + pctText(s52.maxPct) + '</p>' +
             plain('residual', { pct: s52.medianPct, kind: 'cny', lead: '一般的一週（中位數）' });
      } else {
        h += '<p class="muted">每週殘差：資料不足</p>';
      }
      var weekly = (y.weekly || []).slice(-10).reverse();
      h += '<h4>最近 ' + weekly.length + ' 週' + DETAIL_NOTE + '</h4>' + table(
        ['週（週一）', '人民幣兌台幣中價（日期）', '美元兌台幣中價（日期）', '美元兌人民幣（日期）', '公式值', '殘差'],
        weekly.map(function (r) {
          return [dateCell(r.week), tdRaw(num(r.cnyTwdMid, 4) + '（' + dateSpan(r.dCny) + '）'), tdRaw(num(r.usdTwdMid, 4) + '（' + dateSpan(r.dUsd) + '）'),
            tdRaw(num(r.usdCny, 4) + '（' + dateSpan(r.dUsdCny) + '）'), td(num(r.implied, 4)), td(pctText(r.residualPct))];
        }));
    }
    return h;
  }

  /* ------------------------------------------------------------ 成本 */
  function tdWindow(w, years) {
    if (!w) return td('—');
    if (w.reason) return '<td>資料不足<div class="muted">' + esc(w.reason) + '</div></td>';
    return '<td>累計 ' + pctText(w.diffPct) + '，年化 ' + pctText(w.annualizedDiffPct) +
      '<div class="muted">00646 ' + pctText(w.r646Pct) + '、基準（台幣）' + pctText(w.rBenchTwdPct) + '；' + dateSpan(w.from) + '～' + dateSpan(w.through) + '（' + esc(w.weeks) + ' 週）</div>' +
      plain('tracking', { annualPct: w.annualizedDiffPct, years: years }) + '</td>';
  }
  function premiumMinDays() { return (window.CardAnalysis && window.CardAnalysis.PREMIUM_MIN_DAYS) || 20; }
  function renderPremium(id, p) {
    var h = '<h4>' + esc(id) + '</h4>';
    if (!p) return h + '<p class="muted">沒有資料。</p>';
    h += '<p>狀態：<b>' + esc(p.status) + '</b>' + (p.reason ? '　' + esc(p.reason) : '') + '　資料標籤：' + esc(p.label || '') + '</p>';
    if (p.latest) {
      h += tableRaw(['日期', '收盤／成交', term('淨值'), term('折溢價'), '標籤'], (p.latest || []).map(function (r) {
        return [dateCell(r.d), td(num(r.price, 2)), td(num(r.nav, 4)),
                tdRaw(esc(pctText(r.premiumPct)) + plain('premium', { pct: r.premiumPct })), td(r.tag)];
      }));
    }
    ['estimated', 'official', 'short'].forEach(function (k) {
      var s = p[k];
      if (!s) return;
      var title = k === 'estimated' ? '預估口徑' : k === 'official' ? '確定口徑' : s.title || '短期';
      if (typeof s.medianPct === 'number') {
        h += '<p>' + esc(title) + '：' + s.n + ' 個交易日（' + dateSpan(s.from) + '～' + dateSpan(s.through) + '），中位數 ' + pctText(s.medianPct) +
          '、最小 ' + pctText(s.minPct) + '、最大 ' + pctText(s.maxPct) + '</p>' + plain('premium', { pct: s.medianPct, lead: '一般的情況（中位數）' });
      } else {
        h += '<p class="muted">' + esc(title) + '：' + esc(s.reason || '資料不足') + (typeof s.n === 'number' ? '（目前 ' + s.n + ' 個交易日）' : '') + '</p>' +
          (typeof s.n === 'number' ? plain('accumulating', { n: s.n, min: premiumMinDays() }) : '');
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
      h += '<h3>00646 ' + term('追蹤差') + '（00646 報酬 − 基準換算台幣的報酬）</h3>';
      if (!tdiff) {
        h += '<p class="warn">沒有追蹤差。</p>';
      } else {
        [['primary', '主口徑'], ['reference', '對照口徑']].forEach(function (pair) {
          var o = tdiff[pair[0]];
          if (!o) return;
          h += '<h4>' + esc(pair[1]) + '：基準 ' + esc(o.benchmark) + '　資料標籤：' + esc(o.label || '') + '</h4>';
          if (!o.available) { h += '<p class="muted">' + esc(o.reason || '資料不足') + '</p>'; return; }
          h += table(['視窗', '追蹤差'], [[td('1 年（52 週）'), tdWindow((o.windows || {})['1y'], 1)],
                                          [td('3 年（156 週）'), tdWindow((o.windows || {})['3y'], 3)]]);
          h += notesList(o.notes);
        });
      }
      h += '<h3>' + term('折溢價') + '</h3>';
      var pm = cost.premium || {};
      Object.keys(pm).sort().forEach(function (id) { h += renderPremium(id, pm[id]); });
      if (!Object.keys(pm).length) h += '<p class="muted">沒有折溢價資料。</p>';
      var gs = cost.goldSpread;
      h += '<h3>黃金存摺價差（本行賣出 − 本行買入）÷ 中價</h3>';
      if (!gs || gs.reason) {
        h += '<p class="muted">' + esc((gs && gs.reason) || '沒有資料') + '</p>';
      } else {
        h += '<p>最新 ' + dateSpan(gs.latest.d) + '：本行買入 ' + num(gs.latest.buy, 0) + '、本行賣出 ' + num(gs.latest.sell, 0) + '，價差 ' + pctText(gs.latest.spreadPct) +
          '　資料標籤：' + esc(gs.label) + '</p>' + plain('spread', { pct: gs.latest.spreadPct, kind: 'gold', unit: '元' });
        if (gs.summary && gs.summary.n) {
          h += '<p>' + gs.summary.n + ' 天（' + dateSpan(gs.summary.from) + '～' + dateSpan(gs.summary.through) + '）：中位數 ' + pctText(gs.summary.medianPct) +
            '、最小 ' + pctText(gs.summary.minPct) + '、最大 ' + pctText(gs.summary.maxPct) + '</p>' +
            plain('spreadCompare', { current: gs.latest.spreadPct, median: gs.summary.medianPct });
        }
      }
      // A1-6：人民幣計價的黃金存摺，算法跟台幣那張一樣
      var gc = cost.goldSpreadCny;
      h += '<h3>黃金存摺（人民幣）價差（本行賣出 − 本行買入）÷ 中價</h3>';
      if (!gc || gc.reason) {
        h += '<p class="muted">' + esc((gc && gc.reason) || '沒有資料') + '</p>';
      } else {
        h += '<p>最新 ' + dateSpan(gc.latest.d) + '：本行買入 ' + num(gc.latest.buy, 2) + '、本行賣出 ' + num(gc.latest.sell, 2) + '，價差 ' + pctText(gc.latest.spreadPct) +
          '　資料標籤：' + esc(gc.label) + '</p>' + plain('spread', { pct: gc.latest.spreadPct, kind: 'gold', unit: '人民幣' });
        if (gc.summary && gc.summary.n) {
          h += '<p>' + gc.summary.n + ' 天（' + dateSpan(gc.summary.from) + '～' + dateSpan(gc.summary.through) + '）：中位數 ' + pctText(gc.summary.medianPct) +
            '、最小 ' + pctText(gc.summary.minPct) + '、最大 ' + pctText(gc.summary.maxPct) + '</p>' +
            plain('spreadCompare', { current: gc.latest.spreadPct, median: gc.summary.medianPct });
        }
      }
      var bp = cost.barPremium;
      h += '<h3>實體條塊相對存摺的溢價（每公克 ÷ 存摺本行賣出 − 1）</h3>';
      if (!bp || bp.reason) {
        h += '<p class="muted">' + esc((bp && bp.reason) || '沒有資料') + '</p>';
      } else {
        h += '<p>最新 ' + dateSpan(bp.latest.d) + '（存摺本行賣出 ' + num(bp.latest.goldSell, 0) + '）　共 ' + esc(bp.n) + ' 天，自 ' + dateSpan(bp.from) + '　資料標籤：' + esc(bp.label) + '</p>';
        h += tableRaw(['規格', '公克', '整條價', '每公克', term('條塊溢價', '溢價')], (bp.latest.rows || []).map(function (r) {
          return [td(r.spec), td(num(r.grams, 1)), td(num(r.price, 0)), td(num(r.perGram, 1)),
                  tdRaw(esc(pctText(r.premiumPct)) + plain('barPremium', { pct: r.premiumPct, spec: r.spec }))];
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
        // 數值是單一個百分比才寫白話；分級的費率是一段文字，不是一個數字
        var single = e.value !== null && e.value !== undefined && /^\s*\d+(\.\d+)?\s*$/.test(String(e.value)) && /^%/.test(String(e.unit || ''));
        return [td(e.asset), td(e.item),
          tdRaw(esc(e.value === null || e.value === undefined ? 'null（' + (e.nullReason || '沒有查到') + '）' : e.value + (e.unit ? ' ' + e.unit : '')) +
                (single ? plain('fee', { pct: parseFloat(e.value), kind: /稅/.test(String(e.item || '')) ? 'tax' : 'fee' }) : '')),
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
    var twoLabels = f.topTwo.classes.map(function (c) { return c.label; });
    h += table(['事實', '數值', '說明'], [
      [td('最大單一類別'), td(pctText(f.maxClass.pct)), tdRaw(esc(f.maxClass.label) + plain('concMax', { pct: f.maxClass.pct, label: f.maxClass.label }))],
      [td('前二類合計'), td(pctText(f.topTwo.pct)), tdRaw(esc(twoLabels.join('＋')) + plain('concTopTwo', { pct: f.topTwo.pct, labels: twoLabels }))],
      [td('與收入來源代理同向的合計'), td(a.available ? pctText(a.pct) : '資料不足'),
        tdRaw(esc(a.available ? '代理標的 ' + a.proxy + '；3 年週報酬相關係數 > ' + a.threshold + ' 視為同向（下表列出每個係數本身）' : '沒有可用的代理標的') +
              (a.available ? plain('concAligned', { pct: a.pct, proxy: a.proxy }) : ''))]
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
    function volRow(name, v, years) {
      var ok = typeof v.pct === 'number';
      return [tdRaw(term('年化波動', name)), tdRaw(esc(ok ? pctText(v.pct) : '資料不足（' + (v.reason || '') + '）') + (ok ? plain('volatility', { pct: v.pct, window: years }) : '')),
              td(v.from ? v.from + '～' + v.through : '—'), td(v.label || '')];
    }
    h += adhocGlance(r);
    h += table(['項目', '數值', '區間', '資料標籤'], [
      volRow('年化波動 1 年', v1, '1 年'), volRow('年化波動 5 年', v5, '5 年'),
      [tdRaw(term('回檔', '最大回檔')), tdRaw(esc(typeof mdd.pct === 'number' ? pctText(mdd.pct) : '資料不足') +
         (typeof mdd.pct === 'number' ? plain('drawdown', { pct: mdd.pct, recoveredDate: mdd.recoveredDate }) : '')),
       td(mdd.peakDate ? '高點 ' + mdd.peakDate + ' → 低點 ' + mdd.troughDate + (mdd.recoveredDate ? '，' + mdd.recoveredDate + ' 回到高點' : '，尚未回到高點') : '—'), td(mdd.label || '')],
      [tdRaw(term('目前距高點')), tdRaw(esc(typeof cdd.pct === 'number' ? pctText(cdd.pct) : '資料不足') +
         (typeof cdd.pct === 'number' ? plain('currentDrawdown', { pct: cdd.pct, highDate: cdd.highDate }) : '')),
       td(cdd.highDate ? '高點 ' + cdd.highDate + '，資料到 ' + cdd.dataThrough : '—'), td(cdd.label || '')]
    ]);
    var c = r.correlation || {};
    h += '<h4>與公開標的的 3 年週報酬相關（' + esc(c.window || '') + '）</h4>';
    if (c.reason) {
      h += '<p class="warn">' + esc(c.reason) + '</p>';
    } else {
      var top = c.top || [];
      h += '<p class="muted">相關最高的前三名（顯示係數本身；各自幣別、不含匯率換算）：</p>';
      h += tableRaw(['標的', '類別', term('相關係數'), '重疊週數', '區間'], top.map(function (x) {
        return [td(x.id + (x.name ? '　' + x.name : '')), td(x.assetClass || ''), td(rText(x.r)), td(x.n), td(x.from ? x.from + '～' + x.through : '—')];
      })) + plain('similar', { top: top });
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

  /* 試算結果的圖示列（A1-7）：風險（跟卡片同一條規則）＋跟現有標的最像的三檔。
     試算沒有日線，所以沒有位置；也沒有成本的歷史可以比。兩格各看各的。 */
  function adhocGlance(r) {
    var P = window.Plain;
    if (!P) return '';
    var vol = ((r.risk || {}).volatility) || {}, v1 = vol['1y'] || {}, v5 = vol['5y'] || {};
    var top = ((r.correlation || {}).top || []).slice(0, 3);
    return '<div class="glance-row" id="adhoc-glance">' +
      P.badge(P.riskState(v1.pct, v5.pct, (typeof v1.pct !== 'number' ? v1.reason : v5.reason) || ''), '風險') +
      '<div class="glance" data-aspect="similar"><div class="glance-title">跟現有標的最像的三檔</div>' +
        '<div class="glance-main"><span class="glance-word">' + esc(top.length ? top.map(function (x) { return x.id; }).join('、') : P.NA) + '</span></div>' +
        '<div class="glance-because">' + esc(P.sentence('similar', { top: top })) + '</div>' +
        '<div class="glance-rule">規則：3 年週報酬的相關係數由高到低取前三名，只列係數本身</div></div>' +
      '</div><p class="muted">兩格各看各的，這裡不把它們加起來。</p>';
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
        return [td(sym), td(e.assetClass || ''), td(e.currency || ''), dateOrDash(e.dataThrough),
                tdRaw(esc(pctText(e.vol1yPct)) + (typeof e.vol1yPct === 'number' ? plain('volatility', { pct: e.vol1yPct, window: '1 年' }) : '')),
                tdRaw(esc(pctText(e.maxDrawdownPct)) + (typeof e.maxDrawdownPct === 'number' ? plain('drawdown', { pct: e.maxDrawdownPct, unknownRecovery: true }) : '')),
                tdRaw(esc(pctText(e.currentDrawdownPct)) + (typeof e.currentDrawdownPct === 'number' ? plain('currentDrawdown', { pct: e.currentDrawdownPct }) : '')),
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

  /* ------------------------------------------------------------ 換匯助手（A1-6）
   *
   * 公開的部分（位置、成本、規則表、歷史模擬）來自 data/analysis/fx.json，開頁就畫。
   * 私人的部分（計畫起始月、月預算、目標總額、期限、已換紀錄）按一下才從【私人】倉庫讀進瀏覽器；算法（預算池模型）與設定檔的鍵名全部在 js/fxplan.js，
   * 這裡只拿算好的結果去畫，表單欄位的 id 用 fxp_ 開頭、不用鍵名；這一段永遠不把任何金額印到主控台。
   * 呈現照 docs/ANALYSIS.md「呈現原則」：圖示＋狀態詞＋這次的數字＋規則一起出現；每個數字下面一行白話；
   * 幾個圖示各自獨立、不加總；名詞點一下有解釋（js/plain.js、js/glossary.js）。
   */
  var FX_FIXED_NOTES = ['人民幣匯率受人民銀行每日中間價管理，政策影響大，依歷史資料訂的規則可靠度低於股票',
                        '以上為量化整理與歷史模擬，未經實盤驗證，不構成投資建議'];
  var FX_NAMES = { CNY: '人民幣', USD: '美元' };
  var FX_ORDER = ['CNY', 'USD'];
  var FX_METHOD_WORD = { A: '固定分批', B: '依位置調整' };

  function term(name, label) { return window.Glossary ? window.Glossary.term(name, label) : esc(label || name); }
  function plain(kind, data) { return window.Plain ? window.Plain.line(kind, data) : ''; }
  function rate3(x) { return num(x, 3); }
  function money(x) { return num(x, 0); }
  function pct1(x) { return typeof x === 'number' ? num(x, 1) + '%' : '—'; }
  function pct3(x) { return typeof x === 'number' ? num(x, 3) + '%' : '—'; }
  function signed(x, nd) { return typeof x === 'number' ? (x > 0 ? '+' : '') + num(x, nd) + '%' : '—'; }
  function between(a, b) { return (a && b) ? dateSpan(a) + '～' + dateSpan(b) : '—'; }
  /* 表頭可以放名詞按鈕（所以表頭不跳脫，呼叫的人自己負責）；列可以帶 class（標出「現在在這一檔」）；手機上橫向捲動 */
  function rawTable(headers, rows, cls) {
    var h = '<div class="fx-wrap"><table class="fx-table' + (cls ? ' ' + cls : '') + '"><thead><tr>' +
      headers.map(function (x) { return '<th>' + x + '</th>'; }).join('') + '</tr></thead><tbody>';
    rows.forEach(function (r) { h += '<tr' + (r.cls ? ' class="' + r.cls + '"' : '') + '>' + (r.cells || r).join('') + '</tr>'; });
    return h + '</tbody></table></div>';
  }
  function currentMonth(now) {
    var t = now ? new Date(now).getTime() : Date.now();
    return new Date(t + 8 * 3600 * 1000).toISOString().slice(0, 7);          // 台北時間的年月
  }
  function fxDecision(fx) {
    var d = fx && fx.backtest && fx.backtest.decision;
    return (d && d.default === 'B') ? 'B' : 'A';                              // 沒有裁決資料就當作沒過門檻：固定分批
  }

  /* 一眼看懂：三格並排，各自獨立。前兩格是三態圖示，第三格是比例＋白話一句。 */
  function fxGlance(fx) {
    var c = ((fx && fx.currencies) || {}).CNY || {};
    var P = window.Plain;
    var p5 = (c.percentiles || {})['5y'] || {};
    var sp = c.spreads || {};
    var h = '<div class="glance-row" id="fx-glance">';
    h += P.badge(P.fxPositionState(p5.pct), '人民幣現在');
    h += P.badge(P.costState((sp.spot || {}).spreadPct, (sp.spotSpread1y || {}).medianPct), '換匯成本');
    var rule = c.rule || {};
    var bt = (fx && fx.backtest) || {};
    var method = fxDecision(fx);
    var th = (bt.decision && bt.decision.thresholds) || {};
    var quota = P.sentence('quota', { pct: rule.percentile5y, ratioPct: rule.ratioPct, bucket: rule.bucket });
    var word, because;
    if (typeof rule.ratioPct !== 'number') {
      word = P.NA; because = P.NA;
    } else if (method === 'B') {
      word = '預算池的 ' + num(rule.ratioPct, 0) + '%';
      because = quota;
    } else {
      word = '固定分批：每月換一個月預算';
      because = '歷史模擬裡，依位置調整沒有比每個月換一樣多好到過門檻，所以預設顯示固定分批。規則表對照：' + quota;
    }
    h += '<div class="glance" data-aspect="quota" data-method="' + esc(method) + '">' +
           '<div class="glance-title">本月規則試算</div>' +
           '<div class="glance-main"><span class="glance-word">' + esc(word) + '</span></div>' +
           '<div class="glance-because">' + esc(because) + '</div>' +
           '<div class="glance-rule">規則：歷史模擬要同時過兩個門檻（換得比較便宜的視窗 ≥ ' + num(th.winSharePct, 0) +
             '%、中位數改善 ≥ ' + num(th.medianImprovePct, 1) + '%）才預設顯示規則表的比例，否則顯示固定分批</div>' +
         '</div>';
    return h + '</div>';
  }

  function fxPctCell(x, years) {
    if (!x) return '<td>資料不足' + plain('position', {}) + '</td>';
    if (typeof x.pct !== 'number') return '<td>資料不足<div class="muted">' + esc(x.reason || '') + '</div>' + plain('position', {}) + '</td>';
    return '<td><b>' + pct1(x.pct) + '</b>' +
      '<div class="muted">' + esc(x.window || '') + '，' + between(x.from, x.through) + '</div>' +
      '<div class="muted">最低 ' + rate3(x.low) + '（' + dateSpan(x.lowDate) + '）、最高 ' + rate3(x.high) + '（' + dateSpan(x.highDate) + '）</div>' +
      plain('position', { pct: x.pct, years: years }) + '</td>';
  }
  function fxPosition(fx) {
    var cs = (fx && fx.currencies) || {};
    var h = '<h3>位置：現在的匯率排在歷史的哪裡</h3>';
    h += rawTable(['幣別', '即期賣出（台銀牌告）', '1 年' + term('百分位'), '5 年' + term('百分位'), '10 年' + term('百分位'), '距 1 年低點／高點'],
      FX_ORDER.filter(function (k) { return cs[k]; }).map(function (k) {
        var c = cs[k], p = c.percentiles || {}, l = c.latest || {}, d = c.distance1y || {};
        return [
          '<td>' + esc(FX_NAMES[k] || k) + '<div class="muted">' + esc(k) + '／TWD　資料標籤：' + esc(c.label || '') + '</div></td>',
          '<td><b>' + rate3(l.spotSell) + '</b><div class="muted">' + (l.d ? dateSpan(l.d) : '—') + '</div></td>',
          fxPctCell(p['1y'], 1), fxPctCell(p['5y'], 5), fxPctCell(p['10y'], 10),
          '<td>' + (typeof d.fromLowPct === 'number' ? signed(d.fromLowPct, 2) + '／' + signed(d.fromHighPct, 2) : '資料不足') +
            plain('distance', d) + '</td>'
        ];
      }), 'fx-wide');
    var any = cs.CNY || cs.USD;
    if (any && any.originNote) h += '<p class="muted">' + esc(any.originNote) + '　百分位＝視窗裡小於或等於現在這個價的比例；1 年用日線、5 年與 10 年用週線，都拿最新的即期賣出去比。</p>';
    return h;
  }
  function fxSpreadCell(x, kind, extra) {
    if (!x || typeof (x.spreadPct === undefined ? x.pct : x.spreadPct) !== 'number') return '<td>資料不足' + plain(kind, {}) + '</td>';
    var v = x.spreadPct === undefined ? x.pct : x.spreadPct;
    return '<td><b>' + pct3(v) + '</b><div class="muted">' + (x.d ? dateSpan(x.d) : '—') + (extra ? '　' + extra : '') + '</div>' +
      plain(kind, { pct: v }) + '</td>';
  }
  function fxCosts(fx) {
    var cs = (fx && fx.currencies) || {};
    var h = '<h3>換匯成本：銀行掛的兩個價格差多少</h3>';
    h += rawTable(['幣別', term('即期') + term('價差'), '即期價差的 1 年' + term('中位數'), term('現鈔') + '價差', '現金賣出比即期賣出貴'],
      FX_ORDER.filter(function (k) { return cs[k]; }).map(function (k) {
        var s = cs[k].spreads || {}, m = s.spotSpread1y || {};
        return [
          '<td>' + esc(FX_NAMES[k] || k) + '<div class="muted">資料標籤：' + esc(s.label || cs[k].label || '') + '</div></td>',
          fxSpreadCell(s.spot, 'spread', s.spot ? '即期買入 ' + rate3(s.spot.buy) + '、即期賣出 ' + rate3(s.spot.sell) : ''),
          (typeof m.medianPct === 'number'
            ? '<td><b>' + pct3(m.medianPct) + '</b><div class="muted">' + esc(m.n) + ' 個交易日，' + between(m.from, m.through) +
              '；最小 ' + pct3(m.minPct) + '、最大 ' + pct3(m.maxPct) + '</div></td>'
            : '<td>資料不足<div class="muted">' + esc(m.reason || '') + '</div></td>'),
          fxSpreadCell(s.cash, 'spread', s.cash ? '現金買入 ' + rate3(s.cash.buy) + '、現金賣出 ' + rate3(s.cash.sell) : ''),
          fxSpreadCell(s.cashVsSpot, 'cash', '')
        ];
      }), 'fx-wide');
    h += '<p class="muted">價差＝（銀行賣給你的價 − 銀行向你收的價）÷ 兩者的中間價。網銀或大額換匯的優惠不在計算內。現鈔那兩欄只有當天的值，沒有歷史。</p>';
    return h;
  }
  function fxPoolNote() {
    return term('預算池') + '＝從計畫起始月到現在累積的預算 − 這段期間已經換掉的台幣；每個月把月預算放進去，沒換的錢留在裡面，之後便宜時可以一次多換。';
  }
  function fxRules(fx) {
    var c = ((fx && fx.currencies) || {}).CNY || {};
    var rules = (fx && fx.rules) || {}, rule = c.rule || {};
    var p1 = (c.percentiles || {})['1y'] || {};
    var h = '<h3>規則表（分批表）</h3>';
    h += rawTable(['5 年' + term('百分位'), '本月額度（佔' + term('預算池') + '的比例）', ''], (rules.table || []).map(function (r) {
      var here = r.bucket === rule.bucket;
      return { cls: here ? 'fx-here' : '', cells: [td(r.bucket), td(num(r.ratioPct, 0) + '%' + (r.ratioPct === 0 ? '（觀望）' : '')),
        td(here ? '← 現在在這一檔（5 年百分位 ' + pct1(rule.percentile5y) + '）' : '')] };
    }));
    h += '<p>規則試算：本月額度 <b>' + (typeof rule.ratioPct === 'number' ? '預算池的 ' + num(rule.ratioPct, 0) + '%' : '資料不足') + '</b>　<span class="muted">資料標籤：' +
         esc(rule.label || rules.label || '') + '</span></p>' + plain('quota', { pct: rule.percentile5y, ratioPct: rule.ratioPct, bucket: rule.bucket });
    h += '<p class="muted">' + fxPoolNote() + '</p>';
    h += '<p class="muted">1 年百分位 ' + (typeof p1.pct === 'number' ? pct1(p1.pct) : '資料不足') + '：並列參考，不進規則（1 年太短）。' +
         '有設定期限時另有' + term('保底') + '：池子 ÷ 剩餘月數；同時有設目標總額時，再跟「還差的人民幣 × 現在的即期賣出 ÷ 剩餘月數」比，取較大者。' +
         '本月額度取規則額度與保底的較大者，但不超過池子；三個數字都印出來。</p>';
    return h;
  }
  function fxBacktest(fx) {
    var b = fx && fx.backtest;
    var h = '<h3>' + term('歷史模擬') + '：依位置調整，有沒有比每個月換一樣多好</h3>';
    if (!b || !b.main) return h + '<p class="warn">沒有歷史模擬的結果。預設顯示固定分批。</p>' + plain('backtest', {});
    var d = b.decision || {}, th = d.thresholds || {}, m = b.method || {}, mn = b.main;
    h += '<p id="fx-decision">裁決：預設顯示 <b>' + esc(d.word || FX_METHOD_WORD[fxDecision(fx)]) + '</b>。<span class="muted">' + esc(d.reason || '') +
         '　門檻寫死在程式裡：換得比較便宜的視窗 ≥ ' + num(th.winSharePct, 0) + '% 而且中位數改善 ≥ ' + num(th.medianImprovePct, 1) + '%，只看主要比較。</span></p>';
    var cnySpot = ((((fx.currencies || {}).CNY || {}).spreads || {}).spot || {}).spreadPct;
    h += '<div id="fx-story">' + plain('backtestStory', { winSharePct: mn.winSharePct, tieSharePct: mn.tieSharePct, medianImprovePct: mn.medianImprovePct,
                                                          thresholds: th, spreadPct: cnySpot, bigWins: mn.bigWins, method: fxDecision(fx) }) + '</div>';
    h += plain('backtest', { n: mn.n, months: b.months, winSharePct: mn.winSharePct, tieSharePct: mn.tieSharePct,
                             medianImprovePct: mn.medianImprovePct, worstImprovePct: mn.worstImprovePct });
    if (!mn.n) {
      h += '<p class="warn">資料不足：' + esc(mn.reason || '湊不出視窗') + '</p>';
    } else {
      h += rawTable(['比較', '視窗', 'B 換得比較便宜的視窗', '平手', term('中位數') + '改善', '最差／最好', 'B 在期限前換完的視窗', '平均匯率的中位數（A／B）'],
        [[td('主要比較：B（預算池＋期限保底）對 A（每月固定換一個月預算）；總額一樣'),
          td(mn.n + ' 個（' + (mn.firstWindow || '') + '～' + (mn.lastWindow || '') + '）'),
          td(pct1(mn.winSharePct)), td(pct1(mn.tieSharePct)), td(signed(mn.medianImprovePct, 3)),
          td(signed(mn.worstImprovePct, 3) + '／' + signed(mn.bestImprovePct, 3)), td(pct1(mn.finishedSharePct)),
          td(num(mn.medianRateA, 5) + '／' + num(mn.medianRateB, 5))]], 'fx-wide');
      var pu = mn.pure || {};
      h += '<p class="muted">這些期間每次只往後移一個月，彼此' + term('視窗重疊', '大量重疊') + '；「換得比較便宜的視窗」那個比例要打折看。</p>';
      h += '<p class="muted">' + fxPoolNote() + '</p>';
      h += '<p class="muted">A＝' + esc(m.A || '') + '。B＝' + esc(m.B || '') + '。改善為正表示 B 換到的人民幣比較便宜。</p>';
      var bw = mn.bigWins;
      if (bw && typeof bw.n === 'number') {
        h += '<p class="muted" id="fx-bigwins">改善 ' + num(bw.thresholdPct, 1) + '% 以上的視窗 ' + esc(bw.n) + ' 個' +
             (bw.n ? '（' + esc(bw.firstWindow) + '～' + esc(bw.lastWindow) + ' 起算）；它們單月換最多的那一天：' +
               (bw.events || []).map(function (e) { return dateSpan(e.d) + '（' + esc(e.windows) + ' 個視窗）'; }).join('、') : '') +
             '。' + esc(bw.note || '') + '。</p>';
      }
      h += '<p class="muted" id="fx-pure">純 B（' + esc(m.pureB || '不保底') + '）：沒換完預算的視窗 ' + pct1(pu.notDoneSharePct) + '，一般只花掉 ' + pct1(pu.medianSpentSharePct) +
           ' 的預算；它的平均匯率中位數 ' + num(pu.medianRate, 5) + '，同一批視窗 A 是 ' + num(pu.medianRateA, 5) + '——' + esc(pu.note || '花的錢比較少，不能直接比') + '。</p>';
    }
    h += notesList((b.notes || []).concat([m.percentile || '']).filter(function (x) { return x; }));
    h += '<p class="muted">資料標籤：' + esc(b.label || '') + '；模型：' + esc(b.model || '—') + '；這一輪是 ' + (b.computedOn ? dateSpan(b.computedOn) : '—') +
         ' 算的（每週一重算，其餘天沿用）' + (b.reused ? '，今天沿用' : '') +
         (mn.n ? '。決策點 ' + esc(mn.decisions) + ' 個（' + between(mn.firstDecision, mn.lastDecision) + '）' : '') + '。</p>';
    return h;
  }

  /* 私人的部分：本月試算、已換紀錄與累計、表單。s.result 是 FxPlan.compute 算好的結果（預算池模型）。 */
  function fxTrial(r) {
    if (!r) return '';
    if (!r.ok) {
      return '<p class="warn">設定檔有問題，沒有算：</p><ul>' + (r.errors || []).map(function (e) { return '<li class="warn">' + esc(e) + '</li>'; }).join('') + '</ul>';
    }
    var pool = r.pool, f = r.floor, rows = [];
    rows.push([td('預算池（月初）'), td(money(pool.atMonthStartTwd) + ' 元'),
      td('從 ' + r.since + ' 起 ' + r.monthsIn + ' 個月 × 月預算 ' + money(r.budgetTwd) + ' 元 − 本月以前已換 ' + money(pool.spentBeforeTwd) + ' 元')]);
    rows.push([td('固定分批（A）'), td(money(r.fixed.twd) + ' 元'),
      td('一個月預算' + (r.fixed.twd < r.budgetTwd ? '，但池子裡只有這麼多' : ''))]);
    rows.push([td('規則額度（B，依位置調整）'), td(r.rule ? money(r.rule.twd) + ' 元' : '資料不足'),
      td(r.rule ? '池子 × ' + num(r.rule.ratioPct, 0) + '%（5 年百分位 ' + pct1(r.rule.percentile5y) + '，落在「' + r.rule.bucket + '」）' : '沒有規則試算的資料')]);
    if (!f) {
      rows.push([td('保底'), td('沒有'), td('沒有設定期限，所以沒有保底')]);
    } else if (typeof f.twd !== 'number') {
      rows.push([td('保底'), td('沒有算'), td(f.reason || '')]);
    } else {
      var how = '池子 ÷ 剩餘 ' + f.monthsLeft + ' 個月（到 ' + f.deadline + '）＝ ' + money(f.poolPartTwd) + ' 元';
      if (f.goalPartTwd !== null) {
        how += '；目標那一半：月初還差 ' + num(f.atMonthStartCny, 0) + ' 人民幣 × ' + f.spotDate + ' 的即期賣出 ' + rate3(f.spotSell) + ' ÷ ' + f.monthsLeft +
               ' 個月＝ ' + money(f.goalPartTwd) + ' 元；兩個取較大者';
      } else if (f.goalNote) {
        how += '；' + f.goalNote;
      }
      rows.push([td('保底'), td(money(f.twd) + ' 元'), td(how)]);
    }
    var why = '預設顯示「' + FX_METHOD_WORD[r.method] + '」；' + (f && typeof f.twd === 'number'
      ? '跟保底比取較大者——這次是' + (r.quotaFrom === 'floor' ? '保底比較大' : '「' + FX_METHOD_WORD[r.method] + '」比較大或一樣')
      : '沒有保底可以比');
    if (r.cappedByPool) why += '；算出來是 ' + money(r.uncappedTwd) + ' 元，超過池子了，以池子為上限（手上沒有的錢不能換）';
    rows.push({ cls: 'fx-here', cells: [td('本月額度'), td(r.quotaTwd === null ? '資料不足' : money(r.quotaTwd) + ' 元'), td(why)] });
    rows.push([td('本月已換'), td(money(pool.spentThisMonthTwd) + ' 元'), td(r.month + ' 的紀錄加總')]);
    rows.push([td('本月還沒換的額度'), td(r.leftTwd === null ? '資料不足' : money(r.leftTwd) + ' 元'), td('本月額度 − 本月已換，最少是 0')]);
    rows.push([td('預算池（現在）'), td(money(pool.nowTwd) + ' 元'), td('月初的池子 − 本月已換；沒換的會留到下個月')]);
    var h = '<h4>本月試算（' + dateSpan(r.month) + '）</h4>' + rawTable(['項目', '金額', '怎麼算的'], rows);
    h += '<p class="muted">本月額度用月初的池子算：換完本月額度之後再回來看，「本月還沒換的額度」會是 0，不會把剩下的池子再乘一次比例。</p>';
    if (r.warnings && r.warnings.length) h += '<ul>' + r.warnings.map(function (w) { return '<li class="muted">' + esc(w) + '</li>'; }).join('') + '</ul>';
    return h;
  }
  function fxRecords(r) {
    if (!r || !r.ok) return '';
    var h = '<h4>已換紀錄與累計</h4>';
    if (!r.records.length) return h + '<p class="muted">還沒有任何已換紀錄。</p>';
    h += table(['月份', '台幣', '匯率', '換到的人民幣'], r.records.map(function (x) {
      return [dateCell(x.month), td(money(x.twd)), td(num(x.rate, 4)), td(num(x.cny, 2))];
    }));
    h += '<p>累計：台幣 ' + money(r.doneTwd) + ' 元，換到人民幣 ' + num(r.doneCny, 2) + '，平均匯率 ' + num(r.averageRate, 4) + '。</p>';
    if (r.goal) {
      h += '<p>目標總額 ' + num(r.goal.cny, 0) + ' 人民幣，還差 ' + num(r.goal.remainingCny, 2) + '。</p>';
    }
    return h;
  }

  /* 表單：欄位 id 用 fxp_ 開頭（fxp_start、fxp_budget、fxp_target、fxp_deadline、fxp_m_0…）——刻意不用設定檔的鍵名。 */
  function renderFxForm(values, canEdit) {
    var v = values || {};
    var dis = canEdit ? '' : ' disabled';
    var recs = (v.records || []).slice();
    recs.push({ month: '', twd: '', rate: '' });                              // 最後永遠留一列空白可以填
    var h = '<form id="fxp-form" onsubmit="return false;"><table><tbody>';
    h += '<tr><td><label for="fxp_start">計畫起始月（YYYY-MM）</label></td><td><input type="text" id="fxp_start" name="fxp_start" placeholder="2026-10" value="' + esc(v.start || '') + '"' + dis + '></td></tr>';
    h += '<tr><td><label for="fxp_budget">每月預算（台幣）</label></td><td><input type="number" min="0" step="1" id="fxp_budget" name="fxp_budget" value="' + esc(v.budget === undefined ? '' : v.budget) + '"' + dis + '></td></tr>';
    h += '<tr><td><label for="fxp_target">目標總額（人民幣，選填）</label></td><td><input type="number" min="0" step="1" id="fxp_target" name="fxp_target" value="' + esc(v.target === undefined ? '' : v.target) + '"' + dis + '></td></tr>';
    h += '<tr><td><label for="fxp_deadline">期限（YYYY-MM，選填）</label></td><td><input type="text" id="fxp_deadline" name="fxp_deadline" placeholder="2027-06" value="' + esc(v.deadline || '') + '"' + dis + '></td></tr>';
    h += '</tbody></table>';
    h += '<table id="fxp-records"><thead><tr><th>月份（YYYY-MM）</th><th>台幣</th><th>當時的匯率</th></tr></thead><tbody>';
    recs.forEach(function (r, i) {
      h += '<tr><td><input type="text" id="fxp_m_' + i + '" name="fxp_m_' + i + '" placeholder="2026-09" value="' + esc(r.month || '') + '"' + dis + '></td>' +
           '<td><input type="number" min="0" step="1" id="fxp_t_' + i + '" name="fxp_t_' + i + '" value="' + esc(r.twd === undefined ? '' : r.twd) + '"' + dis + '></td>' +
           '<td><input type="number" min="0" step="0.0001" id="fxp_r_' + i + '" name="fxp_r_' + i + '" value="' + esc(r.rate === undefined ? '' : r.rate) + '"' + dis + '></td></tr>';
    });
    h += '</tbody></table>';
    h += canEdit
      ? '<p><button type="button" id="fxp-save">儲存到私人倉庫</button>　<span class="muted">只寫到你自己的私人倉庫；commit 訊息不含任何數字。預算池從計畫起始月開始累積。要多填一筆：先存檔，下面會多出一列空白。要刪一筆：把那一列三格都清空再存。</span></p>'
      : '<p class="muted">要編輯：先到設定頁貼上同步金鑰並開啟雲端同步。</p>';
    return h + '<div id="fxp-msg"></div></form>';
  }
  function readFxForm(root) {
    var r = root || document;
    function val(id) { var el = r.querySelector('#' + id); return el ? String(el.value).trim() : ''; }
    var out = { start: val('fxp_start'), budget: val('fxp_budget'), target: val('fxp_target'), deadline: val('fxp_deadline'), records: [] };
    for (var i = 0; r.querySelector('#fxp_m_' + i); i++) {
      var rec = { month: val('fxp_m_' + i), twd: val('fxp_t_' + i), rate: val('fxp_r_' + i) };
      if (rec.month === '' && rec.twd === '' && rec.rate === '') continue;      // 三格都空＝沒有這一筆
      out.records.push(rec);
    }
    return out;
  }
  function renderFxPlan(state) {
    var s = state || { status: 'unset' };
    var h = '<h3>我的換匯設定與本月試算（只在瀏覽器端）</h3>';
    h += '<p class="muted">計畫起始月、每月預算、目標總額、期限、已換紀錄只從你的私人倉庫讀進瀏覽器計算，不上傳、不寫進任何公開檔。</p>';
    h += '<p><button type="button" id="fxp-load">讀取我的換匯設定（從私人倉庫）</button>　<span id="fxp-status" class="muted"></span></p>';
    if (s.status === 'loading') {
      h += '<p class="muted">讀取中…</p>';
    } else if (s.status === 'error') {
      h += '<p class="warn">讀不到：' + esc(s.reason || '') + '</p>';
    } else if (s.status === 'set') {
      h += '<div id="fxp-result">' + fxTrial(s.result) + fxRecords(s.result) + '</div>';
      h += '<h4>設定檔</h4><div id="fxp-formbox">' + renderFxForm(s.formValues, !!s.canEdit) + '</div>';
    } else {
      h += '<p id="fxp-unset"><b>未設定</b>' + (s.reason ? '　<span class="muted">' + esc(s.reason) + '</span>' : '') +
           '　<span class="muted">沒有設定也看得到上面的位置、成本、規則表與歷史模擬；金額要有設定才算。</span></p>';
      if (s.showForm) h += '<h4>建立設定檔</h4><div id="fxp-formbox">' + renderFxForm(s.formValues, !!s.canEdit) + '</div>';
    }
    return h;
  }

  function renderFx(fx, planState) {
    var h = '<h2>換匯助手（人民幣）</h2>';
    if (!window.Plain || !window.Glossary || !window.FxPlan) {
      return h + '<p class="warn">這一頁沒有載到 js/plain.js／js/glossary.js／js/fxplan.js，換匯助手畫不出來。</p>';
    }
    if (!fx) {
      h += '<p class="warn">讀不到 data/analysis/fx.json——分析還沒跑過，或檔案缺失。</p>';
    } else {
      h += '<p class="muted">產生時間 ' + dateSpan(fx.generatedAt) + '。' + esc((fx.notes || [])[0] || '') + '</p>';
      h += '<h3>一眼看懂</h3>' + fxGlance(fx);
      h += '<p class="muted">三格各看各的，這裡不把它們加起來。有底線的名詞點一下有解釋。</p>';
      h += fxPosition(fx) + fxCosts(fx) + fxRules(fx) + fxBacktest(fx);
    }
    h += '<div id="fx-plan">' + renderFxPlan(planState) + '</div>';
    h += '<ul class="fx-fixed-notes">' + FX_FIXED_NOTES.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>';
    return h;
  }

  /* ------------------------------------------------------------ 總覽表（A1-7）
   *
   * 每個標的一列、每個面向一格（圖示＋狀態詞）。狀態跟儀表板卡片上的圖示列是同一份（js/card-analysis.js 的 aspects），
   * 位置用同一個燈號函式（js/indicators.js），所以要多讀 latest.json 與每個標的的日線。
   * 預設順序＝儀表板的分組與順序；點面向的欄位名稱照那一欄排序（同狀態照名稱；沒有狀態的永遠排最後），「回到預設順序」恢復。
   * 點一列展開那個標的每個面向的數字、規則、白話。這張表不顯示任何計數或加總——各格各看各的。
   */
  var OV_GROUPS = ['貴金屬', '台股', '海外', '匯率'];       // 跟儀表板同一個順序
  var OV_RANK = { low: 0, mid: 1, high: 2 };                // 三態的名次；其餘沒有名次，永遠排最後
  var OV_BLANK = { na: 0, error: 1, none: 2 };              // 沒有名次的彼此之間：資料不足、暫時讀不到、不適用

  /* 最上面那一行：分析上次是幾時跑的、有沒有錯。分析壞了要一眼看得出來，不能讓人以為下面的狀態是新的。 */
  function statusBanner(st) {
    if (!st) return '<p class="warn">讀不到分析狀態（data/analysis/status.json）：下面的數字不知道是幾時算的。</p>';
    return '<p class="' + (st.ok ? 'ok' : 'warn') + '">分析上次執行：<b class="date">' + esc(st.lastRun || st.generatedAt) + '</b>　' +
      (st.ok ? '這一輪沒有錯誤' : '這一輪有錯，下面的數字可能是舊的或不完整（錯誤列在「分析狀態」那一段）') + '</p>';
  }

  function overviewRows(data) {
    var m = data.market || {}, latest = m.latest, CA = window.CardAnalysis;
    if (!latest || !latest.assets || !CA || !CA.aspects) return [];
    var ids = Object.keys(latest.assets);
    function groupOf(id) { return latest.assets[id].group || '其他'; }
    var groups = OV_GROUPS.filter(function (g) { return ids.some(function (id) { return groupOf(id) === g; }); });
    ids.forEach(function (id) { if (groups.indexOf(groupOf(id)) < 0) groups.push(groupOf(id)); });
    var rows = [];
    groups.forEach(function (g) {
      ids.forEach(function (id) {
        if (groupOf(id) !== g) return;
        var hist = (m.histories || {})[id];
        rows.push({ asset: latest.assets[id], order: rows.length,
                    aspects: CA.aspects(latest.assets[id], { points: (hist && hist.points) || [], historyFailed: !hist,
                                                             risk: data.risk, cost: data.cost, fx: data.fx }) });
      });
    });
    return rows;
  }

  function priceCell(a) {
    if (a.status !== 'ok') return '<td>—<div class="muted">更新失敗</div></td>';
    var fr = window.Freshness ? window.Freshness.classify(a, new Date()) : null;
    if (fr && fr.hidePrice) return '<td>—<div class="muted">無法判斷新舊</div></td>';
    var cur = ' <span class="muted">' + esc(a.currency || '') + '</span>';
    if (a.type === 'bot_gold_bar') {
      var top = (a.bars || [])[0];
      return '<td>' + (top ? num(top.sell, 0) : '—') + cur + '<div class="muted">1 公斤掛牌</div></td>';
    }
    return '<td>' + num(a.price, a.decimals) + cur + '</td>';
  }

  function renderOverview(data) {
    var h = '<div id="ov-status">' + statusBanner(data.status) + '</div><h2>總覽</h2>';
    var CA = window.CardAnalysis;
    if (!CA || !CA.aspects || !window.Indicators || !window.Plain) {
      return h + '<p class="warn">這一頁沒有載到 js/indicators.js／js/card-analysis.js／js/plain.js，總覽表畫不出來。</p>';
    }
    if (!data.market || !data.market.latest) return h + '<p class="warn">讀不到 data/latest.json，總覽表畫不出來。</p>';
    var rows = overviewRows(data);
    var names = CA.ASPECT_ORDER.map(function (k) { return window.Plain.ASPECTS[k].name; });
    h += '<p class="muted">每個標的一列，每個' + term('面向') + '一格；圖示旁邊的字是' + term('狀態詞') + '。各格各看各的，這張表不把它們加起來。' +
         '點一列看數字、規則與白話；點「' + esc(names.join('」「')) + '」可以照那一欄排序。</p>';
    h += '<p><button type="button" id="ov-reset">回到預設順序</button>　<span class="muted" id="ov-order">現在的順序：預設（跟儀表板一樣）</span></p>';
    h += '<div class="ov-wrap"><table class="ov-table" id="ov-table"><thead><tr><th class="ov-name">標的</th><th>現價</th><th>位置的資料日期</th>' +
         CA.ASPECT_ORDER.map(function (k, i) {
           return '<th class="ov-sort" data-aspect="' + esc(k) + '" aria-sort="none"><button type="button" class="ov-sortbtn">' + esc(names[i]) + '</button></th>';
         }).join('') + '</tr></thead>';
    rows.forEach(function (r) {
      var a = r.asset;
      h += '<tbody class="ov-item" data-id="' + esc(a.id) + '" data-order="' + r.order + '" data-name="' + esc(a.name) + '"' +
           r.aspects.map(function (x) { return ' data-s-' + esc(x.aspect) + '="' + esc(x.state.key) + '"'; }).join('') + '>' +
           '<tr class="ov-row">' +
             '<td class="ov-name"><button type="button" class="ov-toggle" aria-expanded="false">' + esc(a.name) + '</button>' +
               '<div class="muted">' + esc(a.group || '') + '</div></td>' +
             priceCell(a) + dateCell(a.date) +
             r.aspects.map(function (x) { return '<td class="ov-asp" data-aspect="' + esc(x.aspect) + '">' + CA.cell(x) + '</td>'; }).join('') +
           '</tr>' +
           '<tr class="ov-detail" hidden><td colspan="' + (3 + r.aspects.length) + '"><div class="ov-detail-grid">' +
             r.aspects.map(function (x) { return '<div class="asp-detail" data-aspect="' + esc(x.aspect) + '">' + CA.detail(x) + '</div>'; }).join('') +
           '</div></td></tr></tbody>';
    });
    h += '</table></div>';
    h += '<p class="muted">位置就是卡片上的燈號（日線，日期在第三欄）；風險用週線、成本各有各的日期，都寫在展開的細節裡。' +
         '「資料不足」是之後會有、現在還不夠；「不適用」是這一類標的沒有這個面向。</p>';
    return h;
  }

  /* aspect 是 null＝回到預設順序；dir 是 asc（低的那一態在前）或 desc */
  function sortOverview(table, aspect, dir) {
    var items = Array.prototype.slice.call(table.querySelectorAll('tbody.ov-item'));
    items.sort(function (x, y) {
      if (!aspect) return parseInt(x.getAttribute('data-order'), 10) - parseInt(y.getAttribute('data-order'), 10);
      var kx = x.getAttribute('data-s-' + aspect), ky = y.getAttribute('data-s-' + aspect);
      var a = OV_RANK[kx], b = OV_RANK[ky];
      var an = a === undefined, bn = b === undefined;
      if (an !== bn) return an ? 1 : -1;                                   // 沒有狀態的永遠排最後，升冪降冪都一樣
      if (an && kx !== ky) return (OV_BLANK[kx] === undefined ? 9 : OV_BLANK[kx]) - (OV_BLANK[ky] === undefined ? 9 : OV_BLANK[ky]);
      if (!an && a !== b) return dir === 'desc' ? b - a : a - b;
      return String(x.getAttribute('data-name')).localeCompare(String(y.getAttribute('data-name')), 'zh-Hant');   // 同狀態照名稱
    });
    items.forEach(function (it) { table.appendChild(it); });
    Array.prototype.forEach.call(table.querySelectorAll('th.ov-sort'), function (th) {
      var on = aspect && th.getAttribute('data-aspect') === aspect;
      th.classList.remove('asc', 'desc');
      if (on) th.classList.add(dir === 'desc' ? 'desc' : 'asc');
      th.setAttribute('aria-sort', on ? (dir === 'desc' ? 'descending' : 'ascending') : 'none');
    });
  }

  function wireOverview(root) {
    var table = root && root.querySelector('#ov-table');
    if (!table) return;
    var note = root.querySelector('#ov-order');
    function say(aspect, dir) {
      if (!note) return;
      if (!aspect) { note.textContent = '現在的順序：預設（跟儀表板一樣）'; return; }
      var a = window.Plain.ASPECTS[aspect], w = a.words;
      var seq = dir === 'desc' ? [w.high, w.mid, w.low] : [w.low, w.mid, w.high];
      note.textContent = '現在的順序：照「' + a.name + '」排（' + seq.join(' → ') + '；同一個狀態照名稱；資料不足與不適用排最後）';
    }
    // 欄名與列名都是真的按鈕（鍵盤按 Enter／空白鍵就是點它）；點擊掛在整格／整列上，滑鼠點旁邊也有效
    Array.prototype.forEach.call(table.querySelectorAll('th.ov-sort'), function (th) {
      th.addEventListener('click', function () {
        var dir = th.classList.contains('asc') ? 'desc' : 'asc';
        sortOverview(table, th.getAttribute('data-aspect'), dir);
        say(th.getAttribute('data-aspect'), dir);
      });
    });
    var reset = root.querySelector('#ov-reset');
    if (reset) reset.addEventListener('click', function () { sortOverview(table, null); say(null); });
    Array.prototype.forEach.call(table.querySelectorAll('tr.ov-row'), function (row) {
      row.addEventListener('click', function () {
        var det = row.nextElementSibling, btn = row.querySelector('button.ov-toggle');
        if (!det) return;
        det.hidden = !det.hidden;
        row.classList.toggle('is-open', !det.hidden);
        if (btn) btn.setAttribute('aria-expanded', det.hidden ? 'false' : 'true');
        // 手機上整張表可以橫向捲動，細節要貼著看得到的那一段，不要跟著表格一起變寬
        var grid = det.querySelector('.ov-detail-grid'), wrap = table.parentNode;
        if (grid && wrap && wrap.clientWidth) grid.style.width = Math.max(240, wrap.clientWidth - 24) + 'px';
      });
    });
  }

  /* ------------------------------------------------------------ 組合 */
  function wireTerms(box) { if (box && window.Glossary) window.Glossary.wire(box); }

  function render(data, targets) {
    targets = targets || {};
    var pick = function (key) { return targets[key] || document.getElementById(key); };
    var s = pick('status'), r = pick('risk'), d = pick('decompose'), c = pick('cost'), k = pick('concentration');
    var o = pick('overview');
    if (o && data.market !== undefined) {
      o.innerHTML = renderOverview(data);
      wireOverview(o);
    }
    var fxBox = pick('fx');
    if (fxBox && data.fx !== undefined) fxBox.innerHTML = renderFx(data.fx, data.fxPlan);
    if (s) s.innerHTML = renderStatus(data.status);
    if (r) r.innerHTML = renderRisk(data.risk);
    if (d) d.innerHTML = renderDecompose(data.decompose);
    if (c) c.innerHTML = renderCost(data.cost, data.staticCosts);
    if (k) k.innerHTML = renderConcentration(data.concentration);
    var x = pick('adhoc');
    if (x && data.adhoc) x.innerHTML = renderAdhoc(data.adhoc);
    [o, fxBox, r, d, c, k, x].forEach(wireTerms);             // 名詞按鈕：畫完之後掛上點擊
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
    function setState(state) { section.innerHTML = renderConcentration(state); wire(); wireTerms(section); }
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
    function setState(state) { current = state; section.innerHTML = renderAdhoc(state); wire(); wireTerms(section); }
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

  /* 換匯助手的互動：按一下才去私人倉庫讀；儲存前驗證、確認倉庫是私人的。只重畫 #fx-plan 那一塊；這裡不把任何值印到主控台。 */
  function wireFx(fx) {
    var box = document.getElementById('fx-plan');
    if (!box || !window.FxPlan) return;
    var F = window.FxPlan;
    function result(plan) { return F.compute(plan, ((fx && fx.currencies) || {}).CNY, currentMonth(), fxDecision(fx)); }
    function setState(state) { box.innerHTML = renderFxPlan(state); wire(); wireTerms(box); }
    function canEdit() { return !!(window.Storage && window.Storage.getMode() === 'github' && window.Storage.hasPat()); }
    function loadPlan() {
      setState({ status: 'loading' });
      return window.Storage.loadFile(F.FILE).then(function (r) {
        if (!r.data) {
          var reason = r.reason === 'not-github' ? '本機模式或還沒貼同步金鑰：到設定頁開啟雲端同步後再讀' : '私人倉庫裡還沒有 ' + F.FILE;
          var blankValues = F.toFormValues(F.emptyTemplate());
          blankValues.start = currentMonth();                                 // 新的設定檔：計畫起始月先帶這個月，看得到、可以改
          setState({ status: 'unset', reason: reason, showForm: canEdit(), canEdit: canEdit(), formValues: blankValues });
          return;
        }
        setState({ status: 'set', result: result(r.data), formValues: F.toFormValues(r.data), canEdit: canEdit() });
      }).catch(function (e) { setState({ status: 'error', reason: e && e.message }); });
    }
    function savePlan() {
      var msg = document.getElementById('fxp-msg');
      var plan = F.fromForm(readFxForm(box));
      var v = F.validate(plan);
      if (!v.ok) { msg.innerHTML = '<p class="warn">沒有存：' + v.errors.map(esc).join('；') + '</p>'; return; }
      msg.innerHTML = '<p class="muted">確認倉庫是私人的、寫入中…' + (v.warnings.length ? '（提醒：' + esc(v.warnings.join('；')) + '）' : '') + '</p>';
      window.Storage.testConnection().then(function () {
        return window.Storage.saveFile(F.FILE, plan, 'fx-plan 更新');             // commit 訊息固定這幾個字，不帶任何數字
      }).then(function () {
        setState({ status: 'set', result: result(plan), formValues: F.toFormValues(plan), canEdit: true });
        var m2 = document.getElementById('fxp-msg');
        if (m2) m2.innerHTML = '<p class="ok">已存到私人倉庫，上面是用新設定算的。</p>';
      }).catch(function (e) { msg.innerHTML = '<p class="warn">沒有存：' + esc(e && e.message) + '</p>'; });
    }
    function wire() {
      var b = document.getElementById('fxp-load');
      if (b) b.addEventListener('click', function () {
        if (!window.Storage || !window.Lock) { setState({ status: 'error', reason: '這一頁沒有載到 storage.js／lock.js' }); return; }
        window.Lock.gate(function () { window.Storage.init().then(loadPlan); });
      });
      var sv = document.getElementById('fxp-save');
      if (sv) sv.addEventListener('click', savePlan);
    }
    wire();
  }

  /* 總覽表要的行情與日線：latest.json ＋ 每個標的一檔日線（位置的燈號跟儀表板用同一份資料、同一個函式算） */
  function loadMarket() {
    return fetchJSON('data/latest.json').then(function (latest) {
      if (!latest || !latest.assets) return { latest: null, histories: {} };
      var ids = Object.keys(latest.assets);
      return Promise.all(ids.map(function (id) { return fetchJSON('data/history/' + id + '.json'); })).then(function (hs) {
        var histories = {};
        ids.forEach(function (id, i) { histories[id] = hs[i]; });
        return { latest: latest, histories: histories };
      });
    });
  }

  function boot() {
    Promise.all([fetchJSON('data/analysis/status.json'), fetchJSON('data/analysis/risk.json'), fetchJSON('data/analysis/decompose.json'),
                 fetchJSON('data/analysis/cost.json'), fetchJSON('data/analysis/static-costs.json'), fetchJSON('data/analysis/fx.json'),
                 loadMarket()])
      .then(function (all) {
        render({ status: all[0], risk: all[1], decompose: all[2], cost: all[3], staticCosts: all[4], concentration: { status: 'unset' },
                 adhoc: { status: 'idle' }, fx: all[5], fxPlan: { status: 'unset' }, market: all[6] });
        wireConcentration(all[1]);
        wireAdhoc();
        wireFx(all[5]);
        wireSorting();
        // 從卡片的「換匯助手 →」進來（網址帶 #fx）：內容是讀完檔才畫的，畫好之後再捲到那一段
        var hash = (window.location.hash || '').slice(1);
        var target = hash && document.getElementById(hash);
        if (target && target.scrollIntoView) target.scrollIntoView();
      });
  }

  window.AnalysisDebug = { boot: boot, render: render, renderStatus: renderStatus, renderRisk: renderRisk,
                           renderDecompose: renderDecompose, renderCost: renderCost, renderConcentration: renderConcentration,
                           renderProfileForm: renderProfileForm, readFormValues: readFormValues, renderAdhoc: renderAdhoc,
                           renderAdhocDetail: renderAdhocDetail, esc: esc, num: num,
                           sortTable: sortTable, wireSorting: wireSorting, corrColor: corrColor,
                           renderFx: renderFx, renderFxPlan: renderFxPlan, renderFxForm: renderFxForm, readFxForm: readFxForm,
                           fxGlance: fxGlance, fxDecision: fxDecision, currentMonth: currentMonth,
                           // A1-7：總覽表與狀態橫幅
                           renderOverview: renderOverview, overviewRows: overviewRows, sortOverview: sortOverview, wireOverview: wireOverview,
                           statusBanner: statusBanner, adhocGlance: adhocGlance, corrSentences: corrSentences };
})();
