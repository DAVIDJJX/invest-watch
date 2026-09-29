/*
 * glossary.js — 全站共用的名詞解釋（分析系列 A1-6 起）
 *
 * 名詞點一下就在旁邊展開一句話的定義，再點一下收起來。不靠滑鼠懸停——手機上也能用。
 * 用法：Glossary.term('百分位') 回一個按鈕的 HTML；畫完之後 Glossary.wire(容器) 掛上點擊。
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
    '預算池': '每個月把月預算放進去、換掉多少就拿出多少的一個帳；沒換的錢留在裡面，累積到之後的月份。'
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
      btn.addEventListener('click', function () { toggle(btn); });
    });
  }

  global.Glossary = { TERMS: TERMS, define: define, term: term, toggle: toggle, wire: wire };
})(window);
