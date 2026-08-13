// Google ニュースのサムネイルを収穫して news.json の各記事に付ける。
//
// なぜこれが要るのか:
//   記事ページの og:image は Cloudflare を挟む媒体（Daily Express / Borneo Post /
//   MalaysiaGazette など）から取れない。一方 Google ニュースは自前でサムネイルを
//   持っていて、それは検索ページの `news.google.com/api/attachments/…` から配信される。
//   ここから取れば **弾かれる媒体も含めて全記事** 画像が付く。
//
//   しかも軽い。媒体の og:image が 80〜330KB なのに対し、こちらは
//   `-w800-h450-p-df-rw` を付けても 10KB 前後（実測 554x331 / 8.7KB）。
//   一覧のカードにはこちらを使い、大きく見せるヒーローや記事画面では
//   高解像度の og:image があればそちらを使う、という使い分けにしている。
//
// 記事の対応付けは Google ニュースの記事ID。RSS のリンク
// `news.google.com/rss/articles/<ID>` の <ID> が検索ページのリンクと同じなので、
// それをキーに突き合わせる。
//
// 使い方: node thumbs.mjs [--file news.json] [--all]
//   --all を付けると thumb が既にある記事も上書きする

import { readFileSync, writeFileSync } from 'node:fs';
import { spawn } from 'node:child_process';

const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf(n); return i >= 0 ? args[i + 1] : d; };
const FILE = opt('--file', 'news.json');
const FEEDS = opt('--feeds', 'feeds.json');
const ALL = args.includes('--all');

const PORT = 9378;
const SIZE = '-w800-h450-p-df-rw';   // 元画像より大きくは返らない。実質これが最大
const PAGE_WAIT = 9000;

// ─────────────── Chrome ───────────────

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
  '/usr/bin/chromium-browser', '/usr/bin/chromium',
].filter(Boolean);

let proc = null, ver = null;
for (const bin of CHROME_CANDIDATES) {
  try {
    proc = spawn(bin, [
      '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
      '--no-default-browser-check', '--disable-dev-shm-usage',
      '--window-size=1280,3000', '--user-data-dir=/tmp/sabahlens-thumbs',
      `--remote-debugging-port=${PORT}`, 'about:blank',
    ], { stdio: 'ignore' });
  } catch { continue; }
  for (let i = 0; i < 40 && !ver; i++) {
    await new Promise(r => setTimeout(r, 500));
    try { const v = await fetch(`http://127.0.0.1:${PORT}/json/version`); if (v.ok) ver = await v.json(); } catch {}
  }
  if (ver) { console.log(`Chrome: ${bin}`); break; }
  try { proc.kill(); } catch {}
}
if (!ver) { console.error('Chrome が見つからない。CHROME_PATH を設定してください。'); process.exit(1); }

const ws = new WebSocket(ver.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r, { once: true }));
let msgId = 0; const pending = new Map();
ws.addEventListener('message', e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
});
const send = (method, params = {}, s) => new Promise(res => {
  const i = ++msgId; pending.set(i, res);
  ws.send(JSON.stringify({ id: i, method, params, ...(s ? { sessionId: s } : {}) }));
});

// ─────────────── 収穫 ───────────────

// Google ニュースは <article> を使わない。サムネイルから祖先を遡り、
// 同じ塊の中にある記事IDリンクと対にする。
const HARVEST = String.raw`(() => {
  const out = [];
  for (const img of document.querySelectorAll('img[src*="/api/attachments/"]')) {
    let node = img, aid = '';
    for (let i = 0; i < 8 && node; i++) {
      const a = node.querySelector && node.querySelector('a[href*="./articles/"], a[href*="./read/"]');
      if (a) {
        const m = a.getAttribute('href').match(/(?:articles|read)\/([A-Za-z0-9_\-]{20,})/);
        if (m) { aid = m[1]; break; }
      }
      node = node.parentElement;
    }
    let src = img.currentSrc || img.src || '';
    if (src.startsWith('/')) src = location.origin + src;
    if (aid && src) out.push([aid, src.replace(/-w\d+-h\d+-p-df-rw$/, '')]);
  }
  return JSON.stringify(out);
})()`;

