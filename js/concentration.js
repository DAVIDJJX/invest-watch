/*
 * concentration.js — 個人集中度（分析系列 A1-2）：只在瀏覽器裡算，結果只顯示、不上傳、不寫進任何公開檔。
 *
 * 輸入是使用者放在【私人】倉庫 invest-data 的 analysis-profile.json：
 *   { version:1, asOf:"YYYY-MM", weights:{stock,index_etf,bond_etf,commodity,crypto,fx,gold_tw,other}（百分比）,
 *     salaryProxyAssetId:"tw2330" }
 * 裡面只有各類別的權重百分比與一個代理標的，沒有金額、沒有股數。
 *
 * 這一支是整個公開倉庫裡【唯一】可以出現設定檔鍵名（weights／salaryProxyAssetId／asOf）的地方：
 * 讀設定、驗證、算三個事實、表單值來回轉換全部在這裡；檢視頁只拿算好的結果去畫，
 * 表單欄位的 name／id 也不用這些鍵名。守門測試（scripts/test_analysis_guards.py）盯著這條線。
 *
 * 三個事實（都是算術，不含任何判斷）：
 *   1. 最大單一類別權重（other 算進去）
 *   2. 前二類合計（other 算進去）
 *   3. 與收入來源代理標的「同向」的權重合計：各類別用一個公開標的當代理，拿 risk.json 的 3 年週報酬相關係數，
 *      係數 > 0.5 視為同向；顯示的是係數本身。other 沒有代理、相關性未知，不納入這一項並照實註明。
 *
 * 絕不 console.log 任何權重值。
 */
