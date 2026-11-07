---
description: LLM Wiki の構造と内容の両面で健全性をチェックする。
argument-hint: "[--suggest-pages]"
argument-description: "`--suggest-pages` を付けると、独立したページにすべきコンセプトの候補も挙げる。"
---

`info-hive` スキルの lint ワークフローに従い、wiki に lint をかける。

引数（あれば）: $ARGUMENTS

0. `<WIKI_ROOT>` は、ユーザーが明示したパス、もっとも近いプロジェクトの指示、グローバルなエージェントの指示の順に探し、どれにもなければ作業領域の設定（`.info-hive/workspace.json`）の `dirs.wiki`、それもなければ現在のプロジェクトの `wiki/` とする。指示が別の wiki を指定しているときに、`${PWD}/wiki` を前提にしてはならない。
1. このセッションでまだ読んでいなければ、最初に `<WIKI_ROOT>/SCHEMA.md` を読む。追加の lint ルールが定義されていることがある。
2. 構造の lint をかける。MCP のツールと CLI の使い分けは、`info-hive` スキルの「MCP のツールと CLI の使い分け」に従う。wiki の MCP サーバーのツール `lint` が使える場合は、`wiki=<WIKI_ROOT の絶対パス>` を渡す（ツール名は接続に表示されたもの。Claude Code では `mcp__plugin_wiki_wiki__lint`）。ユーザーが求めた場合は `suggest_pages=true` を渡す。`SCHEMA.md` がページの大きさの上限や必須の frontmatter を既定と違う値に決めている場合は、`soft_cap`、`hard_cap`、`soft_char_cap`、`hard_char_cap`、`required_fm` にその値を渡す。ツールが使えない場合は、`python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_lint.py" "<WIKI_ROOT>"` を実行する（`--suggest-pages`、`--soft-cap` などの同じ意味の引数がある）。この検査は、孤立ページ、リンク切れの wikilink、大きすぎるページ（空白を除いて 10,000 文字か 100 行を超えると警告、25,000 文字か 200 行を超えると要分割）、frontmatter の欠落、更新の止まったページ、重複したスラッグ、参照先のない画像（`missing_assets`）を検出する。ページの大きさには、コードフェンス（mermaid の図を含む）の中身と画像だけの行を数えない。日本語で書いた wiki に特有の誤りとして、frontmatter の全角の「：」、「、」で区切ったリスト、英語と日本語の別名の重複、`YYYY-MM-DD` でない日付、wikilink の中の全角の「｜」と「＃」、二重引用符で囲まれていない文字列の値も指摘する。
2b. グラフの lint の結果も見る。ツールの `lint` は、`<WIKI_ROOT>/graph/ontology.yaml` があれば、グラフの lint の結果を `graph.findings` に入れて返す（`graph.state` が `checked`）。`disabled` はオントロジーがない wiki、`error` はオントロジーを読めないことを表す。CLI を使った場合と、判定レイヤーの `--decide` が要る場合は、オントロジーがあれば `uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_graph_lint.py" "<WIKI_ROOT>"` も実行する。型付きエッジの検出結果（未知の述語、証拠の欠落、リンク切れの目的語の参照、エイリアスの衝突）も、構造の検出結果と同じように仕分ける。つまり、修正案を示し、黙って適用しない。承認された修正を適用したら、`uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_graph_extract.py" "<WIKI_ROOT>"` の実行を提案する。
3. 構造の検出結果をユーザーと一緒に1件ずつ確認し、それぞれに修正案を示す。黙って修正を適用してはならない。1件ずつ、またはまとめての承認を待つ。
4. 内容の確認を行う。最近更新されたページと、もっとも多くリンクされているハブページを読む（ツールの `stats` の `most_linked` に上位 10 件がある。ツールが使えなければ `python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_search.py" "" --wiki "<WIKI_ROOT>" --top-linked 10` で見つけられる）。古いページとの矛盾、張られていない相互参照、多くのページで言及されているのに専用のページがないコンセプトを探す。
5. 足りていない点を挙げる。ユーザーが関心を示したのにまだ取り込んでいないトピックや、新しいソースによってすでに古くなった主張がこれに当たる。
6. 承認された修正を適用したら、変わったインデックスの項目を更新し、lint の結果をまとめた行を `log.md` に `## [YYYY-MM-DD] lint | 構造の修正 N 件、内容の修正 M 件、不足の提案 K 件` の形で1行追記する。
7. lint のレポートが長すぎて、ユーザーが無理なく仕分けられない場合は、lint の頻度が合っていない（もっと頻繁にかけるべき）か、スキーマを見直す必要があるという兆候である。そのことをユーザーに指摘する。

ユーザーが任意の判定レイヤーを有効にしている場合は、`wiki_graph_lint.py` に `--decide` を付ける。`wiki_decide.py contradictions` も提案する。詳しくは `skills/info-hive/references/decision-models.md` を参照。
