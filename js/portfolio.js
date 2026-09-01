/*
 * portfolio.js — 個人紀錄的資料模型與計算（純函式，不碰畫面）
 *
 * 這裡算的每一個數字都只在你的瀏覽器裡跑，不會送到任何地方。
 *
 * 資料長相（portfolio.json）：
 * {
 *   version: 1,
 *   updatedAt: "...",
 *   assets: [ {id, name, kind, linkId, unit, note, cashAmount} ],
 *   trades: [ {id, date, assetId, action, price, qty, amount, fee, note} ],
 *   navs:   [ {id, assetId, date, nav} ]        // 自訂標的的手動淨值
 * }
 *
 * kind（資產配置圓餅圖用的分類）：台股 / 美股ETF / 黃金 / 基金 / 現金 / 其他
 * linkId：對應 data/assets.json 裡的監控標的 id，有連結才抓得到市價與燈號
 */
(function (global) {
  'use strict';

  var KINDS = ['台股', '美股ETF', '債券', '黃金', '基金', '現金', '其他'];

  /*
   * open（期初持有）＝「我現在就有這些，不是今天買的」。
   * 計算上跟買入完全一樣，只是分開標示，你才分得出哪些是登錄既有部位、
   * 哪些是真的有一筆進場。想不起來以前每一筆怎麼買的時候就用這個。
   */
  var ACTIONS = {
    open: '期初持有',
    buy: '買入',
    sell: '賣出',
    dca: '定期定額'
  };

  function uid(prefix) {
    return prefix + '_' + Date.now().toString(36) +
           Math.random().toString(36).slice(2, 7);
  }

  function emptyData() {
    return {
      version: 1,
      updatedAt: null,
      assets: [],
      trades: [],
      navs: []
    };
  }

  /* 讀進來的資料補齊缺欄位，避免舊備份少東西就整頁壞掉 */
  function normalize(d) {
    var out = emptyData();
    if (!d || typeof d !== 'object') return out;
    out.version = d.version || 1;
    out.updatedAt = d.updatedAt || null;
    out.assets = (d.assets || []).map(function (a) {
      return {
        id: a.id || uid('a'),
        name: String(a.name || '未命名'),
        kind: KINDS.indexOf(a.kind) >= 0 ? a.kind : '其他',
        linkId: a.linkId || null,
        unit: a.unit || '',
        note: a.note || '',
        cashAmount: typeof a.cashAmount === 'number' ? a.cashAmount : null
      };
    });
    out.trades = (d.trades || []).map(function (t) {
      return {
        id: t.id || uid('t'),
        date: t.date || '',
        assetId: t.assetId || '',
        action: ACTIONS[t.action] ? t.action : 'buy',
        price: numOrNull(t.price),
        qty: numOrNull(t.qty),
        amount: numOrNull(t.amount),
        fee: numOrNull(t.fee) || 0,
        note: t.note || ''
      };
    });
    out.navs = (d.navs || []).map(function (n) {
      return {
        id: n.id || uid('n'),
        assetId: n.assetId || '',
        date: n.date || '',
        nav: numOrNull(n.nav)
      };
    });
    return out;
  }

  function numOrNull(v) {
    if (v === null || v === undefined || v === '') return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  /*
   * 一筆交易補齊「單價 / 股數 / 金額」三者。
   * 使用者只要填其中兩個，第三個自動算出來：
   *   有單價 + 股數 → 金額 = 單價 × 股數
   *   有單價 + 金額 → 股數 = 金額 ÷ 單價（定期定額常見）
   * 手續費不含在金額裡，另外加。
   */
  function resolveTrade(t) {
    var price = t.price, qty = t.qty, amount = t.amount;
    if (price && qty && !amount) amount = price * qty;
    else if (price && amount && !qty) qty = amount / price;
    else if (qty && amount && !price) price = amount / qty;
    return {
      price: price === null ? null : price,
      qty: qty === null ? null : qty,
      amount: amount === null ? null : amount
    };
  }

  /*
   * 算出每個標的目前的持有數量與平均成本（加權平均法）。
   *
   *   買入：總成本 += 金額 + 手續費，總股數 += 股數
   *   賣出：總成本 -= 平均成本 × 賣出股數，總股數 -= 股數
   *         （賣出的手續費算進已實現損益，不影響剩下部位的平均成本）
   *
   * 定期定額（dca）就是買入，只是通常用金額而不是股數來記。
   */
  function computeHolding(assetId, trades) {
    var rows = trades.filter(function (t) { return t.assetId === assetId; })
                     .slice()
                     .sort(function (a, b) { return String(a.date).localeCompare(String(b.date)); });

    var qty = 0, cost = 0, realized = 0, totalFee = 0;
    var buyCount = 0, firstDate = null, lastDate = null;

    rows.forEach(function (t) {
      var r = resolveTrade(t);
      var fee = t.fee || 0;
      totalFee += fee;
      if (!firstDate) firstDate = t.date;
      lastDate = t.date;

      if (t.action === 'sell') {
        if (!r.qty) return;
        var avg = qty > 0 ? cost / qty : 0;
        var sellQty = Math.min(r.qty, qty);
        var proceeds = (r.amount !== null ? r.amount : (r.price || 0) * sellQty) - fee;
        realized += proceeds - avg * sellQty;
        cost -= avg * sellQty;
        qty -= sellQty;
        if (qty < 1e-9) { qty = 0; cost = 0; }
      } else {
        if (!r.qty && !r.amount) return;
        var addQty = r.qty || 0;
        var addCost = (r.amount !== null ? r.amount : (r.price || 0) * addQty) + fee;
        qty += addQty;
        cost += addCost;
        buyCount++;
      }
    });

    return {
      qty: qty,
      cost: cost,
      avgCost: qty > 0 ? cost / qty : null,
      realized: realized,
      totalFee: totalFee,
      tradeCount: rows.length,
      buyCount: buyCount,
      firstDate: firstDate,
      lastDate: lastDate
    };
  }

  /* 這個標的最後一筆手動淨值 */
  function lastNav(assetId, navs) {
    var rows = (navs || []).filter(function (n) {
      return n.assetId === assetId && n.nav !== null;
    }).sort(function (a, b) { return String(a.date).localeCompare(String(b.date)); });
    return rows.length ? rows[rows.length - 1] : null;
  }

  /*
   * 把持倉、市價、指標燈號組成一張表。
   *
   * market：data/latest.json 的 assets（有 linkId 才對得上）
   * indicators：{ 監控標的 id: Indicators.computeAll(...) }，用來帶「加碼參考」
   *
   * 誠實原則：市價來源會標清楚是「自動抓的市價」還是「你自己輸入的淨值」，
   * 兩者都沒有就直接顯示「沒有市價」，不會拿成本價假裝成現值。
   */
  function buildHoldings(data, market, indicators) {
    market = market || {};
    indicators = indicators || {};

    return (data.assets || []).map(function (a) {
      var row = {
        asset: a,
        kind: a.kind,
        name: a.name,
        isCash: a.kind === '現金'
      };

      if (row.isCash) {
        row.qty = null;
        row.avgCost = null;
        row.cost = a.cashAmount || 0;
        row.value = a.cashAmount || 0;
        row.priceSource = 'cash';
        row.pnl = null;
        row.pnlPct = null;
        row.realized = 0;
        return row;
      }

      var h = computeHolding(a.id, data.trades || []);
      row.qty = h.qty;
      row.avgCost = h.avgCost;
      row.cost = h.cost;
      row.realized = h.realized;
      row.totalFee = h.totalFee;
      row.tradeCount = h.tradeCount;
      row.firstDate = h.firstDate;
      row.lastDate = h.lastDate;

      // 市價：優先用監控清單自動抓的，沒有就用最後一筆手動淨值
      var m = a.linkId ? market[a.linkId] : null;
      var nav = lastNav(a.id, data.navs);
      if (m && m.status === 'ok' && typeof m.price === 'number') {
        row.price = m.price;
        row.priceSource = 'market';
        row.priceDate = m.date;
        row.priceLabel = m.priceLabel;
        row.decimals = m.decimals;
        row.currency = m.currency;
        row.ind = indicators[a.linkId] || null;
      } else if (nav) {
        row.price = nav.nav;
        row.priceSource = 'manual';
        row.priceDate = nav.date;
        row.decimals = 4;
      } else {
        row.price = null;
        row.priceSource = 'none';
        if (m && m.status && m.status !== 'ok') row.priceSource = 'error';
      }

      if (row.price !== null && h.qty > 0) {
        row.value = row.price * h.qty;
        row.pnl = row.value - h.cost;
        row.pnlPct = h.cost > 0 ? (row.pnl / h.cost) * 100 : null;
      } else {
        row.value = null;
        row.pnl = null;
        row.pnlPct = null;
      }
      return row;
    });
  }

  /*
   * 目前要顯示在持倉表的：現金、還有部位的、以及「剛建立還沒有交易紀錄」的。
   * 最後那種一定要留著——不然剛新增完標的它就從畫面上消失，會以為沒建立成功。
   * 真正買過又賣光的才歸到「已清倉」那一區。
   */
  function activeHoldings(rows) {
    return rows.filter(function (r) {
      return r.isCash || (r.qty || 0) > 0 || !r.tradeCount;
    });
  }
  function closedHoldings(rows) {
    return rows.filter(function (r) { return !r.isCash && !(r.qty > 0) && r.tradeCount > 0; });
  }

  /* 資產配置：依 kind 加總現值。沒有市價的標的無法計入，會回報數量 */
  function allocation(rows) {
    var byKind = {};
    var unknown = [];
    rows.forEach(function (r) {
      if (r.value === null || !isFinite(r.value)) {
        if (r.isCash || (r.qty || 0) > 0) unknown.push(r.name);
        return;
      }
      byKind[r.kind] = (byKind[r.kind] || 0) + r.value;
    });
    var items = KINDS.filter(function (k) { return byKind[k]; })
      .map(function (k) { return { kind: k, value: byKind[k] }; });
    var total = items.reduce(function (s, x) { return s + x.value; }, 0);
    items.forEach(function (x) { x.pct = total > 0 ? (x.value / total) * 100 : 0; });
    items.sort(function (a, b) { return b.value - a.value; });
    return { items: items, total: total, unknown: unknown };
  }

  /* 全部加總（只算得出現值的部分） */
  function totals(rows) {
    var value = 0, cost = 0, realized = 0, counted = 0, skipped = 0;
    rows.forEach(function (r) {
      realized += r.realized || 0;
      if (r.value === null || !isFinite(r.value)) {
        if (!r.isCash && (r.qty || 0) > 0) skipped++;
        return;
      }
      value += r.value;
      cost += r.isCash ? r.value : (r.cost || 0);
      counted++;
    });
    return {
      value: value,
      cost: cost,
      pnl: value - cost,
      pnlPct: cost > 0 ? ((value - cost) / cost) * 100 : null,
      realized: realized,
      counted: counted,
      skipped: skipped
    };
  }

  global.Portfolio = {
    KINDS: KINDS,
    ACTIONS: ACTIONS,
    uid: uid,
    emptyData: emptyData,
    normalize: normalize,
    resolveTrade: resolveTrade,
    computeHolding: computeHolding,
    lastNav: lastNav,
    buildHoldings: buildHoldings,
    activeHoldings: activeHoldings,
    closedHoldings: closedHoldings,
    allocation: allocation,
    totals: totals
  };
})(window);
