// Sabah Lens の Service Worker。
// index.html / sw.js を変えたら CACHE の版番号を必ず上げる（上げないと更新が反映されない）。
const CACHE = 'sabah-lens-v4';
const SHELL = ['./', './index.html', './manifest.json', './icon-192.png', './icon-512.png'];

// 記事写真は各媒体のサーバーから来る。毎回落とすと重いので別枠でキャッシュし、
// 上限を決めて古いものから捨てる。
const IMG_CACHE = 'sabah-lens-img-v1';
const IMG_MAX = 220;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE && k !== IMG_CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function trimImages() {
  const c = await caches.open(IMG_CACHE);
  const keys = await c.keys();
  if (keys.length <= IMG_MAX) return;
  for (const k of keys.slice(0, keys.length - IMG_MAX)) await c.delete(k);
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // 記事写真（外部オリジンの画像）: キャッシュ優先、無ければ取りに行って貯める
  if (req.destination === 'image' && url.origin !== location.origin) {
    e.respondWith((async () => {
      const c = await caches.open(IMG_CACHE);
      const hit = await c.match(req);
      if (hit) return hit;
      try {
        const res = await fetch(req, { mode: 'no-cors' });
        if (res && (res.ok || res.type === 'opaque')) { await c.put(req, res.clone()); trimImages(); }
        return res;
      } catch (err) {
        return new Response('', { status: 504 });
      }
    })());
    return;
  }

  if (url.origin !== location.origin) return;

  // ニュース本体は必ず新しいものを優先。落ちたときだけキャッシュに戻る
  if (url.pathname.endsWith('news.json')) {
    e.respondWith(
      fetch(req).then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
        return res;
      }).catch(() => caches.match(req))
    );
    return;
  }

  // アプリ本体はキャッシュ優先（起動を速く）
  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(req, copy));
      return res;
    }))
  );
});
