/* 介紹站的語言下拉。
 *
 * **只有這一份** —— 六個頁面（三種頁 × 三種語言）都載這支，不要各自在頁面裡
 * 寫一次（同一段邏輯抄三份就會有一份先壞掉，而且沒有人會發現）。
 *
 * 不用 inline 的 `onchange`：全站的規矩是「JS 只綁事件、不寫進屬性」
 * （應用程式端的 CSP 直接擋 inline handler，介紹站雖然是靜態頁，
 * 寫法還是跟著同一條走）。
 */
(function () {
  var sel = document.getElementById('langSwitch');
  if (!sel || !sel.options) return;
  sel.addEventListener('change', function () {
    var v = this.value;
    // **只接受同目錄的相對檔名**（`index-ja.html` 這種）。值是我們自己產生的
    // `<option>`，但那是 DOM 裡的字串 —— 只要有人能改到它，
    // `javascript:…` 就會在這裡被執行。限制成白名單形狀就沒有這條路。
    if (/^[a-z0-9][a-z0-9-]*\.html$/i.test(v)) window.location.href = v;
  });
})();
