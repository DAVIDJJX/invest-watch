/*
 * fxplan.js — 換匯助手的私人設定（分析系列 A1-6）
 *
 * 設定檔 fx-plan.json 只放在使用者自己的【私人】倉庫；這裡在瀏覽器裡讀進來算本月試算與累計，
 * 只顯示、不上傳、不寫進任何公開檔。設定檔的鍵名只准出現在這一個檔案（守門測試會掃）；
 * 表單欄位的 id 用 fxp_ 開頭，不用鍵名；這一支永遠不 console.log 任何值。
 *
 * 規則：本月額度＝「比例 × 月預算」與「保底」取較大者，兩個數字都印出來。
 *   比例有兩種做法，哪一種當預設由歷史模擬的裁決決定（fx.json 的 backtest.decision）：
 *     A 固定分批＝每個月 100%；B 依規則表＝5 年百分位對到的那一檔。兩種都算、都印，預設的那一種拿去跟保底比。
 *   保底只在同時設了目標總額與期限時才有：（目標總額 − 已換人民幣）÷ 剩餘月數，再用現在的即期賣出換成台幣。
 *   已換人民幣＝每一筆的台幣 ÷ 當時的匯率，加總；所以每一筆都要有匯率。
 *   本月的保底以月初為準（只扣本月以前已換的）；本月已經換的，從算出來的本月額度裡扣——同一筆不會被算兩次。
 *
 * 純函式（compute、validate、fromForm…）不碰 DOM、不 fetch。測試：scripts/test_fxplan.html ＋ scripts/test_fxplan_js.py。
 */
