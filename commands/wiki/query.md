---
description: LLM Wiki に問い合わせ、蓄積した知識から出典つきで質問に答える。
argument-hint: "<質問>"
---

`llm-wiki` スキルのクエリワークフローに従い、wiki をもとに次の質問に答える。

質問: $ARGUMENTS

スキルに書かれているクエリの手順をすべて実行する。

0. `<WIKI_ROOT>` は、ユーザーが明示したパス、もっとも近いプロジェクトの指示、グローバルなエージェントの指示の順に探し、どれにもなければ現在のプロジェクトの `wiki/` とする。指示が別の wiki を指定しているときに `${PWD}/wiki` を前提にしてはならず、2つの wiki を黙って混ぜてもならない。
1. このセッションでまだ読んでいなければ、最初に `<WIKI_ROOT>/SCHEMA.md` を読む。
2. `<WIKI_ROOT>/index.md`（インデックスが分割されている場合は、該当する `<WIKI_ROOT>/indexes/<type>.md`）を読み、候補のページを見つける。
3. 候補のページを読む。`[[wikilinks]]` は必要なものだけをたどり、すべてのリンクを再帰的に追いかけない。
4. インデックスから適切な候補が見つからない場合は、ローカルのハイブリッド検索を実行する。API キーを調べたり、求めたりしてはならない。バックエンドは FastEmbed と sqlite-vec で、wiki やクエリのテキストを端末の外に送らない。依存パッケージが動かない場合、コマンドは自動的に語彙ベースの BM25 の結果を返す。
4b. `uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_search.py" "<query terms>" --wiki "<WIKI_ROOT>" --top 10 --cache --json` を実行する。依存パッケージを使わない、決定的な語彙ベースの検索にしたい場合は、代わりに `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_search.py" "<query terms>" --wiki "<WIKI_ROOT>" --top 10 --cache --json --no-embed` を実行する。`uv run --script` の形に `--no-embed` を付けても、uv は宣言されたセマンティック検索用のパッケージを解決してしまうので、付けてはならない。必要に応じて `--type concept` や `--tag <tag>` を使い、ページ単位で順位を付けたいときは `--granularity page` を使う。
5. 「X にリンクしているページは？」という質問には、grep ではなく `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_search.py" "" --wiki "<WIKI_ROOT>" --backlinks <slug>` を使う。
5b. 関係を問う質問（「X とつながっているものは？」「Y を提案したのは誰？」「A から B への経路をたどって」）で、`<WIKI_ROOT>/graph/graph.sqlite` がある場合は、ページを読む前に `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_graph_query.py" "<WIKI_ROOT>" neighbors --node <id>`（または `facts`、`edges`、`path`）を実行し、構造化された隣接ノードを得る。開く wiki ページは、型付きエッジの結果をもとに選ぶ。重要な主張については、グラフの行だけをもとに答えてはならない。
6. 使った wiki ページへの `[[wikilink]]` を出典として付け、回答をまとめる。矛盾がある場合は、どちらか一方を選ばず、矛盾をそのまま示す。
7. wiki に関係する内容がない場合は、そうはっきり伝える。話をでっち上げてはならない。足りない部分を補うために取り込むとよいソースを提案する。
8. 回答が新しいつながりを見いだすものであれば、今後のクエリで役立つように `<WIKI_ROOT>/synthesis/` に保存することを提案する。提案するのを基本とし、ささいな回答ならユーザーが断れるようにする。

**ホスト型 wiki。** 特定した wiki がホスト型の URL である場合（または指示でホスト型の URL が指定されている場合）は、`skills/llm-wiki/references/hosted-mode.md` のホストモードを使う。MCP ツールを使い、MCP が使えないときは `python3 "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_remote.py"` を使う。上の手順は、次のように置き換える。

- 手順 1 と 2: `wiki_overview` を呼び出す。`SCHEMA.md` と `index.md` が返る。分割されたインデックスは `wiki_read` で読む。
- 手順 3: `paths` を指定して `wiki_read` を呼び出す。
- 手順 4: `query` と `top`、必要に応じて `type`、`tag`、`since`、`granularity` のフィルターを指定して `wiki_search` を呼び出す。ローカルの検索スクリプトは実行しない。
- 手順 5: `backlinks: "<slug>"` を指定して `wiki_search` を呼び出す。
- 手順 5b: 同じ操作を `wiki_graph` で行う。
- 手順 8: `wiki_commit` を1回送る。シンセシスのページには `create` 操作、インデックスの行と `log.md` の行には `append` 操作を使う。

ユーザーが任意の判定レイヤーを有効にしている場合は、ローカルの検索に `--decide` を、`wiki_search` に `decide: true` を付ける。すべての行が除外された場合は、wiki にはおそらく答えがないとユーザーに伝える。詳しくは `skills/llm-wiki/references/decision-models.md` を参照。
