/*
 * glossary.js — 全站共用的名詞解釋（分析系列 A1-6 起；A1-7 推到卡片的指標格與分析分頁的表頭）
 *
 * 名詞點一下就在旁邊展開一句話的定義，再點一下收起來。不靠滑鼠懸停——手機上也能用。
 * 用法：Glossary.term('百分位') 回一個按鈕的 HTML；畫完之後 Glossary.wire(容器) 掛上點擊。
 * 儀表板是首屏畫完之後才載入這一支：先畫的地方印同樣的字、留一個記號（data-term-pending="名詞"），
 * 載入之後呼叫 Glossary.upgrade(容器) 把它們換成可以點的按鈕。
 * 測試：scripts/test_plain.html。
 */
(function (global) {
  'use strict';

  var TERMS = {
    '百分位': '把一段時間的價格由低到高排隊，現在排在第幾 %。90% 表示這段時間裡有九成的時候比現在低。',
    '波動': '價格上上下下的幅度。年化波動 20% 大致表示一年裡漲跌兩成左右算平常。',
    '回檔': '從最高點往下跌了多少。最大回檔是歷史上跌得最深的那一次。',
    '相關係數': '兩個標的一起漲跌的程度，從 −1 到 1。接近 1 是常常同方向，接近 0 是各走各的，負的是常常反方向。',
    '追蹤差': 'ETF 的報酬跟它追蹤的指數差多少。差距來自費用、換匯的時點與操作方式。',
    '折溢價': 'ETF 的成交價比它的淨值貴（溢價）或便宜（折價）多少。',
    '即期': '用帳戶裡的錢換匯、不拿實體鈔票時，銀行掛的價格。',
    '現鈔': '拿實體鈔票換匯時，銀行掛的價格；通常比即期貴，因為銀行要保管與運送鈔票。',
    '價差': '銀行賣給你的價格，跟它向你買回去的價格之間的差，就是來回一趟的成本。',
    '中位數': '把數字由小到大排好，排在正中間的那一個；比平均不容易被少數極端值拉走。',
    '歷史模擬': '拿過去的資料照規則重演一遍算出來的結果，不是實際發生過的交易。',
    '預算池': '每個月把月預算放進去、換掉多少就拿出多少的一個帳；沒換的錢留在裡面，累積到之後的月份。',
    // A1-7：呈現用的詞
    '面向': '看一個標的的一個角度，例如位置、風險、成本。每個面向各看各的，這裡不把它們合成一個結論。',
    '狀態詞': '把某個面向的數字照固定規則分成三種說法，例如風險分成平靜、正常、劇烈。規則印在旁邊，狀態詞只是把數字換成好讀的話。',
    '保底': '有設定期限時，每個月至少要換的金額：把還沒換的錢平均分到剩下的月份，免得期限到了還沒換完。',
    '視窗重疊': '拿歷史資料做模擬時，每個期間只往後移一個月，相鄰的期間大部分日子是重複的；所以「贏了幾成」不能當成那麼多次各自獨立的結果。',
    // A1-7：卡片指標格與分析分頁表頭的詞
    'RSI': '把最近 14 天的漲幅和跌幅拿來比，換成 0 到 100 的數字。數字高表示最近漲得多，數字低表示最近跌得多；它只描述最近的漲跌。',
    '移動平均': '最近 N 天收盤價的平均（MA20 是 20 天、MA60 是 60 天）。把每天的上下起伏抹平，方便看價格大致的走向。',
    '近 10 日漲跌': '現在的價格跟 10 個交易日之前的收盤價比，漲或跌了百分之幾。',
    '年化波動': '把每天（或每週）漲跌的幅度換算成一年的尺度。只看最近 30 個交易日的版本，會比看 1 年或 5 年的版本跳動得快。',
    '區間位置': '把一段時間裡的最低價當 0、最高價當 100，現在的價格在哪裡。它只說現在在這段期間的高低位置，不代表便宜或貴。',
    '目前距高點': '現在的價格比這段資料裡的最高點低了多少。',
    '殘差': '實際的數字減掉公式算出來的數字，剩下對不起來的那一塊。',
    '淨值': 'ETF 手上所有資產的價值除以單位數，也就是每一單位本來值多少。',
    '資料標籤': '每個數字旁邊標的可信程度：估算、單一來源、有對照、多來源一致。',
    '條塊溢價': '同樣重量的黃金，實體條塊比黃金存摺貴多少；差額是鑄造與加工的費用。'
  };

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function define(name) { return TERMS[name] || null; }

  /* 有定義的名詞才變成可以點的按鈕；沒有定義就只是普通的字 */
  function term(name, label) {
    if (!TERMS[name]) return esc(label || name);
    return '<button type="button" class="term" data-term="' + esc(name) + '" aria-expanded="false">' + esc(label || name) + '</button>';
  }

  /* 名詞解釋還沒載入時，頁面先印同樣的字、留一個記號（<span data-term-pending="名詞">）；這裡把它們換成按鈕並掛上點擊 */
  function upgrade(root) {
    Array.prototype.forEach.call((root || document).querySelectorAll('[data-term-pending]'), function (el) {
      var holder = document.createElement('span');
      holder.innerHTML = term(el.getAttribute('data-term-pending'), el.textContent);
      el.parentNode.replaceChild(holder.firstChild, el);
    });
    wire(root);
  }

  function toggle(btn) {
    var next = btn.nextElementSibling;
    if (next && next.classList && next.classList.contains('term-def')) {
      next.parentNode.removeChild(next);
      btn.setAttribute('aria-expanded', 'false');
      return false;
    }
    var box = document.createElement('span');
    box.className = 'term-def';
    box.setAttribute('role', 'note');
    box.textContent = btn.getAttribute('data-term') + '：' + (define(btn.getAttribute('data-term')) || '');
    btn.parentNode.insertBefore(box, btn.nextSibling);
    btn.setAttribute('aria-expanded', 'true');
    return true;
  }

  function wire(root) {
    Array.prototype.forEach.call((root || document).querySelectorAll('button.term'), function (btn) {
      if (btn.getAttribute('data-wired')) return;
      btn.setAttribute('data-wired', '1');
      // 名詞可能放在會響應點擊的地方（可排序的表頭、可展開的列）：點名詞只展開解釋，不往外傳
      btn.addEventListener('click', function (ev) { if (ev && ev.stopPropagation) ev.stopPropagation(); toggle(btn); });
    });
  }

  global.Glossary = { TERMS: TERMS, define: define, term: term, toggle: toggle, wire: wire, upgrade: upgrade };
})(window);