(function (global) {
  'use strict';

  var FILE = 'fx-plan.json';
  var MONTH = /^\d{4}-(0[1-9]|1[0-2])$/;

  function toNum(x) {
    if (typeof x === 'number') return x;
    if (x === null || x === undefined) return NaN;
    var s = String(x).replace(/,/g, '').trim();
    if (s === '') return NaN;
    return Number(s);
  }
  function blank(x) { return x === null || x === undefined || String(x).trim() === ''; }

  function emptyTemplate() {
    return { version: 1, currency: 'CNY', monthlyBudgetTwd: 0, targetCny: null, deadlineMonth: null, converted: [] };
  }

  /* 表單 ↔ 設定檔。表單只給 {budget, target, deadline, records:[{month, twd, rate}]}，鍵名在這裡才組出來。 */
  function fromForm(v) {
    v = v || {};
    return {
      version: 1, currency: 'CNY',
      monthlyBudgetTwd: blank(v.budget) ? NaN : toNum(v.budget),
      targetCny: blank(v.target) ? null : toNum(v.target),
      deadlineMonth: blank(v.deadline) ? null : String(v.deadline).trim(),
      converted: (v.records || []).map(function (r) {
        return { month: String(r.month || '').trim(), twd: toNum(r.twd), rate: toNum(r.rate) };
      })
    };
  }
  function toFormValues(plan) {
    var p = plan || {};
    return {
      budget: (p.monthlyBudgetTwd === undefined || p.monthlyBudgetTwd === null || isNaN(p.monthlyBudgetTwd)) ? '' : p.monthlyBudgetTwd,
      target: (p.targetCny === undefined || p.targetCny === null) ? '' : p.targetCny,
      deadline: p.deadlineMonth || '',
      records: (p.converted || []).map(function (r) { return { month: r.month, twd: r.twd, rate: r.rate }; })
    };
  }

  function validate(plan) {
    var p = plan || {}, errors = [], warnings = [];
    var budget = p.monthlyBudgetTwd;
    if (typeof budget !== 'number' || isNaN(budget)) errors.push('月預算要填數字');
    else if (budget < 0) errors.push('月預算不能是負數');
    var target = p.targetCny;
    if (target !== null && target !== undefined) {
      if (typeof target !== 'number' || isNaN(target)) errors.push('目標總額要是數字，或留白');
      else if (target <= 0) errors.push('目標總額要大於 0，或留白');
    }
    var deadline = p.deadlineMonth;
    if (deadline && !MONTH.test(deadline)) errors.push('期限的格式是 YYYY-MM，例如 2027-06');
    var hasTarget = typeof target === 'number' && !isNaN(target) && target > 0;
    if (deadline && !hasTarget) warnings.push('有期限但沒有目標總額：不算保底');
    if (hasTarget && !deadline) warnings.push('有目標總額但沒有期限：不算保底');
    (p.converted || []).forEach(function (r, i) {
      var tag = '第 ' + (i + 1) + ' 筆紀錄';
      if (!MONTH.test(String(r.month || ''))) errors.push(tag + '：月份的格式是 YYYY-MM');
      if (typeof r.twd !== 'number' || isNaN(r.twd) || r.twd <= 0) errors.push(tag + '：台幣金額要大於 0');
      if (typeof r.rate !== 'number' || isNaN(r.rate) || r.rate <= 0) errors.push(tag + '：匯率必填而且要大於 0');
    });
    return { ok: errors.length === 0, errors: errors, warnings: warnings, hasFloor: !!(hasTarget && deadline && MONTH.test(deadline)) };
  }

  /* 從 from 到 to 還有幾個月（兩頭都算）；期限已過回 0 */
  function monthsBetween(from, to) {
    if (!MONTH.test(String(from)) || !MONTH.test(String(to))) return null;
    var a = from.split('-'), b = to.split('-');
    var nMonths = (Number(b[0]) - Number(a[0])) * 12 + (Number(b[1]) - Number(a[1])) + 1;
    return nMonths < 1 ? 0 : nMonths;
  }

  /*
   * cny：fx.json 的 currencies.CNY（公開的市場資料）；nowMonth：'YYYY-MM'；
   * method：'A' 固定分批（預設）或 'B' 依規則表——由歷史模擬的裁決決定，不是使用者的設定。
   * 回傳的金額只拿來顯示在使用者自己的瀏覽器裡。
   */
  function compute(plan, cny, nowMonth, method) {
    var v = validate(plan);
    if (!v.ok) return { ok: false, errors: v.errors, warnings: v.warnings };
    if (!MONTH.test(String(nowMonth || ''))) return { ok: false, errors: ['不知道現在是哪個月，沒有算'], warnings: v.warnings };
    method = method === 'B' ? 'B' : 'A';
    var p = plan, c = cny || {};
    var spot = c.latest && c.latest.spotSell;
    var rule = c.rule && typeof c.rule.ratioPct === 'number' ? c.rule : null;
    var records = (p.converted || []).slice().sort(function (x, y) { return x.month < y.month ? -1 : (x.month > y.month ? 1 : 0); });
    var doneTwd = 0, doneCny = 0, thisMonthTwd = 0, beforeCny = 0;
    records.forEach(function (r) {
      doneTwd += r.twd;
      doneCny += r.twd / r.rate;
      if (r.month === nowMonth) thisMonthTwd += r.twd;
      if (r.month < nowMonth) beforeCny += r.twd / r.rate;
    });
    var out = {
      ok: true, warnings: v.warnings, month: nowMonth, budgetTwd: p.monthlyBudgetTwd,
      records: records.map(function (r) { return { month: r.month, twd: r.twd, rate: r.rate, cny: r.twd / r.rate }; }),
      doneTwd: doneTwd, doneCny: doneCny, averageRate: doneCny > 0 ? doneTwd / doneCny : null, thisMonthTwd: thisMonthTwd,
      method: method, fixed: { ratioPct: 100, twd: p.monthlyBudgetTwd },
      rule: null, floor: null, methodTwd: null, quotaTwd: null, quotaFrom: null, leftTwd: null
    };
    if (rule) {
      out.rule = { percentile5y: rule.percentile5y, bucket: rule.bucket, ratioPct: rule.ratioPct, twd: p.monthlyBudgetTwd * rule.ratioPct / 100 };
    }
    if (v.hasFloor) {
      var left = monthsBetween(nowMonth, p.deadlineMonth);
      var remaining = Math.max(0, p.targetCny - doneCny);          // 現在還差多少：目標總額 − 已換人民幣（全部紀錄）
      // 本月的保底以月初為準：本月已經換的不先扣，算出本月額度之後再從額度裡扣（leftTwd），同一筆才不會被算兩次
      var atMonthStart = Math.max(0, p.targetCny - beforeCny);
      if (left === null) {
        out.floor = { reason: '月份格式不對，算不出剩餘月數' };
      } else if (left === 0) {
        out.floor = { reason: '期限已過', goalCny: p.targetCny, remainingCny: remaining, deadline: p.deadlineMonth };
      } else if (typeof spot !== 'number') {
        out.floor = { reason: '資料不足：沒有現在的即期賣出價，保底換不成台幣', goalCny: p.targetCny, remainingCny: remaining,
                      monthsLeft: left, deadline: p.deadlineMonth };
      } else {
        out.floor = { goalCny: p.targetCny, remainingCny: remaining, atMonthStartCny: atMonthStart, monthsLeft: left, deadline: p.deadlineMonth,
                      cny: atMonthStart / left, twd: atMonthStart / left * spot, spotSell: spot, spotDate: c.latest.d };
      }
    }
    // 預設做法的那個數字拿去跟保底比，取較大者；規則表沒資料（fx.json 讀不到）時 B 算不出來，照實留空
    var methodTwd = method === 'B' ? (out.rule ? out.rule.twd : null) : out.fixed.twd;
    var floorTwd = out.floor && typeof out.floor.twd === 'number' ? out.floor.twd : null;
    out.methodTwd = methodTwd;
    if (methodTwd !== null && floorTwd !== null) {
      out.quotaTwd = Math.max(methodTwd, floorTwd);
      out.quotaFrom = floorTwd > methodTwd ? 'floor' : 'method';
    } else if (methodTwd !== null) {
      out.quotaTwd = methodTwd;
      out.quotaFrom = 'method';
    } else if (floorTwd !== null) {
      out.quotaTwd = floorTwd;
      out.quotaFrom = 'floor';
    }
    if (out.quotaTwd !== null) out.leftTwd = Math.max(0, out.quotaTwd - thisMonthTwd);
    return out;
  }

  global.FxPlan = {
    FILE: FILE, emptyTemplate: emptyTemplate, fromForm: fromForm, toFormValues: toFormValues,
    validate: validate, monthsBetween: monthsBetween, compute: compute
  };
})(window);
