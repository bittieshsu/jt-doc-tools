/* 前端字串翻譯。
 *
 * 樣板的 `{{ tr('…') }}` 是**伺服器端渲染時**求值的，JS 執行期拿不到 ——
 * 按鈕文字、錯誤訊息、動態插進 DOM 的說明都得走這一支。
 *
 * 用法與樣板端一致：key 就是繁體中文原文。
 *
 *     btn.textContent = tr('開始轉換');
 *
 * 查不到就原樣回傳中文 —— 這一點很重要：
 *   * 繁體中文底下字典**是空的**（連載都不用載），所以零風險零成本；
 *   * 英文底下漏翻的字串會顯示中文，而不是顯示 key 或空白。
 *
 * 帶變數的句子**一律參數化**，不可以把內插後的整句當 key（內插值一變就查不到）：
 *
 *     tr('已選：{0}').replace('{0}', file.name)
 */
(function () {
  window.__I18N__ = window.__I18N__ || {};
  window.tr = function (s) {
    if (typeof s !== 'string') return s;
    var v = window.__I18N__[s];
    return (typeof v === 'string' && v) ? v : s;
  };
})();
