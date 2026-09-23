/* 會議錄音轉逐字稿：游標在波形上時，那個時間點是誰在講。
 *
 * 抽成獨立的一支，是為了**在 node 裡真的跑它**測邊界
 * （tests/test_meeting_wave_tip.py）—— 畫在畫面上的東西沒有資料時測不到。
 *
 * 判準：時間落在某一段的 [start_ms, end_ms) 裡就是那一段的發言者；
 * 兩段之間的空檔**回 null，不猜一個人上去**（空檔本來就沒人講話，
 * 硬挑最近的一位會讓人以為那段有聲音）。兩人重疊時取**後開始**的那一段 ——
 * 那通常是插話的人，也是逐字稿上排在後面、使用者正要找的那一句。
 */
(function (root) {
  'use strict';

  function hasTimes(s) {
    return s && typeof s.start_ms === 'number' && typeof s.end_ms === 'number'
      && s.end_ms > s.start_ms;
  }

  /** 只留有起訖時間的段落，依開始時間排好（查詢用二分搜尋）。 */
  function index(segs) {
    return (segs || []).filter(hasTimes).slice().sort(function (a, b) {
      return a.start_ms - b.start_ms;
    });
  }

  /** `ms` 這個時間點是哪一段；沒有人在講就回 null。`sorted` 要先過 `index()`。 */
  function segmentAt(sorted, ms) {
    if (!sorted || !sorted.length || typeof ms !== 'number' || !(ms >= 0)) return null;
    // 最後一段「開始時間 <= ms」的位置
    var lo = 0, hi = sorted.length - 1, at = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (sorted[mid].start_ms <= ms) { at = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    // 往回找第一個還涵蓋 ms 的（重疊時後開始的優先）。長段落可能被後面幾段短的蓋過去，
    // 所以不能只看 `at` 那一段 —— 但往回找要有上限，不然很長的會議滑一下要掃整份。
    for (var i = at, n = 0; i >= 0 && n < 50; i--, n++) {
      if (ms < sorted[i].end_ms) return sorted[i];
    }
    return null;
  }

  root.MtWaveTip = { index: index, segmentAt: segmentAt };
})(typeof window !== 'undefined' ? window : globalThis);
