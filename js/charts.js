/*
 * charts.js — 畫圖（迷你走勢線用手寫 SVG，展開後的大圖用本地 Chart.js）
 * Chart.js 存在 lib/chart.umd.min.js，不依賴任何 CDN。
 *
 * U1（A1-4）：顏色一律從 css/style.css 的變數讀（畫圖的當下用 getComputedStyle），這支檔裡不寫任何色碼。
 * 主題切換（head 那一小段腳本發出 iw:theme 事件）時，已經畫好的大圖用同一份資料重畫一次。
 */
(function (global) {
  'use strict';

  /* 每一種用途對應哪一個 CSS 變數（深色的值跟以前寫死在這裡的一模一樣，寫在 css/style.css 的 html[data-theme="dark"]） */
  var COLOR_VARS = {
    line: '--accent', ma20: '--gold', ma60: '--chart-ma60', grid: '--chart-grid', text: '--text-dim',
    up: '--up', down: '--down', fill: '--chart-fill',
    tipBg: '--bg', tipLine: '--line', tipText: '--text', pieBorder: '--card', pieNone: '--pie-none'
  };
  var PIE_VARS = {
    '台股': '--pie-tw', '美股ETF': '--pie-us', '債券': '--pie-bond', '黃金': '--pie-gold',
    '基金': '--pie-fund', '現金': '--pie-cash', '其他': '--pie-other'
  };
  var COLORS = {};
  var PIE_COLORS = {};

  function readVar(name) {
    try {
      return global.getComputedStyle(global.document.documentElement).getPropertyValue(name).trim();
    } catch (e) {
      return '';
    }
  }
  /* 把 COLORS／PIE_COLORS 換成現在這個主題的值（物件本身不換，別的檔拿著的參照照樣有效） */
  function refreshColors() {
    Object.keys(COLOR_VARS).forEach(function (k) { COLORS[k] = readVar(COLOR_VARS[k]); });
    Object.keys(PIE_VARS).forEach(function (k) { PIE_COLORS[k] = readVar(PIE_VARS[k]); });
    return COLORS;
  }
  refreshColors();

  /* ---------------------------------------------------------- 迷你走勢線
   * 用純 SVG 畫，不開 Chart.js 實例，卡片再多也不卡。
   * 上漲用紅、下跌用綠（台股習慣）。顏色寫在 style 裡（可以是 var(--up) 這種寫法）。
   */
  function sparkline(values, opts) {
    opts = opts || {};
    var w = 300, h = 34, pad = 3;
    if (!values || values.length < 2) return '';

    var min = Math.min.apply(null, values);
    var max = Math.max.apply(null, values);
    var span = max - min || 1;
    var step = (w - pad * 2) / (values.length - 1);

    var pts = values.map(function (v, i) {
      var x = pad + i * step;
      var y = pad + (h - pad * 2) * (1 - (v - min) / span);
      return x.toFixed(1) + ',' + y.toFixed(1);
    });

    var rising = values[values.length - 1] >= values[0];
    var color = opts.color || (rising ? 'var(--up)' : 'var(--down)');
    var id = 'sg' + Math.random().toString(36).slice(2, 8);

    return '' +
      '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" ' +
      'role="img" aria-label="近期走勢縮圖">' +
        '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1">' +
          '<stop offset="0%" style="stop-color:' + color + '" stop-opacity=".28"/>' +
          '<stop offset="100%" style="stop-color:' + color + '" stop-opacity="0"/>' +
        '</linearGradient></defs>' +
        '<polygon fill="url(#' + id + ')" points="' +
          pad + ',' + (h - pad) + ' ' + pts.join(' ') + ' ' +
          (w - pad) + ',' + (h - pad) + '"/>' +
        '<polyline fill="none" style="stroke:' + color + '" stroke-width="1.6" ' +
          'stroke-linejoin="round" stroke-linecap="round" points="' + pts.join(' ') + '"/>' +
      '</svg>';
  }

  /* ---------------------------------------------------------- 大圖 */

  var instances = {};   // id -> Chart 實例（重畫前要先銷毀）
  var drawn = {};       // id -> 怎麼畫的（主題切換時用同一份資料重畫）

  function destroy(canvasId) {
    if (instances[canvasId]) {
      instances[canvasId].destroy();
      delete instances[canvasId];
    }
    delete drawn[canvasId];
  }

  function fmtDate(d) {
    // 2026-08-31 -> 8/31
    var p = String(d).split('-');
    return p.length === 3 ? (Number(p[1]) + '/' + Number(p[2])) : d;
  }

  function tooltipColors() {
    return { backgroundColor: COLORS.tipBg, borderColor: COLORS.tipLine, borderWidth: 1, titleColor: COLORS.tipText, bodyColor: COLORS.tipText, padding: 10 };
  }

  /*
   * 近一年互動走勢圖，疊 MA20 / MA60。
   * points：[{d, c}]；decimals：小數位數
   */
  function drawHistory(canvas, points, opts) {
    opts = opts || {};
    if (!global.Chart) return false;
    refreshColors();

    var labels = points.map(function (p) { return p.d; });
    var values = points.map(function (p) { return p.c; });
    var ma20 = global.Indicators.maSeries(values, 20);
    var ma60 = global.Indicators.maSeries(values, 60);

    destroy(canvas.id);

    var dec = typeof opts.decimals === 'number' ? opts.decimals : 2;
    var nf = new Intl.NumberFormat('zh-TW', {
      minimumFractionDigits: dec, maximumFractionDigits: dec
    });
    var tip = tooltipColors();
    tip.displayColors = true;
    tip.callbacks = {
      label: function (ctx) {
        if (ctx.parsed.y === null) return null;
        return ctx.dataset.label + '  ' + nf.format(ctx.parsed.y);
      }
    };

    instances[canvas.id] = new global.Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        labels: labels,
        datasets: [
          {
            label: opts.label || '收盤',
            data: values,
            borderColor: COLORS.line,
            backgroundColor: COLORS.fill,
            borderWidth: 1.8,
            pointRadius: 0,
            pointHitRadius: 12,
            fill: true,
            tension: 0.15,
            order: 3
          },
          {
            label: 'MA20',
            data: ma20,
            borderColor: COLORS.ma20,
            borderWidth: 1.3,
            pointRadius: 0,
            fill: false,
            spanGaps: false,
            order: 2
          },
          {
            label: 'MA60',
            data: ma60,
            borderColor: COLORS.ma60,
            borderWidth: 1.3,
            pointRadius: 0,
            fill: false,
            spanGaps: false,
            order: 1
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 260 },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: tip
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: COLORS.grid },
            ticks: {
              color: COLORS.text,
              font: { size: 10 },
              maxRotation: 0,
              autoSkip: true,
              maxTicksLimit: 7,
              callback: function (v, i) { return fmtDate(labels[i]); }
            }
          },
          y: {
            position: 'right',
            grid: { color: COLORS.grid },
            border: { display: false },
            ticks: {
              color: COLORS.text,
              font: { size: 10 },
              maxTicksLimit: 6,
              callback: function (v) { return nf.format(v); }
            }
          }
        }
      }
    });
    drawn[canvas.id] = { fn: drawHistory, canvas: canvas, data: points, opts: opts };
    return true;
  }

  /* ---------------------------------------------------------- 資產配置圓餅圖
   *
   * 只在瀏覽器裡畫，資料完全不離開這台裝置。
   * items: [{kind, value, pct}]
   */
  function drawPie(canvas, items, opts) {
    opts = opts || {};
    if (!global.Chart || !canvas) return false;
    refreshColors();
    destroy(canvas.id);
    if (!items || !items.length) return false;

    var nf = new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 0 });
    var tip = tooltipColors();
    tip.callbacks = {
      label: function (ctx) {
        var v = ctx.parsed;
        var total = ctx.dataset.data.reduce(function (a, b) { return a + b; }, 0);
        var pct = total > 0 ? (v / total * 100).toFixed(1) : '0.0';
        return ' ' + ctx.label + '　' +
               (opts.hideAmount ? pct + '%' : nf.format(v) + '　' + pct + '%');
      }
    };

    instances[canvas.id] = new global.Chart(canvas.getContext('2d'), {
      type: 'doughnut',
      data: {
        labels: items.map(function (x) { return x.kind; }),
        datasets: [{
          data: items.map(function (x) { return x.value; }),
          backgroundColor: items.map(function (x) {
            return PIE_COLORS[x.kind] || COLORS.pieNone;
          }),
          borderColor: COLORS.pieBorder,
          borderWidth: 2,
          hoverOffset: 6
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '58%',
        animation: { duration: 300 },
        plugins: {
          legend: { display: false },
          tooltip: tip
        }
      }
    });
    drawn[canvas.id] = { fn: drawPie, canvas: canvas, data: items, opts: opts };
    return true;
  }

  /* 主題換了：顏色重讀一次，畫好的大圖用同一份資料重畫 */
  function redrawAll() {
    refreshColors();
    Object.keys(drawn).forEach(function (id) {
      var d = drawn[id];
      d.fn(d.canvas, d.data, d.opts);
    });
  }
  try { global.document.addEventListener('iw:theme', redrawAll); } catch (e) { /* 沒有 document 就不聽 */ }

  global.Charts = {
    sparkline: sparkline,
    drawHistory: drawHistory,
    drawPie: drawPie,
    destroy: destroy,
    refreshColors: refreshColors,
    redrawAll: redrawAll,
    COLORS: COLORS,
    PIE_COLORS: PIE_COLORS,
    COLOR_VARS: COLOR_VARS,
    PIE_VARS: PIE_VARS
  };
})(window);
