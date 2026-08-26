// news.json の記事に「実URL・og:image・抜粋・本文」を足す。
//
// なぜブラウザが要るのか:
//   Google ニュースRSSのリンク（news.google.com/rss/articles/…）は署名付きで、
//   HTTPだけでは実URLに解決できない。中継ページの静的HTMLにも転送先は無く、
//   JSを実行して初めて記事に飛ぶ。よって headless Chrome を CDP で駆動する。
//
// 一度解決した記事は news.json に焼き付き、二度と触らない（enriched_v で判定）。
// 日々の実行で実際に処理されるのは新着数件だけになる。
//
// 使い方:
//   node enrich.mjs [--limit N] [--conc N] [--file news.json] [--retry-failed] [--force]

import { readFileSync, writeFileSync } from 'node:fs';
import { spawn } from 'node:child_process';

// 抽出の仕様を変えたら上げる。上げると全記事が取り直しになる。
const VERSION = 3;

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : dflt; };
const FILE = opt('--file', 'news.json');
const LIMIT = Number(opt('--limit', '0')) || Infinity;
const CONC = Number(opt('--conc', '5'));
const RETRY_FAILED = args.includes('--retry-failed');
const FORCE = args.includes('--force');

const PORT = 9377;
const NAV_MS = 22000;      // 中継ページが記事に飛ぶまでの待ち上限
const SETTLE_MS = 2000;    // 着地後、og メタが入るまで
const CF_WAIT_MS = 20000;  // Cloudflare の自動チャレンジを待つ上限

// 本文は全文を保存する（アプリ内で原文と和訳の両方を読めるようにするため）。
// news.json は GitHub Pages でそのまま公開される点はユーザー了承済み。
const BODY_PARAS = 200;
const BODY_MAX = 12000;

// HeadlessChrome を名乗ると弾く媒体があるので、通常の Chrome を名乗る。
// これだけで Berita Harian などは通る（Daily Express / Borneo Post は通らない）。
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36';

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
        '--disable-blink-features=AutomationControlled',
        '--window-size=1280,900', '--lang=en-US,en',
        `--user-agent=${UA}`,
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

const EXTRACT = String.raw`(() => {
  const meta = sel => document.querySelector(sel)?.content?.trim() || '';
  const abs = u => { try { return new URL(u, location.href).href; } catch { return ''; } };
  const title = document.title || '';

  const blocked = /just a moment|attention required|checking your browser|access denied|are you a robot|verifying you are human/i.test(title)
    || !!document.querySelector('#challenge-running, #cf-challenge-running, form#challenge-form');

  let img = meta('meta[property="og:image"]')
         || meta('meta[property="og:image:secure_url"]')
         || meta('meta[name="twitter:image"]')
         || meta('meta[name="twitter:image:src"]');

  if (!img) {
    for (const el of document.querySelectorAll('article img, .entry-content img, .post-content img, main img')) {
      const s = el.currentSrc || el.src || '';
      if (!s || /logo|icon|avatar|placeholder|blank|spacer|1x1/i.test(s)) continue;
      if ((el.naturalWidth || el.width || 0) < 320) continue;
      img = s; break;
    }
  }

  // 本文の取り出し。
  // 決め打ちのセレクタ（.entry-content など）だけだと、当てはまらないサイトで
  // まるごと空になる。そこで「段落テキストを一番多く直に抱えている要素が本文」
  // という見方で探す。Readability の考え方の簡易版。
  const NOISE = /^(share|tweet|advertisement|iklan|baca juga|read more|related|follow us|subscribe|sign up|copyright|photo:|foto:|gambar:|by |oleh )/i;
  const clean = el => (el.innerText || '').trim().replace(/\s+/g, ' ');
  const goodParas = el => [...el.querySelectorAll(':scope > p')]
    .map(clean).filter(t => t.length > 35 && !NOISE.test(t));

  let best = null, bestLen = 0;
  for (const el of document.querySelectorAll('article, main, section, div, td')) {
    // 画面から隠れている塊（別タブ用の複製など）は無視する
    if (!el.offsetParent && el.tagName !== 'BODY') {
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    }
    const ps = goodParas(el);
    if (ps.length < 2) continue;
    const len = ps.join('').length;
    if (len > bestLen) { bestLen = len; best = el; }
  }

  let paras = best ? goodParas(best) : [];
  // それでも取れなければ、文書全体の段落から拾う
  if (paras.join('').length < 250) {
    const all = [...document.querySelectorAll('p')].map(clean)
      .filter(t => t.length > 60 && !NOISE.test(t));
    if (all.join('').length > paras.join('').length) paras = all;
  }
  paras = [...new Set(paras)];   // 同じ段落を繰り返し出すサイトがある
  paras = paras.slice(0, __PARAS__);   // 冒頭だけ（全文は保存しない）

  return JSON.stringify({
    landed: location.href,
    title,
    blocked,
    img: img ? abs(img) : '',
    imgAlt: meta('meta[property="og:image:alt"]'),
    desc: meta('meta[property="og:description"]') || meta('meta[name="description"]'),
    site: meta('meta[property="og:site_name"]'),
    body: paras.join('\n\n'),
  });
})()`;

