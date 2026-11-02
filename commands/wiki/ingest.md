---
description: ソース（論文、記事、文字起こし、PDF、メモ）を LLM Wiki に取り込む。
argument-hint: "<ソースのパス、URL、または説明>"
---

`llm-wiki` スキルの取り込みワークフローに従い、ソースを wiki に取り込む。

ソース: $ARGUMENTS

スキルに書かれている取り込みの手順をすべて実行する。

0. `<WIKI_ROOT>` と `<RAW_ROOT>` は、ユーザーが明示したパス、もっとも近いプロジェクトの指示、グローバルなエージェントの指示の順に探し、どれにもなければ現在のプロジェクトの `wiki/` と `raw/` とする。指示が別の wiki を指定しているときに `${PWD}/wiki` を前提にしてはならず、2つの wiki を黙って混ぜてもならない。
1. このセッションでまだ読んでいなければ、最初に `<WIKI_ROOT>/SCHEMA.md` を読む。既定の規約がここで上書きされていることがある。
2. raw ソースがまだ `<RAW_ROOT>/` にない場合は、そこに置く。ファイル名はスラッグ化したものにする。
3. ソースを読む。大きい場合（約 5000 語を超える、または長い PDF）は分割して読む。コンテキストウィンドウの約 25% を超えるなら、全体を一度にコンテキストに読み込んではならない。
4. wiki に何かを書く前に、要点をユーザーと簡単に話し合う。目立つ点、既存のページとつながる点、意外な点を挙げる。
5. wiki を調べて、このソースが関係する既存のページを洗い出す。候補のページはそれぞれ読んで確かめる。
6. `<WIKI_ROOT>/sources/` にソースの要約ページを書く。関係するエンティティやコンセプトのページは、`str_replace` で必要な箇所だけを更新する。新しいエンティティやコンセプトには新しいページを作り、それぞれに少なくとも1つの被リンクを付ける。インデックスを更新し、`log.md` に1行追記する。
7. `<WIKI_ROOT>/graph/ontology.yaml` があり、今回の取り込みで型付きの `graph.relationships[]` を追加した、または追加できる場合は、ソースが明示的に裏づけている場合に限って型付きのエッジを追加する（述語、ソースページのスラッグ、証拠となる引用、確信度、ステータスを付ける）。次に `uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_graph_lint.py" "<WIKI_ROOT>"` を実行し、検出結果をユーザーと一緒に仕分けてから、`uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_graph_extract.py" "<WIKI_ROOT>"` を実行する。`log.md` の取り込みの行の下に、`   graph: +N nodes, +M typed edges` というサブ行を追記する。
8. 作業が終わったら、何をしたかをユーザーに伝える。更新したページ、作成したページ、指摘した矛盾、追って調べる価値のある点を報告する。

**ホスト型 wiki。** 特定した wiki がホスト型の URL である場合（または指示でホスト型の URL が指定されている場合）は、`skills/llm-wiki/references/hosted-mode.md` のホストモードを使う。MCP ツールを使い、MCP が使えないときは `python3 "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_remote.py"` を使う。上の手順は、次のように置き換える。

- 手順 1: `wiki_overview` を呼び出す。`SCHEMA.md` と `index.md` が返る。
- 手順 2: ファイルは `wiki_remote.py raw put <file>` でアップロードし、テキストのソースは `wiki_raw` の `put_text` で保存する。サーバーに raw ストレージがない場合は、ファイルをローカルに置いたままにし、そのパスと SHA-256 をソースページに記録する。
- 手順 3: 大きい raw ファイルは、`wiki_raw` の `read` に `offset` と `length` を指定して分割して読む。
- 手順 6: `wiki_commit` を1回送る。新しいページには `create`、更新するページには `base_rev` を付けた `edit`（ホスト版の `str_replace`）、インデックスの行と `log.md` の行には `append` を使う。競合が起きたら、ページを読み直して再試行する。ほかのエージェントの変更を上書きしてはならない。
- 手順 7: extract は実行しない。サーバーが同じコミットの中でグラフを更新する。`wiki_lint` を実行し、グラフの検出結果をユーザーと一緒に仕分ける。

wiki がまだない場合は、プロジェクトをまたいで使う個人用の wiki なら `/wiki:init --global`、プロジェクト用の wiki なら `/wiki:init`、ホスト型の wiki なら `/wiki:init --hosted <url>` を提案する。
