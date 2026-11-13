---
description: wiki の型付きグラフ層をコンパイル、lint、またはクエリする。
argument-hint: "extract | lint | neighbors --node <id> | edges --subject <id> | path --from <id> --to <id> | facts --about <id>"
argument-description: "最初の語でサブコマンドを選ぶ。`extract` はグラフをコンパイルし、`lint` は型付きエッジを検証する。`neighbors --node <id>`、`edges --subject <id> [--predicate <p>]`、`path --from <id> --to <id> [--max-depth N]`、`facts --about <id>` はコンパイル済みのグラフに問い合わせる。"
---

`info-hive` スキルのグラフワークフローに従い、wiki に対してグラフ層の操作を実行する。手順、終了コード、オントロジーがないときの扱いの正本は `skills/info-hive/references/graph-workflow.md` である。

引数: $ARGUMENTS

0. `<WIKI_ROOT>` は、ユーザーが明示したパス、もっとも近いプロジェクトの指示、グローバルなエージェントの指示の順に探し、どれにもなければ作業領域の設定（`.info-hive/workspace.json`）の `dirs.wiki`、それもなければ現在のプロジェクトの `wiki/` とする。指示が別の wiki を指定しているときに、`${PWD}/wiki` を前提にしてはならない。`<WIKI_ROOT>/SCHEMA.md` がなければ、初期化された wiki ではない（ほかの用途の `wiki/` のこともある）。MCP のツールもスクリプトも呼ばず、何も書かずに止まり、ユーザースコープの wiki なら `/wiki:init --user`、プロジェクトの wiki なら `/wiki:init` を提案する。別の場所の wiki に黙って切り替えたり、`SCHEMA.md` を自分で作ったりしてはならない。ツールやスクリプトが「初期化された wiki ではない」と返したときも同じにする。
1. `<WIKI_ROOT>/graph/ontology.yaml` があるかを確認する。ない場合は、グラフ層を導入する前の wiki である。スクリプトを実行しても、何も書き出さずに終了コード 1 で止まる（MCP のツールでは、`lint` の `graph.state` が `disabled` になり、`graph` はエラーを返す）。`/wiki:upgrade` で `graph/` を足すことを提案し、オントロジーの項目をでっち上げてはならない。
2. 最初の引数に応じて、次の処理を実行する。MCP のツールと CLI の使い分けは、`info-hive` スキルの「MCP のツールと CLI の使い分け」に従う。ツール名は接続に表示されたものを使う（Claude Code では `mcp__plugin_wiki_wiki__graph` など）。どのツールにも `wiki=<WIKI_ROOT の絶対パス>` を渡す。ツールが使えない場合は、矢印の右の CLI を実行する。
   - `extract` → いつも CLI で実行する（wiki の `graph/` に書き込むので、ツールにはない）。`uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_graph_extract.py" "<WIKI_ROOT>"`
   - `lint` → ツール `lint` の結果の `graph`（`state` が `checked` なら `findings`）を見る。構造の lint の結果も返るが、ここでは型付きエッジの検出結果だけを扱う。判定レイヤーの `--decide` が要るときと、ツールが使えないときは `uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_graph_lint.py" "<WIKI_ROOT>"`
   - `neighbors --node <id>` → ツール `graph` に `command="neighbors"`、`node=<id>`。CLI は `python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_graph_query.py" "<WIKI_ROOT>" neighbors --node <id>`
   - `edges --subject <id> [--predicate <p>]` → ツール `graph` に `command="edges"`、`subject=<id>`、任意の `predicate=<p>`。CLI は `python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_graph_query.py" "<WIKI_ROOT>" edges --subject <id> [--predicate <p>]`
   - `path --from <id> --to <id> [--max-depth N]` → ツール `graph` に `command="path"`、`from_node=<id>`、`to_node=<id>`、任意の `max_depth=N`。CLI は `python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_graph_query.py" "<WIKI_ROOT>" path --from <id> --to <id> [--max-depth N]`
   - `facts --about <id>` → ツール `graph` に `command="facts"`、`about=<id>`。CLI は `python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_graph_query.py" "<WIKI_ROOT>" facts --about <id>`
3. クエリ系のサブコマンドでは、結果を得たあと、関連の深いエッジをたどって元の wiki ページを開き、内容を要約する。引用には、グラフの行ではなく wiki ページへの `[[wikilinks]]` を使う。グラフは目的のページを早く見つけるためのものであり、証拠になるのは wiki ページとその raw ソースである。
4. `lint` では、検出結果を編集案として示す。型付きの `graph.relationships` を黙って書き換えてはならない。承認された修正を適用したら、コンパイル済みの成果物を markdown と同期させるために、`extract` を再実行するよう提案する。
5. `graph.sqlite` が `log.md` にある最新の取り込み（ingest）の行より古い場合は、先に `extract` を実行するよう提案する。
6. `extract` が途中で止まっても、前の `graph.sqlite` は残り、問い合わせを続けられる。新しい内容にするには `extract` をもう一度実行する。
