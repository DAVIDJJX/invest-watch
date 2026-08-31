/*
 * charts.js — 畫圖（迷你走勢線用手寫 SVG，展開後的大圖用本地 Chart.js）
 * Chart.js 存在 lib/chart.umd.min.js，不依賴任何 CDN。
 */
(function (global) {
  'use strict';

  var COLORS = {
    line:  '#5b9cf8',
    ma20:  '#e5b567',
    ma60:  '#a78bfa',
    grid:  'rgba(38, 51, 73, .75)',
    text:  '#8fa2bd',
    up:    '#ff6b6b',
    down:  '#4ade80'
  };

  /* ---------------------------------------------------------- 迷你走勢線
   * 用純 SVG 畫，不開 Chart.js 實例，卡片再多也不卡。
   * 上漲用紅、下跌用綠（台股習慣）。
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
    var color = opts.color || (rising ? COLORS.up : COLORS.down);
    var id = 'sg' + Math.random().toString(36).slice(2, 8);

    return '' +
      '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" ' +
      'role="img" aria-label="近期走勢縮圖">' +
        '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1">' +
          '<stop offset="0%" stop-color="' + color + '" stop-opacity=".28"/>' +
          '<stop offset="100%" stop-color="' + color + '" stop-opacity="0"/>' +
        '</linearGradient></defs>' +
        '<polygon fill="url(#' + id + ')" points="' +
          pad + ',' + (h - pad) + ' ' + pts.join(' ') + ' ' +
          (w - pad) + ',' + (h - pad) + '"/>' +
        '<polyline fill="none" stroke="' + color + '" stroke-width="1.6" ' +
          'stroke-linejoin="round" stroke-linecap="round" points="' + pts.join(' ') + '"/>' +
      '</svg>';
  }

  /* ---------------------------------------------------------- 大圖 */

  var instances = {};   // id -> Chart 實例（重畫前要先銷毀）

  function destroy(canvasId) {
    if (instances[canvasId]) {
      instances[canvasId].destroy();
      delete instances[canvasId];
    }
  }

  function fmtDate(d) {
    // 2026-08-31 -> 8/31
    var p = String(d).split('-');
    return p.length === 3 ? (Number(p[1]) + '/' + Number(p[2])) : d;
  }

  /*
   * 近一年互動走勢圖，疊 MA20 / MA60。
   * points：[{d, c}]；decimals：小數位數
   */
  function drawHistory(canvas, points, opts) {
    opts = opts || {};
    if (!global.Chart) return false;

    var labels = points.map(function (p) { return p.d; });
    var values = points.map(function (p) { return p.c; });
    var ma20 = global.Indicators.maSeries(values, 20);
    var ma60 = global.Indicators.maSeries(values, 60);

    destroy(canvas.id);

    var dec = typeof opts.decimals === 'number' ? opts.decimals : 2;
    var nf = new Intl.NumberFormat('zh-TW', {
      minimumFractionDigits: dec, maximumFractionDigits: dec
    });

    instances[canvas.id] = new global.Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        labels: labels,
        datasets: [
          {
            label: opts.label || '收盤',
            data: values,
            borderColor: COLORS.line,
            backgroundColor: 'rgba(91, 156, 248, .12)',
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
          tooltip: {
            backgroundColor: '#0e1420',
            borderColor: '#263349',
            borderWidth: 1,
            titleColor: '#e6edf7',
            bodyColor: '#e6edf7',
            padding: 10,
            displayColors: true,
            callbacks: {
              label: function (ctx) {
                if (ctx.parsed.y === null) return null;
                return ctx.dataset.label + '  ' + nf.format(ctx.parsed.y);
              }
            }
          }
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
    return true;
  }

  global.Charts = {
    sparkline: sparkline,
    drawHistory: drawHistory,
    destroy: destroy,
    COLORS: COLORS
  };
})(window);
