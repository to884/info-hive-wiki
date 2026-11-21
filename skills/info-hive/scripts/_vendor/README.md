# 同梱したサードパーティのパッケージ

`wiki_pages.py` は、同梱の Markdown 解析器で本文の wikilink を検出します。lint と統計は、別途パッケージをインストールせずに使えます。閲覧（browse）でも同じ解析器を使い、コードと本文を見分ける規則を 1 か所にまとめています（ADR-0010 の決定の 1）。

| パッケージ | 版 | 取得元の wheel | SHA-256 | ライセンス |
| --- | --- | --- | --- | --- |
| markdown-it-py（`markdown_it/`） | 3.0.0 | `markdown_it_py-3.0.0-py3-none-any.whl` | `355216845c60bd96232cd8d8c40e8f9765cc86f46880e43a8fd22dc1a1a8cab1` | MIT（[licenses/markdown-it-py.LICENSE](licenses/markdown-it-py.LICENSE)。移植元の markdown-it は [licenses/markdown-it.LICENSE](licenses/markdown-it.LICENSE)） |
| mdurl（`mdurl/`） | 0.1.2 | `mdurl-0.1.2-py3-none-any.whl` | `84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8` | MIT（[licenses/mdurl.LICENSE](licenses/mdurl.LICENSE)） |

- どちらも PyPI の wheel をそのまま展開したもので、中身は書き換えていません。2026年11月9日に、SHA-256 が PyPI の公表値と一致することを確認しました。
- markdown-it-py は mdurl に依存します。どちらも純粋な Python で、ほかの依存はありません。
- `wiki_pages.py` は、このディレクトリを `sys.path` の先頭に追加してから読み込みます。別の場所の `markdown_it` や `mdurl` が先に読み込まれていても、同梱したものに替えます。読み込んだものが同梱したもので、版が `MARKDOWN_IT_VERSION` と `MDURL_VERSION` と同じことを確かめ、違えば止まります。

## 版を上げるとき

1. 新しい版の wheel を取得し、SHA-256 を PyPI の公表値と照らします。
2. `markdown_it/` と `mdurl/` を入れ替え、ライセンスの文書も入れ替えます。
3. `wiki_pages.py` の `MARKDOWN_IT_VERSION` と `MDURL_VERSION` を上げます。読み込んだ版がこの定数と違えば止まります。MCP サーバーの版（`wiki_mcp_common.code_version`）は `_vendor/` の中身も含めて計算するので、動いているサーバーは入れ替わります。検索のパースキャッシュは、解析器の名前（`PARSER_ID`）が変わるので作り直されます。
4. 開発リポジトリの `tests/wiki/test_page_set.py` の例が成功することを確かめます。閲覧の中核も同じ版にそろえます。
