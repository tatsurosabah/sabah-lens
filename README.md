# Sabah Lens

サバ州の無国籍・難民ニュースを **記事写真つきで** 追う個人用 PWA。
文字だけのリストで読む [sabah-watch](https://github.com/tatsurosabah/sabah-watch) のビジュアル版フォーク。
収集ロジックは共通で、見せ方と「記事の実URL・写真・抜粋を解決する工程」が違う。

- 公開URL: https://tatsurosabah.github.io/sabah-lens/
- 元アプリ（文字版）: https://tatsurosabah.github.io/sabah-watch/

## 何が違うか

| | sabah-watch | sabah-lens |
|---|---|---|
| 見せ方 | 文字リスト | 写真カード＋ヒーロー |
| 記事リンク | news.google.com 経由 | **媒体の実URL直リンク** |
| 写真 | なし | og:image（164件中127件＝77%） |
| 抜粋 | アラート由来のみ | **全記事**（og:description を和訳） |
| 収集時間 | 30秒 | 数分〜（新着分だけ解決するため） |

## 写真をどうやって取っているか

Google ニュースRSSには `media:content` も `enclosure` も無い。写真は記事ページの
`og:image` から取るしかないが、RSSのリンクは `news.google.com/rss/articles/…` という
署名付きの中継URLで、**HTTPだけでは実URLに解決できない**（中継ページの静的HTMLに
転送先が無く、JSを実行して初めて記事に飛ぶ）。

そこで `enrich.mjs` が headless Chrome を CDP で駆動し、実際にページを踏んで

- 着地した実URL → `url_real`
- `og:image` → `image`
- `og:description` → `summary`

を拾う。GitHub Actions の ubuntu ランナーには Chrome が入っているのでそれを使う。

**一度解決した記事には `enriched` が付き、二度と触らない。** 日々の実行で実際に
ブラウザを開くのは新着数件だけなので、初回だけ時間がかかる。

取れない記事もある。Daily Express / Borneo Post / Berita Harian は Cloudflare の
ボット判定に当たる（`enriched: "blocked"`）。これらは **タグ色のタイポグラフィカード**
として表示され、リンク自体は実URLで通る。

## iPhone に入れる

1. Safari で公開URLを開く
2. 共有ボタン → **ホーム画面に追加**
3. アイコンは丸いレンズ型。sabah-watch（四角い州旗）と並べても区別が付く

既読・保存・メモは端末内 `localStorage`（キー `sabahlens_state`）にのみ保存される。
sabah-watch とはキーが別なので、既読状態は共有されない。

## 手元で動かす

```bash
python3 -m http.server 8781 --directory .     # http://localhost:8781

python3 fetch_news.py                         # 収集＋見出しの翻訳
node enrich.mjs --conc 6                       # 実URL・写真・抜粋の解決（要 Node 22+ / Chrome）
python3 fetch_news.py --translate-only         # 増えた抜粋を和訳
python3 make_icon.py                           # アイコン再生成
```

`enrich.mjs` のオプション:

| | |
|---|---|
| `--limit N` | 解決する件数の上限（試すときに使う） |
| `--conc N` | 同時に開くタブ数。既定 5 |
| `--retry-failed` | `blocked` / `noimage` になった記事をもう一度試す |

macOS の python.org 版で SSL 証明書エラーが出る場合は `SW_INSECURE_SSL=1` を付ける。
Chrome の場所は `CHROME_PATH` で上書きできる。

## 更新するときの注意

- **`index.html` や `sw.js` を変えたら `sw.js` の `CACHE` の版番号を上げる**
  （`sabah-lens-v1` → `v2`）。上げないと古いキャッシュが返り続けて新版が反映されない。
  ローカルで確認するときも、変更が出ないと思ったらまずこれを疑う。
- 写真は各媒体のサーバーから直接読む（`referrerpolicy="no-referrer"`）。
  Service Worker が別枠のキャッシュに最大220枚まで貯め、古いものから捨てる。
- `fetch_news.py` の既存マージは `url_real` / `image` / `enriched` を必ず引き継ぐ。
  ここを落とすと毎回全件を解決し直すことになり、ジョブが数十分伸びる。

## ファイル

| ファイル | 役割 |
|---|---|
| `index.html` | アプリ本体（1枚完結） |
| `enrich.mjs` | 実URL・og:image・抜粋の解決（headless Chrome / CDP） |
| `fetch_news.py` | 収集・絞り込み・タグ付け・翻訳 |
| `feeds.json` | 収集元と絞り込みの設定 |
| `news.json` | 収集結果。Actions が自動更新 |
| `make_icon.py` | アイコン生成（`LAYOUT = "lens"`） |
| `sw.js` | Service Worker（オフライン表示・画像キャッシュ） |
| `.github/workflows/update.yml` | 1日4回の自動更新 |
