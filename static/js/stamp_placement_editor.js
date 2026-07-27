// StampPlacementEditor — pdf-stamp「每頁獨立位置」模式的多頁多物件編輯器
// （issue #38 / Phase B）。
//
// 與共用的 DragPositionEditor 的差異、也是**另開一支檔案**的原因：
//   * DragPositionEditor = 單頁、單一物件、一組 x/y/w/h（watermark / asset_edit
//     也在用）→ 不能為了本功能改它，否則會波及那兩處。
//   * 本編輯器 = 多頁、每頁可多個物件（placements），並支援點擊放置、選取、
//     Delete 刪除、複製到其他頁。
//
// 座標模型與 DragPositionEditor 一致：左上原點、單位 mm，方便共用後端。
// 對外：new StampPlacementEditor(opts) / .getValue() → placements 陣列。
(function () {
  const SNAP_MM = 1.5;
  let _seq = 0;

  class StampPlacementEditor {
    /**
     * opts:
     *   root        容器元素（內含 .spe-* 骨架，見 pdf_stamp.html）
     *   pageCount   總頁數
     *   pagesDims   [{w_mm,h_mm}, ...]（各頁尺寸；缺則沿用第一頁）
     *   bgUrlFor(i) 回第 i 頁背景圖 URL（0-based）
     *   assetUrl    主印章 / 簽名圖 URL（放置時預設用它）
     *   defaultSize {width_mm, height_mm}
     *   overlayFor(kind)  'date' / 'restrict' 的圖與尺寸（未啟用回 null）
     *   onChange()  placements 有變動時回呼
     */
    constructor(opts) {
      this.root = opts.root;
      // 綁定來源檔案：切換模式時用來判斷可否沿用（見模板 ensurePlacementEditor）
      this.uploadId = opts.uploadId || null;
      this.pageCount = Math.max(1, opts.pageCount || 1);
      this.pagesDims = opts.pagesDims || [];
      this.bgUrlFor = opts.bgUrlFor || (() => null);
      this.assetUrl = opts.assetUrl || '';
      // currentAsset()：放置當下才呼叫，回 {url, asset_id, png_b64, width_mm,
      // height_mm} → 支援「先選大章放幾個、改選小章再放」的混用工作流。
      this.currentAsset = opts.currentAsset || null;
      // overlayFor(kind)：日期(1b) / 個資限用章(1c) 的圖與尺寸來源，
      // 回 {url, png_b64, width_mm, height_mm, assetKey} 或 null（未啟用）。
      this.overlayFor = opts.overlayFor || null;
      // onKindUnavailable(kind)：使用者點了尚未啟用的種類時的引導動作
      this.onKindUnavailable = opts.onKindUnavailable || null;
      this._kindOk = { stamp: true };
      this.defaultSize = opts.defaultSize || { width_mm: 30, height_mm: 30 };
      this.onChange = opts.onChange || (() => {});

      this.placements = [];      // {id, page, kind, x_mm, y_mm, width_mm, height_mm, rotation_deg, url}
      this.page = 0;             // 目前編輯頁（0-based）
      this.selectedId = null;
      this.zoom = 1;

      this.$paper = this.root.querySelector('.spe-paper');
      this.$bg = this.root.querySelector('.spe-bg');
      this.$layer = this.root.querySelector('.spe-layer');
      this.$pageLabel = this.root.querySelector('.spe-page-label');
      this.$prev = this.root.querySelector('.spe-prev');
      this.$next = this.root.querySelector('.spe-next');
      this.$thumbs = this.root.querySelector('.spe-thumbs');
      this.$count = this.root.querySelector('.spe-count');
      this.$btnDel = this.root.querySelector('.spe-del');
      this.$btnClearPage = this.root.querySelector('.spe-clear-page');
      this.$btnCopyAll = this.root.querySelector('.spe-copy-all');
      this.$guideV = this.root.querySelector('.spe-guide.v');
      this.$guideH = this.root.querySelector('.spe-guide.h');
      this.$kindBtns = this.root.querySelectorAll('.spe-kind-btn');
      this.kind = 'stamp';          // 目前要放置的種類：stamp / date / restrict
      this.$rotBox = this.root.querySelector('.spe-rot');
      this.$rotInput = this.root.querySelector('.spe-rot-input');
      this.$rotReset = this.root.querySelector('.spe-rot-reset');
      this.$rotRand = this.root.querySelectorAll('.spe-rot-rand');

      this._bind();
      this._buildThumbs();
      this.goToPage(0);
      window.addEventListener('resize', () => this._relayout());
    }

    // ---- 頁面 ----------------------------------------------------------
    dimsOf(i) {
      const d = this.pagesDims[i] || this.pagesDims[0] || {};
      return { w: d.w_mm || 210, h: d.h_mm || 297 };
    }

    goToPage(i) {
      this.page = Math.min(this.pageCount - 1, Math.max(0, i));
      this._loadBg(this.page);
      if (this.$pageLabel)
        this.$pageLabel.textContent = `第 ${this.page + 1} / ${this.pageCount} 頁`;
      if (this.$prev) this.$prev.disabled = (this.page === 0);
      if (this.$next) this.$next.disabled = (this.page >= this.pageCount - 1);
      this._syncThumbs();
      this._relayout();
    }

    /** 載入第 i 頁背景。bgUrlFor(i) 回的是 **API 端點**（/preview-bg/…），它回
     *  JSON `{preview_url}`（不是圖片本身）→ 要先 fetch 再把 preview_url 給 <img>。
     *  （踩過：直接把端點塞進 img.src 會破圖。） */
    async _loadBg(i) {
      const api = this.bgUrlFor(i);
      if (!api) return;
      try {
        const r = await fetch(api);
        if (!r.ok) return;
        const j = await r.json();
        if (i !== this.page) return;              // 期間又換頁了
        const u = (j.preview_url || '') + '?t=' + Date.now();
        this.$bg.src = window.safeImgSrc ? window.safeImgSrc(u) : u;
      } catch (_e) { /* 背景載不到不影響放置功能 */ }
    }

    _buildThumbs() {
      if (!this.$thumbs) return;
      this.$thumbs.textContent = '';
      for (let i = 0; i < this.pageCount; i++) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'spe-thumb';
        b.dataset.page = String(i);
        b.textContent = String(i + 1);
        b.title = `第 ${i + 1} 頁`;
        b.addEventListener('click', () => this.goToPage(i));
        this.$thumbs.appendChild(b);
      }
    }

    _syncThumbs() {
      if (!this.$thumbs) return;
      this.$thumbs.querySelectorAll('.spe-thumb').forEach((b) => {
        const p = +b.dataset.page;
        b.classList.toggle('active', p === this.page);
        const n = this.placements.filter(x => x.page === p).length;
        b.classList.toggle('has-items', n > 0);
        b.title = n ? `第 ${p + 1} 頁（${n} 個）` : `第 ${p + 1} 頁`;
      });
    }

    _relayout() {
      const dims = this.dimsOf(this.page);
      const wrap = this.$paper.parentElement;
      // 精確扣掉容器的 padding（clientWidth/Height 含 padding），否則紙張會比可用
      // 空間多出 padding 的量 → zoom=1 也出現捲軸（原本只扣 8，實際上下共 16）。
      const cs = getComputedStyle(wrap);
      const padX = parseFloat(cs.paddingLeft || 0) + parseFloat(cs.paddingRight || 0);
      const padY = parseFloat(cs.paddingTop || 0) + parseFloat(cs.paddingBottom || 0);
      const availW = Math.max(120, wrap.clientWidth - padX - 2);
      const availH = Math.max(160, wrap.clientHeight - padY - 2);
      const scale = Math.min(availW / dims.w, availH / dims.h);
      const pw = dims.w * scale * this.zoom;
      const ph = dims.h * scale * this.zoom;
      this.$paper.style.width = pw + 'px';
      this.$paper.style.height = ph + 'px';
      this.mmPerPx = dims.w / pw;
      this._renderAll();
    }

    _mmToPx(mm) { return mm / this.mmPerPx; }
    _pxToMm(px) { return px * this.mmPerPx; }

    // ---- placements ----------------------------------------------------
    add(x_mm, y_mm, opts) {
      opts = opts || {};
      // 每次放置都取「**當下選取的**印章 / 簽名」→ 可以先放大章、改選小章再放，
      // 同一頁混用不同章（各自帶自己的圖與預設尺寸）。
      const kind = opts.kind || this.kind || 'stamp';
      // date / restrict 的圖與尺寸來自 1b / 1c 面板（overlayFor 由外部注入）；
      // stamp 則取目前選取的印章 / 簽名。
      const cur = (kind !== 'stamp' && typeof this.overlayFor === 'function')
        ? (this.overlayFor(kind) || {})
        : ((typeof this.currentAsset === 'function') ? (this.currentAsset() || {}) : {});
      if (kind !== 'stamp' && !cur.png_b64) {
        if (window.showToast) window.showToast(
          kind === 'date' ? '請先在上方「1b. 插入日期」啟用並設定內容'
                          : '請先在上方「1c. 個資限用章」啟用並設定內容', 'err');
        return null;
      }
      const dims = this.dimsOf(this.page);
      const w = opts.width_mm || cur.width_mm || this.defaultSize.width_mm;
      const h = opts.height_mm || cur.height_mm || this.defaultSize.height_mm;
      const p = {
        id: 'p' + (++_seq),
        page: this.page,
        kind: kind,
        // 以點擊處為中心放置
        x_mm: Math.min(Math.max(0, x_mm - w / 2), Math.max(0, dims.w - w)),
        y_mm: Math.min(Math.max(0, y_mm - h / 2), Math.max(0, dims.h - h)),
        width_mm: w,
        height_mm: h,
        rotation_deg: opts.rotation_deg || 0,
        // 每個 placement 記住自己的圖：asset_id（共用資產）或 png_b64（臨時資產），
        // 後端據此各自取圖 → 同一份 PDF 可混用多種印章。
        url: opts.url || cur.url || this.assetUrl,
        asset_id: opts.asset_id || cur.asset_id || null,
        png_b64: opts.png_b64 || cur.png_b64 || null,
        // 用來把「同一個資產」的多個 placement 一起換成預載好的 objectURL
        assetKey: cur.assetKey || opts.asset_id || cur.asset_id || '__cur__',
      };
      this.placements.push(p);
      this.selectedId = p.id;
      this._changed();
      return p;
    }

    // 某個資產的圖已預載成 objectURL → 把用到它的 placement 換成該 URL 並重繪。
    // （伺服器對資產圖送 no-store，每個 img 都會各自重新下載，物件一多就要等
    //   好幾秒才顯示；預載一次共用可完全避免。）
    refreshAssetUrl(key, url) {
      if (!key || !url) return;
      let hit = false;
      for (const p of this.placements) {
        if (p.assetKey === key && p.url !== url) { p.url = url; hit = true; }
      }
      if (this.assetKey === key) this.assetUrl = url;
      if (hit) this._renderAll();
    }

    remove(id) {
      const n = this.placements.length;
      this.placements = this.placements.filter(p => p.id !== id);
      if (this.selectedId === id) this.selectedId = null;
      if (this.placements.length !== n) this._changed();
    }

    clearPage(page) {
      const p = (page == null) ? this.page : page;
      const n = this.placements.length;
      this.placements = this.placements.filter(x => x.page !== p);
      if (this.placements.length !== n) { this.selectedId = null; this._changed(); }
    }

    clearAll() {
      if (!this.placements.length) return;
      this.placements = [];
      this.selectedId = null;
      this._changed();
    }

    /** 把目前頁的物件複製到其他所有頁（同座標）。 */
    copyPageToAll() {
      const mine = this.placements.filter(p => p.page === this.page);
      if (!mine.length) return 0;
      let added = 0;
      for (let i = 0; i < this.pageCount; i++) {
        if (i === this.page) continue;
        for (const src of mine) {
          this.placements.push(Object.assign({}, src, {
            id: 'p' + (++_seq), page: i,
          }));
          added++;
        }
      }
      if (added) this._changed();
      return added;
    }

    getValue() {
      return this.placements.map(p => ({
        page: p.page,
        kind: p.kind,
        x_mm: +p.x_mm.toFixed(2),
        y_mm: +p.y_mm.toFixed(2),
        width_mm: +p.width_mm.toFixed(2),
        height_mm: +p.height_mm.toFixed(2),
        rotation_deg: +(p.rotation_deg || 0).toFixed(2),
        asset_id: p.asset_id || undefined,
        png_b64: p.asset_id ? undefined : (p.png_b64 || undefined),
      }));
    }

    /** 旋轉面板：只在有選取物件時顯示，數值跟著選取的物件走。 */
    _syncRot() {
      if (!this.$rotBox) return;
      const p = this.selectedId ? this._find(this.selectedId) : null;
      this.$rotBox.hidden = !p;
      if (p && this.$rotInput && document.activeElement !== this.$rotInput) {
        this.$rotInput.value = (p.rotation_deg || 0).toFixed(1);
      }
    }

    setRotation(deg) {
      const p = this.selectedId ? this._find(this.selectedId) : null;
      if (!p) return;
      let d = Number(deg);
      if (!isFinite(d)) d = 0;
      d = ((d % 360) + 360) % 360;          // 正規化到 0~360
      p.rotation_deg = d;
      this._changed();
    }

    _changed() {
      this._renderAll();
      this._syncThumbs();
      this._syncRot();
      if (this.$count)
        this.$count.textContent = `${this.placements.length} 個物件`;
      this.onChange(this.getValue());
    }

    // ---- 繪製 ----------------------------------------------------------
    _renderAll() {
      if (!this.$layer) return;
      // 重用既有的 DOM（依 placement id）：拖曳時每次 mousemove 都會重繪，若每次
      // 都重建 <img> 會讓圖片反覆重新載入 → 看起來是空框（實測換第二個章後很明顯）。
      const reuse = new Map();
      for (const el of this.$layer.querySelectorAll('.spe-item')) {
        if (el.dataset.id) reuse.set(el.dataset.id, el);
      }
      this.$layer.textContent = '';
      for (const p of this.placements) {
        if (p.page !== this.page) continue;
        const old = reuse.get(p.id);
        if (old) {                      // 只更新位置 / 選取狀態，圖片節點原封不動
          old.className = 'spe-item' + (p.id === this.selectedId ? ' selected' : '')
            + ' kind-' + p.kind;
          old.style.left = this._mmToPx(p.x_mm) + 'px';
          old.style.top = this._mmToPx(p.y_mm) + 'px';
          old.style.width = this._mmToPx(p.width_mm) + 'px';
          old.style.height = this._mmToPx(p.height_mm) + 'px';
          old.style.transform = `rotate(${p.rotation_deg || 0}deg)`;
          this.$layer.appendChild(old);
          continue;
        }
        const el = document.createElement('div');
        el.className = 'spe-item' + (p.id === this.selectedId ? ' selected' : '')
          + ' kind-' + p.kind;
        el.dataset.id = p.id;
        el.style.left = this._mmToPx(p.x_mm) + 'px';
        el.style.top = this._mmToPx(p.y_mm) + 'px';
        el.style.width = this._mmToPx(p.width_mm) + 'px';
        el.style.height = this._mmToPx(p.height_mm) + 'px';
        el.style.transform = `rotate(${p.rotation_deg || 0}deg)`;
        el.style.transformOrigin = '50% 50%';
        if (p.url) {
          const img = document.createElement('img');
          img.src = window.safeImgSrc ? window.safeImgSrc(p.url) : p.url;
          img.alt = '';
          img.draggable = false;
          el.appendChild(img);
        }
        // 四角把手（與「統一位置」的 DragPositionEditor 手感一致）
        for (const c of ['nw', 'ne', 'sw', 'se']) {
          const hd = document.createElement('div');
          hd.className = 'spe-handle ' + c;
          hd.dataset.h = c;
          el.appendChild(hd);
        }
        this.$layer.appendChild(el);
      }
    }

    _clampItem(p) {
      const dims = this.dimsOf(p.page);
      if (p.width_mm < 3) p.width_mm = 3;
      if (p.height_mm < 3) p.height_mm = 3;
      if (p.width_mm > dims.w) p.width_mm = dims.w;
      if (p.height_mm > dims.h) p.height_mm = dims.h;
      if (p.x_mm < 0) p.x_mm = 0;
      if (p.y_mm < 0) p.y_mm = 0;
      if (p.x_mm + p.width_mm > dims.w) p.x_mm = dims.w - p.width_mm;
      if (p.y_mm + p.height_mm > dims.h) p.y_mm = dims.h - p.height_mm;
    }

    _snap(mm, candidates) {
      for (const c of candidates)
        if (Math.abs(mm - c) < SNAP_MM) return { mm: c, hit: true };
      return { mm, hit: false };
    }

    _showGuides(v, h) {
      if (!this.$guideV || !this.$guideH) return;
      if (v != null) { this.$guideV.hidden = false; this.$guideV.style.left = this._mmToPx(v) + 'px'; }
      else this.$guideV.hidden = true;
      if (h != null) { this.$guideH.hidden = false; this.$guideH.style.top = this._mmToPx(h) + 'px'; }
      else this.$guideH.hidden = true;
    }

    _find(id) { return this.placements.find(p => p.id === id) || null; }

    // ---- 互動 ----------------------------------------------------------
    /** 切換要放置的種類（印章 / 日期 / 個資限用章）。 */
    setKind(kind) {
      this.kind = kind || 'stamp';
      (this.$kindBtns || []).forEach((b) => {
        b.classList.toggle('active', b.dataset.kind === this.kind);
        b.setAttribute('aria-pressed', b.dataset.kind === this.kind ? 'true' : 'false');
      });
    }

    /** 標記某個種類目前可不可用（1b / 1c 尚未啟用時）。

     *  **刻意不用 disabled**：灰掉的按鈕按不下去、`title` 提示在多數瀏覽器也不會
     *  顯示，使用者只看到「不能按」卻不知道要去哪啟用。改成仍可點擊，點下去直接
     *  把對應的 1b / 1c 區塊展開並捲過去（由 onKindUnavailable 處理）。 */
    setKindAvailable(kind, ok) {
      this._kindOk = this._kindOk || {};
      this._kindOk[kind] = !!ok;
      let fellBack = false;
      (this.$kindBtns || []).forEach((b) => {
        if (b.dataset.kind !== kind) return;
        b.classList.toggle('needs-setup', !ok);
        b.title = ok ? '' : (kind === 'date'
          ? '尚未啟用 — 點一下前往「1b. 插入日期」設定'
          : '尚未啟用 — 點一下前往「1c. 個資限用章」設定');
        if (!ok && this.kind === kind) fellBack = true;
      });
      if (fellBack) this.setKind('stamp');
    }

    kindAvailable(kind) {
      if (kind === 'stamp') return true;
      return !!(this._kindOk && this._kindOk[kind]);
    }

    _bind() {
      // 0) 種類切換（印章 / 簽名、插入日期、個資限用章）
      (this.$kindBtns || []).forEach((b) => {
        b.addEventListener('click', () => {
          const k = b.dataset.kind;
          // 尚未啟用 → 不是「按不動」，而是帶使用者去啟用的地方
          if (!this.kindAvailable(k)) {
            if (typeof this.onKindUnavailable === 'function') this.onKindUnavailable(k);
            return;
          }
          this.setKind(k);
        });
      });
      // 1) 點擊紙面空白處 → 在該點放置一個物件（issue #38 需求 2）
      this.$paper.addEventListener('pointerdown', (e) => {
        const item = e.target.closest('.spe-item');
        if (item) return;                       // 點在物件上 → 交給物件的處理
        // 印章 / 簽名要先選圖才放；日期 / 個資限用章的圖來自 1b / 1c 面板，
        // 由 add() 自行檢查（沒啟用會跳提示）。
        if ((this.kind || 'stamp') === 'stamp' && !this.assetUrl) return;
        const r = this.$paper.getBoundingClientRect();
        this.add(this._pxToMm(e.clientX - r.left), this._pxToMm(e.clientY - r.top));
      });

      // 2) 物件拖曳 / 縮放 / 選取（事件委派，物件是動態產生的）
      this.$layer.addEventListener('pointerdown', (e) => {
        const el = e.target.closest('.spe-item');
        if (!el) return;
        e.stopPropagation();
        const p = this._find(el.dataset.id);
        if (!p) return;
        this.selectedId = p.id;
        this._renderAll();
        this._syncRot();          // 旋轉面板跟著選取的物件

        const isResize = e.target.classList.contains('spe-handle');
        const corner = e.target.dataset ? e.target.dataset.h : null;   // nw/ne/sw/se
        const startX = e.clientX, startY = e.clientY;
        const o = { x: p.x_mm, y: p.y_mm, w: p.width_mm, h: p.height_mm };
        const aspect = (o.w / o.h) || 1;
        const target = this.$layer.querySelector(`.spe-item[data-id="${p.id}"]`);
        if (target) target.setPointerCapture(e.pointerId);

        const move = (ev) => {
          const dx = this._pxToMm(ev.clientX - startX);
          const dy = this._pxToMm(ev.clientY - startY);
          if (isResize) {
            // 四角縮放（與「統一位置」編輯器同樣的數學）：拉左 / 上側時要同步
            // 位移 x / y，物件的對角才會固定不動。預設鎖長寬比，按住 Shift 自由。
            let nx = o.x, ny = o.y, nw = o.w, nh = o.h;
            if (corner === 'se') { nw = o.w + dx; nh = o.h + dy; }
            else if (corner === 'ne') { nw = o.w + dx; nh = o.h - dy; ny = o.y + dy; }
            else if (corner === 'sw') { nw = o.w - dx; nh = o.h + dy; nx = o.x + dx; }
            else if (corner === 'nw') { nw = o.w - dx; nh = o.h - dy; nx = o.x + dx; ny = o.y + dy; }
            else { nw = o.w + dx; nh = o.h + dy; }         // 無 corner 資訊 → 當右下
            if (!ev.shiftKey && aspect) {                   // 鎖長寬比：以寬為準
              const fixedH = nw / aspect;
              if (corner === 'nw' || corner === 'ne') ny = o.y + (o.h - fixedH);
              nh = fixedH;
            }
            p.x_mm = nx; p.y_mm = ny; p.width_mm = nw; p.height_mm = nh;
          } else {
            const dims = this.dimsOf(p.page);
            let nx = o.x + dx, ny = o.y + dy;
            const cx = dims.w / 2 - p.width_mm / 2;
            const cy = dims.h / 2 - p.height_mm / 2;
            const sx = this._snap(nx, [0, cx, dims.w - p.width_mm]);
            const sy = this._snap(ny, [0, cy, dims.h - p.height_mm]);
            nx = sx.mm; ny = sy.mm;
            p.x_mm = nx; p.y_mm = ny;
            this._showGuides(sx.hit ? nx + p.width_mm / 2 : null,
                             sy.hit ? ny + p.height_mm / 2 : null);
          }
          this._clampItem(p);
          this._renderAll();
        };
        const up = () => {
          window.removeEventListener('pointermove', move);
          window.removeEventListener('pointerup', up);
          this._showGuides(null, null);
          this._changed();
        };
        window.addEventListener('pointermove', move);
        window.addEventListener('pointerup', up);
      });

      // 3) Delete / Backspace 刪除選取物件（issue #38 需求 3）
      //    在輸入框 / 可編輯區內不攔截，避免誤刪使用者正在打的字。
      this._onKey = (e) => {
        if (e.key !== 'Delete' && e.key !== 'Backspace') return;
        const t = e.target;
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
                  || t.tagName === 'SELECT' || t.isContentEditable)) return;
        if (!this.selectedId) return;
        if (!this.root.offsetParent) return;    // 編輯器沒顯示時不作用
        e.preventDefault();
        this.remove(this.selectedId);
      };
      document.addEventListener('keydown', this._onKey);

      // 4) 頁面導覽 / 工具鈕
      if (this.$prev) this.$prev.addEventListener('click', () => this.goToPage(this.page - 1));
      if (this.$next) this.$next.addEventListener('click', () => this.goToPage(this.page + 1));
      if (this.$btnDel) this.$btnDel.addEventListener('click', () => {
        if (this.selectedId) this.remove(this.selectedId);
      });
      // 旋轉：輸入框即時套用；歸零 / 隨機角度（仿真手蓋章的自然歪斜）
      if (this.$rotInput) {
        this.$rotInput.addEventListener('input', () => this.setRotation(this.$rotInput.value));
      }
      if (this.$rotReset) this.$rotReset.addEventListener('click', () => this.setRotation(0));
      if (this.$rotRand) this.$rotRand.forEach((b) => {
        b.addEventListener('click', () => {
          const r = parseFloat(b.dataset.range || '3');
          this.setRotation((Math.random() * 2 - 1) * r);
        });
      });
      if (this.$btnClearPage) this.$btnClearPage.addEventListener('click', () => this.clearPage());
      if (this.$btnCopyAll) this.$btnCopyAll.addEventListener('click', () => {
        const n = this.copyPageToAll();
        if (window.showToast) {
          window.showToast(n ? `已複製 ${n} 個物件到其他頁` : '目前頁沒有物件可複製',
                           n ? 'ok' : 'err');
        }
      });
    }

    /** 重新計算版面（切換顯示 / 視窗大小改變後呼叫）。物件與選取狀態都保留。 */
    relayout() { this._relayout(); }

    destroy() {
      if (this._onKey) document.removeEventListener('keydown', this._onKey);
      // 清掉自己畫在共用容器裡的物件，否則重建後舊 DOM 會留下變成空框
      if (this.$layer) this.$layer.textContent = '';
      if (this.$thumbs) this.$thumbs.textContent = '';
    }
  }

  window.StampPlacementEditor = StampPlacementEditor;
})();
