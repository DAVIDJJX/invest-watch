/*
 * history.js — 歷史頁：選日期 → 看當天三份報告與行情快照
 *
 * 資料來源全是自家倉庫的靜態檔：
 *   data/archive/index.json               有哪些日期、各有哪幾份報告
 *   data/archive/<日期>/<slot>.json       報告本體
 *   data/archive/<日期>/snapshot.json     當天最後一次的行情快照
 *
 * 網址會帶上 #日期/時段（例如 #2026-08-31/close），
 * 可以直接把連結存起來或分享，上一頁也能正常回去。
 */
(function () {
  'use strict';

  var SLOT_NAME = {
    morning: '晨報',
    midday: '午盤',
    close: '收盤',
    manual: '手動更新'
  };
  var SLOT_ORDER = ['morning', 'midday', 'close', 'manual'];

  var state = { index: null, day: null, slot: null, month: null, cache: {} };

  function $(s, r) { return (r || document).querySelector(s); }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function fetchJSON(path) {
    var url = path + (path.indexOf('?') < 0 ? '?' : '&') +
              't=' + Math.floor(Date.now() / 60000);
    return fetch(url, { cache: 'no-cache' }).then(function (r) {
      if (!r.ok) throw new Error('讀取失敗（HTTP ' + r.status + '）');
      return r.json();
    });
  }

  function num(v, dec) {
    if (typeof v !== 'number' || !isFinite(v)) return '—';
    return v.toLocaleString('zh-TW', {
      minimumFractionDigits: dec, maximumFractionDigits: dec
    });
  }

  /* ---------------------------------------------------------- 月曆選日期
   *
   * 用月曆而不是清單：報告一天天累積下去，清單很快就會長到找不到東西。
   * 月曆一眼就看得出「哪幾天有報告、哪天缺、哪天休市」。
   */

  var WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'];

  function ym(dateStr) { return String(dateStr).slice(0, 7); }   // 2026-09-01 -> 2026-09

  function ymTitle(m) {
    var p = m.split('-');
    return p[0] + ' 年 ' + Number(p[1]) + ' 月';
  }

  function shiftMonth(m, delta) {
    var p = m.split('-');
    var d = new Date(Number(p[0]), Number(p[1]) - 1 + delta, 1);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  }

  /* 依日期建索引，月曆才查得快 */
  function dayMap() {
    var map = {};
    ((state.index && state.index.days) || []).forEach(function (d) {
      map[d.date] = d;
    });
    return map;
  }

  function renderDays() {
    var box = $('#day-list');
    var days = (state.index && state.index.days) || [];
    if (!days.length) {
      box.innerHTML = '<div class="rp-dim" style="padding:10px 2px">' +
        '還沒有任何封存的報告。第一份會在下一次自動更新後出現。</div>';
      return;
    }

    var map = dayMap();
    var newest = days[0].date;                 // index 是新到舊排序
    var oldest = days[days.length - 1].date;
    if (!state.month) state.month = ym(state.day || newest);

    var p = state.month.split('-');
    var year = Number(p[0]), mon = Number(p[1]);
    var first = new Date(year, mon - 1, 1);
    var daysInMonth = new Date(year, mon, 0).getDate();
    var lead = first.getDay();                 // 這個月 1 號是星期幾（0=日）

    var canPrev = state.month > ym(oldest);
    var canNext = state.month < ym(newest);

    var h = '<div class="cal">';
    h += '<div class="cal-head">' +
           '<button class="cal-nav" data-dir="-1"' + (canPrev ? '' : ' disabled') +
             ' aria-label="上個月">‹</button>' +
           '<span class="cal-title">' + esc(ymTitle(state.month)) + '</span>' +
           '<button class="cal-nav" data-dir="1"' + (canNext ? '' : ' disabled') +
             ' aria-label="下個月">›</button>' +
         '</div>';

    h += '<div class="cal-grid">';
    WEEKDAYS.forEach(function (w) { h += '<span class="cal-wd">' + w + '</span>'; });
    for (var i = 0; i < lead; i++) h += '<span class="cal-pad"></span>';

    for (var dd = 1; dd <= daysInMonth; dd++) {
      var ds = year + '-' + String(mon).padStart(2, '0') + '-' + String(dd).padStart(2, '0');
      var entry = map[ds];
      if (!entry) {
        h += '<span class="cal-day is-empty">' + dd + '</span>';
        continue;
      }
      var nSlots = (entry.slots || []).length;
      var bad = entry.dataStatus && entry.dataStatus.error;
      var cls = 'cal-day has-data';
      if (ds === state.day) cls += ' is-active';
      if (entry.twTradingDay === false) cls += ' is-holiday';
      var tip = ds + '　' + nSlots + ' 份報告' +
                (entry.twTradingDay === false ? '　台股休市' : '') +
                (bad ? '　' + bad + ' 項更新失敗' : '');
      h += '<button class="' + cls + '" data-date="' + esc(ds) + '" title="' + esc(tip) + '">' +
             dd +
             '<span class="cal-dots">' +
               (bad ? '<i class="cal-dot bad"></i>'
                    : new Array(Math.min(nSlots, 3) + 1).join('<i class="cal-dot"></i>')) +
             '</span>' +
           '</button>';
    }
    h += '</div>';

    h += '<div class="cal-legend">' +
           '<span><i class="cal-dot"></i> 一點＝一份報告</span>' +
           '<span><i class="cal-dot bad"></i> 有更新失敗</span>' +
           '<span class="cal-holiday-key">灰底＝台股休市</span>' +
         '</div>';

    if (state.day !== newest) {
      h += '<button class="cal-jump" id="cal-jump">跳到最新（' + esc(newest) + '）</button>';
    }
    h += '</div>';
    box.innerHTML = h;

    box.querySelectorAll('.cal-day.has-data').forEach(function (b) {
      b.addEventListener('click', function () { go(b.dataset.date, null); });
    });
    box.querySelectorAll('.cal-nav').forEach(function (b) {
      b.addEventListener('click', function () {
        if (b.disabled) return;
        state.month = shiftMonth(state.month, Number(b.dataset.dir));
        renderDays();
      });
    });
    var jump = $('#cal-jump');
    if (jump) {
      jump.addEventListener('click', function () {
        state.month = ym(newest);
        go(newest, null);
      });
    }
  }

  /* ---------------------------------------------------------- 時段分頁 */

  function dayEntry(date) {
    return ((state.index && state.index.days) || []).filter(function (d) {
      return d.date === date;
    })[0];
  }

  function renderTabs() {
    var entry = dayEntry(state.day);
    var slots = (entry && entry.slots) || [];
    slots = SLOT_ORDER.filter(function (s) { return slots.indexOf(s) >= 0; });
    var times = (entry && entry.slotTimes) || {};
    var h = '';
    slots.forEach(function (s) {
      // 標示實際產生時間，而不是排程的名目時間（Actions 常會晚幾分鐘才啟動）
      var t = times[s];
      h += '<button class="slot-tab' + (s === state.slot ? ' active' : '') +
           '" data-slot="' + esc(s) + '">' + esc(SLOT_NAME[s] || s) +
           (t ? '<span class="slot-time">' + esc(t) + '</span>' : '') +
           '</button>';
    });

    // 缺少的時段也要顯示出來（灰色不可點），不要安靜地當作不存在——
    // 2026-09-01 就發生過整份晨報遺失，畫面上卻完全看不出來。
    var missing = ['morning', 'midday', 'close'].filter(function (s) {
      return slots.indexOf(s) < 0;
    });
    if (entry && entry.twTradingDay !== false) {
      missing.forEach(function (s) {
        h += '<span class="slot-tab is-missing" title="這個時段沒有產生報告">' +
             esc(SLOT_NAME[s] || s) + '<span class="slot-time">缺</span></span>';
      });
    }

    var box = $('#slot-tabs');
    var oldNote = $('#missing-note');    // 切換日期時先清掉上一天的提示
    if (oldNote) oldNote.remove();
    box.innerHTML = h;
    if (missing.length && entry && entry.twTradingDay !== false) {
      var names = missing.map(function (s) { return SLOT_NAME[s]; }).join('、');
      box.insertAdjacentHTML('afterend',
        '<div class="rp-note-line" id="missing-note">這一天缺少「' + esc(names) +
        '」報告——多半是那個時段的自動更新沒有跑到（例如電腦在待機、' +
        '或雲端排程被跳過）。</div>');
    }
    box.querySelectorAll('.slot-tab').forEach(function (b) {
      b.addEventListener('click', function () { go(state.day, b.dataset.slot); });
    });
  }

  /* ---------------------------------------------------------- 當日快照 */

  function renderSnapshot(snap) {
    var box = $('#snapshot');
    if (!snap || !snap.assets) {
      box.innerHTML = '<div class="rp-dim">這一天沒有留下行情快照。</div>';
      return;
    }
    var h = '<div class="table-scroll"><table class="rp-table"><thead><tr>' +
            '<th class="al-left">標的</th><th class="al-right">收盤/最後價格</th>' +
            '<th class="al-right">當日漲跌</th><th class="al-left">資料日期</th>' +
            '</tr></thead><tbody>';
    Object.keys(snap.assets).forEach(function (id) {
      var a = snap.assets[id];
      if (a.status !== 'ok') {
        h += '<tr><td class="al-left">' + esc(a.name) + '</td>' +
             '<td class="al-right err" colspan="2">更新失敗</td>' +
             '<td class="al-left">' + esc(a.date || '—') + '</td></tr>';
        return;
      }
      var pct = a.changePct;
      var cls = typeof pct === 'number' ? (pct > 0 ? 'up' : (pct < 0 ? 'down' : '')) : '';
      h += '<tr><td class="al-left">' + esc(a.name) + '</td>' +
           '<td class="al-right">' + num(a.price, a.decimals) + '</td>' +
           '<td class="al-right ' + cls + '">' +
             (typeof pct === 'number'
               ? (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%' : '—') + '</td>' +
           '<td class="al-left">' + esc(a.date || '—') + '</td></tr>';
    });
    h += '</tbody></table></div>' +
         '<div class="rp-dim" style="margin-top:8px">快照時間：' +
         esc((snap.updatedAtText || '—')) + '（台北時間）</div>';
    box.innerHTML = h;
  }

  /* ---------------------------------------------------------- 載入與切換 */

  function load(path) {
    if (state.cache[path]) return Promise.resolve(state.cache[path]);
    return fetchJSON(path).then(function (j) {
      state.cache[path] = j;
      return j;
    });
  }

  function show() {
    var entry = dayEntry(state.day);
    if (!entry) {
      $('#report').innerHTML = '<div class="fatal">找不到 ' + esc(state.day) +
        ' 的報告。</div>';
      return;
    }
    var slots = SLOT_ORDER.filter(function (s) {
      return (entry.slots || []).indexOf(s) >= 0;
    });
    if (!state.slot || slots.indexOf(state.slot) < 0) {
      // 預設看當天「產生時間最晚」的那份報告，不是時段名稱排最後的那份
      var times = entry.slotTimes || {};
      state.slot = slots.slice().sort(function (a, b) {
        return String(times[a] || '').localeCompare(String(times[b] || ''));
      }).pop();
    }
    location.hash = state.day + '/' + state.slot;
    renderDays();
    renderTabs();

    $('#report').innerHTML = '<div class="loading">載入報告中…</div>';
    $('#snapshot').innerHTML = '<div class="loading">載入中…</div>';

    var base = 'data/archive/' + state.day + '/';
    load(base + state.slot + '.json')
      .then(function (r) { window.Report.render($('#report'), r); })
      .catch(function (e) {
        $('#report').innerHTML = '<div class="fatal">報告讀取失敗：' +
          esc(e.message) + '</div>';
      });
    load(base + 'snapshot.json')
      .then(renderSnapshot)
      .catch(function () {
        $('#snapshot').innerHTML = '<div class="rp-dim">這一天沒有留下行情快照。</div>';
      });
  }

  function go(date, slot) {
    state.day = date;
    state.slot = slot;
    state.month = ym(date);      // 月曆跟著跳到選到的那個月
    location.hash = date + (slot ? '/' + slot : '');
    show();
  }

  function fromHash() {
    var h = (location.hash || '').replace(/^#/, '');
    if (!h) return null;
    var p = h.split('/');
    return { date: p[0], slot: p[1] || null };
  }

  function boot() {
    fetchJSON('data/archive/index.json')
      .then(function (idx) {
        state.index = idx;
        var days = idx.days || [];
        if (!days.length) {
          renderDays();
          $('#report').innerHTML = '<div class="placeholder">' +
            '<div class="big">🗓</div><h2>還沒有封存的報告</h2>' +
            '<p>報告會在每天 10:00 / 13:00 / 15:00 自動更新後產生，' +
            '之後這裡就會一天天累積起來。</p></div>';
          $('#snapshot').innerHTML = '';
          return;
        }
        var want = fromHash();
        var valid = want && days.some(function (d) { return d.date === want.date; });
        state.day = valid ? want.date : days[0].date;
        state.slot = valid ? want.slot : null;
        show();
      })
      .catch(function (e) {
        $('#report').innerHTML =
          '<div class="fatal">讀不到封存索引（data/archive/index.json）：' +
          esc(e.message) + '<br><br>如果你是剛部署好，' +
          '第一份報告會在下一次資料更新後出現。</div>';
      });
  }

  window.addEventListener('hashchange', function () {
    var w = fromHash();
    if (w && (w.date !== state.day || w.slot !== state.slot)) {
      state.day = w.date;
      state.slot = w.slot;
      show();
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
