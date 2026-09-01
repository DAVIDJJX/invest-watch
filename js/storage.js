/*
 * storage.js — 個人紀錄的存放層（雙後端，介面完全相同）
 *
 *   local  ：存在這台裝置的瀏覽器裡（localStorage）。不設定任何金鑰也完全可用。
 *   github ：另外同步到你自己的**私人**倉庫 invest-data 的 portfolio.json，
 *            這樣手機和電腦才看得到同一份資料。
 *
 * 隱私原則（PLAN.md 第 3 章）：
 *   * 個人持倉、成本、金額**只會**出現在這台裝置的瀏覽器，以及你的私人倉庫。
 *     公開倉庫 invest-watch 裡永遠不會有這些數字。
 *   * 同步金鑰（PAT）只存在這台裝置的 localStorage，不會被送到任何第三方，
 *     只會用來呼叫 api.github.com。畫面上一律遮罩顯示，隨時可以清除。
 *   * 開了密碼鎖（js/lock.js）之後，寫進 localStorage 的資料與金鑰都是**加密**的，
 *     沒有密碼連開發者工具也讀不出來。
 *
 * GitHub Contents API 的寫入規則：更新既有檔案必須帶正確的 sha，
 * 所以每次寫入前都先讀一次最新的 sha，避免手機和電腦互相蓋掉。
 *
 * 用法：頁面載入時先 await Storage.init()，之後 getPat() 等同步函式才拿得到值。
 */
