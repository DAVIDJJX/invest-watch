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
 *   * 即使開了 github 同步，資料同時也會留一份在本機——網路斷了照樣看得到。
 *
 * GitHub Contents API 的寫入規則：更新既有檔案必須帶正確的 sha，
 * 所以每次寫入前都先讀一次最新的 sha，避免手機和電腦互相蓋掉。
 */
(function (global) {
  'use strict';

  var LS_DATA = 'iw_portfolio';       // 本機資料
  var LS_PAT = 'iw_pat';              // 同步金鑰
  var LS_REPO = 'iw_repo';            // 私人倉庫（預設 DAVIDJJX/invest-data）
  var LS_MODE = 'iw_mode';            // local | github
  var FILE = 'portfolio.json';
  var DEFAULT_REPO = 'DAVIDJJX/invest-data';

  /* ---------------------------------------------------------- 小工具 */

  function lsGet(k, dflt) {
    try { var v = localStorage.getItem(k); return v === null ? dflt : v; }
    catch (e) { return dflt; }
  }
  function lsSet(k, v) {
    try { localStorage.setItem(k, v); return true; } catch (e) { return false; }
  }
  function lsDel(k) {
    try { localStorage.removeItem(k); } catch (e) { /* 隱私模式下可能不給寫 */ }
  }

  /* UTF-8 安全的 base64（GitHub API 要 base64，中文備註不能直接 btoa） */
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

  /* ---------------------------------------------------------- 設定 */

  function getMode() {
    var m = lsGet(LS_MODE, 'local');
    return (m === 'github' && getPat()) ? 'github' : 'local';
  }
  function setMode(m) { lsSet(LS_MODE, m === 'github' ? 'github' : 'local'); }

  function getPat() { return lsGet(LS_PAT, '') || ''; }
  function setPat(p) {
    var v = String(p || '').trim();
    if (!v) { clearPat(); return; }
    lsSet(LS_PAT, v);
  }
  function clearPat() { lsDel(LS_PAT); setMode('local'); }
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
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (e) { return null; }
  }
  function localSave(data) {
    var ok = lsSet(LS_DATA, JSON.stringify(data));
    if (!ok) {
      throw new Error('這台裝置的瀏覽器不允許儲存資料（可能是無痕模式或空間已滿）');
    }
  }

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

  /* 讀私人倉庫的 portfolio.json。回傳 {data, sha}；檔案不存在回 {data:null, sha:null} */
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
        message: 'portfolio 更新（' + new Date().toISOString().slice(0, 16).replace('T', ' ') + '）',
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

  /* 測試連線：確認倉庫看得到、權限夠 */
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

  /*
   * 讀取資料。github 模式會以雲端為準，並順手在本機留一份備份；
   * 雲端讀不到（沒網路、金鑰過期）就退回本機那份，並回報原因。
   */
  function load() {
    var localData = localLoad();
    if (getMode() !== 'github') {
      return Promise.resolve({ data: localData, source: 'local', warning: null });
    }
    return ghLoad()
      .then(function (r) {
        if (r.data) {
          try { localSave(r.data); } catch (e) { /* 本機存不了不影響讀取 */ }
          return { data: r.data, source: 'github', warning: null };
        }
        // 私人倉庫還沒有這個檔案 → 用本機這份，下次儲存時會自動建立
        return { data: localData, source: 'local',
                 warning: '私人倉庫還沒有 portfolio.json，第一次儲存時會自動幫你建立。' };
      })
      .catch(function (e) {
        return { data: localData, source: 'local',
                 warning: '雲端同步讀取失敗，先顯示這台裝置上的資料：' + e.message };
      });
  }

  /*
   * 儲存。本機一定先寫（確保不會因為網路問題掉資料），
   * github 模式再往雲端推一份。
   */
  function save(data) {
    data.updatedAt = new Date().toISOString();
    localSave(data);
    if (getMode() !== 'github') {
      return Promise.resolve({ synced: false, warning: null });
    }
    return ghSave(data)
      .then(function () { return { synced: true, warning: null }; })
      .catch(function (e) {
        return { synced: false,
                 warning: '已存在這台裝置，但雲端同步失敗：' + e.message };
      });
  }

  /* ---------------------------------------------------------- 備份 */

  function exportJSON(data) {
    return JSON.stringify(data, null, 1);
  }

  function importJSON(text) {
    var d = JSON.parse(text);
    if (!d || typeof d !== 'object') throw new Error('檔案內容不是一份備份資料');
    if (!Array.isArray(d.assets) || !Array.isArray(d.trades)) {
      throw new Error('這個檔案看起來不是 InvestWatch 的備份（缺少 assets 或 trades）');
    }
    return d;
  }

  global.Storage = {
    getMode: getMode, setMode: setMode,
    getPat: getPat, setPat: setPat, clearPat: clearPat,
    hasPat: hasPat, maskedPat: maskedPat,
    getRepo: getRepo, setRepo: setRepo, DEFAULT_REPO: DEFAULT_REPO,
    load: load, save: save,
    testConnection: testConnection,
    exportJSON: exportJSON, importJSON: importJSON,
    _localLoad: localLoad, _localSave: localSave
  };
})(window);
