// Minimaler Service Worker — macht die App installierbar. Netz-zuerst, App-Shell
// nur als Offline-Fallback. Bewusst schlank; Daten kommen live vom Server.
// Bei Asset-Änderungen SHELL-Version bumpen → alter Cache wird beim activate gelöscht.
const SHELL = "warroom-v5";
const ASSETS = ["/", "/static/style.css", "/static/fonts/germania-one.woff2",
  "/static/vendor/leaflet/leaflet.css", "/static/vendor/leaflet/leaflet.js"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) =>
    Promise.all(ks.filter((k) => k !== SHELL).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/")) return; // Live-Daten nie cachen
  // Netz zuerst; nur wenn offline → Cache (Query ?v= beim Match ignorieren).
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request, {ignoreSearch: true})));
});

// Der Rabe bringt Kunde: gebündelte Wächter-Meldung vom Poller.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch (err) {}
  e.waitUntil(self.registration.showNotification(d.title || "Warroom", {
    body: d.body || "",
    tag: d.tag || "warroom",
    renotify: true,
    icon: "/static/icon-raider.png",
    badge: "/static/icon-raider.png",
    data: { url: d.url || "/?tab=waechter" },
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
    for (const c of cs) {
      if ("focus" in c) { c.navigate(url); return c.focus(); }
    }
    return clients.openWindow(url);
  }));
});
