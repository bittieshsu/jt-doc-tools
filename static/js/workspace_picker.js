// Workspace picker + save helpers — shared across all tools.
//
// Loaded globally (only when the workspace feature is enabled). Provides:
//   window.openWorkspacePicker({accept, onPick})  — modal to choose a saved file
//   window.workspaceFileAsFile(fileId, meta)      — fetch a saved file as a File
//   window.saveToWorkspace(blobOrSpec, name, tool)— POST output into workspace
//   window.workspaceAcceptExts(acceptAttr, wsExts) — accept ∩ 工作區清單
(function () {
  function fmtBytes(n) {
    if (!n) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB']; let i = 0; let v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i];
  }
  function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

  // 這個上傳框收得下、而且工作區供應得了的副檔名交集。
  //
  // **工作區那半邊的清單一律讀 `data-ws-exts`（伺服器端給），不可寫死。**
  // 第一版把它寫死成 {pdf, png}，而工作區從 v1.14.6 起實際收 9 種 ——
  // 於是 accept 只有 docx/xlsx 的工具（辦公文件格式互轉）按鈕永遠不接線、
  // 按了沒反應，而且沒有任何錯誤（2026-08-17 使用者回報）。
  // 跟「存至工作區」按鈕那宗（v1.14.35）是同一家族的清單漂移。
  function workspaceAcceptExts(acceptAttr, wsExtsAttr) {
    const server = String(wsExtsAttr || '').toLowerCase().split(/\s+/)
      .filter(Boolean);
    const ws = server.length ? server
      : ['pdf', 'png'];      // 舊模板沒帶清單時的保守退路
    const a = (acceptAttr || '').toLowerCase();
    if (!a.trim() || a.includes('*/*')) return ws;
    const out = ws.filter((e) =>
      a.includes('.' + e) || a.includes('/' + e)
      || (e === 'pdf' && a.includes('pdf'))
      || (e === 'png' && a.includes('image/')));
    return out;
  }

  async function workspaceFileAsFile(fileId, meta) {
    const r = await fetch('/workspace/file/' + fileId);
    if (!r.ok) throw new Error('讀取工作區檔案失敗');
    const blob = await r.blob();
    const name = (meta && meta.name) || ('workspace' + ((meta && meta.ext) || ''));
    const type = (meta && meta.mime) || blob.type || 'application/octet-stream';
    return new File([blob], name, { type });
  }

  // Save a tool's output into the workspace.
  //   spec: {jobId} | {blob} | {url}
  async function saveToWorkspace(spec, name, tool) {
    const fd = new FormData();
    if (name) fd.append('name', name);
    if (tool) fd.append('source_tool', tool);
    if (spec && spec.jobId) {
      fd.append('job_id', spec.jobId);
    } else if (spec && spec.blob) {
      fd.append('file', spec.blob, name || 'file');
    } else if (spec && spec.url) {
      const rr = await fetch(spec.url);
      if (!rr.ok) throw new Error('讀取輸出檔失敗');
      const blob = await rr.blob();
      fd.append('file', blob, name || 'file');
    } else {
      throw new Error('沒有可儲存的內容');
    }
    const r = await fetch('/workspace/save', { method: 'POST', body: fd });
    if (!r.ok) {
      throw new Error(await window.friendlyServerError(r, '存至工作區失敗'));
    }
    return await r.json();  // { ok, file, duplicate }
  }

  function buildModal() {
    let m = document.getElementById('ws-picker-modal');
    if (m) return m;
    m = document.createElement('div');
    m.id = 'ws-picker-modal';
    m.className = 'ws-picker-backdrop';
    m.hidden = true;
    m.innerHTML =
      '<div class="ws-picker-dialog" role="dialog" aria-modal="true">' +
      '  <div class="ws-picker-head"><b>從工作區載入</b>' +
      '    <button type="button" class="ws-picker-close" aria-label="關閉">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12"/><path d="M6 18L18 6"/></svg></button></div>' +
      '  <div class="ws-picker-body"><div class="ws-picker-status muted">載入中…</div>' +
      '    <div class="ws-picker-grid"></div></div>' +
      '</div>';
    document.body.appendChild(m);
    // 樣式在 platform.css 的「工作區選擇視窗」區塊 —— **不可以在這裡動態
    // 注入 <style>**：CSP 的 style-src 只收 'self' 與 nonce，動態注入的
    // 沒有 nonce 會被整段擋掉，視窗變成攤在頁尾的無樣式 div（沒有遮罩、
    // 沒有置中），看起來像「按了沒反應」（2026-08-17 使用者回報，
    // 用印與騎縫章兩頁都中）。功能面不會有任何例外，只有 console 一行
    // CSP 違規 —— 所以「沒有 JS 錯誤」的檢查抓不到它。
    m.querySelector('.ws-picker-close').addEventListener('click', () => { m.hidden = true; });
    m.addEventListener('click', (e) => { if (e.target === m) m.hidden = true; });
    return m;
  }

  async function openWorkspacePicker(opts) {
    opts = opts || {};
    const exts = opts.accept && opts.accept.length ? opts.accept : ['pdf', 'png'];
    const m = buildModal();
    const grid = m.querySelector('.ws-picker-grid');
    const status = m.querySelector('.ws-picker-status');
    grid.innerHTML = '';
    status.textContent = '載入中…';
    status.hidden = false;
    m.hidden = false;
    let files = [];
    try {
      const r = await fetch('/workspace/api/list?accept=' + encodeURIComponent(exts.join(',')));
      if (!r.ok) throw new Error(await window.friendlyServerError(r, '載入工作區失敗'));
      files = (await r.json()).files || [];
    } catch (e) { status.textContent = e.message || '載入工作區失敗'; return; }
    if (!files.length) { status.textContent = '工作區內沒有符合的檔案（' + exts.join(' / ').toUpperCase() + '）。'; return; }
    status.hidden = true;
    grid.innerHTML = files.map(f => {
      const ext = (f.ext || '').replace('.', '');
      const thumb = '<img src="/workspace/thumb/' + f.file_id + '" alt="" loading="lazy" ' +
        'onerror="this.onerror=null;this.replaceWith(Object.assign(document.createElement(\'span\'),{className:\'ws-pick-badge\',textContent:\'' + ext.toUpperCase() + '\'}))">';
      return '<div class="ws-pick-card" data-id="' + f.file_id + '">' +
        '<div class="ws-pick-thumb">' + thumb + '</div>' +
        '<div class="ws-pick-info"><div class="ws-pick-name">' + esc(f.name) + '</div>' +
        '<div class="ws-pick-meta">' + fmtBytes(f.size) + '</div></div></div>';
    }).join('');
    grid.querySelectorAll('.ws-pick-card').forEach(card => {
      card.addEventListener('click', async () => {
        const id = card.dataset.id;
        const meta = files.find(x => x.file_id === id);
        m.hidden = true;
        try { await opts.onPick(id, meta); }
        catch (e) { alert(e.message || '載入失敗'); }
      });
    });
  }

  // Wire a 「存至工作區」 button for direct-download tools. `specFn` runs on
  // click and returns {url|blob, name, tool}. `saveToWorkspace` can fetch a
  // blob: URL just as well as a server URL, so the same anchor.href works for
  // both client-blob and server-file downloads.
  function attachWorkspaceSave(btn, specFn) {
    if (!btn || !window.saveToWorkspace) { if (btn) btn.hidden = true; return; }
    btn.hidden = false;
    btn.disabled = false;
    const orig = btn.dataset.wsOrig || (btn.dataset.wsOrig = btn.innerHTML);
    btn.innerHTML = orig;
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const s = await specFn();
        const res = await window.saveToWorkspace(s, s.name, s.tool);
        const dup = res && res.duplicate;
        btn.innerHTML = '已存至工作區';
        if (window.showToast) window.showToast(
          dup ? '已存至工作區（工作區已有同名檔，已另存一份）' : '已存至工作區', 'ok');
      } catch (e) {
        btn.disabled = false;
        (window.showAlert || window.alert)(e.message || '存至工作區失敗');
      }
    };
  }

  // Wire a 「從工作區載入」 button for tools with a CUSTOM upload UI (not the
  // shared file_upload component). On pick it sets the given <input type=file>
  // and dispatches a 'change' event, so the tool's existing handler runs.
  function attachWorkspaceLoadButton(btn, inputEl, opts) {
    opts = opts || {};
    if (!btn || !inputEl || !window.openWorkspacePicker) { if (btn) btn.hidden = true; return; }
    const exts = (opts.accept && opts.accept.length)
      ? opts.accept : workspaceAcceptExts(inputEl.getAttribute('accept') || '',
          (btn.dataset && btn.dataset.wsExts) || '');
    if (!exts.length) { btn.hidden = true; return; }
    btn.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      openWorkspacePicker({ accept: exts, onPick: async (id, meta) => {
        const file = await workspaceFileAsFile(id, meta);
        try { const dt = new DataTransfer(); dt.items.add(file); inputEl.files = dt.files; } catch (_e) {}
        inputEl.dispatchEvent(new Event('change', { bubbles: true }));
      }});
    });
  }

  // 把這個工具的產出送去**另一個工具**繼續處理。
  //
  // 走**我的工作區**當中轉：檔案是持久的，重啟、隔天回來都還在，使用者也
  // 看得到、刪得掉。之後要把多個工具串成工作流程也是走這條路，所以接口
  // 一開始就做成通用的（給 toolId 就好，不綁任何特定工具）。
  //
  // 工作區被管理員停用時退回作業結果（`from_job`）—— 那條路作業會過期、
  // 重啟後也不在，所以只當退路。
  //
  //   spec: {jobId} | {blob} | {url}   —— 與 saveToWorkspace 相同
  async function handoffToTool(toolId, spec, name, fromTool) {
    const qs = new URLSearchParams();
    if (name) qs.set('from_name', name);
    let usedWorkspace = false;
    try {
      const res = await saveToWorkspace(spec, name, fromTool || '');
      const fid = res && res.file && (res.file.id || res.file.file_id);
      if (fid) { qs.set('from_ws', fid); usedWorkspace = true; }
    } catch (_e) {
      // 工作區停用或存檔失敗 —— 不要卡住使用者，改走作業結果
    }
    if (!usedWorkspace) {
      if (!spec || !spec.jobId) {
        throw new Error('沒有可以帶過去的檔案');
      }
      qs.set('from_job', spec.jobId);
    }
    window.location.href = '/tools/' + toolId + '/?' + qs.toString();
  }

  window.handoffToTool = handoffToTool;
  window.openWorkspacePicker = openWorkspacePicker;
  window.attachWorkspaceSave = attachWorkspaceSave;
  window.attachWorkspaceLoadButton = attachWorkspaceLoadButton;
  window.workspaceFileAsFile = workspaceFileAsFile;
  window.saveToWorkspace = saveToWorkspace;
  window.workspaceAcceptExts = workspaceAcceptExts;
})();
