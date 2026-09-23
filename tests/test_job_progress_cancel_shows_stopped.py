"""按下「停止」之後，畫面要看得出來停了。

## 由來（v1.15.69，使用者回報）

使用者按了「停止分析」，回報「**時間是沒走了 但是畫面看不出是停止的樣子**」：
進度條停在原來的位置、狀態文字還寫著「擷取重點 4/5」、
「這份工作在伺服器上執行，可以關掉這一頁」還掛著 ——
跟「卡住了」長得一模一樣。

原因很諷刺：`cancel()` 做的是「停輪詢 ＋ 呼叫取消 API」，
而**把畫面畫成「已停止」的程式碼在輪詢那條路上**
（讀到伺服器回的 `status: 'cancelled'` 才會執行）。
一停輪詢，那段就永遠不會跑到。

## 判準

**不是比對原始碼字串** —— 那種檢查擋不住「有呼叫但沒作用」。
這裡真的在 node 裡把 `job_progress.js` 跑起來、餵一個最小 DOM、
呼叫 `cancel()`，然後看**畫面狀態有沒有變**：

* 狀態文字不可以還是原來那句
* 根元素要帶上 `jp-stopped`（灰掉的樣式靠它）
* 「可以關掉這一頁」要收起來

另外驗 `hide()` 會把 `jp-stopped` 清掉 —— 不清的話下一次送檔案，
進度條一開始就是灰的而且寫著「已停止」。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "js" / "job_progress.js"
CSS = ROOT / "static" / "css" / "platform.css"

_HARNESS = r"""
// 最小 DOM：只做 job_progress.js 真的會用到的那幾件事。
function makeEl(cls) {
  return {
    className: cls || '', hidden: false, textContent: '', href: '',
    style: {}, dataset: {},
    _classes: new Set(),
    classList: {
      add: function (c) { this._el._classes.add(c); },
      remove: function (c) { this._el._classes.delete(c); },
      contains: function (c) { return this._el._classes.has(c); }
    },
    addEventListener: function () {},
    querySelector: function (sel) { return this._kids[sel] || null; },
    _kids: {}
  };
}
function wire(el) { el.classList._el = el; return el; }

var root = wire(makeEl('job-progress'));
['.job-bar-inner', '.job-status', '.job-download', '.job-download-png',
 '.job-save-ws', '.job-reset', '.job-elapsed', '.job-bg-note'].forEach(function (s) {
  root._kids[s] = wire(makeEl(s.slice(1)));
});

global.window = {};
global.tr = function (s) { return s; };
global.fetch = function () { return Promise.resolve({ ok: true,
  json: function () { return Promise.resolve({}); } }); };
global.setInterval = function () { return 0; };
global.clearInterval = function () {};

require(process.argv[2]);
var JP = global.window.JobProgress;

var cancelledCalls = 0;
var jp = new JP(root, { onCancel: function () { cancelledCalls++; } });
jp.jobId = 'abc';
root._kids['.job-status'].textContent = '擷取重點 4/5';
root._kids['.job-bg-note'].hidden = false;
root.hidden = false;

jp.cancel();

var after = {
  status: root._kids['.job-status'].textContent,
  stoppedClass: root._classes.has('jp-stopped'),
  bgNoteHidden: root._kids['.job-bg-note'].hidden,
  onCancelCalls: cancelledCalls
};
jp.hide();
after.stoppedClassAfterHide = root._classes.has('jp-stopped');
console.log(JSON.stringify(after));
"""


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("沒有 node")
    h = tmp_path_factory.mktemp("jp") / "harness.js"
    h.write_text(_HARNESS, encoding="utf-8")
    r = subprocess.run(["node", str(h), str(JS)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"harness 跑不起來：{r.stderr[-2000:]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_status_text_changes(outcome):
    assert outcome["status"] != "擷取重點 4/5", (
        "按了停止，狀態文字還停在原來那句 —— 畫面跟卡住一模一樣")
    assert outcome["status"], "狀態文字被清成空的，那更看不出發生了什麼"


def test_the_bar_is_marked_stopped(outcome):
    assert outcome["stoppedClass"], (
        "根元素沒有帶上 `jp-stopped` —— 進度條不會灰掉，"
        "看起來就像還在跑")


def test_the_keep_waiting_note_is_taken_down(outcome):
    assert outcome["bgNoteHidden"] is True, (
        "「可以關掉這一頁」還掛著 —— 作業已經停了，那句話變成錯的")


def test_the_page_is_told(outcome):
    assert outcome["onCancelCalls"] == 1, (
        "沒有通知頁面（`onCancel`），工具頁的停止按鈕不會收起來")


def test_starting_again_is_not_stuck_in_the_stopped_look(outcome):
    assert outcome["stoppedClassAfterHide"] is False, (
        "`hide()` 沒有清掉 `jp-stopped` —— 下一次送檔案，"
        "進度條一開始就是灰的而且寫著「已停止」")


def test_the_stopped_look_actually_has_styles():
    """**自己發明的類別名一定要補樣式**（本專案 v1.14.21 記過）。"""
    css = CSS.read_text(encoding="utf-8")
    assert ".jp-stopped" in css, (
        f"`jp-stopped` 在 {CSS.name} 裡沒有樣式 —— 那個 class 加了等於沒加")
