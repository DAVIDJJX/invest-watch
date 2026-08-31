/*
 * app.js — 儀表板頁面邏輯
 *
 * 資料來源全部是自家倉庫的靜態 JSON（data/latest.json、data/history/<id>.json），
 * 由 GitHub Actions 上的 scripts/fetch_data.py 每天更新三次。
 * 前端不直接連台銀／證交所（會被 CORS 擋，見 PLAN.md 7.8）。
 *
 * 誠實原則：任一標的抓取失敗，卡片會明確顯示「資料更新失敗＋失敗時間」，
 * 舊價格只會出現在標了「上次成功」字樣的灰字裡，不會冒充成現價。
 */
(function () {
  'use strict';

  var GROUP_ORDER = ['貴金屬', '台股', '海外', '匯率'];
  var state = { latest: null, history: {}, openId: null };

  /* ---------------------------------------------------------- 小工具 */

  function $(sel, root) { return (root || document).querySelector(sel); }

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function num(v, dec) {
    if (typeof v !== 'number' || !isFinite(v)) return '—';
    return v.toLocaleString('zh-TW', {
      minimumFractionDigits: dec, maximumFractionDigits: dec
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

  function arrow(v) {
    if (typeof v !== 'number' || !isFinite(v) || v === 0) return '—';
    return v > 0 ? '▲' : '▼';
  }

  /* 2026-08-31T21:21:17+08:00 -> 08/31 21:21 */
  function shortTime(iso) {
    if (!iso) return '—';
    var m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    return m ? (m[2] + '/' + m[3] + ' ' + m[4] + ':' + m[5]) : iso;
  }

  function fetchJSON(path) {
    // 加時間戳避免瀏覽器拿到快取的舊資料
    var url = path + (path.indexOf('?') < 0 ? '?' : '&') +
              't=' + Math.floor(Date.now() / 60000);
    return fetch(url, { cache: 'no-cache' }).then(function (r) {
      if (!r.ok) throw new Error(path + ' 讀取失敗（HTTP ' + r.status + '）');
      return r.json();
    });
  }

  /* ---------------------------------------------------------- 頂部狀態列 */

  function renderStatus(latest) {
    var bar = $('#status-bar');
    if (!bar) return;
    var s = latest.summary || {};
    var bad = (s.error || 0) > 0;

    var html = '';
    html += '<span class="chip time">最後更新　' + esc(latest.updatedAtText || '—') +
            '（台北時間）</span>';
    html += '<span class="chip slot">' + esc(latest.slotLabel || latest.slot || '—') + '</span>';
    html += '<span class="chip ' + (bad ? 'bad' : 'ok') + '">' +
              (bad ? '⚠ ' : '✓ ') + (s.ok || 0) + ' 項成功' +
              (bad ? ' ／ ' + s.error + ' 項更新失敗' : '') +
            '</span>';
    if (bad && s.errorIds && s.errorIds.length) {
      var names = s.errorIds.map(function (id) {
        var a = latest.assets[id];
        return a ? a.shortName || a.name : id;
      });
      html += '<span class="chip bad">失敗：' + esc(names.join('、')) + '</span>';
    }
    bar.innerHTML = html;
  }

  /* ---------------------------------------------------------- 卡片：頭部 */

  function goldPairHtml(a, latest) {
    var dec = a.decimals;
    var h = '';
    h += '<span class="kv">本行買入 <b>' + num(a.buy, dec) + '</b></span>';
    h += '<span class="kv">本行賣出 <b>' + num(a.sell, dec) + '</b></span>';

    // 兩種幣別對照：黃金存摺台銀本來就同時掛 TWD 與 CNY 牌價
    var other = a.id === 'gold_twd' ? latest.assets.gold_cny
              : a.id === 'gold_cny' ? latest.assets.gold_twd : null;
    if (other && other.status === 'ok' && typeof other.sell === 'number') {
      h += '<span class="kv">' + (other.currency === 'CNY' ? '人民幣計價' : '台幣計價') +
           ' <b>' + num(other.sell, other.decimals) + '</b></span>';
    }
    if (a.quoteTime) {
      h += '<span class="kv">掛牌 <b>' + esc(a.quoteTime) + '</b></span>';
    }
    return h;
  }

  function fxPairHtml(a) {
    var d = a.decimals;
    var h = '';
    h += '<span class="kv">即期買入 <b>' + num(a.spotBuy, d) + '</b></span>';
    h += '<span class="kv">即期賣出 <b>' + num(a.spotSell, d) + '</b></span>';
    if (typeof a.cashBuy === 'number') {
      h += '<span class="kv">現金買入 <b>' + num(a.cashBuy, d) + '</b></span>';
      h += '<span class="kv">現金賣出 <b>' + num(a.cashSell, d) + '</b></span>';
    }
    return h;
  }

  /*
   * 實體黃金條塊：列出各規格掛牌價，並換算成「每公克單價」。
   * 每公克單價才能互相比較——買越大條通常每公克越便宜，
   * 也才看得出比黃金存摺貴多少（那個差價就是鑄造與加工費）。
   */
  function barTableHtml(a, latest) {
    var fx = latest.assets.fx_cny;
    var rate = (fx && fx.status === 'ok') ? fx.spotSell : null;
    var hasPremium = (a.bars || []).some(function (b) {
      return typeof b.premiumPct === 'number';
    });

    var h = '<div class="table-scroll"><table class="bar-table"><thead><tr>' +
            '<th>規格</th><th>掛牌賣出 (TWD)</th><th>每公克</th>' +
            (hasPremium ? '<th>比存摺貴</th>' : '') +
            (rate ? '<th>約當人民幣</th>' : '') +
            '</tr></thead><tbody>';
    (a.bars || []).forEach(function (b) {
      h += '<tr><td>' + esc(b.spec) + '</td>';
      h += '<td>' + num(b.sell, 0) + '</td>';
      h += '<td>' + (typeof b.perGram === 'number' ? num(b.perGram, 1) : '—') + '</td>';
      if (hasPremium) {
        h += '<td>' + (typeof b.premiumPct === 'number'
              ? '+' + b.premiumPct.toFixed(2) + '%' : '—') + '</td>';
      }
      if (rate) h += '<td>' + num(b.sell / rate, 0) + '</td>';
      h += '</tr>';
    });
    h += '</tbody></table></div>';

    var notes = [];
    if (hasPremium && typeof a.goldSell === 'number') {
      notes.push('「比存摺貴」是把每公克單價和今日黃金存摺賣出價 ' +
                 num(a.goldSell, 0) + ' 元相比，差額就是鑄造與加工費。');
    }
    if (rate) {
      notes.push('人民幣欄位是用今日台銀 CNY 即期賣出 ' + num(rate, 3) +
                 ' 換算的參考值，不是台銀的人民幣掛牌價。');
    }
    if (notes.length) {
      h += '<div class="range-note">' + notes.join('<br>') + '</div>';
    }
    return h;
  }

  function errorHtml(a) {
    var h = '<div class="err-box"><b>⚠ 這一項資料更新失敗</b>　' +
            esc(a.error || '未知原因') +
            '<br>失敗時間：' + esc(shortTime(a.errorAt || a.fetchedAt));
    var lg = a.lastGood;
    if (lg && (typeof lg.price === 'number' || lg.bars)) {
      h += '<br>上次成功：' + esc(lg.date || '—') + '　';
      if (typeof lg.price === 'number') {
        h += '收盤 ' + num(lg.price, a.decimals) + '（僅供參考，不是現在的價格）';
      } else {
        h += '（僅供參考，不是現在的價格）';
      }
    }
    h += '</div>';
    return h;
  }

  function makeCard(a, latest) {
    var card = el('article', 'card');
    card.dataset.id = a.id;
    if (a.status !== 'ok') card.classList.add('is-error');

    var isBar = a.type === 'bot_gold_bar';
    // 條塊卡也能展開：台銀不公布條塊歷史牌價，展開後先用黃金存摺的走勢代表金價，
    // 等本站自己累積夠天數再換成條塊自己的線。
    var hasHistory = a.status === 'ok' && (isBar || (a.points || 0) > 1);

    /* --- 頭部 --- */
    var head = el('div', 'card-head');

    var left = el('div');
    var name = el('div', 'card-name');
    // 位置燈號要等歷史資料載進來才算得出來，先放一個佔位。
    // 條塊沒有足夠歷史可以算區間位置，就不掛燈號。
    name.innerHTML = esc(a.name) + (hasHistory && !isBar
      ? ' <span class="signal unknown" data-signal="' + esc(a.id) + '">' +
        '<span class="dot"></span>計算中</span>'
      : '');
    left.appendChild(name);

    var subBits = [];
    if (a.priceLabel && !isBar) subBits.push(esc(a.priceLabel));
    if (a.unit) subBits.push(esc(a.unit));
    if (a.date) subBits.push(esc(a.date));
    if (a.quoteTime && !isBar && a.type !== 'bot_gold') subBits.push(esc(a.quoteTime));
    left.appendChild(el('div', 'card-sub',
      subBits.join(' · ') +
      (hasHistory ? ' <span class="caret">▼ 看走勢</span>' : '')));
    head.appendChild(left);

    var right = el('div', 'card-price-box');
    if (isBar) {
      var top = (a.bars || [])[0];
      right.innerHTML =
        '<div class="card-price">' + (top ? num(top.sell, 0) : '—') +
        '<span class="cur">TWD</span></div>' +
        '<div class="card-change flat">1 公斤掛牌</div>';
    } else if (a.status === 'ok') {
      right.innerHTML =
        '<div class="card-price">' + num(a.price, a.decimals) +
        '<span class="cur">' + esc(a.currency || '') + '</span></div>' +
        '<div class="card-change ' + dirClass(a.change) + '">' +
          arrow(a.change) + ' ' + num(Math.abs(a.change || 0), a.decimals) +
          '　' + pct(a.changePct) +
        '</div>';
    } else {
      right.innerHTML =
        '<div class="card-price" style="color:var(--text-faint)">—</div>' +
        '<div class="card-change" style="color:var(--error)">更新失敗</div>';
    }
    head.appendChild(right);

    /* --- 買賣價 / 條塊表 --- */
    if (a.status === 'ok') {
      if (a.type === 'bot_gold') {
        head.appendChild(el('div', 'pair', goldPairHtml(a, latest)));
      } else if (a.type === 'bot_fx') {
        head.appendChild(el('div', 'pair', fxPairHtml(a)));
      } else if (isBar) {
        var wrap = el('div', 'pair');
        wrap.style.display = 'block';
        wrap.innerHTML =
          (a.quoteTime ? '<div class="card-sub" style="margin-bottom:6px">掛牌時間 ' +
            esc(a.quoteTime) + '</div>' : '') + barTableHtml(a, latest);
        head.appendChild(wrap);
      }
    }

    /* --- 位置燈號（要有歷史才算得出來） --- */
    if (hasHistory) {
      var spark = el('div', 'spark');
      // 線的顏色跟當日漲跌一致，避免同一張卡出現兩種相反的紅綠訊號
      spark.innerHTML = window.Charts.sparkline(a.spark || [], {
        color: typeof a.change === 'number' && a.change !== 0
          ? (a.change > 0 ? window.Charts.COLORS.up : window.Charts.COLORS.down)
          : null
      });
      head.appendChild(spark);
    }

    if (a.status !== 'ok') head.appendChild(el('div', null, errorHtml(a)));
    if (a.sourceNote) {
      head.appendChild(el('div', 'note-box', '註：' + esc(a.sourceNote)));
    }
    if (a.carriedOver) {
      head.appendChild(el('div', 'note-box',
        '這一項本次沒有重新抓取，顯示的是 ' + esc(shortTime(a.fetchedAt)) + ' 的資料。'));
    }

    card.appendChild(head);

    /* --- 展開區（先留空，點開才載入歷史） --- */
    if (hasHistory) {
      var body = el('div', 'card-body');
      body.innerHTML = '<div class="loading">載入歷史資料中…</div>';
      card.appendChild(body);
      head.addEventListener('click', function () { toggleCard(card, a); });
      head.setAttribute('role', 'button');
      head.setAttribute('tabindex', '0');
      head.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          toggleCard(card, a);
        }
      });
    }
    return card;
  }

  /* ---------------------------------------------------------- 展開：圖表＋指標 */

  function metricsHtml(a, ind) {
    var d = a.decimals;
    function box(k, v, cls, note) {
      return '<div class="metric"><div class="k">' + k + '</div>' +
             '<div class="v ' + (cls || '') + '">' + v + '</div>' +
             (note ? '<div class="n">' + note + '</div>' : '') + '</div>';
    }
    var h = '<div class="metrics">';
    h += box('RSI (14)',
             ind.rsi14 === null ? '—' : ind.rsi14.toFixed(1),
             '',
             ind.rsi14 === null ? '資料不足 15 點' : '0～100，越高代表近期漲多');
    h += box('現價 vs MA20',
             ind.vsMa20 === null ? '—' : pct(ind.vsMa20),
             dirClass(ind.vsMa20),
             ind.ma20 === null ? '不足 20 個交易日' : 'MA20 = ' + num(ind.ma20, d));
    h += box('現價 vs MA60',
             ind.vsMa60 === null ? '—' : pct(ind.vsMa60),
             dirClass(ind.vsMa60),
             ind.ma60 === null ? '不足 60 個交易日' : 'MA60 = ' + num(ind.ma60, d));
    h += box('近 10 日漲跌',
             ind.change10d === null ? '—' : pct(ind.change10d),
             dirClass(ind.change10d),
             '與 10 個交易日前收盤比');
    h += box('年化波動 (30日)',
             ind.volatility30 === null ? '—' : ind.volatility30.toFixed(1) + '%',
             '',
             '僅供了解價格起伏大小');
    h += box('歷史資料點',
             ind.count + ' 天',
             '',
             '本站保留最近 400 個交易日');
    h += '</div>';
    return h;
  }

  function rangeHtml(a, ind) {
    var r = ind.range;
    if (!r) return '';
    var d = a.decimals;
    var sig = ind.signal;
    var windowText = r.full ? '52 週' : ('近 ' + Math.max(1, Math.round(r.days / 21)) + ' 個月');
    var h = '<div class="range-bar">';
    h += '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">' +
           '<span style="font-size:12px;color:var(--text-faint)">' + windowText +
           '區間位置</span>' +
           '<span class="signal ' + sig.level + '"><span class="dot"></span>' +
           esc(sig.label) + '</span>' +
         '</div>';
    h += '<div class="range-track"><span class="pin" style="left:' +
         Math.max(0, Math.min(100, r.percentile)).toFixed(1) + '%"></span></div>';
    h += '<div class="range-ends"><span>最低 ' + num(r.low, d) + '</span>' +
         '<span>第 ' + r.percentile.toFixed(0) + ' 百分位</span>' +
         '<span>最高 ' + num(r.high, d) + '</span></div>';
    h += '<div class="range-note">分類規則：低於 25% → 相對低檔區；25%～75% → 中性；' +
         '高於 75% → 相對高檔區。目前' + esc(sig.rule) + '。' +
         (r.full ? '' : '（這項標的目前只有 ' + r.days + ' 個交易日的資料，' +
           '所以是用實際區間算的，不是完整 52 週。）') +
         '</div>';
    h += '</div>';
    return h;
  }

  /*
   * 實體條塊的展開內容。
   * 台銀不公布條塊的歷史牌價，所以：
   *   累積夠 5 天 → 畫條塊自己的「每公克單價」走勢
   *   還不夠     → 先畫黃金存摺賣出價（同一塊金子的價格，條塊只是再加鑄造費）
   * 兩種情況都會在圖下面寫清楚現在看的是哪一條線。
   */
  function renderBarBody(card, a) {
    var body = $('.card-body', card);
    var barPts = ((state.history.gold_bar || {}).points) || [];
    var goldPts = ((state.history.gold_twd || {}).points) || [];
    var useBar = barPts.length >= 5;

    var points = (useBar ? barPts : goldPts).filter(function (p) {
      return typeof p.c === 'number';
    });
    var since = barPts.length ? barPts[0].d : null;

    if (points.length < 2) {
      body.innerHTML = '<div class="fatal">目前還沒有足夠的歷史可以畫圖。' +
        '台銀不公布實體條塊的歷史牌價，本站' +
        (since ? '從 ' + esc(since) + ' 起' : '') +
        '開始自行累積，過幾天這裡就會有走勢了。</div>';
      return;
    }

    var canvasId = 'chart-' + a.id;
    var label = useBar ? '1 公斤條塊 每公克單價' : '黃金存摺 本行賣出';
    var explain = useBar
      ? ('這是本站自 ' + esc(since) + ' 起累積的台銀條塊掛牌價，換算成每公克單價，' +
         '目前共 ' + barPts.length + ' 個交易日。')
      : ('台銀不公布實體條塊的歷史牌價，本站' +
         (since ? '從 ' + esc(since) + ' 起' : '') + '自行累積（目前 ' +
         barPts.length + ' 天，滿 5 天後這裡會換成條塊自己的走勢）。' +
         '下圖先用<b>黃金存摺賣出價</b>代表金價走勢——條塊價就是它再加上鑄造與加工費。');

    body.innerHTML =
      '<div class="chart-box"><canvas id="' + canvasId + '"></canvas></div>' +
      '<div class="chart-legend">' +
        '<span><i style="background:' + window.Charts.COLORS.line + '"></i>' +
          esc(label) + '</span>' +
        '<span><i style="background:' + window.Charts.COLORS.ma20 + '"></i>MA20</span>' +
        '<span><i style="background:' + window.Charts.COLORS.ma60 + '"></i>MA60</span>' +
        '<span style="color:var(--text-faint)">' + points.length + ' 個交易日 · ' +
          esc(points[0].d) + ' ～ ' + esc(points[points.length - 1].d) + '</span>' +
      '</div>' +
      '<div class="range-bar"><div class="range-note">' + explain + '</div></div>';

    window.Charts.drawHistory(document.getElementById(canvasId), points,
      { decimals: useBar ? 1 : 0, label: label });
  }

  function renderBody(card, a, hist) {
    if (a.type === 'bot_gold_bar') return renderBarBody(card, a);
    var body = $('.card-body', card);
    var points = (hist && hist.points) || [];
    if (points.length < 2) {
      body.innerHTML = '<div class="fatal">這個標的還沒有足夠的歷史資料可以畫圖。</div>';
      return;
    }

    var ind = window.Indicators.computeAll(points, a.price);
    var canvasId = 'chart-' + a.id;

    body.innerHTML =
      '<div class="chart-box"><canvas id="' + canvasId + '"></canvas></div>' +
      '<div class="chart-legend">' +
        '<span><i style="background:' + window.Charts.COLORS.line + '"></i>' +
          esc(a.priceLabel || '收盤') + '</span>' +
        '<span><i style="background:' + window.Charts.COLORS.ma20 + '"></i>MA20</span>' +
        '<span><i style="background:' + window.Charts.COLORS.ma60 + '"></i>MA60</span>' +
        '<span style="color:var(--text-faint)">' + points.length + ' 個交易日 · ' +
          esc(points[0].d) + ' ～ ' + esc(points[points.length - 1].d) + '</span>' +
      '</div>' +
      metricsHtml(a, ind) +
      rangeHtml(a, ind);

    var ok = window.Charts.drawHistory(
      document.getElementById(canvasId), points,
      { decimals: a.decimals, label: a.priceLabel || '收盤' }
    );
    if (!ok) {
      $('.chart-box', body).innerHTML =
        '<div class="fatal">圖表函式庫沒有載入成功（lib/chart.umd.min.js）。</div>';
    }
  }

  function toggleCard(card, a) {
    var opening = !card.classList.contains('is-open');

    // 一次只開一張，避免手機上一路往下滑
    document.querySelectorAll('.card.is-open').forEach(function (c) {
      if (c !== card) {
        c.classList.remove('is-open');
        window.Charts.destroy('chart-' + c.dataset.id);
      }
    });

    if (!opening) {
      card.classList.remove('is-open');
      window.Charts.destroy('chart-' + a.id);
      return;
    }
    card.classList.add('is-open');

    // 條塊的圖需要同時有條塊歷史與黃金存摺歷史（存摺是底圖）
    var need = a.type === 'bot_gold_bar' ? ['gold_bar', 'gold_twd'] : [a.id];
    var missing = need.filter(function (id) { return !state.history[id]; });

    if (!missing.length) {
      renderBody(card, a, state.history[a.id]);
      return;
    }
    ensureHistories(need)
      .then(function () {
        if (!card.classList.contains('is-open')) return;
        if (!state.history[a.id]) {
          $('.card-body', card).innerHTML =
            '<div class="fatal">歷史資料讀取失敗。</div>';
          return;
        }
        renderBody(card, a, state.history[a.id]);
      });
  }

  /* 確保這些標的的歷史都載進 state.history（已載過的不重複抓） */
  function ensureHistories(ids) {
    return Promise.all(ids.map(function (id) {
      if (state.history[id]) return Promise.resolve(state.history[id]);
      return fetchJSON('data/history/' + id + '.json')
        .then(function (h) { state.history[id] = h; return h; })
        .catch(function () { return null; });
    }));
  }

  /* ---------------------------------------------------------- 主流程 */

  function render(latest) {
    state.latest = latest;
    renderStatus(latest);

    var root = $('#dashboard');
    root.innerHTML = '';

    var byGroup = {};
    Object.keys(latest.assets).forEach(function (id) {
      var a = latest.assets[id];
      var g = a.group || '其他';
      (byGroup[g] = byGroup[g] || []).push(a);
    });

    var groups = GROUP_ORDER.filter(function (g) { return byGroup[g]; })
      .concat(Object.keys(byGroup).filter(function (g) {
        return GROUP_ORDER.indexOf(g) < 0;
      }));

    groups.forEach(function (g) {
      root.appendChild(el('div', 'section-title', esc(g)));
      var grid = el('div', 'cards');
      byGroup[g].forEach(function (a) { grid.appendChild(makeCard(a, latest)); });
      root.appendChild(grid);
    });
  }

  /* 卡片上的位置燈號需要整年歷史才算得準，所以開頁後把各標的歷史一起載進來。
     每個檔約 10KB，載完也讓「點開卡片」變成即時反應。 */
  function updateSignal(a, hist) {
    var node = document.querySelector('[data-signal="' + a.id + '"]');
    if (!node) return;
    var ind = window.Indicators.computeAll((hist && hist.points) || [], a.price);
    var s = ind.signal;
    node.className = 'signal ' + s.level;
    node.innerHTML = '<span class="dot"></span>' + esc(s.label);
    node.title = s.rule;
  }

  function loadHistories(latest) {
    Object.keys(latest.assets).forEach(function (id) {
      var a = latest.assets[id];
      var isBar = a.type === 'bot_gold_bar';
      if (a.status !== 'ok') return;
      if (!(a.points > 1) && !(isBar && a.points >= 1)) return;
      fetchJSON('data/history/' + id + '.json')
        .then(function (h) {
          state.history[id] = h;
          updateSignal(a, h);
          var card = document.querySelector('.card[data-id="' + id + '"]');
          if (card && card.classList.contains('is-open')) renderBody(card, a, h);
        })
        .catch(function () {
          var node = document.querySelector('[data-signal="' + id + '"]');
          if (node) {
            node.className = 'signal unknown';
            node.innerHTML = '<span class="dot"></span>燈號不可用';
            node.title = '歷史資料讀取失敗，無法計算區間位置';
          }
        });
    });
  }

  /* 頂部那條「今天的報告」入口。沒有報告就整條不顯示。 */
  function loadReportBanner() {
    var box = $('#report-banner');
    if (!box) return;
    fetchJSON('data/report-latest.json')
      .then(function (r) {
        var notes = (r.sections || []).filter(function (s) {
          return s.type === 'notes';
        })[0];
        var n = notes ? (notes.items || []).length : 0;
        box.href = 'history.html#' + r.date + '/' + r.slot;
        box.innerHTML =
          '<span class="rp-banner-tag">最新報告</span>' +
          '<span class="rp-banner-title">' + esc(r.title || '') + '</span>' +
          '<span class="rp-banner-meta">' + esc(r.date) + ' ' +
            esc((r.generatedAtText || '').slice(11)) +
            (n ? '　' + n + ' 則觀察' : '') + '</span>' +
          '<span class="rp-banner-go">看報告 →</span>';
        box.hidden = false;
      })
      .catch(function () { /* 還沒有報告就安靜地不顯示 */ });
  }

  function boot() {
    loadReportBanner();
    fetchJSON('data/latest.json')
      .then(function (latest) {
        render(latest);
        loadHistories(latest);
      })
      .catch(function (e) {
        $('#dashboard').innerHTML =
          '<div class="fatal">讀不到行情資料（data/latest.json）：' + esc(e.message) +
          '<br><br>如果你是剛部署好，請先到 GitHub 的 Actions 頁手動執行一次 ' +
          '「更新市場資料」。</div>';
        var bar = $('#status-bar');
        if (bar) bar.innerHTML = '<span class="chip bad">⚠ 資料檔讀取失敗</span>';
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
