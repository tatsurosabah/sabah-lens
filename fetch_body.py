#!/usr/bin/env python3
"""本文が取れなかった記事を、別ルートで取り直す。

enrich.mjs は headless Chrome で記事ページを踏むが、Cloudflare を挟む媒体
（Daily Express / Borneo Post / MalaysiaGazette など）は30秒待っても抜けられない。
そこで読み取りプロキシ（r.jina.ai）を試す。これは相手のサイトを自前の基盤で取得して
本文だけをMarkdownで返すもので、APIキーは不要。

実測では万能ではない。Berita Harian(pressreader) や Sabah Media は取れるが、
Daily Express と MalaysiaGazette は同じく弾かれる。取れたぶんだけ埋める。

使い方: python3 fetch_body.py [--limit N] [--retry]
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "news.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

BLOCKED_RE = re.compile(
    r"(just a moment|security verification|captcha|attention required|"
    r"enable javascript|access denied|are you a robot)", re.I)

# Markdown から本文らしい段落だけ残す
DROP_LINE_RE = re.compile(
    r"^\s*(\!\[|\[|#{1,6}\s|\||\*\s*\*|>\s*$|Image \d|Title:|URL Source:|"
    r"Published Time:|Warning:|Markdown Content:)", re.I)

BODY_MAX = 12000


def read_via_jina(url, timeout=45):
    r = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout), "-A", UA,
         "https://r.jina.ai/" + url],
        capture_output=True)
    t = r.stdout.decode("utf-8", "replace")
    if not t or BLOCKED_RE.search(t[:1500]):
        return None
    # ヘッダ部を落とす
    t = re.sub(r"^.*?Markdown Content:", "", t, flags=re.S)
    lines = []
    for ln in t.split("\n"):
        ln = ln.strip()
        if not ln or DROP_LINE_RE.match(ln):
            continue
        # Markdown のリンク記法を落として素のテキストにする
        ln = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", ln)
        ln = re.sub(r"[*_`]{1,3}", "", ln).strip()
        if len(ln) < 40:
            continue
        lines.append(ln)
    body = "\n\n".join(dict.fromkeys(lines))[:BODY_MAX]
    return body if len(body) > 250 else None


def main():
    argv = sys.argv[1:]
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 10**9
    retry = "--retry" in argv

    with open(OUT, encoding="utf-8") as f:
        data = json.load(f)
    items = data["items"]

    todo = [it for it in items
            if not it.get("body") and it.get("url_real")
            and (retry or it.get("body_src") != "jina-failed")][:limit]
    print(f"本文なし {len(todo)} 件を読み取りプロキシで試す")

    ok = 0
    for n, it in enumerate(todo, 1):
        body = read_via_jina(it["url_real"])
        if body:
            it["body"] = body
            it["body_ja"] = ""          # 訳し直す
            it["body_src"] = "jina"
            it["enriched"] = "ok"
            ok += 1
        else:
            it["body_src"] = "jina-failed"
        if n % 5 == 0 or n == len(todo):
            print(f"  {n}/{len(todo)}  取得 {ok}")
            with open(OUT, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.write("\n")
        time.sleep(1.0)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    have = sum(1 for i in items if i.get("body"))
    print(f"\n完了: {ok} 件を追加 / 本文あり {have}/{len(items)}")


if __name__ == "__main__":
    main()
