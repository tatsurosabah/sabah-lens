// news.json の記事に「実URL・og:image・抜粋」を足す。
//
// なぜブラウザが要るのか:
//   Google ニュースRSSのリンク（news.google.com/rss/articles/…）は署名付きで、
//   HTTPだけでは実URLに解決できない。中継ページの静的HTMLにも転送先は無く、
//   JSを実行して初めて記事に飛ぶ。よって headless Chrome を CDP で駆動する。
//   そして Google ニュースRSSには media:content も enclosure も無いので、
//   画像は着地先の og:image から取るしかない。
//
// 一度解決した記事は news.json に焼き付くので二度と触らない。
// 日々の実行で実際に処理されるのは新着数件だけになる。
//
// 使い方:
//   node enrich.mjs [--limit N] [--conc N] [--file news.json] [--retry-failed]

import { readFileSync, writeFileSync } from 'node:fs';
import { spawn } from 'node:child_process';

const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : dflt;
};
const FILE = opt('--file', 'news.json');
const LIMIT = Number(opt('--limit', '0')) || Infinity;
const CONC = Number(opt('--conc', '5'));
const RETRY_FAILED = args.includes('--retry-failed');

const PORT = 9377;
const NAV_MS = 22000;      // 中継ページが記事に飛ぶまでの待ち上限
const SETTLE_MS = 2500;    // 着地後、og メタが入るまで

// ─────────────── Chrome を起こす ───────────────

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable',
  '/usr/bin/chromium-browser',
  '/usr/bin/chromium',
].filter(Boolean);

let chromeProc = null;
async function startChrome() {
  for (const bin of CHROME_CANDIDATES) {
    try {
      chromeProc = spawn(bin, [
        '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
        '--no-default-browser-check', '--disable-dev-shm-usage',
        '--user-data-dir=/tmp/sabahlens-chrome',
        `--remote-debugging-port=${PORT}`, 'about:blank',
      ], { stdio: 'ignore' });
    } catch { continue; }

    for (let i = 0; i < 40; i++) {
      await new Promise(r => setTimeout(r, 500));
      try {
        const v = await fetch(`http://127.0.0.1:${PORT}/json/version`);
        if (v.ok) { console.log(`Chrome: ${bin}`); return await v.json(); }
      } catch {}
    }
    try { chromeProc.kill(); } catch {}
  }
  throw new Error('Chrome が見つからない。CHROME_PATH を設定してください。');
}

const ver = await startChrome();
const ws = new WebSocket(ver.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r, { once: true }));

let msgId = 0;
const pending = new Map();
const frameUrl = new Map();
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); return; }
  if (m.method === 'Page.frameNavigated' && !m.params.frame.parentId) {
    frameUrl.set(m.sessionId, m.params.frame.url);
  }
});
const send = (method, params = {}, sessionId) => new Promise(resolve => {
  const id = ++msgId;
  pending.set(id, resolve);
  ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
});

// ─────────────── 記事ページから拾うもの ───────────────

const EXTRACT = `(() => {
  const meta = sel => document.querySelector(sel)?.content?.trim() || '';
  const abs = u => { try { return new URL(u, location.href).href; } catch { return ''; } };

  let img = meta('meta[property="og:image"]')
         || meta('meta[property="og:image:secure_url"]')
         || meta('meta[name="twitter:image"]')
         || meta('meta[name="twitter:image:src"]');

  // og が無い場合だけ本文の先頭画像を拾う。ロゴやアイコンは弾く。
  if (!img) {
    for (const el of document.querySelectorAll('article img, .entry-content img, .post-content img, main img')) {
      const s = el.currentSrc || el.src || '';
      if (!s || /logo|icon|avatar|placeholder|blank|spacer|1x1/i.test(s)) continue;
      if ((el.naturalWidth || el.width || 0) < 320) continue;
      img = s; break;
    }
  }

  const title = document.title || '';
  return JSON.stringify({
    landed: location.href,
    img: img ? abs(img) : '',
    imgAlt: meta('meta[property="og:image:alt"]'),
    desc: meta('meta[property="og:description"]') || meta('meta[name="description"]'),
    site: meta('meta[property="og:site_name"]'),
    published: meta('meta[property="article:published_time"]'),
    blocked: /just a moment|attention required|checking your browser|access denied|are you a robot/i.test(title),
  });
})()`;

async function resolveOne(url) {
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  try {
    await send('Page.enable', {}, sessionId);
    frameUrl.delete(sessionId);
    await send('Page.navigate', { url }, sessionId);

    const isGoogle = u => !u || u.startsWith('about:') || /^https?:\/\/(news|www)\.google\.com/.test(u);
    const deadline = Date.now() + NAV_MS;
    let landedOff = false;
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 400));
      if (!isGoogle(frameUrl.get(sessionId))) { landedOff = true; break; }
    }
    if (!landedOff) return { landed: '', img: '', desc: '', blocked: false, timeout: true };

    await new Promise(r => setTimeout(r, SETTLE_MS));
    const r = await send('Runtime.evaluate', { expression: EXTRACT, returnByValue: true }, sessionId);
    return r?.result?.value ? JSON.parse(r.result.value) : { landed: frameUrl.get(sessionId) || '' };
  } finally {
    await send('Target.closeTarget', { targetId });
  }
}

// ─────────────── 本体 ───────────────

const data = JSON.parse(readFileSync(FILE, 'utf8'));
const items = data.items || [];

// enriched が付いていれば済み。失敗記録があるものは --retry-failed のときだけ再挑戦する。
const todo = items.filter(it => {
  if (it.enriched === 'ok') return false;
  if (it.enriched && !RETRY_FAILED) return false;
  return /^https?:\/\/news\.google\.com/.test(it.url || '');
}).slice(0, LIMIT);

console.log(`対象 ${todo.length} 件 / 全 ${items.length} 件（同時 ${CONC}）`);

let done = 0, withImg = 0, blocked = 0, failed = 0;
let cursor = 0;
await Promise.all(Array.from({ length: Math.min(CONC, todo.length) }, async () => {
  while (cursor < todo.length) {
    const it = todo[cursor++];
    let r;
    try { r = await resolveOne(it.url); }
    catch (e) { r = { err: String(e).slice(0, 100) }; }

    if (r.landed) {
      it.url_real = r.landed;
      if (r.img) { it.image = r.img; if (r.imgAlt) it.image_alt = r.imgAlt; withImg++; }
      // 抜粋は既にアラート由来のものがあればそちらを優先する
      if (r.desc && !it.summary) it.summary = r.desc.slice(0, 400);
      if (r.site && !it.site_name) it.site_name = r.site;
      it.enriched = r.img ? 'ok' : (r.blocked ? 'blocked' : 'noimage');
      if (r.blocked) blocked++;
    } else {
      it.enriched = 'failed';
      failed++;
    }

    done++;
    if (done % 10 === 0 || done === todo.length) {
      console.log(`  ${done}/${todo.length}  画像 ${withImg} / ブロック ${blocked} / 失敗 ${failed}`);
    }
  }
}));

if (todo.length) {
  data.enriched_at = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  writeFileSync(FILE, JSON.stringify(data, null, 1) + '\n');
}

const total = items.length;
const haveImg = items.filter(i => i.image).length;
console.log(`\n完了: 画像あり ${haveImg}/${total} (${(haveImg / total * 100).toFixed(0)}%)`);

ws.close();
try { chromeProc?.kill(); } catch {}
process.exit(0);