async function resolveOne(url) {
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  try {
    const extract = EXTRACT.replace('__PARAS__', String(BODY_PARAS));
    await send('Page.enable', {}, sessionId);
    await send('Emulation.setUserAgentOverride',
      { userAgent: UA, acceptLanguage: 'en-US,en;q=0.9', platform: 'MacIntel' }, sessionId);
    frameUrl.delete(sessionId);
    await send('Page.navigate', { url }, sessionId);

    // 「まだ Google にいる」か「そもそもページとして開けていない」かを見る。
    // chrome-error:// を着地扱いにすると url_real がそれで上書きされて壊れる。
    const notLanded = u => !u || u.startsWith('about:') || u.startsWith('chrome-error:')
      || u.startsWith('chrome:') || /^https?:\/\/(news|www)\.google\.com/.test(u);
    const deadline = Date.now() + NAV_MS;
    let landedOff = false;
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 400));
      if (!notLanded(frameUrl.get(sessionId))) { landedOff = true; break; }
    }
    if (!landedOff) return { landed: '', timeout: true };

    await new Promise(r => setTimeout(r, SETTLE_MS));

    // Cloudflare のチャレンジは自力で抜けることがある。抜けるまで粘る。
    let out = null;
    const cfDeadline = Date.now() + CF_WAIT_MS;
    for (;;) {
      const r = await send('Runtime.evaluate', { expression: extract, returnByValue: true }, sessionId);
      out = r?.result?.value ? JSON.parse(r.result.value) : { landed: frameUrl.get(sessionId) || '' };
      if (!out.blocked || Date.now() > cfDeadline) break;
      await new Promise(r => setTimeout(r, 1500));
    }
    return out;
  } finally {
    await send('Target.closeTarget', { targetId });
  }
}

// ─────────────── 本体 ───────────────

const data = JSON.parse(readFileSync(FILE, 'utf8'));
const items = data.items || [];

const todo = items.filter(it => {
  if (!/^https?:\/\/news\.google\.com/.test(it.url || '')) return false;
  if (FORCE) return true;
  if (it.enriched_v === VERSION) {
    // 版が同じでも、前回ブロックされたものは --retry-failed で再挑戦する
    return RETRY_FAILED && it.enriched !== 'ok';
  }
  return true;
}).slice(0, LIMIT);

console.log(`対象 ${todo.length} 件 / 全 ${items.length} 件（同時 ${CONC}, 版 ${VERSION}）`);

let done = 0, withImg = 0, withBody = 0, blocked = 0, failed = 0;
let cursor = 0;
await Promise.all(Array.from({ length: Math.min(CONC, todo.length) }, async () => {
  while (cursor < todo.length) {
    const it = todo[cursor++];
    let r;
    try { r = await resolveOne(it.url); }
    catch (e) { r = { err: String(e).slice(0, 100) }; }

    if (r.landed && /^https?:/.test(r.landed)) {
      it.url_real = r.landed;
      if (r.img) { it.image = r.img; if (r.imgAlt) it.image_alt = r.imgAlt; withImg++; }
      if (r.desc && !it.summary) it.summary = r.desc.slice(0, 400);
      if (r.site && !it.site_name) it.site_name = r.site;
      if (r.body && r.body.length > 200) {
        const next = r.body.slice(0, BODY_MAX);
        if (next !== it.body) { it.body = next; it.body_ja = ''; }   // 本文が変わったら訳し直す
        withBody++;
      }
      it.enriched = r.body && r.body.length > 200 ? 'ok'
                  : r.blocked ? 'blocked'
                  : r.img ? 'nobody' : 'thin';
      if (r.blocked) blocked++;
    } else {
      it.enriched = 'failed';
      failed++;
    }
    it.enriched_v = VERSION;

    done++;
    if (done % 10 === 0 || done === todo.length) {
      console.log(`  ${done}/${todo.length}  画像 ${withImg} / 本文 ${withBody} / ブロック ${blocked} / 失敗 ${failed}`);
    }
  }
}));

if (todo.length) {
  data.enriched_at = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  writeFileSync(FILE, JSON.stringify(data, null, 1) + '\n');
}

const total = items.length;
console.log(`\n完了: 画像 ${items.filter(i => i.image || i.thumb).length}/${total}` +
            ` / 本文 ${items.filter(i => i.body).length}/${total}`);

ws.close();
try { chromeProc?.kill(); } catch {}
process.exit(0);
