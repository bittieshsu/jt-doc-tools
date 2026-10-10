// 單行欄位的「之前填過的」（2026-10-08 使用者：「可以自動記住之前填過的，旁邊有按鈕點
// 下拉可以選之前填過的，也可以刪除」）。
//
// 樣板在輸入框**後面**放一顆按鈕（圖示與 title 在樣板裡寫、走 tr()）：
//
//   <input type="text" id="odUnit" class="field">
//   <button type="button" class="fh-btn" data-fh-for="odUnit"
//           data-fh-key="jtdt.od.hist.odUnit" title="…">…</button>
//
// 這支負責：把按鈕疊到輸入框裡的右緣、打開**本站樣式**的清單（不是原生 select / datalist ——
// 原生的長相各瀏覽器不同，也沒有辦法放「刪除這一筆」）、點一筆填回去、× 刪掉那一筆
// （清單不關）、Esc / 點外面關、方向鍵移動、Enter 選。
//
// **按鈕不包進新的一層**：頁面的版面量測（同一列輸入框一樣高）是照
// `.od-f > input.field` 找輸入框的，包一層就量不到了。所以按鈕用絕對定位疊上去，
// 位置照輸入框實際的位置算（標題折成兩行、模式切換才顯示，都會重算）。
//
// **紀錄鍵每次用的時候才讀**（`keyOf`），不在接上時記下來：頁面會在執行中換鍵（公文撰擬
// 切換成企業時，公司名稱跟機關全銜分開記）—— 接上時記下來的話，清單一直讀舊的那一組。
// 鍵也可以跟著另一格走：`data-fh-key-from="odOrg"` ＋ `data-fh-key-prefix="…"`＝前綴＋那一格的值
// （NFKC、去空白）；那一格空著就沒有鍵，清單顯示 `data-fh-nokey` 的說明。
// `data-fh-empty` 換掉「還沒有填過的紀錄」（說明文字由樣板寫、走 tr()）。
//
// 紀錄存在**這個瀏覽器**（localStorage，只是個人方便）—— 讀寫失敗（無痕、被擋、被清掉）
// 一律當成沒有紀錄，頁面照常。**什麼時候存由頁面決定**（`FieldHistory.push`）：
// 公文撰擬是「草稿產生成功之後」才存，打錯又沒送出的字不會被記住。
(function () {
  'use strict';

  var MAX_ITEMS = 10;     // 每一格最多記幾筆（最近的在前面）
  var MAX_LEN = 200;      // 太長的不記（**不截斷**：截過的字串不是使用者填過的東西）
  var open = null;        // 目前打開的那一個（一次只開一個）

  // ---------------------------------------------------------------- 存取

  function read(key) {
    var raw = null;
    try { raw = window.localStorage.getItem(key); } catch (e) { raw = null; }
    if (!raw) return [];
    var list;
    try { list = JSON.parse(raw); } catch (e) { return []; }
    if (!Array.isArray(list)) return [];
    return list.filter(function (v) { return typeof v === 'string' && v.trim(); })
               .slice(0, MAX_ITEMS);
  }

  function write(key, list) {
    try {
      if (list.length) window.localStorage.setItem(key, JSON.stringify(list));
      else window.localStorage.removeItem(key);
    } catch (e) { /* 只是方便：存不了就算了 */ }
  }

  function push(key, value) {
    if (!key) return;
    var v = String(value == null ? '' : value).trim();
    if (!v || v.length > MAX_LEN) return;
    var list = read(key).filter(function (x) { return x !== v; });
    list.unshift(v);
    write(key, list.slice(0, MAX_ITEMS));
  }

  function remove(key, value) {
    write(key, read(key).filter(function (x) { return x !== value; }));
  }

  // ---------------------------------------------------------------- 紀錄鍵

  function keyOf(btn) {
    if (!btn) return '';
    var from = btn.dataset.fhKeyFrom;
    if (from) {
      var src = document.getElementById(from);
      var v = src ? String(src.value || '') : '';
      if (v.normalize) v = v.normalize('NFKC');
      v = v.replace(/\s+/g, '');
      return v ? (btn.dataset.fhKeyPrefix || '') + v : '';
    }
    return btn.dataset.fhKey || '';
  }

  // ---------------------------------------------------------------- 一顆按鈕

  function attach(btn) {
    if (!btn || btn.dataset.fhMounted) return;
    var input = document.getElementById(btn.dataset.fhFor || '');
    var host = btn.parentNode;
    if (!input || !host) return;
    btn.dataset.fhMounted = '1';
    host.classList.add('fh-host');
    input.classList.add('fh-input');

    var panel = document.createElement('div');
    panel.className = 'fh-panel';
    panel.setAttribute('role', 'listbox');
    panel.hidden = true;
    host.appendChild(panel);

    // 位置：疊在輸入框裡面的右緣。走 CSSOM（CSP 擋行內 style 屬性，不擋 CSSOM）。
    function place() {
      if (!input.getClientRects().length) return;          // 還藏著（另一個模式）
      var top = input.offsetTop + (input.offsetHeight - btn.offsetHeight) / 2;
      var right = host.clientWidth - (input.offsetLeft + input.offsetWidth) + 3;
      btn.style.top = Math.round(top) + 'px';
      btn.style.right = Math.max(0, Math.round(right)) + 'px';
      if (!panel.hidden) placePanel();
    }
    function placePanel() {
      panel.style.top = Math.round(input.offsetTop + input.offsetHeight + 4) + 'px';
      panel.style.left = Math.round(input.offsetLeft) + 'px';
      panel.style.width = Math.round(input.offsetWidth) + 'px';
    }
    if (window.ResizeObserver) {
      var ro = new ResizeObserver(place);
      ro.observe(input);
      ro.observe(host);
    }
    window.addEventListener('resize', place);
    place();

    function rows() { return Array.prototype.slice.call(panel.querySelectorAll('.fh-row')); }

    function render(focusIdx) {
      var key = keyOf(btn);
      var list = key ? read(key) : [];
      panel.replaceChildren();
      if (!list.length) {
        var empty = document.createElement('div');
        empty.className = 'fh-empty';
        empty.textContent = (!key && btn.dataset.fhNokey) || btn.dataset.fhEmpty
                            || tr('還沒有填過的紀錄');
        panel.appendChild(empty);
      }
      list.forEach(function (v) {
        var row = document.createElement('div');
        row.className = 'fh-row';
        row.setAttribute('role', 'option');
        row.tabIndex = -1;
        row.dataset.value = v;
        var val = document.createElement('span');
        val.className = 'fh-val';
        val.textContent = v;            // 使用者自己打的字：一律 textContent
        val.title = v;
        val.setAttribute('data-i18n', 'skip');
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'fh-del';
        del.tabIndex = -1;
        del.title = tr('刪除這一筆');
        del.setAttribute('aria-label', tr('刪除這一筆'));
        del.textContent = '×';
        del.addEventListener('click', function (e) {
          // **清單不關**：不讓這一下傳到 document（那邊會當成「點外面」）
          e.stopPropagation();
          var idx = rows().indexOf(row);
          remove(key, v);
          render(idx);
        });
        row.appendChild(val);
        row.appendChild(del);
        row.addEventListener('click', function () { pick(v); });
        panel.appendChild(row);
      });
      if (focusIdx != null) {
        var rs = rows();
        if (rs.length) rs[Math.min(focusIdx, rs.length - 1)].focus();
        else btn.focus();
      }
    }

    function pick(v) {
      input.value = v;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      close();
      input.focus();
    }

    function openPanel(focusFirst) {
      if (open && open !== api) open.close();
      open = api;
      // **先顯示再畫**：焦點要移進清單的話，清單藏著時 focus() 不會有作用
      panel.hidden = false;
      render(focusFirst ? 0 : null);
      placePanel();
      btn.setAttribute('aria-expanded', 'true');
      btn.classList.add('open');
    }
    function close() {
      if (panel.hidden) return;
      panel.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
      btn.classList.remove('open');
      if (open === api) open = null;
    }
    var api = { close: close, open: openPanel, render: render, input: input,
                key: function () { return keyOf(btn); } };

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (panel.hidden) openPanel(e.detail === 0);    // 鍵盤（Enter / 空白鍵）打開的：焦點移進清單
      else close();
    });
    btn.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); openPanel(true); }
      else if (e.key === 'Escape') close();
    });
    panel.addEventListener('keydown', function (e) {
      var rs = rows(), i = rs.indexOf(document.activeElement);
      if (e.key === 'Escape') { e.preventDefault(); close(); btn.focus(); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); if (rs.length) rs[Math.min(rs.length - 1, i + 1)].focus(); }
      else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (i <= 0) btn.focus(); else rs[i - 1].focus();
      } else if (e.key === 'Enter' && i >= 0) { e.preventDefault(); pick(rs[i].dataset.value); }
      else if (e.key === 'Delete' && i >= 0) {
        e.preventDefault();
        remove(keyOf(btn), rs[i].dataset.value);
        render(i);
      } else if (e.key === 'Tab') close();
    });
    // 點在清單裡（列與列之間的空白）不關
    panel.addEventListener('click', function (e) { e.stopPropagation(); });
    btn._fieldHistory = api;
  }

  function attachAll(root) {
    (root || document).querySelectorAll('.fh-btn[data-fh-for]').forEach(attach);
  }

  document.addEventListener('click', function () { if (open) open.close(); });

  window.FieldHistory = { attach: attach, attachAll: attachAll, push: push, list: read,
                          remove: remove, keyOf: keyOf, MAX_ITEMS: MAX_ITEMS, MAX_LEN: MAX_LEN };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { attachAll(); });
  } else {
    attachAll();
  }
})();
