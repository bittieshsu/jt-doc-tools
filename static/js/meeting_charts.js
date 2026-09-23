/* 會議摘要的三張圖，**在瀏覽器裡畫**。
 *
 * ## 為什麼搬到前端（使用者 2026-09-19：「圖都要用前端產 才能互動」）
 *
 * 原本是伺服器產 SVG、頁面用 `<img>` 嵌 —— 那樣**沒有辦法互動**：
 * 圖裡的每一格都對應逐字稿的某一段，而這支工具的賣點就是「每一條都指得回
 * 原文」，可是 `<img>` 裡的東西點不動。而且固定寬度的圖在寬螢幕上右邊會
 * 留一大片空白。
 *
 * 在瀏覽器裡畫換到三件事：
 *   1. **點得下去** —— 每個節點帶 `data-seq`，頁面既有的事件委派
 *      （`document` 上的 `[data-seq]` handler）就會跳到逐字稿那一段並highlight。
 *      **不要另外寫一份跳轉邏輯**，卡片與章節用的是同一支。
 *   2. **跟著容器寬度重畫**，視窗縮放也重畫。
 *   3. 滑鼠移上去看得到是哪一段。
 *
 * ## 匯出仍然是伺服器畫的，這是**刻意的**
 *
 * PNG / PDF / Markdown 內嵌用的是 `app/core/meeting_charts.py` 產的 SVG。
 * 兩邊**畫法**確實有兩份（本專案一向反對這種事），但要命的不是畫法漂掉
 * —— 那是看得見的；要命的是**內容**漂掉。所以：
 *
 *   * 兩邊的**資料來源同一份**（`build_mindmap()` / `chapter_times()` /
 *     `speaker_stats()` 的輸出），前端只負責排版。
 *   * **配色從伺服器送過來**（`data-palette` / `data-kinds`），
 *     不在這裡抄一份 —— 不然畫面上是藍的、下載的 PNG 是綠的。
 *   * 守門 `tests/test_meeting_chart_parity.py` 驗「兩邊畫出來的東西
 *     講的是同一件事」（同樣的章節、同樣的語者、同樣的段號）。
 *
 * 要把匯出也改成用畫面上這一份，得讓前端把 SVG 送回伺服器轉檔 ——
 * 那等於讓 MuPDF 解析使用者送上來的 SVG，要先做白名單過濾並放進
 * 稽核 F04 那條獨立行程裡。**還沒做**。
 */
