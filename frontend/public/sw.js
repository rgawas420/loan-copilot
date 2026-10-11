// Service worker: makes the app installable, starts it fast, and shows an offline page instead of a browser error.
//
// What is cached, and what never is:
//   /assets/*       built JS/CSS; names contain a content hash, so cache-first is always correct
//   icons, fonts    cache-first / stale-while-revalidate
//   pages (/)       network-first: a deploy is visible on the next load; offline -> offline.html
//   /api/*          NEVER cached: answers, chats and account data are personal and live (and answers stream)
// Bump VERSION to drop every cache on the next visit.
const VERSION = "v1";
const SHELL = `um-shell-${VERSION}`;
const RUNTIME = `um-runtime-${VERSION}`;
const PRECACHE = ["/offline.html", "/icons/icon-192.png", "/manifest.webmanifest"];
const FONT_HOSTS = ["fonts.googleapis.com", "fonts.gstatic.com"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keep = new Set([SHELL, RUNTIME]);
    for (const key of await caches.keys()) if (!keep.has(key)) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/classic") || url.pathname.startsWith("/static/")) return;
    if (req.mode === "navigate") { event.respondWith(page(req)); return; }
    if (url.pathname.startsWith("/assets/") || url.pathname.startsWith("/icons/")) { event.respondWith(cacheFirst(req)); return; }
    return;
  }
  if (FONT_HOSTS.includes(url.hostname)) event.respondWith(staleWhileRevalidate(req));
});

async function page(req) {
  try {
    return await fetch(req);
  } catch {
    return (await caches.match("/offline.html")) || new Response("You are offline.", { status: 503 });
  }
}

async function cacheFirst(req) {
  const hit = await caches.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) (await caches.open(RUNTIME)).put(req, res.clone());
  return res;
}

async function staleWhileRevalidate(req) {
  const cache = await caches.open(RUNTIME);
  const hit = await cache.match(req);
  const fresh = fetch(req).then((res) => { if (res.ok || res.type === "opaque") cache.put(req, res.clone()); return res; })
                          .catch(() => hit);
  return hit || fresh;
}
