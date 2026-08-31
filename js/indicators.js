/*
 * indicators.js — 指標計算（純函式，不碰畫面）
 * 公式全部依 PLAN.md 第 7.9 節，與舊黃金 App 相同邏輯。
 *
 * 誠實原則：資料不足時一律回 null，讓畫面顯示「—」，
 * 絕不用比較少的資料硬算出一個看起來很像樣的數字。
 */
(function (global) {
  'use strict';

  var TRADING_DAYS_YEAR = 252;   // 一年約幾個交易日

  /* 從歷史點陣列取出收盤價序列（由舊到新） */
  function closes(points) {
    if (!Array.isArray(points)) return [];
    var out = [];
    for (var i = 0; i < points.length; i++) {
      var c = points[i] && points[i].c;
      if (typeof c === 'number' && isFinite(c)) out.push(c);
    }
    return out;
  }

  /* 簡單移動平均：最近 n 個收盤的平均。不足 n 點回 null */
  function ma(series, n) {
    if (!series || series.length < n || n <= 0) return null;
    var sum = 0;
    for (var i = series.length - n; i < series.length; i++) sum += series[i];
    return sum / n;
  }

  /* 整條 MA 序列（畫圖用）。前 n-1 個位置放 null，Chart.js 會自動斷線 */
  function maSeries(series, n) {
    var out = [];
    var sum = 0;
    for (var i = 0; i < series.length; i++) {
      sum += series[i];
      if (i >= n) sum -= series[i - n];
      out.push(i >= n - 1 ? sum / n : null);
    }
    return out;
  }

  /*
   * RSI(14)：近 14 日漲幅均值 G、跌幅均值 L，RSI = 100 - 100/(1+G/L)。
   * L = 0 時 RSI = 100。資料不足 15 點回 null（畫面顯示「—」）。
   */
  function rsi(series, period) {
    period = period || 14;
    if (!series || series.length < period + 1) return null;
    var gain = 0, loss = 0;
    for (var i = series.length - period; i < series.length; i++) {
      var diff = series[i] - series[i - 1];
      if (diff >= 0) gain += diff; else loss -= diff;
    }
    var g = gain / period, l = loss / period;
    if (l === 0) return 100;
    return 100 - 100 / (1 + g / l);
  }

  /*
   * 52 週（約 252 個交易日）區間位置。
   * 資料不足一年時，用手上全部的資料算，並回報實際用了幾個交易日，
   * 讓畫面能誠實寫出「近 N 個月區間」而不是謊稱 52 週。
   */
  function rangePosition(series, price) {
    if (!series || !series.length || typeof price !== 'number') return null;
    var window = series.slice(-TRADING_DAYS_YEAR);
    var hi = window[0], lo = window[0];
    for (var i = 1; i < window.length; i++) {
      if (window[i] > hi) hi = window[i];
      if (window[i] < lo) lo = window[i];
    }
    if (price > hi) hi = price;
    if (price < lo) lo = price;
    var pct = hi === lo ? 50 : ((price - lo) / (hi - lo)) * 100;
    return {
      high: hi,
      low: lo,
      percentile: pct,
      days: window.length,
      full: window.length >= TRADING_DAYS_YEAR * 0.9   // 是否真的涵蓋約一年
    };
  }

  /* 乖離率：(現價 - MA20) / MA20 × 100 */
  function bias(price, maValue) {
    if (typeof price !== 'number' || !maValue) return null;
    return ((price - maValue) / maValue) * 100;
  }

  /* 近 n 個交易日漲跌%：(現價 - n 日前收盤) / n 日前收盤 × 100 */
  function changeOver(series, price, n) {
    if (!series || series.length < n + 1 || typeof price !== 'number') return null;
    var base = series[series.length - 1 - n];
    if (!base) return null;
    return ((price - base) / base) * 100;
  }

  /* 年化波動（預設 30 日）：日報酬標準差 × √252 × 100。僅供參考顯示 */
  function volatility(series, n) {
    n = n || 30;
    if (!series || series.length < n + 1) return null;
    var rets = [];
    for (var i = series.length - n; i < series.length; i++) {
      if (!series[i - 1]) continue;
      rets.push(series[i] / series[i - 1] - 1);
    }
    if (rets.length < 2) return null;
    var mean = rets.reduce(function (a, b) { return a + b; }, 0) / rets.length;
    var varSum = rets.reduce(function (a, b) {
      return a + (b - mean) * (b - mean);
    }, 0);
    var sd = Math.sqrt(varSum / (rets.length - 1));
    return sd * Math.sqrt(TRADING_DAYS_YEAR) * 100;
  }

  /*
   * 位置燈號（PLAN 3.2）：只做狀態分類，不給任何買賣建議。
   * <25% 相對低檔區 / 25-75% 中性 / >75% 相對高檔區
   */
  function positionSignal(rangeInfo) {
    if (!rangeInfo) {
      return { level: 'unknown', label: '資料不足', rule: '歷史資料還不夠算出區間位置' };
    }
    var p = rangeInfo.percentile;
    var windowText = rangeInfo.full
      ? '52 週'
      : ('近 ' + Math.max(1, Math.round(rangeInfo.days / 21)) + ' 個月');
    var rule = '位於' + windowText + '區間第 ' + p.toFixed(0) + ' 百分位';
    if (p < 25) return { level: 'low', label: '相對低檔區', rule: rule + ' → 低於 25%' };
    if (p > 75) return { level: 'high', label: '相對高檔區', rule: rule + ' → 高於 75%' };
    return { level: 'mid', label: '中性', rule: rule + ' → 落在 25%～75% 之間' };
  }

  /*
   * 一次算出一個標的的全部指標。
   * points：history/<id>.json 的 points；price：latest.json 的現價。
   */
  function computeAll(points, price) {
    var series = closes(points);
    if (typeof price !== 'number' || !isFinite(price)) {
      price = series.length ? series[series.length - 1] : null;
    }
    // 現價視為序列的最後一點（盤中時比歷史檔更新）
    var live = series.slice();
    if (typeof price === 'number' && live.length) live[live.length - 1] = price;

    var ma20 = ma(live, 20);
    var ma60 = ma(live, 60);
    var range = rangePosition(live, price);

    return {
      price: price,
      count: live.length,
      ma20: ma20,
      ma60: ma60,
      vsMa20: bias(price, ma20),
      vsMa60: bias(price, ma60),
      rsi14: rsi(live, 14),
      range: range,
      signal: positionSignal(range),
      change10d: changeOver(live, price, 10),
      volatility30: volatility(live, 30)
    };
  }

  global.Indicators = {
    closes: closes,
    ma: ma,
    maSeries: maSeries,
    rsi: rsi,
    rangePosition: rangePosition,
    bias: bias,
    changeOver: changeOver,
    volatility: volatility,
    positionSignal: positionSignal,
    computeAll: computeAll
  };
})(window);
