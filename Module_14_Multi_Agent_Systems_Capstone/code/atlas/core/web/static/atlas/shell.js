// The shell's hands (spec 4): number keys 1 to 8 jump between rooms, F toggles
// Focus mode, Esc goes back to the Bridge, holding Space talks to Atlas, and
// holding the pointer anywhere empty opens the summon ring. None of these
// fire while a text field has focus: typing "1", "f" or a space types it.
(() => {
  const HOLD_MS = 350, MOVE_CANCEL = 8, TALK_MS = 250, DEAD = 40;
  const INTERACTIVE = "a, button, input, textarea, select, label, summary, details, [contenteditable], [role=button], [role=tab], " +
    "[role=switch], .graph-svg, .no-ring, audio, video, .sheet, .palette, .ring";

  function isTyping(el) {
    if (!el || el === document.body) return false;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "textarea" || tag === "select") return true;
    if (tag === "input") return !["button", "checkbox", "radio", "range", "submit", "reset", "file"].includes((el.type || "").toLowerCase());
    return !!el.isContentEditable;
  }

  function init(app) {
    let hold = null, talk = null;
    const count = () => app.rooms().length;

    // ---------- keys ----------
    document.addEventListener("keydown", (e) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      const typing = isTyping(document.activeElement);
      if (app.ring.open) {
        if (e.key === "Escape") { e.preventDefault(); closeRing(); }
        else if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); app.ring.hi = (app.ring.hi + 1 + count()) % count(); }
        else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); app.ring.hi = (Math.max(app.ring.hi, 0) - 1 + count()) % count(); }
        else if (e.key === "Enter") { e.preventDefault(); openHighlighted(); }
        return;
      }
      if (e.key === "Escape") {
        if (app.modal || app.paletteOpen || typing) return;
        if (app.room !== "bridge") { e.preventDefault(); app.goRoom("bridge"); }
        return;
      }
      if (typing) return;
      if (e.key >= "1" && e.key <= "9") {
        const r = app.rooms()[Number(e.key) - 1];
        if (r) { e.preventDefault(); app.goRoom(r.id); }
        return;
      }
      if (e.key === "f" || e.key === "F") { e.preventDefault(); app.toggleFocus(); return; }
      if (e.key === " " && e.shiftKey) { e.preventDefault(); openRing(innerWidth / 2, innerHeight / 2, 0); return; }
      if (e.key === " " && !e.repeat) {
        e.preventDefault();
        talk = setTimeout(() => {
          talk = "on";
          app.goRoom("comms", "channel");
          if (!app.listening && !app.pending) app.toggleMic();
        }, TALK_MS);
      } else if (e.key === " ") {
        e.preventDefault();
      }
    });
    document.addEventListener("keyup", (e) => {
      if (e.key !== " " || talk === null) return;
      if (talk === "on") { if (app.listening) app.stopMic(); } else clearTimeout(talk);
      talk = null;
    });

    // ---------- the summon ring ----------
    function openRing(x, y, hi) {
      app.ring = { open: true, x, y, hi };
      document.body.classList.add("ringing");
      if (window.AtlasGlyphs) window.AtlasGlyphs.burst(x, y);
    }
    function closeRing() {
      app.ring = { open: false, x: 0, y: 0, hi: -1 };
      document.body.classList.remove("ringing");
    }
    function openHighlighted() {
      const r = app.rooms()[app.ring.hi];
      closeRing();
      if (r) app.goRoom(r.id);
    }
    function segmentAt(x, y) {
      const dx = x - app.ring.x, dy = y - app.ring.y;
      if (Math.hypot(dx, dy) < DEAD) return -1;
      const n = count(), step = 360 / n;
      const deg = (Math.atan2(dy, dx) * 180 / Math.PI + 90 + 360 + step / 2) % 360;
      return Math.floor(deg / step);
    }

    window.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || app.ring.open || app.modal || app.paletteOpen) return;
      if (e.target.closest && e.target.closest(INTERACTIVE)) return;
      const sel = window.getSelection && window.getSelection();
      if (sel && String(sel).length) return;
      const x = e.clientX, y = e.clientY;
      hold = { x, y, timer: setTimeout(() => { hold = null; openRing(x, y, -1); }, HOLD_MS) };
    });
    window.addEventListener("pointermove", (e) => {
      if (hold && Math.hypot(e.clientX - hold.x, e.clientY - hold.y) > MOVE_CANCEL) { clearTimeout(hold.timer); hold = null; }
      if (app.ring.open) app.ring.hi = segmentAt(e.clientX, e.clientY);
    }, { passive: true });
    const release = (e) => {
      if (hold) { clearTimeout(hold.timer); hold = null; }
      if (!app.ring.open) return;
      if (e.type === "pointercancel") { closeRing(); return; }
      // Releasing over a segment opens that room; releasing in the middle cancels.
      const hi = segmentAt(e.clientX, e.clientY);
      if (hi >= 0) { app.ring.hi = hi; openHighlighted(); } else closeRing();
    };
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    document.addEventListener("selectionchange", () => {
      const sel = window.getSelection && window.getSelection();
      if (hold && sel && String(sel).length) { clearTimeout(hold.timer); hold = null; }
    });
    window.addEventListener("contextmenu", (e) => { if (app.ring.open || hold) e.preventDefault(); });
  }

  // One segment path per room, in dock order, 0 at the top, clockwise.
  function ringPaths(n = 6, size = 260, inner = 64, outer = 124) {
    const c = size / 2, out = [], step = 360 / n, half = step / 2 - 2;
    for (let i = 0; i < n; i++) {
      const a0 = (i * step - 90 - half) * Math.PI / 180, a1 = (i * step - 90 + half) * Math.PI / 180, am = (i * step - 90) * Math.PI / 180;
      const p = (r, a) => `${(c + r * Math.cos(a)).toFixed(1)} ${(c + r * Math.sin(a)).toFixed(1)}`;
      out.push({ d: `M${p(outer, a0)} A${outer} ${outer} 0 0 1 ${p(outer, a1)} L${p(inner, a1)} A${inner} ${inner} 0 0 0 ${p(inner, a0)}Z`,
        lx: +(c + 94 * Math.cos(am)).toFixed(1), ly: +(c + 94 * Math.sin(am)).toFixed(1) });
    }
    return out;
  }

  window.AtlasShell = { isTyping, init, ringPaths };
})();
