---
description: LLM Wiki の構造と内容の両面で健全性をチェックする。
argument-hint: "[--suggest-pages]"
---

`llm-wiki` スキルの lint ワークフローに従い、wiki に lint をかける。

引数（あれば）: $ARGUMENTS

0. `<WIKI_ROOT>` は、ユーザーが明示したパス、もっとも近いプロジェクトの指示、グローバルなエージェントの指示の順に探し、どれにもなければ現在のプロジェクトの `wiki/` とする。指示が別の wiki を指定しているときに、`${PWD}/wiki` を前提にしてはならない。
1. このセッションでまだ読んでいなければ、最初に `<WIKI_ROOT>/SCHEMA.md` を読む。追加の lint ルールが定義されていることがある。
2. 構造の lint スクリプトを実行する: `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_lint.py" "<WIKI_ROOT>"`。ユーザーが求めた場合は `--suggest-pages` を付ける。このスクリプトは、孤立ページ、リンク切れの wikilink、大きすぎるページ、frontmatter の欠落、更新の止まったページ、重複したスラッグを検出する。
2b. `<WIKI_ROOT>/graph/ontology.yaml` がある場合は、`uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_graph_lint.py" "<WIKI_ROOT>"` も実行する。型付きエッジの検出結果（未知の述語、証拠の欠落、リンク切れの目的語の参照、エイリアスの衝突）も、構造の検出結果と同じように仕分ける。つまり、修正案を示し、黙って適用しない。承認された修正を適用したら、`uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_graph_extract.py" "<WIKI_ROOT>"` の実行を提案する。
3. 構造の検出結果をユーザーと一緒に1件ずつ確認し、それぞれに修正案を示す。黙って修正を適用してはならない。1件ずつ、またはまとめての承認を待つ。
4. 内容の確認を行う。最近更新されたページと、もっとも多くリンクされているハブページを読む（`python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_search.py" "" --wiki "<WIKI_ROOT>" --top-linked 10` で見つけられる）。古いページとの矛盾、張られていない相互参照、多くのページで言及されているのに専用のページがないコンセプトを探す。
5. 足りていない点を挙げる。ユーザーが関心を示したのにまだ取り込んでいないトピックや、新しいソースによってすでに古くなった主張がこれに当たる。
6. 承認された修正を適用したら、変わったインデックスの項目を更新し、lint の結果をまとめた行を `log.md` に1行追記する。
7. lint のレポートが長すぎて、ユーザーが無理なく仕分けられない場合は、lint の頻度が合っていない（もっと頻繁にかけるべき）か、スキーマを見直す必要があるという兆候である。そのことをユーザーに指摘する。

**ホスト型 wiki。** 特定した wiki がホスト型の URL である場合（または指示でホスト型の URL が指定されている場合）は、`skills/llm-wiki/references/hosted-mode.md` のホストモードを使う。MCP ツールを使い、MCP が使えないときは `python3 "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_remote.py"` を使う。上の手順は、次のように置き換える。

- 手順 1: `wiki_overview` を呼び出して `SCHEMA.md` を得る。
- 手順 2 と 2b: `wiki_lint` を呼び出す。構造の検出結果、グラフの検出結果、未解決のレビューフラグが1つの結果にまとめて返る。extract の手順は不要である。
- 手順 4: `wiki_search` を `top_linked: 10` で呼び出し、そのあと `wiki_read` で読む。
- 手順 6: 承認された修正は `wiki_commit` で適用する（`edit`、または `base_rev` を付けた `move` と `delete`）。`move` はリンクを書き換えないので、参照元のページも同じコミットで編集する。`log.md` の行は `append` 操作で追加する。
- 未解決のレビューフラグは、ほかの検出結果と一緒に示す。ユーザーが判断してから、1件ずつ `wiki_remote.py review --resolve <id> --status accepted|dismissed` で解決する。

ユーザーが任意の判定レイヤーを有効にしている場合は、`wiki_graph_lint.py` に `--decide` を、`wiki_lint` に `decide: true` を付ける。ローカルの wiki では `wiki_decide.py contradictions` も提案する。詳しくは `skills/llm-wiki/references/decision-models.md` を参照。
