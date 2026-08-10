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
    todo = [it for it in items
            if not it.get("title_ja") or (it.get("summary") and not it.get("summary_ja"))]
    print(f"未訳: {len(todo)} 件（上限 {budget} 回）")
    if not todo:
        return

    done = 0
    for it in todo:
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
        if done and done % 50 == 0:
            print(f"  翻訳 {done} 回…")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    have = sum(1 for it in items if it.get("summary_ja"))
    print(f"翻訳完了: {done} 回 / 抜粋の和訳あり {have}/{len(items)} 件")


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

    # enrich.mjs が付けた解決結果。RSS を取り直しても必ず引き継ぐ。
    # （落とすと同じ記事を毎回ブラウザで開き直すことになり、実行が数十分伸びる）
    ENRICHED_KEYS = ("url_real", "image", "image_alt", "site_name", "enriched")

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
