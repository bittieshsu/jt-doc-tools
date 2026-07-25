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
     *   onChange()  placements 有變動時回呼
     */
    constructor(opts) {
      this.root = opts.root;
      this.pageCount = Math.max(1, opts.pageCount || 1);
      this.pagesDims = opts.pagesDims || [];
      this.bgUrlFor = opts.bgUrlFor || (() => null);
      this.assetUrl = opts.assetUrl || '';
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
      const url = this.bgUrlFor(this.page);
      if (url && window.safeImgSrc) this.$bg.src = window.safeImgSrc(url);
      else if (url) this.$bg.src = url;
      if (this.$pageLabel)
        this.$pageLabel.textContent = `第 ${this.page + 1} / ${this.pageCount} 頁`;
      if (this.$prev) this.$prev.disabled = (this.page === 0);
      if (this.$next) this.$next.disabled = (this.page >= this.pageCount - 1);
      this._syncThumbs();
      this._relayout();
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
      const availW = Math.max(120, wrap.clientWidth - 8);
      const availH = Math.max(160, wrap.clientHeight - 8);
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
      const dims = this.dimsOf(this.page);
      const w = opts.width_mm || this.defaultSize.width_mm;
      const h = opts.height_mm || this.defaultSize.height_mm;
      const p = {
        id: 'p' + (++_seq),
        page: this.page,
        kind: opts.kind || 'stamp',
        // 以點擊處為中心放置
        x_mm: Math.min(Math.max(0, x_mm - w / 2), Math.max(0, dims.w - w)),
        y_mm: Math.min(Math.max(0, y_mm - h / 2), Math.max(0, dims.h - h)),
        width_mm: w,
        height_mm: h,
        rotation_deg: opts.rotation_deg || 0,
        url: opts.url || this.assetUrl,
        png_b64: opts.png_b64 || null,
      };
      this.placements.push(p);
      this.selectedId = p.id;
      this._changed();
      return p;
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
        png_b64: p.png_b64 || undefined,
      }));
    }

    _changed() {
      this._renderAll();
      this._syncThumbs();
      if (this.$count)
        this.$count.textContent = `${this.placements.length} 個物件`;
      this.onChange(this.getValue());
    }

    // ---- 繪製 ----------------------------------------------------------
    _renderAll() {
      if (!this.$layer) return;
      this.$layer.textContent = '';
      for (const p of this.placements) {
        if (p.page !== this.page) continue;
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
        const hd = document.createElement('div');
        hd.className = 'spe-handle';
        el.appendChild(hd);
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
    _bind() {
      // 1) 點擊紙面空白處 → 在該點放置一個物件（issue #38 需求 2）
      this.$paper.addEventListener('pointerdown', (e) => {
        const item = e.target.closest('.spe-item');
        if (item) return;                       // 點在物件上 → 交給物件的處理
        if (!this.assetUrl) return;             // 沒選圖不放
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

        const isResize = e.target.classList.contains('spe-handle');
        const startX = e.clientX, startY = e.clientY;
        const o = { x: p.x_mm, y: p.y_mm, w: p.width_mm, h: p.height_mm };
        const aspect = (o.w / o.h) || 1;
        const target = this.$layer.querySelector(`.spe-item[data-id="${p.id}"]`);
        if (target) target.setPointerCapture(e.pointerId);

        const move = (ev) => {
          const dx = this._pxToMm(ev.clientX - startX);
          const dy = this._pxToMm(ev.clientY - startY);
          if (isResize) {
            p.width_mm = o.w + dx;
            p.height_mm = ev.shiftKey ? (o.h + dy) : (p.width_mm / aspect);
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
      if (this.$btnClearPage) this.$btnClearPage.addEventListener('click', () => this.clearPage());
      if (this.$btnCopyAll) this.$btnCopyAll.addEventListener('click', () => {
        const n = this.copyPageToAll();
        if (window.showToast) {
          window.showToast(n ? `已複製 ${n} 個物件到其他頁` : '目前頁沒有物件可複製',
                           n ? 'ok' : 'err');
        }
      });
    }

    destroy() {
      if (this._onKey) document.removeEventListener('keydown', this._onKey);
    }
  }

  window.StampPlacementEditor = StampPlacementEditor;
})();
