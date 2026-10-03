// friendlyServerError — 把 fetch Response 轉成 user-friendly 中文錯誤訊息
//   - JSON {detail / error / message}: 直接取
//   - FastAPI validation array: 取第一筆 msg
//   - 純文字短回應: 直接用
//   - HTML / stacktrace: 隱藏細節寫進 console,訊息列只顯示狀態碼
//
// 用法:
//   const r = await fetch(...);
//   if (!r.ok) {
//     alert(await friendlyServerError(r, '上傳失敗'));
//     return;
//   }
//
// 全域可呼叫: window.friendlyServerError
(function () {
  async function friendlyServerError(r, fallback) {
    const code = r ? r.status : 0;
    const codeMap = {
      400: tr('請求格式錯誤'), 401: tr('未登入或登入逾期'), 403: tr('權限不足'),
      404: tr('找不到資源'), 408: tr('請求逾時'), 410: tr('檔案已過期'),
      413: tr('檔案太大'), 415: tr('不支援的檔案格式'), 422: tr('參數驗證失敗'),
      429: tr('請求過於頻繁'),
      500: tr('伺服器內部錯誤'), 502: tr('後端服務無回應'), 503: tr('服務暫時不可用'),
      504: tr('後端逾時'),
    };
    const base = codeMap[code] || (fallback || tr('操作失敗'));
    let detail = '';
    try {
      const ct = (r.headers && r.headers.get && r.headers.get('content-type')) || '';
      if (ct.indexOf('application/json') >= 0) {
        const j = await r.json();
        const d = (j && (j.detail || j.error || j.message));
        if (typeof d === 'string' && d.length && d.length < 300) detail = d;
        else if (Array.isArray(d) && d[0] && d[0].msg) detail = String(d[0].msg).slice(0, 300);
      } else {
        const t = await r.text();
        if (t && t.indexOf('<') !== 0 && t.length < 300 && !/\n/.test(t)) {
          // 純文字短回應 — 嘗試解 JSON (有些 server 不設 ct)
          let parsed = null;
          try {
            parsed = JSON.parse(t);
          } catch (_) {}
          if (parsed && typeof parsed === 'object') {
            const d = parsed.detail || parsed.error || parsed.message;
            if (typeof d === 'string') detail = d;
          } else {
            detail = t;
          }
        } else if (t) {
          try { console.error('[friendly_error] server body:', t.slice(0, 1500)); } catch (_) {}
        }
      }
    } catch (_) {}
    const codeTag = code ? `（${code}）` : '';
    // **檔案太大要說得出是哪一段擋的**（使用者 2026-10-02：300 MB 的錄音被網站前面的
    // 反向代理擋下，畫面只寫「檔案太大」，看不出該去哪裡改）。本系統回的 413 都帶
    // `x-jtdt-limit`（site＝全站上限、tool＝這項功能自己的上限，見 app/main.py）；
    // **沒帶、也沒有我們的 JSON 說明的，就是前面的反向代理擋的**。
    if (code === 413) {
      const layer = (r.headers && r.headers.get && r.headers.get('x-jtdt-limit')) || '';
      if (layer === 'site') {
        const mb = (r.headers.get('x-jtdt-limit-mb') || '').replace(/[^0-9]/g, '');
        return `${base}${codeTag}：` + tr('本系統的單次上傳上限是 {0} MB。請分批上傳，或請管理員到「系統狀態 → 可上傳的檔案大小」調整。').replace('{0}', mb || '?');
      }
      if (layer === 'tool' || detail) {
        return `${base}${codeTag}：` + tr('本系統這項功能的上限') + (detail ? ` —— ${detail}` : '');
      }
      return `${base}${codeTag}：` + tr('這是網站前面的反向代理擋下的，不是本系統的上限。請管理員調高反向代理的上傳上限（nginx 是 client_max_body_size），目前的上限可以在「系統狀態 → 可上傳的檔案大小」實測。');
    }
    // 伺服器的說明是中文原文 —— 語系檔裡有那一句的話照介面語言顯示（查不到 `tr` 原樣回傳）
    return detail ? `${base}${codeTag}：${tr(detail)}` : `${base}${codeTag}`;
  }
  window.friendlyServerError = friendlyServerError;
})();
