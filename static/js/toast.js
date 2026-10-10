// Shared toast helper. Usage: showToast('訊息', 'ok' | 'err' | '')
window.showToast = function(msg, kind) {
  let host = document.getElementById('toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toast-host';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 250);
  }, 1800);
};

// 卡片標題點一下收折：`.panel` 的第一個子元素是 `<h2>` 的都算（`<details class="panel">`
// 本來就會收折，不管）。收折狀態記在 localStorage（鍵是網址路徑 ＋ 標題文字）。
//
// **整頁一個事件委派，不在載入當下逐張掛** —— 原本只掛 DOMContentLoaded 那一刻已經在頁面上的卡片，
// 程式之後才建出來的卡片（公文知識庫「政府公開資料」那幾張、重畫過的清單）標題有箭頭、點了卻沒反應
// （2026-10-09 使用者：「點了卡片標題 怎麼不能收折」）。記住的狀態也一樣：卡片一出現就套上。
(function () {
  function headOf(panel) {
    if (!panel || panel.tagName === 'DETAILS') return null;
    const h2 = panel.firstElementChild;
    return h2 && h2.tagName === 'H2' ? h2 : null;
  }
  function keyOf(h2) {
    return 'panel-collapsed:' + location.pathname + ':' + (h2.textContent || '').trim();
  }
  function restore(panel) {
    const h2 = headOf(panel);
    if (!h2 || panel.dataset.collapseRestored) return;
    panel.dataset.collapseRestored = '1';
    let saved = null;
    try { saved = localStorage.getItem(keyOf(h2)); } catch (_e) { saved = null; }
    if (saved === '1') panel.classList.add('collapsed');
  }
  function restoreIn(node) {
    if (!node || node.nodeType !== 1) return;
    if (node.classList.contains('panel')) restore(node);
    node.querySelectorAll('.panel').forEach(restore);
  }
  document.addEventListener('click', (e) => {
    const h2 = e.target.closest && e.target.closest('.panel > h2:first-child');
    if (!h2) return;
    const panel = h2.parentElement;
    if (headOf(panel) !== h2) return;
    // 點在標題裡的連結、按鈕、輸入框上不收折
    const ctl = e.target.closest('a, button, input, select, textarea, label');
    if (ctl && h2.contains(ctl)) return;
    panel.classList.toggle('collapsed');
    try {
      localStorage.setItem(keyOf(h2), panel.classList.contains('collapsed') ? '1' : '0');
    } catch (_e) { /* 只是記住偏好：存不了就算了 */ }
  });
  function start() {
    restoreIn(document.body);
    if (window.MutationObserver) {
      new MutationObserver((muts) => {
        muts.forEach((m) => m.addedNodes.forEach(restoreIn));
      }).observe(document.body, { childList: true, subtree: true });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();

// flashSaved(buttonEl): briefly turn a button green with "✓ 已儲存" label.
window.flashSaved = function(btn, originalHTML) {
  const restore = originalHTML || btn.dataset.origHTML || btn.innerHTML;
  btn.dataset.origHTML = restore;
  btn.innerHTML = tr('✓ 已儲存');
  btn.classList.add('saved-flash');
  btn.disabled = true;
  setTimeout(() => {
    btn.innerHTML = restore;
    btn.classList.remove('saved-flash');
    btn.disabled = false;
  }, 1600);
};
