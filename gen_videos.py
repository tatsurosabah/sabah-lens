#!/usr/bin/env python3
"""サバ州の無国籍問題に関する動画を集めて videos.json を書き出す。

APIキーは要らない。YouTube の検索結果ページに埋まっている ytInitialData
（サーバー側で描画済みのJSON）から拾う。ChineseVocab の gen_suggest.py は
チャンネルRSSを使っているが、この題材は特定チャンネルに固まっていないので
検索でないと拾えない。

媒体の優先順位は日本 > アメリカ > 台湾 > サバ > マレーシア > その他。
同じ出来事でも、どの国のメディアが取り上げたかで見え方が変わるため。

使い方: python3 gen_videos.py
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "videos.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# sp=EgIQAQ%3D%3D は「動画のみ」の絞り込み。チャンネルや再生リストを弾く
VIDEO_ONLY = "EgIQAQ%3D%3D"

QUERIES = [
    # 日本語
    "サバ州 無国籍", "マレーシア 無国籍 子ども", "ボルネオ 無国籍",
    "マレーシア 無国籍 ドキュメンタリー",
    # 英語
    "Sabah stateless", "Sabah stateless children", "Sabah undocumented children",
    "Bajau Laut stateless", "Malaysia stateless children documentary",
    "Sabah invisible children",
    # 中国語（台湾メディア向け）
    "沙巴 无国籍", "沙巴 無國籍 兒童", "馬來西亞 無國籍",
    # マレー語
    "kanak-kanak tanpa kewarganegaraan Sabah", "tanpa kewarganegaraan Sabah dokumentari",
    "PATI Sabah dokumentari",
]

# チャンネル名の一部 -> (地域ラベル, 優先度)。小さいほど上に出す
MEDIA_PRIORITY = [
    # 日本。"ann" のような短い綴りは "Channel" に部分一致して誤判定するので境界を付ける
    (r"(\bnhk\b|テレ朝|annnews|\btbs\b|日テレ|テレビ東京|フジテレビ|"
     r"朝日新聞|毎日新聞|読売|共同通信|時事通信|\bjica\b|"
     r"国連広報|unhcr japan|日本国際|難民支援協会|"
     r"[ぁ-んァ-ヶ][ぁ-んァ-ヶー]{2,})", "日本", 1),
    # アメリカ
    (r"(\bcnn\b|new york times|nytimes|\bpbs\b|\bnpr\b|\bvoa\b|voice of america|"
     r"vice news|\bvox\b|abc news|nbc news|cbs news|bloomberg|time magazine|"
     r"national geographic)", "アメリカ", 2),
    # 台湾
    (r"(公視|公共電視|民視|tvbs|三立|中天|東森|慈濟|慈济|tzu.?chi|台視|華視|"
     r"鏡新聞|天下雜誌|報導者)", "台湾", 3),
    # サバ州の媒体
    (r"(sabah|daily express|borneo|jesselton|nabalu|kinabalu|sayang sabah)",
     "サバ", 4),
    # マレーシア・シンガポールの媒体
    (r"(malaysiakini|the star|astro awani|\brtm\b|bernama|new straits|malay mail|"
     r"free malaysia|\bcna\b|channel news ?asia|mediacorp|berita|tv3|ntv7|"
     r"malaysia|ml studios)", "マレーシア", 5),
    # 国際機関・国際メディア。優先指定には無いが、この題材では最も踏み込んだ
    # ドキュメンタリーがここにあるので「その他」に埋もれさせない
    (r"(al ?jazeera|\bunhcr\b|\bunicef\b|\bbbc\b|deutsche welle|\bdw\b|"
     r"\bafp\b|reuters|associated press|human rights watch|amnesty|"
     r"united nations|\bcgtn\b|scmp|south china morning)", "国際", 6),
]
MEDIA_PRIORITY = [(re.compile(p, re.I), label, pri) for p, label, pri in MEDIA_PRIORITY]

# 題材に合っているか。ここを緩めると旅行動画が大量に混ざる
# 「国籍」「citizenship」だけでは弱い。"多国籍スタッフ" や "北朝鮮国籍" まで拾って
# 無関係な動画が混ざったので、無国籍そのものを指す語に限定した。
RELEVANT_RE = re.compile(
    r"(stateless|tanpa kewarganegaraan|無国籍|無國籍|无国籍|"
    r"undocumented|invisible child|anak tanpa|"
    r"refugee|pelarian|難民|难民|"
    r"bajau|sea gypsy|sea nomad|orang laut|\bpati\b)", re.I)

SABAH_RE = re.compile(r"(sabah|沙巴|サバ|borneo|婆羅洲|ボルネオ|semporna|"
                      r"sandakan|tawau|kota kinabalu|bajau|malaysia|马来西亚|"
                      r"馬來西亞|マレーシア)", re.I)


def fetch(url):
    """Python の SSL 検証が通らない環境があるので curl に投げる"""
    r = subprocess.run(["curl", "-sSL", "--max-time", "30", "-A", UA,
                        "-H", "Accept-Language: en,ja;q=0.8", url],
                       capture_output=True)
    return r.stdout.decode("utf-8", "replace")


def walk_videos(obj):
    """ytInitialData を再帰的に辿って videoRenderer を拾う"""
    if isinstance(obj, dict):
        if "videoRenderer" in obj:
            yield obj["videoRenderer"]
        for v in obj.values():
            yield from walk_videos(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_videos(v)


def runs_text(node):
    if not node:
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(r.get("text", "") for r in node.get("runs", []))


# 見出しの文字種でも判断する。チャンネル名がローマ字の日本人発信者もいるため、
# 日本語（かな）が本文に出ていれば日本の発信とみなす方が実態に合う。
KANA_RE = re.compile(r"[ぁ-んァ-ヶ]")
TRAD_RE = re.compile(r"(無國籍|沙巴|馬來西亞|兒童|國籍|臺灣|華語)")


def classify(channel, title=""):
    for rx, label, pri in MEDIA_PRIORITY:
        if rx.search(channel or ""):
            return label, pri
    if KANA_RE.search(title):
        return "日本", 1
    if TRAD_RE.search(title):
        return "台湾", 3
    return "その他", 9


def search(q):
    url = ("https://www.youtube.com/results?search_query="
           + urllib.parse.quote(q) + "&sp=" + VIDEO_ONLY)
    html = fetch(url)
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        print(f"  ! 検索結果を読めず: {q}", file=sys.stderr)
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    out = []
    for v in walk_videos(data):
        vid = v.get("videoId")
        title = runs_text(v.get("title"))
        if not vid or not title:
            continue
        channel = runs_text(v.get("ownerText")) or runs_text(v.get("longBylineText"))
        desc = runs_text(v.get("detailedMetadataSnippets", [{}])[0].get("snippetText")) \
            if v.get("detailedMetadataSnippets") else ""
        blob = f"{title} {desc} {channel}"
        if not (RELEVANT_RE.search(blob) and SABAH_RE.search(blob)):
            continue
        label, pri = classify(channel, title)
        out.append({
            "vid": vid,
            "title": title.strip(),
            "channel": channel.strip(),
            "desc": desc.strip()[:220],
            "dur": runs_text(v.get("lengthText")),
            "published": runs_text(v.get("publishedTimeText")),
            "views": runs_text(v.get("viewCountText")),
            "region": label,
            "pri": pri,
            "q": q,
        })
    return out


def main():
    found = {}
    for q in QUERIES:
        got = search(q)
        new = 0
        for v in got:
            if v["vid"] not in found:
                found[v["vid"]] = v
                new += 1
        print(f"  {len(got):3d} 件 / 新規 {new:3d}   {q}")

    items = list(found.values())
    # 優先度 → 再生数 の順。再生数は "1.2M views" のような文字列なので数値化する
    def views_num(s):
        m = re.match(r"([\d,\.]+)\s*([KMB])?", (s or "").replace(",", ""))
        if not m:
            return 0
        n = float(m.group(1) or 0)
        return int(n * {"K": 1e3, "M": 1e6, "B": 1e9}.get(m.group(2) or "", 1))

    items.sort(key=lambda v: (v["pri"], -views_num(v.get("views"))))

    payload = {
        "updated": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"),
        "count": len(items),
        "items": items,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")

    from collections import Counter
    print(f"\n書き出し: {len(items)} 件 → {OUT}")
    print("地域別:", dict(Counter(v["region"] for v in items)))


if __name__ == "__main__":
    main()
