#!/usr/bin/env python3
"""Sabah Watch — ニュース収集スクリプト

feeds.json の設定に従って
  1. Google News RSS（キーワード検索）
  2. Google Alerts の RSS フィード
を取得し、Sabah の無国籍・難民関連だけに絞り、日本語訳を付けて news.json に書き出す。

GitHub Actions から定期実行される。ローカル実行も可:
    python3 fetch_news.py            # 通常
    python3 fetch_news.py --no-translate
"""

import hashlib
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
FEEDS_PATH = os.path.join(HERE, "feeds.json")
OUT_PATH = os.path.join(HERE, "news.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# macOS の python.org 版はルート証明書が入っていないことがあるので、その場合だけ緩める
_SSL_CTX = None
if os.environ.get("SW_INSECURE_SSL") == "1":
    _SSL_CTX = ssl._create_unverified_context()


# ---------------------------------------------------------------- 取得ユーティリティ

def http_get(url, timeout=30, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept-Language": "en,ms,ja"})
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
                return r.read()
        except Exception as e:                                  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    print(f"  ! 取得失敗 {url[:90]} : {type(last).__name__} {last}", file=sys.stderr)
    return None


def strip_tags(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


TITLE_SEPS = (" - ", " | ", " – ", " — ", " :: ")
SRC_STOPWORDS = {"news", "online", "com", "my", "the", "daily", "tv", "net", "org",
                 "berita", "media", "portal", "co", "www"}
TAIL_NOISE = re.compile(r"(berita terkini|latest news|breaking news|video|foto|photos?)$", re.I)


def clean_title(title, source):
    """Google News の見出し末尾に付く媒体名（"… - Malay Mail", "… | Berita Terkini"）を落とす"""
    t = (title or "").strip()
    src_tokens = set(re.findall(r"[a-z0-9]+", (source or "").lower())) - SRC_STOPWORDS
    for _ in range(3):
        cut = None
        for sep in TITLE_SEPS:
            idx = t.rfind(sep)
            if idx <= 10:
                continue
            tail = t[idx + len(sep):].strip()
            if not tail or len(tail) > 40:
                continue
            tail_tokens = set(re.findall(r"[a-z0-9]+", tail.lower()))
            if (source and tail.lower() == source.lower()) \
                    or (src_tokens and tail_tokens & src_tokens) \
                    or TAIL_NOISE.fullmatch(tail):
                cut = idx
                break
        if cut is None:
            break
        t = t[:cut].strip()
    return t or (title or "").strip()


def norm_key(title):
    """重複判定用の正規化キー（Alert と News の同一記事をまとめる）"""
    t = title.lower()
    t = re.sub(r"[^a-z0-9぀-ヿ一-鿿]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:110]


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_date(text, fallback=None):
    if text:
        text = text.strip()
        try:
            return parsedate_to_datetime(text).astimezone(timezone.utc)
        except Exception:                                       # noqa: BLE001
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:                                       # noqa: BLE001
            pass
    return fallback or datetime.now(timezone.utc)


# ---------------------------------------------------------------- 関連度フィルタ / タグ

SABAH_RE = re.compile(
    r"\b(sabah|sabahan|sandakan|semporna|tawau|kota kinabalu|lahad datu|kudat|kunak|"
    r"keningau|beaufort|papar|ranau|tuaran|kinabatangan|labuan|borneo|sipadan|mabul)\b", re.I)

# Sabah の語が無くても文脈上ほぼ確実にサバ州の話題
STRONG_RE = re.compile(r"(bajau laut|imm13|imm 13|mykas|pss card|kad pss|pati\b|\bpti\b)", re.I)

TOPIC_RE = re.compile(
    r"(stateless|statelessness|refugee|asylum|unhcr|undocumented|citizenship|"
    r"birth certificate|deport|detention|migrant|immigration|rohingya|bajau|"
    r"tanpa kewarganegaraan|kewarganegaraan|warganegara|pelarian|pendatang|"
    r"imigresen|imm13|mykas|sijil lahir|dokumen pengenalan|tahanan|pati\b|\bpti\b)", re.I)

TAG_RULES = [
    ("無国籍",   r"stateless|tanpa kewarganegaraan|tiada warganegara|tanpa dokumen"),
    ("難民",     r"refugee|asylum|unhcr|pelarian|suaka"),
    ("市民権",   r"citizenship|kewarganegaraan|warganegara|mykas|imm13|imm 13|"
                 r"birth certificate|sijil lahir|dokumen|permohonan"),
    ("入管・摘発", r"immigration|imigresen|deport|detention|detained|arrest|raid|"
                 r"ditahan|serbuan|operasi|pati\b|\bpti\b|depot|penguatkuasaan"),
    ("子ども",   r"child|children|kid|teen|minor|kanak-kanak|anak|remaja|pelajar|student"),
    ("教育",     r"school|education|learning cent|classroom|pendidikan|sekolah|pusat pembelajaran"),
    ("医療・福祉", r"health|hospital|clinic|medical|vaccin|malnutri|kesihatan|hospital|klinik|perubatan"),
    ("バジャウ",  r"bajau|sea gypsy|sea nomad|pala'u|palauh"),
    ("ロヒンギャ", r"rohingya"),
    ("政策・政治", r"government|minister|ministry|parliament|policy|cabinet|assembly|bill|act\b|"
                 r"kerajaan|menteri|kementerian|parlimen|dasar|dewan|rang undang"),
    ("人権・NGO", r"human rights|ngo|civil society|suhakam|activist|advocacy|"
                 r"hak asasi|masyarakat sipil|pertubuhan"),
]
TAG_RULES = [(name, re.compile(pat, re.I)) for name, pat in TAG_RULES]

# "Daily Sabah" はトルコの日刊紙。州名と紛らわしいだけで無関係
EXCLUDE_SOURCES = {"daily sabah", "dailysabah.com"}


def is_relevant(text):
    if not TOPIC_RE.search(text):
        return False
    return bool(SABAH_RE.search(text) or STRONG_RE.search(text))


def auto_tags(text):
    return [name for name, rx in TAG_RULES if rx.search(text)]


# ---------------------------------------------------------------- 報道の広がり
#
# 同じ出来事でも「サバ州のローカル紙だけが書いている」のか「全国紙が取り上げた」
# のかで意味が違う。後者は州外にも届いている話題ということ。媒体のドメインで分ける。

LOCAL_DOMAINS = {
    # サバ州の媒体
    "sabahmedia.com", "dailyexpress.com.my", "sabahnews.com.my", "tvsabahnews.com",
    "sabahnewstoday.net", "jesseltontimes.com", "sabahpost.net", "nabalunews.com",
    "sayangsabah.com.my", "sabahkini2.com", "newsabahtimes.com.my", "sabahtoday.net",
    # ボルネオ島の地域紙（サバ版を持つ／州外には広がっていない扱い）
    "theborneopost.com", "utusanborneo.com.my", "tvsarawak.my", "dayakdaily.com",
}

NATIONAL_DOMAINS = {
    "nst.com.my", "thestar.com.my", "malaymail.com", "bernama.com", "bharian.com.my",
    "freemalaysiatoday.com", "astroawani.com", "malaysiagazette.com", "hmetro.com.my",
    "thevibes.com", "rtm.gov.my", "berita.rtm.gov.my", "newswav.com", "malaysiakini.com",
    "buletintv3.my", "optionstheedge.com", "theedgemalaysia.com", "utusan.com.my",
    "gempak.com", "sinarharian.com.my", "kosmo.com.my", "themalaysianreserve.com",
    "mkn.gov.my", "malaysiamadani.gov.my", "theedgemarkets.com", "says.com",
}


def domain_of(url):
    m = re.match(r"https?://([^/]+)", url or "")
    if not m:
        return ""
    host = m.group(1).lower()
    return host[4:] if host.startswith("www.") else host


def classify_scope(item):
    """local（州内・ボルネオ）/ national（マレーシア全国）/ foreign（海外）"""
    host = domain_of(item.get("url_real") or "")
    if not host:
        # 実URLが未解決のときは媒体名から推測する
        name = (item.get("source") or "").lower()
        if any(k in name for k in ("sabah", "borneo", "jesselton", "nabalu", "kinabalu")):
            return "local"
        return "national"

    for d in LOCAL_DOMAINS:
        if host == d or host.endswith("." + d):
            return "local"
    for d in NATIONAL_DOMAINS:
        if host == d or host.endswith("." + d):
            return "national"
    # 未知のドメイン。.my なら国内、そうでなければ海外とみなす
    return "national" if host.endswith(".my") else "foreign"


# ---------------------------------------------------------------- フィード解析

ATOM = "{http://www.w3.org/2005/Atom}"


def fetch_google_news(q, hl, gl, ceid):
    url = ("https://news.google.com/rss/search?" +
           urllib.parse.urlencode({"q": q, "hl": hl, "gl": gl, "ceid": ceid}))
    raw = http_get(url)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  ! XML解析失敗 ({q}): {e}", file=sys.stderr)
        return []

    lang = "ms" if hl.startswith("ms") else "en"
    out = []
    for it in root.findall("./channel/item"):
        title = strip_tags(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        source = strip_tags(it.findtext("source")) or ""
        if not title or not link:
            continue
        if source.lower() in EXCLUDE_SOURCES:
            continue
        out.append({
            "title": clean_title(title, source),
            "summary": "",
            "url": link,
            "source": source or "Google News",
            "date": iso(parse_date(it.findtext("pubDate"))),
            "lang": lang,
            "origin": "news",
            "query": q,
        })
    return out


def fetch_google_alert(feed_url):
    raw = http_get(feed_url)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  ! XML解析失敗 (alert): {e}", file=sys.stderr)
        return []

    out = []
    for en in root.findall(f"./{ATOM}entry"):
        title = strip_tags(en.findtext(f"{ATOM}title"))
        summary = strip_tags(en.findtext(f"{ATOM}content"))
        link_el = en.find(f"{ATOM}link")
        href = link_el.get("href") if link_el is not None else ""
        if not title or not href:
            continue
        # google.com/url?...&url=<実URL>&... から実URLを取り出す
        real = href
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            if qs.get("url"):
                real = qs["url"][0]
        except Exception:                                       # noqa: BLE001
            pass
        host = urllib.parse.urlparse(real).netloc.replace("www.", "")
        if host.lower() in EXCLUDE_SOURCES:
            continue
        out.append({
            "title": clean_title(title, host),
            "summary": summary,
            "url": real,
            "source": host or "Google Alert",
            "date": iso(parse_date(en.findtext(f"{ATOM}published"))),
            "lang": "ms" if re.search(r"\b(yang|dan|tidak|kerajaan|kanak)\b", title, re.I) else "en",
            "origin": "alert",
            "query": "google-alert",
        })
    return out


# ---------------------------------------------------------------- 翻訳

def translate(text, target="ja", source="auto"):
    """非公式の Google 翻訳エンドポイント。失敗したら None（アプリ側は原文を出す）"""
    text = (text or "").strip()
    if not text:
        return ""
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode({
        "client": "gtx", "sl": source, "tl": target, "dt": "t", "q": text[:1800],
    })
    raw = http_get(url, timeout=20, retries=1)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
        return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
    except Exception as e:                                      # noqa: BLE001
        print(f"  ! 翻訳解析失敗: {type(e).__name__} {e}", file=sys.stderr)
        return None


SENT_END = re.compile(r"(?<=[。！？])")


def translate_long(text, chunk=1500):
    """長い本文を文の切れ目で分割して訳し、つないで返す。

    翻訳エンドポイントは1回あたり2000字程度で頭打ちになるので、
    段落・文の境界で切る。途中で1つでも失敗したら None を返し、
    中途半端な訳文を残さない（次回まるごとやり直す）。
    """
    # 1) 段落に分ける。長すぎる段落だけ文で割る
    units = []
    for para in (p.strip() for p in text.split("\n\n")):
        if not para:
            continue
        if len(para) <= chunk:
            units.append(para)
            continue
        cur = ""
        for sent in SENT_END.split(para):
            if len(cur) + len(sent) > chunk and cur:
                units.append(cur)
                cur = ""
            cur += sent
        if cur:
            units.append(cur)

    # 2) 段落の切れ目を保ったまま、上限まで詰めて1回分にする
    parts, buf, n = [], [], 0
    for u in units:
        if n + len(u) > chunk and buf:
            parts.append("\n\n".join(buf))
            buf, n = [], 0
        buf.append(u)
        n += len(u) + 2
    if buf:
        parts.append("\n\n".join(buf))

    out = []
    for p in parts:
        ja = translate(p)
        if not ja:
            return None
        out.append(ja)
        time.sleep(0.3)
    return "\n\n".join(out).strip()


def summarize_ja(text, max_sentences=3, max_chars=220):
    """日本語の本文から抜き出し式の要約を作る。

    ニュースは逆ピラミッド型で第1文がほぼ要約なので、まず第1文を必ず採る。
    残りは「本文全体でよく出てくる語」を多く含む文から選び、元の順に並べ直す。
    外部APIを使わずに済ませるための素朴な方式だが、見出しの次に読む3行としては十分。
    """
    if not text:
        return ""
    body = text.replace("\n", " ")
    sents = [s.strip() for s in SENT_END.split(body) if len(s.strip()) > 15]
    if not sents:
        return text[:max_chars]
    if len(sents) <= max_sentences:
        return "".join(sents)[:max_chars]

    # 2文字以上の漢字・カタカナのまとまりを内容語とみなして数える
    words = re.findall(r"[一-龥]{2,}|[ァ-ヴー]{3,}", body)
    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1

    scored = []
    for i, s in enumerate(sents):
        toks = re.findall(r"[一-龥]{2,}|[ァ-ヴー]{3,}", s)
        if not toks:
            continue
        score = sum(freq.get(t, 0) for t in toks) / (len(toks) ** 0.5)
        scored.append((score, i))
    scored.sort(reverse=True)

    picked = {0}
    for _, i in scored:
        if len(picked) >= max_sentences:
            break
        picked.add(i)

    out = ""
    for i in sorted(picked):
        if len(out) + len(sents[i]) > max_chars and out:
            break
        out += sents[i]
    return out


def translate_missing(cfg):
    """news.json を読み、未訳の見出し・抜粋だけ翻訳して書き戻す。

    収集はしないので単体で何度でも回せる。enrich.mjs の直後に呼ぶ想定。
    """
    if not os.path.exists(OUT_PATH):
        print("news.json が無いので何もしません")
        return
    with open(OUT_PATH, encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items", [])

    budget = int(cfg.get("max_translations_per_run", 450))

    def needs(it):
        return (not it.get("title_ja")
                or (it.get("summary") and not it.get("summary_ja"))
                or (it.get("body") and not it.get("body_ja")))

    todo = [it for it in items if needs(it)]
    print(f"未訳: {len(todo)} 件（上限 {budget} 回）")

    done = 0
    saved_every = 20
    for n, it in enumerate(todo, 1):
        if done >= budget:
            print("  … 上限に達したので残りは次回に回します")
            break

        if not it.get("title_ja"):
            ja = translate(it["title"])
            if ja:
                it["title_ja"] = ja
            done += 1
            time.sleep(0.25)

        if it.get("summary") and not it.get("summary_ja"):
            ja = translate(it["summary"])
            if ja:
                it["summary_ja"] = ja
            done += 1
            time.sleep(0.25)

        # 本文は長いので分割翻訳。1回の実行で全部は終わらないこともある
        if it.get("body") and not it.get("body_ja"):
            ja = translate_long(it["body"])
            if ja:
                it["body_ja"] = ja
            done += max(1, len(it["body"]) // 1500)
            if n % 5 == 0 or n == len(todo):
                print(f"  {n}/{len(todo)} 本文訳 … （翻訳 {done} 回）")

        # 途中で落ちても成果を捨てないよう、こまめに書き出す
        if n % saved_every == 0:
            _write(payload)

    # 報道の広がりも付け直す（enrich で url_real が入った後に確定する）
    for it in items:
        it["scope"] = classify_scope(it)

    # 要約は訳文から作り直す（外部APIを使わない抜き出し式）
    for it in items:
        if it.get("body_ja"):
            it["summary_ja2"] = summarize_ja(it["body_ja"])
        elif it.get("summary_ja"):
            it["summary_ja2"] = summarize_ja(it["summary_ja"])

    _write(payload)
    print(f"翻訳完了: {done} 回 / 本文の和訳 {sum(1 for i in items if i.get('body_ja'))}"
          f"/{sum(1 for i in items if i.get('body'))} 件"
          f" / 要約 {sum(1 for i in items if i.get('summary_ja2'))} 件")


def _write(payload):
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- メイン

def main():
    do_translate = "--no-translate" not in sys.argv

    with open(FEEDS_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    # --translate-only: 収集はせず、news.json の未訳だけ埋める。
    # enrich.mjs が og:description から抜粋を足した「後」に走らせるための入口。
    # （収集→翻訳→解決 の順だと、解決で増えた抜粋が翌回まで英語・マレー語のまま残る）
    if "--translate-only" in sys.argv:
        translate_missing(cfg)
        return

    previous = {}
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                for it in json.load(f).get("items", []):
                    previous[it["id"]] = it
        except Exception as e:                                  # noqa: BLE001
            print(f"! 既存 news.json を読めませんでした: {e}", file=sys.stderr)
    print(f"既存: {len(previous)} 件")

    # ---- 収集
    raw_items = []
    for feed in cfg.get("google_alerts", []):
        got = fetch_google_alert(feed)
        print(f"[alert] {len(got):3d} 件  {feed[:60]}")
        raw_items += got
    for spec in cfg.get("google_news", []):
        got = fetch_google_news(spec["q"], spec.get("hl", "en-MY"),
                                spec.get("gl", "MY"), spec.get("ceid", "MY:en"))
        print(f"[news ] {len(got):3d} 件  {spec['q']}")
        raw_items += got
        time.sleep(0.4)

    # ---- 絞り込み・重複排除（Alert 由来を優先＝実URLと抜粋があるため）
    raw_items.sort(key=lambda x: 0 if x["origin"] == "alert" else 1)
    merged = {}
    for it in raw_items:
        blob = it["title"] + " " + it["summary"]
        if not is_relevant(blob):
            continue
        key = norm_key(it["title"])
        if not key:
            continue
        it["id"] = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        it["tags"] = auto_tags(blob)
        if it["id"] in merged:
            old = merged[it["id"]]
            if not old["summary"] and it["summary"]:
                old["summary"] = it["summary"]
            old["tags"] = sorted(set(old["tags"]) | set(it["tags"]))
            continue
        merged[it["id"]] = it
    print(f"関連あり: {len(merged)} 件（重複排除後）")

    # enrich.mjs / thumbs.mjs が付けた解決結果と、その和訳。
    # RSS を取り直しても必ず引き継ぐ。落とすと同じ記事を毎回ブラウザで開き直し、
    # 本文を訳し直すことになって実行が数十分伸びる。
    ENRICHED_KEYS = ("url_real", "image", "image_alt", "thumb", "site_name",
                     "enriched", "enriched_v", "body", "body_ja", "summary_ja2")

    # ---- 既存とマージ（既訳・既存の日付・解決済みの画像などは維持）
    for iid, it in merged.items():
        old = previous.get(iid)
        if old:
            it["title_ja"] = old.get("title_ja", "")
            it["first_seen"] = old.get("first_seen", it["date"])
            for k in ENRICHED_KEYS:
                if old.get(k):
                    it[k] = old[k]
            # 抜粋は og:description 由来（enrich が入れたもの）の方が中身がある。
            # RSS 側が空なら旧値を残し、既訳もそのまま使う。
            if not it["summary"] and old.get("summary"):
                it["summary"] = old["summary"]
            it["summary_ja"] = old.get("summary_ja", "") if it["summary"] == old.get("summary") else ""
            if old.get("origin") == "alert" and it["origin"] != "alert":
                it["url"], it["source"], it["origin"] = old["url"], old["source"], "alert"
        else:
            it["title_ja"] = ""
            it["summary_ja"] = ""
            it["first_seen"] = iso(datetime.now(timezone.utc))

    items = dict(previous)
    items.update(merged)

    # ---- 保持ポリシー
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(cfg.get("max_age_days", 400)))
    kept = [it for it in items.values() if parse_date(it.get("date")) >= cutoff]
    kept.sort(key=lambda x: x.get("date", ""), reverse=True)
    kept = kept[: int(cfg.get("max_items", 500))]

    # ---- 翻訳（未訳のものだけ）
    if do_translate:
        budget = int(cfg.get("max_translations_per_run", 450))
        todo = [it for it in kept if not it.get("title_ja")
                or (it.get("summary") and not it.get("summary_ja"))]
        print(f"翻訳対象: {len(todo)} 件（上限 {budget}）")
        done = 0
        for it in todo:
            if done >= budget:
                print("  … 翻訳上限に達したので残りは次回に回します")
                break
            if not it.get("title_ja"):
                ja = translate(it["title"])
                if ja:
                    it["title_ja"] = ja
                done += 1
                time.sleep(0.25)
            if it.get("summary") and not it.get("summary_ja"):
                ja = translate(it["summary"])
                if ja:
                    it["summary_ja"] = ja
                done += 1
                time.sleep(0.25)
            if done % 50 == 0:
                print(f"  翻訳 {done} 件…")
        print(f"翻訳完了: {done} 回")

    # ---- 書き出し
    # scope は url_real が解決されると変わりうるので毎回付け直す
    for it in kept:
        it["scope"] = classify_scope(it)

    sources = {}
    tag_counts = {}
    for it in kept:
        sources[it.get("source", "?")] = sources.get(it.get("source", "?"), 0) + 1
        for t in it.get("tags", []):
            tag_counts[t] = tag_counts.get(t, 0) + 1

    payload = {
        "updated": iso(datetime.now(timezone.utc)),
        "count": len(kept),
        "tags": dict(sorted(tag_counts.items(), key=lambda kv: -kv[1])),
        "sources": dict(sorted(sources.items(), key=lambda kv: -kv[1])),
        "items": kept,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    new_count = len([i for i in kept if i["id"] not in previous])
    print(f"\n書き出し: {len(kept)} 件（うち新着 {new_count} 件） → {OUT_PATH}")


if __name__ == "__main__":
    main()
