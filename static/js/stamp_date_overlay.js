// StampDateOverlay: a 2nd draggable / resizable overlay that lives on the
// same paper element as DragPositionEditor. Used by pdf-stamp tool to place
// a handwriting-style date image alongside the main stamp.
//
// Coordinates: mm, top-left origin. Shares mmPerPx with the primary editor.
(function () {
  class StampDateOverlay {
    constructor(opts) {
      this.editor = opts.editor;           // DragPositionEditor instance (primary)
      this.png_src = opts.png_src;         // data URL or http
      this.onChange = opts.onChange || (() => {});
      this.value = Object.assign({
        x_mm: 150, y_mm: 250, width_mm: 50, height_mm: 12, rotation_deg: 0,
      }, opts.value || {});
      this.aspect = this.value.width_mm / Math.max(1, this.value.height_mm);
      // 可選自訂邊框 / 背景色，給不同類型的 overlay 視覺區分
      this.borderColor = opts.borderColor || '#10b981';
      this.bgColor = opts.bgColor || 'rgba(16, 185, 129, 0.05)';
      this._mounted = false;
      this._mount();
      // Re-render when primary editor relayouts (paper resized / paper changed)
      this.editor.onRelayout(() => this._render());
    }

    _mount() {
      const paperEl = this.editor.getPaperEl();
      this.el = document.createElement('div');
      this.el.className = 'dpe-asset dpe-asset-date';
      this.el.style.zIndex = '5';
      this.el.style.border = '2px dashed ' + this.borderColor;
      this.el.style.background = this.bgColor;
      // **不可以用字串拼 `style="…"`**：CSP 的 style-src 已移除
      // 'unsafe-inline'，那個屬性會被瀏覽器丟掉（字串留在 HTML 裡，但
      // `el.style.length === 0`）。這裡目前剛好被 `.dpe-asset img` 的 CSS
      // 規則補回來所以畫面沒壞，但每次掛載都在 console 留一筆 CSP 違規，
      // 而且下一個人照抄就不一定有那條 CSS 兜著。改用 DOM API。
      this.el.textContent = '';
      const img = document.createElement('img');
      img.alt = 'date';
      img.style.width = '100%';
      img.style.height = '100%';
      img.style.pointerEvents = 'none';
      this.el.appendChild(img);
      ['nw', 'ne', 'sw', 'se'].forEach(h => {
        const g = document.createElement('div');
        g.className = 'dpe-handle ' + h;
        g.dataset.h = h;
        this.el.appendChild(g);
      });
      this.$img = img;
      this.$img.src = this.png_src;
      paperEl.appendChild(this.el);
      this._bindDrag();
      this._render();
      this._mounted = true;
    }

    _mmPerPx() { return this.editor.getMmPerPx() || 1; }
    _mmToPx(mm) { return mm / this._mmPerPx(); }
    _pxToMm(px) { return px * this._mmPerPx(); }

    _render() {
      if (!this.el) return;
      const left = this._mmToPx(this.value.x_mm);
      const top = this._mmToPx(this.value.y_mm);
      const w = this._mmToPx(this.value.width_mm);
      const h = this._mmToPx(this.value.height_mm);
      this.el.style.left = left + 'px';
      this.el.style.top = top + 'px';
      this.el.style.width = w + 'px';
      this.el.style.height = h + 'px';
      this.el.style.transform = `rotate(${this.value.rotation_deg || 0}deg)`;
      this.el.style.transformOrigin = '50% 50%';
    }

    _clamp() {
      const p = this.editor.getPaper();
      if (this.value.width_mm < 5) this.value.width_mm = 5;
      if (this.value.height_mm < 3) this.value.height_mm = 3;
      if (this.value.width_mm > p.w) this.value.width_mm = p.w;
      if (this.value.height_mm > p.h) this.value.height_mm = p.h;
      if (this.value.x_mm < 0) this.value.x_mm = 0;
      if (this.value.y_mm < 0) this.value.y_mm = 0;
      if (this.value.x_mm + this.value.width_mm > p.w)
        this.value.x_mm = p.w - this.value.width_mm;
      if (this.value.y_mm + this.value.height_mm > p.h)
        this.value.y_mm = p.h - this.value.height_mm;
    }

    _emit() { this.onChange(this.getValue()); }

    _bindDrag() {
      // Drag (anywhere except handles)
      this.el.addEventListener('pointerdown', (e) => {
        if (e.target.classList.contains('dpe-handle')) return;
        e.preventDefault();
        this.el.setPointerCapture(e.pointerId);
        const startX = e.clientX, startY = e.clientY;
        const origX = this.value.x_mm, origY = this.value.y_mm;
        const move = (ev) => {
          this.value.x_mm = origX + this._pxToMm(ev.clientX - startX);
          this.value.y_mm = origY + this._pxToMm(ev.clientY - startY);
          this._clamp(); this._render();
        };
        const up = () => {
          this.el.removeEventListener('pointermove', move);
          this.el.removeEventListener('pointerup', up);
          this.el.removeEventListener('pointercancel', up);
          this._emit();
        };
        this.el.addEventListener('pointermove', move);
        this.el.addEventListener('pointerup', up);
        this.el.addEventListener('pointercancel', up);
      });
      // Resize on corner handles (aspect locked by default for date)
      this.el.querySelectorAll('.dpe-handle').forEach((h) => {
        h.addEventListener('pointerdown', (e) => {
          e.stopPropagation();
          e.preventDefault();
          h.setPointerCapture(e.pointerId);
          const handle = h.dataset.h;
          const startX = e.clientX, startY = e.clientY;
          const o = Object.assign({}, this.value);
          const move = (ev) => {
            const dx = this._pxToMm(ev.clientX - startX);
            const dy = this._pxToMm(ev.clientY - startY);
            let nx = o.x_mm, ny = o.y_mm, nw = o.width_mm, nh = o.height_mm;
            if (handle === 'se') { nw = o.width_mm + dx; nh = o.height_mm + dy; }
            if (handle === 'ne') { nw = o.width_mm + dx; nh = o.height_mm - dy; ny = o.y_mm + dy; }
            if (handle === 'sw') { nw = o.width_mm - dx; nh = o.height_mm + dy; nx = o.x_mm + dx; }
            if (handle === 'nw') { nw = o.width_mm - dx; nh = o.height_mm - dy; nx = o.x_mm + dx; ny = o.y_mm + dy; }
            // Aspect lock (date string preserves ratio)
            const aspect = this.aspect || 4;
            const newH = nw / aspect;
            const dh = newH - nh;
            nh = newH;
            if (handle === 'ne' || handle === 'nw') ny -= dh;
            if (nw < 5) nw = 5;
            if (nh < 3) nh = 3;
            this.value = { x_mm: nx, y_mm: ny, width_mm: nw, height_mm: nh,
                            rotation_deg: o.rotation_deg };
            this._clamp(); this._render();
          };
          const up = () => {
            h.removeEventListener('pointermove', move);
            h.removeEventListener('pointerup', up);
            h.removeEventListener('pointercancel', up);
            this._emit();
          };
          h.addEventListener('pointermove', move);
          h.addEventListener('pointerup', up);
          h.addEventListener('pointercancel', up);
        });
      });
    }

    setPng(src, { aspect } = {}) {
      this.png_src = src;
      if (this.$img) this.$img.src = src;
      if (aspect && aspect > 0) {
        this.aspect = aspect;
        // Maintain current width, adjust height to match new aspect
        this.value.height_mm = this.value.width_mm / aspect;
        this._clamp(); this._render(); this._emit();
      }
    }

    setSize(width_mm, height_mm) {
      this.value.width_mm = width_mm;
      this.value.height_mm = height_mm;
      this.aspect = width_mm / Math.max(1, height_mm);
      this._clamp(); this._render();
    }

    setPosition(x_mm, y_mm) {
      this.value.x_mm = x_mm;
      this.value.y_mm = y_mm;
      this._clamp(); this._render();
    }

    setRotation(deg) {
      this.value.rotation_deg = +deg;
      this._render();
    }

    autoPositionRightOf(primaryValue, paper, gap_mm = 5) {
      // Place to the right of the primary stamp at vertical center alignment
      const px = primaryValue.x_mm + primaryValue.width_mm + gap_mm;
      const cy = primaryValue.y_mm + primaryValue.height_mm / 2 - this.value.height_mm / 2;
      this.value.x_mm = Math.min(px, paper.w - this.value.width_mm);
      this.value.y_mm = Math.max(0, cy);
      this._clamp(); this._render(); this._emit();
    }

    getValue() {
      return {
        x_mm: +this.value.x_mm.toFixed(2),
        y_mm: +this.value.y_mm.toFixed(2),
        width_mm: +this.value.width_mm.toFixed(2),
        height_mm: +this.value.height_mm.toFixed(2),
        rotation_deg: +(this.value.rotation_deg || 0).toFixed(2),
      };
    }

    destroy() {
      if (this.el && this.el.parentNode) this.el.parentNode.removeChild(this.el);
      this._mounted = false;
    }
  }
  window.StampDateOverlay = StampDateOverlay;
})();
