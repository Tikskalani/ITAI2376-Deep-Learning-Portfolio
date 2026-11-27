// The Bridge's 24-hour dial (spec 5.1): pure functions from /api/day to SVG.
// 00:00 is at the top and the day runs clockwise. Times are read in Atlas's
// time zone (day.tz), never the browser's, so the laptop and the phone agree.
// Alpine cannot loop <template> inside SVG, so the dots are rendered here as
// SVG strings (every piece of text escaped) and bound with x-html.
(() => {
  const fmtCache = {};
  function partsIn(tz) {
    return fmtCache[tz] || (fmtCache[tz] = new Intl.DateTimeFormat("en-US", {
      timeZone: tz, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }));
  }

  // Degrees clockwise from the top for an ISO time, in the given zone.
  function angleOf(iso, tz) {
    const d = new Date(iso);
    if (isNaN(d)) return 0;
    const p = Object.fromEntries(partsIn(tz || "UTC").formatToParts(d).map(x => [x.type, x.value]));
    const secs = (Number(p.hour) % 24) * 3600 + Number(p.minute) * 60 + Number(p.second);
    return (secs / 86400) * 360;
  }

  function point(cx, cy, r, deg) {
    const rad = (deg - 90) * Math.PI / 180;
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
  }

  // One clockwise arc from deg0 to deg1, wrapping past midnight when needed.
  function arc(cx, cy, r, deg0, deg1) {
    let span = ((deg1 - deg0) % 360 + 360) % 360;
    if (span === 0 && deg0 !== deg1) span = 360;
    const f = (p) => `${p.x.toFixed(1)} ${p.y.toFixed(1)}`;
    const a = point(cx, cy, r, deg0);
    if (span >= 359.9) {
      const mid = point(cx, cy, r, deg0 + 180), end = point(cx, cy, r, deg0 + 359.9);
      return `M${f(a)} A${r} ${r} 0 1 1 ${f(mid)} A${r} ${r} 0 1 1 ${f(end)}`;
    }
    const b = point(cx, cy, r, deg0 + span);
    return `M${f(a)} A${r} ${r} 0 ${span > 180 ? 1 : 0} 1 ${f(b)}`;
  }

  // Corner slots for up to four agents, as fractions of the dial's size.
  const SLOTS = [[0.225, 0.105], [0.775, 0.105], [0.225, 0.895], [0.775, 0.895]];

  function layout(day, agents, size, nowIso) {
    const c = size / 2, tz = (day && day.tz) || "UTC";
    const r = { dial: size * 0.2917, outer: size * 0.3167, label: size * 0.345, tick: size * 0.25, core: size * 0.0667 };
    const dot = (it, rad) => {
      const deg = angleOf(it.at, tz), p = point(c, c, rad, deg);
      return { x: p.x, y: p.y, deg, at: it.at, title: it.title || "", kind: it.kind || "", agent: it.agent || "",
        episode_id: it.episode_id || null, all_day: !!it.all_day };
    };
    const done = ((day && day.done) || []).filter(d => d && d.at).map(d => dot(d, r.dial));
    const next = ((day && day.next) || []).filter(d => d && d.at).map(d => dot(d, r.dial));
    const nowDeg = angleOf(nowIso || (day && day.now) || new Date().toISOString(), tz);
    const n0 = point(c, c, r.dial - size * 0.075, nowDeg), n1 = point(c, c, r.dial + size * 0.07, nowDeg);
    const nl = point(c, c, r.dial + size * 0.11, nowDeg);
    const night = day && day.night ? arc(c, c, r.dial, angleOf(day.night.start, tz), angleOf(day.night.end, tz)) : "";
    const sorted = (agents || []).slice().sort((a, b) => (b.live ? 1 : 0) - (a.live ? 1 : 0));
    const nodes = sorted.slice(0, 4).map((a, i) => {
      const [fx, fy] = SLOTS[i];
      const p = { x: fx * size, y: fy * size };
      const toward = Math.atan2(p.y - c, p.x - c);
      return { name: a.name, step: a.step || "", live: !!a.live, episode_id: a.episode_id || null, x: p.x, y: p.y,
        lx: c + Math.cos(toward) * (r.outer + 6), ly: c + Math.sin(toward) * (r.outer + 6), left: fx < 0.5, top: fy < 0.5 };
    });
    const ticks = [0, 6, 12, 18].map(h => ({ h: String(h).padStart(2, "0"), ...point(c, c, r.tick, h * 15) }));
    return { size, c, r, done, next, nodes, ticks, night,
      needle: { deg: nowDeg, x1: n0.x, y1: n0.y, x2: n1.x, y2: n1.y, lx: nl.x, ly: nl.y,
        label: "NOW " + new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
          .format(new Date(nowIso || (day && day.now) || Date.now())) } };
  }

  const esc = (s) => String(s).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  const hm = (iso, tz) => new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(iso));

  // SVG strings for the moving parts; the static rings live in the markup.
  // Push labels apart so two close times never print on top of each other.
  function unclash(labels, gap) {
    const placed = [];
    for (const l of labels.sort((a, b) => a.y - b.y)) {
      for (const p of placed) {
        if (Math.abs(p.x - l.x) < 150 && Math.abs(p.y - l.y) < gap) l.y = p.y + (l.y >= p.y ? gap : -gap);
      }
      placed.push(l);
    }
    return labels;
  }

  function render(L, tz) {
    if (!L) return { done: "", next: "", nodes: "", needle: "" };
    const done = L.done.map(d =>
      `<circle class="d-done${d.episode_id ? " link" : ""}" cx="${d.x.toFixed(1)}" cy="${d.y.toFixed(1)}" r="5" data-ep="${esc(d.episode_id || "")}">` +
      `<title>${esc(hm(d.at, tz) + " · " + (d.agent ? d.agent.toUpperCase() + " · " : "") + d.title.slice(0, 140))}</title></circle>`).join("");
    const N = L.needle;
    const nowLabel = { x: N.lx, y: N.ly, anchor: N.deg > 180 ? "end" : "start", text: N.label, now: true };
    const labels = unclash([nowLabel, ...L.next.slice(0, 4).map(d => {
      const x = L.c + (d.x - L.c) * 1.2, y = L.c + (d.y - L.c) * 1.2;
      return { x, y, anchor: Math.abs(x - L.c) < 20 ? "middle" : (x > L.c ? "start" : "end"),
        text: (d.all_day ? "" : hm(d.at, tz) + " ") + d.title.toUpperCase().slice(0, 24) };
    })], 15).filter(l => !l.now);
    const next = L.next.map(d =>
      `<circle class="d-next" cx="${d.x.toFixed(1)}" cy="${d.y.toFixed(1)}" r="5.5"><title>${esc(hm(d.at, tz) + " · " + d.title)}</title></circle>`
    ).join("") + labels.map(l =>
      `<text class="d-next-l" x="${l.x.toFixed(1)}" y="${(l.y + 4).toFixed(1)}" text-anchor="${l.anchor}">${esc(l.text)}</text>`).join("");
    const needle = `<line x1="${N.x1.toFixed(1)}" y1="${N.y1.toFixed(1)}" x2="${N.x2.toFixed(1)}" y2="${N.y2.toFixed(1)}"/>` +
      `<text x="${nowLabel.x.toFixed(1)}" y="${(nowLabel.y + 4).toFixed(1)}" text-anchor="${nowLabel.anchor}">${esc(N.label)}</text>`;
    const nodes = L.nodes.map(n => {
      const w = 190, h = 44, x = n.left ? n.x - w / 2 : n.x - w / 2, y = n.top ? n.y - h / 2 : n.y - h / 2;
      return `<g class="d-node${n.live ? " live" : ""}${n.episode_id ? " link" : ""}" data-ep="${esc(n.episode_id || "")}">` +
        `<path class="d-link${n.live ? " flow" : ""}" d="M${n.lx.toFixed(1)} ${n.ly.toFixed(1)} L${n.x.toFixed(1)} ${(n.top ? y + h : y).toFixed(1)}"/>` +
        `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w}" height="${h}" rx="2"/>` +
        `<text class="d-node-n" x="${(x + 12).toFixed(1)}" y="${(y + 18).toFixed(1)}">${esc(n.name.toUpperCase().replace(/_/g, " ").slice(0, 18))}</text>` +
        `<text class="d-node-s" x="${(x + 12).toFixed(1)}" y="${(y + 34).toFixed(1)}">${esc((n.live ? n.step || "WORKING" : n.step || "STANDBY").toUpperCase().slice(0, 26))}</text></g>`;
    }).join("");
    return { done, next, nodes, needle };
  }

  window.AtlasDial = { angleOf, point, arc, layout, render };
})();
