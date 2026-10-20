/* 全站日期時間的顯示格式 —— **只在這裡定義**（v1.17.1 起全站統一、24 小時制）。
 *
 *   jtTime.dt(v)                → 2026/10/19 23:05
 *   jtTime.dt(v, {seconds:true}) → 2026/10/19 23:05:07
 *   jtTime.date(v)              → 2026/10/19
 *   jtTime.time(v, {seconds})   → 23:05 / 23:05:07
 *   jtTime.md(v, {seconds})     → 10/19 23:05（清單裡同一年的精簡寫法）
 *
 * `v` 可以是 epoch 秒、epoch 毫秒、ISO 字串或 Date；讀不懂就回空字串。一律用瀏覽器的時區、24 小時制，
 * 不用 toLocaleString（會依語系出現「下午」或 AM / PM）。伺服器端的同一格式在 `app/main.py:_tpl_localdt`。
 * 檢查：tests/test_datetime_format_is_unified.py 掃全站，不可以自己組時間。
 */
(function () {
  function toDate(v) {
    if (v === null || v === undefined || v === '') return null;
    if (v instanceof Date) return isNaN(v.getTime()) ? null : v;
    if (typeof v === 'number' || (typeof v === 'string' && /^\d+(\.\d+)?$/.test(v.trim()))) {
      const n = Number(v);
      if (!isFinite(n) || n <= 0) return null;
      const d = new Date(n < 1e12 ? n * 1000 : n);     // 秒或毫秒
      return isNaN(d.getTime()) ? null : d;
    }
    const d = new Date(v);
    return isNaN(d.getTime()) ? null : d;
  }
  function p2(n) { return String(n).padStart(2, '0'); }
  function date(v) {
    const d = toDate(v);
    return d ? d.getFullYear() + '/' + p2(d.getMonth() + 1) + '/' + p2(d.getDate()) : '';
  }
  function time(v, opts) {
    const d = toDate(v);
    if (!d) return '';
    return p2(d.getHours()) + ':' + p2(d.getMinutes()) + (opts && opts.seconds ? ':' + p2(d.getSeconds()) : '');
  }
  function dt(v, opts) {
    const d = toDate(v);
    return d ? date(d) + ' ' + time(d, opts) : '';
  }
  function md(v, opts) {
    const d = toDate(v);
    return d ? p2(d.getMonth() + 1) + '/' + p2(d.getDate()) + ' ' + time(d, opts) : '';
  }
  window.jtTime = { toDate: toDate, date: date, time: time, dt: dt, md: md };
})();
