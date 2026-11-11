# 変更履歴

このプラグインの主な変更は、すべてこのファイルに記録する。

書式は [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) にもとづき、このプロジェクトは [Semantic Versioning](https://semver.org/spec/v2.0.0.html) に従う。

## [Unreleased]

## [0.9.2] - 2026-11-11

### Fixed

グラフの抽出（`wiki_graph_extract.py`）で、エッジが別のページのノードに付く、または黙って消える不具合を直した。閲覧（browse）の関係表は、エッジを書いたページで関係を振り分けるので、それに必要な形にそろえた。

- 別のディレクトリに同じスラッグのページ（`a/Foo.md` と `b/Foo.md`）があると、ノードをスラッグで引いていたので、後に読んだページが前のページを上書きしていた。別の `graph.node_id` を付けても防げなかった。
  - `graph.relationships` の型付きの関係、`mentions`、`sourced_from`、`summarizes_raw` の主語が、別のページのノードになっていた。今は、そのエッジを書いたページのノードになる。
  - パスを付けた `[[a/Foo]]` も、別のページのノードを指していた。今は、そのページを指す。`a/Foo.md` から `[[b/Foo]]` へのリンクは、自分へのリンクとして捨てていたが、今は `mentions` のエッジになる（2 つのページが同じノード ID を持つときは、今までどおり作らない）。
  - パスのない `[[Foo]]` と `sources:` の `Foo` は、同じスラッグのページが複数あるとき、どれにも寄せず、エッジを作らない（後に読んだページを選んでいた）。重なったスラッグは、wiki の lint の `duplicate_slugs` が挙げる。スラッグが 1 つしかないページへのリンクは、今までどおりである。
  - 同じスラッグの 2 つのノード（別の `graph.node_id` か、既定の node_id で型が違うもの。`concept:Foo` と `product:Foo` など）があると、`graph.sqlite` の `nodes.slug` の UNIQUE 制約に反し、抽出が止まってグラフを作れなかった。`nodes.slug` の UNIQUE を外した。問い合わせ（`wiki_graph_query.py` と MCP のツール `graph`）はノードを ID で引くので、影響はない。
- 別のページに同じ内容の関係を書くと、エッジが 1 本にまとまり、後のページの `page` が失われていた。エッジの ID にページ（ディスク上の相対パス）を入れ、別のエッジにした。NFC と NFD の名前の違いだけの 2 つのファイルも、`page` は同じ NFC のパスになるが、別のエッジになる。
- 同じページの `graph.relationships` の 2 つの項目が、`status` や `valid_from`、`valid_to` だけ違うと、エッジが 1 本にまとまっていた（エッジの ID が、主語、述語、目的語、出典、証拠しか見ていなかった）。今は、エッジにする項目（`predicate` と `object` があり、目的語のノード ID が曖昧でないもの）1 つから、必ず 1 本のエッジを作る。エッジの ID には、主語、述語、目的語、出典、証拠に加えて `confidence`、`status`、`valid_from`、`valid_to`、`notes`、`raw_ref` と、`contradicts`、`supersedes` のうちエッジの ID の形（16 進 24 桁）でない値を入れ、これらがすべて同じ項目は、ページの中で出てきた順の番号で分ける。`contradicts` と `supersedes` に書いたエッジの ID は ID に入れないので、互いのエッジの ID を書いても、自分の ID は変わらない。ID に入る欄の違う項目を並べ替えても、ID は変わらない。エッジの ID を書いた参照の欄か未知の欄だけが違う項目は、並べ替えると ID が別の項目に移りうるので、グラフの lint が `order_dependent_edge_ids` に挙げる。
- 別のスラッグのページに同じ `graph.node_id` を付けると、後のページの関係が黙って消えていた。今は、ノードは最初のページから 1 つだけ作り、どのページの関係も、そのノードを主語にして残す。そのノード ID を目的語にした型付きの関係は、どのページのノードか決められないので、エッジにしない。
- `sources:` にパス（`sources/paper`）か `[[sources/paper]]` を書くと、lint の `unresolved_sources` は解決するのに、`sourced_from` のエッジを作らなかった。今は、lint の `unresolved_sources` と同じ関数（`wiki_pages.classify_source_ref`）で、外側の `[[ ]]` を外し、wikilink の規則でページに解決する。ページが 1 つなら `sourced_from` を作る（source でないページを指すときも作り、lint の `unresolved_sources` が `not_source` として挙げる）。`sources: paper` のように 1 つの値を文字列で書いたときは、1 文字ずつ別の値として読んでいたが、今は 1 つの値として読む。
- グラフの lint（`wiki_graph_lint.py` と MCP のツール `lint` の `graph`）の `orphan_typed_nodes` も、同じ理由で、関係を書いたページを孤立と挙げ、別のページを孤立から外していた。今は、抽出と同じエッジで判断する。
- グラフの lint は、`object: 1` のような数値の目的語を、`graph.node_id: 1` のノードに当たらないとして `broken_object_refs` に挙げていた。抽出と同じく、文字列にしてノード ID と比べる。

### Added

- グラフの lint に、抽出がエッジにしなかった参照の検出結果を足した。`ambiguous_object_refs`（目的語のノード ID を持つページが複数ある型付きの関係）、`ambiguous_sources`（`sources:` の値に当たるページが複数ある）、`unresolved_sources`（当たるページがない）、`order_dependent_edge_ids`（並べ替えるとエッジの ID が入れ替わりうる関係の項目）である。どれも `summary` に件数が入る。抽出も、これらと `duplicate_node_ids` にあたるものがあれば、件数を標準エラーに書く。
- `build_nodes()` と `build_edges()` は、引数 `diagnostics` にリストを渡すと、上の診断を足す。`sources:` の診断は、`sourced_from` を作らないソースページでも挙げる。
- `graph.graphml` のエッジに、`page`（そのエッジを書いたページ）を足した。
- `graph.relationships` の `notes` などに、`2:` のような数値のキーと文字列のキーが混ざっていると、`graph.sqlite` と `edges.jsonl` を書くところで止まっていた。キーを JSON と同じ規則で文字列にそろえてから書く（`2` は `"2"`、`true` は `"true"` で、今まで書けていた出力は変わらない）。文字列のキーとぶつかる数値などのキー（`2` と `"2"` が両方あるとき）は、`"2#int"` のように型の名前を付けて、値を消さない。

### Changed

- wiki の lint の `unresolved_sources` は、パスを付けた値（`concepts/paper`）を、そのパスのページの `type` で判断するようになった。今までは同じスラッグのページすべての `type` で判断していたので、`sources/paper.md` があると、source でない `concepts/paper.md` を指す値を挙げなかった。パスのない値の判断は変わらない。
- エッジの ID にページが入ったので、すべてのエッジの ID が変わる。`contradicts:` や `supersedes:` にエッジの ID を書いていたら、作り直したあとの ID に直す（グラフの lint は、エッジの ID の参照を確かめていない）。
- `build_nodes()` の 2 つ目の返り値は、スラッグからではなく、ページ（ディスク上の相対パス）から node_id を引く対応になった。`build_edges()` はそれを受け取る。`graph.node_id` に文字列でない値（`1`）を書いたときは、lint と同じく文字列（`"1"`）のノード ID にする。
- グラフ層を使っている wiki は、`wiki_graph_extract.py` でグラフを作り直す。作り直すまで、`graph/graph.sqlite` などと MCP のツール `graph` は、古い ID と、別のページのノードに付いたエッジを返す。

## [0.9.1] - 2026-11-09

### Fixed

- `init_wiki.py` が書き出す `SCHEMA.md` と `graph/README.md` のコマンドの例で、スクリプトのパスが、wiki を使う側のどこから実行しても解決しなかった。ひな形は `scripts/wiki_graph_extract.py`（スキルのディレクトリから見たパス）と `skills/info-hive/scripts/wiki_search.py`（プラグインから見たパス）を混ぜて書いていたが、どちらも、プロジェクトのルートからは見つからない。
  - ひな形では `"<skill-root>/scripts/..."` と書き、同じファイルの中で、`<skill-root>` はインストールした info-hive スキルのディレクトリ（Claude Code のプラグインでは `${CLAUDE_PLUGIN_ROOT}/skills/info-hive`）の実際のパスに置き換えると説明するようにした。`${CLAUDE_PLUGIN_ROOT}` はプラグインのコマンドの中で展開されるもので、書き出したファイルの中では展開されず、シェルでも設定されていないことがあるので、そのまま書かない。初期化のときに実際のパスを書き込むこともしない。プラグインの版ごとにパスが変わり、マシンにも依存するからである。
  - スクリプトのパスは `"` で囲み、パスに空白があっても引数が分かれないようにした。
  - `SCHEMA.md` の検索の例に `--wiki wiki/` を付けた。今までは対象の wiki を指定していなかったので、`--wiki-dir` で別の名前にした wiki では、作業領域の設定がなければ `./wiki` を検索していた。`wiki/` は、別の名前で初期化した wiki では置き換えるよう、`SCHEMA.md` と `graph/README.md` に書いた。
  - 既存の wiki の `SCHEMA.md` と `graph/README.md` は書き換えない。見出しは変えていないので、`init_wiki.py --upgrade` も差分を案内しない。直したいときは、ひな形（`assets/SCHEMA.md.template` の「Retrieval」と「Graph layer」、`assets/graph_README.md.template`）に合わせて手で直す。

## [0.9.0] - 2026-11-09

### Added

- lint（`wiki_lint.py` と MCP のツール `lint`）に `case_only_duplicates` を足した。`concepts/Kalman.md` と `concepts/kalman.md`、`Concepts/` と `concepts/` のように、大文字と小文字だけが違うページかディレクトリを挙げる。Windows と macOS の既定のファイルシステムでは同じ場所を取るので、閲覧のための書き出しで困る。1 件は `{"kind": "file" | "dir", "paths": [...]}` で、`summary.case_only_duplicates` に件数が入る。報告するだけで、wiki は直さない。
- ページの集合と wikilink の抜き出しと解決の規則を、新しい `scripts/wiki_pages.py` の 1 か所にまとめた。lint、検索（MCP の `query` の索引を含む）、統計、グラフ、判定レイヤーがこれを使う。閲覧（browse）の準備である（ADR-0010 の決定の 1）。
- Markdown の解析器 markdown-it-py 3.0.0 と mdurl 0.1.2（どちらも MIT ライセンス）を、`scripts/_vendor/` に同梱した。lint と統計は、今までどおり何も入れずに動く。取得元、SHA-256、ライセンスは `scripts/_vendor/README.md` にある。

### Changed

- コードブロック（フェンスと字下げ）とインラインのコードの中の `[[...]]` を、wikilink として数えなくなった。lint のリンク切れと被リンク、検索の `links`、グラフの `mentions` のエッジ、統計のリンクの数、判定レイヤーの矛盾の候補が変わる。本文は、閲覧が使うものと同じ Markdown の解析器（markdown-it-py 3.0.0、commonmark、表を有効）で読み、本文の文字とするところだけからリンクを拾う。引用とリストの入れ子の中のフェンス、行をまたぐコードスパン、表のセル、参照リンクの定義（`[ref]: url "タイトル"`）の中も、閲覧と同じに扱う。`\[\[x]]` のように角括弧を escape したものは wikilink として数え、`[[R&amp;D]]` のような文字参照は元の文字（`R&D`）に戻してから解決する。
- 検索（`wiki_search.py` と MCP の `query`）が、wiki の最上位の `README.md` と `assets/` の中の Markdown を対象にしなくなった。lint と統計は `.wiki-cache/` の中の Markdown を、グラフは `assets/` と `.wiki-cache/` の中の Markdown を、ページとして数えなくなった。どれも、lint が数えていたページの集合にそろえた。外れるページがある wiki では、検索の BM25 の文書数が変わるので、残ったページの順位も変わりうる。MCP の `get_page` は `README.md` を返さなくなった。
- 検索のセクションの分割が、コードブロックの中の `#` と、`#　見出し` のように `#` のあとが全角空白の行で分かれなくなった（閲覧でも見出しにならない）。検索のパースキャッシュ（`search-index.json`）は、形式の番号を上げたので自動で作り直される。
- グラフ層を使っている wiki は、`wiki_graph_extract.py` でグラフを作り直す。0.8.0 で作った `graph/graph.sqlite` などは、作り直すまで、コードの中の `[[...]]` によるエッジと、`assets/` の中のノードを含んだままである。MCP のツール `graph` も、作り直すまで古い結果を返す。

## [0.8.0] - 2026-11-09

### Added

- 取り込み（`/wiki:ingest`）で、ソースの要約ページの `## 関係するページ` の直前に `## 主張と行き先` の表を置くようにした。抜き出した主張ごとに、候補の番号、主張と成り立つ条件、根拠の位置、行き先、理由を 1 行で書く。行き先は `create`（新しいページを作って載せた）、`update`（既存のページに足した）、`source_only`（ソースの要約ページだけに残した）のどれかで、英語の識別子のまま書く。`create` と `update` の理由には、作ったページか更新したページを wikilink で書く。何を独立したページにし、何をソースの要約ページだけに残したかを、あとで確かめられるようにするためである。
  - 表は取り込みで実際にしたことの記録で、表のためにページを作ったり更新したりはしない。lint はこのセクションを検査しない。
  - 手順は `references/ingest-workflow.md` の手順 7b、形は `references/page-conventions.md` の「ソースのページの主張と行き先」にある。ページのひな形（`assets/page.md.template`）にもこのセクションを足した。
- `SCHEMA.md` のひな形に `## Claim destinations` のセクションを足した。`init_wiki.py --upgrade` は、このセクションのない `SCHEMA.md` に、手作業でのマージを案内する。既存の `SCHEMA.md`、`.page-template.md`、ソースのページは書き換えない。既存のソースのページに移行作業は要らない。

## [0.7.1] - 2026-11-09

### Fixed

- TLS を検査する社内ネットワークで、wiki の MCP サーバーが `CONNECTION_CLOSED` で起動しなかった（#30）。uv は既定で OS の証明書ストアを使わないので、社内の CA が署名した PyPI の証明書を検証できず、入口（`wiki_mcp_launch.py`）と中継が依存を取得できなかった。
  - 入口と中継は、`uv sync` が証明書の検証（`invalid peer certificate` など）で失敗したら、`UV_SYSTEM_CERTS=true` で 1 回だけやり直す。`UV_SYSTEM_CERTS` を知らない古い uv（`uv sync --help` で見分ける）では `UV_NATIVE_TLS=true` を使う。直ったら、以後の uv の呼び出しと、中継が用意するサーバーの環境でも使う。利用者が `UV_SYSTEM_CERTS` か `UV_NATIVE_TLS` を設定しているときは、その設定を変えない。OS の証明書ストアを最初から使わないのは、uv がそちらを遅いとしている（特に macOS）ためである。
  - それでも失敗したら、環境が壊れたとみなして入れ直すことをやめ、すぐに止まる。理由と対処（`UV_SYSTEM_CERTS=true`、`uv.toml` の `system-certs = true`、`SSL_CERT_FILE`）を標準エラーと `logs/launch.log` に書く。
- README に「トラブルシューティング」の節を足した。コマンドがエージェントに実行させる `uv run --script` は入口を通らずやり直さないので、ユーザーの環境変数か uv の設定ファイルで設定するよう案内する。スキルと `references/mcp-server.md` にも、証明書のエラーのときに `--system-certs` を付けてやり直すことを書いた。

## [0.7.0] - 2026-11-09

### Added

- lint（`wiki_lint.py` と MCP のツール `lint`）に 4 つの検査を足した。どれも報告するだけで、wiki は直さない。
  - `index_type_mismatch`: 索引の種類の見出し（「ソース」「エンティティ」「コンセプト」「シンセシス」と英語の見出し）の下に、別の `type` のページが並んでいるもの。たとえば「シンセシス」の欄にある `type: "source"` のページである。分割した索引（`indexes/`）はファイル名を種類として読む。読むのは見出しの下のリストの項目の最初の wikilink だけで、冒頭の説明文の例示や要約の中のリンクは数えない。これまで lint は `index.md` を読んでいなかったので、この誤りを見つけられなかった。
  - `unresolved_sources`: frontmatter の `sources:` に、ないページ（`missing`）か、source でないページ（`not_source`）を書いているもの。
  - `missing_raw`: source ページの `raw:` の原本がないもの。相対パスは wiki のルート、wiki の親、ページのディレクトリのどれかを基準にして見つかればよく、rag が書く絶対パスはそのまま確かめる。URL は見ない。
  - `unknown_tags`: `SCHEMA.md` のタグの体系にないタグ。体系は `--allowed-tags`（ツールでは `allowed_tags`）で渡す。渡さないとこの検査はせず、`summary.unknown_tags` は `null` になる。`/wiki:lint` は `SCHEMA.md` の「タグの体系」に並ぶタグを渡す。
- `SCHEMA.md` のひな形の「タグの体系」に、lint がここのタグで体系の外のタグを指摘することを書いた。既存の wiki の `SCHEMA.md` は書き換えない。

### Changed

- `wiki_lint.py --json` とツール `lint` の結果に、上の 4 つのキーと、`summary` の同じ名前の件数が加わった。`summary.unknown_tags` は、検査しなかったときに `null` になる。

## [0.6.1] - 2026-11-07

### Fixed

- MCP の中継とサーバーの起動が速くなった。0.4.2 から、中継とサーバーは起動の最初に、dist-info の `RECORD` の各行を 1 つずつ確かめて、壊れた uv の環境を見つけている。この確かめで、ファイルを 1 つずつ開くのをやめ、site-packages のディレクトリの一覧（`os.scandir`）から大きさを読むようにした。Windows で 0.6.0 と交互に測ると、`initialize` の中央値は 1,290 ms から 1,173 ms に、サーバーが止まっている状態からの `tools/list` は 2,019 ms から 1,757 ms になった。一覧で決められないファイル（`Scripts/` の実行ファイル、シンボリックリンクなど）と大きさの違うファイルは、従来どおり 1 つずつ確かめる。欠けたファイルは、0.6.0 と同じくすべて見つかる。

## [0.6.0] - 2026-11-07

### Changed

- PDF の取り込み（rag が有効な場合）の手順を、rag 0.4.0 の参照ルートの廃止（開発リポジトリの ADR-0013）に合わせた。`register_pdf` の `source` には、ユーザーが指定した PDF の絶対パスを渡す。PDF はどのフォルダにあってもよく、`/rag:setup` で先に登録する案内はやめた。ユーザーが指定していない PDF は登録しない。登録を拒まれたら、理由をそのまま伝える。

### Added

- ソースの要約ページに、PDF の内容の SHA-256 を書くキー `raw_sha256` を足した。`raw` には rag が返す `/` 区切りの絶対パスを書き、どちらも二重引用符で囲んで `\` と `"` をエスケープする。別の PC で同じ PDF を取り込み直すと、`raw_sha256` で同じ出典と分かる。

## [0.5.0] - 2026-11-07

### Changed

- `wiki_lint.py`（位置引数の `<wiki>`）と `wiki_search.py`（`--wiki`）は、wiki を省くと作業領域の設定（`.info-hive/workspace.json`）の `dirs.wiki` を使うようにした。0.3.0 で `setup_wiki.py`、`wiki_graph_lint.py`、`wiki_stats.py`、`wiki_decide.py`、`wiki_evolve.py` に入れたのと同じ動きで、設定がなければ従来どおり `./wiki` を使い、設定が壊れているときは理由を書いて終了コード 1 で止まる。これで wiki を省けるスクリプトは、すべて同じ既定になった。wiki を明示したときは設定を読まない。MCP のツールは wiki を引数で受け取り、設定を読まないので、動きは変わらない。
- `/wiki:lint`、`/wiki:query`、`/wiki:stats` の手順で、`<WIKI_ROOT>` がどの指示にもないときに、`./wiki` の前に作業領域の設定の `dirs.wiki` を見るようにした（`/wiki:graph` と同じ）。`references/lint-workflow.md` と `references/query-workflow.md` にも、省いたときの既定を書いた。
- `wiki_search.py` の `--cache` のヘルプで、既定の置き場所を `<wiki>/.wiki-cache/search-index.json` と書くようにした（`./wiki` の下とは限らないため）。

## [0.4.2] - 2026-11-07

### Fixed

- 0.4.1 の修正は、`uv sync` が失敗する壊れ方（dist-info の `METADATA` の欠け）しか直せなかった。dist-info はそろっていて、モジュールの本体だけが欠けた環境では、`uv sync` が成功するので入れ直さず、MCP のサーバーが起動しないままだった。中継とサーバーは、起動の最初に、各パッケージの dist-info の `RECORD` と照らして欠けたファイルがないかを確かめ、主な依存を import する。壊れていれば終了コード 75 で止まる。入口と中継は、それを見て環境を入れ直し、1 回だけ起動し直す。
- 壊れた環境に 2 つのセッションが同時に入ると、後から来たセッションの `--reinstall` が、先に起動した中継の使っている環境を消すことがあった。入れ直しはスクリプトごとのロック（uv のキャッシュの下に置く）を取ってから行い、ロックを取ったあとで直っていることを確かめたら入れ直さない。
- `uv python find` が失敗したとき、uv の出した理由が入口のログに残らなかった。

## [0.4.1] - 2026-11-07

### Fixed

- uv がサーバーや中継の依存を入れている途中でプロセスが止められると（初めて使うセッションをすぐ閉じたときなど）、uv の環境が作りかけのまま残り、それ以降は MCP のサーバーが起動しなくなっていた（Windows の uv 0.12 で、dist-info の `METADATA` が欠けた）。`.mcp.json` は依存を持たない入口 `wiki_mcp_launch.py` を起動するようにした。入口は中継の環境を、中継はサーバーの環境を `uv sync --script` で確かめ、失敗したら `--reinstall` で 1 回だけ入れ直す。0.4.0 で環境が壊れた人も、更新すれば次の起動で直る。Claude Code が失敗を覚えていてツールが出ないときは、`/mcp` でつなぎ直す（`references/mcp-server.md` の「困ったとき」）。

## [0.4.0] - 2026-11-07

### Added

- 検索のためのローカルの MCP サーバーを追加した（`.mcp.json`、`wiki_mcp_relay.py`、`wiki_mcp_server.py`）。ホストは stdio の中継を起動し、中継は 127.0.0.1 で待ち受けるサーバーを 1 つだけ起動して使う。サーバーはセッションをまたいで残り、要求がない状態が 15 分続くと止まる。起動ごとのトークンと Host ヘッダーを確かめ、応答は JSON で返す。ツールは `query`、`get_page`、`list_index`、`lint`、`stats`、`graph`、`index_chunks`。wiki の Markdown を書き換えるツールは持たない。仕組みと置き場所は `references/mcp-server.md` に書いた。
- MCP のツール `lint`、`stats`、`graph` を追加した。`wiki_lint.py`、`wiki_stats.py`、`wiki_graph_query.py` の関数をサーバーの中で呼び、CLI の `--json` と同じ結果を返す。`lint` は、オントロジーがあれば `wiki_graph_lint.py` の検査の結果も `graph` に入れる（判定モデルの `--decide` は含めない）。オントロジーがないときは、CLI と同じ文面で `disabled` を返す。`stats` は、索引のセクションの数、埋め込みの進み具合、埋め込みモデル、サーバーのコードの版も返す。`graph` は `graph.sqlite` を読み取り専用で開く。グラフの lint のために、サーバーの依存に PyYAML 6.0.3 を足した。
- `wiki_stats.py` に `--json` を追加した。キーは英語のまま。テキストの出力は変えていない。
- サーバーの検索の基盤（`wiki_index.py`）を追加した。埋め込みは cl-nagoya/ruri-v3-130m の ONNX、ベクトル索引は usearch、語彙検索は Tantivy の BM25（wiki の bigram のトークン）で、RRF（k 30、語彙の重み 0.5、候補は片側 200 件）で融合する。索引は wiki の外（Windows は `%LOCALAPPDATA%\info-hive\index\`、macOS と Linux は XDG の data ディレクトリ）に置き、更新の途中で止まっても次に開いたときにそろえ直す。初回は wiki 全体を埋め込むので時間がかかる。そのあいだは埋め込みを裏で進め、語彙検索と埋め込み済みの分で答える。
- rag がチャンクを渡すための `index_chunks` を追加した。collection ごとに wiki とは別の索引に入れ、文書ごとに古い世代の行を消す。語彙検索のトークンは呼び出し側が作る。
- コマンド（`/wiki:query`、`/wiki:lint`、`/wiki:stats`、`/wiki:graph`）と `/wiki:ingest` のグラフの lint は、まず MCP のツール（`query`、`lint`、`stats`、`graph`）を使うようにした。ツールがない、つながらない、返らないときと、CLI にしかない機能（`--backlinks`、`--granularity page`、`--no-embed`、`--decide`）が要るときは、今の CLI に戻る。グラフの作り直し（`wiki_graph_extract.py`）はいつも CLI で行う。使い分けの規則は `SKILL.md` の「MCP のツールと CLI の使い分け」に 1 か所だけ書き、コマンドと手順の文書（`query-workflow.md`、`lint-workflow.md`、`graph-workflow.md`）から参照する。

### Fixed

- macOS と Linux で、中継が起動したサーバーの終了を回収せず、止まったサーバーがゾンビとして残っていた。中継の残るあいだは pid が生きているように見え、要求がなくなって止まったかどうかを外から確かめられなかった。中継は、起動したサーバーの終了を待って回収する。
- `wiki_graph_extract.py` は、形式ごとに書いてはすぐ置き換えていたので、途中の形式で失敗すると新旧の成果物が混ざっていた。選んだ形式の成果物をすべて一時ファイルに書き終えてから、`graph.sqlite` を先頭に置き換えるようにした。`graph.sqlite` を置き換えられなければ、ほかの成果物も前のまま残して、理由を書いて終了コード 1 で止まる。
- 同じ出力先の extract が同時に走ると、成果物が混ざることがあった。OS のファイルロック（`graph/.extract.lock`）で 1 つずつ動かし、ロックを取ってからページを読む。止まった実行の一時ファイルは、10 分より古いかどうかではなくロックを根拠に消す（SQLite の `-journal` を含む）。以前は、止まっていただけの実行の一時ファイルも消せた。`graph/.gitignore` のひな形は、ロックのファイルと `*.tmp-*` も無視する。
- 最上位が `[]`、`false`、`0` のオントロジーを、空のオントロジーとして通していた。extract と lint は終了コード 2 で止まる。
- 作業領域の設定で、未知の役割の値が `null` だと誤りにしていた。新しい版が足した役割かもしれないので、警告だけにした。UTF-8 として読めない設定は、理由を書いて終了コード 1 で止まる。
- wiki を使わない `wiki_evolve.py verify-bundle` と `capture` が、作業領域の設定を読み、設定が壊れていると止まっていた。設定を読まないようにした。
- `references/graph-workflow.md` に、オントロジーの `maps_from` を変えたときも extract を実行することを書いた。

## [0.3.0] - 2026-11-06

### Added

- Web ページの取り込みを追加した。`/wiki:ingest <URL>` で、新しいスクリプト `wiki_fetch.py`（標準ライブラリだけ）がページを取得し、本文を Markdown にして `raw/<スラッグ>.md` に、本文の画像を `raw/assets/<スラッグ>/` に保存する。frontmatter に URL、取得日時、原文の HTML の SHA-256、原文の言語を記録し、同じ内容の再取得では何も書かない。内容が違えば別版のスラッグを提案する。エージェントは残った雑音を削り、日本語でなければ段落ごとに訳して `translated_by` に表示名を入れ、画像の直後に `> <表示名> 補足: ` の説明を書く。手順は `commands/ingest.md` と `references/ingest-workflow.md` の「Web ページ」に書いた（ADR-0004）。
- 知識ページの図表の決まりを `page-conventions.md` の「図表」に書いた。流れや関係は mermaid の図、比較は表、mermaid で描けない図だけ svg を `wiki/assets/` に置き、図表の直後に `> <表示名> 補足: ` の説明を置く（ADR-0005）。取り込みとクエリの手順にも、図表を足す場面を書いた。
- `init_wiki.py`（`--upgrade` を含む）が `wiki/assets/` を作るようにした。
- lint に `missing_assets` を足した。本文の画像の参照先がないか wiki の外を指すときに指摘する。参照先はページのファイルの位置から解決し、wiki のルートを基準に書いた誤り（`assets/x.svg`）には正しい相対パスを添える。
- 作業領域の設定（`.info-hive/workspace.json`）を読む `scripts/wiki_workspace.py` を追加した。カレントディレクトリから親へさかのぼって案内のファイル `.info-hive.json` と設定を探し、サブモジュールの `.git` では止まらない。`..` と絶対パスを拒否し、未知のキーは警告にして残す。
- グラフの 3 つのスクリプトが共有する `scripts/wiki_graph_common.py` を追加した。

### Changed

- lint と統計のページの大きさから、コードフェンス（mermaid を含む）の中身と画像だけの行を除いた。`--suggest-pages` も、図のソースと画像の alt を語の候補にしない。`wiki/assets/` の中のファイルはページとして数えない。
- `setup_wiki.py`、`wiki_graph_lint.py`、`wiki_stats.py`、`wiki_decide.py`（`shortlist`、`contradictions`）、`wiki_evolve.py` は、`--wiki`（または位置引数の `<wiki>`）を省くと、作業領域の設定の `dirs.wiki` を使うようにした。設定がなければ従来どおり `./wiki` を使う。設定が壊れているときは理由を書いて終了コード 1 で止まる。`wiki_lint.py` と `wiki_search.py` はまだ `./wiki` のままである。
- オントロジー（`graph/ontology.yaml`）がないときの扱いを、`wiki_graph_extract.py`、`wiki_graph_lint.py`、`wiki_graph_query.py` でそろえた。どれも何も書き出さずに、同じ文面を標準エラーに書いて終了コード 1 で止まる。以前の extract は空のオントロジーで続け、`graph/` と生成物を作っていた。query は、オントロジーがなければ古い `graph.sqlite` が残っていても答えない（`--db` で DB を明示したときを除く）。
- グラフの手順の正本を `references/graph-workflow.md` にまとめた。取り込み、問い合わせ、lint の手順と `/wiki:graph` は、呼ぶ時点と結果の扱いだけを書き、規約はそこを参照する。

### Fixed

- ADR-0005 の当初の案では svg を `assets/<スラッグ>-NN.svg` と参照するとしていたが、`wiki/<種類>/` のページからはそのパスで表示されない。参照を `../assets/<スラッグ>-NN.svg` に改めた。
- extract を省く条件の記述を直した。以前は「型付きの関係も新しいページもない取り込みでは extract を省く」と書いていたが、extract は本文の wikilink、`sources:`、`raw:` からもエッジを作るので、ページを書き換えたら必ず extract を実行する。
- `wiki_graph_extract.py` は、`graph.sqlite` を先に消してから作り直していたので、途中で止まると問い合わせができなくなっていた。成果物を同じディレクトリの一時ファイルに書いてから置き換えるようにした。Windows でほかのプロセスが DB を開いているときは、少し待ってやり直し、置き換えられなければ前の DB を残してエラーで止まる。`graph/.gitignore` のひな形は一時ファイル（`*.tmp`）を無視する。

## [0.2.1] - 2026-11-06

### Changed

- ページの大きさの上限を引き上げた。ソフト上限を 10,000 文字または 100 行、ハード上限を 25,000 文字または 200 行にした（従来は 4,000 文字／400 行、8,000 文字／800 行）。`SCHEMA.md` のテンプレート、`wiki_lint.py` と `wiki_stats.py` の既定値、関連する文書をそろえた。既存の wiki は `SCHEMA.md` の値を書き換えるか、`wiki_lint.py` に `--soft-char-cap`・`--hard-char-cap`・`--soft-cap`・`--hard-cap` を渡して従来の値を保てる。

### Fixed

- クエリの回答をシンセシスのページとして保存する手順（`references/query-workflow.md`）の frontmatter の例に、lint が必須とする `title`、`created`、`updated` と、source 以外のページに求める `sources` を足した。例のとおり保存すると `missing_frontmatter` が出ていた。`sources`（ソースの要約ページだけ）と `sources_consulted`（参照したページすべて）の使い分けを、`page-conventions.md` と `SCHEMA.md` のテンプレートにも書いた。

## [0.2.0] - 2026-11-05

### Added

- PDF の取り込みで、任意の `rag@info-hive` に `register_pdf` を明示的に呼ぶ手順を追加した。コマンドと自然言語の両方で使い、受付と完了を区別する。PDF 原本はコピーせずに参照し、処理済みの Markdown を読んでから要約ページを作る。README と `docs/commands.md` にも、この流れを書いた。

## [0.1.1] - 2026-11-04

### Changed

- **互換性のない変更:** `/wiki:init` と `/wiki:upgrade` の `--global` を `--user` に改名した。`--global` は受け付けず、渡されたときは何も実行せずに `--user` を使うよう伝える。作られる場所（`~/info-hive/wiki/` と `~/info-hive/raw/`）は変わらない。文書の「個人用のグローバルな wiki」も「ユーザースコープの wiki」に改めた。エージェントのグローバルな指示と、スキルのグローバルなインストールの呼び方は変えていない。
- `/wiki:help` の一覧を、コードブロックのプレーンテキストではなく、書式付きの Markdown で表示するようにした。コマンド名は太字のインラインコード、引数の書式はインラインコードにするので、`<質問>` の山かっこや `|` も崩れない。`--json` の出力は変わらない。

## [0.1.0] - 2026-11-04

移植元 praneybehl/llm-wiki-plugin v4.0.1 を日本語の wiki 向けに移植した、最初のリリース。移植元のバージョンごとの変更履歴は [docs/upstream-changelog.md](docs/upstream-changelog.md) にある。

### Added

- `/wiki:help` を追加した。`commands/*.md` の frontmatter から、コマンド名・引数・説明・引数の説明の一覧を `wiki_help.py` で生成して表示する。引数の説明は、各コマンドの frontmatter に足した `argument-description` に書く。
- `wiki_lint.py` は、`created`・`updated`・`ingested` が `YYYY-MM-DD` の形でなければ `non_iso_dates` として指摘する。
- `wiki_lint.py` は、二重引用符で囲まれていない文字列の値を `unquoted_strings` として指摘する。
- `wiki_encoding.py` を加えた。テキストのソースの文字コード（UTF-8、Shift_JIS（CP932）、EUC-JP、ISO-2022-JP など）を判定し、`--output` で UTF-8 に変換したものを別のファイルに書き出す。元のファイルは書き換えない。`/wiki:ingest` の手順に加えた。

### Changed

- 移植元 [praneybehl/llm-wiki-plugin](https://github.com/praneybehl/llm-wiki-plugin) v4.0.1（コミット `2d24ccf`）を起点に、プラグイン名を `wiki`、スキル名とマーケットプレイス名を `info-hive` にした。コマンドは `commands/` の直下に置き、`/wiki:<名前>` の形で起動する。スキルのディレクトリを `skills/llm-wiki/` から `skills/info-hive/` に、判定レイヤーの環境変数を `LLM_WIKI_DECISIONS*` から `INFO_HIVE_DECISIONS*` に、モデルのキャッシュを `~/.cache/llm-wiki/` から `~/.cache/info-hive/` に変えた。移植元との関係は `UPSTREAM.md` に記録している。
- `/wiki:init --global` の既定を、`~/wiki/` と `~/wiki/raw/` から `~/info-hive/wiki/` と `~/info-hive/raw/` に変えた。標準のグローバル構成の初期化とアップグレードは、ベースディレクトリに `~/info-hive` を渡すだけでよい（`--wiki-dir` と `--raw-dir` は既定の `wiki` と `raw`）。`init_wiki.py` は、ベースディレクトリがなくても親のディレクトリがあれば作る。親もないときは止める。以前の既定で作った wiki は移動せず、今までどおり `--wiki-dir wiki --raw-dir wiki/raw` で指す。
- frontmatter の文字列の値は、配列の要素も含めて二重引用符で囲む決まりにした（`type: "source"`、`tags: ["税制", "間接税"]`）。日付と数値は囲まない。lint・検索・統計の frontmatter の解析を `wiki_text.py` の共通のパーサーにまとめ、引用符の中のカンマで配列を分けず、引用符の外の行末コメントを捨て、`\"` と `\\` を戻すようにした。評価ループが書くページとテンプレートも引用符つきにした。検索のパースキャッシュは自動で作り直される。
- リポジトリを `to884/info-hive` から `to884/info-hive-wiki` に移した。旧リポジトリはアーカイブする。
- マーケットプレイスの定義（`.claude-plugin/marketplace.json`）を、別のリポジトリ [to884/info-hive-marketplace](https://github.com/to884/info-hive-marketplace) に移した。マーケットプレイス名は `info-hive` のままで、`/plugin install wiki@info-hive` で入れる。
- 旧リポジトリをマーケットプレイスとして追加していた場合は、`/plugin marketplace remove info-hive` のあとに `/plugin marketplace add to884/info-hive-marketplace` と `/plugin install wiki@info-hive` を実行し直す。
- `npx skills add` と `git clone` で指定するリポジトリも `to884/info-hive-wiki` になる。
- 検索のトークン化を日本語に対応させた。NFKC で正規化して小文字にしたあと、文字種の境界で区切り、英数字は語単位、かなと漢字は文字 bigram にする。漢字は 1 文字単位のトークンも加え、「税」のような単漢字のクエリが「消費税」に当たるようにした。英数字だけのテキストのトークンは変わらない。
- 日本語のトークンを含むクエリでは、IDF で重みを付けたクエリの被覆率が 0.25 に満たないページを、語彙ベースの結果から外すようにした。文字 bigram が関係のないページと偶然に一致するのを防ぐためである。
- 既定の埋め込みモデルを、英語専用の `BAAI/bge-small-en-v1.5` から多言語の `intfloat/multilingual-e5-small`（fp16 の ONNX、約 0.25 GB）に替えた。クエリとセクションに `query: ` と `passage: ` の接頭辞を付け、コサイン距離の上限を 0.35 から 0.193 にした。既存の `embeddings.sqlite` は、次の検索で自動的に作り直される。環境変数 `INFO_HIVE_EMBED_MODEL` でモデルを選べるようにした。大きいモデルとして `intfloat/multilingual-e5-large`（約 2.24 GB、距離の上限 0.19）も選べる。
- fastembed を読み込む前に `HF_HUB_DISABLE_SHARED_BLOBS=1` を設定するようにした。huggingface_hub 1.33 の共有の置き場所では、重みを `model.onnx_data` に分けたモデルを onnxruntime 1.31 が読み込めないためである。
- 語彙側のトークンが 0 個のクエリでも、ハイブリッド検索では埋め込み検索を行うようにした。
- `wiki_lint.py` は、空白を除いた文字数でもページの大きさを判定するようにした。目安は 4,000 文字、上限は 8,000 文字で（`--soft-char-cap`、`--hard-char-cap`）、行数の上限と、どちらかを超えたら指摘する。`--json` の `oversized_hard`・`oversized_soft` の各項目に `chars` を加えた。
- `wiki_lint.py --suggest-pages` に、3 文字以上のカタカナ語と、3 文字以上の漢字の複合語を加えた。
- `wiki_stats.py` に文字数を加え、大きいページを文字数の多い順に並べるようにした。
- D2 の文分割を「。！？」に対応させ、引用の照合で日本語の文字どうしの間の改行と空白を吸収するようにした。英数字の間の空白は区別する。評価ループの引用と答えの照合も同じ規則にした。
- 評価ループが wiki に書くページ、index と log の行、`PURPOSE.md` の文面を日本語にした。
- スクリプトの出力、`--help`、lint の報告を日本語にした。表は全角の文字を 2 桁と数えてそろえる。`--json` のキーは英語のまま残している。
- SKILL.md、コマンド、references、テンプレートを、日本語の wiki の運用として書き直した。
  - SKILL.md の起動条件（`description`）に、日本語の依頼の例を加えた。
  - スラッグには日本語を使ってよいとし、英字は小文字にしてハイフンでつなぐこと、空白と使えない記号、NFC での照合を書いた。
  - ページの大きさを、空白を除いた文字数（目安 4,000 文字、上限 8,000 文字）と行数で書くようにした。語数の目安はやめた。
  - 引用の書き方（本文では「」で囲む、グラフの `evidence` と判定・評価ループの根拠はソースの文をそのまま写す）と、照合の規則を書いた。
  - ログ行の説明とサブ行を日本語で書くようにした。操作名（`ingest`、`query`、`lint` など）は英語のまま残す。
  - 英語の wiki を前提にした例文を、日本語の wiki の例に替えた。
  - 埋め込みモデルの記述を `intfloat/multilingual-e5-small` に直し、日本語のクエリを分かち書きせずに渡せることを書いた。
  - `/wiki:upgrade` に、移植元の llm-wiki で作った wiki から移るときの注意点を書いた。
- テンプレートの見出しを日本語にした。`SCHEMA.md` のアップグレード検出に使う目印（`## Skill evolution`、`## Optional graph metadata`、`## Graph layer`、`## Retrieval` と、`Graph lint + extract`、`- Semantic backend: local FastEmbed + sqlite-vec` の行）だけは英語のまま残している。評価ループが `index.md` と `log.md` を新しく作るときの見出しも、テンプレートに合わせた。
- オントロジーのテンプレートの述語の説明を日本語にした。述語名とノードの種類は変えていない。

### Removed

- ホスト型 wiki を提供しない。移植元の `server/`（Cloudflare Worker）、`wiki_remote.py`、`references/hosted-mode.md`、`/wiki:migrate`、`/wiki:init --hosted`、`eval/remote/`、`docs/hosted.md` と、環境変数 `LLM_WIKI_URL`・`LLM_WIKI_TOKEN`・`LLM_WIKI_CONFIG` を削った。wiki はローカルのディレクトリだけを扱う。
- ドキュメントのサイト（VitePress）と GitHub Pages へのデプロイのワークフローを削り、`docs/` の Markdown だけを残した。
- 移植元の調査メモ `research/` と、移植元の英語の評価セットでの測定記録（`eval/decisions/results-*.json`）は取り込んでいない。
- Paperclip 連携は移植元の v4.0.0 で削除されており、info-hive にもない。

[Unreleased]: https://github.com/to884/info-hive-wiki/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/to884/info-hive-wiki/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/to884/info-hive-wiki/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/to884/info-hive-wiki/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/to884/info-hive-wiki/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/to884/info-hive-wiki/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/to884/info-hive-wiki/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/to884/info-hive-wiki/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/to884/info-hive-wiki/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/to884/info-hive-wiki/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/to884/info-hive-wiki/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/to884/info-hive-wiki/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/to884/info-hive-wiki/releases/tag/v0.1.0
