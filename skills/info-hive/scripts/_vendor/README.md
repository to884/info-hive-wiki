# 同梱したサードパーティのパッケージ

`wiki_pages.py` が本文の wikilink を拾うときに使う Markdown の解析器を、ここに同梱している。lint と統計を、何も入れずに動かすためである。閲覧（browse）の中核も同じものを使い、どこがコードでどこが本文かの規則を 1 か所にする（ADR-0010 の決定の 1）。

| パッケージ | 版 | 取得元の wheel | SHA-256 | ライセンス |
| --- | --- | --- | --- | --- |
| markdown-it-py（`markdown_it/`） | 3.0.0 | `markdown_it_py-3.0.0-py3-none-any.whl` | `355216845c60bd96232cd8d8c40e8f9765cc86f46880e43a8fd22dc1a1a8cab1` | MIT（[licenses/markdown-it-py.LICENSE](licenses/markdown-it-py.LICENSE)。移植元の markdown-it は [licenses/markdown-it.LICENSE](licenses/markdown-it.LICENSE)） |
| mdurl（`mdurl/`） | 0.1.2 | `mdurl-0.1.2-py3-none-any.whl` | `84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8` | MIT（[licenses/mdurl.LICENSE](licenses/mdurl.LICENSE)） |

- どちらも PyPI の wheel をそのまま展開したもので、中身は書き換えていない。SHA-256 は、PyPI が公表している値と一致することを確かめた（2026年11月9日）。
- markdown-it-py は mdurl に依存する。どちらも純粋な Python で、ほかの依存はない。
- `wiki_pages.py` は、このディレクトリを `sys.path` の先頭に足してから読み込む。別の場所の `markdown_it` や `mdurl` が先に読み込まれていても、同梱したものに替える。読み込んだものが同梱したもので、版が `MARKDOWN_IT_VERSION` と `MDURL_VERSION` と同じことを確かめ、違えば止まる。

## 版を上げるとき

1. 新しい版の wheel を取得し、SHA-256 を PyPI の公表値と照らす。
2. `markdown_it/` と `mdurl/` を入れ替え、ライセンスの文書も入れ替える。
3. `wiki_pages.py` の `MARKDOWN_IT_VERSION` と `MDURL_VERSION` を上げる。読み込んだ版がこの定数と違えば止まる。MCP サーバーの版（`wiki_mcp_common.code_version`）は `_vendor/` の中身も含めて計算するので、動いているサーバーは入れ替わる。検索のパースキャッシュは、解析器の名前（`PARSER_ID`）が変わるので作り直される。
4. 開発リポジトリの `tests/wiki/test_page_set.py` の例が通ることを確かめる。閲覧の中核も同じ版にそろえる。
