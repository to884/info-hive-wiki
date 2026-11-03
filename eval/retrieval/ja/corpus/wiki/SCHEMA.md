# 評価用 wiki のスキーマ

日本語の検索品質を測るためのフィクスチャである。ページの文章はすべてこのリポジトリのために書いたもので、出典として挙げる資料や会議は架空である。

## Retrieval

- Semantic backend: local FastEmbed + sqlite-vec（既定のモデルは `wiki_search.py` の `LOCAL_EMBED_MODEL`）。

ローカルの埋め込みの依存パッケージがあればハイブリッド検索を、なければ語彙検索だけを評価する。

## 境界のケース

- 日本語のファイル名: `concepts/消費税.md`、`concepts/全文検索.md`、`concepts/文字コード.md`、`entities/東京都.md`、`sources/文字化け障害報告.md`
- NFD のファイル名: `sources/熱中症予防ガイド要約.md`。リポジトリには NFC の名前で置き、`run_eval.py` が評価のときに一時ディレクトリへ複製して、名前を NFD（濁点を結合文字にした形）に変える
- BOM 付きのファイル: `sources/文字化け障害報告.md`
- frontmatter のキーが日本語のページ: `concepts/invoice-system.md`、`entities/mecab.md`
- 全角英数字: `concepts/unicode-normalization.md`、`concepts/bm25.md`
- 句点だけで区切った長い段落: `sources/search-study-notes.md`