(function (global) {
  'use strict';

  var CLASSES = ['stock', 'index_etf', 'bond_etf', 'commodity', 'crypto', 'fx', 'gold_tw', 'other'];
  var LABELS = { stock: '個股', index_etf: '廣泛指數 ETF', bond_etf: '債券 ETF', commodity: '商品', crypto: '加密貨幣',
                 fx: '外幣', gold_tw: '台銀黃金', other: '其他（未分類）' };
  // 各類別的代理標的（David 的裁決）；other 沒有代理
  var PROXY = { stock: 'tw2330', index_etf: 'tw00646', bond_etf: 'tw00679b', commodity: 'gold_intl', crypto: 'btc',
                fx: 'fx_usd', gold_tw: 'gold_intl' };
  var PROXY_CHOICES = [
    { id: 'tw2330', label: '台積電 2330（半導體／台股）' }, { id: 'nvda', label: 'NVIDIA（半導體／美股）' },
    { id: 'twii', label: '台股加權指數' }, { id: 'gspc', label: 'S&P 500' }, { id: 'tw00646', label: '元大 S&P500 00646' },
    { id: 'tw00679b', label: '元大美債 20 年 00679B' }, { id: 'gold_intl', label: '國際金價' }, { id: 'btc', label: '比特幣' },
    { id: 'wti', label: 'WTI 原油' }, { id: 'fx_usd', label: '美元／台幣' }
  ];
  var THRESHOLD = 0.5;
  var SUM_LOW = 95, SUM_HIGH = 105;

  function num(x) {
    if (typeof x === 'number') return isFinite(x) ? x : NaN;
    if (typeof x === 'string' && x.trim() !== '') return Number(x);
    return NaN;
  }

  /* 驗證設定檔：負數／非數字 → error（不能存、不能算）；總和不在 95～105 → 只警告。 */
  function validate(profile) {
    var errors = [], warnings = [], w = {};
    var p = profile || {};
    var src = p.weights || {};
    CLASSES.forEach(function (c) {
      var raw = src[c];
      if (raw === undefined || raw === null || raw === '') { w[c] = 0; return; }
      var v = num(raw);
      if (isNaN(v)) { errors.push(LABELS[c] + '：不是數字'); w[c] = 0; return; }
      if (v < 0) { errors.push(LABELS[c] + '：不能是負數'); w[c] = 0; return; }
      if (v > 100) { errors.push(LABELS[c] + '：超過 100'); w[c] = 0; return; }
      w[c] = v;
    });
    var sum = CLASSES.reduce(function (s, c) { return s + w[c]; }, 0);
    if (!errors.length && (sum < SUM_LOW || sum > SUM_HIGH)) {
      warnings.push('八個百分比加起來是 ' + Math.round(sum * 10) / 10 + '，不在 95～105 之間（照樣算，只是提醒）');
    }
    var proxy = p.salaryProxyAssetId;
    var known = PROXY_CHOICES.some(function (x) { return x.id === proxy; });
    if (proxy && !known) warnings.push('收入來源代理標的「' + String(proxy) + '」不在公開清單裡，同向那一項會是資料不足');
    if (!proxy) warnings.push('沒有填收入來源代理標的，同向那一項會是資料不足');
    var month = typeof p.asOf === 'string' && /^\d{4}-\d{2}$/.test(p.asOf) ? p.asOf : null;
    return { ok: errors.length === 0, errors: errors, warnings: warnings, weights: w, sum: sum,
             proxy: known ? proxy : null, month: month };
  }

  /* 從 risk.json 的相關矩陣取兩個標的的係數；同一個標的是 1。 */
  function corrOf(corr, a, b) {
    if (!a || !b) return null;
    if (a === b) return 1;
    var m = corr && corr.matrix;
    var cell = m && m[a] && m[a][b];
    return cell && typeof cell.r === 'number' ? cell.r : null;
  }

  /* 三個事實。corr 是 risk.json 的 correlation 區塊（可以是 null → 同向那一項資料不足）。 */
  function facts(profile, corr) {
    var v = validate(profile);
    if (!v.ok) return { ok: false, errors: v.errors, warnings: v.warnings };
    var w = v.weights;
    var ranked = CLASSES.map(function (c) { return { cls: c, label: LABELS[c], pct: w[c] }; })
      .sort(function (a, b) { return b.pct - a.pct; });
    var maxClass = ranked[0];
    var topTwo = ranked.slice(0, 2);
    var rows = [], alignedPct = 0, unknown = [];
    CLASSES.forEach(function (c) {
      if (c === 'other') return;
      var proxy = PROXY[c];
      var r = corrOf(corr, proxy, v.proxy);
      var aligned = typeof r === 'number' && r > THRESHOLD;
      if (typeof r !== 'number') unknown.push(c);
      if (aligned) alignedPct += w[c];
      rows.push({ cls: c, label: LABELS[c], pct: w[c], proxy: proxy, r: r, aligned: aligned });
    });
    var notes = [];
    if (w.other > 0) notes.push('other（' + w.other + '%）沒有代理標的、相關性未知，未納入同向計算');
    if (!v.proxy) notes.push('沒有可用的收入來源代理標的：同向合計＝資料不足');
    else if (unknown.length) notes.push('相關係數資料不足的類別：' + unknown.map(function (c) { return LABELS[c]; }).join('、'));
    return {
      ok: true, warnings: v.warnings, month: v.month, sum: v.sum,
      maxClass: maxClass,
      topTwo: { classes: topTwo, pct: topTwo[0].pct + topTwo[1].pct },
      aligned: { available: !!v.proxy, pct: v.proxy ? alignedPct : null, proxy: v.proxy, threshold: THRESHOLD, rows: rows,
                 otherExcluded: w.other > 0 },
      notes: notes
    };
  }

  /* 表單 ↔ 設定檔。表單只給欄位值（鍵是類別名與 'proxy'／'month'），設定檔的鍵名只在這裡組出來。 */
  function fromForm(values) {
    var weights = {};
    CLASSES.forEach(function (c) { weights[c] = num(values[c]); if (isNaN(weights[c])) weights[c] = values[c]; });
    return { version: 1, asOf: values.month || '', weights: weights, salaryProxyAssetId: values.proxy || '' };
  }

  function toFormValues(profile) {
    var p = profile || {}, src = p.weights || {}, out = {};
    CLASSES.forEach(function (c) { out[c] = (src[c] === undefined || src[c] === null) ? '' : src[c]; });
    out.proxy = p.salaryProxyAssetId || '';
    out.month = p.asOf || '';
    return out;
  }

  function emptyTemplate() {
    var weights = {};
    CLASSES.forEach(function (c) { weights[c] = 0; });
    return { version: 1, asOf: '', weights: weights, salaryProxyAssetId: 'tw2330' };
  }

  global.Concentration = {
    CLASSES: CLASSES, LABELS: LABELS, PROXY: PROXY, PROXY_CHOICES: PROXY_CHOICES, THRESHOLD: THRESHOLD,
    FILE: 'analysis-profile.json',
    validate: validate, facts: facts, fromForm: fromForm, toFormValues: toFormValues, emptyTemplate: emptyTemplate
  };
})(window);
