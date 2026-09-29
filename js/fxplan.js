/*
 * fxplan.js — 換匯助手的私人設定（分析系列 A1-6）
 *
 * 設定檔 fx-plan.json 只放在使用者自己的【私人】倉庫；這裡在瀏覽器裡讀進來算本月試算與累計，
 * 只顯示、不上傳、不寫進任何公開檔。設定檔的鍵名只准出現在這一個檔案（守門測試會掃）；
 * 表單欄位的 id 用 fxp_ 開頭，不用鍵名；這一支永遠不 console.log 任何值。
 *
 * 模型：預算池（跟 scripts/analyze.py 的歷史模擬同一套）。
 *   預算池＝從計畫起始月到現在累積的預算 − 這段期間已經換掉的台幣。每個月把月預算放進池子；沒換的錢留在池子裡，
 *     之後便宜時可以一次多換。計畫起始月以前的紀錄不從池子扣（那時候池子還沒開始），但算進已換人民幣。
 *   規則額度＝規則比例 × 池子（比例是 5 年百分位對到規則表的那一檔）。
 *   保底（有設期限才有）＝池子 ÷ 剩餘月數；同時有設目標總額時，再跟（目標總額 − 已換人民幣）× 現在的即期賣出 ÷ 剩餘月數 比，取較大者。
 *   本月額度＝max(預設做法的額度, 保底)，但不得超過池子——手上沒有的錢不能換。
 *     預設做法由歷史模擬的裁決決定（fx.json 的 backtest.decision）：A 固定分批＝一個月預算；B 依位置調整＝規則額度。兩種都算、都印。
 *   本月的額度用「月初的池子」算（這個月的預算已經放進去、這個月換掉的還沒扣）；這個月已經換的，再從算出來的額度裡扣。
 *     這樣換完本月額度之後再回來看，頁面會說「本月還沒換的額度 0」，不會叫你把剩下的池子再乘一次比例。
 *   已換人民幣＝每一筆的台幣 ÷ 當時的匯率，加總；所以每一筆都要有匯率。
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
    return { version: 1, currency: 'CNY', startMonth: null, monthlyBudgetTwd: 0, targetCny: null, deadlineMonth: null, converted: [] };
  }

  /* 表單 ↔ 設定檔。表單只給 {start, budget, target, deadline, records:[{month, twd, rate}]}，鍵名在這裡才組出來。 */
  function fromForm(v) {
    v = v || {};
    return {
      version: 1, currency: 'CNY',
      startMonth: blank(v.start) ? null : String(v.start).trim(),
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
      start: p.startMonth || '',
      budget: (p.monthlyBudgetTwd === undefined || p.monthlyBudgetTwd === null || isNaN(p.monthlyBudgetTwd)) ? '' : p.monthlyBudgetTwd,
      target: (p.targetCny === undefined || p.targetCny === null) ? '' : p.targetCny,
      deadline: p.deadlineMonth || '',
      records: (p.converted || []).map(function (r) { return { month: r.month, twd: r.twd, rate: r.rate }; })
    };
  }

  function validate(plan) {
    var p = plan || {}, errors = [], warnings = [];
    var start = p.startMonth;
    var startOk = !!start && MONTH.test(String(start));
    if (!start) errors.push('計畫起始月必填：預算池從那個月開始累積（格式 YYYY-MM，例如 2026-10）');
    else if (!startOk) errors.push('計畫起始月的格式是 YYYY-MM，例如 2026-10');
    var budget = p.monthlyBudgetTwd;
    if (typeof budget !== 'number' || isNaN(budget)) errors.push('月預算要填數字');
    else if (budget < 0) errors.push('月預算不能是負數');
    var target = p.targetCny;
    if (target !== null && target !== undefined) {
      if (typeof target !== 'number' || isNaN(target)) errors.push('目標總額要是數字，或留白');
      else if (target <= 0) errors.push('目標總額要大於 0，或留白');
    }
    var deadline = p.deadlineMonth;
    var deadlineOk = !!deadline && MONTH.test(String(deadline));
    if (deadline && !deadlineOk) errors.push('期限的格式是 YYYY-MM，例如 2027-06');
    if (deadlineOk && startOk && deadline < start) errors.push('期限不能早於計畫起始月');
    var hasTarget = typeof target === 'number' && !isNaN(target) && target > 0;
    if (hasTarget && !deadline) warnings.push('有目標總額但沒有期限：不算保底');
    (p.converted || []).forEach(function (r, i) {
      var tag = '第 ' + (i + 1) + ' 筆紀錄';
      var monthOk = MONTH.test(String(r.month || ''));
      if (!monthOk) errors.push(tag + '：月份的格式是 YYYY-MM');
      if (typeof r.twd !== 'number' || isNaN(r.twd) || r.twd <= 0) errors.push(tag + '：台幣金額要大於 0');
      if (typeof r.rate !== 'number' || isNaN(r.rate) || r.rate <= 0) errors.push(tag + '：匯率必填而且要大於 0');
      if (monthOk && startOk && r.month < start) warnings.push(tag + '在計畫起始月之前：算進已換人民幣，但不從預算池扣');
    });
    return { ok: errors.length === 0, errors: errors, warnings: warnings, hasFloor: deadlineOk, hasTarget: hasTarget };
  }

  /* 從 from 到 to 總共幾個月（兩頭都算）；to 比 from 早就回 0 */
  function monthsBetween(from, to) {
    if (!MONTH.test(String(from)) || !MONTH.test(String(to))) return null;
    var a = from.split('-'), b = to.split('-');
    var nMonths = (Number(b[0]) - Number(a[0])) * 12 + (Number(b[1]) - Number(a[1])) + 1;
    return nMonths < 1 ? 0 : nMonths;
  }

  /* 本月額度＝max(想換的, 保底)，但不得超過池子；池子是空的就是 0 */
  function capToPool(wanted, floor, pool) {
    if (!(pool > 0)) return { twd: 0, capped: Math.max(wanted || 0, floor || 0) > 0, uncapped: Math.max(wanted || 0, floor || 0) };
    var uncapped = Math.max(wanted || 0, floor || 0, 0);
    return { twd: Math.min(uncapped, pool), capped: uncapped > pool + 1e-9, uncapped: uncapped };
  }

  /*
   * cny：fx.json 的 currencies.CNY（公開的市場資料）；nowMonth：'YYYY-MM'；
   * method：'A' 固定分批（預設）或 'B' 依位置調整——由歷史模擬的裁決決定，不是使用者的設定。
   * 回傳的金額只拿來顯示在使用者自己的瀏覽器裡。回傳物件的欄位名稱刻意不沿用設定檔的鍵名。
   */
  function compute(plan, cny, nowMonth, method) {
    var v = validate(plan);
    if (!v.ok) return { ok: false, errors: v.errors, warnings: v.warnings };
    if (!MONTH.test(String(nowMonth || ''))) return { ok: false, errors: ['不知道現在是哪個月，沒有算'], warnings: v.warnings };
    method = method === 'B' ? 'B' : 'A';
    var p = plan, c = cny || {};
    var budget = p.monthlyBudgetTwd, since = p.startMonth;
    var spot = c.latest && c.latest.spotSell;
    var rule = c.rule && typeof c.rule.ratioPct === 'number' ? c.rule : null;
    var records = (p.converted || []).slice().sort(function (x, y) { return x.month < y.month ? -1 : (x.month > y.month ? 1 : 0); });
    var doneTwd = 0, doneCny = 0, thisMonthTwd = 0, beforeCny = 0, spentBefore = 0, spentThisMonth = 0, outsideTwd = 0;
    records.forEach(function (r) {
      doneTwd += r.twd;
      doneCny += r.twd / r.rate;
      if (r.month === nowMonth) thisMonthTwd += r.twd;
      if (r.month < nowMonth) beforeCny += r.twd / r.rate;
      if (r.month < since) { outsideTwd += r.twd; return; }                 // 計畫開始以前換的：不從池子扣
      if (r.month < nowMonth) spentBefore += r.twd;
      else spentThisMonth += r.twd;                                          // 這個月（或日期寫到未來）的
    });
    var monthsIn = monthsBetween(since, nowMonth);                           // 計畫走到第幾個月；還沒開始是 0
    var soFar = budget * monthsIn;
    var atStart = Math.max(0, soFar - spentBefore);                          // 月初的池子：這個月的預算已經放進去、這個月換的還沒扣
    var poolNow = Math.max(0, soFar - spentBefore - spentThisMonth);
    var out = {
      ok: true, warnings: v.warnings.slice(), month: nowMonth, budgetTwd: budget, since: since, monthsIn: monthsIn,
      pool: { soFarTwd: soFar, spentBeforeTwd: spentBefore, atMonthStartTwd: atStart, spentThisMonthTwd: spentThisMonth, nowTwd: poolNow,
              overspentTwd: Math.max(0, spentBefore + spentThisMonth - soFar), outsideTwd: outsideTwd, notStarted: monthsIn === 0 },
      records: records.map(function (r) { return { month: r.month, twd: r.twd, rate: r.rate, cny: r.twd / r.rate }; }),
      doneTwd: doneTwd, doneCny: doneCny, averageRate: doneCny > 0 ? doneTwd / doneCny : null, thisMonthTwd: thisMonthTwd,
      goal: v.hasTarget ? { cny: p.targetCny, remainingCny: Math.max(0, p.targetCny - doneCny) } : null,
      method: method, fixed: { twd: Math.min(budget, atStart) },
      rule: null, floor: null, methodTwd: null, quotaTwd: null, quotaFrom: null, cappedByPool: false, uncappedTwd: null, leftTwd: null
    };
    if (monthsIn === 0) out.warnings.push('計畫起始月 ' + since + ' 還沒到：預算池是空的');
    if (out.pool.overspentTwd > 0) out.warnings.push('計畫期間已經換的比累積的預算多：預算池是空的');
    if (rule) {
      out.rule = { percentile5y: rule.percentile5y, bucket: rule.bucket, ratioPct: rule.ratioPct, twd: atStart * rule.ratioPct / 100 };
    }
    if (v.hasFloor) {
      var left = monthsBetween(nowMonth, p.deadlineMonth);
      if (left === 0) {
        out.floor = { reason: '期限已過', deadline: p.deadlineMonth };
      } else {
        var f = { monthsLeft: left, deadline: p.deadlineMonth, poolPartTwd: atStart / left, goalPartTwd: null, goalNote: null };
        if (v.hasTarget) {
          f.goalCny = p.targetCny;
          f.remainingCny = Math.max(0, p.targetCny - doneCny);              // 現在還差多少：目標總額 − 已換人民幣（全部紀錄）
          f.atMonthStartCny = Math.max(0, p.targetCny - beforeCny);         // 月初還差多少：這個月換的還沒扣
          if (typeof spot === 'number' && spot > 0) {
            f.goalPartTwd = f.atMonthStartCny * spot / left;
            f.spotSell = spot;
            f.spotDate = c.latest.d;
          } else {
            f.goalNote = '資料不足：沒有現在的即期賣出價，目標那一半換不成台幣，保底只看池子';
          }
        }
        f.from = (f.goalPartTwd !== null && f.goalPartTwd > f.poolPartTwd) ? 'goal' : 'pool';
        f.twd = f.from === 'goal' ? f.goalPartTwd : f.poolPartTwd;
        out.floor = f;
      }
    }
    // 預設做法的那個數字拿去跟保底比，取較大者，再用池子封頂；規則表沒資料（fx.json 讀不到）時 B 算不出來，照實留空
    var methodTwd = method === 'B' ? (out.rule ? out.rule.twd : null) : out.fixed.twd;
    var floorTwd = out.floor && typeof out.floor.twd === 'number' ? out.floor.twd : null;
    out.methodTwd = methodTwd;
    if (methodTwd !== null || floorTwd !== null) {
      var q = capToPool(methodTwd, floorTwd, atStart);
      out.quotaTwd = q.twd;
      out.cappedByPool = q.capped;
      out.uncappedTwd = q.uncapped;
      out.quotaFrom = (floorTwd !== null && (methodTwd === null || floorTwd > methodTwd)) ? 'floor' : 'method';
      out.leftTwd = Math.max(0, out.quotaTwd - spentThisMonth);
    }
    return out;
  }

  global.FxPlan = {
    FILE: FILE, emptyTemplate: emptyTemplate, fromForm: fromForm, toFormValues: toFormValues,
    validate: validate, monthsBetween: monthsBetween, capToPool: capToPool, compute: compute
  };
})(window);