(function (global) {
  'use strict';

  var LS_DATA = 'iw_portfolio';
  var LS_PAT = 'iw_pat';
  var LS_REPO = 'iw_repo';
  var LS_MODE = 'iw_mode';
  var FILE = 'portfolio.json';
  var DEFAULT_REPO = 'DAVIDJJX/invest-data';

  // 解密後的金鑰放記憶體，讓 getPat() 這種同步呼叫還能用
  var patCache = null;
  var ready = false;

  /* ---------------------------------------------------------- 小工具 */

  function lsGet(k, dflt) {
    try { var v = localStorage.getItem(k); return v === null ? dflt : v; }
    catch (e) { return dflt; }
  }
  function lsSet(k, v) {
    try { localStorage.setItem(k, v); return true; } catch (e) { return false; }
  }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) { } }

  function b64encode(str) {
    var bytes = new TextEncoder().encode(str);
    var bin = '';
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }
  function b64decode(b64) {
    var bin = atob(String(b64).replace(/\s/g, ''));
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder('utf-8').decode(bytes);
  }

  /* 有開密碼鎖就加密／解密，沒開就原樣進出 */
  function protect(text) {
    return global.Lock ? global.Lock.protect(text) : Promise.resolve(text);
  }
  function unprotect(text) {
    return global.Lock ? global.Lock.unprotect(text) : Promise.resolve(text);
  }

  /* ---------------------------------------------------------- 啟動 */

  /*
   * 頁面載入時呼叫一次：接回這個分頁先前的解鎖狀態，並把金鑰解密到記憶體。
   * 還鎖著的話 patCache 會是 null，等使用者解鎖後再呼叫一次即可。
   */
  function init() {
    var step = global.Lock ? global.Lock.resume() : Promise.resolve(true);
    return step.then(function () {
      var raw = lsGet(LS_PAT, '');
      if (!raw) { patCache = ''; ready = true; return; }
      return unprotect(raw)
        .then(function (p) { patCache = p || ''; })
        .catch(function () { patCache = null; })   // 還鎖著
        .then(function () { ready = true; });
    });
  }

  /* ---------------------------------------------------------- 設定 */

  function getMode() {
    var m = lsGet(LS_MODE, 'local');
    return (m === 'github' && getPat()) ? 'github' : 'local';
  }
  function setMode(m) { lsSet(LS_MODE, m === 'github' ? 'github' : 'local'); }

  function getPat() { return patCache || ''; }

  function setPat(p) {
    var v = String(p || '').trim();
    if (!v) { clearPat(); return Promise.resolve(); }
    patCache = v;
    return protect(v).then(function (stored) { lsSet(LS_PAT, stored); });
  }

  function clearPat() { patCache = ''; lsDel(LS_PAT); setMode('local'); }
  function hasPat() { return !!getPat(); }

  /* 只給人看的遮罩，例如 github_pat_11AB…q7Xz（永遠不顯示完整金鑰） */
  function maskedPat() {
    var p = getPat();
    if (!p) return '';
    if (p.length <= 14) return p.slice(0, 4) + '…';
    return p.slice(0, 11) + '…' + p.slice(-4);
  }

  function getRepo() { return lsGet(LS_REPO, DEFAULT_REPO) || DEFAULT_REPO; }
  function setRepo(r) {
    var v = String(r || '').trim().replace(/^https?:\/\/github\.com\//i, '')
              .replace(/\.git$/i, '').replace(/\/+$/, '');
    lsSet(LS_REPO, v || DEFAULT_REPO);
  }

  /* ---------------------------------------------------------- 本機後端 */

  function localLoad() {
    var raw = lsGet(LS_DATA, '');
    if (!raw) return Promise.resolve(null);
    return unprotect(raw)
      .then(function (text) {
        try { return JSON.parse(text); } catch (e) { return null; }
      })
      .catch(function (e) { throw e; });   // 鎖著就往上丟，由畫面提示解鎖
  }

  function localSave(data) {
    return protect(JSON.stringify(data)).then(function (stored) {
      if (!lsSet(LS_DATA, stored)) {
        throw new Error('這台裝置的瀏覽器不允許儲存資料（可能是無痕模式或空間已滿）');
      }
    });
  }

  /* 有沒有資料存在這台裝置（不需要解密就看得出來） */
  function hasLocalData() { return !!lsGet(LS_DATA, ''); }

  /* ---------------------------------------------------------- GitHub 後端 */

  function apiUrl(path) {
    return 'https://api.github.com/repos/' + getRepo() + '/contents/' + path;
  }

  function ghHeaders() {
    return {
      'Authorization': 'Bearer ' + getPat(),
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    };
  }

  function ghError(res, body) {
    var msg = (body && body.message) || ('HTTP ' + res.status);
    if (res.status === 401) {
      return new Error('同步金鑰無效或已過期（401）。請到設定頁重新貼一次金鑰。');
    }
    if (res.status === 403) {
      return new Error('金鑰權限不足（403）。請確認它對 ' + getRepo() +
                       ' 有 Contents 的「Read and write」權限。');
    }
    if (res.status === 404) {
      return new Error('找不到倉庫 ' + getRepo() +
                       '（404）。請確認名稱正確，而且金鑰有勾選這個倉庫。');
    }
    if (res.status === 409) {
      return new Error('這個檔案在別的裝置剛被改過（409），請再按一次同步。');
    }
    return new Error('GitHub 回應錯誤：' + msg);
  }

  function ghLoad() {
    return fetch(apiUrl(FILE) + '?t=' + Date.now(), { headers: ghHeaders() })
      .then(function (res) {
        if (res.status === 404) return { data: null, sha: null };
        return res.json().then(function (body) {
          if (!res.ok) throw ghError(res, body);
          var text = b64decode(body.content || '');
          var data;
          try { data = JSON.parse(text); }
          catch (e) { throw new Error('私人倉庫裡的 portfolio.json 內容不是合法 JSON'); }
          return { data: data, sha: body.sha };
        });
      });
  }

  /*
   * 寫回私人倉庫。寫入前一定先讀最新的 sha——
   * 手機和電腦如果同時在用，這一步才不會互相蓋掉。
   */
  function ghSave(data) {
    return ghLoad().then(function (cur) {
      var body = {
        message: 'portfolio 更新（' +
                 new Date().toISOString().slice(0, 16).replace('T', ' ') + '）',
        content: b64encode(JSON.stringify(data, null, 1))
      };
      if (cur.sha) body.sha = cur.sha;
      return fetch(apiUrl(FILE), {
        method: 'PUT',
        headers: Object.assign({ 'Content-Type': 'application/json' }, ghHeaders()),
        body: JSON.stringify(body)
      }).then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (b) {
          if (!res.ok) throw ghError(res, b);
          return true;
        });
      });
    });
  }

  function testConnection() {
    if (!hasPat()) return Promise.reject(new Error('還沒有貼上同步金鑰'));
    return fetch('https://api.github.com/repos/' + getRepo(), { headers: ghHeaders() })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (b) {
          if (!res.ok) throw ghError(res, b);
          if (!b.private) {
            throw new Error('⚠ ' + getRepo() + ' 是「公開」倉庫！個人資料不可以放在公開倉庫，' +
                            '請把它改成 Private 之後再同步。');
          }
          return {
            repo: b.full_name,
            private: true,
            canWrite: !!(b.permissions && b.permissions.push)
          };
        });
      });
  }

  /* ---------------------------------------------------------- 對外介面 */

  function load() {
    return localLoad().then(function (localData) {
      if (getMode() !== 'github') {
        return { data: localData, source: 'local', warning: null };
      }
      return ghLoad()
        .then(function (r) {
          if (r.data) {
            return localSave(r.data)
              .catch(function () { })
              .then(function () {
                return { data: r.data, source: 'github', warning: null };
              });
          }
          return { data: localData, source: 'local',
                   warning: '私人倉庫還沒有 portfolio.json，第一次儲存時會自動幫你建立。' };
        })
        .catch(function (e) {
          return { data: localData, source: 'local',
                   warning: '雲端同步讀取失敗，先顯示這台裝置上的資料：' + e.message };
        });
    });
  }

  function save(data) {
    data.updatedAt = new Date().toISOString();
    return localSave(data).then(function () {
      if (getMode() !== 'github') {
        return { synced: false, warning: null };
      }
      return ghSave(data)
        .then(function () { return { synced: true, warning: null }; })
        .catch(function (e) {
          return { synced: false,
                   warning: '已存在這台裝置，但雲端同步失敗：' + e.message };
        });
    });
  }

  /* 啟用／關閉密碼鎖之後，要把已存的資料與金鑰用新狀態重寫一次 */
  function rewriteStored() {
    var jobs = [];
    var pat = getPat();
    if (pat) jobs.push(protect(pat).then(function (s) { lsSet(LS_PAT, s); }));
    var raw = lsGet(LS_DATA, '');
    if (raw) {
      // 這時 raw 可能還是舊狀態（明文或舊密文），先用目前的鎖狀態解出來再寫回去
      jobs.push(Promise.resolve(raw).then(function (r) {
        if (global.Lock && global.Lock.looksEncrypted(r)) return unprotect(r);
        return r;
      }).then(function (text) {
        return protect(text).then(function (s) { lsSet(LS_DATA, s); });
      }));
    }
    return Promise.all(jobs);
  }

  /* ---------------------------------------------------------- 備份 */

  function exportJSON(data) { return JSON.stringify(data, null, 1); }

  function importJSON(text) {
    var d = JSON.parse(text);
    if (!d || typeof d !== 'object') throw new Error('檔案內容不是一份備份資料');
    if (!Array.isArray(d.assets) || !Array.isArray(d.trades)) {
      throw new Error('這個檔案看起來不是 InvestWatch 的備份（缺少 assets 或 trades）');
    }
    return d;
  }

  global.Storage = {
    init: init, isReady: function () { return ready; },
    getMode: getMode, setMode: setMode,
    getPat: getPat, setPat: setPat, clearPat: clearPat,
    hasPat: hasPat, maskedPat: maskedPat,
    getRepo: getRepo, setRepo: setRepo, DEFAULT_REPO: DEFAULT_REPO,
    load: load, save: save,
    testConnection: testConnection,
    exportJSON: exportJSON, importJSON: importJSON,
    hasLocalData: hasLocalData,
    rewriteStored: rewriteStored,
    _localLoad: localLoad, _localSave: localSave,
    wipeLocal: function () { lsDel(LS_DATA); }
  };
})(window);
