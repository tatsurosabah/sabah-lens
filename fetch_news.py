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


# ---------------------------------------------------------------- 政策上の重み
#
# 収集結果の6割が「PATI を何人捕まえた」という定型の摘発報道で、件数を眺めても
# 無国籍政策の状況はほとんど分からない。一方で判決・法改正・統計・国際機関の報告は
# 数は少ないが効いてくる。その差を機械的に点数化して、絞り込めるようにする。
#
# 外部のLLMは使わない。語の有無で加点する素朴な方式だが、「定型の摘発報道を沈めて
# 制度の話を浮かせる」という目的には十分機能する。

IMPACT_RULES = [
    # (加点, 説明, パターン)
    # 「法廷に送られた」は摘発報道の定型なので mahkamah/court 単体は使えない
    (3, "司法", r"(judicial review|semakan kehakiman|penghakiman|judgment|ruling|verdict|"
                r"rayuan|appeal|mahkamah (?:persekutuan|rayuan|tinggi)|federal court|"
                r"court of appeal|high court|litigation|class action|writ)"),
    # "Akta Imigresen 1959/63" は摘発報道が必ず引くので \bakta\b 単体は使えない
    (3, "立法・制度", r"(parlimen|parliament|dewan rakyat|dewan negara|kabinet|cabinet|"
                     r"rang undang|\bbill\b|pindaan akta|amendment|policy|dasar baharu|"
                     r"gazette|warta kerajaan|moratorium|reform|white paper|kertas putih)"),
    # "warganegara asing"（外国人）は摘発報道の常套句なので市民権の話とは見なさない
    (3, "市民権の実体", r"(citizenship|kewarganegaraan|stateless|tanpa kewarganegaraan|"
                       r"warganegara(?!\s+asing)|birth certificate|sijil (?:kelahiran|lahir)|"
                       r"mykas|imm13|imm 13|\bpss\b|regularis|naturalis|"
                       r"\u7121\u56fd\u7c4d|\u5e02\u6c11\u6a29|\u56fd\u7c4d\u3092)"),
    (2, "国際機関・人権", r"(unhcr|unicef|suhakam|human rights|hak asasi|"
                         r"suruhanjaya|ombudsman|amnesty|\bngo\b|civil society)"),
    (2, "調査・統計", r"(report|laporan|study|kajian|survey|tinjauan|statistic|statistik|"
                     r"\bdata\b|findings|index|census|banci|research|penyelidikan)"),
    (1, "要職の発言", r"(minister|menteri|ketua menteri|chief minister|timbalan|deputy|"
                     r"secretary[- ]general|director[- ]general|ketua pengarah|"
                     r"premier|governor|yang di-pertua)"),
    (1, "子ども・教育", r"(children|kanak-kanak|child|sekolah|school|education|pendidikan|"
                       r"alternative learning|pusat pembelajaran)"),
]
IMPACT_RULES = [(w, name, re.compile(p, re.I)) for w, name, p in IMPACT_RULES]

# 定型の摘発報道。これしか当てはまらない記事は沈める
ROUTINE_RE = re.compile(
    r"(operasi|\braid\b|ditahan|tahanan|arrested|detained|deport|dihantar pulang|"
    r"sweep|tangkap|cekup|serbuan|nabbed|round(?:ed)? up|kompaun)", re.I)


# これらのどれかに当たって初めて「制度の話」とみなす。
# 当局者のコメントや「報告」といった語は摘発報道にも普通に出てくるので、
# それだけで重要扱いすると定型記事が紛れ込む（実際に紛れ込んだので締めた）。
STRONG_NAMES = {"司法", "立法・制度", "市民権の実体"}


# 見出しが「◯◯人を拘束／送還した」型なら、本文に何が書いてあっても定型報道。
# 本文だけで判定すると、摘発記事が引く根拠法や法廷の語に引っぱられて重要扱いになる。
ROUTINE_TITLE_RE = re.compile(
    r"(?=.*\d)"
    r"(?=.*(pati|pendatang|warga asing|foreigner|illegal immigrant|undocumented|migrant|"
    r"\u4e0d\u6cd5\u6ede\u5728|\u4e0d\u6cd5\u79fb\u6c11|\u5916\u56fd\u4eba))"
    r"(?=.*(ditahan|tahan|cekup|dicekup|dihantar pulang|dipindahkan|deport|dipenjara|"
    r"arrest|detain|nabbed|round(?:ed)? up|sent home|repatriat|"
    r"\u62d8\u675f|\u9003\u6355|\u9001\u9084|\u5f37\u5236\u9001\u9084|\u6355|\u53ce\u5bb9))",
    re.I | re.S)


