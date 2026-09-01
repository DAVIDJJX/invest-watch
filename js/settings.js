/*
 * settings.js — 設定頁：同步金鑰、備份還原、清除本機資料
 *
 * 安全原則：
 *   * 金鑰只寫進這台裝置的 localStorage，畫面上一律遮罩，任何時候可以清除。
 *   * 金鑰不會被寫進任何檔案、也不會出現在網址或畫面文字裡。
 *   * 測試連線時會順便檢查那個倉庫是不是 Private——如果是公開的就拒絕同步，
 *     因為個人持倉不可以放進公開倉庫。
 */
(function () {
  'use strict';

  function $(s) { return document.querySelector(s); }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function say(sel, text, cls) {
    var el = $(sel);
    if (!el) return;
    el.innerHTML = '<div class="result ' + (cls || '') + '">' + text + '</div>';
  }

  /* ---------------------------------------------------------- 同步 */

  function renderPatState() {
    var hint = $('#pat-state');
    if (window.Storage.hasPat()) {
      hint.innerHTML = '目前已設定金鑰：<code>' + esc(window.Storage.maskedPat()) +
                       '</code>（只顯示頭尾，完整內容不會顯示出來）';
    } else {
      hint.textContent = '目前沒有設定金鑰——「我的紀錄」會只存在這台裝置。';
    }
  }

  function saveSync() {
    var repo = $('#repo').value.trim();
    var pat = $('#pat').value.trim();

    if (repo) window.Storage.setRepo(repo);
    // 欄位留空代表「沿用已存的金鑰」，不是要清除（清除請按清除鈕）
    if (pat) window.Storage.setPat(pat);
    $('#pat').value = '';

    if (!window.Storage.hasPat()) {
      say('#sync-result', '還沒有金鑰可以測試。請先貼上金鑰再按一次。', 'bad');
      renderPatState();
      return;
    }

    say('#sync-result', '測試連線中…', '');
    window.Storage.testConnection()
      .then(function (info) {
        if (!info.canWrite) {
          window.Storage.setMode('local');
          say('#sync-result',
              '⚠ 連得上 <b>' + esc(info.repo) + '</b>，但金鑰<b>沒有寫入權限</b>。<br>' +
              '請回到 GitHub 的 token 設定，把 Repository permissions 的 ' +
              '<b>Contents</b> 改成 <b>Read and write</b>。', 'bad');
          return;
        }
        window.Storage.setMode('github');
        say('#sync-result',
            '✓ 連線成功！倉庫 <b>' + esc(info.repo) + '</b>（Private，可寫入）。<br>' +
            '跨裝置同步已開啟——到「我的紀錄」新增或修改，都會自動存一份到那個倉庫。<br>' +
            '手機也要同步的話，在手機瀏覽器打開這一頁再貼一次金鑰就好。', 'ok');
        renderPatState();
      })
      .catch(function (e) {
        window.Storage.setMode('local');
        say('#sync-result', '✗ ' + esc(e.message) +
            '<br>同步先維持關閉，「我的紀錄」仍可正常在這台裝置使用。', 'bad');
        renderPatState();
      });
  }

  function clearPat() {
    if (!confirm('確定要清除這台裝置上的同步金鑰嗎？\n' +
                 '清除後這台裝置就不再同步，但已經存進私人倉庫的資料不會被刪除。')) return;
    window.Storage.clearPat();
    $('#pat').value = '';
    renderPatState();
    say('#sync-result', '金鑰已從這台裝置清除，同步已關閉。' +
        '「我的紀錄」還是可以照常使用（只存在這台裝置）。', '');
    renderLocalStat();
  }

  /* ---------------------------------------------------------- 備份 */

  function doExport() {
    window.Storage.load().then(function (r) {
      var data = window.Portfolio.normalize(r.data);
      if (!data.assets.length && !data.trades.length) {
        say('#backup-result', '目前沒有任何紀錄可以匯出。', '');
        return;
      }
      var text = window.Storage.exportJSON(data);
      var blob = new Blob([text], { type: 'application/json' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      var d = new Date();
      a.href = url;
      a.download = 'investwatch-備份-' + d.getFullYear() +
                   String(d.getMonth() + 1).padStart(2, '0') +
                   String(d.getDate()).padStart(2, '0') + '.json';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      say('#backup-result',
          '已匯出 ' + data.assets.length + ' 個標的、' + data.trades.length +
          ' 筆交易紀錄。<br>⚠ 這個檔案含有你的持倉數字，請當成私人文件保管，' +
          '不要放進公開的地方。', 'ok');
    });
  }

  function doImport(file) {
    var reader = new FileReader();
    reader.onload = function () {
      var incoming;
      try {
        incoming = window.Storage.importJSON(String(reader.result));
      } catch (e) {
        say('#backup-result', '✗ 匯入失敗：' + esc(e.message), 'bad');
        return;
      }
      var data = window.Portfolio.normalize(incoming);
      if (!confirm('要用這份備份「取代」目前的紀錄嗎？\n\n' +
                   '備份內容：' + data.assets.length + ' 個標的、' +
                   data.trades.length + ' 筆交易紀錄\n' +
                   '這個動作會覆蓋掉目前的資料，無法復原。')) {
        say('#backup-result', '已取消，沒有變更任何資料。', '');
        return;
      }
      window.Storage.save(data).then(function (r) {
        say('#backup-result',
            '✓ 已匯入 ' + data.assets.length + ' 個標的、' +
            data.trades.length + ' 筆交易紀錄。' +
            (r.synced ? '（也已同步到私人倉庫）' : '') +
            (r.warning ? '<br>' + esc(r.warning) : '') +
            '<br><a href="records.html">到「我的紀錄」看看 →</a>',
            r.warning ? 'bad' : 'ok');
        renderLocalStat();
      });
    };
    reader.onerror = function () {
      say('#backup-result', '✗ 讀不到這個檔案。', 'bad');
    };
    reader.readAsText(file);
  }

  /* ---------------------------------------------------------- 本機資料 */

  function renderLocalStat() {
    var d = window.Storage._localLoad();
    var box = $('#local-stat');
    if (!d) {
      box.textContent = '這台裝置目前沒有存任何紀錄。';
      return;
    }
    var n = window.Portfolio.normalize(d);
    box.innerHTML = '這台裝置存了 <b>' + n.assets.length + '</b> 個標的、<b>' +
      n.trades.length + '</b> 筆交易紀錄、<b>' + n.navs.length + '</b> 筆手動淨值。' +
      (n.updatedAt ? '<br>最後更新：' + esc(String(n.updatedAt).slice(0, 16).replace('T', ' ')) : '') +
      '<br>同步狀態：<b>' +
      (window.Storage.getMode() === 'github'
        ? '已同步到 ' + esc(window.Storage.getRepo())
        : '只存在這台裝置') + '</b>';
  }

  function wipeLocal() {
    if (!confirm('確定要清除這台裝置上的所有紀錄嗎？\n\n' +
                 '建議先「匯出備份」。這個動作無法復原。')) return;
    if (!confirm('再確認一次：真的要清除嗎？')) return;
    try { localStorage.removeItem('iw_portfolio'); } catch (e) { /* 無痕模式 */ }
    renderLocalStat();
    say('#backup-result', '這台裝置上的紀錄已清除。' +
        (window.Storage.getMode() === 'github'
          ? '私人倉庫裡那份沒有被刪除，重新整理「我的紀錄」就會讀回來。'
          : ''), '');
  }

  /* ---------------------------------------------------------- 啟動 */

  function boot() {
    $('#repo').value = window.Storage.getRepo();
    renderPatState();
    renderLocalStat();

    $('#btn-save-sync').addEventListener('click', saveSync);
    $('#btn-clear-pat').addEventListener('click', clearPat);
    $('#btn-toggle-pat').addEventListener('click', function () {
      var f = $('#pat');
      var show = f.type === 'password';
      f.type = show ? 'text' : 'password';
      this.textContent = show ? '隱藏' : '顯示';
    });

    $('#btn-export').addEventListener('click', doExport);
    $('#btn-import').addEventListener('click', function () { $('#import-file').click(); });
    $('#import-file').addEventListener('change', function () {
      if (this.files && this.files[0]) doImport(this.files[0]);
      this.value = '';
    });
    $('#btn-wipe').addEventListener('click', wipeLocal);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
