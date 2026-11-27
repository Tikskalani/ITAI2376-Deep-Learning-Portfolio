// Boot: put each room's markup into its slot, then start Alpine. Alpine must
// start after the markup exists, so it is loaded here rather than by a tag.
// A room that fails to load shows a plain notice instead of a hole.
// The HTML inserted is only Atlas's own same-origin partials, named in the
// fixed list below; nothing user-supplied reaches insertAdjacentHTML.
window.ATLAS_PARTIALS = ["bridge", "comms", "plan", "career", "staff", "inbox", "memory", "settings", "overlays"];

(async () => {
  await Promise.all(window.ATLAS_PARTIALS.map(async (name) => {
    const slot = document.querySelector(`[data-slot="${name}"]`);
    if (!slot) return;
    let html;
    try {
      const r = await fetch(`/static/rooms/${name}.html`, { cache: "no-cache" });
      if (!r.ok) throw new Error(String(r.status));
      html = await r.text();
    } catch (e) {
      html = `<div class="panel bad lost" role="alert" data-room="${name}">The ${name} room did not load. Reload the page.</div>`;
    }
    slot.insertAdjacentHTML("beforebegin", html);
    slot.remove();
  }));
  const alpine = document.createElement("script");
  alpine.src = "/static/vendor/alpine-3.13.5.min.js";
  alpine.onload = () => document.dispatchEvent(new Event("atlas:booted"));
  document.head.appendChild(alpine);
})();