def impact_score(item):
    """0〜5。無国籍・市民権の議論にどれだけ効く記事かの目安"""
    title = " ".join(filter(None, [item.get("title", ""), item.get("title_ja", "")]))
    if ROUTINE_TITLE_RE.search(title):
        return 0

    text = " ".join(filter(None, [
        title, item.get("summary", ""), (item.get("body") or "")[:1500],
    ]))
    if not text.strip():
        return 1

    hits = {name for _, name, rx in IMPACT_RULES if rx.search(text)}
    raw = sum(w for w, name, _ in IMPACT_RULES if name in hits)
    strong = hits & STRONG_NAMES

    # 定型の摘発報道は、制度の話が絡まない限り沈める
    if ROUTINE_RE.search(text) and not strong:
        return 0 if raw <= 2 else 1
    if not hits:
        return 1

    score = min(5, round(raw * 5 / 11))
    return max(score, 3) if strong else min(score, 2)


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
    # 非公式エンドポイントなので連続で叩くと 429 が返る。
    # 429 のときだけ間隔を空けて数回粘る（諦めると本文が丸ごと未訳のまま残る）。
    raw = None
    for wait in (0, 6, 20, 45):
        if wait:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept-Language": "ja,en"})
            with urllib.request.urlopen(req, timeout=25, context=_SSL_CTX) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code != 429:
                print(f"  ! 翻訳 HTTP {e.code}", file=sys.stderr)
                return None
        except Exception:                                       # noqa: BLE001
            pass
    if not raw:
        print("  ! 翻訳が 429 続きで諦め（残りは次回）", file=sys.stderr)
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
        time.sleep(1.1)
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


# ---------------------------------------------------------------- 重複の統合
#
# 同じ記事が別々のクエリから拾われ、見出しの表記ゆれ（"… - TV Sabah" が付く／付かない）
# だけで別IDになることがある。一方で「144 PATI…」「255 PATI…」のように
# 数字だけ違う別の摘発記事もあるので、見出しの類似度だけで潰すと本物を消してしまう。
# そこで「実URLが同じ」か「同一媒体かつ数字が完全一致かつ見出しがほぼ同じ」に限る。

# サイト停止・エラーページ。ここに着地した実URLは記事を指していない
JUNK_URL_RE = re.compile(
    r"(suspendedpage|cgi-sys|account_suspended|/404|/error|page-not-found|"
    r"sorry\.google|consent\.google)", re.I)


def canon_url(u):
    if not u or not u.startswith("http") or JUNK_URL_RE.search(u):
        return ""
    u = re.sub(r"[?#].*$", "", u).rstrip("/")
    return u.lower()


def _title_tokens(t):
    t = re.sub(r"[^\w\s]", " ", (t or "").lower())
    return frozenset(w for w in t.split() if len(w) > 2)


def _digits(t):
    return tuple(sorted(re.findall(r"\d+", t or "")))


def _richness(it):
    """統合時にどちらを残すかの目安。中身が多いものを残す"""
    return (len(it.get("body") or ""), len(it.get("summary") or ""),
            1 if it.get("image") else 0, 1 if it.get("thumb") else 0)


def dedupe(items):
    """重複を畳んで、残した側にタグと first_seen をまとめる"""
    groups = {}          # 代表キー -> 代表アイテム
    order = []
    dropped = 0

    def merge_into(keep, drop):
        keep["tags"] = sorted(set(keep.get("tags", [])) | set(drop.get("tags", [])))
        fs = [x.get("first_seen") for x in (keep, drop) if x.get("first_seen")]
        if fs:
            keep["first_seen"] = min(fs)
        for k in ("image", "thumb", "summary", "body", "body_ja", "summary_ja",
                  "url_real", "site_name"):
            if not keep.get(k) and drop.get(k):
                keep[k] = drop[k]

    for it in items:
        key = None
        cu = canon_url(it.get("url_real"))
        if cu:
            key = ("url", cu)
        if key and key in groups:
            target = groups[key]
            if _richness(it) > _richness(target):
                merge_into(it, target)
                order[order.index(target)] = it
                groups[key] = it
            else:
                merge_into(target, it)
            dropped += 1
            continue

        # 同一媒体・数字一致・見出しほぼ同じ
        tk, dg, src = _title_tokens(it.get("title")), _digits(it.get("title")), it.get("source")
        hit = None
        for other in order:
            if other.get("source") != src:
                continue
            if _digits(other.get("title")) != dg:
                continue
            ot = _title_tokens(other.get("title"))
            if not tk or not ot:
                continue
            if len(tk & ot) / len(tk | ot) >= 0.8:
                hit = other
                break
        if hit is not None:
            if _richness(it) > _richness(hit):
                merge_into(it, hit)
                order[order.index(hit)] = it
            else:
                merge_into(hit, it)
            dropped += 1
            continue

        order.append(it)
        if key:
            groups[key] = it

    if dropped:
        print(f"重複を統合: {dropped} 件（残り {len(order)} 件）")
    return order


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
            time.sleep(0.9)

        if it.get("summary") and not it.get("summary_ja"):
            ja = translate(it["summary"])
            if ja:
                it["summary_ja"] = ja
            done += 1
            time.sleep(0.9)

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

    # 重要度は本文が入ってから確定するのでここで付け直す
    for it in items:
        it["impact"] = impact_score(it)

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
    # id まで見て並べる。date だけだと同点の記事の順番が実行ごとに入れ替わり、
    # 新着が 1 件も無くても news.json に差分が出て 6 時間ごとにコミットが積まれる。
    # 実測（SabahWatch 側）で 202 件中 110 件が他の記事と同じ date を持っていた。
    kept.sort(key=lambda x: (x.get("date", ""), x.get("id", "")), reverse=True)
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
    # 壊れた実URL（サイト停止・エラーページ）は持っていても害しかないので捨てる
    for it in kept:
        if it.get("url_real") and not canon_url(it["url_real"]):
            it.pop("url_real", None)
            it["enriched"] = "failed"

    kept = dedupe(kept)
    for it in kept:
        it["impact"] = impact_score(it)

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
