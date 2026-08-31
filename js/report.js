/*
 * report.js — 把 scripts/report.py 產生的結構化 JSON 畫成畫面
 *
 * Python 那邊只負責「算出事實」，這裡只負責「怎麼呈現」。
 * 好處是改版面完全不用動 Python，而且以前封存的報告永遠讀得回來。
 *
 * 支援的區塊型別：
 *   quotes 報價列表 / notes 觀察句 / table 表格 / text 純文字
 */
(function (global) {
  'use strict';

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  }

  /* ---------------------------------------------------------- 報價列表 */

  function renderQuotes(sec) {
    var box = el('div', 'rp-quotes');
    (sec.items || []).forEach(function (it) {
      var row = el('div', 'rp-quote' + (it.status !== 'ok' ? ' is-error' : ''));

      var head = el('div', 'rp-quote-head');
      head.appendChild(el('div', 'rp-quote-name', esc(it.name)));

      if (it.status === 'ok') {
        if (it.bars) {
          // 實體金條塊：一列列出各規格
          head.appendChild(el('div', 'rp-quote-price',
            esc(it.bars[0] ? it.bars[0].sellText : '—') +
            '<span class="cur">TWD</span>'));
        } else {
          head.appendChild(el('div', 'rp-quote-price',
            esc(it.priceText) + '<span class="cur">' + esc(it.currency || '') + '</span>' +
            (it.changePctText
              ? '<span class="rp-chg ' + esc(it.dir) + '">' + esc(it.changePctText) + '</span>'
              : '')));
        }
      } else {
        head.appendChild(el('div', 'rp-quote-price',
          '<span class="rp-fail">更新失敗</span>'));
      }
      row.appendChild(head);

      if (it.status !== 'ok') {
        var msg = '這一項資料更新失敗：' + esc(it.error || '原因不明');
        if (it.lastGoodText) {
          msg += '<br><span class="rp-dim">' + esc(it.lastGoodText) +
                 '（僅供參考，不是今天的價格）</span>';
        }
        row.appendChild(el('div', 'rp-err', msg));
        box.appendChild(row);
        return;
      }

      if (it.bars) {
        var t = '<div class="table-scroll"><table class="bar-table"><thead><tr>' +
                '<th>規格</th><th>掛牌價 (TWD)</th><th>每公克</th></tr></thead><tbody>';
        it.bars.forEach(function (b) {
          t += '<tr><td>' + esc(b.spec) + '</td><td>' + esc(b.sellText) +
               '</td><td>' + esc(b.perGramText) + '</td></tr>';
        });
        t += '</tbody></table></div>';
        row.appendChild(el('div', null, t));
      }

      if (it.extra && it.extra.length) {
        var kv = el('div', 'rp-kv');
        it.extra.forEach(function (e) {
          kv.appendChild(el('span', 'kv', esc(e.k) + ' <b>' + esc(e.v) + '</b>'));
        });
        row.appendChild(kv);
      }

      if (it.compare) {
        row.appendChild(el('div', 'rp-compare ' + esc(it.compare.dir),
          esc(it.compare.text)));
      }
      box.appendChild(row);
    });
    return box;
  }

  /* ---------------------------------------------------------- 觀察句 */

  var NOTE_ICON = { up: '▲', down: '▼', warn: '⚠', info: '·' };

  function renderNotes(sec) {
    var box = el('ul', 'rp-notes');
    (sec.items || []).forEach(function (it) {
      var li = el('li', 'rp-note ' + esc(it.level || 'info'));
      li.innerHTML = '<span class="rp-note-icon">' +
                     (NOTE_ICON[it.level] || '·') + '</span>' +
                     '<span>' + esc(it.text) + '</span>';
      box.appendChild(li);
    });
    return box;
  }

  /* ---------------------------------------------------------- 表格 */

  function renderTable(sec) {
    var align = sec.align || [];
    var h = '<div class="table-scroll"><table class="rp-table"><thead><tr>';
    (sec.columns || []).forEach(function (c, i) {
      h += '<th class="al-' + (align[i] || 'left') + '">' + esc(c) + '</th>';
    });
    h += '</tr></thead><tbody>';
    (sec.rows || []).forEach(function (row) {
      h += '<tr>';
      row.forEach(function (cell, i) {
        var text = (cell && typeof cell === 'object') ? cell.text : cell;
        var cls = (cell && typeof cell === 'object' && cell.cls) ? cell.cls : '';
        h += '<td class="al-' + (align[i] || 'left') + ' ' + esc(cls) + '">' +
             esc(text) + '</td>';
      });
      h += '</tr>';
    });
    h += '</tbody></table></div>';
    return el('div', null, h);
  }

  /* ---------------------------------------------------------- 純文字 */

  function renderText(sec) {
    var box = el('div', 'rp-text');
    (sec.paragraphs || []).forEach(function (p) {
      box.appendChild(el('p', null, esc(p)));
    });
    return box;
  }

  var RENDERERS = {
    quotes: renderQuotes,
    notes: renderNotes,
    table: renderTable,
    text: renderText
  };

  /* ---------------------------------------------------------- 整份報告 */

  function marketChips(r) {
    var m = r.market || {};
    var out = [];
    var phase = {
      pre: '台股尚未開盤', open: '台股盤中',
      closed: '台股已收盤', holiday: '台股休市'
    }[m.twPhase] || '台股狀態不明';
    out.push('<span class="chip ' + (m.twTradingDay === false ? 'bad' : '') + '">' +
             esc(phase) + '</span>');
    if (m.goldQuoteTime) {
      out.push('<span class="chip' + (m.goldFresh ? '' : ' bad') + '">黃金掛牌 ' +
               esc(m.goldQuoteTime) + (m.goldFresh ? '' : '（非今日）') + '</span>');
    }
    if (m.fxDate) {
      out.push('<span class="chip' + (m.fxFresh ? '' : ' bad') + '">匯率 ' +
               esc(m.fxDate) + (m.fxFresh ? '' : '（非今日）') + '</span>');
    }
    var ds = r.dataStatus || {};
    if (ds.error) {
      out.push('<span class="chip bad">⚠ ' + ds.error + ' 項更新失敗：' +
               esc((ds.errorNames || []).join('、')) + '</span>');
    } else if (ds.ok) {
      out.push('<span class="chip ok">✓ ' + ds.ok + ' 項成功</span>');
    }
    return out.join('');
  }

  /*
   * 把一份報告畫進 container。
   */
  function render(container, r) {
    container.innerHTML = '';
    if (!r) {
      container.appendChild(el('div', 'fatal', '找不到這份報告。'));
      return;
    }

    var head = el('div', 'rp-head');
    head.appendChild(el('h2', 'rp-title', esc(r.title || r.slotLabel || '')));
    head.appendChild(el('div', 'rp-meta',
      esc(r.date) + '　產生於 ' + esc(r.generatedAtText || '') + '（台北時間）'));
    head.appendChild(el('div', 'rp-chips', marketChips(r)));
    if (r.market && r.market.twNote) {
      head.appendChild(el('div', 'rp-note-line', esc(r.market.twNote)));
    }
    container.appendChild(head);

    (r.sections || []).forEach(function (sec) {
      var fn = RENDERERS[sec.type];
      if (!fn) return;
      var wrap = el('section', 'rp-section');
      wrap.appendChild(el('h3', 'rp-section-title', esc(sec.title || '')));
      if (sec.subtitle) {
        wrap.appendChild(el('div', 'rp-subtitle', esc(sec.subtitle)));
      }
      wrap.appendChild(fn(sec));
      container.appendChild(wrap);
    });

    if (r.disclaimer) {
      container.appendChild(el('div', 'disclaimer',
        '<b>⚠</b> ' + esc(r.disclaimer)));
    }
  }

  global.Report = { render: render };
})(window);
