/*
 * lock.js — 密碼鎖：把存在這台裝置上的個人資料與同步金鑰「加密」起來
 *
 * 為什麼不是做「登入畫面」？
 *   這是一個純前端的靜態網站，沒有後端可以驗證身分。做一個登入畫面只是把畫面遮起來，
 *   任何人打開瀏覽器的開發者工具就能直接讀出 localStorage 裡的資料——那是假的保護，
 *   只會讓人以為安全。
 *
 * 這裡做的是真的保護：用你的密碼推導出一把金鑰，把資料**加密**後才寫進 localStorage。
 *   * 沒有密碼，存在裝置上的就只是一團看不懂的亂碼，開發者工具也一樣讀不出來。
 *   * 密碼本身**不會**被存起來（只存一個驗證用的密文），所以誰也拿不回你的密碼。
 *
 * 用的是瀏覽器內建的 Web Crypto：
 *   PBKDF2-SHA256（25 萬次）把密碼推成金鑰 → AES-GCM 256 加密。
 *   每次加密都用新的隨機 IV，鹽（salt）在啟用時隨機產生一次。
 *
 * ⚠ 忘記密碼＝這台裝置上的資料就解不開了（這正是加密該有的行為）。
 *   所以啟用前一定要先匯出備份；有開雲端同步的話，私人倉庫那份也還在。
 *
 * 解鎖後金鑰放在 sessionStorage：同一個分頁裡換頁不用重打，
 * 關掉分頁（或關瀏覽器）就自動上鎖。
 */
