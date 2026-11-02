---
description: wiki の型付きグラフ層をコンパイル、lint、またはクエリする。
argument-hint: "extract | lint | neighbors --node <id> | edges --subject <id> | path --from <id> --to <id> | facts --about <id>"
---

`llm-wiki` スキルのグラフワークフローに従い、wiki に対してグラフ層の操作を実行する。

引数: $ARGUMENTS

0. `<WIKI_ROOT>` は、ユーザーが明示したパス、もっとも近いプロジェクトの指示、グローバルなエージェントの指示の順に探し、どれにもなければ現在のプロジェクトの `wiki/` とする。指示が別の wiki を指定しているときに、`${PWD}/wiki` を前提にしてはならない。
1. `<WIKI_ROOT>/graph/ontology.yaml` があるかを確認する。ない場合は、グラフ層を導入する前の wiki である。ほかの作業に入る前に、`graph/` ディレクトリの初期化（`skills/llm-wiki/assets/` からテンプレートをコピーする）を提案する。オントロジーの項目をでっち上げてはならない。
2. 最初の引数に応じて、次の処理を実行する。
   - `extract` → `uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_graph_extract.py" "<WIKI_ROOT>"`
   - `lint` → `uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_graph_lint.py" "<WIKI_ROOT>"`
   - `neighbors --node <id>` → `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_graph_query.py" "<WIKI_ROOT>" neighbors --node <id>`
   - `edges --subject <id> [--predicate <p>]` → `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_graph_query.py" "<WIKI_ROOT>" edges --subject <id> [--predicate <p>]`
   - `path --from <id> --to <id> [--max-depth N]` → `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_graph_query.py" "<WIKI_ROOT>" path --from <id> --to <id>`
   - `facts --about <id>` → `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_graph_query.py" "<WIKI_ROOT>" facts --about <id>`
3. クエリ系のサブコマンドでは、スクリプトを実行したあと、関連の深いエッジをたどって元の wiki ページを開き、内容を要約する。引用には、グラフの行ではなく wiki ページへの `[[wikilinks]]` を使う。グラフは目的のページを早く見つけるためのものであり、証拠になるのは wiki ページとその raw ソースである。
4. `lint` では、検出結果を編集案として示す。型付きの `graph.relationships` を黙って書き換えてはならない。承認された修正を適用したら、コンパイル済みの成果物を markdown と同期させるために、`extract` を再実行するよう提案する。
5. `graph.sqlite` が `log.md` にある最新の取り込み（ingest）の行より古い場合は、先に `extract` を実行するよう提案する。

**ホスト型 wiki。** 特定した wiki がホスト型の URL である場合（または指示でホスト型の URL が指定されている場合）は、`skills/llm-wiki/references/hosted-mode.md` のホストモードを使う。MCP ツールを使い、MCP が使えないときは `python3 "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_remote.py"` を使う。サーバーはコミットのたびにグラフを最新に保つので、`extract` は不要である。各操作は `wiki_graph`（または `wiki_remote.py graph <op>`）の `neighbors`、`edges`、`path`、`facts` に対応させる。JSONL や GraphML のファイルが必要なときは `export` を使う。`lint` は `wiki_lint` に対応する。サーバーに `graph/ontology.yaml` がない場合は、`wiki_commit` の `create` 操作でテンプレートから作成するよう提案する。

詳しいリファレンス: `skills/llm-wiki/references/graph-workflow.md`
