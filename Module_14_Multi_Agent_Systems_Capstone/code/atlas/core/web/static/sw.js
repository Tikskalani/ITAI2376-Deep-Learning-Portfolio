/* Atlas service worker: push notifications and a minimal offline shell.
 *
 * Push payloads come from core/surfaces/notify.py and look like
 *   { id, kind, title, body, priority, data }
 * where data carries approval_id, brief_id, or signal_id depending on kind.
 * A tap opens the PWA at the matching hash route so the dashboard shows
 * the right card. iOS web push has no action buttons, so the tap is the
 * whole interaction; the card does the rest.
 */

const SHELL_CACHE = "atlas-shell-v7";
const SHELL_URLS = ["/", "/manifest.webmanifest", "/static/icons/icon-192.png",
  "/static/vendor/alpine-3.13.5.min.js", "/static/vendor/d3-7.9.0.min.js",
  "/static/atlas/tokens.css", "/static/atlas/hud.css", "/static/atlas/shell.css", "/static/atlas/rooms.css", "/static/atlas/core.css", "/static/atlas/fetch-guard.js", "/static/atlas/demo.js", "/static/atlas/shell.js", "/static/atlas/dial.js", "/static/atlas/app.js", "/static/atlas/neural-core.js", "/static/atlas/boot.js",
  "/static/rooms/bridge.html", "/static/rooms/comms.html", "/static/rooms/plan.html", "/static/rooms/career.html",
  "/static/rooms/staff.html", "/static/rooms/inbox.html", "/static/rooms/memory.html", "/static/rooms/settings.html",
  "/static/rooms/overlays.html"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_URLS)).catch(() => undefined)
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Network first; the cached shell only when the network is gone. API calls
// are never cached: stale approvals would be worse than an error.
self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.pathname.startsWith("/api/")) return;
  event.respondWith(
    fetch(req).catch(() => caches.match(req).then((hit) => hit || caches.match("/")))
  );
});

function targetUrl(payload) {
  const kind = payload.kind || "";
  const data = payload.data || {};
  if (kind === "approval" && data.approval_id) return "/#approval/" + data.approval_id;
  if (kind.startsWith("brief/") && data.brief_id) return "/#brief/" + data.brief_id;
  if (kind === "decision_review") return "/#inbox/" + (payload.id || "");
  if (kind === "learning_digest") return "/#inbox/" + (payload.id || "");
  return "/#inbox" + (payload.id ? "/" + payload.id : "");
}

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_) {
    payload = { title: "Atlas", body: event.data ? event.data.text() : "" };
  }
  const title = payload.title || "Atlas";
  const options = {
    body: payload.body || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/badge-96.png",
    tag: payload.id || undefined,
    renotify: false,
    requireInteraction: payload.priority === "high",
    data: { url: targetUrl(payload), id: payload.id, kind: payload.kind },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ("focus" in client) {
          if ("navigate" in client) client.navigate(url).catch(() => undefined);
          return client.focus();
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