(function (global) {
  'use strict';

  var LS_LOCK = 'iw_lock';     // {v, salt, iterations, check}
  var SS_KEY = 'iw_k';         // 解鎖後的金鑰（只存在這個分頁的 session）
  var CHECK_TEXT = 'investwatch-lock-ok';
  var ITERATIONS = 250000;

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); return true; } catch (e) { return false; } }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) { } }
  function ssGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function ssSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { } }
  function ssDel(k) { try { sessionStorage.removeItem(k); } catch (e) { } }

  function b64(bytes) {
    var bin = '';
    var arr = new Uint8Array(bytes);
    for (var i = 0; i < arr.length; i++) bin += String.fromCharCode(arr[i]);
    return btoa(bin);
  }
  function unb64(s) {
    var bin = atob(s);
    var arr = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return arr;
  }

  function supported() {
    return !!(global.crypto && global.crypto.subtle && global.TextEncoder);
  }

  function meta() {
    var raw = lsGet(LS_LOCK);
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (e) { return null; }
  }

  function isEnabled() { return !!meta(); }

  /* 用密碼＋鹽推導出 AES 金鑰 */
  function deriveKey(passcode, saltB64, iterations) {
    var enc = new TextEncoder();
    return crypto.subtle.importKey(
      'raw', enc.encode(passcode), { name: 'PBKDF2' }, false, ['deriveKey']
    ).then(function (base) {
      return crypto.subtle.deriveKey(
        {
          name: 'PBKDF2',
          salt: unb64(saltB64),
          iterations: iterations || ITERATIONS,
          hash: 'SHA-256'
        },
        base,
        { name: 'AES-GCM', length: 256 },
        true,                       // 要能匯出，才能放進 sessionStorage 撐過換頁
        ['encrypt', 'decrypt']
      );
    });
  }

  function importSessionKey() {
    var raw = ssGet(SS_KEY);
    if (!raw) return Promise.resolve(null);
    return crypto.subtle.importKey(
      'raw', unb64(raw), { name: 'AES-GCM' }, true, ['encrypt', 'decrypt']
    ).catch(function () { return null; });
  }

  function saveSessionKey(key) {
    return crypto.subtle.exportKey('raw', key).then(function (raw) {
      ssSet(SS_KEY, b64(raw));
    });
  }

  /* ---------------------------------------------------------- 加解密 */

  function encryptWith(key, text) {
    var iv = crypto.getRandomValues(new Uint8Array(12));
    return crypto.subtle.encrypt(
      { name: 'AES-GCM', iv: iv }, key, new TextEncoder().encode(text)
    ).then(function (ct) {
      var out = new Uint8Array(iv.length + ct.byteLength);
      out.set(iv, 0);
      out.set(new Uint8Array(ct), iv.length);
      return b64(out);
    });
  }

  function decryptWith(key, payload) {
    var all = unb64(payload);
    var iv = all.slice(0, 12);
    var ct = all.slice(12);
    return crypto.subtle.decrypt({ name: 'AES-GCM', iv: iv }, key, ct)
      .then(function (buf) { return new TextDecoder().decode(buf); });
  }

  /* ---------------------------------------------------------- 對外介面 */

  var sessionKey = null;         // 解鎖後放在記憶體，避免每次都重匯入

  function isUnlocked() { return !isEnabled() || !!sessionKey; }

  /* 頁面載入時呼叫：如果這個分頁先前解鎖過，就把金鑰接回來 */
  function resume() {
    if (!isEnabled() || !supported()) return Promise.resolve(isUnlocked());
    if (sessionKey) return Promise.resolve(true);
    return importSessionKey().then(function (k) {
      sessionKey = k;
      return !!k;
    });
  }

  function unlock(passcode) {
    var m = meta();
    if (!m) return Promise.resolve(true);
    if (!supported()) return Promise.reject(new Error('這個瀏覽器不支援加密功能'));
    return deriveKey(passcode, m.salt, m.iterations)
      .then(function (key) {
        return decryptWith(key, m.check).then(function (text) {
          if (text !== CHECK_TEXT) throw new Error('bad');
          sessionKey = key;
          return saveSessionKey(key).then(function () { return true; });
        });
      })
      .catch(function () {
        throw new Error('密碼不對');
      });
  }

  function lock() {
    sessionKey = null;
    ssDel(SS_KEY);
  }

  /*
   * 啟用密碼鎖。呼叫端要負責：先把目前的明文資料讀出來，
   * 啟用後再用 reencrypt 把它們寫回去（加密版）。
   */
  function enable(passcode) {
    if (!supported()) {
      return Promise.reject(new Error('這個瀏覽器不支援加密功能，無法啟用密碼鎖'));
    }
    if (!passcode || passcode.length < 4) {
      return Promise.reject(new Error('密碼至少要 4 個字（建議 6 個以上）'));
    }
    var salt = b64(crypto.getRandomValues(new Uint8Array(16)));
    return deriveKey(passcode, salt, ITERATIONS).then(function (key) {
      return encryptWith(key, CHECK_TEXT).then(function (check) {
        lsSet(LS_LOCK, JSON.stringify({
          v: 1, salt: salt, iterations: ITERATIONS, check: check
        }));
        sessionKey = key;
        return saveSessionKey(key).then(function () { return true; });
      });
    });
  }

  /* 關閉密碼鎖（要先解鎖成功才准關） */
  function disable() {
    lsDel(LS_LOCK);
    lock();
  }

  /* 給 storage.js 用：把一段文字加密／解密。沒啟用鎖就原樣進出。 */
  function protect(text) {
    if (!isEnabled()) return Promise.resolve(text);
    if (!sessionKey) return Promise.reject(new Error('資料已鎖定，請先解鎖'));
    return encryptWith(sessionKey, text).then(function (ct) {
      return 'enc:1:' + ct;
    });
  }

  function unprotect(stored) {
    if (stored === null || stored === undefined) return Promise.resolve(stored);
    if (String(stored).indexOf('enc:1:') !== 0) return Promise.resolve(stored);
    if (!sessionKey) return Promise.reject(new Error('資料已鎖定，請先解鎖'));
    return decryptWith(sessionKey, String(stored).slice(6));
  }

  function looksEncrypted(stored) {
    return typeof stored === 'string' && stored.indexOf('enc:1:') === 0;
  }

  /* ---------------------------------------------------------- 解鎖畫面
   *
   * 有開密碼鎖、而且這個分頁還沒解鎖時，蓋一層畫面要求輸入密碼。
   * 這一層不是「安全機制」本身——真正的保護是資料被加密了；
   * 沒有密碼的話，就算把這層畫面砍掉，底下也只是一團亂碼。
   */
  function gate(onUnlocked) {
    if (!isEnabled()) { onUnlocked(); return; }

    var box = document.createElement('div');
    box.className = 'lock-gate';
    box.innerHTML =
      '<div class="lock-card">' +
        '<div class="lock-icon">🔒</div>' +
        '<h2>已上鎖</h2>' +
        '<p>這台裝置上的紀錄是加密保存的，請輸入密碼解開。</p>' +
        '<form id="lock-form" autocomplete="off">' +
          '<input id="lock-input" type="password" inputmode="numeric" ' +
            'placeholder="輸入密碼" autocomplete="current-password">' +
          '<button class="btn btn-primary full" type="submit">解鎖</button>' +
        '</form>' +
        '<div id="lock-err" class="lock-err"></div>' +
        '<p class="lock-hint">忘記密碼的話，這台裝置上的資料就解不開了' +
          '（這正是加密該有的行為）。如果你有開雲端同步，' +
          '私人倉庫裡那一份還在，清除本機資料後重新設定金鑰就能讀回來。</p>' +
      '</div>';
    document.body.appendChild(box);
    document.body.style.overflow = 'hidden';

    var input = box.querySelector('#lock-input');
    var err = box.querySelector('#lock-err');
    setTimeout(function () { input.focus(); }, 60);

    box.querySelector('#lock-form').addEventListener('submit', function (e) {
      e.preventDefault();
      err.textContent = '';
      var pass = input.value;
      if (!pass) return;
      input.disabled = true;
      unlock(pass)
        .then(function () {
          box.remove();
          document.body.style.overflow = '';
          onUnlocked();
        })
        .catch(function (ex) {
          input.disabled = false;
          input.value = '';
          input.focus();
          err.textContent = ex.message || '密碼不對';
        });
    });
  }

  global.Lock = {
    gate: gate,
    supported: supported,
    isEnabled: isEnabled,
    isUnlocked: isUnlocked,
    resume: resume,
    unlock: unlock,
    lock: lock,
    enable: enable,
    disable: disable,
    protect: protect,
    unprotect: unprotect,
    looksEncrypted: looksEncrypted
  };
})(window);
