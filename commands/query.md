---
description: LLM Wiki に問い合わせ、蓄積した知識から出典つきで質問に答える。
argument-hint: "<質問>"
argument-description: "wiki に問い合わせる質問。日本語の質問は分かち書きせずにそのまま書く。"
---

`info-hive` スキルのクエリワークフローに従い、wiki をもとに次の質問に答える。

質問: $ARGUMENTS

スキルに書かれているクエリの手順をすべて実行する。

0. `<WIKI_ROOT>` は、ユーザーが明示したパス、もっとも近いプロジェクトの指示、グローバルなエージェントの指示の順に探し、どれにもなければ現在のプロジェクトの `wiki/` とする。指示が別の wiki を指定しているときに `${PWD}/wiki` を前提にしてはならず、2つの wiki を黙って混ぜてもならない。
1. このセッションでまだ読んでいなければ、最初に `<WIKI_ROOT>/SCHEMA.md` を読む。
2. `<WIKI_ROOT>/index.md`（インデックスが分割されている場合は、該当する `<WIKI_ROOT>/indexes/<type>.md`）を読み、候補のページを見つける。
3. 候補のページを読む。`[[wikilinks]]` は必要なものだけをたどり、すべてのリンクを再帰的に追いかけない。
4. インデックスから適切な候補が見つからない場合は、ローカルのハイブリッド検索を実行する。API キーを調べたり、求めたりしてはならない。検索は端末の中で行い、wiki やクエリのテキストを外に送らない。MCP のツールと CLI の使い分けは、`info-hive` スキルの「MCP のツールと CLI の使い分け」に従う。
4a. wiki の MCP サーバーのツール `query` が使える場合は、`wiki=<WIKI_ROOT の絶対パス>`、`query=<検索語>`、`top=10` を渡す（ツール名は接続に表示されたもの。Claude Code では `mcp__plugin_wiki_wiki__query`）。日本語の検索語は、分かち書きせずにそのまま渡す（「軽減税率の対象」）。語彙検索は日本語を2文字ずつ区切って照合するので、助詞を除いたり語を空白で区切ったりしなくてよい。必要に応じて `type="concept"`、`tags=["<tag>"]`、`since="YYYY-MM-DD"` で絞り込み、1 ページから返すセクションの数は `per_page` で変える。結果の形は CLI の `--json` と同じである。ツールが結果を返したら、0 件でも、同じ検索を CLI でやり直さない。結果に `warning` があるとき（初回の埋め込みの途中など）も、語彙検索はすでに wiki 全体を対象にしているので、その結果を使い、意味の検索が一部だけであることを回答に添える。
4b. ツールが使えない、呼び出しが返らない、またはページ単位の順位（`--granularity page`）か依存パッケージを使わない決定的な検索が要る場合は、CLI を使う。`uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_search.py" "<検索語>" --wiki "<WIKI_ROOT>" --top 10 --cache --json` を実行する。バックエンドは FastEmbed と sqlite-vec で、依存パッケージが動かない場合は自動的に語彙ベースの BM25 の結果を返す。依存パッケージを使わない、決定的な語彙ベースの検索にしたい場合は、代わりに `python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_search.py" "<検索語>" --wiki "<WIKI_ROOT>" --top 10 --cache --json --no-embed` を実行する。`uv run --script` の形に `--no-embed` を付けても、uv は宣言されたセマンティック検索用のパッケージを解決してしまうので、付けてはならない。必要に応じて `--type concept` や `--tag <tag>` を使い、ページ単位で順位を付けたいときは `--granularity page` を使う。ツールが使えずに CLI に戻ったときは、そのことをユーザーに伝える。
5. 「X にリンクしているページは？」という質問には、grep ではなく `python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_search.py" "" --wiki "<WIKI_ROOT>" --backlinks <slug>` を使う。MCP のツールにはこの機能がない。
5b. 関係を問う質問（「X とつながっているものは？」「Y を提案したのは誰？」「A から B への経路をたどって」）で、`<WIKI_ROOT>/graph/graph.sqlite` がある場合は、ページを読む前にグラフに問い合わせ、構造化された隣接ノードを得る。MCP のツール `graph` が使える場合は、`wiki=<WIKI_ROOT の絶対パス>` と `command="neighbors"`、`node=<id>` を渡す（`facts` は `about`、`edges` は `subject`、`path` は `from_node` と `to_node`）。使えない場合は `python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_graph_query.py" "<WIKI_ROOT>" neighbors --node <id>`（または `facts`、`edges`、`path`）を実行する。開く wiki ページは、型付きエッジの結果をもとに選ぶ。重要な主張については、グラフの行だけをもとに答えてはならない。
6. 使った wiki ページへの `[[wikilink]]` を出典として付け、回答をまとめる。矛盾がある場合は、どちらか一方を選ばず、矛盾をそのまま示す。
7. wiki に関係する内容がない場合は、そうはっきり伝える。話をでっち上げてはならない。足りない部分を補うために取り込むとよいソースを提案する。
8. 回答が新しいつながりを見いだすものであれば、今後のクエリで役立つように `<WIKI_ROOT>/synthesis/` に保存することを提案する。提案するのを基本とし、ささいな回答ならユーザーが断れるようにする。

ユーザーが任意の判定レイヤーを有効にしている場合は、MCP のツールではなく検索スクリプト（手順 4b の `uv run --script` の形）に `--decide` を付ける。すべての行が除外された場合は、wiki にはおそらく答えがないとユーザーに伝える。詳しくは `skills/info-hive/references/decision-models.md` を参照。
