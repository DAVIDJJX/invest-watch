/*
 * records.js — 「我的紀錄」頁邏輯
 *
 * 隱私：這一頁處理的每一個數字都只在你的瀏覽器裡，
 *       存放位置由 js/storage.js 決定（本機 localStorage，或再同步到你的私人倉庫）。
 *       它會去讀公開的 data/latest.json 與 data/history/*.json 來取得市價與指標，
 *       但**從來不會把任何個人數字寫回去**。
 */
(function () {
  'use strict';

  var state = {
    data: null,          // portfolio 資料
    market: {},          // data/latest.json 的 assets
    monitored: [],       // 監控清單（下拉選單用）
    indicators: {},      // 各監控標的的指標
    rows: [],            // 算好的持倉
    warning: null
  };

  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function num(v, dec) {
    if (typeof v !== 'number' || !isFinite(v)) return '—';
    return v.toLocaleString('zh-TW', {
      minimumFractionDigits: dec === undefined ? 0 : dec,
      maximumFractionDigits: dec === undefined ? 0 : dec
    });
  }

  function pct(v, dec) {
    if (typeof v !== 'number' || !isFinite(v)) return '—';
    return (v >= 0 ? '+' : '') + v.toFixed(dec === undefined ? 2 : dec) + '%';
  }

  function dirClass(v) {
    if (typeof v !== 'number' || !isFinite(v) || v === 0) return 'flat';
    return v > 0 ? 'up' : 'down';
  }

  function today() {
    var d = new Date();
    return d.getFullYear() + '-' +
           String(d.getMonth() + 1).padStart(2, '0') + '-' +
           String(d.getDate()).padStart(2, '0');
  }

  function fetchJSON(path) {
    return fetch(path + (path.indexOf('?') < 0 ? '?' : '&') +
                 't=' + Math.floor(Date.now() / 60000), { cache: 'no-cache' })
      .then(function (r) {
        if (!r.ok) throw new Error(path + '（HTTP ' + r.status + '）');
        return r.json();
      });
  }

  /* ---------------------------------------------------------- 儲存 */

  function flash(text, cls) {
    var el = $('#save-msg');
    if (!el) return;
    el.textContent = text;
    el.className = 'refresh-msg ' + (cls || 'dim');
    clearTimeout(flash._t);
    flash._t = setTimeout(function () { el.textContent = ''; }, 6000);
  }

  function persist() {
    return window.Storage.save(state.data)
      .then(function (r) {
        if (r.warning) flash(r.warning, 'bad');
        else flash(r.synced ? '已儲存並同步到私人倉庫' : '已儲存在這台裝置', 'ok');
        renderSyncChip();
      })
      .catch(function (e) { flash('儲存失敗：' + e.message, 'bad'); });
  }

  function renderSyncChip() {
    var chip = $('#sync-chip');
    var mode = window.Storage.getMode();
    var n = (state.data.assets || []).length;
    var t = (state.data.trades || []).length;
    if (mode === 'github') {
      chip.className = 'chip ok';
      chip.textContent = '☁ 已同步到 ' + window.Storage.getRepo() +
                         '　' + n + ' 個標的 / ' + t + ' 筆紀錄';
    } else {
      chip.className = 'chip';
      chip.textContent = '💾 只存在這台裝置　' + n + ' 個標的 / ' + t + ' 筆紀錄';
    }
    var pv = $('#privacy-sync');
    if (pv) {
      pv.innerHTML = mode === 'github'
        ? '，以及你的<b>私人</b>倉庫 ' + esc(window.Storage.getRepo())
        : '（還沒開啟跨裝置同步，可到<a href="settings.html">設定</a>頁設定）';
    }
  }

  /* ---------------------------------------------------------- 總覽 */

  function renderOverview() {
    var rows = window.Portfolio.activeHoldings(state.rows);
    var tot = window.Portfolio.totals(rows);
    var alloc = window.Portfolio.allocation(rows);

    var h = '';
    h += ovBox('總現值', tot.counted ? num(tot.value) : '—', 'TWD',
               tot.skipped ? tot.skipped + ' 項沒有市價，未計入' : null);
    h += ovBox('總成本', tot.counted ? num(tot.cost) : '—', 'TWD', null);
    h += ovBox('未實現損益',
               tot.counted ? (tot.pnl >= 0 ? '+' : '') + num(tot.pnl) : '—',
               'TWD', tot.pnlPct === null ? null : pct(tot.pnlPct),
               dirClass(tot.pnl));
    if (Math.abs(tot.realized) > 0.5) {
      h += ovBox('已實現損益', (tot.realized >= 0 ? '+' : '') + num(tot.realized),
                 'TWD', '賣出時結算', dirClass(tot.realized));
    }
    $('#ov-nums').innerHTML = h;

    // 圓餅圖
    var legend = '';
    alloc.items.forEach(function (x) {
      legend += '<span class="pie-key"><i style="background:' +
                (window.Charts.PIE_COLORS[x.kind] || '#64748b') + '"></i>' +
                esc(x.kind) + '<b>' + x.pct.toFixed(1) + '%</b></span>';
    });
    if (alloc.unknown.length) {
      legend += '<span class="pie-note">未計入（沒有市價）：' +
                esc(alloc.unknown.join('、')) + '</span>';
    }
    if (!alloc.items.length) {
      legend = '<span class="pie-note">還沒有可以計算現值的持倉。</span>';
    }
    $('#pie-legend').innerHTML = legend;
    window.Charts.drawPie($('#pie-chart'), alloc.items);
  }

  function ovBox(label, value, unit, note, cls) {
    return '<div class="ov-box">' +
             '<div class="ov-label">' + esc(label) + '</div>' +
             '<div class="ov-value ' + (cls || '') + '">' + esc(value) +
               '<span class="ov-unit">' + esc(unit || '') + '</span></div>' +
             (note ? '<div class="ov-note">' + esc(note) + '</div>' : '') +
           '</div>';
  }

  /* ---------------------------------------------------------- 持倉表 */

  var PRICE_SOURCE_TEXT = {
    market: '自動抓取的市價',
    manual: '你自己輸入的淨值',
    cash: '現金餘額',
    none: '沒有市價',
    error: '市價來源更新失敗'
  };

  function signalCell(row) {
    if (row.isCash) return '<td class="al-left rp-dim">—</td>';
    var ind = row.ind;
    if (!ind) {
      return '<td class="al-left rp-dim">' +
             (row.asset.linkId ? '指標計算中' : '未連結監控標的') + '</td>';
    }
    var sig = ind.signal;
    var rsi = ind.rsi14;
    return '<td class="al-left">' +
             '<span class="signal ' + sig.level + '"><span class="dot"></span>' +
             esc(sig.label) + '</span>' +
             '<div class="cell-sub">' + esc(sig.rule) +
             (rsi === null ? '' : '　RSI ' + rsi.toFixed(0)) + '</div>' +
           '</td>';
  }

  function renderHoldings() {
    var rows = window.Portfolio.activeHoldings(state.rows);
    var closed = window.Portfolio.closedHoldings(state.rows);
    var box = $('#holdings');

    if (!rows.length && !closed.length) {
      box.innerHTML = '<div class="placeholder">' +
        '<div class="big">📒</div><h2>還沒有任何紀錄</h2>' +
        '<p>先按下面的「新增標的」建立你要追蹤的項目（例如台積電、黃金存摺、某檔基金），' +
        '再用「新增一筆紀錄」記下每次進場。所有數字只會存在你自己的裝置上。</p></div>';
      return;
    }

    var h = '<div class="table-scroll"><table class="rp-table holdings"><thead><tr>' +
            '<th class="al-left">標的</th>' +
            '<th class="al-right">數量</th>' +
            '<th class="al-right">平均成本</th>' +
            '<th class="al-right">市價</th>' +
            '<th class="al-right">現值</th>' +
            '<th class="al-right">損益</th>' +
            '<th class="al-left">加碼參考</th>' +
            '<th class="al-right">操作</th>' +
            '</tr></thead><tbody>';

    rows.forEach(function (r) {
      var dec = typeof r.decimals === 'number' ? r.decimals : 2;
      h += '<tr>';
      h += '<td class="al-left"><b>' + esc(r.name) + '</b>' +
           '<div class="cell-sub">' + esc(r.kind) +
           (r.asset.note ? '　' + esc(r.asset.note) : '') + '</div></td>';

      if (r.isCash) {
        h += '<td class="al-right rp-dim">—</td>';
        h += '<td class="al-right rp-dim">—</td>';
        h += '<td class="al-right rp-dim">—</td>';
        h += '<td class="al-right"><b>' + num(r.value) + '</b></td>';
        h += '<td class="al-right rp-dim">—</td>';
      } else if (!r.tradeCount) {
        // 剛建立、還沒有交易紀錄的標的：明白說出來，不要只顯示一排破折號
        h += '<td class="al-right rp-dim" colspan="5">還沒有交易紀錄' +
             '<div class="cell-sub">按上面的「＋ 新增一筆紀錄」記下第一次進場</div></td>';
      } else {
        h += '<td class="al-right">' + num(r.qty, r.qty % 1 ? 4 : 0) + '</td>';
        h += '<td class="al-right">' + num(r.avgCost, dec) + '</td>';
        h += '<td class="al-right">' +
             (r.price === null
               ? '<span class="rp-dim">—</span>'
               : num(r.price, dec)) +
             '<div class="cell-sub">' + esc(PRICE_SOURCE_TEXT[r.priceSource] || '') +
             (r.priceDate ? '　' + esc(r.priceDate) : '') + '</div></td>';
        h += '<td class="al-right"><b>' +
             (r.value === null ? '—' : num(r.value)) + '</b></td>';
        h += '<td class="al-right ' + dirClass(r.pnl) + '">' +
             (r.pnl === null ? '—' : (r.pnl >= 0 ? '+' : '') + num(r.pnl)) +
             '<div class="cell-sub ' + dirClass(r.pnlPct) + '">' +
             (r.pnlPct === null ? '' : pct(r.pnlPct)) + '</div></td>';
      }

      h += signalCell(r);
      h += '<td class="al-right nowrap">' +
             '<button class="mini" data-edit-asset="' + esc(r.asset.id) + '">編輯</button>' +
           '</td>';
      h += '</tr>';
    });
    h += '</tbody></table></div>';

    if (closed.length) {
      h += '<details class="snap-box" style="margin-top:12px">' +
           '<summary>已清倉的標的（' + closed.length + '）</summary><div>';
      h += '<div class="table-scroll"><table class="rp-table"><thead><tr>' +
           '<th class="al-left">標的</th><th class="al-right">已實現損益</th>' +
           '<th class="al-right">交易筆數</th><th class="al-left">最後一筆</th>' +
           '</tr></thead><tbody>';
      closed.forEach(function (r) {
        h += '<tr><td class="al-left">' + esc(r.name) + '</td>' +
             '<td class="al-right ' + dirClass(r.realized) + '">' +
             (r.realized >= 0 ? '+' : '') + num(r.realized) + '</td>' +
             '<td class="al-right">' + r.tradeCount + '</td>' +
             '<td class="al-left">' + esc(r.lastDate || '—') + '</td></tr>';
      });
      h += '</tbody></table></div></div></details>';
    }

    box.innerHTML = h;
    $$('[data-edit-asset]', box).forEach(function (b) {
      b.addEventListener('click', function () { openAssetForm(b.dataset.editAsset); });
    });
  }

  /* ---------------------------------------------------------- 交易紀錄表 */

  function assetName(id) {
    var a = (state.data.assets || []).filter(function (x) { return x.id === id; })[0];
    return a ? a.name : '（已刪除的標的）';
  }

  function renderTrades() {
    var box = $('#trades');
    var trades = (state.data.trades || []).slice()
      .sort(function (a, b) { return String(b.date).localeCompare(String(a.date)); });
    var navs = (state.data.navs || []).slice()
      .sort(function (a, b) { return String(b.date).localeCompare(String(a.date)); });

    if (!trades.length && !navs.length) {
      box.innerHTML = '<div class="rp-dim" style="padding:14px 2px">還沒有任何交易紀錄。</div>';
      return;
    }

    var h = '';
    if (trades.length) {
      h += '<div class="table-scroll"><table class="rp-table"><thead><tr>' +
           '<th class="al-left">日期</th><th class="al-left">標的</th>' +
           '<th class="al-left">動作</th><th class="al-right">單價</th>' +
           '<th class="al-right">數量</th><th class="al-right">金額</th>' +
           '<th class="al-right">手續費</th><th class="al-left">備註</th>' +
           '<th class="al-right">操作</th></tr></thead><tbody>';
      trades.forEach(function (t) {
        var r = window.Portfolio.resolveTrade(t);
        h += '<tr>' +
             '<td class="al-left nowrap">' + esc(t.date) + '</td>' +
             '<td class="al-left">' + esc(assetName(t.assetId)) + '</td>' +
             '<td class="al-left"><span class="act act-' + esc(t.action) + '">' +
               esc(window.Portfolio.ACTIONS[t.action] || t.action) + '</span></td>' +
             '<td class="al-right">' + num(r.price, r.price && r.price < 100 ? 4 : 2) + '</td>' +
             '<td class="al-right">' + num(r.qty, r.qty && r.qty % 1 ? 4 : 0) + '</td>' +
             '<td class="al-right">' + num(r.amount) + '</td>' +
             '<td class="al-right">' + (t.fee ? num(t.fee) : '—') + '</td>' +
             '<td class="al-left">' + esc(t.note || '') + '</td>' +
             '<td class="al-right nowrap">' +
               '<button class="mini" data-edit-trade="' + esc(t.id) + '">編輯</button> ' +
               '<button class="mini danger" data-del-trade="' + esc(t.id) + '">刪除</button>' +
             '</td></tr>';
      });
      h += '</tbody></table></div>';
    }

    if (navs.length) {
      h += '<details class="snap-box" style="margin-top:12px"><summary>手動輸入的淨值（' +
           navs.length + '）</summary><div>';
      h += '<div class="table-scroll"><table class="rp-table"><thead><tr>' +
           '<th class="al-left">日期</th><th class="al-left">標的</th>' +
           '<th class="al-right">淨值</th><th class="al-right">操作</th>' +
           '</tr></thead><tbody>';
      navs.forEach(function (n) {
        h += '<tr><td class="al-left nowrap">' + esc(n.date) + '</td>' +
             '<td class="al-left">' + esc(assetName(n.assetId)) + '</td>' +
             '<td class="al-right">' + num(n.nav, 4) + '</td>' +
             '<td class="al-right nowrap">' +
               '<button class="mini danger" data-del-nav="' + esc(n.id) + '">刪除</button>' +
             '</td></tr>';
      });
      h += '</tbody></table></div></div></details>';
    }

    box.innerHTML = h;
    $$('[data-edit-trade]', box).forEach(function (b) {
      b.addEventListener('click', function () { openTradeForm(b.dataset.editTrade); });
    });
    $$('[data-del-trade]', box).forEach(function (b) {
      b.addEventListener('click', function () { delTrade(b.dataset.delTrade); });
    });
    $$('[data-del-nav]', box).forEach(function (b) {
      b.addEventListener('click', function () { delNav(b.dataset.delNav); });
    });
  }

  /* ---------------------------------------------------------- 對話框 */

  var modalSubmit = null;

  function openModal(title, formHtml, onSave) {
    $('#modal-title').textContent = title;
    $('#modal-form').innerHTML = formHtml;
    modalSubmit = onSave;
    $('#modal').hidden = false;
    document.body.style.overflow = 'hidden';
    var first = $('#modal-form input, #modal-form select');
    if (first) setTimeout(function () { first.focus(); }, 50);
  }

  function closeModal() {
    $('#modal').hidden = true;
    document.body.style.overflow = '';
    modalSubmit = null;
  }

  function field(label, inner, hint) {
    return '<label class="f"><span class="f-label">' + label + '</span>' + inner +
           (hint ? '<span class="f-hint">' + hint + '</span>' : '') + '</label>';
  }

  function assetOptions(selected, filterFn) {
    var list = (state.data.assets || []).filter(filterFn || function () { return true; });
    if (!list.length) return '<option value="">（請先新增標的）</option>';
    return list.map(function (a) {
      return '<option value="' + esc(a.id) + '"' +
             (a.id === selected ? ' selected' : '') + '>' + esc(a.name) + '</option>';
    }).join('');
  }

  /* --- 標的 --- */

  function openAssetForm(id) {
    var a = id ? (state.data.assets || []).filter(function (x) { return x.id === id; })[0] : null;
    var kinds = window.Portfolio.KINDS.map(function (k) {
      return '<option value="' + k + '"' + (a && a.kind === k ? ' selected' : '') + '>' + k + '</option>';
    }).join('');
    var monitored = '<option value="">（不連結，例如基金）</option>' +
      state.monitored.map(function (m) {
        return '<option value="' + esc(m.id) + '"' +
               (a && a.linkId === m.id ? ' selected' : '') + '>' + esc(m.name) + '</option>';
      }).join('');

    var h = '';
    h += field('名稱', '<input name="name" required maxlength="40" value="' +
               esc(a ? a.name : '') + '" placeholder="例：台積電 2330">');
    h += field('分類', '<select name="kind">' + kinds + '</select>',
               '決定資產配置圓餅圖怎麼分');
    h += field('連結監控標的',
               '<select name="linkId">' + monitored + '</select>',
               '連結後就會自動帶市價，加碼參考欄也才算得出來');
    h += '<div class="f cash-only"' + (a && a.kind === '現金' ? '' : ' hidden') + '>' +
           '<span class="f-label">現金餘額</span>' +
           '<input name="cashAmount" type="number" step="any" inputmode="decimal" value="' +
           (a && a.cashAmount !== null ? a.cashAmount : '') + '">' +
           '<span class="f-hint">分類選「現金」時才會用到</span></div>';
    h += field('備註', '<input name="note" maxlength="60" value="' +
               esc(a ? a.note : '') + '">');
    if (a) {
      h += '<button type="button" class="btn danger full" id="btn-del-asset">' +
           '刪除這個標的（連同它的交易紀錄）</button>';
    }

    openModal(a ? '編輯標的' : '新增標的', h, function (fd) {
      var kind = fd.get('kind');
      var obj = {
        id: a ? a.id : window.Portfolio.uid('a'),
        name: String(fd.get('name') || '').trim(),
        kind: kind,
        linkId: fd.get('linkId') || null,
        unit: a ? a.unit : '',
        note: String(fd.get('note') || '').trim(),
        cashAmount: kind === '現金' ? (Number(fd.get('cashAmount')) || 0) : null
      };
      if (!obj.name) throw new Error('請填名稱');
      if (a) {
        state.data.assets = state.data.assets.map(function (x) {
          return x.id === a.id ? obj : x;
        });
      } else {
        state.data.assets.push(obj);
      }
    });

    // 分類切換到現金時才顯示餘額欄
    var kindSel = $('#modal-form select[name=kind]');
    var cashBox = $('#modal-form .cash-only');
    if (kindSel && cashBox) {
      kindSel.addEventListener('change', function () {
        cashBox.hidden = kindSel.value !== '現金';
      });
    }
    var del = $('#btn-del-asset');
    if (del) del.addEventListener('click', function () { delAsset(a.id); });
  }

  function delAsset(id) {
    var name = assetName(id);
    var n = (state.data.trades || []).filter(function (t) { return t.assetId === id; }).length;
    if (!confirm('確定要刪除「' + name + '」嗎？\n它的 ' + n + ' 筆交易紀錄也會一起刪除，無法復原。')) return;
    state.data.assets = state.data.assets.filter(function (a) { return a.id !== id; });
    state.data.trades = state.data.trades.filter(function (t) { return t.assetId !== id; });
    state.data.navs = state.data.navs.filter(function (t) { return t.assetId !== id; });
    closeModal();
    persist().then(recompute);
  }

  /* --- 交易 --- */

  function openTradeForm(id) {
    var t = id ? (state.data.trades || []).filter(function (x) { return x.id === id; })[0] : null;
    if (!(state.data.assets || []).filter(function (a) { return a.kind !== '現金'; }).length) {
      alert('請先新增一個標的（現金以外），才能記錄交易。');
      return openAssetForm(null);
    }
    var acts = Object.keys(window.Portfolio.ACTIONS).map(function (k) {
      return '<option value="' + k + '"' + (t && t.action === k ? ' selected' : '') + '>' +
             window.Portfolio.ACTIONS[k] + '</option>';
    }).join('');

    var h = '';
    h += field('日期', '<input name="date" type="date" required value="' +
               esc(t ? t.date : today()) + '">');
    h += field('標的', '<select name="assetId" required>' +
               assetOptions(t ? t.assetId : null,
                            function (a) { return a.kind !== '現金'; }) + '</select>');
    h += field('動作', '<select name="action">' + acts + '</select>');
    h += field('單價', '<input name="price" type="number" step="any" inputmode="decimal" value="' +
               (t && t.price !== null ? t.price : '') + '" placeholder="每股／每公克／每單位">');
    h += '<div class="f-two">' +
         field('數量', '<input name="qty" type="number" step="any" inputmode="decimal" value="' +
               (t && t.qty !== null ? t.qty : '') + '">') +
         field('金額', '<input name="amount" type="number" step="any" inputmode="decimal" value="' +
               (t && t.amount !== null ? t.amount : '') + '">') +
         '</div>';
    h += '<div class="f-hint" style="margin:-4px 0 10px">' +
         '「數量」和「金額」<b>填一個就好</b>，另一個會自動算出來（定期定額通常填金額）。</div>';
    h += field('手續費', '<input name="fee" type="number" step="any" inputmode="decimal" value="' +
               (t && t.fee ? t.fee : '') + '">');
    h += field('備註', '<input name="note" maxlength="60" value="' + esc(t ? t.note : '') + '">');

    openModal(t ? '編輯紀錄' : '新增一筆紀錄', h, function (fd) {
      var obj = {
        id: t ? t.id : window.Portfolio.uid('t'),
        date: fd.get('date'),
        assetId: fd.get('assetId'),
        action: fd.get('action'),
        price: toNum(fd.get('price')),
        qty: toNum(fd.get('qty')),
        amount: toNum(fd.get('amount')),
        fee: toNum(fd.get('fee')) || 0,
        note: String(fd.get('note') || '').trim()
      };
      if (!obj.date) throw new Error('請填日期');
      if (!obj.assetId) throw new Error('請選標的');
      var r = window.Portfolio.resolveTrade(obj);
      if (r.qty === null && r.amount === null) {
        throw new Error('「數量」和「金額」至少要填一個');
      }
      if (r.qty === null) throw new Error('只填金額的話，也要填「單價」才算得出數量');
      if (t) {
        state.data.trades = state.data.trades.map(function (x) {
          return x.id === t.id ? obj : x;
        });
      } else {
        state.data.trades.push(obj);
      }
    });
  }

  function toNum(v) {
    if (v === null || v === undefined || String(v).trim() === '') return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function delTrade(id) {
    if (!confirm('確定要刪除這一筆紀錄嗎？無法復原。')) return;
    state.data.trades = state.data.trades.filter(function (t) { return t.id !== id; });
    persist().then(recompute);
  }

  /* --- 手動淨值 --- */

  function openNavForm() {
    var custom = (state.data.assets || []).filter(function (a) {
      return a.kind !== '現金' && !a.linkId;
    });
    if (!custom.length) {
      alert('手動淨值是給「沒有連結監控標的」的項目用的（例如基金）。\n' +
            '目前沒有這樣的標的——你可以新增一個，或把某個標的的「連結監控標的」設成不連結。');
      return;
    }
    var h = '';
    h += field('日期', '<input name="date" type="date" required value="' + today() + '">');
    h += field('標的', '<select name="assetId" required>' +
               custom.map(function (a) {
                 return '<option value="' + esc(a.id) + '">' + esc(a.name) + '</option>';
               }).join('') + '</select>');
    h += field('淨值', '<input name="nav" type="number" step="any" inputmode="decimal" required>',
               '每單位的淨值，用來估算現值');

    openModal('輸入淨值', h, function (fd) {
      var obj = {
        id: window.Portfolio.uid('n'),
        assetId: fd.get('assetId'),
        date: fd.get('date'),
        nav: toNum(fd.get('nav'))
      };
      if (obj.nav === null) throw new Error('請填淨值');
      // 同一天同一標的只留一筆，重複輸入就是更新
      state.data.navs = state.data.navs.filter(function (n) {
        return !(n.assetId === obj.assetId && n.date === obj.date);
      });
      state.data.navs.push(obj);
    });
  }

  function delNav(id) {
    if (!confirm('確定要刪除這一筆淨值嗎？')) return;
    state.data.navs = state.data.navs.filter(function (n) { return n.id !== id; });
    persist().then(recompute);
  }

  /* ---------------------------------------------------------- 計算與繪製 */

  function recompute() {
    state.rows = window.Portfolio.buildHoldings(state.data, state.market, state.indicators);
    renderSyncChip();
    renderOverview();
    renderHoldings();
    renderTrades();
  }

  /* 只載入「我有持有、而且有連結監控標的」的歷史，不用把 12 個都抓下來 */
  function loadIndicators() {
    var ids = {};
    (state.data.assets || []).forEach(function (a) {
      if (a.linkId) ids[a.linkId] = true;
    });
    var list = Object.keys(ids);
    if (!list.length) return Promise.resolve();
    return Promise.all(list.map(function (id) {
      return fetchJSON('data/history/' + id + '.json')
        .then(function (h) {
          var m = state.market[id];
          state.indicators[id] = window.Indicators.computeAll(
            (h && h.points) || [], m && m.price);
        })
        .catch(function () { /* 這一項沒有歷史就不顯示燈號，不影響其他 */ });
    }));
  }

  /* ---------------------------------------------------------- 啟動 */

  function boot() {
    $('#btn-add-trade').addEventListener('click', function () { openTradeForm(null); });
    $('#btn-add-asset').addEventListener('click', function () { openAssetForm(null); });
    $('#btn-add-nav').addEventListener('click', openNavForm);
    $('#modal-close').addEventListener('click', closeModal);
    $('#modal-cancel').addEventListener('click', closeModal);
    $('#modal').addEventListener('click', function (e) {
      if (e.target === $('#modal')) closeModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !$('#modal').hidden) closeModal();
    });
    $('#modal-save').addEventListener('click', submitModal);
    $('#modal-form').addEventListener('submit', function (e) {
      e.preventDefault();
      submitModal();
    });

    Promise.all([
      fetchJSON('data/latest.json').catch(function () { return null; }),
      fetchJSON('data/assets.json').catch(function () { return null; }),
      window.Storage.load()
    ]).then(function (res) {
      var latest = res[0], cfg = res[1], stored = res[2];
      state.market = (latest && latest.assets) || {};
      state.monitored = ((cfg && cfg.assets) || [])
        .filter(function (a) { return a.type !== 'bot_gold_bar'; })
        .map(function (a) { return { id: a.id, name: a.name }; });
      state.data = window.Portfolio.normalize(stored.data);
      if (stored.warning) flash(stored.warning, 'bad');

      return loadIndicators();
    }).then(function () {
      recompute();               // 先算完再切換畫面，出錯才看得到訊息
      $('#loading').hidden = true;
      $('#content').hidden = false;
    }).catch(function (e) {
      // 錯誤一定要顯示在看得到的地方——不能寫進已經被隱藏的元素裡
      var box = $('#loading');
      box.hidden = false;
      $('#content').hidden = true;
      box.innerHTML = '<div class="fatal">載入失敗：' + esc(e.message) +
        '<br><br>如果你剛更新過網站，請重新整理一次（手機：下拉重新整理）。</div>';
      if (window.console) console.error(e);
    });
  }

  function submitModal() {
    if (!modalSubmit) return;
    var form = $('#modal-form');
    if (!form.reportValidity()) return;
    try {
      modalSubmit(new FormData(form));
    } catch (e) {
      alert(e.message);
      return;
    }
    closeModal();
    persist().then(recompute);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
