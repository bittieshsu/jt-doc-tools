// 修正 Fabric.js 文字編輯時把畫面「捲走」的問題（GitHub issue #42）。
//
// 成因：Fabric 進入文字編輯時會建一個隱藏 <textarea>（`position: absolute` 定位在
// 文字所在座標、寬高 1px、opacity 0），然後呼叫 `hiddenTextarea.focus()`。瀏覽器
// 對焦時**預設會把該元素捲進視野**，於是整個視窗被往下拉，頂端工具列與左側縮圖欄
// 被推出畫面外；接著每次按鍵 Fabric 都會重新定位 textarea（`_updateTextarea`），
// 畫面就一直跳。
//
// 修法（不動 vendored fabric，用 prototype 包裝）：
//   1) 讓該 textarea 的 focus 一律帶 `preventScroll: true`。
//   2) 在 `enterEditing` / `_updateTextarea` 前後快照捲動位置，若被動到就還原。
//
// **刻意不改 textarea 的定位方式**：它的位置決定中文輸入法候選字視窗出現在哪裡，
// 移到固定位置會讓候選字跑到畫面角落。這裡只阻止「因對焦而捲動」，位置照舊。
(function () {
  if (!window.fabric || !fabric.IText || !fabric.IText.prototype) return;
  var P = fabric.IText.prototype;
  if (P.__jtdtNoFocusScroll) return;      // 防重複套用
  P.__jtdtNoFocusScroll = true;

  // 會被文字編輯波及的捲動容器：視窗 + 編輯器的畫布外框
  function snapshot() {
    var wrap = document.getElementById('canvasWrap');
    return {
      x: window.scrollX, y: window.scrollY,
      wrap: wrap,
      wl: wrap ? wrap.scrollLeft : 0,
      wt: wrap ? wrap.scrollTop : 0,
    };
  }
  function restore(s) {
    if (window.scrollX !== s.x || window.scrollY !== s.y) {
      window.scrollTo(s.x, s.y);
    }
    if (s.wrap && (s.wrap.scrollLeft !== s.wl || s.wrap.scrollTop !== s.wt)) {
      s.wrap.scrollLeft = s.wl;
      s.wrap.scrollTop = s.wt;
    }
  }

  var origInit = P.initHiddenTextarea;
  P.initHiddenTextarea = function () {
    var r = origInit.apply(this, arguments);
    var ta = this.hiddenTextarea;
    if (ta && !ta.__jtdtPatched) {
      ta.__jtdtPatched = true;
      var origFocus = ta.focus.bind(ta);
      ta.focus = function (opts) {
        var o = {};
        if (opts) { for (var k in opts) { o[k] = opts[k]; } }
        o.preventScroll = true;           // 關鍵：對焦不要捲動畫面
        return origFocus(o);
      };
    }
    return r;
  };

  ['enterEditing', '_updateTextarea'].forEach(function (name) {
    var orig = P[name];
    if (typeof orig !== 'function') return;
    P[name] = function () {
      var s = snapshot();
      var r = orig.apply(this, arguments);
      restore(s);                          // 編輯行為不該改變使用者的檢視位置
      return r;
    };
  });
})();