(function () {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';
  var LINE_H = 16;

  function el(name, attrs) {
    var n = document.createElementNS(NS, name);
    if (attrs) { for (var k in attrs) { if (attrs[k] != null) n.setAttribute(k, attrs[k]); } }
    return n;
  }

  function text(x, y, s, attrs) {
    var n = el('text', attrs || {});
    n.setAttribute('x', x); n.setAttribute('y', y);
    n.textContent = s;                       // 一律 textContent，不用 innerHTML
    return n;
  }

  /* 跟 `meeting_charts._wrap` 同一條規則：中文沒有詞界，按字數折；
     遇到空白優先斷在空白處，拉丁字才不會被切在單字中間。 */
  function wrap(s, per) {
    s = String(s == null ? '' : s).split(/\s+/).join(' ').trim();
    if (!s) return [];
    var out = [], cur = '';
    for (var i = 0; i < s.length; i++) {
      cur += s[i];
      if (cur.length >= per) {
        var cut = cur.lastIndexOf(' ');
        if (cut > per * 0.5) { out.push(cur.slice(0, cut)); cur = cur.slice(cut + 1); }
        else { out.push(cur); cur = ''; }
      }
    }
    if (cur) out.push(cur);
    return out;
  }

  function mmss(ms) {
    if (ms == null) return '';
    var t = Math.floor(ms / 1000), h = Math.floor(t / 3600);
    var m = Math.floor((t % 3600) / 60), s = t % 60;
    var mm = h ? (m < 10 ? '0' + m : '' + m) : '' + m;
    return (h ? h + ':' : '') + mm + ':' + (s < 10 ? '0' + s : '' + s);
  }

  /* 可以點的節點：帶 `data-seq` 給頁面既有的委派用，並給鍵盤使用者一個焦點。
   *
   * `idx` 是「同一筆資料在這張圖上的編號」—— 長條與它下面那一列圖例共用，
   * 滑過其中一個，另一個才知道自己要亮起來。 */
  /* 節點上畫的是**縮短過**的文字（見 `meeting_insight.shorten_for_node`），
     而提示框要給**完整**的那一份 —— 縮短時伺服器端會多帶一個 `label_full`。
     用縮過的那份當提示框等於「滑鼠移上去還是看不到後面」，
     那正是使用者回報「後面有字被截斷」時最想確認的事。 */
  function clickable(g, seq, label, idx) {
    if (idx != null) g.setAttribute('data-idx', idx);
    if (seq == null) {
      if (idx != null) g.setAttribute('class', 'mc-row');
      if (label) { var t0 = el('title'); t0.textContent = label; g.appendChild(t0); }
      return g;
    }
    g.setAttribute('data-seq', seq);
    g.setAttribute('tabindex', '0');
    g.setAttribute('role', 'button');
    g.setAttribute('class', idx != null ? 'mc-hit mc-row' : 'mc-hit');
    var t = el('title'); t.textContent = label; g.appendChild(t);
    return g;
  }

  /* 滑過任何一格 → 同編號的都亮著，其餘變淡。
   *
   * **用事件委派掛在 svg 上**，不要逐格 addEventListener ——
   * 一張圖幾十格，而且重畫時整棵換掉，逐格掛的話監聽器會跟著漏掉。 */
  function wireHover(svg) {
    function setFocus(idx) {
      var rows = svg.querySelectorAll('.mc-row');
      for (var i = 0; i < rows.length; i++) {
        var on = idx == null || rows[i].getAttribute('data-idx') === idx;
        rows[i].classList.toggle('mc-faded', !on);
        rows[i].classList.toggle('mc-lit', idx != null && on);
      }
    }
    svg.addEventListener('mouseover', function (e) {
      var row = e.target.closest ? e.target.closest('.mc-row') : null;
      setFocus(row ? row.getAttribute('data-idx') : null);
    });
    svg.addEventListener('mouseleave', function () { setFocus(null); });
    return svg;
  }

  /* **寬高用實際像素，不要用 `width:100%`。**
   *
   * 用百分比的話，只要量到的容器寬度比實際小，整張圖就會被等比放大 ——
   * **字跟著變大**，而且看起來像「字級設錯了」而不是「量錯寬度」。
   * 2026-09-19 實際踩到：心智圖的容器在量的當下還是 `hidden`，
   * `clientWidth` 是 0 → 退到預設 760 → 撐到 ~1300px ＝ 1.7 倍，
   * 使用者回報「圖二字又過大」。
   *
   * 畫成實際像素之後，就算再量錯一次也只是**留白**不會變形，
   * 而重畫是綁在 resize 上的，不需要靠 CSS 縮放。 */
  function svgRoot(w, h, title) {
    var s = el('svg', {
      xmlns: NS, viewBox: '0 0 ' + w + ' ' + h,
      width: w, height: h, role: 'img', 'font-family': 'inherit'
    });
    var t = el('title'); t.textContent = title || ''; s.appendChild(t);
    return s;
  }

  // ---------------------------------------------------------------- 心智圖
  function mindmap(nodes, width, kinds, tr) {
    if (!nodes || nodes.length < 2) return null;
    var kids = {}, roots = [];
    nodes.forEach(function (n) {
      var p = n.parent_id == null ? '' : n.parent_id;
      (kids[p] = kids[p] || []).push(n);
    });
    roots = kids[''] || [];
    if (!roots.length) return null;

    var pad = 14, gap = 46;
    // 左欄佔容器的四分之一左右，但夾在 190~300 之間 ——
    // 太窄章節標題會折成一條細柱，太寬右邊的項目就擠了。
    var col1 = Math.max(190, Math.min(300, Math.round(width * 0.24)));
    var col2x = pad + col1 + gap, col2w = width - col2x - pad;
    var fullw = width - pad * 2;
    // 每行幾個字：依實際欄寬換算（13px 的中文字約 13px 寬）。
    var rootChars = Math.max(6, Math.floor((col1 - 26) / 13));
    var itemChars = Math.max(12, Math.floor((col2w - 26) / 12.6));

    function boxH(label, per) {
      return Math.max(34, wrap(label, per).length * LINE_H + 16);
    }

    var g = document.createDocumentFragment(), y = pad;
    roots.forEach(function (root) {
      var children = kids[root.node_id] || [];
      var ks = kinds[root.type || 'topic'] || { colour: '#4338ca', label: '' };
      var label = root.label || '';
      var seq = (root.segment_ids && root.segment_ids[0] != null) ? root.segment_ids[0] : null;

      if (!children.length) {
        // **沒有掛東西的章節畫成整寬的扁條** —— 留在窄欄的話右半邊會是
        // 一片空白，看起來像圖畫壞了（2026-09-19 回報）。
        var h0 = boxH(label, Math.floor((fullw - 150) / 13));
        var row = el('g');
        row.appendChild(el('rect', { x: pad, y: y, width: fullw, height: h0,
                                     rx: 8, fill: ks.colour, opacity: 0.06 }));
        row.appendChild(el('rect', { x: pad, y: y, width: 4, height: h0,
                                     rx: 2, fill: ks.colour, opacity: 0.55 }));
        wrap(label, Math.floor((fullw - 150) / 13)).forEach(function (ln, i) {
          row.appendChild(text(pad + 14, y + 21 + i * LINE_H, ln,
            { 'font-size': 13, 'font-weight': 600, fill: ks.colour }));
        });
        row.appendChild(text(width - pad - 12, y + h0 / 2 + 4, tr('無決議／待辦'),
          { 'font-size': 11, 'text-anchor': 'end', fill: '#94a3b8' }));
        g.appendChild(clickable(row, seq, root.label_full || label));
        y += h0 + 10;
        return;
      }

      var rh = boxH(label, rootChars);
      var chH = children.map(function (c) { return boxH(c.label || '', itemChars); });
      var blockH = Math.max(rh, chH.reduce(function (a, b) { return a + b; }, 0)
                                + Math.max(0, children.length - 1) * 8);
      var ry = y + Math.floor((blockH - rh) / 2);

      var head = el('g');
      head.appendChild(el('rect', { x: pad, y: ry, width: col1, height: rh,
                                    rx: 8, fill: ks.colour, opacity: 0.10 }));
      head.appendChild(el('rect', { x: pad, y: ry, width: 4, height: rh,
                                    rx: 2, fill: ks.colour }));
      wrap(label, rootChars).forEach(function (ln, i) {
        head.appendChild(text(pad + 14, ry + 21 + i * LINE_H, ln,
          { 'font-size': 13, 'font-weight': 600, fill: ks.colour }));
      });
      g.appendChild(clickable(head, seq, root.label_full || label));

      var cy = y;
      children.forEach(function (c, ci) {
        var ch = chH[ci];
        var cks = kinds[c.type || 'topic'] || { colour: '#475569', label: '' };
        var midR = ry + rh / 2, midC = cy + ch / 2;
        g.appendChild(el('path', {
          d: 'M' + (pad + col1) + ' ' + Math.round(midR)
             + ' C' + Math.round(pad + col1 + gap / 2) + ' ' + Math.round(midR)
             + ', ' + Math.round(col2x - gap / 2) + ' ' + Math.round(midC)
             + ', ' + col2x + ' ' + Math.round(midC),
          fill: 'none', stroke: cks.colour, 'stroke-width': 1.4, opacity: 0.45
        }));
        var box = el('g');
        box.appendChild(el('rect', { x: col2x, y: cy, width: col2w, height: ch,
                                     rx: 7, fill: '#ffffff', stroke: cks.colour,
                                     'stroke-width': 1, opacity: 0.95 }));
        box.appendChild(el('rect', { x: col2x, y: cy, width: 3, height: ch,
                                     rx: 1.5, fill: cks.colour }));
        var cseq = (c.segment_ids && c.segment_ids[0] != null) ? c.segment_ids[0] : null;
        box.appendChild(text(col2x + 12, cy + 14,
          // **兩半都要翻，而且分隔符交給譯文決定** —— `cks.label` 是
          // 伺服器送來的中文（`data-kinds`），不包 `tr()` 的話英 / 日
          // 介面下會變成「待辦・Segment 2」這種一半一半的東西。
          (cseq != null
            ? tr('{0}・第 {1} 段').replace('{0}', tr(cks.label))
                                 .replace('{1}', cseq)
            : tr(cks.label)),
          { 'font-size': 10, fill: cks.colour }));
        wrap(c.label || '', itemChars).forEach(function (ln, i) {
          box.appendChild(text(col2x + 12, cy + 29 + i * LINE_H, ln,
            { 'font-size': 12.5, fill: '#1e293b' }));
        });
        g.appendChild(clickable(box, cseq, c.label_full || c.label || ''));
        cy += ch + 8;
      });
      y += blockH + 18;
    });

    var s = svgRoot(width, y + 4, tr('討論結構'));
    s.appendChild(g);
    return s;
  }

  // ------------------------------------------------------------ 章節佔比
  function timeline(chapters, width, palette, tr) {
    if (!chapters || chapters.length < 2) return null;
    // **`is not None` 不是真假值** —— 一章裡只有一個時間標記時長度是 0，
    // 用真假值判斷整張圖會退回用段數（伺服器端同一條）。
    var useTime = chapters.every(function (c) { return c.duration_ms != null; });
    var vals = chapters.map(function (c) {
      return useTime ? c.duration_ms : (c.segment_ids || []).length;
    });
    var total = vals.reduce(function (a, b) { return a + b; }, 0) || 1;

    // **標題與說明不畫在圖裡**，改放 HTML：
    //   ① SVG 裡放不了共用的圖示元件，而每個區塊都要有 icon
    //   ② 原本寫成 `tr(useTime ? 'A' : 'B')` —— **鍵是執行期算出來的，
    //      抽鍵的掃描器永遠看不到**，所以那兩句從來沒有被翻譯過
    //      （2026-09-19 使用者回報）。CLAUDE.md 記過這條：
    //      **要逐個分支各自包 `tr()`，不要包整個三元運算。**
    var barY = 4, barH = 26;
    var h = barY + barH + 18 + 22 * chapters.length;
    var s = svgRoot(width, h, tr('各議題佔多少時間'));
    var g = document.createDocumentFragment();
    var x = 0;
    chapters.forEach(function (c, i) {
      var w = Math.max(2, Math.round(width * vals[i] / total));
      var seq = (c.segment_ids || [])[0];
      var bar = el('g');
      bar.appendChild(el('rect', { x: x, y: barY, width: w, height: barH,
                                   fill: palette[i % palette.length] }));
      g.appendChild(clickable(bar, seq == null ? null : seq, c.title || '', i));
      x += w;
    });
    chapters.forEach(function (c, i) {
      var y = barY + barH + 22 + i * 22;
      var seq = (c.segment_ids || [])[0];
      var row = el('g');
      row.appendChild(el('rect', { x: 0, y: y - 9, width: 10, height: 10,
                                   rx: 2, fill: palette[i % palette.length] }));
      row.appendChild(text(18, y, c.title || '', { 'font-size': 13.5, fill: '#334155' }));
      // **數字要分欄靠右，不要串成一句**（2026-09-19 回報「排列不整齊」）——
      // 串成一句之後整串一起靠右，前面那個百分比就會被後面那欄的長度推著跑
      // （`9.0%` 與 `13.8%` 差一個字，整欄就歪了）。
      row.appendChild(text(width - 58, y, (vals[i] * 100 / total).toFixed(1) + '%',
        { 'font-size': 12.5, 'text-anchor': 'end', fill: '#475569',
          'class': 'mc-num' }));
      row.appendChild(text(width, y,
        useTime ? mmss(c.start_ms)
                : tr('第 {0} 段起').replace('{0}', seq == null ? 0 : seq),
        { 'font-size': 12.5, 'text-anchor': 'end', fill: '#475569',
          'class': 'mc-num' }));
      g.appendChild(clickable(row, seq == null ? null : seq, c.title || '', i));
    });
    s.appendChild(g);
    return wireHover(s);
  }

  /* 容器量得到的寬度。隱藏中就往上找，最後才退到預設值。 */
  function measure(box) {
    var n = box;
    while (n && n.getBoundingClientRect) {
      var w = n.clientWidth || Math.round(n.getBoundingClientRect().width);
      if (w > 0) {
        // 扣掉這一層到 box 之間的左右內距，不然圖會比容器寬一點點。
        if (n !== box) {
          var cs = window.getComputedStyle ? getComputedStyle(n) : null;
          if (cs) w -= (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
        }
        return Math.max(360, w);
      }
      n = n.parentElement;
    }
    return 760;
  }

  // ------------------------------------------------------------------ API
  function render(opts) {
    var tr = opts.tr || function (s) { return s; };
    var palette = opts.palette, kinds = opts.kinds;
    var jobs = [
      ['mindmap', opts.into.mindmap, function (w) {
        return mindmap(opts.data.mindmap, w, kinds, tr); }],
      ['timeline', opts.into.timeline, function (w) {
        return timeline(opts.data.chapters, w, palette, tr); }]

    ];
    var drawn = [];
    jobs.forEach(function (j) {
      var box = j[1];
      if (!box) return;
      var made = null;
      // **寬度要量容器不是量視窗** —— 側欄收合、視窗縮放都會變。
      //
      // ⚠ 容器（或它的某個祖先）可能**還是 `hidden`**，那時 `clientWidth`
      // 是 0。往上找到第一個量得到寬度的祖先才準。
      // 2026-09-19 踩到：心智圖那一區是「畫完才決定要不要顯示」，
      // 量的當下整區隱藏 → 0 → 退到預設值 → 圖被 CSS 撐大 1.7 倍。
      var w = Math.max(360, Math.round(measure(box)));
      var node = j[2](w);
      box.replaceChildren();
      if (!node) return;
      box.appendChild(node);
      made = node;
      drawn.push(j[0]);

      // **畫完量一次，差太多就照實際寬度重畫。**
      //
      // 圖是照像素畫的，CSS 只留 `max-width:100%` 當安全網 —— 但安全網一旦
      // 真的生效就是**整張圖連字一起縮**，使用者看到的是「字怎麼變這麼小」
      // （2026-09-19 回報）。反過來量太窄就會被撐大（同一天的另一份回報）。
      //
      // 量錯的來源很多（容器還在攤開、字型還沒載完、捲軸出現讓寬度少 15px），
      // 與其一個一個堵，不如**畫完照鏡子**：真實寬度跟畫的差超過 2% 就重畫
      // 一次。只重畫一次，不遞迴 —— 不然兩個寬度互相推來推去會停不下來。
      var real = Math.round(made.getBoundingClientRect().width);
      if (real > 60 && Math.abs(real - w) > w * 0.02) {
        var again = j[2](Math.max(360, real));
        if (again) { box.replaceChildren(); box.appendChild(again); }
      }
    });
    return drawn;
  }

  window.MeetingCharts = { render: render };
})();
