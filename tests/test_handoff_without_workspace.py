"""工具之間的「轉送」在工作區被停用時要改帶作業結果（2026-10-08 客戶回報）。

轉逐字稿 →「轉送會議摘要」：工作區沒開時 `/workspace/save` 回 404，而送的是畫面上那份 JSON
（blob）、沒有作業編號可以退 —— `handoffToTool` 丟出錯誤、按鈕的處理函式沒接住，
**畫面上什麼都沒發生**（主控台才看得到 `Uncaught (in promise)`）。

`workspace_picker.js` 不只 base.html 在載（幾支工具自己也載，為了轉送），所以工作區停用時
它照樣在 —— 「存至工作區」「從工作區載入」也會出現、按下去只會 404。
修法：頁面用 `<meta name="jt-workspace">` 講工作區有沒有開；腳本照它決定。

這支**真的用 node 跑那支 JS**（假的 DOM 與 fetch），驗的是行為不是寫法。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_NODE = shutil.which("node")

_HARNESS = r"""
const fs = require('fs');
const scenario = JSON.parse(process.argv[3]);
const calls = [];
let navigated = null;
function el() {
  return {hidden: false, disabled: false, innerHTML: 'x', dataset: {}, onclick: null,
          listeners: {}, addEventListener(t, f) { this.listeners[t] = f; },
          getAttribute() { return ''; }};
}
global.document = {
  querySelector(sel) {
    if (sel === 'meta[name="jt-workspace"]') {
      if (scenario.meta === null) return null;
      return {getAttribute: () => scenario.meta};
    }
    return null;
  },
  getElementById() { return null; },
  createElement() { return el(); },
  body: {appendChild() {}},
};
global.window = {location: {}};
Object.defineProperty(global.window.location, 'href', {set(v) { navigated = v; }, get() { return ''; }});
global.tr = (s) => s;
global.FormData = class { constructor() { this.f = {}; } append(k, v) { this.f[k] = v; } };
global.Blob = class { constructor(p, o) { this.type = (o || {}).type; } };
global.fetch = async (url, opts) => {
  calls.push(url);
  if (url === '/workspace/save') {
    if (scenario.save === 'ok') return {ok: true, json: async () => ({file: {id: 'wsfile123456'}})};
    return {ok: false, status: 404, json: async () => ({detail: '工作區功能未啟用'}),
            text: async () => '', headers: {get: () => 'application/json'}};
  }
  throw new Error('unexpected fetch ' + url);
};
window.friendlyServerError = async () => '工作區功能未啟用';
eval(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  const out = {};
  try {
    await window.handoffToTool('meeting-summary', scenario.spec, 'a-逐字稿.json', 'meeting-transcribe');
    out.navigated = navigated;
  } catch (e) { out.error = e.message; }
  out.calls = calls;
  const btn = el();
  window.attachWorkspaceSave(btn, async () => ({}));
  out.saveHidden = btn.hidden;
  const btn2 = el();
  window.attachWorkspaceLoadButton(btn2, el(), {accept: ['json']});
  out.loadHidden = btn2.hidden;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run(tmp_path, **scenario) -> dict:
    if not _NODE:
        pytest.skip("沒有 node")
    h = tmp_path / "h.js"
    h.write_text(_HARNESS, encoding="utf-8")
    out = subprocess.run([_NODE, str(h), str(ROOT / "static/js/workspace_picker.js"),
                          json.dumps(scenario)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


BLOB_WITH_JOB = {"blob": True, "fallbackJobId": "job123456789"}


def test_workspace_off_goes_straight_to_the_job_result(tmp_path):
    got = _run(tmp_path, meta="off", save="404", spec=BLOB_WITH_JOB)
    assert "error" not in got, got
    assert "from_job=job123456789" in got["navigated"], got
    assert "/workspace/save" not in got["calls"], "工作區沒開就不要去打 /workspace/save（只會 404）"


def test_workspace_off_hides_the_workspace_buttons(tmp_path):
    got = _run(tmp_path, meta="off", save="404", spec=BLOB_WITH_JOB)
    assert got["saveHidden"] is True and got["loadHidden"] is True, got


def test_workspace_on_but_save_fails_still_falls_back(tmp_path):
    """工作區有開但存不進去（額度滿了…）：照樣帶得過去。"""
    got = _run(tmp_path, meta="on", save="404", spec=BLOB_WITH_JOB)
    assert "from_job=job123456789" in got.get("navigated", ""), got


def test_workspace_on_uses_the_workspace(tmp_path):
    got = _run(tmp_path, meta="on", save="ok", spec=BLOB_WITH_JOB)
    assert "from_ws=wsfile123456" in got["navigated"] and "from_job" not in got["navigated"], got
    assert got["saveHidden"] is False, "工作區有開時按鈕要在"


def test_no_meta_keeps_the_old_behaviour(tmp_path):
    """沒有那個 meta 的頁面照舊當成有開。"""
    got = _run(tmp_path, meta=None, save="ok", spec=BLOB_WITH_JOB)
    assert "from_ws=" in got["navigated"], got


def test_nothing_to_carry_says_so(tmp_path):
    got = _run(tmp_path, meta="off", save="404", spec={"blob": True})
    assert got.get("error") and "工作區沒有開" in got["error"], got


def test_base_template_declares_the_workspace_state(client, auth_off, monkeypatch):
    from app.core import workspace as ws
    monkeypatch.setattr(ws, "is_enabled", lambda: False)
    html = client.get("/tools/meeting-transcribe/").text
    assert '<meta name="jt-workspace" content="off">' in html
    monkeypatch.setattr(ws, "is_enabled", lambda: True)
    html = client.get("/tools/meeting-transcribe/").text
    assert '<meta name="jt-workspace" content="on">' in html


def test_transcribe_handoff_passes_its_job_and_reports_failure():
    src = (ROOT / "app/tools/meeting_transcribe/templates/meeting_transcribe.html").read_text(
        encoding="utf-8")
    body = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    i = body.index("el('mtToSummary')")
    block = body[i:i + 2500]
    assert re.search(r"fallbackJobId:\s*currentJobId\s*\|\|\s*job\.jobId", block), \
        "工作區沒開時沒有作業可以退 —— 按了沒反應"
    assert "catch" in block and "showAlert" in block, "帶不過去要講出來，不可以安靜失敗"