async function harvest(url) {
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  try {
    await send('Page.enable', {}, sessionId);
    await send('Page.navigate', { url }, sessionId);
    await new Promise(r => setTimeout(r, PAGE_WAIT));
    const q = await send('Runtime.evaluate', { expression: HARVEST, returnByValue: true }, sessionId);
    return q?.result?.value ? JSON.parse(q.result.value) : [];
  } finally {
    await send('Target.closeTarget', { targetId });
  }
}

// ─────────────── 本体 ───────────────

const cfg = JSON.parse(readFileSync(FEEDS, 'utf8'));
const data = JSON.parse(readFileSync(FILE, 'utf8'));
const items = data.items || [];

// 記事ID → 記事 の索引を作る
const idOf = u => (String(u || '').match(/(?:articles|read)\/([A-Za-z0-9_\-]{20,})/) || [])[1] || '';
const byId = new Map();
for (const it of items) {
  const a = idOf(it.url);
  if (a) byId.set(a, it);
}
console.log(`記事 ${items.length} 件（うち Google ニュースID あり ${byId.size} 件）`);

const found = new Map();
for (const [i, spec] of (cfg.google_news || []).entries()) {
  const u = 'https://news.google.com/search?q=' + encodeURIComponent(spec.q) +
    `&hl=${spec.hl || 'en-MY'}&gl=${spec.gl || 'MY'}&ceid=${spec.ceid || 'MY:en'}`;
  let pairs = [];
  try { pairs = await harvest(u); } catch (e) { console.log(`  ! ${spec.q}: ${String(e).slice(0, 60)}`); }
  for (const [aid, base] of pairs) if (!found.has(aid)) found.set(aid, base);
  const hit = pairs.filter(([aid]) => byId.has(aid)).length;
  console.log(`[${String(i + 1).padStart(2)}/${cfg.google_news.length}] ${String(pairs.length).padStart(3)} 枚 / 手持ちと一致 ${String(hit).padStart(3)}  ${spec.q}`);
}

let added = 0;
for (const [aid, base] of found) {
  const it = byId.get(aid);
  if (!it) continue;
  if (it.thumb && !ALL) continue;
  it.thumb = base + SIZE;
  added++;
}

// news.google.com の画像URLは `cross-origin-resource-policy: same-site` を返すので、
// 別オリジンのアプリからは <img> で読めない（curl は通るのでこれに気づきにくい）。
// 転送先の encrypted-tbnN.gstatic.com は cross-origin 許可＋1年キャッシュなので、
// 302 を解決して最終URLの方を保存する。
async function resolveRedirect(u) {
  try {
    const r = await fetch(u, { redirect: 'manual' });
    const loc = r.headers.get('location');
    if (loc && /gstatic\.com/.test(loc)) return loc;
    if (r.ok && /gstatic\.com/.test(r.url)) return r.url;
  } catch {}
  return null;
}

const needResolve = items.filter(it => it.thumb && it.thumb.includes('news.google.com'));
if (needResolve.length) {
  console.log(`\n転送先を解決: ${needResolve.length} 件`);
  let ok = 0, cursor2 = 0;
  await Promise.all(Array.from({ length: 8 }, async () => {
    while (cursor2 < needResolve.length) {
      const it = needResolve[cursor2++];
      const real = await resolveRedirect(it.thumb);
      if (real) { it.thumb = real; ok++; }
      else delete it.thumb;      // 解決できないURLは持っていても表示できない
    }
  }));
  console.log(`  解決 ${ok}/${needResolve.length}`);
}

if (added) {
  data.thumbs_at = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  writeFileSync(FILE, JSON.stringify(data, null, 1) + '\n');
}
const have = items.filter(i => i.thumb || i.image).length;
console.log(`\n付与 ${added} 件 / 画像を持つ記事 ${have}/${items.length} (${(have / items.length * 100).toFixed(0)}%)`);

ws.close();
try { proc.kill(); } catch {}
process.exit(0);
